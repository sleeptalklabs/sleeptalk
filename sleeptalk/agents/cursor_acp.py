"""Cursor ACP client. Nested cursor/* outcomes, not generic -32601."""

from __future__ import annotations

import asyncio
import os
import signal
import tempfile
from contextlib import suppress
from collections.abc import AsyncIterator, Callable
from typing import Any, Mapping

from sleeptalk.agent_environment import agent_environment

from sleeptalk.adapter import (
    AgentCapabilityManifest,
    AgentControl,
    AnswerClarification,
    CapabilitySupport,
    ClarificationRequested,
    ControlAccepted,
    ControlRejected,
    ControlResult,
    PermissionDecision,
    PermissionRequested,
    ResolvePermission,
    SupportLevel,
    VoiceSessionCapability,
    WorkerCapability,
    publish_event,
)
from sleeptalk.agents.acp import AcpClient, AcpError
from sleeptalk.agents.cursor_voice import wrap_cursor_voice_sink
from sleeptalk.voice_skills import discover_skills, rewrite_spoken_skill
from sleeptalk.agents.profiles.cursor import (
    CURSOR_CLI_LOGIN_MESSAGE,
    ASK_QUESTION,
    CREATE_PLAN,
    CURSOR_PROFILE,
    CURSOR_PROFILE_NO_EXTENSIONS,
    GENERATE_IMAGE,
    KNOWN_METHODS,
    TASK,
    UPDATE_TODOS,
    ask_question_answered,
    ask_question_skipped,
    auth_method_ids,
    cancelled_outcome,
    create_plan_accepted,
    create_plan_rejected,
    first_question_answers,
    plan_prompt,
    question_prompt,
)


