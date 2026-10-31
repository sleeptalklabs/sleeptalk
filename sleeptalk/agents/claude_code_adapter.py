from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging
import os
from pathlib import Path
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from sleeptalk.agent_environment import sdk_environment

from sleeptalk.adapter import (
    AgentCapabilityManifest,
    AgentControl,
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
    SupportLevel,
    ThinkingStarted,
    ToolEvent,
    ToolPhase,
    UsageReported,
    VoiceSessionCapability,
    WorkerCapability,
    WorkerEvent,
    WorkerEvidence,
    WorkerPhase,
    publish_event,
    workbench_event,
)
from sleeptalk.agents.claude_code_workers import (
    is_lifecycle_message,
    is_worker_tool_name,
    worker_from_lifecycle,
    worker_from_tool,
)
from sleeptalk.streaming import (
    DEFAULT_TOOL_VERBS,
    KEEPALIVE,
    DeadlineClock,
    EventGate,
    SentenceStreamer,
    ToolVocab,
    await_activity,
    narrate_tool,
)
from sleeptalk.text import extract_speakable_text


TOOL_VOCAB = ToolVocab(
    kinds={
        "bash": "bash",
        "read": "read",
        "write": "write",
        "edit": "edit",
        "notebookedit": "edit",
        "todowrite": "write",
        "websearch": "search",
        "webfetch": "search",
        "grep": "search",
        "glob": "search",
        "task": "task",
    },
    subject_args={
        "bash": ("command",),
        "read": ("file_path", "path"),
        "write": ("file_path", "path"),
        "edit": ("file_path", "path"),
        "search": ("query", "pattern"),
        "task": ("description",),
    },
    verbs={**DEFAULT_TOOL_VERBS, "task": "starting"},
)

_SUPPORTED = CapabilitySupport(SupportLevel.SUPPORTED)
_UNSUPPORTED = CapabilitySupport(SupportLevel.UNSUPPORTED)

STARTUP_CONFIG_FILES = (
    ".mcp.json",
    ".claude/settings.json",
    ".claude/settings.local.json",
)

CLAUDE_ABORT_TIMEOUT_S = 1.0

logger = logging.getLogger(__name__)


class ClaudeCodeError(RuntimeError):
    pass


class ClaudeSession(Protocol):
    session_id: str
    can_use_tool: Callable[..., Any] | None

    async def connect(self) -> None: ...

    async def query(self, text: str) -> None: ...

    def receive(self) -> AsyncIterator[Any]: ...

    async def interrupt(self) -> None: ...

    async def close(self) -> None: ...


@dataclass(frozen=True)
class PermissionBinding:
    sdk_mode: str
    install_tool_gate: bool


@dataclass(frozen=True)
class ToolPermissionOutcome:
    allow: bool
    tool_input: dict[str, Any]


PERMISSION_MODES: dict[str, PermissionBinding] = {
    "skip": PermissionBinding("bypassPermissions", False),
    "bypass": PermissionBinding("bypassPermissions", False),
    "bypasspermissions": PermissionBinding("bypassPermissions", False),
    "deny": PermissionBinding("dontAsk", False),
    "dontask": PermissionBinding("dontAsk", False),
    "pending": PermissionBinding("default", True),
    "default": PermissionBinding("default", True),
    "acceptedits": PermissionBinding("acceptEdits", False),
    "plan": PermissionBinding("plan", False),
    "auto": PermissionBinding("auto", False),
}


def resolve_permission_mode(profile: str) -> PermissionBinding:
    key = profile.strip().lower() or "skip"
    return PERMISSION_MODES.get(key, PermissionBinding("default", True))


def _tool_event(name: str, tool_input: Any) -> tuple[str, str]:
    return narrate_tool(name, tool_input, TOOL_VOCAB)


