"""Sleeptalk Agent Bridge v1.

Language-neutral JSON frames on one authenticated stream. Semantics match
``AgentAdapter``. See ``docs/agent-bridge-protocol.md``.
"""

from __future__ import annotations

import asyncio
import enum
import hmac
import json
import secrets
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlparse

from sleeptalk.adapter import (
    CONTRACT_VERSION,
    AgentCapabilityManifest,
    AgentControl,
    AgentError,
    AgentRunState,
    AnswerClarification,
    AssistantDelta,
    Cancel,
    CapabilitySupport,
    ClarificationRequested,
    ControlAccepted,
    ControlRejected,
    DiscordVoiceTarget,
    LegacyAgentEvent,
    PermissionDecision,
    PermissionRequested,
    ResolvePermission,
    SessionIdentified,
    StartVoiceSession,
    StatusChanged,
    Steer,
    SupportLevel,
    ThinkingStarted,
    ToolEvent,
    ToolPhase,
    UsageReported,
    WebVoiceTarget,
    WorkerEvent,
    WorkerPhase,
    adapter_capabilities,
    adapter_control,
    event_from_legacy,
    publish_event,
    typed_events,
)

PROTOCOL_VERSION = CONTRACT_VERSION
SUBPROTOCOL = "sleeptalk-agent-bridge-v1"
WS_PATH = "/v1/agent-bridge"

DEFAULT_HEARTBEAT_MS = 15_000
DEFAULT_MISSED_HEARTBEATS = 2
DEFAULT_SEND_QUEUE = 32
DEFAULT_INITIAL_CREDIT = 16
DEFAULT_REPLAY_WINDOW_MS = 120_000
DEFAULT_MAX_FRAME_BYTES = 65_536

_SECRET_KEYS = frozenset(
    {
        "auth",
        "token",
        "resumeToken",
        "resume_token",
        "Authorization",
        "authorization",
    }
)

_SNAKE_TO_CAMEL = {
    "turn_id": "turnId",
    "request_id": "requestId",
    "call_id": "callId",
    "session_id": "sessionId",
    "target_id": "targetId",
    "input_tokens": "inputTokens",
    "output_tokens": "outputTokens",
    "cost_minor_units": "costMinorUnits",
    "resume_token": "resumeToken",
    "heartbeat_ms": "heartbeatMs",
    "missed_heartbeats": "missedHeartbeats",
    "send_queue": "sendQueue",
    "initial_credit": "initialCredit",
    "replay_window_ms": "replayWindowMs",
    "max_frame_bytes": "maxFrameBytes",
    "result_summary": "resultSummary",
    "parent_turn_id": "parentTurnId",
    "worker_id": "workerId",
    "tool_call_id": "toolCallId",
    "upstream_session_id": "upstreamSessionId",
    "return_url": "returnUrl",
    "guild_id": "guildId",
    "channel_id": "channelId",
    "last_id": "lastId",
    "session_identity": "sessionIdentity",
    "tool_lifecycle": "toolLifecycle",
    "structured_events": "structuredEvents",
    "targeted_cancellation": "targetedCancellation",
    "voice_sessions": "voiceSessions",
}

_CAMEL_TO_SNAKE = {camel: snake for snake, camel in _SNAKE_TO_CAMEL.items()}

CAPABILITY_KEYS = (
    "sessionIdentity",
    "toolLifecycle",
    "permissions",
    "clarification",
    "structuredEvents",
    "status",
    "usage",
    "steering",
    "targetedCancellation",
    "workers",
    "voiceSessions",
)

_SIDE_EFFECTING = frozenset(
    {
        "turn.prompt",
        "turn.abort",
        "permission.resolve",
        "clarification.answer",
        "steer",
        "cancel",
        "voice_session.start",
        "session.warmup",
        "session.close",
    }
)

_CREDIT_EXEMPT = frozenset(
    {
        "hello",
        "hello.ok",
        "hello.err",
        "heartbeat",
        "credit",
        "disconnect",
    }
)

_ID_OPTIONAL = _CREDIT_EXEMPT

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


class KeyStyle(enum.Enum):
    CAMEL = "camel"
    SNAKE = "snake"


class Phase(enum.Enum):
    NEW = "new"
    READY = "ready"
    TURN = "turn"
    CLOSING = "closing"
    DEAD = "dead"


@dataclass(frozen=True)
class SessionLimits:
    heartbeat_ms: int = DEFAULT_HEARTBEAT_MS
    missed_heartbeats: int = DEFAULT_MISSED_HEARTBEATS
    send_queue: int = DEFAULT_SEND_QUEUE
    initial_credit: int = DEFAULT_INITIAL_CREDIT
    replay_window_ms: int = DEFAULT_REPLAY_WINDOW_MS
    max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES

    def to_canonical(self) -> dict[str, int]:
        return {
            "heartbeatMs": self.heartbeat_ms,
            "missedHeartbeats": self.missed_heartbeats,
            "sendQueue": self.send_queue,
            "initialCredit": self.initial_credit,
            "replayWindowMs": self.replay_window_ms,
            "maxFrameBytes": self.max_frame_bytes,
        }

    @classmethod
    def from_frame(cls, raw: dict[str, Any] | None) -> SessionLimits:
        if not raw:
            return cls()
        canon = canonicalize(raw)
        return cls(
            heartbeat_ms=int(canon.get("heartbeatMs", DEFAULT_HEARTBEAT_MS)),
            missed_heartbeats=int(canon.get("missedHeartbeats", DEFAULT_MISSED_HEARTBEATS)),
            send_queue=int(canon.get("sendQueue", DEFAULT_SEND_QUEUE)),
            initial_credit=int(canon.get("initialCredit", DEFAULT_INITIAL_CREDIT)),
            replay_window_ms=int(canon.get("replayWindowMs", DEFAULT_REPLAY_WINDOW_MS)),
            max_frame_bytes=int(canon.get("maxFrameBytes", DEFAULT_MAX_FRAME_BYTES)),
        )


class AgentBridgeError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        fatal: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.fatal = fatal


class Transport(Protocol):
    async def send(self, data: bytes) -> None: ...
    async def recv(self) -> bytes: ...
    async def close(self) -> None: ...
    def peer_host(self) -> str: ...


def canonicalize(value: Any) -> Any:
    if isinstance(value, list):
        return [canonicalize(item) for item in value]
    if not isinstance(value, dict):
        return value
    out: dict[str, Any] = {}
    for key, item in value.items():
        mapped = _SNAKE_TO_CAMEL.get(key, key)
        out[mapped] = canonicalize(item)
    return out


def stylize(value: Any, style: KeyStyle) -> Any:
    if style is KeyStyle.CAMEL:
        return value
    if isinstance(value, list):
        return [stylize(item, style) for item in value]
    if not isinstance(value, dict):
        return value
    out: dict[str, Any] = {}
    for key, item in value.items():
        out[_CAMEL_TO_SNAKE.get(key, key)] = stylize(item, style)
    return out


def detect_style(frame: dict[str, Any]) -> KeyStyle:
    for key in frame:
        if key in _SNAKE_TO_CAMEL:
            return KeyStyle.SNAKE
    caps = frame.get("capabilities")
    if isinstance(caps, dict):
        for key in caps:
            if key in _SNAKE_TO_CAMEL:
                return KeyStyle.SNAKE
    limits = frame.get("limits")
    if isinstance(limits, dict):
        for key in limits:
            if key in _SNAKE_TO_CAMEL:
                return KeyStyle.SNAKE
    return KeyStyle.CAMEL


