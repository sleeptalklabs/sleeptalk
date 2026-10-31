"""Native Codex adapter over `codex app-server` JSON-RPC stdio."""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import os
import re
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

from sleeptalk.adapter import (
    AgentCapabilityManifest,
    AgentControl,
    AgentError,
    AgentRunState,
    AssistantDelta,
    Cancel,
    CapabilitySupport,
    ControlAccepted,
    ControlRejected,
    ControlResult,
    PermissionDecision,
    PermissionRequested,
    ResolvePermission,
    SessionIdentified,
    StatusChanged,
    Steer,
    SupportLevel,
    ThinkingStarted,
    ToolEvent,
    ToolPhase,
    UsageReported,
    VoiceSessionCapability,
    WorkerCapability,
    publish_event,
    workbench_event,
)
from sleeptalk.streaming import (
    DEFAULT_TOOL_VERBS,
    KEEPALIVE,
    DeadlineClock,
    EventGate,
    SentenceStreamer,
    StdioJsonRpcProcess,
    ToolVocab,
    await_activity,
    narrate_tool,
)
from sleeptalk.text import extract_speakable_text as _extract_speakable_text
from sleeptalk.harness_versions import inventory, install_spec

_SCHEMA_DIR = Path(__file__).with_name("codex_app_server_schema")
SUPPORTED_CODEX_VERSION = (_SCHEMA_DIR / "VERSION").read_text(encoding="utf-8").strip()
_VERSION_RE = re.compile(r"/(\d+\.\d+\.\d+)")
CLIENT_NAME = "sleeptalk"
CLIENT_TITLE = "Sleeptalk"
CLIENT_VERSION = "0.1.0"

STABLE_METHODS = frozenset(
    {
        "initialize",
        "initialized",
        "thread/start",
        "thread/resume",
        "turn/start",
        "turn/steer",
        "turn/interrupt",
    }
)
EXPERIMENTAL_METHODS = frozenset(
    {
        "thread/turns/list",
        "thread/items/list",
        "thread/backgroundTerminals/list",
        "thread/backgroundTerminals/clean",
        "thread/backgroundTerminals/terminate",
        "process/spawn",
        "process/writeStdin",
        "process/resizePty",
        "process/kill",
        "experimentalFeature/list",
        "experimentalFeature/enablement/set",
        "environment/info",
        "collaborationMode/list",
        "tool/requestUserInput",
    }
)
EXPERIMENTAL_FIELDS = frozenset(
    {
        "dynamicTools",
        "historyMode",
        "parentThreadId",
        "ancestorThreadId",
        "additionalPermissions",
        "runtimeWorkspaceRoots",
        "multiAgentMode",
    }
)

_SUPPORTED = CapabilitySupport(SupportLevel.SUPPORTED)
_UNSUPPORTED = CapabilitySupport(SupportLevel.UNSUPPORTED)

CODEX_APP_SERVER_MANIFEST = AgentCapabilityManifest(
    contract_version=1,
    structured_events=_SUPPORTED,
    tool_lifecycle=_SUPPORTED,
    permissions=_SUPPORTED,
    clarification=_UNSUPPORTED,
    session_identity=_SUPPORTED,
    status=_SUPPORTED,
    usage=_SUPPORTED,
    steering=_SUPPORTED,
    targeted_cancellation=_SUPPORTED,
    workers=WorkerCapability(SupportLevel.UNSUPPORTED),
    voice_sessions=VoiceSessionCapability(SupportLevel.UNSUPPORTED),
)

TOOL_VOCAB = ToolVocab(
    kinds={
        "commandexecution": "bash",
        "filechange": "edit",
        "mcptoolcall": "mcp",
        "dynamictoolcall": "other",
    },
    subject_args={
        "bash": ("command",),
        "edit": ("path", "file_path"),
        "mcp": ("tool", "name"),
        "other": ("tool", "name"),
    },
    verbs={**DEFAULT_TOOL_VERBS, "mcp": "calling", "other": "calling"},
)