def _manifest(*, permissions: CapabilitySupport) -> AgentCapabilityManifest:
    return AgentCapabilityManifest(
        contract_version=1,
        structured_events=_SUPPORTED,
        tool_lifecycle=_SUPPORTED,
        permissions=permissions,
        clarification=_UNSUPPORTED,
        session_identity=_SUPPORTED,
        status=_UNSUPPORTED,
        usage=_SUPPORTED,
        steering=_UNSUPPORTED,
        targeted_cancellation=_SUPPORTED,
        workers=WorkerCapability(
            SupportLevel.SUPPORTED, evidence=WorkerEvidence.LIFECYCLE_NATIVE
        ),
        voice_sessions=VoiceSessionCapability(SupportLevel.UNSUPPORTED),
    )


def _permission_prompt(tool_name: str, tool_input: dict[str, Any]) -> str:
    """What the caller is asked: "Allow Write?" alone said nothing about
    what would be written or run."""
    path = str(tool_input.get("file_path") or tool_input.get("path") or "").strip()
    command = " ".join(str(tool_input.get("command") or "").split())
    if path:
        return f"Allow {tool_name} on {Path(path).name}?"
    if command:
        return f"Allow {tool_name}: {command[:80]}{'…' if len(command) > 80 else ''}?"
    return f"Allow {tool_name}?"


def _system_prompt(persona: str) -> dict[str, str]:
    prompt = {"type": "preset", "preset": "claude_code"}
    text = persona.strip()
    if text:
        prompt["append"] = text
    return prompt


def _usage_tokens(usage: Any) -> tuple[int | None, int | None]:
    if not isinstance(usage, dict):
        return None, None
    incoming = usage.get("input_tokens", usage.get("prompt_tokens"))
    outgoing = usage.get("output_tokens", usage.get("completion_tokens"))
    return (
        int(incoming) if isinstance(incoming, (int, float)) else None,
        int(outgoing) if isinstance(outgoing, (int, float)) else None,
    )


# A 754KB PNG as a tool result is already over the SDK's 1MB default.
CLI_MAX_BUFFER_SIZE = 8 * 1024 * 1024


class SdkClaudeSession:
    def __init__(
        self,
        *,
        cwd: str,
        model: str = "",
        system_prompt: dict[str, str] | None = None,
        resume: str | None = None,
        permission_mode: str = "default",
        can_use_tool: Callable[..., Any] | None = None,
        setting_sources: list[str] | None = None,
        env: dict[str, str] | None = None,
        max_buffer_size: int = CLI_MAX_BUFFER_SIZE,
    ) -> None:
        self.session_id = resume or ""
        self._cwd = cwd
        self._model = model
        self._system_prompt = system_prompt
        self._resume = resume
        self._permission_mode = permission_mode
        self.can_use_tool = can_use_tool
        self._setting_sources = setting_sources
        self._env = env
        self._max_buffer_size = max_buffer_size
        self._client: Any = None

    def _build_client(self) -> Any:
        try:
            from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
        except ImportError as exc:
            raise ClaudeCodeError(
                "claude-agent-sdk is not installed. Install sleeptalk[claude]."
            ) from exc
        options_kwargs: dict[str, Any] = {
            "cwd": self._cwd,
            "permission_mode": self._permission_mode,
            "include_partial_messages": True,
        }
        if self._model.strip():
            options_kwargs["model"] = self._model.strip()
        options_kwargs["system_prompt"] = self._system_prompt or {
            "type": "preset",
            "preset": "claude_code",
        }
        if self._resume:
            options_kwargs["resume"] = self._resume
        if self.can_use_tool is not None:
            options_kwargs["can_use_tool"] = self._sdk_tool_gate
        if self._setting_sources is not None:
            options_kwargs["setting_sources"] = self._setting_sources
        options_kwargs["env"] = sdk_environment(self._env)
        options_kwargs["max_buffer_size"] = self._max_buffer_size
        return ClaudeSDKClient(options=ClaudeAgentOptions(**options_kwargs))

    async def _sdk_tool_gate(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        context: Any,
    ) -> Any:
        from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

        outcome = await self.can_use_tool(tool_name, tool_input, context)
        if not isinstance(outcome, ToolPermissionOutcome):
            return outcome
        if outcome.allow:
            return PermissionResultAllow(updated_input=outcome.tool_input)
        return PermissionResultDeny(message="denied")

    async def connect(self) -> None:
        self._client = self._build_client()
        await self._client.connect()

    async def query(self, text: str) -> None:
        if self._client is None:
            raise ClaudeCodeError("Claude session is not connected.")
        await self._client.query(text)

    async def receive(self) -> AsyncIterator[Any]:
        if self._client is None:
            raise ClaudeCodeError("Claude session is not connected.")
        async for message in self._client.receive_response():
            self._capture_session_id(message)
            yield message

    async def interrupt(self) -> None:
        if self._client is None:
            return
        await self._client.interrupt()

    async def close(self) -> None:
        client = self._client
        self._client = None
        if client is None:
            return
        await client.disconnect()

    def _capture_session_id(self, message: Any) -> None:
        sid = getattr(message, "session_id", None)
        if sid:
            self.session_id = str(sid)
            return
        data = getattr(message, "data", None)
        if isinstance(data, dict) and data.get("session_id"):
            self.session_id = str(data["session_id"])