def redact_frame(value: Any) -> Any:
    if isinstance(value, list):
        return [redact_frame(item) for item in value]
    if not isinstance(value, dict):
        return value
    out: dict[str, Any] = {}
    for key, item in value.items():
        if key in _SECRET_KEYS:
            out[key] = "<redacted>"
        else:
            out[key] = redact_frame(item)
    return out


def parse_frame(raw: str | bytes, *, max_bytes: int = DEFAULT_MAX_FRAME_BYTES) -> dict[str, Any]:
    if isinstance(raw, str):
        data = raw.encode("utf-8")
        text = raw
    else:
        data = raw
        text = raw.decode("utf-8")
    if len(data) > max_bytes:
        raise AgentBridgeError("protocol", "frame exceeds maxFrameBytes", fatal=True)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AgentBridgeError("protocol", "malformed JSON", fatal=True) from exc
    if not isinstance(parsed, dict):
        raise AgentBridgeError("protocol", "frame must be a JSON object", fatal=True)
    if "type" not in parsed:
        raise AgentBridgeError("protocol", "frame missing type", fatal=True)
    if "v" in parsed and int(parsed["v"]) != PROTOCOL_VERSION:
        if parsed["type"] != "hello":
            raise AgentBridgeError("version_mismatch", "major version is not 1", fatal=True)
    return parsed


def encode_frame(frame: dict[str, Any], *, style: KeyStyle = KeyStyle.CAMEL) -> bytes:
    payload = stylize(frame, style)
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _is_loopback(host: str) -> bool:
    return host in _LOOPBACK_HOSTS


def assert_transport_allowed(scheme: str, host: str) -> None:
    secure = scheme in {"wss", "tls", "ssl"}
    if secure:
        return
    if scheme in {"ws", "tcp"} and _is_loopback(host):
        return
    raise AgentBridgeError(
        "protocol",
        "plaintext is valid only on loopback",
        fatal=True,
    )


def _tokens_equal(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def _capability_level(raw: Any) -> SupportLevel:
    if isinstance(raw, CapabilitySupport):
        return raw.level
    if isinstance(raw, SupportLevel):
        return raw
    if isinstance(raw, dict):
        level = canonicalize(raw).get("level", "unsupported")
        return SupportLevel(level)
    if isinstance(raw, str):
        return SupportLevel(raw)
    return SupportLevel.UNSUPPORTED


def _capability_reason(raw: Any) -> str | None:
    if isinstance(raw, CapabilitySupport):
        return raw.reason
    if isinstance(raw, dict):
        reason = canonicalize(raw).get("reason")
        return str(reason) if reason else None
    return None


def manifest_to_caps(manifest: AgentCapabilityManifest) -> dict[str, Any]:
    def pack(support: CapabilitySupport, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"level": support.level.value}
        if support.reason:
            body["reason"] = support.reason
        if extra:
            body.update(extra)
        return body

    return {
        "sessionIdentity": pack(manifest.session_identity),
        "toolLifecycle": pack(manifest.tool_lifecycle),
        "permissions": pack(manifest.permissions),
        "clarification": pack(manifest.clarification),
        "structuredEvents": pack(manifest.structured_events),
        "status": pack(manifest.status),
        "usage": pack(manifest.usage),
        "steering": pack(manifest.steering),
        "targetedCancellation": pack(manifest.targeted_cancellation),
        "workers": pack(
            manifest.workers,
            {"evidence": manifest.workers.evidence.value},
        ),
        "voiceSessions": pack(
            manifest.voice_sessions,
            {"kinds": sorted(kind.value for kind in manifest.voice_sessions.kinds)},
        ),
    }


def intersect_capabilities(
    offered: dict[str, Any],
    advertised: dict[str, Any],
) -> dict[str, Any]:
    offered_c = canonicalize(offered)
    advertised_c = canonicalize(advertised)
    unknown = [key for key in offered_c if key not in CAPABILITY_KEYS]
    for key in unknown:
        if _capability_level(offered_c[key]) is SupportLevel.SUPPORTED:
            raise AgentBridgeError(
                "unknown_capability",
                f"required capability is not in v1: {key}",
                fatal=True,
            )
    out: dict[str, Any] = {}
    for key in CAPABILITY_KEYS:
        host = offered_c.get(key, {"level": "unsupported"})
        agent = advertised_c.get(key, {"level": "unsupported"})
        host_level = _capability_level(host)
        agent_level = _capability_level(agent)
        if (
            SupportLevel.UNSUPPORTED in (host_level, agent_level)
            or host_level is SupportLevel.UNSUPPORTED
        ):
            level = SupportLevel.UNSUPPORTED
        elif host_level is SupportLevel.UNAVAILABLE or agent_level is SupportLevel.UNAVAILABLE:
            level = SupportLevel.UNAVAILABLE
        else:
            level = SupportLevel.SUPPORTED
        body: dict[str, Any] = {"level": level.value}
        if level is SupportLevel.UNAVAILABLE:
            reason = _capability_reason(agent) or _capability_reason(host) or "unavailable"
            body["reason"] = reason
        if key == "voiceSessions":
            host_kinds = set(canonicalize(host).get("kinds") or [])
            agent_kinds = set(canonicalize(agent).get("kinds") or [])
            if host_kinds and agent_kinds:
                body["kinds"] = sorted(host_kinds & agent_kinds)
            elif agent_kinds:
                body["kinds"] = sorted(agent_kinds)
        if key == "workers":
            evidence = canonicalize(agent).get("evidence")
            if evidence:
                body["evidence"] = evidence
        out[key] = body
    return out


def _supported_set(caps: dict[str, Any]) -> set[str]:
    return {
        key
        for key, raw in canonicalize(caps).items()
        if _capability_level(raw) is SupportLevel.SUPPORTED
    }


def _cap_for_type(frame_type: str) -> str | None:
    return {
        "session.identified": "sessionIdentity",
        "assistant.delta": "structuredEvents",
        "tool": "toolLifecycle",
        "permission.requested": "permissions",
        "permission.resolve": "permissions",
        "clarification.requested": "clarification",
        "clarification.answer": "clarification",
        "status.changed": "status",
        "usage.reported": "usage",
        "steer": "steering",
        "cancel": "targetedCancellation",
        "worker": "workers",
        "voice_session.start": "voiceSessions",
    }.get(frame_type)


class ReplayWindow:
    def __init__(self, window_ms: int) -> None:
        self.window_ms = window_ms
        self._entries: dict[str, tuple[float, dict[str, Any]]] = {}
        self._inflight: dict[str, asyncio.Future[dict[str, Any]]] = {}

    def _purge(self, now: float) -> None:
        expired = [key for key, (deadline, _) in self._entries.items() if deadline <= now]
        for key in expired:
            del self._entries[key]

    def lookup(self, frame_id: str) -> dict[str, Any] | str | None:
        now = time.monotonic()
        self._purge(now)
        if frame_id in self._entries:
            return self._entries[frame_id][1]
        if frame_id in self._inflight:
            return "inflight"
        return None

    def begin(self, frame_id: str) -> asyncio.Future[dict[str, Any]]:
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._inflight[frame_id] = future
        return future

    def store(self, frame_id: str, result: dict[str, Any]) -> None:
        deadline = time.monotonic() + (self.window_ms / 1000)
        self._entries[frame_id] = (deadline, result)
        future = self._inflight.pop(frame_id, None)
        if future is not None and not future.done():
            future.set_result(result)

    def expire_message(self) -> dict[str, Any]:
        return {
            "v": PROTOCOL_VERSION,
            "type": "error",
            "code": "replay_expired",
            "message": "duplicate id outside the replay window",
            "retryable": False,
            "fatal": False,
        }


@dataclass
class CreditAccount:
    balance: int
    send_queue: int
    unacked: list[str] = field(default_factory=list)
    _changed: asyncio.Event = field(default_factory=asyncio.Event)

    def grant(self, add: int) -> None:
        self.balance += max(0, int(add))
        self._changed.set()

    def ack_through(self, last_id: str | None) -> None:
        if not last_id or last_id not in self.unacked:
            return
        index = self.unacked.index(last_id)
        del self.unacked[: index + 1]
        self._changed.set()

    def can_send(self, frame_type: str, *, fatal: bool = False) -> bool:
        if frame_type in _CREDIT_EXEMPT:
            return True
        if frame_type == "turn.aborted":
            return True
        if frame_type == "error" and fatal:
            return True
        if frame_type == "error":
            return True
        return self.balance > 0 and len(self.unacked) < self.send_queue

    def consume(self, frame_id: str | None, frame_type: str) -> None:
        if frame_type in _CREDIT_EXEMPT:
            return
        if frame_type == "turn.aborted":
            return
        if self.balance > 0:
            self.balance -= 1
        if frame_id:
            self.unacked.append(frame_id)


class JsonlTransport:
    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        peer_host: str,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._peer_host = peer_host

    def peer_host(self) -> str:
        return self._peer_host

    async def send(self, data: bytes) -> None:
        self._writer.write(data + b"\n")
        await self._writer.drain()

    async def recv(self) -> bytes:
        line = await self._reader.readline()
        if not line:
            raise AgentBridgeError("protocol", "connection closed", fatal=True)
        return line.rstrip(b"\r\n")

    async def close(self) -> None:
        self._writer.close()
        try:
            await self._writer.wait_closed()
        except Exception:
            pass


class WebsocketTransport:
    def __init__(self, ws: Any, peer_host: str) -> None:
        self._ws = ws
        self._peer_host = peer_host

    def peer_host(self) -> str:
        return self._peer_host

    async def send(self, data: bytes) -> None:
        await self._ws.send_str(data.decode("utf-8"))

    async def recv(self) -> bytes:
        from aiohttp import WSMsgType

        msg = await self._ws.receive()
        if msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED):
            raise AgentBridgeError("protocol", "connection closed", fatal=True)
        if msg.type == WSMsgType.ERROR:
            raise AgentBridgeError("protocol", "websocket error", fatal=True)
        if msg.type == WSMsgType.TEXT:
            return msg.data.encode("utf-8")
        if msg.type == WSMsgType.BINARY:
            return bytes(msg.data)
        raise AgentBridgeError("protocol", "unexpected websocket message", fatal=True)

    async def close(self) -> None:
        await self._ws.close()


