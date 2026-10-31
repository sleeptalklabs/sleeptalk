"""Generic ACP v1 client. JSON-RPC 2.0 NDJSON over stdio."""

from __future__ import annotations

import asyncio
import itertools
import logging
import os
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sleeptalk.adapter import (
    AgentCapabilityManifest,
    AgentControl,
    AssistantDelta,
    CapabilitySupport,
    ControlAccepted,
    ControlRejected,
    ControlResult,
    PermissionDecision,
    PermissionRequested,
    ResolvePermission,
    SupportLevel,
    ThinkingStarted,
    ToolEvent,
    ToolPhase,
    VoiceSessionCapability,
    WorkerCapability,
    publish_event,
)
from sleeptalk.agents.profiles import AcpProfile
from sleeptalk.streaming import (
    KEEPALIVE,
    DeadlineClock,
    SentenceStreamer,
    StdioJsonRpcProcess,
    await_activity,
)
from sleeptalk.text import extract_speakable_text

CLIENT_NAME = "sleeptalk"
CLIENT_VERSION = "0.1.0"

logger = logging.getLogger(__name__)

EVENT_QUEUE_MAXSIZE = 64
PERMISSION_MODES = frozenset({"auto", "deny", "pending"})
KNOWN_SESSION_UPDATES = frozenset(
    {
        "agent_thought_chunk",
        "agent_message_chunk",
        "tool_call",
        "tool_call_update",
    }
)
_UNSUPPORTED = CapabilitySupport(SupportLevel.UNSUPPORTED)

_DECISION_ALIASES: dict[str, PermissionDecision] = {
    "allow_once": PermissionDecision.ALLOW_ONCE,
    "allow-once": PermissionDecision.ALLOW_ONCE,
    "allow_always": PermissionDecision.ALLOW_SESSION,
    "allow-always": PermissionDecision.ALLOW_SESSION,
    "allow_session": PermissionDecision.ALLOW_SESSION,
    "allow-session": PermissionDecision.ALLOW_SESSION,
    "deny": PermissionDecision.DENY,
    "reject": PermissionDecision.DENY,
    "reject_once": PermissionDecision.DENY,
    "reject-once": PermissionDecision.DENY,
    "deny_once": PermissionDecision.DENY,
    "deny-once": PermissionDecision.DENY,
}

# ACP v1 standard option kinds. optionId is free-form and agent-specific, so
# kind decides; the id aliases above only cover agents that omit kind.
_KIND_DECISIONS: dict[str, PermissionDecision] = {
    "allow_once": PermissionDecision.ALLOW_ONCE,
    "allow_always": PermissionDecision.ALLOW_SESSION,
    "reject_once": PermissionDecision.DENY,
    "reject_always": PermissionDecision.DENY,
}
# Most-preferred first. Deny never persists a rule, and a session grant may
# fall back to a single grant, never the other way round.
_DECISION_KINDS: dict[PermissionDecision, tuple[str, ...]] = {
    PermissionDecision.ALLOW_ONCE: ("allow_once",),
    PermissionDecision.ALLOW_SESSION: ("allow_always", "allow_once"),
    PermissionDecision.DENY: ("reject_once", "reject_always"),
}


class AcpError(RuntimeError):
    pass


@dataclass
class _OpenPermission:
    rpc_id: Any
    options: list[dict[str, Any]]


def _text_from_content(content: Any) -> str:
    if isinstance(content, dict) and content.get("type") == "text":
        return str(content.get("text") or "")
    if isinstance(content, str):
        return content
    return ""


def _option_id(option: Mapping[str, Any]) -> str:
    return str(option.get("optionId") or option.get("id") or "")


def _option_kind(option: Mapping[str, Any]) -> str:
    """The option's standard kind; the id only when the agent sent no kind."""
    kind = str(option.get("kind") or "").replace("-", "_")
    if kind:
        return kind if kind in _KIND_DECISIONS else ""
    decision = _DECISION_ALIASES.get(_option_id(option))
    if decision is PermissionDecision.ALLOW_ONCE:
        return "allow_once"
    if decision is PermissionDecision.ALLOW_SESSION:
        return "allow_always"
    if decision is PermissionDecision.DENY:
        return "reject_once"
    return ""