class CursorAcpClient(AcpClient):
    def __init__(
        self,
        *,
        cwd: str | None = None,
        session_id: str | None = None,
        model: str = "",
        permission_mode: str = "auto",
        handle_extensions: bool = True,
        env: Mapping[str, str] | None = None,
        keepalive_interval_s: float = 0.0,
    ) -> None:
        profile = CURSOR_PROFILE if handle_extensions else CURSOR_PROFILE_NO_EXTENSIONS
        super().__init__(
            profile,
            cwd=cwd,
            session_id=session_id,
            permission_mode=permission_mode,
            env=env,
            keepalive_interval_s=keepalive_interval_s,
        )
        self.model = model
        self._model_session = ""
        self._isolated_home = None
        self._native_isolated = self.permission_mode in {"pending", "deny"} and bool(self._subprocess_env.get("CURSOR_API_KEY")) and not session_id
        self.handle_extensions = handle_extensions
        self.agent_capabilities = _cursor_manifest(handle_extensions=handle_extensions)
        self._open_cursor: dict[str, dict[str, Any]] = {}

    async def respond_streaming(
        self,
        text: str,
        timeout: float = 300.0,
        *,
        on_event: Callable[..., Any] | None = None,
    ) -> AsyncIterator[str]:
        text = rewrite_spoken_skill(text, discover_skills(self.cwd))
        on_event = wrap_cursor_voice_sink(on_event)
        async for sentence in super().respond_streaming(
            text, timeout, on_event=on_event
        ):
            yield sentence

    async def _ensure_started(self) -> None:
        if self._native_isolated:
            from sleeptalk.agents.cursor_isolation import require_hook_free_workspace
            require_hook_free_workspace(self.cwd)
        try:
            async with asyncio.timeout(30 if self._native_isolated else None):
                await super()._ensure_started()
                if self.model and self._model_session != self._session_id:
                    selected = await self._request("session/set_config_option", {
                        "sessionId": self._session_id, "configId": "model", "value": self.model,
                    })
                    options = selected.get("configOptions")
                    if not isinstance(options, list) or not any(
                        isinstance(item, dict) and item.get("id") == "model"
                        and item.get("currentValue") == self.model for item in options
                    ):
                        raise AcpError("Cursor did not confirm the selected model")
                    self._model_session = self._session_id
        except BaseException:
            if self._native_isolated or self.model:
                await self._terminate()
            raise

    async def _spawn(self, args: list[str], env: dict[str, str] | None = None) -> None:
        if not self._native_isolated:
            await super()._spawn(args, env)
            return
        from sleeptalk.agents import cursor_isolation
        self._isolated_home = tempfile.TemporaryDirectory(prefix="sleeptalk-cursor-")
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *cursor_isolation.command(args[0]), cwd=self._isolated_home.name,
                env=cursor_isolation.environment(self._subprocess_env, self._isolated_home.name),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,
            )
            self._reader_task = asyncio.create_task(self._read_stdout(), name=self.reader_task_name)
        except BaseException:
            await self._terminate()
            raise

    async def _terminate(self) -> None:
        group = self._proc.pid if self._isolated_home is not None and self._proc is not None else None
        if group is not None:
            with suppress(ProcessLookupError):
                os.killpg(group, signal.SIGTERM)
        try:
            with suppress(ProcessLookupError):
                await super()._terminate()
        finally:
            if group is not None:
                with suppress(ProcessLookupError):
                    os.killpg(group, signal.SIGKILL)
            if self._isolated_home is not None:
                self._isolated_home.cleanup()
                self._isolated_home = None

    async def _before_spawn(self, binary: str) -> None:
        if self._native_isolated:
            return
        # `authenticate` on a logged-out CLI blocks on a browser flow that
        # nobody is watching, so ask the CLI first. Only the real Cursor
        # binary understands `status`; anything else (test fakes) is skipped.
        if os.path.basename(binary) not in _CURSOR_CLI_NAMES:
            return
        output = await _run_status(binary, self.cwd, env=self._subprocess_env)
        if "not logged in" in output.lower():
            raise AcpError(CURSOR_CLI_LOGIN_MESSAGE)

    async def _after_initialize(self, init: dict[str, Any]) -> None:
        if self._native_isolated:
            return
        if "cursor_login" not in auth_method_ids(init):
            return
        try:
            await self._request("authenticate", {"methodId": "cursor_login"})
        except AcpError as exc:
            self._diag(
                "cursor_auth_failed",
                f"cursor_login authenticate failed ({exc}); continuing if CLI is already logged in",
            )

    async def _handle_permission(self, req_id: Any, params: Mapping[str, Any]) -> None:
        if not self._native_isolated:
            await super()._handle_permission(req_id, params)
            return
        options = [item for item in (params.get("options") or []) if isinstance(item, dict)
                   and item.get("optionId") in {"allow-once", "reject-once"}]
        if not any(item.get("optionId") == "reject-once" for item in options):
            await self._send_json({"jsonrpc": "2.0", "id": req_id, "result": {"outcome": {"outcome": "cancelled"}}})
            return
        await super()._handle_permission(req_id, {**params, "options": options,
            "reason": "Allow the pending agent tool operation?"})

    async def _resolve_permission(self, command: ResolvePermission) -> ControlResult:
        if self._native_isolated and command.decision not in {PermissionDecision.DENY, PermissionDecision.ALLOW_ONCE}:
            return ControlRejected(reason="Cursor isolated sessions support allow once or deny.", retryable=False)
        return await super()._resolve_permission(command)

    async def _handle_agent_request(self, message: dict[str, Any]) -> None:
        method = str(message.get("method") or "")
        if method.startswith("cursor/"):
            await self._handle_cursor_request(message)
            return
        await super()._handle_agent_request(message)

    async def _route_message(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        if (
            method in {UPDATE_TODOS, TASK, GENERATE_IMAGE}
            and "id" not in message
        ):
            self._note_cursor_notification(str(method), message.get("params") or {})
            return
        await super()._route_message(message)

    async def _handle_cursor_request(self, message: dict[str, Any]) -> None:
        method = str(message.get("method") or "")
        req_id = message["id"]
        params = message.get("params") or {}
        if not isinstance(params, dict):
            params = {}
        if not self.handle_extensions or method not in KNOWN_METHODS:
            self._diag(
                "unknown_extension",
                f"cancelled Cursor extension {method}",
                namespace="cursor",
            )
            await self._send_json(
                {"jsonrpc": "2.0", "id": req_id, "result": cancelled_outcome()}
            )
            return
        if method == ASK_QUESTION:
            await self._handle_ask_question(req_id, params)
            return
        if method == CREATE_PLAN:
            await self._handle_create_plan(req_id, params)
            return
        await self._send_json(
            {"jsonrpc": "2.0", "id": req_id, "result": cancelled_outcome()}
        )

    async def _handle_ask_question(self, req_id: Any, params: Mapping[str, Any]) -> None:
        request_id = str(req_id)
        if self.permission_mode == "pending":
            # Reported only when the caller must answer (see _handle_create_plan).
            self._open_cursor[request_id] = {
                "kind": ASK_QUESTION,
                "rpc_id": req_id,
                "params": dict(params),
            }
            publish_event(
                self._on_event,
                ClarificationRequested(
                    request_id=request_id,
                    prompt=question_prompt(params),
                    choices=_question_choices(params),
                ),
            )
            return
        if self.permission_mode == "deny":
            result = ask_question_skipped("permission_mode=deny")
        else:
            result = ask_question_answered(first_question_answers(params))
        await self._send_json({"jsonrpc": "2.0", "id": req_id, "result": result})

    async def _handle_create_plan(self, req_id: Any, params: Mapping[str, Any]) -> None:
        request_id = str(req_id)
        if self.permission_mode == "pending":
            # Reported only when the caller must answer; auto and deny settle
            # it below without asking anyone.
            self._open_cursor[request_id] = {
                "kind": CREATE_PLAN,
                "rpc_id": req_id,
                "params": dict(params),
            }
            publish_event(
                self._on_event,
                PermissionRequested(
                    request_id=request_id,
                    prompt=plan_prompt(params),
                    options=frozenset(
                        {PermissionDecision.ALLOW_ONCE, PermissionDecision.DENY}
                    ),
                    tool_call_id=str(params.get("toolCallId") or "") or None,
                ),
            )
            return
        if self.permission_mode == "deny":
            result = create_plan_rejected("permission_mode=deny")
        else:
            result = create_plan_accepted()
        await self._send_json({"jsonrpc": "2.0", "id": req_id, "result": result})

    def _note_cursor_notification(self, method: str, params: Mapping[str, Any]) -> None:
        self._diag(
            "cursor_notification",
            f"received {method}",
            toolCallId=str(params.get("toolCallId") or ""),
        )

    async def control(self, command: AgentControl) -> ControlResult:
        if isinstance(command, AnswerClarification):
            return await self._answer_question(command)
        if isinstance(command, ResolvePermission):
            cursor = self._open_cursor.get(command.request_id)
            if cursor and cursor["kind"] == CREATE_PLAN:
                return await self._resolve_plan(command)
            return await super().control(command)
        return await super().control(command)

    async def _answer_question(self, command: AnswerClarification) -> ControlResult:
        open_req = self._open_cursor.pop(command.request_id, None)
        if open_req is None or open_req["kind"] != ASK_QUESTION:
            return ControlRejected(reason="unknown Cursor question", retryable=False)
        params = open_req["params"]
        answers = _answers_from_text(params, command.answer)
        try:
            await self._send_json(
                {
                    "jsonrpc": "2.0",
                    "id": open_req["rpc_id"],
                    "result": ask_question_answered(answers),
                }
            )
        except (AcpError, BrokenPipeError, ConnectionResetError, OSError) as exc:
            return ControlRejected(reason=str(exc), retryable=True)
        return ControlAccepted(request_id=command.request_id)

    async def _resolve_plan(self, command: ResolvePermission) -> ControlResult:
        open_req = self._open_cursor.pop(command.request_id, None)
        if open_req is None:
            return ControlRejected(reason="unknown Cursor plan", retryable=False)
        if command.decision is PermissionDecision.DENY:
            result = create_plan_rejected("denied")
        else:
            result = create_plan_accepted()
        try:
            await self._send_json(
                {"jsonrpc": "2.0", "id": open_req["rpc_id"], "result": result}
            )
        except (AcpError, BrokenPipeError, ConnectionResetError, OSError) as exc:
            return ControlRejected(reason=str(exc), retryable=True)
        return ControlAccepted(request_id=command.request_id)


_CURSOR_CLI_NAMES = frozenset({"agent", "cursor-agent"})
_STATUS_TIMEOUT_S = 10.0


async def _run_status(binary: str, cwd: str, *, env: Mapping[str, str] | None = None) -> str:
    """Output of `agent status`, or "" when it cannot be asked (never blocks)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            binary,
            "status",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=cwd,
            env=agent_environment(env),
        )
    except OSError:
        return ""
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), _STATUS_TIMEOUT_S)
    except asyncio.TimeoutError:
        proc.kill()
        return ""
    return out.decode("utf-8", errors="replace")


def _cursor_manifest(*, handle_extensions: bool) -> AgentCapabilityManifest:
    supported = CapabilitySupport(SupportLevel.SUPPORTED)
    unsupported = CapabilitySupport(SupportLevel.UNSUPPORTED)
    return AgentCapabilityManifest(
        contract_version=1,
        structured_events=supported,
        tool_lifecycle=supported,
        permissions=supported,
        clarification=(
            supported
            if handle_extensions
            else CapabilitySupport(
                SupportLevel.UNAVAILABLE, reason="Cursor extensions disabled"
            )
        ),
        session_identity=unsupported,
        status=unsupported,
        usage=unsupported,
        steering=unsupported,
        targeted_cancellation=unsupported,
        workers=WorkerCapability(SupportLevel.UNSUPPORTED),
        voice_sessions=VoiceSessionCapability(SupportLevel.UNSUPPORTED),
    )


def _question_choices(params: Mapping[str, Any]) -> tuple[str, ...]:
    choices: list[str] = []
    for question in params.get("questions") or []:
        if not isinstance(question, Mapping):
            continue
        for option in question.get("options") or []:
            if isinstance(option, Mapping):
                label = str(option.get("label") or option.get("id") or "")
                if label:
                    choices.append(label)
    return tuple(choices)


def _answers_from_text(
    params: Mapping[str, Any], answer: str
) -> list[dict[str, Any]]:
    needle = answer.strip().lower()
    answers: list[dict[str, Any]] = []
    for question in params.get("questions") or []:
        if not isinstance(question, Mapping):
            continue
        selected: list[str] = []
        for option in question.get("options") or []:
            if not isinstance(option, Mapping):
                continue
            token = str(option.get("id") or "")
            label = str(option.get("label") or "")
            if needle in {token.lower(), label.lower()}:
                selected.append(token)
        if not selected:
            first = (question.get("options") or [None])[0]
            if isinstance(first, Mapping) and first.get("id"):
                selected.append(str(first["id"]))
        answers.append(
            {
                "questionId": str(question.get("id") or ""),
                "selectedOptionIds": selected,
            }
        )
    return answers