def event_to_frame(event: object, turn_id: int) -> dict[str, Any] | None:
    base = {"v": PROTOCOL_VERSION, "turnId": turn_id}
    if isinstance(event, ThinkingStarted):
        return {**base, "type": "thinking.started"}
    if isinstance(event, AssistantDelta):
        return {**base, "type": "assistant.delta", "text": event.text}
    if isinstance(event, SessionIdentified):
        return {
            **base,
            "type": "session.identified",
            "upstreamSessionId": event.upstream_session_id,
            "resumable": event.resumable,
        }
    if isinstance(event, ToolEvent):
        frame = {
            **base,
            "type": "tool",
            "name": event.name,
            "phase": event.phase.value,
        }
        if event.call_id:
            frame["callId"] = event.call_id
        if event.detail is not None:
            frame["detail"] = event.detail
        if event.result_summary is not None:
            frame["resultSummary"] = event.result_summary
        return frame
    if isinstance(event, PermissionRequested):
        return {
            **base,
            "type": "permission.requested",
            "requestId": event.request_id,
            "prompt": event.prompt,
            "options": sorted(item.value for item in event.options),
            "toolCallId": event.tool_call_id,
        }
    if isinstance(event, ClarificationRequested):
        return {
            **base,
            "type": "clarification.requested",
            "requestId": event.request_id,
            "prompt": event.prompt,
            "choices": list(event.choices),
        }
    if isinstance(event, StatusChanged):
        return {
            **base,
            "type": "status.changed",
            "state": event.state.value,
            "summary": event.summary,
        }
    if isinstance(event, UsageReported):
        return {
            **base,
            "type": "usage.reported",
            "inputTokens": event.input_tokens,
            "outputTokens": event.output_tokens,
            "costMinorUnits": event.cost_minor_units,
            "currency": event.currency,
        }
    if isinstance(event, WorkerEvent):
        return {
            **base,
            "type": "worker",
            "workerId": event.worker_id,
            "phase": event.phase.value,
            "label": event.label,
            "parentTurnId": event.parent_turn_id,
            "detached": event.detached,
            "summary": event.summary,
        }
    if isinstance(event, AgentError):
        return {
            **base,
            "type": "error",
            "code": event.code,
            "message": event.message,
            "retryable": event.retryable,
            "fatal": event.fatal,
        }
    if isinstance(event, LegacyAgentEvent):
        return {**base, "type": "legacy", "value": event.value}
    return None


def frame_to_event(frame: dict[str, Any]) -> object | None:
    canon = canonicalize(frame)
    typ = canon["type"]
    if typ == "thinking.started":
        return ThinkingStarted()
    if typ == "assistant.delta":
        return AssistantDelta(text=str(canon.get("text") or ""))
    if typ == "session.identified":
        return SessionIdentified(
            upstream_session_id=str(canon.get("upstreamSessionId") or ""),
            resumable=bool(canon.get("resumable")),
        )
    if typ == "tool":
        phase = ToolPhase(canon.get("phase") or "started")
        return ToolEvent(
            name=str(canon.get("name") or ""),
            call_id=canon.get("callId"),
            phase=phase,
            detail=canon.get("detail"),
            result_summary=canon.get("resultSummary"),
        )
    if typ == "permission.requested":
        options = frozenset(
            PermissionDecision(item) for item in (canon.get("options") or ())
        )
        return PermissionRequested(
            request_id=str(canon.get("requestId") or ""),
            prompt=str(canon.get("prompt") or ""),
            options=options,
            tool_call_id=canon.get("toolCallId"),
        )
    if typ == "clarification.requested":
        return ClarificationRequested(
            request_id=str(canon.get("requestId") or ""),
            prompt=str(canon.get("prompt") or ""),
            choices=tuple(canon.get("choices") or ()),
        )
    if typ == "status.changed":
        return StatusChanged(
            state=AgentRunState(canon.get("state") or "idle"),
            summary=canon.get("summary"),
        )
    if typ == "usage.reported":
        return UsageReported(
            input_tokens=canon.get("inputTokens"),
            output_tokens=canon.get("outputTokens"),
            cost_minor_units=canon.get("costMinorUnits"),
            currency=canon.get("currency"),
        )
    if typ == "worker":
        return WorkerEvent(
            worker_id=str(canon.get("workerId") or ""),
            phase=WorkerPhase(canon.get("phase") or "started"),
            label=canon.get("label"),
            parent_turn_id=canon.get("parentTurnId"),
            detached=canon.get("detached"),
            summary=canon.get("summary"),
        )
    if typ == "error":
        return AgentError(
            code=str(canon.get("code") or "agent"),
            message=str(canon.get("message") or ""),
            retryable=bool(canon.get("retryable")),
            fatal=bool(canon.get("fatal")),
        )
    if typ == "legacy":
        return LegacyAgentEvent(value=str(canon.get("value") or ""))
    return None