_PERMISSION_TO_DECISION = {
    PermissionDecision.DENY: "decline",
    PermissionDecision.ALLOW_ONCE: "accept",
    PermissionDecision.ALLOW_SESSION: "acceptForSession",
}


class CodexAppServerError(RuntimeError):
    pass


class CodexSchemaError(CodexAppServerError):
    pass


def parse_codex_version(user_agent: str) -> str:
    match = _VERSION_RE.search(user_agent)
    if match:
        return match.group(1)
    return user_agent.strip()


def reject_experimental_params(params: dict[str, Any], *, experimental: bool) -> None:
    if experimental:
        return
    found = sorted(key for key in params if key in EXPERIMENTAL_FIELDS)
    if found:
        raise CodexAppServerError(
            f"{found[0]} requires experimentalApi capability"
        )


def check_supported_schema(user_agent: str) -> str:
    version = parse_codex_version(user_agent)
    if version not in inventory()["harnesses"]["codex"]["schemas"]:
        raise CodexSchemaError(
            f"installed Codex {version} does not match supported schema "
            f"{SUPPORTED_CODEX_VERSION}. Generate schemas with "
            "`codex app-server generate-json-schema` and update the pin."
        )
    return version


class CodexAppServerClient(StdioJsonRpcProcess):
    """AgentAdapter for `codex app-server` over stdio JSONL."""

    agent_capabilities = CODEX_APP_SERVER_MANIFEST
    reader_task_name = "codex-app-server-reader"

    def __init__(
        self,
        *,
        cwd: str | None = None,
        name: str = "sleeptalk",
        model: str = "",
        permission_mode: str = "",
        resume_thread_id: str = "",
        codex_bin: str = "",
        experimental_api: bool = False,
        keepalive_interval_s: float = 0.0,
        env: dict[str, str] | None = None,
    ) -> None:
        super().__init__(
            env=env,
            cwd=cwd or os.getcwd(),
            error_cls=CodexAppServerError,
            binary="codex",
            install_hint=(
                "codex binary not found. Install the Codex CLI "
                f"(npm i -g {install_spec('codex')}) and keep app-server on stdio."
            ),
        )
        self.name = name
        self.model = model
        self.permission_mode = permission_mode
        self.resume_thread_id = resume_thread_id
        self.codex_bin = codex_bin
        self.experimental_api = experimental_api
        self.keepalive_interval_s = keepalive_interval_s
        self._lock = asyncio.Lock()
        self._event_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._abort: asyncio.Event | None = None
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._approvals: dict[str, dict[str, Any]] = {}
        self._req_counter = itertools.count(1)
        self.upstream_session_id = ""
        self._turn_id = ""
        self._identified = False
        self._user_agent = ""
        self._streamed_items: set[str] = set()

    async def _ensure_started(self) -> None:
        if self._proc is not None and self._proc.returncode is None:
            return
        if self._closed:
            raise CodexAppServerError("Codex app-server client is closed.")
        await self._spawn(self._launch_args())
        await self._handshake()
        await self._ensure_thread()

    def _launch_args(self) -> list[str]:
        binary = self._resolve_binary(self.codex_bin)
        self._check_binary(binary)
        return [binary, "app-server"]

    async def _handshake(self) -> None:
        capabilities: dict[str, Any] = {"experimentalApi": False}
        if self.experimental_api:
            capabilities["experimentalApi"] = True
        result = await self._rpc(
            "initialize",
            {
                "clientInfo": {
                    "name": CLIENT_NAME,
                    "title": CLIENT_TITLE,
                    "version": CLIENT_VERSION,
                },
                "capabilities": capabilities,
            },
        )
        user_agent = ""
        if isinstance(result, dict):
            user_agent = str(result.get("userAgent") or "")
        self._user_agent = user_agent
        check_supported_schema(user_agent)
        await self._notify("initialized", {})

    async def _ensure_thread(self) -> None:
        if self.upstream_session_id:
            return
        if self.resume_thread_id:
            result = await self._rpc(
                "thread/resume", {"threadId": self.resume_thread_id}
            )
        else:
            params: dict[str, Any] = {"cwd": self.cwd}
            if self.model:
                params["model"] = self.model
            reject_experimental_params(params, experimental=self.experimental_api)
            result = await self._rpc("thread/start", params)
        thread = result.get("thread") if isinstance(result, dict) else None
        thread_id = ""
        if isinstance(thread, dict):
            thread_id = str(thread.get("id") or "")
        if not thread_id:
            raise CodexAppServerError("Codex thread handshake returned no thread id.")
        self.upstream_session_id = thread_id

    async def _route_message(self, message: dict[str, Any]) -> None:
        req_id = message.get("id")
        if req_id is not None and ("result" in message or "error" in message):
            future = self._pending.pop(str(req_id), None)
            if future is not None and not future.done():
                future.set_result(message)
            return
        method = str(message.get("method") or "")
        if req_id is not None and method:
            self._approvals[str(req_id)] = message
        await self._event_queue.put(message)

    async def _rpc(self, method: str, params: dict[str, Any] | None = None) -> Any:
        if method in EXPERIMENTAL_METHODS and not self.experimental_api:
            raise CodexAppServerError(
                f"{method} requires experimentalApi capability"
            )
        req_id = next(self._req_counter)
        future: asyncio.Future[dict[str, Any]] = (
            asyncio.get_running_loop().create_future()
        )
        self._pending[str(req_id)] = future
        payload: dict[str, Any] = {"method": method, "id": req_id}
        if params is not None:
            payload["params"] = params
        await self._send_json(payload)
        try:
            message = await asyncio.wait_for(future, timeout=20.0)
        except TimeoutError as exc:
            self._pending.pop(str(req_id), None)
            raise CodexAppServerError(f"Codex {method} timed out.") from exc
        if message.get("error"):
            error = message["error"]
            if isinstance(error, dict):
                raise CodexAppServerError(str(error.get("message") or error))
            raise CodexAppServerError(str(error))
        return message.get("result")

    async def _notify(self, method: str, params: dict[str, Any]) -> None:
        await self._send_json({"method": method, "params": params})

    async def _reply(self, req_id: Any, result: Any) -> None:
        await self._send_json({"id": req_id, "result": result})

    def _auto_approve(self) -> bool:
        return self.permission_mode in {"auto", "skip", "never"}

    async def respond(self, text: str, timeout: float = 300.0) -> str:
        parts: list[str] = []
        async for sentence in self.respond_streaming(text, timeout=timeout):
            parts.append(sentence)
        return _extract_speakable_text(" ".join(parts))

    async def respond_streaming(
        self,
        text: str,
        timeout: float = 300.0,
        on_event: Callable[..., Any] | None = None,
    ) -> AsyncIterator[str]:
        abort_event = asyncio.Event()
        async with self._lock:
            await self._ensure_started()
            while not self._event_queue.empty():
                try:
                    self._event_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
            self._abort = abort_event
            self._streamed_items.clear()
            params: dict[str, Any] = {
                "threadId": self.upstream_session_id,
                "input": [{"type": "text", "text": text}],
            }
            if self.model:
                params["model"] = self.model
            reject_experimental_params(params, experimental=self.experimental_api)
            turn = await self._rpc("turn/start", params)
            if isinstance(turn, dict):
                inner = turn.get("turn") if isinstance(turn.get("turn"), dict) else turn
                if isinstance(inner, dict):
                    self._turn_id = str(inner.get("id") or "")
            turn_id = self._turn_id

        try:
            streamer = SentenceStreamer()
            gate = EventGate(on_event)
            clock = DeadlineClock(
                timeout,
                idle_error=lambda: CodexAppServerError("Codex response timed out."),
                hard_error=lambda: CodexAppServerError(
                    "Codex response exceeded maximum time."
                ),
                keepalive_interval_s=self.keepalive_interval_s,
                on_keepalive=lambda: gate.emit("keepalive"),
            )
            self._emit_identity(on_event)
            publish_event(on_event, StatusChanged(state=AgentRunState.WORKING))
            settled = False
            while not settled:
                get_task = asyncio.ensure_future(self._event_queue.get())
                abort_task = asyncio.ensure_future(abort_event.wait())
                try:
                    which = await await_activity(
                        clock, {"abort": abort_task, "event": get_task}
                    )
                finally:
                    for task in (get_task, abort_task):
                        if not task.done():
                            task.cancel()
                            with contextlib.suppress(asyncio.CancelledError):
                                await task
                if which == KEEPALIVE:
                    continue
                if which == "abort":
                    return
                message = get_task.result()
                clock.note_activity()
                if turn_id and _message_turn_id(message) not in ("", turn_id):
                    # A cancelled turn's tail ("turn/completed", status
                    # interrupted) can land after this turn started; taken
                    # as this turn's end, the answer came back empty.
                    continue
                async for sentence in self._apply_message(
                    message, gate, streamer, on_event, clock
                ):
                    yield sentence
                if _turn_terminal(message):
                    settled = True
            tail = streamer.flush()
            if tail:
                yield tail
        finally:
            async with self._lock:
                if self._abort is abort_event:
                    self._abort = None

    async def _apply_message(
        self,
        message: dict[str, Any],
        gate: EventGate,
        streamer: SentenceStreamer,
        on_event: Callable[..., Any] | None,
        clock: DeadlineClock,
    ) -> AsyncIterator[str]:
        method = str(message.get("method") or "")
        params = message.get("params")
        if not isinstance(params, dict):
            params = {}
        if method.endswith("/requestApproval") or method == "item/permissions/requestApproval":
            req_id = message.get("id")
            if self.permission_mode == "deny" and req_id is not None:
                await self._reply(req_id, _approval_response(message, "decline"))
                self._approvals.pop(str(req_id), None)
                return
            prompt = _approval_prompt(method, params)
            publish_event(
                on_event,
                PermissionRequested(
                    request_id=str(req_id),
                    prompt=prompt,
                    options=frozenset(
                        {
                            PermissionDecision.DENY,
                            PermissionDecision.ALLOW_ONCE,
                            PermissionDecision.ALLOW_SESSION,
                        }
                    ),
                    tool_call_id=str(params.get("itemId") or "") or None,
                ),
            )
            publish_event(on_event, StatusChanged(state=AgentRunState.BLOCKED))
            if self._auto_approve() and req_id is not None:
                await self._reply(req_id, _approval_response(message, "accept"))
                self._approvals.pop(str(req_id), None)
            return
        if method == "thread/tokenUsage/updated":
            usage = (params.get("tokenUsage") or {}).get("last")
            if isinstance(usage, dict):
                self._emit_usage(on_event, {"usage": usage})
            return
        if method == "item/reasoning/summaryTextDelta" or method == "item/reasoning/textDelta":
            gate.fire("thinking")
            publish_event(on_event, ThinkingStarted())
            gate.emit(workbench_event("thinking"))
            return
        if method == "item/agentMessage/delta":
            raw = str(params.get("delta") or params.get("text") or "")
            item_id = str(params.get("itemId") or "")
            if item_id:
                self._streamed_items.add(item_id)
            gate.fire("text")
            publish_event(on_event, AssistantDelta(text=raw))
            if raw:
                gate.emit(workbench_event("text", text=raw))
            for sentence in streamer.feed(raw):
                clock.note_audible()
                yield sentence
            return
        if method in {"item/started", "item/completed"}:
            item = params.get("item")
            if isinstance(item, dict):
                async for sentence in self._apply_item(
                    item,
                    gate,
                    streamer,
                    on_event,
                    clock,
                    completed=method == "item/completed",
                ):
                    yield sentence
            return
        if method == "item/commandExecution/outputDelta":
            detail = str(params.get("delta") or "")
            if detail:
                publish_event(
                    on_event,
                    ToolEvent(
                        name="commandExecution",
                        call_id=str(params.get("itemId") or "") or None,
                        phase=ToolPhase.UPDATED,
                        detail=detail,
                    ),
                )
            return
        if method == "turn/completed":
            turn = params.get("turn") if isinstance(params.get("turn"), dict) else params
            status = str(turn.get("status") or "") if isinstance(turn, dict) else ""
            if status == "failed":
                error = turn.get("error") if isinstance(turn, dict) else None
                message_text = "Codex turn failed."
                code = "turn_failed"
                if isinstance(error, dict):
                    message_text = str(error.get("message") or message_text)
                    code = str(error.get("code") or code)
                publish_event(
                    on_event,
                    AgentError(code=code, message=message_text, retryable=False),
                )
                publish_event(on_event, StatusChanged(state=AgentRunState.FAILED))
                raise CodexAppServerError(message_text)
            if status == "interrupted":
                publish_event(
                    on_event, StatusChanged(state=AgentRunState.CANCELLED)
                )
            else:
                publish_event(
                    on_event, StatusChanged(state=AgentRunState.COMPLETED)
                )
            self._emit_usage(on_event, turn if isinstance(turn, dict) else {})
            self._turn_id = ""
            return
        if method == "error" or message.get("error"):
            # A JSON-RPC error carries it at the top; an "error" notification
            # carries it in params ("model is not supported...").
            params = message.get("params")
            error = message.get("error") or (params.get("error") if isinstance(params, dict) else None)
            text = str(error) if not isinstance(error, dict) else str(
                error.get("message") or error
            )
            publish_event(
                on_event,
                AgentError(code="rpc_error", message=text, retryable=False, fatal=True),
            )
            raise CodexAppServerError(text)

    async def _apply_item(
        self,
        item: dict[str, Any],
        gate: EventGate,
        streamer: SentenceStreamer,
        on_event: Callable[..., Any] | None,
        clock: DeadlineClock,
        *,
        completed: bool,
    ) -> AsyncIterator[str]:
        item_type = str(item.get("type") or "")
        item_id = str(item.get("id") or "")
        if item_type == "reasoning" and not completed:
            gate.fire("thinking")
            publish_event(on_event, ThinkingStarted())
            return
        if item_type == "agentMessage" and completed:
            text = str(item.get("text") or "")
            if text and item_id not in self._streamed_items:
                gate.fire("text")
                publish_event(on_event, AssistantDelta(text=text))
                for sentence in streamer.feed(text):
                    clock.note_audible()
                    yield sentence
            return
        if item_type.strip().lower() in TOOL_VOCAB.kinds:
            gate.fire("tool")
            args = {
                "command": item.get("command"),
                "path": item.get("path"),
                "tool": item.get("tool") or item.get("name"),
            }
            mapped, detail = narrate_tool(item_type, args, TOOL_VOCAB)
            if not completed:
                if detail:
                    gate.emit(f"tooldetail:{detail}")
                    clock.note_audible()
                gate.emit(f"tool:{mapped}")
            status = str(item.get("status") or "")
            if completed:
                phase = (
                    ToolPhase.FAILED
                    if status in {"failed", "declined"}
                    else ToolPhase.COMPLETED
                )
            else:
                phase = ToolPhase.STARTED
            publish_event(
                on_event,
                ToolEvent(
                    name=item_type,
                    call_id=item_id or None,
                    phase=phase,
                    detail=detail or None,
                    result_summary=str(item.get("aggregatedOutput") or "") or None,
                ),
            )
            gate.emit(
                workbench_event(
                    "tool_result" if completed else "tool_start",
                    name=item_type,
                    detail=detail,
                    tool_call_id=item_id,
                    input=args,
                    status="done" if completed else "running",
                )
            )

    def _emit_identity(self, on_event: Callable[..., Any] | None) -> None:
        if self._identified or not self.upstream_session_id:
            return
        self._identified = True
        publish_event(
            on_event,
            SessionIdentified(
                upstream_session_id=self.upstream_session_id,
                resumable=True,
            ),
        )

    def _emit_usage(
        self, on_event: Callable[..., Any] | None, turn: dict[str, Any]
    ) -> None:
        usage = turn.get("usage")
        if not isinstance(usage, dict):
            return
        publish_event(
            on_event,
            UsageReported(
                input_tokens=_as_int(usage.get("inputTokens") or usage.get("input")),
                output_tokens=_as_int(
                    usage.get("outputTokens") or usage.get("output")
                ),
            ),
        )

    async def abort(self) -> None:
        abort_event = self._abort
        if abort_event is not None:
            abort_event.set()
        if self._proc is None or self._proc.returncode is not None:
            return
        if self.upstream_session_id and self._turn_id:
            with contextlib.suppress(CodexAppServerError):
                await self._rpc(
                    "turn/interrupt",
                    {"threadId": self.upstream_session_id, "turnId": self._turn_id},
                )

    async def warmup(self) -> None:
        async with self._lock:
            await self._ensure_started()

    async def control(self, command: AgentControl) -> ControlResult:
        await self._ensure_started()
        if isinstance(command, Steer):
            params: dict[str, Any] = {
                "threadId": self.upstream_session_id,
                "input": [{"type": "text", "text": command.instruction}],
            }
            if self._turn_id:
                params["expectedTurnId"] = self._turn_id
            reject_experimental_params(params, experimental=self.experimental_api)
            try:
                result = await self._rpc("turn/steer", params)
            except CodexAppServerError as exc:
                return ControlRejected(reason=str(exc), retryable=True)
            turn_id = ""
            if isinstance(result, dict):
                turn_id = str(result.get("turnId") or "")
            return ControlAccepted(resource=turn_id or self.upstream_session_id or None)
        if isinstance(command, Cancel):
            await self.abort()
            return ControlAccepted(request_id=command.target_id)
        if isinstance(command, ResolvePermission):
            pending = self._approvals.pop(command.request_id, None)
            if pending is None:
                return ControlRejected(
                    reason=f"unknown approval {command.request_id}", retryable=False
                )
            decision = _PERMISSION_TO_DECISION.get(command.decision)
            if decision is None:
                return ControlRejected(reason="unsupported permission decision")
            await self._reply(pending.get("id"), _approval_response(pending, decision))
            return ControlAccepted(request_id=command.request_id)
        return ControlRejected(reason="unsupported control", retryable=False)