class ClaudeCodeClient:
    def __init__(
        self,
        *,
        cwd: str | None = None,
        name: str = "claude",
        model: str = "",
        api_key: str = "",
        permission_mode: str = "skip",
        persona_instructions: str = "",
        keepalive_interval_s: float = 0.0,
        session: ClaudeSession | None = None,
        session_factory: Callable[..., ClaudeSession] | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self._env = dict(env) if env is not None else None
        self.cwd = cwd or os.getcwd()
        self.name = name
        self.model = model
        self.api_key = api_key
        self.permission_mode = permission_mode.strip().lower() or "skip"
        self.persona_instructions = persona_instructions
        self.keepalive_interval_s = keepalive_interval_s
        self._binding = resolve_permission_mode(self.permission_mode)
        self._session_factory = session_factory or SdkClaudeSession
        self._injected = session
        self._session: ClaudeSession | None = session
        self._session_id = getattr(session, "session_id", "") or ""
        self._closed = False
        self._connected = False
        self._identified = False
        self._config_snapshot: dict[str, float] = {}
        self._lock = asyncio.Lock()
        self._permission_waiters: dict[str, asyncio.Future[PermissionDecision]] = {}
        self._permission_ids = itertools.count(1)
        self._worker_call_ids: set[str] = set()
        self._on_event: Callable[..., Any] | None = None
        self._receive: AsyncIterator[Any] | None = None
        self._pending_get: asyncio.Future[Any] | None = None
        self._idle_task: asyncio.Task[None] | None = None
        if self._binding.install_tool_gate and self._session is not None:
            self._session.can_use_tool = self._approve_tool
        elif self._session is not None:
            self._session.can_use_tool = None
        self.agent_capabilities = _manifest(
            permissions=(
                _SUPPORTED
                if self._binding.install_tool_gate
                else CapabilitySupport(
                    SupportLevel.UNAVAILABLE,
                    reason=f"permission_mode={self.permission_mode}",
                )
            )
        )

    def _session_kwargs(self) -> dict[str, Any]:
        env = self._env
        if self.api_key.strip():
            env = {**(env if env is not None else os.environ), "ANTHROPIC_API_KEY": self.api_key.strip()}
        return {
            "cwd": self.cwd,
            "model": self.model,
            "system_prompt": _system_prompt(self.persona_instructions),
            "resume": self._session_id or None,
            "permission_mode": self._binding.sdk_mode,
            "can_use_tool": self._approve_tool if self._binding.install_tool_gate else None,
            "setting_sources": [] if self._binding.install_tool_gate else None,
            "env": env,
            "max_buffer_size": CLI_MAX_BUFFER_SIZE,
        }

    def _snapshot_startup_config(self) -> dict[str, float]:
        snapshot: dict[str, float] = {}
        for rel in STARTUP_CONFIG_FILES:
            try:
                snapshot[rel] = os.path.getmtime(os.path.join(self.cwd, rel))
            except OSError:
                pass
        return snapshot

    async def _reconnect(self) -> None:
        session = self._session
        if session is not None:
            if session.session_id:
                self._session_id = session.session_id
            await session.close()
        if self._injected is None:
            self._session = None
        self._connected = False

    async def _ensure_started(self) -> None:
        if self._closed:
            raise ClaudeCodeError("Claude Code client is closed.")
        if self._connected and self._session is not None:
            if self._snapshot_startup_config() == self._config_snapshot:
                return
            await self._reconnect()
        kwargs = self._session_kwargs()
        if self._injected is not None:
            self._session = self._injected
            if kwargs["resume"]:
                self._session.session_id = kwargs["resume"]
            self._session.can_use_tool = kwargs["can_use_tool"]
        elif self._session is None:
            self._session = self._session_factory(**kwargs)
        await self._session.connect()
        self._connected = True
        if self._session.session_id:
            self._session_id = self._session.session_id
        self._config_snapshot = self._snapshot_startup_config()

    async def respond(self, text: str, timeout: float = 300.0) -> str:
        parts: list[str] = []
        async for sentence in self.respond_streaming(text, timeout=timeout):
            parts.append(sentence)
        return extract_speakable_text(" ".join(parts))

    async def respond_streaming(
        self,
        text: str,
        timeout: float = 300.0,
        on_event: Callable[..., Any] | None = None,
    ) -> AsyncIterator[str]:
        async with self._lock:
            await self._stop_idle_drain()
            await self._ensure_started()
            assert self._session is not None
            session = self._session
            try:
                await session.query(text)
                self._on_event = on_event
                streamer = SentenceStreamer()
                gate = EventGate(on_event)
                clock = DeadlineClock(
                    timeout,
                    idle_error=lambda: ClaudeCodeError("Claude Code response timed out."),
                    hard_error=lambda: ClaudeCodeError(
                        "Claude Code response exceeded maximum time."
                    ),
                    keepalive_interval_s=self.keepalive_interval_s,
                    on_keepalive=lambda: gate.emit("keepalive"),
                )
                streamed_text = [False]
                get_task: asyncio.Future[Any] | None = None
                try:
                    receive = session.receive()
                    self._receive = receive
                    while True:
                        if get_task is None:
                            get_task = asyncio.ensure_future(anext(receive, _TURN_DONE))
                        which = await await_activity(clock, {"event": get_task})
                        if which == KEEPALIVE:
                            continue
                        message = get_task.result()
                        get_task = None
                        if message is _TURN_DONE:
                            break
                        clock.note_activity()
                        self._capture_identity(message, on_event)
                        async for sentence in self._publish_message(
                            message,
                            streamer,
                            gate,
                            clock,
                            on_event,
                            streamed_text,
                        ):
                            yield sentence
                        if _is_result(message):
                            if getattr(message, "is_error", False):
                                raise ClaudeCodeError(
                                    str(
                                        getattr(message, "result", None)
                                        or "Claude Code turn failed."
                                    )[:300]
                                )
                            break
                    tail = streamer.flush()
                    if tail:
                        yield tail
                    self._start_idle_drain()
                finally:
                    if self._idle_task is None:
                        if get_task is not None and not get_task.done():
                            get_task.cancel()
                        self._receive = None
                    elif get_task is not None and not get_task.done():
                        self._pending_get = get_task
            except BaseException:
                await self._reconnect()
                raise

    async def abort(self) -> None:
        await self._stop_idle_drain()
        session = self._session
        if session is not None:
            if session.session_id:
                self._session_id = session.session_id
            self._session = None
            self._connected = False
            try:
                await asyncio.wait_for(
                    session.interrupt(), timeout=CLAUDE_ABORT_TIMEOUT_S
                )
            except TimeoutError:
                logger.warning("Claude Code interrupt timed out; reconnecting session")
            except Exception as exc:
                logger.warning("Claude Code interrupt failed; reconnecting session: %s", exc)
            try:
                await asyncio.wait_for(session.close(), timeout=CLAUDE_ABORT_TIMEOUT_S)
            except TimeoutError:
                logger.warning("Claude Code session close timed out during abort")
            except Exception as exc:
                logger.warning("Claude Code session close failed during abort: %s", exc)
        for waiter in self._permission_waiters.values():
            if not waiter.done():
                waiter.set_result(PermissionDecision.DENY)
        self._permission_waiters.clear()

    async def warmup(self) -> None:
        await self._ensure_started()
        await self.respond("Say OK.", timeout=60.0)

    async def close(self) -> None:
        self._closed = True
        self._connected = False
        await self._stop_idle_drain()
        session = self._session
        self._session = None
        if session is not None:
            await session.close()

    async def control(self, command: AgentControl) -> ControlResult:
        if isinstance(command, ResolvePermission):
            waiter = self._permission_waiters.get(command.request_id)
            if waiter is None or waiter.done():
                return ControlRejected(
                    reason="no pending permission request", retryable=False
                )
            waiter.set_result(command.decision)
            return ControlAccepted(request_id=command.request_id)
        if isinstance(command, Cancel):
            session = self._session
            if session is None:
                return ControlRejected(reason="no active session", retryable=True)
            await session.interrupt()
            return ControlAccepted(request_id=command.target_id, resource=self._session_id)
        return ControlRejected(reason="unsupported control", retryable=False)

    async def _approve_tool(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        context: Any,
    ) -> Any:
        request_id = str(
            getattr(context, "tool_use_id", None) or f"perm-{next(self._permission_ids)}"
        )
        loop = asyncio.get_running_loop()
        waiter: asyncio.Future[PermissionDecision] = loop.create_future()
        self._permission_waiters[request_id] = waiter
        publish_event(
            self._on_event,
            PermissionRequested(
                request_id=request_id,
                prompt=_permission_prompt(tool_name, tool_input),
                options=frozenset(
                    {
                        PermissionDecision.DENY,
                        PermissionDecision.ALLOW_ONCE,
                    }
                ),
                tool_call_id=request_id,
            ),
        )
        try:
            decision = await waiter
        finally:
            self._permission_waiters.pop(request_id, None)
        allow = decision in {
            PermissionDecision.ALLOW_ONCE,
            PermissionDecision.ALLOW_SESSION,
        }
        return ToolPermissionOutcome(allow=allow, tool_input=tool_input)

    def _capture_identity(self, message: Any, on_event: Callable[..., Any] | None) -> None:
        sid = getattr(message, "session_id", None)
        if not sid:
            data = getattr(message, "data", None)
            if isinstance(data, dict):
                sid = data.get("session_id")
        if not sid:
            return
        self._session_id = str(sid)
        if self._session is not None:
            self._session.session_id = self._session_id
        if self._identified:
            return
        self._identified = True
        publish_event(
            on_event,
            SessionIdentified(upstream_session_id=self._session_id, resumable=True),
        )

    async def _publish_message(
        self,
        message: Any,
        streamer: SentenceStreamer,
        gate: EventGate,
        clock: DeadlineClock,
        on_event: Callable[..., Any] | None,
        streamed_text: list[bool],
    ) -> AsyncIterator[str]:
        if is_lifecycle_message(message):
            worker = worker_from_lifecycle(message)
            if worker is not None:
                publish_event(on_event, worker)
            return

        event = getattr(message, "event", None)
        if isinstance(event, dict):
            async for sentence in self._publish_stream_event(
                event, streamer, gate, clock, on_event, streamed_text
            ):
                yield sentence
            return

        content = getattr(message, "content", None)
        if isinstance(content, list):
            for block in content:
                async for sentence in self._publish_block(
                    block, streamer, gate, clock, on_event, streamed_text
                ):
                    yield sentence
            return

        if _is_result(message):
            usage = getattr(message, "usage", None)
            cost = getattr(message, "total_cost_usd", None)
            incoming, outgoing = _usage_tokens(usage)
            if incoming is not None or outgoing is not None or cost is not None:
                publish_event(
                    on_event,
                    UsageReported(
                        input_tokens=incoming,
                        output_tokens=outgoing,
                        cost_minor_units=int(round(float(cost) * 100))
                        if isinstance(cost, (int, float))
                        else None,
                        currency="USD" if cost is not None else None,
                    ),
                )

    async def _publish_stream_event(
        self,
        event: dict[str, Any],
        streamer: SentenceStreamer,
        gate: EventGate,
        clock: DeadlineClock,
        on_event: Callable[..., Any] | None,
        streamed_text: list[bool],
    ) -> AsyncIterator[str]:
        if event.get("type") != "content_block_delta":
            return
        delta = event.get("delta") or {}
        delta_type = delta.get("type")
        if delta_type == "text_delta":
            raw_text = str(delta.get("text", ""))
            streamed_text[0] = True
            gate.fire("text")
            publish_event(on_event, AssistantDelta(text=raw_text))
            if raw_text:
                gate.emit(workbench_event("text", text=raw_text))
            for sentence in streamer.feed(raw_text):
                clock.note_audible()
                yield sentence
            return
        if delta_type == "thinking_delta":
            self._emit_thinking(gate, on_event)

    async def _publish_block(
        self,
        block: Any,
        streamer: SentenceStreamer,
        gate: EventGate,
        clock: DeadlineClock,
        on_event: Callable[..., Any] | None,
        streamed_text: list[bool],
    ) -> AsyncIterator[str]:
        if isinstance(block, dict):
            block_type = block.get("type")
            if block_type == "tool_use":
                self._emit_tool(
                    str(block.get("name", "")),
                    block.get("input"),
                    str(block.get("id", "") or ""),
                    gate,
                    clock,
                    on_event,
                )
                return
            if block_type == "tool_result":
                self._emit_tool_result(
                    str(block.get("tool_use_id", "") or ""),
                    block.get("content", ""),
                    bool(block.get("is_error", False)),
                    gate,
                    on_event,
                )
                return
            if block_type == "text" and not streamed_text[0]:
                raw_text = str(block.get("text", ""))
                gate.fire("text")
                publish_event(on_event, AssistantDelta(text=raw_text))
                if raw_text:
                    gate.emit(workbench_event("text", text=raw_text))
                for sentence in streamer.feed(raw_text):
                    clock.note_audible()
                    yield sentence
                return
            if block_type == "thinking":
                self._emit_thinking(gate, on_event)
            return

        name = getattr(block, "name", None)
        tool_input = getattr(block, "input", None)
        if name and tool_input is not None and not hasattr(block, "text"):
            self._emit_tool(
                str(name),
                tool_input,
                str(getattr(block, "id", "") or ""),
                gate,
                clock,
                on_event,
            )
            return
        tool_use_id = getattr(block, "tool_use_id", None)
        if tool_use_id is not None and hasattr(block, "content"):
            self._emit_tool_result(
                str(tool_use_id),
                getattr(block, "content", ""),
                bool(getattr(block, "is_error", False)),
                gate,
                on_event,
            )
            return
        thinking = getattr(block, "thinking", None)
        if thinking is not None and not hasattr(block, "text"):
            self._emit_thinking(gate, on_event)
            return
        raw_text = getattr(block, "text", None)
        if raw_text is not None and not streamed_text[0]:
            gate.fire("text")
            publish_event(on_event, AssistantDelta(text=str(raw_text)))
            if raw_text:
                gate.emit(workbench_event("text", text=str(raw_text)))
            for sentence in streamer.feed(str(raw_text)):
                clock.note_audible()
                yield sentence

    def _emit_tool(
        self,
        name: str,
        tool_input: Any,
        call_id: str,
        gate: EventGate,
        clock: DeadlineClock,
        on_event: Callable[..., Any] | None,
    ) -> None:
        if is_worker_tool_name(name):
            if call_id:
                self._worker_call_ids.add(call_id)
            worker = worker_from_tool(name, tool_input, call_id)
            if worker is not None:
                publish_event(on_event, worker)
            return
        gate.fire("tool")
        kind, detail = _tool_event(name, tool_input)
        typed = getattr(on_event, "_skip_legacy_fire", False)
        if detail and not typed:
            publish_event(
                on_event,
                ToolEvent(name="", phase=ToolPhase.UPDATED, detail=detail),
            )
            clock.note_audible()
        publish_event(
            on_event,
            ToolEvent(
                name=kind,
                call_id=call_id or None,
                phase=ToolPhase.STARTED,
                detail=detail or None,
            ),
        )
        if detail and typed:
            clock.note_audible()
        gate.emit(
            workbench_event(
                "tool_start",
                name=name,
                tool_kind=kind,
                detail=detail,
                input=tool_input,
            )
        )

    def _emit_thinking(
        self,
        gate: EventGate,
        on_event: Callable[..., Any] | None,
    ) -> None:
        if not gate.fire("thinking"):
            return
        if getattr(on_event, "_skip_legacy_fire", False):
            publish_event(on_event, ThinkingStarted())

    def _emit_tool_result(
        self,
        call_id: str,
        content: Any,
        is_error: bool,
        gate: EventGate,
        on_event: Callable[..., Any] | None,
    ) -> None:
        if call_id and call_id in self._worker_call_ids:
            self._worker_call_ids.discard(call_id)
            publish_event(
                on_event,
                WorkerEvent(
                    worker_id=call_id,
                    phase=WorkerPhase.FAILED if is_error else WorkerPhase.COMPLETED,
                ),
            )
            return
        publish_event(
            on_event,
            ToolEvent(
                name="",
                call_id=call_id or None,
                phase=ToolPhase.FAILED if is_error else ToolPhase.COMPLETED,
            ),
        )
        gate.emit(
            workbench_event(
                "tool_result",
                tool_use_id=call_id,
                content=content,
                is_error=is_error,
            )
        )

    def _start_idle_drain(self) -> None:
        if self._closed or self._idle_task is not None or self._receive is None:
            return
        self._idle_task = asyncio.create_task(
            self._idle_drain(), name="claude-idle-drain"
        )

    async def _stop_idle_drain(self) -> None:
        task = self._idle_task
        self._idle_task = None
        pending = self._pending_get
        self._pending_get = None
        if pending is not None and not pending.done():
            pending.cancel()
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._receive = None

    async def _idle_drain(self) -> None:
        receive = self._receive
        if receive is None:
            return
        get_task = self._pending_get
        self._pending_get = None
        try:
            while not self._closed:
                if get_task is None:
                    get_task = asyncio.ensure_future(anext(receive, _TURN_DONE))
                message = await get_task
                get_task = None
                if message is _TURN_DONE:
                    return
                self._ingest_idle_message(message)
        except asyncio.CancelledError:
            if get_task is not None and not get_task.done():
                get_task.cancel()
            raise

    def _ingest_idle_message(self, message: Any) -> None:
        if is_lifecycle_message(message):
            worker = worker_from_lifecycle(message)
            if worker is not None:
                publish_event(self._on_event, worker)
            return
        content = getattr(message, "content", None)
        if not isinstance(content, list):
            return
        gate = EventGate(self._on_event)
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                self._emit_tool_result(
                    str(block.get("tool_use_id", "") or ""),
                    block.get("content", ""),
                    bool(block.get("is_error", False)),
                    gate,
                    self._on_event,
                )
                continue
            tool_use_id = getattr(block, "tool_use_id", None)
            if tool_use_id is not None and hasattr(block, "content"):
                self._emit_tool_result(
                    str(tool_use_id),
                    getattr(block, "content", ""),
                    bool(getattr(block, "is_error", False)),
                    gate,
                    self._on_event,
                )


def _is_result(message: Any) -> bool:
    return hasattr(message, "is_error") and (
        hasattr(message, "session_id") or hasattr(message, "result")
    )


_TURN_DONE = object()