def _decisions_from_options(
    options: list[Mapping[str, Any]],
) -> frozenset[PermissionDecision]:
    found = {
        _KIND_DECISIONS[kind]
        for option in options
        if (kind := _option_kind(option)) and _option_id(option)
    }
    # Deny is always possible: with no reject option the request is cancelled.
    return frozenset(found | {PermissionDecision.DENY})


def _option_id_for_decision(
    decision: PermissionDecision, options: list[Mapping[str, Any]]
) -> str | None:
    """The option that carries out this decision, or None to cancel.

    Never picks an option of another kind: an unrecognized option list turns
    every decision into a cancel, which the agent treats as a refusal.
    """
    for wanted in _DECISION_KINDS[decision]:
        for option in options:
            if _option_kind(option) == wanted and _option_id(option):
                return _option_id(option)
    return None


def _permission_outcome(option_id: str | None) -> dict[str, Any]:
    if option_id is None:
        return {"outcome": "cancelled"}
    return {"outcome": "selected", "optionId": option_id}


def read_workspace_text(cwd: str, path: str) -> str:
    """Read a text file for the agent, only from inside its workspace."""
    root = Path(cwd).resolve()
    target = (root / path).resolve()
    if not target.is_relative_to(root):
        raise PermissionError(f"{path} is outside the workspace")
    return target.read_text(encoding="utf-8", errors="replace")


def _tool_event(kind: str, update: Mapping[str, Any]) -> ToolEvent:
    name = str(update.get("kind") or update.get("title") or "tool").strip() or "tool"
    call_id = str(update.get("toolCallId") or "") or None
    status = str(update.get("status") or "").lower()
    title = str(update.get("title") or "").strip() or None
    if kind == "tool_call":
        phase = ToolPhase.STARTED
    elif status == "completed":
        phase = ToolPhase.COMPLETED
    elif status == "failed":
        phase = ToolPhase.FAILED
    else:
        phase = ToolPhase.UPDATED
    return ToolEvent(name=name, call_id=call_id, phase=phase, detail=title)


def _acp_capabilities() -> AgentCapabilityManifest:
    return AgentCapabilityManifest(
        contract_version=1,
        structured_events=CapabilitySupport(SupportLevel.SUPPORTED),
        tool_lifecycle=CapabilitySupport(SupportLevel.SUPPORTED),
        permissions=CapabilitySupport(SupportLevel.SUPPORTED),
        clarification=_UNSUPPORTED,
        session_identity=_UNSUPPORTED,
        status=_UNSUPPORTED,
        usage=_UNSUPPORTED,
        steering=_UNSUPPORTED,
        targeted_cancellation=_UNSUPPORTED,
        workers=WorkerCapability(SupportLevel.UNSUPPORTED),
        voice_sessions=VoiceSessionCapability(SupportLevel.UNSUPPORTED),
    )