def _message_turn_id(message: dict[str, Any]) -> str:
    """The turn a notification belongs to, when it says."""
    params = message.get("params")
    if not isinstance(params, dict):
        return ""
    turn = params.get("turn")
    if isinstance(turn, dict):
        return str(turn.get("id") or "")
    return str(params.get("turnId") or "")


def _turn_terminal(message: dict[str, Any]) -> bool:
    if str(message.get("method") or "") != "turn/completed":
        return False
    params = message.get("params")
    if not isinstance(params, dict):
        return True
    turn = params.get("turn")
    if isinstance(turn, dict) and str(turn.get("status") or "") == "failed":
        return False
    return True


def _approval_response(message: dict[str, Any], decision: str) -> dict[str, Any]:
    if message.get("method") != "item/permissions/requestApproval":
        return {"decision": decision}
    requested = (message.get("params") or {}).get("permissions") or {}
    allowed = decision in {"accept", "acceptForSession"}
    return {
        "permissions": {key: requested[key] for key in ("fileSystem", "network")
                        if allowed and key in requested},
        "scope": "session" if decision == "acceptForSession" else "turn",
    }


def _approval_prompt(method: str, params: dict[str, Any]) -> str:
    reason = str(params.get("reason") or "").strip()
    if reason:
        return reason
    command = str(params.get("command") or "").strip()
    if command:
        return f"Allow command: {command}"
    if "fileChange" in method:
        return "Allow file change?"
    if "permissions" in method:
        return "Allow requested permissions?"
    return "Allow Codex action?"


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    return None
