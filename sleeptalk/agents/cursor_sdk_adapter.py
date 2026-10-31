"""Cursor Python SDK adapter. Agent -> Run, not a Claude session clone."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass, replace
from typing import Any, Protocol

from sleeptalk.agent_environment import is_voice_credential

from sleeptalk.adapter import (
    AgentCapabilityManifest,
    AssistantDelta,
    CapabilitySupport,
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
)
from sleeptalk.agents.cursor_voice import (
    build_local_agent_options,
    local_setting_sources,
    wrap_cursor_voice_sink,
)
from sleeptalk.voice_skills import discover_skills, rewrite_spoken_skill
from sleeptalk.streaming import (
    DEFAULT_TOOL_VERBS,
    KEEPALIVE,
    DeadlineClock,
    SentenceStreamer,
    ToolVocab,
    await_activity,
    narrate_tool,
)
from sleeptalk.text import extract_speakable_text


class CursorSdkError(RuntimeError):
    pass


class CursorRun(Protocol):
    status: str

    def messages(self) -> AsyncIterator[Any]: ...

    async def wait(self) -> Any: ...

    async def cancel(self) -> None: ...

    def supports(self, operation: str) -> bool: ...


class CursorAgent(Protocol):
    agent_id: str

    async def send(self, text: str) -> CursorRun: ...

    async def close(self) -> None: ...


class CursorAgentFactory(Protocol):
    async def start(self) -> CursorAgent: ...


TOOL_VOCAB = ToolVocab(
    kinds={
        "read": "read",
        "write": "write",
        "edit": "edit",
        "grep": "search",
        "glob": "search",
        "ls": "read",
        "shell": "bash",
        "task": "task",
        "mcp": "mcp",
    },
    subject_args={
        "read": ("path", "file_path"),
        "write": ("path", "file_path"),
        "edit": ("path", "file_path"),
        "search": ("pattern", "query"),
        "bash": ("command",),
        "task": ("description", "prompt"),
    },
    verbs={**DEFAULT_TOOL_VERBS, "task": "starting"},
)

_SUPPORTED = CapabilitySupport(SupportLevel.SUPPORTED)
_UNSUPPORTED = CapabilitySupport(SupportLevel.UNSUPPORTED)
_WORKER_TOOLS = frozenset({"task"})

CURSOR_SDK_MANIFEST = AgentCapabilityManifest(
    contract_version=1,
    structured_events=_SUPPORTED,
    tool_lifecycle=_SUPPORTED,
    permissions=CapabilitySupport(
        SupportLevel.UNAVAILABLE,
        reason="Cursor SDK has no interactive tool-approval callback",
    ),
    clarification=_UNSUPPORTED,
    session_identity=_SUPPORTED,
    status=_SUPPORTED,
    usage=_SUPPORTED,
    steering=_UNSUPPORTED,
    targeted_cancellation=_SUPPORTED,
    workers=WorkerCapability(
        SupportLevel.SUPPORTED, evidence=WorkerEvidence.CORRELATED_CHILD
    ),
    voice_sessions=VoiceSessionCapability(SupportLevel.UNSUPPORTED),
)


@dataclass(frozen=True)
class _Usage:
    input_tokens: int | None
    output_tokens: int | None


class CursorSdkClient:
    agent_capabilities = CURSOR_SDK_MANIFEST

    def __init__(
        self,
        *,
        cwd: str | None = None,
        name: str = "cursor",
        model: str = "",
        api_key: str = "",
        agent_id: str = "",
        permission_mode: str = "auto",
        keepalive_interval_s: float = 0.0,
        agent: CursorAgent | None = None,
        agent_factory: CursorAgentFactory | None = None,
    ) -> None:
        self.cwd = cwd or os.getcwd()
        self.name = name
        self.model = model.strip() or "composer-2.5"
        self.api_key = api_key.strip()
        self.agent_id = agent_id.strip()
        self.permission_mode = permission_mode.strip().lower() or "auto"
        if self.permission_mode == "deny":
            self.agent_capabilities = replace(CURSOR_SDK_MANIFEST, permissions=_SUPPORTED, workers=WorkerCapability(SupportLevel.UNSUPPORTED))
        self.keepalive_interval_s = keepalive_interval_s
        self._injected = agent
        self._factory = agent_factory
        self._agent: CursorAgent | None = agent
        self._run: CursorRun | None = None
        self._closed = False
        self._identified = False
        self._lock = asyncio.Lock()

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
        text = rewrite_spoken_skill(text, discover_skills(self.cwd))
        on_event = wrap_cursor_voice_sink(on_event)
        async with self._lock:
            await self.warmup()
            self._identify(on_event)
            assert self._agent is not None
            try:
                run = await asyncio.wait_for(self._agent.send(text), timeout)
            except TimeoutError as exc:
                raise CursorSdkError("Cursor SDK send timed out before a run was returned.") from exc
        self._run = run
        streamer = SentenceStreamer()
        clock = DeadlineClock(
            timeout,
            idle_error=lambda: CursorSdkError("Cursor SDK run timed out."),
            hard_error=lambda: CursorSdkError("Cursor SDK run exceeded maximum time."),
            keepalive_interval_s=self.keepalive_interval_s,
        )
        iterator = run.messages()
        get_task = asyncio.ensure_future(iterator.__anext__())
        settled = False
        try:
            while True:
                which = await await_activity(clock, {"event": get_task})
                if which == KEEPALIVE:
                    continue
                if which != "event":
                    get_task.cancel()
                    continue
                try:
                    message = get_task.result()
                except StopAsyncIteration:
                    break
                clock.note_activity()
                for sentence in self._ingest(message, streamer, on_event):
                    clock.note_audible()
                    yield sentence
                get_task = asyncio.ensure_future(iterator.__anext__())
            get_task = asyncio.ensure_future(run.wait())
            while await await_activity(clock, {"event": get_task}) != "event":
                pass
            result = get_task.result()
            settled = True
            status = getattr(result, "status", None) or getattr(run, "status", "")
            if status == "cancelled":
                raise CursorSdkError("Cursor run was cancelled")
            if status != "finished":
                raise CursorSdkError(f"Cursor run {getattr(result, 'id', '')} failed ({status})")
        except Exception:
            streamer.clear_buffer()
            raise
        finally:
            get_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await get_task
            if not settled and getattr(run, "status", "running") == "running":
                with suppress(Exception):
                    async with asyncio.timeout(3):
                        await run.cancel()
                        await run.wait()
            if hasattr(iterator, "aclose"):
                with suppress(Exception):
                    await iterator.aclose()
            if self._run is run:
                self._run = None
        tail = streamer.flush()
        if tail:
            yield tail

    async def abort(self) -> None:
        run = self._run
        if run is None:
            return
        if hasattr(run, "supports") and not run.supports("cancel"):
            return
        if getattr(run, "status", "running") != "running":
            return
        await run.cancel()

    async def warmup(self) -> None:
        if self._closed:
            raise CursorSdkError("Cursor SDK client is closed.")
        if self._agent is not None:
            self.agent_id = getattr(self._agent, "agent_id", self.agent_id)
            self._identify(None)
            return
        factory = self._factory or SdkCursorFactory(
            cwd=self.cwd,
            model=self.model,
            api_key=self.api_key,
            agent_id=self.agent_id,
            permission_mode=self.permission_mode,
        )
        self._agent = await factory.start()
        self.agent_id = getattr(self._agent, "agent_id", self.agent_id)
        self._identify(None)

    async def close(self) -> None:
        self._closed = True
        await self.abort()
        agent = self._agent
        self._agent = None
        self._run = None
        if agent is not None and agent is not self._injected:
            await agent.close()

    def _identify(self, on_event: Callable[..., Any] | None) -> None:
        if self._identified or not self.agent_id or on_event is None:
            return
        self._identified = True
        publish_event(
            on_event,
            SessionIdentified(upstream_session_id=self.agent_id, resumable=self.permission_mode != "deny"),
        )

    def _ingest(
        self,
        message: Any,
        streamer: SentenceStreamer,
        on_event: Callable[..., Any] | None,
    ) -> list[str]:
        kind = _message_type(message)
        if kind == "assistant":
            text = _assistant_text(message)
            if not text:
                return []
            publish_event(on_event, AssistantDelta(text=text))
            return list(streamer.feed(text))
        if kind == "thinking":
            publish_event(on_event, ThinkingStarted())
            return []
        if kind == "tool_call":
            if _tool_name(message).lower() in _WORKER_TOOLS:
                publish_event(on_event, _worker_event(message))
                return []
            publish_event(on_event, _tool_event(message))
            return []
        if kind == "usage":
            usage = _usage_of(message)
            publish_event(
                on_event,
                UsageReported(
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                ),
            )
            return []
        if kind == "task":
            publish_event(on_event, _worker_event(message))
            return []
        if kind == "status":
            return []
        return []


class SdkCursorFactory:
    def __init__(
        self,
        *,
        cwd: str,
        model: str,
        api_key: str,
        agent_id: str = "",
        permission_mode: str = "auto",
    ) -> None:
        self.cwd = cwd
        self.model = model
        self.api_key = api_key
        self.agent_id = agent_id
        self.permission_mode = permission_mode
        self._client: Any = None
        self._client_cm: Any = None

    async def start(self) -> CursorAgent:
        if self.permission_mode == "pending":
            raise CursorSdkError("Cursor SDK has no pending tool-approval callback.")
        if self.permission_mode == "deny":
            from sleeptalk.agents.cursor_sdk_isolation import bridge, create_agent
            if self.agent_id or not self.api_key:
                raise CursorSdkError("Isolated Cursor SDK requires a native API key and a new local session.")
            manager = bridge(self.cwd)
            client = await manager.__aenter__()
            try:
                agent = await create_agent(client, self.cwd, self.model, self.api_key)
            except BaseException:
                await manager.__aexit__(None, None, None)
                raise
            return _OwnedAgent(agent, manager)
        try:
            from cursor_sdk import AsyncClient, LocalAgentOptions
        except ImportError as exc:
            raise CursorSdkError(
                "cursor-sdk is not installed. Install sleeptalk[cursor]."
            ) from exc
        api_key = self.api_key or os.environ.get("CURSOR_API_KEY", "").strip()
        if not api_key:
            raise CursorSdkError("CURSOR_API_KEY is missing.")
        if any(value and is_voice_credential(name) for name, value in os.environ.items()):
            raise CursorSdkError(
                "Cursor SDK cannot isolate inherited voice credentials. Use private credential "
                "references and remove speech and bot keys from the parent environment."
            )
        self._client_cm = await AsyncClient.launch_bridge(workspace=self.cwd)
        self._client = await self._client_cm.__aenter__()
        try:
            options = LocalAgentOptions(
                **build_local_agent_options(self.cwd, local_setting_sources())
            )
        except TypeError:
            options = LocalAgentOptions(cwd=self.cwd)
        if self.agent_id:
            agent = await self._client.agents.resume(
                self.agent_id,
                api_key=api_key,
            )
        else:
            agent = await self._client.agents.create(
                model=self.model,
                api_key=api_key,
                local=options,
            )
        return _OwnedAgent(agent, self._client_cm)

    async def close(self) -> None:
        return None


class _OwnedAgent:
    def __init__(self, agent: Any, client_cm: Any) -> None:
        self._agent = agent
        self._client_cm = client_cm

    @property
    def agent_id(self) -> str:
        return str(getattr(self._agent, "agent_id", "") or "")

    async def send(self, text: str) -> CursorRun:
        return await self._agent.send(text)

    async def close(self) -> None:
        try:
            closer = getattr(self._agent, "aclose", None) or getattr(self._agent, "close", None)
            if closer is not None:
                result = closer()
                if hasattr(result, "__await__"):
                    await result
        finally:
            if self._client_cm is not None:
                await self._client_cm.__aexit__(None, None, None)


def _message_type(message: Any) -> str:
    kind = getattr(message, "type", None)
    if kind:
        return str(kind)
    if isinstance(message, dict):
        return str(message.get("type") or "")
    return ""


def _assistant_text(message: Any) -> str:
    payload = getattr(message, "message", None)
    content = getattr(payload, "content", None) if payload is not None else None
    if content is None and isinstance(message, dict):
        inner = message.get("message") or message
        if isinstance(inner, dict):
            content = inner.get("content")
        elif isinstance(message.get("text"), str):
            return str(message["text"])
    if isinstance(content, str):
        return content
    chunks: list[str] = []
    if isinstance(content, Sequence) and not isinstance(content, (str, bytes)):
        for block in content:
            kind = getattr(block, "type", None) or (
                block.get("type") if isinstance(block, dict) else ""
            )
            if str(kind) != "text":
                continue
            text = getattr(block, "text", None)
            if text is None and isinstance(block, dict):
                text = block.get("text")
            if text:
                chunks.append(str(text))
    return "".join(chunks)


def _attr_or_key(message: Any, name: str) -> Any:
    value = getattr(message, name, None)
    if value not in (None, ""):
        return value
    if isinstance(message, dict):
        return message.get(name)
    return None


def _tool_name(message: Any) -> str:
    return str(_attr_or_key(message, "name") or "")


def _worker_event(message: Any) -> WorkerEvent:
    worker_id = str(
        _attr_or_key(message, "call_id")
        or _attr_or_key(message, "agent_id")
        or "cursor-task"
    )
    label = str(_attr_or_key(message, "text") or "")
    args = _attr_or_key(message, "args")
    if not label and isinstance(args, dict):
        label = str(
            args.get("description") or args.get("prompt") or args.get("subagent_type") or ""
        )
    return WorkerEvent(
        worker_id=worker_id,
        phase=_task_phase(message),
        label=label or None,
        detached=True,
    )


def _tool_event(message: Any) -> ToolEvent:
    name = _tool_name(message)
    status = str(getattr(message, "status", "") or "")
    if not status and isinstance(message, dict):
        status = str(message.get("status") or "")
    args = getattr(message, "args", None)
    if args is None and isinstance(message, dict):
        args = message.get("args")
    kind, detail = narrate_tool(name, args if isinstance(args, dict) else {}, TOOL_VOCAB)
    phase = ToolPhase.STARTED
    if status in {"completed", "complete"}:
        phase = ToolPhase.COMPLETED
    elif status == "error":
        phase = ToolPhase.FAILED
    call_id = getattr(message, "call_id", None)
    if call_id is None and isinstance(message, dict):
        call_id = message.get("call_id")
    return ToolEvent(
        name=kind or name or "tool",
        call_id=str(call_id) if call_id else None,
        phase=phase,
        detail=detail or None,
    )


def _usage_of(message: Any) -> _Usage:
    usage = getattr(message, "usage", None)
    if usage is None and isinstance(message, dict):
        usage = message.get("usage")
    incoming = getattr(usage, "input_tokens", None)
    outgoing = getattr(usage, "output_tokens", None)
    if incoming is None and isinstance(usage, dict):
        incoming = usage.get("input_tokens")
        outgoing = usage.get("output_tokens")
    return _Usage(
        input_tokens=int(incoming) if isinstance(incoming, (int, float)) else None,
        output_tokens=int(outgoing) if isinstance(outgoing, (int, float)) else None,
    )


def _task_phase(message: Any) -> WorkerPhase:
    status = str(getattr(message, "status", "") or "")
    if not status and isinstance(message, dict):
        status = str(message.get("status") or "")
    if status in {"completed", "complete"}:
        return WorkerPhase.COMPLETED
    if status == "error":
        return WorkerPhase.FAILED
    if status == "cancelled":
        return WorkerPhase.CANCELLED
    if status == "running":
        return WorkerPhase.RUNNING
    return WorkerPhase.STARTED