def _control_from_frame(frame: dict[str, Any]) -> AgentControl:
    canon = canonicalize(frame)
    typ = canon["type"]
    if typ == "permission.resolve":
        if "decision" not in canon:
            raise AgentBridgeError("protocol", "permission.resolve requires decision")
        return ResolvePermission(
            request_id=str(canon["requestId"]),
            decision=PermissionDecision(canon["decision"]),
        )
    if typ == "clarification.answer":
        return AnswerClarification(
            request_id=str(canon["requestId"]),
            answer=str(canon.get("answer") or ""),
        )
    if typ == "steer":
        return Steer(
            instruction=str(canon.get("instruction") or ""),
            target_id=canon.get("targetId"),
        )
    if typ == "cancel":
        return Cancel(target_id=str(canon["targetId"]))
    if typ == "voice_session.start":
        target_raw = canonicalize(canon.get("target") or {})
        kind = target_raw.get("kind")
        if kind == "discord_channel":
            target: WebVoiceTarget | DiscordVoiceTarget = DiscordVoiceTarget(
                guild_id=str(target_raw["guildId"]),
                channel_id=str(target_raw["channelId"]),
            )
        else:
            target = WebVoiceTarget(return_url=target_raw.get("returnUrl"))
        return StartVoiceSession(request_id=str(canon["requestId"]), target=target)
    raise AgentBridgeError("protocol", f"not a control frame: {typ}")


class _Link:
    def __init__(
        self,
        transport: Transport,
        *,
        limits: SessionLimits,
        style: KeyStyle,
        role: str,
    ) -> None:
        self.transport = transport
        self.limits = limits
        self.style = style
        self.role = role
        self.phase = Phase.NEW
        self.credit = CreditAccount(limits.initial_credit, limits.send_queue)
        self.replay = ReplayWindow(limits.replay_window_ms)
        self.last_recv_id: str | None = None
        self.last_recv_at = time.monotonic()
        self.last_send_at = time.monotonic()
        self.heartbeats_received = 0
        self.session_id = ""
        self.resume_token = ""
        self.capabilities: dict[str, Any] = {}
        self._write_lock = asyncio.Lock()
        self._closed = asyncio.Event()
        self._tasks: list[asyncio.Task[Any]] = []
        self._incoming: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._id_seq = 0

    def next_id(self, prefix: str) -> str:
        self._id_seq += 1
        return f"{prefix}{self._id_seq}-{secrets.token_hex(4)}"

    def _note_recv(self, frame: dict[str, Any]) -> None:
        self.last_recv_at = time.monotonic()
        frame_id = canonicalize(frame).get("id")
        if frame_id and frame["type"] not in _ID_OPTIONAL:
            self.last_recv_id = str(frame_id)

    async def send(self, frame: dict[str, Any], *, force: bool = False) -> bool:
        outgoing = dict(frame)
        outgoing.setdefault("v", PROTOCOL_VERSION)
        typ = outgoing["type"]
        fatal = bool(outgoing.get("fatal"))
        if not force and not self.credit.can_send(typ, fatal=fatal):
            return False
        if typ not in _ID_OPTIONAL and "id" not in outgoing:
            outgoing["id"] = self.next_id("f-")
        data = encode_frame(outgoing, style=self.style)
        if len(data) > self.limits.max_frame_bytes:
            raise AgentBridgeError("protocol", "frame exceeds maxFrameBytes", fatal=True)
        async with self._write_lock:
            await self.transport.send(data)
        self.last_send_at = time.monotonic()
        if typ not in _CREDIT_EXEMPT:
            self.credit.consume(outgoing.get("id"), typ)
        return True

    async def send_or_backpressure(self, frame: dict[str, Any]) -> None:
        # With no credit the sender waits for a grant or ack; only a peer that
        # stays silent for a heartbeat period gets a backpressure error.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(self.limits.heartbeat_ms / 1000, 0.05)
        while True:
            self.credit._changed.clear()
            if await self.send(frame):
                return
            remaining = deadline - loop.time()
            if remaining <= 0 or self._closed.is_set():
                break
            try:
                await asyncio.wait_for(self.credit._changed.wait(), remaining)
            except TimeoutError:
                break
        await self.send(
            {
                "v": PROTOCOL_VERSION,
                "type": "error",
                "code": "backpressure",
                "message": "credit or queue exhausted",
                "retryable": True,
                "fatal": False,
            },
            force=True,
        )

    async def recv_raw(self) -> dict[str, Any]:
        raw = await self.transport.recv()
        frame = parse_frame(raw, max_bytes=self.limits.max_frame_bytes)
        self._note_recv(frame)
        if frame["type"] == "heartbeat":
            self.heartbeats_received += 1
            last_id = canonicalize(frame).get("lastId")
            if last_id:
                self.credit.ack_through(str(last_id))
        elif frame["type"] == "credit":
            self.credit.grant(int(canonicalize(frame).get("add") or 0))
        return frame

    async def close(self) -> None:
        self.phase = Phase.DEAD
        self._closed.set()
        for task in self._tasks:
            task.cancel()
        await self.transport.close()

    async def heartbeat_loop(self) -> None:
        period = max(self.limits.heartbeat_ms / 1000, 0.02)
        miss_after = period * self.limits.missed_heartbeats
        try:
            while not self._closed.is_set():
                await asyncio.sleep(min(period, 0.05) if period < 0.1 else period / 2)
                now = time.monotonic()
                if now - self.last_recv_at > miss_after:
                    self.phase = Phase.DEAD
                    self._closed.set()
                    return
                if now - self.last_send_at >= period:
                    await self.send(
                        {
                            "v": PROTOCOL_VERSION,
                            "type": "heartbeat",
                            "lastId": self.last_recv_id,
                        }
                    )
        except asyncio.CancelledError:
            return
        except AgentBridgeError:
            self.phase = Phase.DEAD
            self._closed.set()


@dataclass
class _ResumeRecord:
    token: str
    agent: object
    limits: SessionLimits
    capabilities: dict[str, Any]
    replay: ReplayWindow
    abort_ids: set[str]


