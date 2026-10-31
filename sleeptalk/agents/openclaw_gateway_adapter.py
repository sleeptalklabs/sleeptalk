"""OpenClaw Gateway WebSocket adapter.

Canonical surface is the Gateway control plane (protocol v4). `openclaw acp`
is the local fallback for availability/protocol failures; rejected credentials
stop startup.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
import os
import ssl
import sys
import time
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlsplit, urlunsplit

import aiohttp

from sleeptalk.harness_versions import install_spec

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
    Steer,
    SupportLevel,
    ThinkingStarted,
    ToolEvent,
    ToolPhase,
    VoiceSessionCapability,
    WorkerCapability,
    publish_event,
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

logger = logging.getLogger(__name__)

PROTOCOL = 4
DEFAULT_GATEWAY_URL = "ws://127.0.0.1:18789"
OPERATOR_SCOPES = ("operator.read", "operator.write", "operator.approvals")
TOOL_EVENTS_CAP = "tool-events"
CONNECT_TIMEOUT_S = 15.0
REQUEST_TIMEOUT_S = 30.0
# A gateway that was just started answers "gateway starting" until its
# sidecars are up; a slow host can take several seconds.
STARTUP_SIDECAR_WAIT_S = 20.0

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
        "search": "search",
        "web_extract": "search",
    },
    subject_args={
        "bash": ("command",),
        "read": ("path", "file_path"),
        "write": ("path", "file_path"),
        "edit": ("path", "file_path"),
        "search": ("query",),
    },
)

_SUPPORTED = CapabilitySupport(SupportLevel.SUPPORTED)
_UNSUPPORTED = CapabilitySupport(SupportLevel.UNSUPPORTED)

_DECISION_TO_GATEWAY = {
    PermissionDecision.ALLOW_ONCE: "allow-once",
    PermissionDecision.ALLOW_SESSION: "allow-always",
    PermissionDecision.DENY: "deny",
}
_GATEWAY_TO_DECISION = {
    "allow-once": PermissionDecision.ALLOW_ONCE,
    "allow-always": PermissionDecision.ALLOW_SESSION,
    "deny": PermissionDecision.DENY,
}
_TOOL_PHASE = {
    "start": ToolPhase.STARTED,
    "started": ToolPhase.STARTED,
    "update": ToolPhase.UPDATED,
    "updated": ToolPhase.UPDATED,
    "progress": ToolPhase.UPDATED,
    "end": ToolPhase.COMPLETED,
    "complete": ToolPhase.COMPLETED,
    "completed": ToolPhase.COMPLETED,
    "result": ToolPhase.COMPLETED,
    "error": ToolPhase.FAILED,
    "failed": ToolPhase.FAILED,
    "fail": ToolPhase.FAILED,
}


class OpenClawGatewayError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        reason: str = "",
        retry_after_ms: int | None = None,
        authentication_failed: bool = False,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.reason = reason
        self.retry_after_ms = retry_after_ms
        self.authentication_failed = authentication_failed


class GatewaySession(Protocol):
    async def connect(self) -> None: ...

    async def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]: ...

    async def events(self) -> asyncio.Queue[dict[str, Any]]: ...

    async def close(self) -> None: ...


@dataclass
class _InFlight:
    idempotency_key: str
    text: str
    session_key: str
    run_id: str = ""


def _manifest(*, steering: bool) -> AgentCapabilityManifest:
    return AgentCapabilityManifest(
        contract_version=1,
        structured_events=_SUPPORTED,
        tool_lifecycle=_SUPPORTED,
        permissions=_SUPPORTED,
        clarification=_UNSUPPORTED,
        session_identity=_SUPPORTED,
        status=_UNSUPPORTED,
        usage=_UNSUPPORTED,
        steering=_SUPPORTED if steering else _UNSUPPORTED,
        targeted_cancellation=_SUPPORTED,
        workers=WorkerCapability(SupportLevel.UNSUPPORTED),
        voice_sessions=VoiceSessionCapability(SupportLevel.UNSUPPORTED),
    )


class _FrameMux:
    def __init__(self) -> None:
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._ids = itertools.count(1)
        self._events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._write_lock = asyncio.Lock()

    def next_id(self) -> str:
        return str(next(self._ids))

    def wait(self, req_id: str) -> asyncio.Future[dict[str, Any]]:
        future = asyncio.get_running_loop().create_future()
        self._pending[req_id] = future
        return future

    def drop(self, req_id: str) -> None:
        future = self._pending.pop(req_id, None)
        if future is not None and not future.done():
            future.cancel()

    async def ingest(self, frame: dict[str, Any]) -> None:
        kind = str(frame.get("type") or "")
        if kind == "res":
            future = self._pending.pop(str(frame.get("id") or ""), None)
            if future is None or future.done():
                return
            if frame.get("ok") is False:
                future.set_exception(_error_from_frame(frame))
                return
            payload = frame.get("payload")
            future.set_result(payload if isinstance(payload, dict) else {})
            return
        if kind == "event":
            if frame.get("event") == "tick":
                return
            await self._events.put(
                {
                    "event": frame.get("event"),
                    "payload": frame.get("payload")
                    if isinstance(frame.get("payload"), dict)
                    else {},
                    "seq": frame.get("seq"),
                }
            )

    def events(self) -> asyncio.Queue[dict[str, Any]]:
        return self._events

    def fail_open(self, error: OpenClawGatewayError) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)
        self._pending.clear()
        self._events.put_nowait({"event": "_disconnect", "payload": {}})

    async def send(self, ws: aiohttp.ClientWebSocketResponse, frame: dict[str, Any]) -> None:
        async with self._write_lock:
            await ws.send_str(json.dumps(frame))


class AiohttpGatewaySession:
    def __init__(
        self,
        url: str,
        *,
        ca_file: str = "",
    ) -> None:
        self._url = _ws_url(url)
        self._ca_file = ca_file
        self._http: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._mux = _FrameMux()
        self._reader: asyncio.Task[None] | None = None

    async def connect(self) -> None:
        if self._ws is not None and not self._ws.closed:
            return
        await self.close()
        self._mux = _FrameMux()
        timeout = aiohttp.ClientTimeout(
            total=CONNECT_TIMEOUT_S,
            sock_connect=CONNECT_TIMEOUT_S,
        )
        self._http = aiohttp.ClientSession(timeout=timeout)
        try:
            self._ws = await asyncio.wait_for(
                self._http.ws_connect(self._url, ssl=_ssl_option(self._url, self._ca_file)),
                timeout=CONNECT_TIMEOUT_S,
            )
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise OpenClawGatewayError(
                f"OpenClaw gateway unreachable: {exc}", retryable=True
            ) from exc
        finally:
            if self._ws is None:
                await self.close()
        self._reader = asyncio.create_task(self._read(), name="openclaw-gateway-ws")

    async def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if self._ws is None or self._ws.closed:
            raise OpenClawGatewayError("OpenClaw gateway websocket is not connected.", retryable=True)
        req_id = self._mux.next_id()
        future = self._mux.wait(req_id)
        try:
            await self._mux.send(
                self._ws,
                {"type": "req", "id": req_id, "method": method, "params": params or {}},
            )
            return await asyncio.wait_for(future, timeout=REQUEST_TIMEOUT_S)
        except OpenClawGatewayError:
            raise
        except (aiohttp.ClientError, TimeoutError, ConnectionError, OSError) as exc:
            raise OpenClawGatewayError(
                f"OpenClaw gateway request failed: {exc}", retryable=True
            ) from exc
        finally:
            self._mux.drop(req_id)

    async def events(self) -> asyncio.Queue[dict[str, Any]]:
        return self._mux.events()

    async def _read(self) -> None:
        assert self._ws is not None
        try:
            async for msg in self._ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    if msg.type in {aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR}:
                        break
                    continue
                try:
                    frame = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue
                if isinstance(frame, dict):
                    await self._mux.ingest(frame)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            self._mux.fail_open(OpenClawGatewayError(str(exc), retryable=True))
            return
        self._mux.fail_open(OpenClawGatewayError("disconnected", retryable=True))

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
        if self._http is not None:
            await self._http.close()
            self._http = None
        self._mux.fail_open(OpenClawGatewayError("disconnected", retryable=True))


class OpenClawGatewayClient:
    def __init__(
        self,
        *,
        url: str = DEFAULT_GATEWAY_URL,
        token: str = "",
        password: str = "",
        session_key: str = "",
        name: str = "openclaw",
        cwd: str | None = None,
        permission_mode: str = "auto",
        keepalive_interval_s: float = 0.0,
        ca_file: str = "",
        session: GatewaySession | None = None,
        fallback_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.url = url.strip() or DEFAULT_GATEWAY_URL
        self.token = token
        self.password = password
        self.session_key = session_key
        self._existing_session = bool(session_key)
        self.name = name
        self.cwd = cwd or os.getcwd()
        self.permission_mode = permission_mode.strip().lower() or "auto"
        self.keepalive_interval_s = keepalive_interval_s
        self.ca_file = ca_file
        self._injected_session = session
        self._fallback_factory = fallback_factory
        self._session: GatewaySession | None = None
        self._fallback: Any | None = None
        self.fallback_reason: str | None = None
        self._lock = asyncio.Lock()
        self._start_lock = asyncio.Lock()
        self._closed = False
        self._identified = False
        self._inflight: _InFlight | None = None
        self._methods: frozenset[str] = frozenset()
        self._steer_supported = True
        self.agent_capabilities = _manifest(steering=True)

    async def respond(self, text: str, timeout: float = 300.0) -> str:
        from sleeptalk.text import extract_speakable_text

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
            self._raise_if_closed()
            if self._fallback is not None:
                async for sentence in self._fallback.respond_streaming(
                    text, timeout=timeout, on_event=on_event
                ):
                    yield sentence
                return
            assert self._session is not None
            await self._attach_session()
            self._emit_identity(on_event)
            events = await self._session.events()
            _drain_queue(events)
            self._inflight = _InFlight(
                idempotency_key=str(uuid.uuid4()),
                text=text,
                session_key=self.session_key,
            )
            try:
                events = await self._send_inflight()
                streamer = SentenceStreamer()
                gate = EventGate(on_event)
                started: set[str] = set()
                clock = DeadlineClock(
                    timeout,
                    idle_error=lambda: OpenClawGatewayError("OpenClaw gateway response timed out."),
                    hard_error=lambda: OpenClawGatewayError(
                        "OpenClaw gateway response exceeded maximum time."
                    ),
                    keepalive_interval_s=self.keepalive_interval_s,
                    on_keepalive=lambda: gate.emit("keepalive"),
                )
                while True:
                    get_task = asyncio.ensure_future(events.get())
                    event = None
                    try:
                        which = await await_activity(clock, {"event": get_task})
                        if which != KEEPALIVE:
                            event = get_task.result()
                    except OpenClawGatewayError as exc:
                        if exc.retryable:
                            events = await self._reconnect_inflight()
                            clock.note_activity()
                            continue
                        raise
                    finally:
                        if not get_task.done():
                            get_task.cancel()
                    if event is None:
                        continue
                    clock.note_activity()
                    try:
                        outcome = await self._handle_event(
                            event, on_event=on_event, streamer=streamer, started=started, clock=clock
                        )
                    except OpenClawGatewayError as exc:
                        if exc.retryable:
                            events = await self._reconnect_inflight()
                            clock.note_activity()
                            continue
                        raise
                    if outcome is None:
                        continue
                    if isinstance(outcome, str) and outcome == "done":
                        break
                    if isinstance(outcome, list):
                        for sentence in outcome:
                            clock.note_audible()
                            yield sentence
                tail = streamer.flush()
                if tail:
                    yield tail
            finally:
                self._inflight = None

    async def abort(self) -> None:
        if self._fallback is not None:
            await self._fallback.abort()
            return
        if self._session is None:
            return
        await self._abort_run()

    async def warmup(self) -> None:
        await self._ensure_started()

    async def close(self) -> None:
        self._closed = True
        if self._fallback is not None:
            await self._fallback.close()
        if self._session is not None:
            await self._session.close()

    async def control(self, command: AgentControl) -> ControlResult:
        await self._ensure_started()
        self._raise_if_closed()
        if self._fallback is not None:
            return await _delegate_control(self._fallback, command)
        if self._session is None:
            return ControlRejected(reason="openclaw gateway is not connected", retryable=True)
        if isinstance(command, Steer):
            if not self._steer_supported or "sessions.steer" not in self._methods:
                self._steer_supported = False
                self.agent_capabilities = _manifest(steering=False)
                return ControlRejected(reason="sessions.steer is not advertised", retryable=False)
            result = await self._session.request(
                "sessions.steer",
                {"key": self.session_key, "message": command.instruction},
            )
            run_id = str(result.get("runId") or result.get("run_id") or "")
            if run_id and self._inflight is not None:
                self._inflight.run_id = run_id
            return ControlAccepted(request_id=run_id or None, resource=self.session_key)
        if isinstance(command, ResolvePermission):
            await self._session.request(
                "exec.approval.resolve",
                {
                    "id": command.request_id,
                    "decision": _DECISION_TO_GATEWAY.get(command.decision, "deny"),
                },
            )
            return ControlAccepted(request_id=command.request_id)
        if isinstance(command, Cancel):
            await self._abort_run(run_id=command.target_id)
            return ControlAccepted(request_id=command.target_id, resource=self.session_key)
        return ControlRejected(reason="unsupported control", retryable=False)

    def _auth_present(self) -> bool:
        return bool(self.token.strip() or self.password.strip())

    def _connect_params(self) -> dict[str, Any]:
        auth: dict[str, str] = {}
        if self.token.strip():
            auth["token"] = self.token.strip()
        if self.password.strip():
            auth["password"] = self.password.strip()
        return {
            "minProtocol": PROTOCOL,
            "maxProtocol": PROTOCOL,
            "client": {
                "id": "cli",
                "version": "0.1.0",
                "platform": sys.platform,
                "mode": "cli",
            },
            "role": "operator",
            "scopes": list(OPERATOR_SCOPES),
            "caps": [TOOL_EVENTS_CAP],
            "auth": auth,
            "locale": "en-US",
            "userAgent": "sleeptalk/0.1.0",
        }

    def _emit_identity(self, on_event: Callable[..., Any] | None) -> None:
        if self._identified or not self.session_key:
            return
        self._identified = True
        publish_event(
            on_event,
            SessionIdentified(
                upstream_session_id=self.session_key,
                resumable=self._existing_session,
            ),
        )

    def _raise_if_closed(self) -> None:
        if self._closed:
            raise OpenClawGatewayError("OpenClaw gateway client is closed.")

    async def _close_resource(self, resource: Any) -> None:
        if resource is None:
            return
        with contextlib.suppress(Exception):
            await resource.close()

    async def _ensure_started(self) -> None:
        async with self._start_lock:
            self._raise_if_closed()
            if self._session is not None or self._fallback is not None:
                return
            try:
                if not self._auth_present():
                    raise OpenClawGatewayError("OpenClaw gateway token is required")
                self._session = await self._open_session()
                await self._handshake()
            except Exception as exc:
                if self._closed:
                    raise OpenClawGatewayError("OpenClaw gateway client is closed.") from exc
                self.fallback_reason = str(exc)
                if self._session is not None:
                    await self._close_resource(self._session)
                self._session = None
                if isinstance(exc, OpenClawGatewayError) and exc.authentication_failed:
                    # A rejected credential for a selected gateway must not
                    # silently switch the caller to another local runtime.
                    raise
                logger.warning(
                    "OpenClaw gateway unavailable (%s); using `openclaw acp` instead, "
                    "without steering or the gateway's existing sessions",
                    self.fallback_reason[:200],
                )
                fallback = await self._open_fallback()
                if self._closed:
                    await self._close_resource(fallback)
                    raise OpenClawGatewayError("OpenClaw gateway client is closed.") from exc
                self._fallback = fallback
            except BaseException:
                session = self._session
                self._session = None
                await self._close_resource(session)
                raise

    async def _open_session(self) -> GatewaySession:
        self._raise_if_closed()
        if self._injected_session is not None:
            await self._injected_session.connect()
            self._raise_if_closed()
            return self._injected_session
        session = AiohttpGatewaySession(
            self.url,
            ca_file=self.ca_file,
        )
        await session.connect()
        try:
            self._raise_if_closed()
        except OpenClawGatewayError:
            await session.close()
            raise
        return session

    async def _handshake(self) -> None:
        deadline = time.monotonic() + STARTUP_SIDECAR_WAIT_S
        while True:
            self._raise_if_closed()
            if not self._auth_present():
                raise OpenClawGatewayError("OpenClaw gateway token is required")
            assert self._session is not None
            events = await self._session.events()
            challenge = await self._wait_for_challenge(events)
            try:
                hello = await self._session.request("connect", self._connect_params())
            except OpenClawGatewayError as exc:
                delay = min((exc.retry_after_ms or 200) / 1000, 2.0)
                if exc.reason != "startup-sidecars" or time.monotonic() + delay > deadline:
                    raise
                await asyncio.sleep(delay)
                await self._session.close()
                self._session = await self._open_session()
                continue
            protocol = hello.get("protocol")
            if protocol != PROTOCOL:
                raise OpenClawGatewayError(
                    f"OpenClaw gateway protocol mismatch: expected {PROTOCOL}, got {protocol!r}"
                )
            features = hello.get("features") if isinstance(hello.get("features"), dict) else {}
            methods = features.get("methods") if isinstance(features.get("methods"), list) else []
            self._methods = frozenset(str(item) for item in methods)
            self._steer_supported = "sessions.steer" in self._methods
            self.agent_capabilities = _manifest(steering=self._steer_supported)
            return

    async def _attach_session(self) -> None:
        assert self._session is not None
        if self.session_key:
            try:
                resolved = await self._session.request(
                    "sessions.resolve", {"key": self.session_key}
                )
            except OpenClawGatewayError:
                resolved = {}
            key = resolved.get("key") or resolved.get("sessionKey")
            if key:
                self.session_key = str(key)
        else:
            try:
                created = await self._session.request("sessions.create", {"label": self.name})
            except OpenClawGatewayError as exc:
                if "label already in use" not in str(exc):
                    raise
                # The profile's earlier call holds the label: every call
                # after the first failed. This call gets its own.
                label = f"{self.name} {time.strftime('%Y-%m-%d %H:%M:%S')} {uuid.uuid4().hex[:4]}"
                created = await self._session.request("sessions.create", {"label": label})
            self.session_key = str(created.get("key") or created.get("sessionKey") or "")
            if not self.session_key:
                raise OpenClawGatewayError("OpenClaw gateway did not return a session key.")
        if "sessions.subscribe" in self._methods:
            with contextlib.suppress(OpenClawGatewayError):
                await self._session.request("sessions.subscribe", {})
        if "sessions.messages.subscribe" in self._methods:
            with contextlib.suppress(OpenClawGatewayError):
                await self._session.request(
                    "sessions.messages.subscribe", {"key": self.session_key}
                )

    async def _send_inflight(self, *, retrying: bool = False) -> asyncio.Queue[dict[str, Any]]:
        self._raise_if_closed()
        assert self._session is not None and self._inflight is not None
        try:
            result = await self._session.request(
                "chat.send",
                {
                    "sessionKey": self._inflight.session_key,
                    "message": self._inflight.text,
                    "idempotencyKey": self._inflight.idempotency_key,
                },
            )
        except OpenClawGatewayError as exc:
            if exc.retryable and not retrying:
                return await self._reconnect_inflight()
            raise
        except (TimeoutError, ConnectionError, OSError) as exc:
            if not retrying:
                return await self._reconnect_inflight()
            raise OpenClawGatewayError(str(exc), retryable=True) from exc
        self._raise_if_closed()
        self._inflight.run_id = str(result.get("runId") or "")
        self._inflight.session_key = str(result.get("sessionKey") or self._inflight.session_key)
        if self._inflight.session_key:
            self.session_key = self._inflight.session_key
        events = await self._session.events()
        _enqueue_cached_send(events, result, self._inflight)
        return events

    async def _reconnect_inflight(self) -> asyncio.Queue[dict[str, Any]]:
        self._raise_if_closed()
        assert self._inflight is not None
        if self._session is not None:
            await self._session.close()
            self._raise_if_closed()
            if self._injected_session is None:
                self._session = None
        try:
            self._session = await self._open_session()
            self._raise_if_closed()
            await self._handshake()
            self._raise_if_closed()
        except BaseException:
            session = self._session
            self._session = None
            await self._close_resource(session)
            raise
        if self._inflight.session_key:
            self.session_key = self._inflight.session_key
        await self._attach_session()
        self._raise_if_closed()
        return await self._send_inflight(retrying=True)

    async def _wait_for_challenge(self, events: asyncio.Queue[dict[str, Any]]) -> dict[str, Any]:
        deadline = asyncio.get_running_loop().time() + CONNECT_TIMEOUT_S
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise OpenClawGatewayError("OpenClaw gateway connect timed out", retryable=True)
            challenge = await asyncio.wait_for(events.get(), timeout=remaining)
            kind = str(challenge.get("event") or "")
            if kind == "_disconnect":
                raise OpenClawGatewayError("disconnected", retryable=True)
            if kind == "connect.challenge":
                return challenge

    async def _abort_run(self, run_id: str | None = None) -> None:
        if self._session is None:
            return
        target = run_id or (self._inflight.run_id if self._inflight else "")
        if not target:
            return
        if self.session_key:
            await self._session.request(
                "chat.abort",
                {"sessionKey": self.session_key, "runId": target},
            )
            return
        await self._session.request("sessions.abort", {"runId": target})

    async def _handle_event(
        self,
        event: dict[str, Any],
        *,
        on_event: Callable[..., Any] | None,
        streamer: SentenceStreamer,
        started: set[str],
        clock: DeadlineClock,
    ) -> list[str] | str | None:
        kind = str(event.get("event") or "")
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        if kind == "_disconnect":
            raise OpenClawGatewayError("disconnected", retryable=True)
        if kind == "tick":
            return None
        if kind == "exec.approval.requested":
            payload = _flatten_approval(payload)
        if not self._targets_turn(payload):
            return None
        if kind in {"session.operation", "agent"} and _looks_like_thinking(payload):
            if "thinking" not in started:
                started.add("thinking")
                publish_event(on_event, ThinkingStarted())
            return None
        stream = str(payload.get("stream") or "")
        if kind == "session.tool" or (kind == "agent" and stream == "tool"):
            return _publish_tool(payload, on_event=on_event, clock=clock)
        if kind == "exec.approval.requested":
            await self._publish_approval(payload, on_event=on_event)
            return None
        if kind == "chat":
            return self._handle_chat(payload, on_event=on_event, streamer=streamer, started=started)
        return None

    def _handle_chat(
        self,
        payload: dict[str, Any],
        *,
        on_event: Callable[..., Any] | None,
        streamer: SentenceStreamer,
        started: set[str],
    ) -> list[str] | str | None:
        state = str(payload.get("state") or "")
        run_id = str(payload.get("runId") or "")
        if run_id and self._inflight is not None:
            self._inflight.run_id = run_id
        if _looks_like_thinking(payload) and "thinking" not in started:
            started.add("thinking")
            publish_event(on_event, ThinkingStarted())
        if state == "delta":
            if payload.get("replace"):
                return None
            delta = str(payload.get("deltaText") or "")
            if delta:
                publish_event(on_event, AssistantDelta(text=delta))
                return streamer.feed(delta)
            return None
        if state == "error":
            raise OpenClawGatewayError(
                str(payload.get("errorMessage") or payload.get("message") or "OpenClaw gateway failed")
            )
        if state in {"final", "aborted"}:
            return "done"
        return None

    def _targets_turn(self, payload: dict[str, Any]) -> bool:
        session = str(payload.get("sessionKey") or payload.get("key") or "")
        run = str(payload.get("runId") or "")
        if session and self.session_key and session != self.session_key:
            return False
        if run and self._inflight is not None and self._inflight.run_id and run != self._inflight.run_id:
            return False
        return True

    async def _publish_approval(
        self, payload: dict[str, Any], *, on_event: Callable[..., Any] | None
    ) -> None:
        scoped = str(payload.get("sessionKey") or payload.get("key") or payload.get("runId") or "")
        if not scoped:
            return
        if self.permission_mode != "pending" and self._inflight is None:
            return
        request_id = str(payload.get("id") or payload.get("requestId") or "approval")
        choices = payload.get("allowedDecisions")
        if not isinstance(choices, list):
            choices = ["allow-once", "allow-always", "deny"]
        publish_event(
            on_event,
            PermissionRequested(
                request_id=request_id,
                prompt=str(
                    payload.get("command")
                    or payload.get("commandText")
                    or payload.get("message")
                    or "approve exec"
                ),
                options=_decisions_from_choices(choices),
            ),
        )
        if self.permission_mode != "pending" and self._session is not None:
            decision = "deny" if self.permission_mode == "deny" else "allow-once"
            await self._session.request(
                "exec.approval.resolve",
                {"id": request_id, "decision": decision},
            )

    async def _open_fallback(self) -> Any:
        if self._fallback_factory is not None:
            fallback = self._fallback_factory()
            try:
                await fallback.warmup()
            except BaseException:
                await self._close_resource(fallback)
                raise
        else:
            from sleeptalk.agents.hermes_adapter import HermesACPClient

            try:
                fallback = HermesACPClient(
                    cwd=self.cwd,
                    name=self.name,
                    binary="openclaw",
                    binary_fallback="",
                    install_hint=(
                        f"openclaw binary not found. Install with supported Node: npm i --engine-strict -g {install_spec('openclaw')} "
                        "(then `openclaw onboard` once)"
                    ),
                    permission_mode=self.permission_mode,
                )
            except Exception as exc:
                raise OpenClawGatewayError(
                    f"OpenClaw gateway unavailable ({self.fallback_reason}); "
                    f"ACP fallback failed ({exc})"
                ) from exc
            try:
                await fallback.warmup()
            except BaseException as exc:
                await self._close_resource(fallback)
                if isinstance(exc, Exception):
                    raise OpenClawGatewayError(
                        f"OpenClaw gateway unavailable ({self.fallback_reason}); "
                        f"ACP fallback failed ({exc})"
                    ) from exc
                raise
        if self._closed:
            await self._close_resource(fallback)
            raise OpenClawGatewayError("OpenClaw gateway client is closed.")
        return fallback


def _publish_tool(
    payload: dict[str, Any],
    *,
    on_event: Callable[..., Any] | None,
    clock: DeadlineClock,
) -> None:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    if not isinstance(data, dict):
        data = payload
    name = str(
        data.get("name") or data.get("toolName") or data.get("tool_name") or payload.get("name") or ""
    ).strip()
    args = data.get("args") if isinstance(data.get("args"), dict) else {}
    mapped, detail = narrate_tool(name, args or name, TOOL_VOCAB)
    raw_phase = str(data.get("phase") or payload.get("phase") or "start").strip().lower()
    phase = _TOOL_PHASE.get(raw_phase, ToolPhase.STARTED)
    if phase is ToolPhase.STARTED:
        publish_event(
            on_event,
            ToolEvent(name=mapped or name, phase=ToolPhase.STARTED, detail=detail or None),
        )
        if detail:
            publish_event(
                on_event,
                ToolEvent(name=mapped or name, phase=ToolPhase.UPDATED, detail=detail),
            )
            clock.note_audible()
        return None
    publish_event(
        on_event,
        ToolEvent(name=mapped or name, phase=phase, detail=detail or None),
    )
    return None


def _enqueue_cached_send(
    events: asyncio.Queue[dict[str, Any]],
    result: dict[str, Any],
    inflight: _InFlight,
) -> None:
    status = str(result.get("status") or "").strip().lower()
    if status in {"", "started", "in_flight"}:
        return
    payload: dict[str, Any] = {
        "sessionKey": inflight.session_key,
        "runId": inflight.run_id,
        "seq": 0,
    }
    if status in {"ok", "done", "final"}:
        payload["state"] = "final"
    elif status == "aborted":
        payload["state"] = "aborted"
    elif status in {"error", "timeout"}:
        payload["state"] = "error"
        payload["errorMessage"] = str(
            result.get("summary")
            or result.get("errorMessage")
            or (
                "OpenClaw gateway timed out"
                if status == "timeout"
                else "OpenClaw gateway failed"
            )
        )
    else:
        return
    events.put_nowait({"event": "chat", "payload": payload})


def _flatten_approval(payload: dict[str, Any]) -> dict[str, Any]:
    request = payload.get("request")
    if not isinstance(request, dict):
        return payload
    return {**request, **payload}


def _looks_like_thinking(payload: dict[str, Any]) -> bool:
    if payload.get("thinking") or payload.get("state") == "thinking":
        return True
    if str(payload.get("stream") or "").lower() == "thinking":
        return True
    phase = str(payload.get("phase") or payload.get("kind") or payload.get("type") or "").lower()
    return phase in {"thinking", "reasoning"}


def _decisions_from_choices(choices: list[Any]) -> frozenset[PermissionDecision]:
    found = {
        _GATEWAY_TO_DECISION[str(choice)]
        for choice in choices
        if str(choice) in _GATEWAY_TO_DECISION
    }
    return frozenset(found) or frozenset(PermissionDecision)


def _drain_queue(events: asyncio.Queue[dict[str, Any]]) -> None:
    while True:
        try:
            events.get_nowait()
        except asyncio.QueueEmpty:
            return


def _error_from_frame(frame: dict[str, Any]) -> OpenClawGatewayError:
    error = frame.get("error") if isinstance(frame.get("error"), dict) else {}
    details = error.get("details") if isinstance(error.get("details"), dict) else {}
    code = str(error.get("code") or details.get("code") or "")
    reason = str(details.get("reason") or "")
    retry_after = error.get("retryAfterMs", details.get("retryAfterMs"))
    retryable = bool(details.get("retryable")) or code == "UNAVAILABLE" or reason == "startup-sidecars"
    message = str(error.get("message") or code or "OpenClaw gateway request failed")
    return OpenClawGatewayError(
        message,
        retryable=retryable,
        reason=reason,
        retry_after_ms=int(retry_after) if isinstance(retry_after, int) else None,
        authentication_failed=(
            str(details.get("code") or code).startswith("AUTH_")
            or code in {"UNAUTHORIZED", "FORBIDDEN"}
            or message.lower().startswith("unauthorized:")
        ),
    )


def _ws_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme in {"ws", "wss"}:
        return url
    scheme = "wss" if parts.scheme == "https" else "ws"
    return urlunsplit((scheme, parts.netloc, parts.path, parts.query, ""))


def _ssl_option(url: str, ca_file: str) -> ssl.SSLContext | bool:
    if urlsplit(url).scheme != "wss":
        return False
    if not ca_file:
        return True
    ctx = ssl.create_default_context()
    ctx.load_verify_locations(cafile=ca_file)
    return ctx


async def _delegate_control(agent: Any, command: AgentControl) -> ControlResult:
    control = getattr(agent, "control", None)
    if control is None:
        return ControlRejected(reason="fallback agent does not implement control", retryable=False)
    result = control(command)
    if asyncio.iscoroutine(result) or asyncio.isfuture(result):
        result = await result
    if isinstance(result, (ControlAccepted, ControlRejected)):
        return result
    return ControlRejected(reason="fallback returned an invalid control result", retryable=False)
