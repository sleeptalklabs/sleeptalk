"""Hermes TUI Gateway adapter.

Canonical surface is TUI Gateway JSON-RPC over stdio or WebSocket
(`hermes dashboard` / `tui_gateway.entry`). A configured `gateway_url` joins
that dashboard and never spawns a local TUI process. `hermes acp` is the
local fallback only when no gateway_url is set.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import os
import shutil
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

import aiohttp

from sleeptalk.agent_environment import agent_environment

from sleeptalk.adapter import (
    AgentCapabilityManifest,
    AgentControl,
    AnswerClarification,
    AssistantDelta,
    Cancel,
    CapabilitySupport,
    ClarificationRequested,
    ControlAccepted,
    ControlRejected,
    ControlResult,
    PermissionDecision,
    PermissionRequested,
    ResolvePermission,
    SessionIdentified,
    Steer,
    SupportLevel,
    ReasoningDelta,
    ThinkingStarted,
    ToolEvent,
    ToolPhase,
    VoiceSessionCapability,
    WorkerCapability,
    WorkerEvidence,
    publish_event,
)
from sleeptalk.agents.hermes_workers import (
    is_child_target,
    is_delegate_tool_name,
    worker_from_gateway,
)
from sleeptalk.streaming import (
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
        "terminal": "bash",
        "bash": "bash",
        "execute": "bash",
        "read_file": "read",
        "read": "read",
        "write_file": "write",
        "write": "write",
        "edit_file": "edit",
        "edit": "edit",
        "web_search": "search",
        "websearch": "search",
        "search": "search",
        "web_extract": "search",
        "webfetch": "search",
        "grep": "search",
        "glob": "search",
        "browser": "search",
    },
    subject_args={
        "bash": ("command",),
        "read": ("path", "file_path"),
        "write": ("path", "file_path"),
        "edit": ("path", "file_path"),
        "search": ("query", "pattern"),
    },
)

_SUPPORTED = CapabilitySupport(SupportLevel.SUPPORTED)
_UNSUPPORTED = CapabilitySupport(SupportLevel.UNSUPPORTED)

HERMES_GATEWAY_MANIFEST = AgentCapabilityManifest(
    contract_version=1,
    structured_events=_SUPPORTED,
    tool_lifecycle=_SUPPORTED,
    permissions=_SUPPORTED,
    clarification=_SUPPORTED,
    session_identity=_SUPPORTED,
    status=_UNSUPPORTED,
    usage=_UNSUPPORTED,
    steering=_SUPPORTED,
    targeted_cancellation=_SUPPORTED,
    workers=WorkerCapability(
        SupportLevel.SUPPORTED, evidence=WorkerEvidence.LIFECYCLE_NATIVE
    ),
    voice_sessions=VoiceSessionCapability(SupportLevel.UNSUPPORTED),
)

_APPROVAL_TO_DECISION = {
    "once": PermissionDecision.ALLOW_ONCE,
    "session": PermissionDecision.ALLOW_SESSION,
    "always": PermissionDecision.ALLOW_SESSION,
    "deny": PermissionDecision.DENY,
}
_DECISION_TO_APPROVAL = {
    PermissionDecision.ALLOW_ONCE: "once",
    PermissionDecision.ALLOW_SESSION: "session",
    PermissionDecision.DENY: "deny",
}

TURN_DONE = frozenset(
    {"message.complete", "error", "run.completed", "run.failed", "run.cancelled"}
)
# How long a new prompt waits for a cancelled run to report its end.
_SETTLE_PREVIOUS_RUN_S = 5.0
# A run interrupted before its model answered leaves that model call running
# inside Hermes, and once the next run starts, its text streams out as that
# run's own message.delta events: two replies woven together mid-word, job
# tags broken, a brief read aloud. For this long after such an interrupt only
# the clean text a run reports in message.complete is used.
_ORPHANED_STREAM_S = 60.0


def _safe_reason(exc: BaseException) -> str:
    """Why a gateway connection failed, without the URL (which holds the token)."""
    status = getattr(exc, "status", None)
    if isinstance(status, int) and status:
        return f"HTTP {status}" + (" (the session token was refused)" if status in {401, 403} else "")
    return type(exc).__name__


class HermesGatewayError(RuntimeError):
    pass


class TuiGatewayLink(Protocol):
    async def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]: ...

    async def events(self) -> asyncio.Queue[dict[str, Any]]: ...

    async def close(self) -> None: ...


@dataclass
class _OpenAsk:
    kind: str
    request_id: str


def inspect_awaitable(value: Any) -> bool:
    return asyncio.iscoroutine(value) or asyncio.isfuture(value)


class QueueLink:
    """In-process JSON-RPC link used by tests."""

    def __init__(self, handler: Callable[[str, dict[str, Any]], Any]) -> None:
        self._handler = handler
        self._events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = params or {}
        self.calls.append((method, payload))
        result = self._handler(method, payload)
        if inspect_awaitable(result):
            result = await result
        if not isinstance(result, dict):
            raise HermesGatewayError(f"{method} returned {type(result).__name__}")
        return result

    async def events(self) -> asyncio.Queue[dict[str, Any]]:
        return self._events

    async def push(self, event: dict[str, Any]) -> None:
        await self._events.put(event)

    async def close(self) -> None:
        return None


class _JsonRpcMux:
    def __init__(self) -> None:
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._ids = itertools.count(1)
        self._events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._write_lock = asyncio.Lock()

    def next_id(self) -> int:
        return next(self._ids)

    def wait(self, req_id: int) -> asyncio.Future[dict[str, Any]]:
        future = asyncio.get_running_loop().create_future()
        self._pending[req_id] = future
        return future

    async def ingest(self, message: dict[str, Any]) -> None:
        if "id" in message and ("result" in message or "error" in message):
            future = self._pending.pop(int(message["id"]), None)
            if future is not None and not future.done():
                if "error" in message:
                    future.set_exception(HermesGatewayError(str(message["error"])))
                else:
                    result = message.get("result") or {}
                    future.set_result(result if isinstance(result, dict) else {})
            return
        if message.get("method") == "event":
            params = message.get("params") or {}
            if isinstance(params, dict):
                await self._events.put(params)


class StdioTuiLink:
    def __init__(self, argv: list[str], *, cwd: str, env: dict[str, str] | None = None) -> None:
        self._argv = argv
        self._cwd = cwd
        self._env = env
        self._proc: asyncio.subprocess.Process | None = None
        self._mux = _JsonRpcMux()
        self._reader: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._proc is not None:
            return
        self._proc = await asyncio.create_subprocess_exec(
            *self._argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            cwd=self._cwd,
            env=agent_environment(self._env),
        )
        self._reader = asyncio.create_task(self._read(), name="hermes-tui-reader")
        ready = await asyncio.wait_for(self._mux._events.get(), timeout=15.0)
        if ready.get("type") != "gateway.ready":
            raise HermesGatewayError(
                f"incompatible Hermes gateway: first event was {ready.get('type')!r}"
            )

    async def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if self._proc is None or self._proc.stdin is None:
            raise HermesGatewayError("Hermes TUI gateway is not running.")
        req_id = self._mux.next_id()
        future = self._mux.wait(req_id)
        frame = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}}
        async with self._mux._write_lock:
            self._proc.stdin.write((json.dumps(frame) + "\n").encode())
            await self._proc.stdin.drain()
        return await asyncio.wait_for(future, timeout=30.0)

    async def events(self) -> asyncio.Queue[dict[str, Any]]:
        return self._mux._events

    async def _read(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        try:
            while True:
                raw = await self._proc.stdout.readline()
                if not raw:
                    break
                try:
                    message = json.loads(raw.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if isinstance(message, dict):
                    await self._mux.ingest(message)
        except asyncio.CancelledError:
            return

    async def close(self) -> None:
        if self._reader is not None:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader
            self._reader = None
        if self._proc is not None and self._proc.returncode is None:
            self._proc.terminate()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._proc.wait(), timeout=5.0)
        self._proc = None


class WebsocketTuiLink:
    def __init__(self, url: str, *, api_key: str = "") -> None:
        self._url = url
        self._api_key = api_key
        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._mux = _JsonRpcMux()
        self._reader: asyncio.Task[None] | None = None

    async def start(self) -> None:
        headers = {}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        self._session = aiohttp.ClientSession()
        try:
            self._ws = await self._session.ws_connect(self._url, headers=headers)
        except aiohttp.ClientError as exc:
            await self.close()
            # The socket URL carries the session token, and aiohttp's own
            # message quotes it, so the reason is rebuilt without the URL.
            raise HermesGatewayError(f"configured Hermes gateway unreachable: {_safe_reason(exc)}") from None
        self._reader = asyncio.create_task(self._read(), name="hermes-tui-ws")
        ready = await asyncio.wait_for(self._mux._events.get(), timeout=15.0)
        if ready.get("type") != "gateway.ready":
            await self.close()
            raise HermesGatewayError(
                f"incompatible Hermes gateway: first event was {ready.get('type')!r}"
            )

    async def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if self._ws is None or self._ws.closed:
            raise HermesGatewayError("Hermes TUI gateway websocket is not connected.")
        req_id = self._mux.next_id()
        future = self._mux.wait(req_id)
        frame = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}}
        async with self._mux._write_lock:
            await self._ws.send_str(json.dumps(frame))
        return await asyncio.wait_for(future, timeout=30.0)

    async def events(self) -> asyncio.Queue[dict[str, Any]]:
        return self._mux._events

    async def _read(self) -> None:
        assert self._ws is not None
        try:
            async for msg in self._ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    if msg.type in {aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR}:
                        break
                    continue
                try:
                    message = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue
                if isinstance(message, dict):
                    await self._mux.ingest(message)
        except asyncio.CancelledError:
            return

    async def close(self) -> None:
        if self._reader is not None:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader
            self._reader = None
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()
            self._ws = None
        if self._session is not None:
            await self._session.close()
            self._session = None


class HermesGatewayClient:
    """AgentAdapter for a Hermes TUI Gateway session, with ACP fallback."""

    agent_capabilities = HERMES_GATEWAY_MANIFEST

    def __init__(
        self,
        *,
        base_url: str = "",
        api_key: str = "",
        session_token: str = "",
        session_id: str = "",
        name: str = "hermes-gateway",
        cwd: str | None = None,
        permission_mode: str = "auto",
        keepalive_interval_s: float = 0.0,
        tui_python: str = "",
        hermes_root: str = "",
        bridge_config=None,
        link: TuiGatewayLink | None = None,
        fallback_factory: Callable[[], Any] | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self._bridge_config = bridge_config
        if bridge_config is not None:
            self.agent_capabilities = replace(HERMES_GATEWAY_MANIFEST, tool_lifecycle=_UNSUPPORTED,
                                              clarification=_UNSUPPORTED, steering=_UNSUPPORTED,
                                              workers=WorkerCapability(SupportLevel.UNSUPPORTED))
        self._bridge_launch = None
        self._env = agent_environment(env)
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.session_token = session_token.strip()
        self.session_id = session_id
        self._existing_session = bool(session_id)
        self.name = name
        self.cwd = cwd or os.getcwd()
        self.permission_mode = permission_mode.strip().lower() or "auto"
        self.keepalive_interval_s = keepalive_interval_s
        self.tui_python = tui_python
        self.hermes_root = hermes_root
        self._injected_link = link
        self._fallback_factory = fallback_factory
        self._link: TuiGatewayLink | None = None
        self._fallback: Any | None = None
        self.fallback_reason: str | None = None
        self._lock = asyncio.Lock()
        self._closed = False
        self._pending_ask: _OpenAsk | None = None
        self._identified = False
        self._pending: dict[str, bool] = {}
        # A prompt was submitted and its run has not reported an end yet.
        self._run_open = False
        # Until then, streamed text may come from an orphaned model call.
        self._orphaned_until = 0.0

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
            await self._ensure_started()
            if self._fallback is not None:
                async for sentence in self._fallback.respond_streaming(
                    text, timeout=timeout, on_event=on_event
                ):
                    yield sentence
                return
            assert self._link is not None
            self._emit_identity(on_event)
            events = await self._link.events()
            await self._settle_previous_run(events)
            self._pending["turn"] = True
            try:
                submitted = await self._link.request(
                    "prompt.submit",
                    {"session_id": self.session_id, "text": text},
                )
                self._run_open = True
                # Hermes still busy with an earlier run (one that would not
                # stop in time) interrupts it and queues this prompt, merged
                # with any other queued one. Until that run ends and ours
                # starts, every event belongs to it: read as ours, its tail
                # once spoke a job brief aloud for two minutes.
                queued = isinstance(submitted, dict) and submitted.get("status") == "queued"
                waiting_for = "done" if queued else ""
                streamed = False
                streamer = SentenceStreamer()
                gate = EventGate(on_event)
                started: set[str] = set()
                get_task: asyncio.Future[dict[str, Any]] | None = None
                clock = DeadlineClock(
                    timeout,
                    idle_error=lambda: HermesGatewayError("Hermes gateway response timed out."),
                    hard_error=lambda: HermesGatewayError(
                        "Hermes gateway response exceeded maximum time."
                    ),
                    keepalive_interval_s=self.keepalive_interval_s,
                    on_keepalive=lambda: gate.emit("keepalive"),
                )
                while True:
                    get_task = asyncio.ensure_future(events.get())
                    which = await await_activity(clock, {"event": get_task})
                    if which == KEEPALIVE:
                        get_task.cancel()
                        continue
                    event = get_task.result()
                    clock.note_activity()
                    kind = str(event.get("type") or "")
                    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
                    if waiting_for == "done":
                        if kind in TURN_DONE:
                            self._note_run_end(payload)
                            waiting_for = "start"
                        continue
                    if waiting_for == "start":
                        if kind == "message.start":
                            waiting_for = ""
                        continue
                    worker = worker_from_gateway(kind, payload)
                    if worker is not None:
                        publish_event(on_event, worker)
                        continue
                    if kind.startswith("subagent.") or (
                        kind in {"tool.start", "tool.progress", "tool.complete"}
                        and is_delegate_tool_name(
                            str(payload.get("tool_name") or payload.get("name") or "")
                        )
                    ):
                        continue
                    if kind in {"thinking.delta", "reasoning.delta"}:
                        if "thinking" not in started:
                            started.add("thinking")
                            publish_event(on_event, ThinkingStarted())
                        if kind == "reasoning.delta" and payload.get("text"):
                            publish_event(on_event, ReasoningDelta(text=str(payload["text"])))
                        continue
                    if kind == "tool.generating":
                        # The model is writing a tool call's arguments (a
                        # whole file can take minutes): nothing else arrives
                        # until it ends.
                        raw_name = str(payload.get("name") or "")
                        publish_event(on_event, ToolEvent(
                            name="", phase=ToolPhase.UPDATED, detail=None,
                            display_name=raw_name or None, label="writing…",
                        ))
                        continue
                    if kind == "message.delta":
                        if time.monotonic() < self._orphaned_until:
                            continue  # may be another call's text; see message.complete
                        delta = str(payload.get("text") or payload.get("delta") or "")
                        if delta:
                            streamed = True
                            publish_event(on_event, AssistantDelta(text=delta))
                        for sentence in streamer.feed(delta):
                            clock.note_audible()
                            yield sentence
                        continue
                    if kind == "tool.start":
                        mapped, detail = _tool_event(payload)
                        raw_name, label, call_id = _tool_display(payload)
                        if detail:
                            publish_event(
                                on_event,
                                ToolEvent(name="", phase=ToolPhase.UPDATED, detail=detail),
                            )
                            clock.note_audible()
                        publish_event(
                            on_event,
                            ToolEvent(
                                name=mapped or raw_name,
                                call_id=call_id,
                                phase=ToolPhase.STARTED,
                                detail=detail or None,
                                display_name=raw_name or None,
                                label=label or None,
                            ),
                        )
                        continue
                    if kind == "tool.progress":
                        mapped, detail = _tool_event(payload)
                        if detail:
                            publish_event(
                                on_event,
                                ToolEvent(
                                    name=mapped,
                                    phase=ToolPhase.UPDATED,
                                    detail=detail,
                                ),
                            )
                        continue
                    if kind == "tool.complete":
                        mapped, detail = _tool_event(payload)
                        raw_name, label, call_id = _tool_display(payload)
                        publish_event(
                            on_event,
                            ToolEvent(
                                name=mapped,
                                call_id=call_id,
                                phase=ToolPhase.COMPLETED,
                                detail=detail or None,
                                display_name=raw_name or None,
                                label=label or None,
                            ),
                        )
                        continue
                    if kind == "approval.request":
                        request_id = str(
                            payload.get("request_id") or payload.get("id") or "approval"
                        )
                        self._pending_ask = _OpenAsk("approval", request_id)
                        choices = (
                            payload.get("choices")
                            if isinstance(payload.get("choices"), list)
                            else []
                        )
                        publish_event(
                            on_event,
                            PermissionRequested(
                                request_id=request_id,
                                prompt=str(
                                    payload.get("command")
                                    or payload.get("message")
                                    or "approve tool"
                                ),
                                options=_decisions_from_choices(choices),
                            ),
                        )
                        if self.permission_mode != "pending":
                            choice = "deny" if self.permission_mode == "deny" else "once"
                            await self._link.request(
                                "approval.respond",
                                {"session_id": self.session_id, "choice": choice},
                            )
                        continue
                    if kind == "clarify.request":
                        request_id = str(
                            payload.get("request_id") or payload.get("id") or "clarify"
                        )
                        self._pending_ask = _OpenAsk("clarify", request_id)
                        raw_choices = (
                            payload.get("choices")
                            if isinstance(payload.get("choices"), list)
                            else []
                        )
                        publish_event(
                            on_event,
                            ClarificationRequested(
                                request_id=request_id,
                                prompt=str(payload.get("prompt") or payload.get("message") or ""),
                                choices=tuple(str(item) for item in raw_choices),
                            ),
                        )
                        continue
                    if kind in TURN_DONE:
                        self._run_open = False
                        final = str(payload.get("text") or "") if kind == "message.complete" else ""
                        if final and not streamed and not _interrupted(final):
                            # Nothing usable streamed (a short reply Hermes
                            # sends only here, or deltas set aside above).
                            publish_event(on_event, AssistantDelta(text=final))
                            for sentence in streamer.feed(final):
                                clock.note_audible()
                                yield sentence
                        self._note_run_end(payload)
                        if kind in {"error", "run.failed"}:
                            raise HermesGatewayError(
                                str(
                                    payload.get("message")
                                    or event.get("message")
                                    or "Hermes gateway failed"
                                )
                            )
                        break
                    continue
                tail = streamer.flush()
                if tail:
                    yield tail
            finally:
                # A turn cancelled while waiting leaves its read pending on
                # the shared queue, where it would take the next event (a
                # chunk of the next reply, or the end of a run) and drop it.
                if get_task is not None and not get_task.done():
                    get_task.cancel()
                self._pending.clear()

    async def _settle_previous_run(self, events: asyncio.Queue[dict[str, Any]]) -> None:
        """Let a cancelled run finish before the next prompt.

        The gateway's events carry no run id. A reply cancelled mid-stream
        (the caller kept talking) goes on sending text until Hermes stops it,
        and that text was read as the next reply: two answers woven into one,
        mid-word."""
        stale_done = False
        while True:
            try:
                stale = events.get_nowait()
            except asyncio.QueueEmpty:
                break
            if str(stale.get("type") or "") in TURN_DONE:
                stale_done = True
                self._note_run_end(stale.get("payload"))
        if not self._run_open or stale_done:
            self._run_open = False
            return
        assert self._link is not None
        with contextlib.suppress(Exception):
            await self._link.request("session.interrupt", {"session_id": self.session_id})
        deadline = time.monotonic() + _SETTLE_PREVIOUS_RUN_S
        while (left := deadline - time.monotonic()) > 0:
            try:
                stale = await asyncio.wait_for(events.get(), left)
            except TimeoutError:
                break
            if str(stale.get("type") or "") in TURN_DONE:
                self._note_run_end(stale.get("payload"))
                break
        self._run_open = False

    def _note_run_end(self, payload: Any) -> None:
        text = str(payload.get("text") or "") if isinstance(payload, dict) else ""
        if _interrupted(text) and "waiting for model" in text.lower():
            self._orphaned_until = time.monotonic() + _ORPHANED_STREAM_S

    async def abort(self) -> None:
        if self._fallback is not None:
            await self._fallback.abort()
            return
        if self._link is None or not self.session_id:
            return
        await self._link.request("session.interrupt", {"session_id": self.session_id})

    async def warmup(self) -> None:
        await self._ensure_started()

    async def close(self) -> None:
        self._closed = True
        if self._fallback is not None:
            await self._fallback.close()
        if self._link is not None:
            await self._link.close()
        if self._bridge_launch is not None:
            self._bridge_launch.close()
            self._bridge_launch = None

    async def control(self, command: AgentControl) -> ControlResult:
        if self._bridge_config is not None:
            if isinstance(command, (Steer, AnswerClarification)):
                return ControlRejected(reason="The isolated Hermes bridge does not support this control", retryable=False)
            if isinstance(command, ResolvePermission) and command.decision not in {PermissionDecision.ALLOW_ONCE, PermissionDecision.DENY}:
                return ControlRejected(reason="Only one-time approval or denial is offered", retryable=False)
        await self._ensure_started()
        if self._fallback is not None:
            return await _delegate_control(self._fallback, command)
        if self._link is None:
            return ControlRejected(reason="hermes gateway is not connected", retryable=True)
        if isinstance(command, Steer):
            if command.target_id and is_child_target(command.target_id):
                result = await self._link.request(
                    "subagent.steer",
                    {
                        "session_id": self.session_id,
                        "subagent_id": command.target_id,
                        "text": command.instruction,
                    },
                )
                resource = command.target_id
            else:
                result = await self._link.request(
                    "session.steer",
                    {"session_id": self.session_id, "text": command.instruction},
                )
                resource = self.session_id
            if str(result.get("status") or "") == "rejected":
                return ControlRejected(reason="hermes rejected steer", retryable=False)
            return ControlAccepted(request_id=None, resource=resource)
        if isinstance(command, ResolvePermission):
            await self._link.request(
                "approval.respond",
                {
                    "session_id": self.session_id,
                    "choice": _DECISION_TO_APPROVAL.get(command.decision, "deny"),
                    **({"request_id": command.request_id} if self._bridge_config is not None else {}),
                },
            )
            return ControlAccepted(request_id=command.request_id)
        if isinstance(command, AnswerClarification):
            await self._link.request(
                "clarify.respond",
                {"session_id": self.session_id, "text": command.answer},
            )
            return ControlAccepted(request_id=command.request_id)
        if isinstance(command, Cancel):
            if is_child_target(command.target_id):
                await self._link.request(
                    "subagent.interrupt",
                    {"session_id": self.session_id, "subagent_id": command.target_id},
                )
                return ControlAccepted(
                    request_id=command.target_id, resource=command.target_id
                )
            await self._link.request("session.interrupt", {"session_id": self.session_id})
            return ControlAccepted(request_id=command.target_id, resource=self.session_id)
        return ControlRejected(reason="unsupported control", retryable=False)

    def _emit_identity(self, on_event: Callable[..., Any] | None) -> None:
        if self._identified or not self.session_id:
            return
        self._identified = True
        publish_event(
            on_event,
            SessionIdentified(
                upstream_session_id=self.session_id,
                resumable=self._existing_session,
            ),
        )

    async def _ensure_started(self) -> None:
        if self._closed:
            raise HermesGatewayError("Hermes gateway client is closed.")
        if self._link is not None or self._fallback is not None:
            return
        try:
            self._link = await self._connect()
            if not self.session_id:
                created = await self._link.request(
                    "session.create", {"cwd": self.cwd, "title": self.name}
                )
                self.session_id = str(created.get("session_id") or "")
            if not self.session_id:
                raise HermesGatewayError("Hermes TUI gateway did not return a session_id.")
        except BaseException as exc:
            if self._bridge_config is not None:
                link, self._link = self._link, None
                self.session_id = ""
                try:
                    if link is not None:
                        await link.close()
                finally:
                    if self._bridge_launch is not None:
                        self._bridge_launch.close()
                        self._bridge_launch = None
                if isinstance(exc, asyncio.CancelledError):
                    raise
                raise HermesGatewayError("The selected isolated Hermes gateway could not start") from exc
            if not isinstance(exc, Exception):
                raise
            if self.base_url:
                raise HermesGatewayError(
                    f"configured Hermes dashboard unavailable: {exc}"
                ) from exc
            self.fallback_reason = str(exc)
            self._link = None
            self._fallback = await self._open_fallback()

    async def _connect(self) -> TuiGatewayLink:
        if self._injected_link is not None:
            return self._injected_link
        if self._bridge_config is not None:
            from sleeptalk.agents.hermes_bridge import prepare
            self._bridge_launch = prepare(self._bridge_config, self._env, transport="gateway")
            link = StdioTuiLink(self._bridge_launch.args, cwd=self._bridge_launch.home.name, env=self._bridge_launch.env)
            try:
                await link.start()
            except BaseException:
                await link.close()
                raise
            return link
        if self.base_url:
            token = self.session_token
            from sleeptalk.hermes_connect import is_loopback_origin, read_local_session_token

            if is_loopback_origin(self.base_url):
                # A local dashboard issues a new token each start; prefer its current one.
                token = await read_local_session_token(self.base_url) or token
            link = WebsocketTuiLink(
                _ws_url(self.base_url, token), api_key=self.api_key
            )
            await link.start()
            return link
        python, root = _tui_launch(self.tui_python, self.hermes_root)
        if not python:
            raise HermesGatewayError(
                "Hermes TUI gateway python not found. Set HERMES_AGENT_ROOT or install Hermes."
            )
        link = StdioTuiLink(
            [python, "-m", "tui_gateway.entry"],
            cwd=root or self.cwd,
            env=_tui_env(root, self._env),
        )
        await link.start()
        return link

    async def _open_fallback(self) -> Any:
        if self._fallback_factory is not None:
            fallback = self._fallback_factory()
            await fallback.warmup()
            return fallback
        from sleeptalk.agents.hermes_adapter import HermesACPClient

        try:
            fallback = HermesACPClient(
                cwd=self.cwd, name=self.name, permission_mode=self.permission_mode, env=self._env
            )
            await fallback.warmup()
        except Exception as exc:
            raise HermesGatewayError(
                f"Hermes TUI gateway unavailable ({self.fallback_reason}); "
                f"ACP fallback failed ({exc})"
            ) from exc
        return fallback


def _tool_display(data: dict[str, Any]) -> tuple[str, str, str | None]:
    """(Hermes's tool name, its label of the call, the call id) for display."""
    name = str(data.get("tool_name") or data.get("name") or "").strip()
    label = " ".join(str(data.get("context") or "").split())
    call_id = str(data.get("tool_id") or data.get("id") or "") or None
    return name, label, call_id


def _interrupted(text: str) -> bool:
    """Hermes's end-of-run text for a run it stopped ("Operation interrupted: ...")."""
    return text.strip().lower().startswith("operation interrupted")


def _tool_event(data: dict[str, Any]) -> tuple[str, str]:
    """(narration kind, spoken detail) from a tool payload."""
    name = str(data.get("tool_name") or data.get("name") or data.get("title") or "").strip()
    args = data.get("args") if isinstance(data.get("args"), dict) else {}
    return narrate_tool(name, args, TOOL_VOCAB)


def _decisions_from_choices(choices: list[Any]) -> frozenset[PermissionDecision]:
    found = {
        _APPROVAL_TO_DECISION[str(choice)]
        for choice in choices
        if str(choice) in _APPROVAL_TO_DECISION
    }
    return frozenset(found) or frozenset(PermissionDecision)


def _tui_launch(tui_python: str, hermes_root: str) -> tuple[str, str]:
    if tui_python:
        root = hermes_root or os.environ.get("HERMES_AGENT_ROOT", "").strip()
        return tui_python, root
    roots: list[Path] = []
    if hermes_root:
        roots.append(Path(hermes_root))
    env_root = os.environ.get("HERMES_AGENT_ROOT", "").strip()
    if env_root:
        roots.append(Path(env_root))
    roots.append(Path.home() / ".hermes" / "hermes-agent")
    for root in roots:
        candidate = root / "venv" / "bin" / "python"
        if candidate.is_file():
            return str(candidate), str(root)
    hermes = shutil.which("hermes")
    if hermes:
        sibling = Path(hermes).resolve().parent.parent / "hermes-agent"
        python = sibling / "venv" / "bin" / "python"
        if python.is_file():
            return str(python), str(sibling)
    return "", ""


def _tui_env(hermes_root: str, environment: dict[str, str] | None = None) -> dict[str, str]:
    env = agent_environment(environment)
    if hermes_root:
        current = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = hermes_root if not current else f"{hermes_root}:{current}"
    return env


def _ws_url(base_url: str, session_token: str = "") -> str:
    parts = urlsplit(base_url)
    scheme = parts.scheme
    if scheme not in {"ws", "wss"}:
        scheme = "wss" if scheme == "https" else "ws"
    path = parts.path.rstrip("/")
    if not path.endswith("/api/ws"):
        path = "/api/ws"
    query = dict(
        (key, values[-1])
        for key, values in parse_qs(parts.query).items()
        if values
    )
    token = session_token.strip() or query.get("token", "")
    if token:
        query["token"] = token
    return urlunsplit((scheme, parts.netloc, path, urlencode(query), ""))


async def _delegate_control(agent: Any, command: AgentControl) -> ControlResult:
    control = getattr(agent, "control", None)
    if control is None:
        return ControlRejected(reason="fallback agent does not implement control", retryable=False)
    result = control(command)
    if inspect_awaitable(result):
        result = await result
    if isinstance(result, (ControlAccepted, ControlRejected)):
        return result
    return ControlRejected(reason="fallback returned an invalid control result", retryable=False)