class AgentBridgeServer:
    """Remote-agent side. Host connects in and speaks host-to-agent frames."""

    def __init__(
        self,
        *,
        token: str,
        agent: object,
        limits: SessionLimits | None = None,
        advertised: AgentCapabilityManifest | None = None,
    ) -> None:
        if not token:
            raise ValueError("bridge token must be non-empty")
        self._token = token
        self._agent = agent
        self._limits = limits or SessionLimits()
        self._advertised = advertised or adapter_capabilities(agent)
        self._sessions: dict[str, _ResumeRecord] = {}
        self._tcp_server: asyncio.AbstractServer | None = None
        self._ws_runner: Any = None
        self._closed = asyncio.Event()
        self.abort_calls = 0
        self.links: list[_Link] = []

    @property
    def limits(self) -> SessionLimits:
        return self._limits

    async def start_tcp(self, host: str = "127.0.0.1", port: int = 0) -> tuple[str, int]:
        assert_transport_allowed("tcp", host)
        self._tcp_server = await asyncio.start_server(self._on_tcp, host, port)
        sock = self._tcp_server.sockets[0]
        bound = sock.getsockname()
        return bound[0], int(bound[1])

    async def start_ws(self, host: str = "127.0.0.1", port: int = 0) -> tuple[str, int]:
        from aiohttp import web

        assert_transport_allowed("ws", host)
        app = web.Application()
        app.router.add_get(WS_PATH, self._on_ws)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, host, port)
        await site.start()
        self._ws_runner = runner
        address = runner.addresses[0]
        return str(address[0]), int(address[1])

    async def aclose(self) -> None:
        self._closed.set()
        if self._tcp_server is not None:
            self._tcp_server.close()
            await self._tcp_server.wait_closed()
            self._tcp_server = None
        if self._ws_runner is not None:
            await self._ws_runner.cleanup()
            self._ws_runner = None
        for link in list(self.links):
            await link.close()

    async def wait_closed(self) -> None:
        await self._closed.wait()

    async def _on_tcp(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        peer = writer.get_extra_info("peername")
        host = peer[0] if peer else "127.0.0.1"
        transport = JsonlTransport(reader, writer, host)
        await self._serve(transport)

    async def _on_ws(self, request: Any) -> Any:
        from aiohttp import web

        if request.query_string:
            return web.Response(status=400, text="query strings carry no credentials")
        header_token = _bearer_from_headers(request.headers)
        ws = web.WebSocketResponse(protocols=[SUBPROTOCOL])
        await ws.prepare(request)
        peer = request.remote or "127.0.0.1"
        transport = WebsocketTransport(ws, peer)
        await self._serve(transport, header_token=header_token)
        return ws

    async def _serve(self, transport: Transport, *, header_token: str | None = None) -> None:
        link = _Link(transport, limits=self._limits, style=KeyStyle.CAMEL, role="agent")
        self.links.append(link)
        try:
            first = await link.recv_raw()
            if first.get("type") != "hello":
                await link.send(
                    {
                        "v": PROTOCOL_VERSION,
                        "type": "hello.err",
                        "code": "protocol",
                        "message": "first frame must be hello",
                    },
                    force=True,
                )
                return
            try:
                session = self._handshake(first, header_token=header_token)
            except AgentBridgeError as exc:
                await link.send(
                    {
                        "v": PROTOCOL_VERSION,
                        "type": "hello.err",
                        "code": exc.code,
                        "message": exc.message,
                    },
                    force=True,
                )
                return
            link.style = detect_style(first)
            link.session_id = session.session_id
            link.resume_token = session.resume_token
            link.capabilities = session.capabilities
            link.replay = session.replay
            link.credit = CreditAccount(self._limits.initial_credit, self._limits.send_queue)
            link.phase = Phase.READY
            await link.send(
                {
                    "v": PROTOCOL_VERSION,
                    "type": "hello.ok",
                    "sessionId": session.session_id,
                    "resumeToken": session.resume_token,
                    "capabilities": session.capabilities,
                    "limits": self._limits.to_canonical(),
                },
                force=True,
            )
            link._tasks.append(asyncio.create_task(link.heartbeat_loop()))
            await self._agent_loop(link, session)
        except AgentBridgeError:
            pass
        finally:
            await link.close()
            if link in self.links:
                self.links.remove(link)

    def _handshake(
        self,
        hello: dict[str, Any],
        *,
        header_token: str | None,
    ) -> _BoundSession:
        canon = canonicalize(hello)
        version = int(canon.get("v", -1))
        if version != PROTOCOL_VERSION:
            raise AgentBridgeError("version_mismatch", "major version is not 1", fatal=True)
        presented = _token_from_hello(canon, header_token)
        if presented is None or not _tokens_equal(presented, self._token):
            raise AgentBridgeError("auth", "expired", fatal=True)
        resume_id = canon.get("sessionId")
        resume_tok = canon.get("resumeToken")
        if resume_id or resume_tok:
            record = self._sessions.get(str(resume_id or ""))
            if record is None or resume_tok is None or not _tokens_equal(str(resume_tok), record.token):
                raise AgentBridgeError("auth", "expired", fatal=True)
            rotated = secrets.token_urlsafe(32)
            record.token = rotated
            return _BoundSession(
                session_id=str(resume_id),
                resume_token=rotated,
                capabilities=record.capabilities,
                replay=record.replay,
                abort_ids=record.abort_ids,
                agent=record.agent,
                resumed=True,
            )
        offered = canon.get("capabilities") or {}
        advertised = manifest_to_caps(self._advertised)
        try:
            capabilities = intersect_capabilities(offered, advertised)
        except AgentBridgeError:
            raise
        session_id = "sess-" + secrets.token_urlsafe(12)
        resume_token = secrets.token_urlsafe(32)
        record = _ResumeRecord(
            token=resume_token,
            agent=self._agent,
            limits=self._limits,
            capabilities=capabilities,
            replay=ReplayWindow(self._limits.replay_window_ms),
            abort_ids=set(),
        )
        self._sessions[session_id] = record
        return _BoundSession(
            session_id=session_id,
            resume_token=resume_token,
            capabilities=capabilities,
            replay=record.replay,
            abort_ids=record.abort_ids,
            agent=self._agent,
            resumed=False,
        )

    async def _agent_loop(self, link: _Link, session: _BoundSession) -> None:
        inflight: asyncio.Task[None] | None = None
        inflight_turn: int | None = None
        abort_event = asyncio.Event()
        supported = _supported_set(session.capabilities)

        async def cancel_inflight() -> None:
            nonlocal inflight, inflight_turn
            abort_event.set()
            if inflight is not None and not inflight.done():
                inflight.cancel()
                try:
                    await inflight
                except (asyncio.CancelledError, AgentBridgeError):
                    pass
            inflight = None
            inflight_turn = None
            abort_event.clear()

        while not link._closed.is_set() and link.phase is not Phase.DEAD:
            try:
                frame = await asyncio.wait_for(link.recv_raw(), timeout=0.5)
            except TimeoutError:
                continue
            except AgentBridgeError:
                if inflight_turn is not None:
                    await self._safe_abort(session)
                return
            typ = frame.get("type")
            if typ in {"heartbeat", "credit"}:
                continue
            if typ == "disconnect":
                await cancel_inflight()
                link.phase = Phase.CLOSING
                return
            if typ in _SIDE_EFFECTING:
                result = await self._replay_or_run(
                    link,
                    session,
                    frame,
                    inflight=inflight,
                    inflight_turn=inflight_turn,
                    abort_event=abort_event,
                    supported=supported,
                )
                if typ == "turn.prompt" and result is not None:
                    inflight = result[0]
                    inflight_turn = result[1]
                elif typ == "turn.abort":
                    await cancel_inflight()
                elif typ == "session.close":
                    return
                continue
            await link.send_or_backpressure(
                {
                    "v": PROTOCOL_VERSION,
                    "type": "error",
                    "code": "protocol",
                    "message": f"out-of-order type {typ}",
                    "retryable": False,
                    "fatal": False,
                }
            )

    async def _replay_or_run(
        self,
        link: _Link,
        session: _BoundSession,
        frame: dict[str, Any],
        *,
        inflight: asyncio.Task[None] | None,
        inflight_turn: int | None,
        abort_event: asyncio.Event,
        supported: set[str],
    ) -> tuple[asyncio.Task[None], int] | None:
        canon = canonicalize(frame)
        typ = canon["type"]
        frame_id = canon.get("id")
        if not frame_id:
            await link.send_or_backpressure(
                {
                    "v": PROTOCOL_VERSION,
                    "type": "error",
                    "code": "protocol",
                    "message": "application frame missing id",
                    "retryable": False,
                    "fatal": False,
                }
            )
            return None
        needed = _cap_for_type(typ)
        if needed and needed not in supported:
            await link.send_or_backpressure(
                {
                    "v": PROTOCOL_VERSION,
                    "type": "error",
                    "code": "protocol",
                    "message": f"{typ} requires {needed}",
                    "retryable": False,
                    "fatal": False,
                }
            )
            return None
        cached = session.replay.lookup(str(frame_id))
        if isinstance(cached, dict):
            await link.send(dict(cached), force=True)
            return None
        if cached == "inflight":
            original = await session.replay._inflight[str(frame_id)]
            await link.send(dict(original), force=True)
            return None
        if typ == "turn.prompt":
            if inflight is not None and not inflight.done():
                await link.send_or_backpressure(
                    {
                        "v": PROTOCOL_VERSION,
                        "type": "error",
                        "code": "protocol",
                        "message": "one turn is already in flight",
                        "retryable": False,
                        "fatal": False,
                    }
                )
                return None
            turn_id = int(canon.get("turnId") or 0)
            session.replay.begin(str(frame_id))
            task = asyncio.create_task(
                self._run_prompt(link, session, canon, abort_event)
            )
            return task, turn_id
        if typ == "turn.abort":
            await self._run_abort(link, session, canon)
            return None
        if typ == "session.warmup":
            await session.agent.warmup()
            result = {
                "v": PROTOCOL_VERSION,
                "type": "control.accepted",
                "id": link.next_id("e-"),
                "ref": frame_id,
            }
            session.replay.store(str(frame_id), result)
            await link.send_or_backpressure(result)
            return None
        if typ == "session.close":
            await session.agent.close()
            result = {
                "v": PROTOCOL_VERSION,
                "type": "control.accepted",
                "id": link.next_id("e-"),
                "ref": frame_id,
            }
            session.replay.store(str(frame_id), result)
            await link.send_or_backpressure(result)
            link.phase = Phase.CLOSING
            return None
        await self._run_control(link, session, canon)
        return None

    async def _run_prompt(
        self,
        link: _Link,
        session: _BoundSession,
        canon: dict[str, Any],
        abort_event: asyncio.Event,
    ) -> None:
        prompt_id = str(canon["id"])
        turn_id = int(canon.get("turnId") or 0)
        mode = canon.get("mode") or "stream"
        text = str(canon.get("text") or "")
        link.phase = Phase.TURN

        pending: list[dict[str, Any]] = []

        def on_event(event: object) -> None:
            if isinstance(event, str):
                typed = event_from_legacy(event)
            else:
                typed = event
            frame = event_to_frame(typed, turn_id)
            if frame is None:
                return
            needed = _cap_for_type(frame["type"])
            if needed and needed not in _supported_set(session.capabilities):
                return
            frame["id"] = link.next_id("e-")
            pending.append(frame)
            # Deliver events without waiting for the next sentence: an agent
            # that asks permission and then waits must not deadlock.
            nonlocal flusher
            if flusher is None or flusher.done():
                flusher = asyncio.get_running_loop().create_task(flush_events())

        flush_lock = asyncio.Lock()
        flusher: asyncio.Task[None] | None = None

        async def flush_events() -> None:
            async with flush_lock:
                while pending:
                    await link.send_or_backpressure(pending.pop(0))

        completed: dict[str, Any] | None = None
        try:
            if abort_event.is_set():
                raise asyncio.CancelledError
            if mode == "full":
                reply = await session.agent.respond(text)
                await flush_events()
                completed = {
                    "v": PROTOCOL_VERSION,
                    "type": "turn.completed",
                    "id": link.next_id("e-"),
                    "turnId": turn_id,
                    "ref": prompt_id,
                    "text": reply,
                }
            else:
                async for sentence in session.agent.respond_streaming(
                    text,
                    on_event=typed_events(on_event),
                ):
                    if abort_event.is_set():
                        raise asyncio.CancelledError
                    await flush_events()
                    await link.send_or_backpressure(
                        {
                            "v": PROTOCOL_VERSION,
                            "type": "sentence",
                            "id": link.next_id("e-"),
                            "turnId": turn_id,
                            "text": sentence,
                        }
                    )
                await flush_events()
                completed = {
                    "v": PROTOCOL_VERSION,
                    "type": "turn.completed",
                    "id": link.next_id("e-"),
                    "turnId": turn_id,
                    "ref": prompt_id,
                }
            if completed is not None:
                session.replay.store(prompt_id, completed)
                await link.send_or_backpressure(completed)
        except asyncio.CancelledError:
            aborted = {
                "v": PROTOCOL_VERSION,
                "type": "turn.aborted",
                "id": link.next_id("e-"),
                "turnId": turn_id,
                "ref": prompt_id,
            }
            session.replay.store(prompt_id, aborted)
            return
        except Exception as exc:
            error = {
                "v": PROTOCOL_VERSION,
                "type": "error",
                "id": link.next_id("e-"),
                "turnId": turn_id,
                "code": "agent",
                "message": str(exc),
                "retryable": True,
                "fatal": False,
            }
            session.replay.store(prompt_id, error)
            await link.send_or_backpressure(error)
        finally:
            link.phase = Phase.READY

    async def _run_abort(
        self,
        link: _Link,
        session: _BoundSession,
        canon: dict[str, Any],
    ) -> None:
        frame_id = str(canon["id"])
        turn_id = int(canon.get("turnId") or 0)
        if frame_id not in session.abort_ids:
            session.abort_ids.add(frame_id)
            await self._safe_abort(session)
        result = {
            "v": PROTOCOL_VERSION,
            "type": "turn.aborted",
            "id": link.next_id("e-"),
            "turnId": turn_id,
            "ref": frame_id,
        }
        session.replay.store(frame_id, result)
        await link.send(result, force=True)

    async def _safe_abort(self, session: _BoundSession) -> None:
        self.abort_calls += 1
        abort = getattr(session.agent, "abort", None)
        if abort is not None:
            await abort()

    async def _run_control(
        self,
        link: _Link,
        session: _BoundSession,
        canon: dict[str, Any],
    ) -> None:
        frame_id = str(canon["id"])
        try:
            command = _control_from_frame(canon)
        except AgentBridgeError as exc:
            error = {
                "v": PROTOCOL_VERSION,
                "type": "error",
                "id": link.next_id("e-"),
                "code": exc.code,
                "message": exc.message,
                "retryable": False,
                "fatal": False,
            }
            session.replay.store(frame_id, error)
            await link.send_or_backpressure(error)
            return
        result = await adapter_control(session.agent, command)
        if isinstance(result, ControlAccepted):
            frame = {
                "v": PROTOCOL_VERSION,
                "type": "control.accepted",
                "id": link.next_id("e-"),
                "ref": frame_id,
                "requestId": result.request_id or canon.get("requestId"),
                "resource": result.resource,
            }
        else:
            rejected = result if isinstance(result, ControlRejected) else ControlRejected(
                reason="agent does not implement control"
            )
            frame = {
                "v": PROTOCOL_VERSION,
                "type": "control.rejected",
                "id": link.next_id("e-"),
                "ref": frame_id,
                "reason": rejected.reason,
                "retryable": rejected.retryable,
            }
        session.replay.store(frame_id, frame)
        await link.send_or_backpressure(frame)


@dataclass
class _BoundSession:
    session_id: str
    resume_token: str
    capabilities: dict[str, Any]
    replay: ReplayWindow
    abort_ids: set[str]
    agent: object
    resumed: bool


def _token_from_hello(canon: dict[str, Any], header_token: str | None) -> str | None:
    auth = canon.get("auth")
    body_token = None
    if isinstance(auth, dict):
        body_token = auth.get("token")
    if header_token and body_token and not _tokens_equal(header_token, str(body_token)):
        raise AgentBridgeError("auth", "expired", fatal=True)
    if header_token:
        return header_token
    if body_token:
        return str(body_token)
    return None


def _bearer_from_headers(headers: Any) -> str | None:
    value = headers.get("Authorization") or headers.get("authorization")
    if not value:
        return None
    if not value.lower().startswith("bearer "):
        return None
    return value.split(" ", 1)[1]


class AgentBridgeClient:
    """Host side. Connects to a remote agent and implements ``AgentAdapter``."""

    def __init__(
        self,
        *,
        key_style: KeyStyle = KeyStyle.SNAKE,
        auto_credit: bool = True,
    ) -> None:
        self.key_style = key_style
        self.auto_credit = auto_credit
        self.link: _Link | None = None
        self.session_id = ""
        self.resume_token = ""
        self.limits = SessionLimits()
        self.capabilities: dict[str, Any] = {}
        self._turn_seq = 0
        self._reader_task: asyncio.Task[None] | None = None
        self._http: Any = None
        # Control replies are routed here by ref, so an active sentence stream
        # never consumes them and a waiting control never consumes sentences.
        self._control_waiters: dict[str, asyncio.Future[dict[str, Any]]] = {}

    async def connect(
        self,
        url: str,
        token: str,
        *,
        capabilities: dict[str, Any] | None = None,
        session_id: str | None = None,
        resume_token: str | None = None,
        client: dict[str, str] | None = None,
        header_auth: bool = False,
    ) -> None:
        parsed = urlparse(url)
        scheme = parsed.scheme or "tcp"
        host = parsed.hostname or "127.0.0.1"
        assert_transport_allowed(scheme, host)
        if parsed.query:
            raise AgentBridgeError("auth", "expired", fatal=True)
        transport = await self._open_transport(parsed, token if header_auth else None)
        link = _Link(transport, limits=SessionLimits(), style=self.key_style, role="host")
        hello: dict[str, Any] = {
            "v": PROTOCOL_VERSION,
            "type": "hello",
            "capabilities": capabilities if capabilities is not None else _host_offer(),
            "client": client or {"name": "sleeptalk", "language": "python"},
        }
        if not header_auth:
            hello["auth"] = {"scheme": "bearer", "token": token}
        if session_id:
            hello["sessionId"] = session_id
        if resume_token:
            hello["resumeToken"] = resume_token
        await link.send(hello, force=True)
        reply = await link.recv_raw()
        if reply.get("type") == "hello.err":
            canon = canonicalize(reply)
            raise AgentBridgeError(
                str(canon.get("code") or "auth"),
                str(canon.get("message") or "hello failed"),
                fatal=True,
            )
        if reply.get("type") != "hello.ok":
            raise AgentBridgeError("protocol", "expected hello.ok", fatal=True)
        ok = canonicalize(reply)
        self.session_id = str(ok["sessionId"])
        self.resume_token = str(ok["resumeToken"])
        self.limits = SessionLimits.from_frame(ok.get("limits"))
        self.capabilities = ok.get("capabilities") or {}
        link.limits = self.limits
        link.credit = CreditAccount(self.limits.initial_credit, self.limits.send_queue)
        link.session_id = self.session_id
        link.resume_token = self.resume_token
        link.capabilities = self.capabilities
        link.phase = Phase.READY
        self.link = link
        link._tasks.append(asyncio.create_task(link.heartbeat_loop()))
        self._reader_task = asyncio.create_task(self._read_loop())
        link._tasks.append(self._reader_task)

    async def _open_transport(self, parsed: Any, header_token: str | None) -> Transport:
        scheme = parsed.scheme or "tcp"
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port
        if scheme in {"tcp", "tls", "ssl"}:
            if port is None:
                raise AgentBridgeError("protocol", "tcp url requires a port", fatal=True)
            ssl_ctx = None
            if scheme in {"tls", "ssl"}:
                import ssl

                ssl_ctx = ssl.create_default_context()
            reader, writer = await asyncio.open_connection(host, port, ssl=ssl_ctx)
            return JsonlTransport(reader, writer, host)
        if scheme in {"ws", "wss"}:
            import aiohttp

            url = parsed.geturl()
            if not parsed.path:
                url = f"{scheme}://{host}:{port}{WS_PATH}"
            headers = {}
            if header_token:
                headers["Authorization"] = f"Bearer {header_token}"
            self._http = aiohttp.ClientSession()
            ws = await self._http.ws_connect(
                url,
                protocols=[SUBPROTOCOL],
                headers=headers,
            )
            return WebsocketTransport(ws, host)
        raise AgentBridgeError("protocol", f"unsupported url scheme {scheme}", fatal=True)

    async def _read_loop(self) -> None:
        assert self.link is not None
        unacked = 0
        try:
            while not self.link._closed.is_set():
                frame = await self.link.recv_raw()
                if frame.get("type") in {"heartbeat", "credit"}:
                    continue
                if self.auto_credit and frame.get("type") not in _CREDIT_EXEMPT:
                    await self.grant_credit(1)
                    # The agent may hold only sendQueue unacked frames, and a
                    # heartbeat's lastId is the only ack. Idle heartbeats never
                    # fire while credit keeps flowing, so ack before it fills.
                    unacked += 1
                    if unacked >= max(1, self.limits.send_queue // 2):
                        unacked = 0
                        await self.link.send(
                            {"v": PROTOCOL_VERSION, "type": "heartbeat", "lastId": self.link.last_recv_id},
                            force=True,
                        )
                if frame.get("type") in {"control.accepted", "control.rejected"}:
                    waiter = self._control_waiters.pop(str(canonicalize(frame).get("ref") or ""), None)
                    if waiter is not None:
                        if not waiter.done():
                            waiter.set_result(canonicalize(frame))
                        continue
                await self.link._incoming.put(frame)
                if frame.get("type") == "disconnect":
                    return
        except AgentBridgeError:
            self.link.phase = Phase.DEAD
            self.link._closed.set()
        except asyncio.CancelledError:
            return

    async def grant_credit(self, add: int) -> None:
        if self.link is None:
            raise AgentBridgeError("protocol", "not connected", fatal=True)
        await self.link.send({"v": PROTOCOL_VERSION, "type": "credit", "add": add}, force=True)

    async def disconnect(self, reason: str = "done") -> None:
        if self.link is None:
            return
        try:
            await self.link.send(
                {"v": PROTOCOL_VERSION, "type": "disconnect", "reason": reason},
                force=True,
            )
        except AgentBridgeError:
            pass
        await self._drop_link()

    def respond_streaming(
        self,
        text: str,
        *,
        on_event: Callable[..., Any] | None = None,
    ) -> AsyncIterator[str]:
        async def _stream() -> AsyncIterator[str]:
            if self.link is None:
                raise AgentBridgeError("protocol", "not connected", fatal=True)
            self._turn_seq += 1
            turn_id = self._turn_seq
            prompt_id = self.link.next_id("p-")
            await self.link.send_or_backpressure(
                {
                    "v": PROTOCOL_VERSION,
                    "type": "turn.prompt",
                    "id": prompt_id,
                    "turnId": turn_id,
                    "text": text,
                    "mode": "stream",
                }
            )
            while True:
                frame = await self._next_app_frame()
                canon = canonicalize(frame)
                if canon.get("turnId") not in (None, turn_id) and canon["type"] not in {
                    "error",
                    "turn.aborted",
                    "turn.completed",
                }:
                    continue
                typ = canon["type"]
                if typ == "sentence":
                    if canon.get("turnId") not in (None, turn_id):
                        continue
                    yield str(canon.get("text") or "")
                    continue
                if typ == "turn.completed":
                    if canon.get("turnId") in (None, turn_id):
                        return
                    continue
                if typ == "turn.aborted":
                    if canon.get("turnId") in (None, turn_id):
                        return
                    continue
                if typ == "error":
                    if canon.get("code") == "abort":
                        return
                    if canon.get("fatal"):
                        raise AgentBridgeError(
                            str(canon.get("code") or "agent"),
                            str(canon.get("message") or ""),
                            retryable=bool(canon.get("retryable")),
                            fatal=True,
                        )
                    event = frame_to_event(canon)
                    if event is not None:
                        publish_event(on_event, event)  # type: ignore[arg-type]
                    if canon.get("code") == "backpressure":
                        raise AgentBridgeError(
                            "backpressure",
                            str(canon.get("message") or "credit or queue exhausted"),
                            retryable=True,
                        )
                    continue
                event = frame_to_event(canon)
                if event is not None:
                    publish_event(on_event, event)  # type: ignore[arg-type]

        return _stream()

    async def respond(self, text: str, timeout: float = 300.0) -> str:
        if self.link is None:
            raise AgentBridgeError("protocol", "not connected", fatal=True)
        self._turn_seq += 1
        turn_id = self._turn_seq
        prompt_id = self.link.next_id("p-")
        await self.link.send_or_backpressure(
            {
                "v": PROTOCOL_VERSION,
                "type": "turn.prompt",
                "id": prompt_id,
                "turnId": turn_id,
                "text": text,
                "mode": "full",
            }
        )
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AgentBridgeError("agent", "respond timed out", retryable=True)
            frame = await asyncio.wait_for(self._next_app_frame(), timeout=remaining)
            canon = canonicalize(frame)
            if canon["type"] == "turn.completed" and canon.get("turnId") == turn_id:
                return str(canon.get("text") or "")
            if canon["type"] == "error" and canon.get("fatal"):
                raise AgentBridgeError(
                    str(canon.get("code") or "agent"),
                    str(canon.get("message") or ""),
                    fatal=True,
                )

    async def abort(self) -> None:
        if self.link is None:
            return
        turn_id = self._turn_seq
        abort_id = self.link.next_id("a-")
        await self.link.send(
            {
                "v": PROTOCOL_VERSION,
                "type": "turn.abort",
                "id": abort_id,
                "turnId": turn_id,
            },
            force=True,
        )

    async def warmup(self) -> None:
        if self.link is None:
            raise AgentBridgeError("protocol", "not connected", fatal=True)
        frame_id = self.link.next_id("w-")
        await self.link.send_or_backpressure(
            {"v": PROTOCOL_VERSION, "type": "session.warmup", "id": frame_id}
        )
        await self._wait_ref(frame_id)

    async def close(self) -> None:
        if self.link is None:
            return
        try:
            if self.link.phase not in {Phase.DEAD, Phase.CLOSING}:
                frame_id = self.link.next_id("c-")
                await self.link.send(
                    {"v": PROTOCOL_VERSION, "type": "session.close", "id": frame_id},
                    force=True,
                )
        except AgentBridgeError:
            pass
        await self._drop_link()

    async def _drop_link(self) -> None:
        for waiter in self._control_waiters.values():
            if not waiter.done():
                waiter.set_exception(AgentBridgeError("protocol", "connection closed", retryable=True))
        self._control_waiters.clear()
        if self.link is None:
            return
        await self.link.close()
        if self._http is not None:
            await self._http.close()
            self._http = None
        self.link = None

    async def send_control(self, frame: dict[str, Any], *, timeout: float = 5.0) -> dict[str, Any]:
        """Send a control frame and wait for its control.accepted or control.rejected."""

        if self.link is None:
            raise AgentBridgeError("protocol", "not connected", fatal=True)
        frame = dict(frame)
        frame_id = str(frame.get("id") or self.link.next_id("k-"))
        frame["id"] = frame_id
        waiter: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._control_waiters[frame_id] = waiter
        try:
            await self.link.send_or_backpressure(frame)
            return await asyncio.wait_for(waiter, timeout)
        finally:
            self._control_waiters.pop(frame_id, None)

    async def send_frame(self, frame: dict[str, Any]) -> None:
        if self.link is None:
            raise AgentBridgeError("protocol", "not connected", fatal=True)
        await self.link.send_or_backpressure(frame)

    async def next_frame(self, *, timeout: float = 2.0) -> dict[str, Any]:
        return await asyncio.wait_for(self._next_app_frame(), timeout=timeout)

    async def _next_app_frame(self) -> dict[str, Any]:
        if self.link is None:
            raise AgentBridgeError("protocol", "not connected", fatal=True)
        return await self.link._incoming.get()

    async def _wait_ref(self, frame_id: str, *, timeout: float = 2.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AgentBridgeError("protocol", "missing reply", retryable=True)
            frame = await asyncio.wait_for(self._next_app_frame(), timeout=remaining)
            if canonicalize(frame).get("ref") == frame_id:
                return frame

    @property
    def heartbeats_received(self) -> int:
        return 0 if self.link is None else self.link.heartbeats_received


def _host_offer() -> dict[str, Any]:
    supported = {"level": "supported"}
    return {key: dict(supported) for key in CAPABILITY_KEYS}


__all__ = [
    "PROTOCOL_VERSION",
    "SUBPROTOCOL",
    "WS_PATH",
    "AgentBridgeClient",
    "AgentBridgeError",
    "AgentBridgeServer",
    "KeyStyle",
    "SessionLimits",
    "assert_transport_allowed",
    "canonicalize",
    "encode_frame",
    "parse_frame",
    "redact_frame",
]