class AcpClient(StdioJsonRpcProcess):
    """AgentAdapter for one ACP v1 stdio server, parameterized by profile."""

    reader_task_name = "acp-stdio-reader"

    def __init__(
        self,
        profile: AcpProfile,
        *,
        cwd: str | None = None,
        session_id: str | None = None,
        permission_mode: str = "auto",
        event_queue_maxsize: int = EVENT_QUEUE_MAXSIZE,
        env: Mapping[str, str] | None = None,
        keepalive_interval_s: float = 0.0,
    ) -> None:
        mode = permission_mode.strip().lower() or "auto"
        if mode not in PERMISSION_MODES:
            raise ValueError(f"unknown permission_mode: {permission_mode!r}")
        if event_queue_maxsize < 1:
            raise ValueError("event_queue_maxsize must be >= 1")
        super().__init__(
            env=dict(env) if env is not None else None,
            cwd=cwd or os.getcwd(),
            error_cls=AcpError,
            binary=profile.binary,
            binary_fallback=profile.binary_fallbacks[0] if profile.binary_fallbacks else "",
            binary_fallbacks=profile.binary_fallbacks,
            install_hint=profile.install_hint or f"{profile.binary} binary not found.",
        )
        self.profile = profile
        self.permission_mode = mode
        self.keepalive_interval_s = keepalive_interval_s
        self.agent_capabilities = _acp_capabilities()
        self.diagnostics: list[dict[str, Any]] = []
        self.initialize_result: dict[str, Any] = {}
        self.server_capabilities: dict[str, Any] = {}
        self._resume_session_id = session_id
        self._env = dict(env) if env is not None else None
        self._lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._event_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(
            maxsize=event_queue_maxsize
        )
        self._pending: dict[Any, asyncio.Future[Any]] = {}
        self._open_permissions: dict[str, _OpenPermission] = {}
        self._req_counter = itertools.count(1)
        self._session_id = ""
        self._on_event: Callable[..., Any] | None = None
        # Set by abort(); the current stream yields nothing more once set.
        self._turn_aborted = False

    def _diag(self, code: str, message: str, **extra: Any) -> None:
        record = {"code": code, "message": message, **extra}
        self.diagnostics.append(record)
        logger.info("acp %s: %s", code, message)

    def _settle_pending(self, factory: Callable[[], Exception]) -> None:
        pending = list(self._pending.values())
        self._pending.clear()
        for future in pending:
            if not future.done():
                future.set_exception(factory())
        self._open_permissions.clear()

    async def _read_stdout(self) -> None:
        try:
            await super()._read_stdout()
        finally:
            self._settle_pending(lambda: AcpError("ACP process ended"))

    async def _send_json(self, payload: dict[str, Any]) -> None:
        async with self._write_lock:
            await super()._send_json(payload)

    async def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        req_id = next(self._req_counter)
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[req_id] = future
        try:
            await self._send_json(
                {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
            )
        except (BrokenPipeError, ConnectionResetError) as exc:
            # The agent exited before reading the request: the same failure as
            # losing it mid-reply, whichever side of the race noticed first.
            self._pending.pop(req_id, None)
            raise AcpError("ACP process ended") from exc
        try:
            result = await future
        finally:
            self._pending.pop(req_id, None)
        if not isinstance(result, dict):
            return {}
        return result

    def _enqueue(self, message: dict[str, Any]) -> None:
        try:
            self._event_queue.put_nowait(message)
        except asyncio.QueueFull:
            self._diag(
                "event_queue_full",
                "dropped session/update (queue full; newest dropped)",
                maxsize=self._event_queue.maxsize,
            )

    def _note_update(self, kind: str) -> None:
        if not kind or kind in KNOWN_SESSION_UPDATES:
            return
        namespace = kind.split("/", 1)[0] if "/" in kind else None
        if namespace and namespace not in self.profile.extension_namespaces:
            self._diag(
                "unknown_extension",
                f"ignored extension update {kind}",
                namespace=namespace,
            )
            return
        self._diag("unknown_update", f"ignored session update {kind}")

    async def _route_message(self, message: dict[str, Any]) -> None:
        if "id" in message and ("result" in message or "error" in message):
            future = self._pending.pop(message["id"], None)
            if future is not None and not future.done():
                if "error" in message:
                    future.set_exception(AcpError(str(message["error"])))
                else:
                    future.set_result(message.get("result") or {})
            return
        if "method" in message and "id" in message:
            await self._handle_agent_request(message)
            return
        method = message.get("method")
        if method == "session/update":
            update = message.get("params", {}).get("update") or {}
            if isinstance(update, dict):
                self._note_update(str(update.get("sessionUpdate") or ""))
            self._enqueue(message)
            return
        if method:
            namespace = method.split("/", 1)[0] if "/" in method else None
            if namespace and namespace not in self.profile.extension_namespaces:
                self._diag(
                    "unknown_extension",
                    f"ignored extension method {method}",
                    namespace=namespace,
                )
            else:
                self._diag("unknown_method", f"ignored method {method}")

    async def _handle_agent_request(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        req_id = message["id"]
        params = message.get("params") or {}
        if method == "session/request_permission":
            await self._handle_permission(req_id, params)
            return
        if method == "fs/read_text_file":
            path = str(params.get("path") or "")
            try:
                content = read_workspace_text(self.cwd, path)
                await self._send_json(
                    {"jsonrpc": "2.0", "id": req_id, "result": {"content": content}}
                )
            except OSError as exc:
                await self._send_json(
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {"code": -32602, "message": str(exc)},
                    }
                )
            return
        await self._send_json(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"Unsupported: {method}"},
            }
        )

    async def _handle_permission(self, req_id: Any, params: Mapping[str, Any]) -> None:
        options = [
            option
            for option in (params.get("options") or [])
            if isinstance(option, dict)
        ]
        tool_call = params.get("toolCall") or {}
        prompt = str(
            params.get("reason")
            or (tool_call.get("title") if isinstance(tool_call, dict) else "")
            or "Permission required"
        )
        request_id = str(req_id)
        if self.permission_mode == "pending":
            # Only a request that waits on the caller is reported. Auto and
            # deny answer it below, at once: reporting those had the call
            # read every Cursor tool call aloud as a question ("`cd … && ls
            # …`? Say yes or no.") that nothing was waiting on.
            self._open_permissions[request_id] = _OpenPermission(
                rpc_id=req_id, options=options
            )
            publish_event(
                self._on_event,
                PermissionRequested(
                    request_id=request_id,
                    prompt=prompt,
                    options=_decisions_from_options(options),
                    tool_call_id=(
                        str(tool_call.get("toolCallId") or "") or None
                        if isinstance(tool_call, dict)
                        else None
                    ),
                ),
            )
            return
        if self.permission_mode == "deny":
            option_id = _option_id_for_decision(PermissionDecision.DENY, options)
        else:
            # Auto grants each call once where it can; an "always" rule is
            # saved by the agent and would outlive the call.
            option_id = _option_id_for_decision(
                PermissionDecision.ALLOW_ONCE, options
            ) or _option_id_for_decision(PermissionDecision.ALLOW_SESSION, options)
        await self._send_json(
            {"jsonrpc": "2.0", "id": req_id, "result": {"outcome": _permission_outcome(option_id)}}
        )

    async def _ensure_started(self) -> None:
        if self._proc is not None and self._proc.returncode is None and self._session_id:
            return
        if self._closed:
            raise AcpError("ACP client is closed.")
        if self._proc is not None:
            await self._terminate()
            self._session_id = ""
            self._req_counter = itertools.count(1)
        binary = self._resolve_binary(self.profile.binary)
        self._check_binary(binary)
        await self._before_spawn(binary)
        spawn_env = None
        if self._env is not None:
            spawn_env = {**os.environ, **self._env}
        await self._spawn([binary, *self.profile.extra_args], env=spawn_env)
        init = await self._request(
            "initialize",
            {
                "protocolVersion": 1,
                # Cursor's `agent acp` validates clientInfo strictly and
                # rejects the handshake when version is absent.
                "clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION},
                "clientCapabilities": {
                    "fs": {"readTextFile": True, "writeTextFile": False}
                },
            },
        )
        self.initialize_result = init
        caps = init.get("agentCapabilities")
        self.server_capabilities = caps if isinstance(caps, dict) else dict(init)
        await self._after_initialize(init)
        if self._resume_session_id:
            loaded = await self._request(
                "session/load",
                {
                    "sessionId": self._resume_session_id,
                    "cwd": self.cwd,
                    "mcpServers": [],
                },
            )
            self._session_id = str(
                loaded.get("sessionId") or self._resume_session_id
            )
        else:
            created = await self._request(
                "session/new", {"cwd": self.cwd, "mcpServers": []}
            )
            self._session_id = str(created.get("sessionId") or "")
        if not self._session_id:
            raise AcpError("ACP server did not return a sessionId.")

    async def _after_initialize(self, init: dict[str, Any]) -> None:
        return None

    def _drain_queue(self) -> None:
        while True:
            try:
                self._event_queue.get_nowait()
            except asyncio.QueueEmpty:
                return

    async def respond(self, text: str, timeout: float = 300.0) -> str:
        parts: list[str] = []
        async for sentence in self.respond_streaming(text, timeout=timeout):
            parts.append(sentence)
        return extract_speakable_text(" ".join(parts))

    async def respond_streaming(
        self,
        text: str,
        timeout: float = 300.0,
        *,
        on_event: Callable[..., Any] | None = None,
    ) -> AsyncIterator[str]:
        async with self._lock:
            await self._ensure_started()
            self._drain_queue()
            self._turn_aborted = False
            self._on_event = on_event
            req_id = next(self._req_counter)
            future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
            self._pending[req_id] = future
            await self._send_json(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "method": "session/prompt",
                    "params": {
                        "sessionId": self._session_id,
                        "prompt": [{"type": "text", "text": text}],
                    },
                }
            )
            streamer = SentenceStreamer()
            clock = DeadlineClock(
                timeout,
                idle_error=lambda: AcpError("ACP response timed out."),
                hard_error=lambda: AcpError("ACP response exceeded maximum time."),
                keepalive_interval_s=self.keepalive_interval_s,
            )
            prompt_task = asyncio.ensure_future(future)
            try:
                while not self._turn_aborted:
                    if future.done():
                        if self._event_queue.empty():
                            break
                        event = self._event_queue.get_nowait()
                    else:
                        get_task = asyncio.ensure_future(self._event_queue.get())
                        which = await await_activity(
                            clock, {"event": get_task, "prompt": prompt_task}
                        )
                        if which != "event":
                            get_task.cancel()
                            if which == KEEPALIVE:
                                continue
                            continue
                        event = get_task.result()
                    clock.note_activity()
                    update = event.get("params", {}).get("update") or {}
                    if not isinstance(update, dict):
                        continue
                    kind = str(update.get("sessionUpdate") or "")
                    if kind == "agent_thought_chunk":
                        publish_event(on_event, ThinkingStarted())
                    elif kind == "agent_message_chunk":
                        raw_text = _text_from_content(update.get("content"))
                        publish_event(on_event, AssistantDelta(text=raw_text))
                        for sentence in streamer.feed(raw_text):
                            if self._turn_aborted:
                                break
                            clock.note_audible()
                            yield sentence
                    elif kind in ("tool_call", "tool_call_update"):
                        publish_event(on_event, _tool_event(kind, update))
                if not self._turn_aborted or future.done():
                    future.result()
            except Exception:
                streamer.clear_buffer()
                raise
            finally:
                self._pending.pop(req_id, None)
                if self._on_event is on_event:
                    self._on_event = None
            if self._turn_aborted:
                return
            tail = streamer.flush()
            if tail:
                yield tail

    async def abort(self) -> None:
        self._turn_aborted = True
        session_id = self._session_id
        proc = self._proc
        if proc is not None and proc.returncode is None and session_id and proc.stdin is not None:
            try:
                await self._send_json(
                    {
                        "jsonrpc": "2.0",
                        "method": "session/cancel",
                        "params": {"sessionId": session_id},
                    }
                )
            except (AcpError, BrokenPipeError, ConnectionResetError, OSError):
                pass
        self._settle_pending(lambda: AcpError("ACP session cancelled"))

    async def _before_spawn(self, binary: str) -> None:
        """Profile-specific preflight once the binary is known (default: none)."""

    async def warmup(self) -> None:
        await self._ensure_started()

    async def close(self) -> None:
        self._closed = True
        self._settle_pending(lambda: AcpError("ACP client closed"))
        await self._terminate()
        self._session_id = ""
        self._on_event = None

    async def control(self, command: AgentControl) -> ControlResult:
        if isinstance(command, ResolvePermission):
            return await self._resolve_permission(command)
        return ControlRejected(reason="unsupported command", retryable=False)

    async def _resolve_permission(self, command: ResolvePermission) -> ControlResult:
        open_req = self._open_permissions.pop(command.request_id, None)
        if open_req is None:
            return ControlRejected(
                reason="unknown permission request", retryable=False
            )
        option_id = _option_id_for_decision(command.decision, open_req.options)
        try:
            await self._send_json(
                {
                    "jsonrpc": "2.0",
                    "id": open_req.rpc_id,
                    "result": {"outcome": _permission_outcome(option_id)},
                }
            )
        except (AcpError, BrokenPipeError, ConnectionResetError, OSError) as exc:
            return ControlRejected(reason=str(exc), retryable=True)
        return ControlAccepted(request_id=command.request_id)


__all__ = [
    "EVENT_QUEUE_MAXSIZE",
    "AcpClient",
    "AcpError",
    "AcpProfile",
]
