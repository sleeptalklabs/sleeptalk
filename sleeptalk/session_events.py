"""Normalized session events for the RealtimeSession contract (ADR 0003).

Typed, serializable event envelope and variants covering every route
(cascade, native, hybrid). Events correlate by ``session_id`` and, where
applicable, ``turn_id`` and ``attempt_id``. Unknown fields from future
versions are preserved on round-trip so consumers and producers can interoperate
across upgrades (``extra`` is never dropped).

Events never carry credentials, raw keys, or full transcripts beyond what the
route already emits; audio itself travels on streams, not events.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Mapping

from sleeptalk.adapter import (
    AdapterEvent,
    AgentControl,
    AnswerClarification,
    AssistantDelta,
    Cancel,
    CapabilitySupport,
    ClarificationRequested,
    PermissionRequested,
    ResolvePermission,
    StartVoiceSession,
    Steer,
    ThinkingStarted,
    ToolEvent,
    ToolPhase,
    WebVoiceTarget,
    WorkerEvent,
)
from sleeptalk.subagent_roster import RosterChange


class EventError(Exception):
    """Raised when a serialized event is structurally invalid."""


@enum.unique
class SessionEventType(enum.Enum):
    """Normalized event types (ADR 0003 §3)."""

    SESSION_STATE = "session.state"
    TRANSCRIPT_PARTIAL = "transcript.partial"
    TRANSCRIPT_FINAL = "transcript.final"
    RESPONSE_STARTED = "response.started"
    RESPONSE_FIRST_OUTPUT = "response.first_output"
    RESPONSE_COMPLETED = "response.completed"
    RESPONSE_CANCELLED = "response.cancelled"
    AGENT_EVENT = "agent.event"
    TOOL_STARTED = "tool.started"
    TOOL_COMPLETED = "tool.completed"
    TOOL_FAILED = "tool.failed"
    PROGRESS = "progress"
    INTERRUPTION = "interruption"
    ERROR = "error"
    WARNING = "warning"
    USAGE_ATTEMPT_STARTED = "usage.attempt.started"
    USAGE_ATTEMPT_FINISHED = "usage.attempt.finished"
    USAGE_SUMMARY = "usage.summary"
    ROSTER_CHANGED = "roster.changed"


#: Event types that carry turn correlation (ADR 0003 §1: turn-scoped work).
TURN_SCOPED_TYPES = frozenset({
    SessionEventType.TRANSCRIPT_PARTIAL,
    SessionEventType.TRANSCRIPT_FINAL,
    SessionEventType.RESPONSE_STARTED,
    SessionEventType.RESPONSE_FIRST_OUTPUT,
    SessionEventType.RESPONSE_COMPLETED,
    SessionEventType.RESPONSE_CANCELLED,
    SessionEventType.AGENT_EVENT,
    SessionEventType.TOOL_STARTED,
    SessionEventType.TOOL_COMPLETED,
    SessionEventType.TOOL_FAILED,
    SessionEventType.PROGRESS,
    SessionEventType.INTERRUPTION,
    SessionEventType.USAGE_ATTEMPT_STARTED,
    SessionEventType.USAGE_ATTEMPT_FINISHED,
})

#: Event types that carry attempt correlation (provider work).
ATTEMPT_SCOPED_TYPES = frozenset({
    SessionEventType.USAGE_ATTEMPT_STARTED,
    SessionEventType.USAGE_ATTEMPT_FINISHED,
})


@enum.unique
class SessionState(enum.Enum):
    """Lifecycle states (ADR 0003 §2)."""

    CREATED = "created"
    CONNECTING = "connecting"
    ACTIVE = "active"
    RECOVERING = "recovering"
    CLOSING = "closing"
    CLOSED = "closed"


@enum.unique
class PaymentMode(enum.Enum):
    """Who pays providers for the session (ADR 0004)."""

    HOSTED = "hosted"
    BYOK = "byok"


@enum.unique
class Speaker(enum.Enum):
    USER = "user"
    ASSISTANT = "assistant"


@enum.unique
class InterruptionSource(enum.Enum):
    USER_SPEECH = "user_speech"
    CANCEL = "cancel"


@enum.unique
class InterruptionPhase(enum.Enum):
    WORKING = "working"
    ANSWERING = "answering"


@enum.unique
class AttemptStatus(enum.Enum):
    OK = "ok"
    ERROR = "error"
    CANCELLED = "cancelled"
    SESSION_CLOSED = "session_closed"
    RETRY = "retry"

_PAYLOAD_KEYS = frozenset({
    "type", "session_id", "sessionId", "turn_id", "turnId",
    "attempt_id", "attemptId", "timestamp", "payload", "extra",
})


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class SessionEvent:
    """One normalized event from a RealtimeSession.

    ``payload`` holds type-specific normalized fields plus, when the event
    originates from a route-native source, the original fields under
    ``payload["raw"]``. ``extra`` preserves unknown future fields verbatim so
    older consumers never drop data newer producers add.
    """

    type: SessionEventType
    session_id: str
    payload: dict[str, Any] = field(default_factory=dict)
    turn_id: int | None = None
    attempt_id: str | None = None
    timestamp: str = field(default_factory=_now_iso)
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.session_id:
            raise EventError("session_id is required")
        if self.turn_id is not None and self.turn_id < 0:
            raise EventError("turn_id must be a non-negative integer")
        if self.type in TURN_SCOPED_TYPES and self.turn_id is None:
            raise EventError(f"{self.type.value} requires turn_id")
        if self.type in ATTEMPT_SCOPED_TYPES and not self.attempt_id:
            raise EventError(f"{self.type.value} requires attempt_id")

    # -- serialization ----------------------------------------------------

    def to_json(self) -> dict[str, Any]:
        event: dict[str, Any] = {
            "type": self.type.value,
            "sessionId": self.session_id,
            "timestamp": self.timestamp,
            "payload": self.payload,
        }
        if self.turn_id is not None:
            event["turnId"] = self.turn_id
        if self.attempt_id is not None:
            event["attemptId"] = self.attempt_id
        if self.extra:
            event["extra"] = self.extra
        return event

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> SessionEvent:
        if not isinstance(data, Mapping):
            raise EventError("event must be a JSON object")
        try:
            event_type = SessionEventType(data["type"])
        except (KeyError, ValueError) as exc:
            raise EventError(f"unknown or missing event type: {exc}") from exc
        session_id = data.get("sessionId")
        if not session_id:
            raise EventError("sessionId is required")
        turn_id = data.get("turnId")
        if turn_id is not None and not isinstance(turn_id, int) or isinstance(turn_id, bool):
            raise EventError("turnId must be an integer when present")
        payload = data.get("payload") or {}
        if not isinstance(payload, dict):
            raise EventError("payload must be a JSON object")
        extra = data.get("extra") or {}
        if not isinstance(extra, dict):
            raise EventError("extra must be a JSON object")
        unknown = {k: v for k, v in data.items() if k not in _PAYLOAD_KEYS}
        # Future top-level fields are folded into extra rather than dropped.
        merged_extra = {**unknown, **extra}
        return cls(
            type=event_type,
            session_id=session_id,
            payload=payload,
            turn_id=turn_id,
            attempt_id=data.get("attemptId"),
            timestamp=data.get("timestamp") or _now_iso(),
            extra=merged_extra,
        )


# -- constructors for the common shapes ---------------------------------


def session_state(session_id: str, state: SessionState, **fields: Any) -> SessionEvent:
    return SessionEvent(
        type=SessionEventType.SESSION_STATE,
        session_id=session_id,
        payload={"state": state.value, **fields},
    )


def transcript(
    session_id: str,
    turn_id: int,
    *,
    speaker: Speaker,
    text: str,
    final: bool,
    **fields: Any,
) -> SessionEvent:
    return SessionEvent(
        type=(
            SessionEventType.TRANSCRIPT_FINAL if final else SessionEventType.TRANSCRIPT_PARTIAL
        ),
        session_id=session_id,
        turn_id=turn_id,
        payload={"speaker": speaker.value, "text": text, **fields},
    )


def response(
    session_id: str,
    turn_id: int,
    kind: SessionEventType,
    *,
    output_kind: str | None = None,
    **fields: Any,
) -> SessionEvent:
    if kind not in {
        SessionEventType.RESPONSE_STARTED,
        SessionEventType.RESPONSE_FIRST_OUTPUT,
        SessionEventType.RESPONSE_COMPLETED,
        SessionEventType.RESPONSE_CANCELLED,
    }:
        raise EventError(f"{kind.value} is not a response event type")
    payload: dict[str, Any] = dict(fields)
    if output_kind is not None:
        payload["kind"] = output_kind
    return SessionEvent(type=kind, session_id=session_id, turn_id=turn_id, payload=payload)


def interruption(
    session_id: str,
    turn_id: int,
    *,
    source: InterruptionSource,
    phase: InterruptionPhase,
    played: Any = None,
    generated: Any = None,
    **fields: Any,
) -> SessionEvent:
    """Interruption with partial-output accounting (ADR 0003 §4 clause 3).

    ``played``/``generated`` report what crossed the playout boundary versus
    what the route produced; routes differ in granularity but must report
    both measures when a partial output exists.
    """
    payload: dict[str, Any] = {"source": source.value, "phase": phase.value, **fields}
    if played is not None:
        payload["played"] = played
    if generated is not None:
        payload["generated"] = generated
    return SessionEvent(
        type=SessionEventType.INTERRUPTION,
        session_id=session_id,
        turn_id=turn_id,
        payload=payload,
    )


def agent_event(
    session_id: str, turn_id: int, name: str, *, raw: Any = None, **fields: Any
) -> SessionEvent:
    """Normalize an ``AgentAdapter.on_event`` name (thinking/text/tool/...)."""
    payload: dict[str, Any] = {"name": name, **fields}
    if raw is not None:
        payload["raw"] = raw
    return SessionEvent(
        type=SessionEventType.AGENT_EVENT,
        session_id=session_id,
        turn_id=turn_id,
        payload=payload,
    )


_TOOL_PHASE_TYPES = {
    ToolPhase.STARTED: SessionEventType.TOOL_STARTED,
    ToolPhase.UPDATED: SessionEventType.TOOL_STARTED,
    ToolPhase.COMPLETED: SessionEventType.TOOL_COMPLETED,
    ToolPhase.FAILED: SessionEventType.TOOL_FAILED,
}


def _adapter_v1_event(
    session_id: str, turn_id: int, kind: str, **fields: Any
) -> SessionEvent:
    payload = {"schema": "adapter.v1", "kind": kind, **fields}
    return SessionEvent(
        type=SessionEventType.AGENT_EVENT,
        session_id=session_id,
        turn_id=turn_id,
        payload=payload,
    )


def adapter_session_event(
    session_id: str, turn_id: int, event: AdapterEvent
) -> SessionEvent:
    if isinstance(event, PermissionRequested):
        return _adapter_v1_event(
            session_id,
            turn_id,
            event.kind,
            request_id=event.request_id,
            prompt=event.prompt,
            options=sorted(option.value for option in event.options),
        )
    if isinstance(event, ClarificationRequested):
        fields: dict[str, Any] = {
            "request_id": event.request_id,
            "prompt": event.prompt,
        }
        if event.choices:
            fields["choices"] = list(event.choices)
        return _adapter_v1_event(session_id, turn_id, event.kind, **fields)
    if isinstance(event, ToolEvent):
        tool_fields: dict[str, Any] = {}
        if event.detail is not None:
            tool_fields["detail"] = event.detail
        if event.result_summary is not None:
            tool_fields["result_summary"] = event.result_summary
        path = getattr(event, "path", None)
        if path is not None:
            tool_fields["path"] = path
        tool_input = getattr(event, "input", None)
        if tool_input is not None:
            tool_fields["input"] = tool_input
        return tool_event(
            session_id,
            turn_id,
            _TOOL_PHASE_TYPES[event.phase],
            name=event.display_name or event.name,
            call_id=event.call_id,
            **tool_fields,
        )
    if isinstance(event, WorkerEvent):
        raise EventError(
            "WorkerEvent is session-scoped; apply it on SubagentRoster "
            "and emit roster_session_event"
        )
    if isinstance(event, ThinkingStarted):
        return agent_event(session_id, turn_id, "thinking")
    if isinstance(event, AssistantDelta):
        return agent_event(
            session_id,
            turn_id,
            "text",
            text=event.text,
        )
    return _adapter_v1_event(session_id, turn_id, event.kind)


def _support_payload(support: CapabilitySupport) -> dict[str, Any]:
    payload: dict[str, Any] = {"level": support.level.value}
    if support.reason is not None:
        payload["reason"] = support.reason
    return payload


def roster_session_event(change: RosterChange) -> SessionEvent:
    return SessionEvent(
        type=SessionEventType.ROSTER_CHANGED,
        session_id=change.session_id,
        payload={
            "workerId": change.worker_id,
            "phase": change.phase.value,
            "previousPhase": (
                None if change.previous_phase is None else change.previous_phase.value
            ),
            "worker": {
                "workerId": change.record.worker_id,
                "label": change.record.label,
                "parentTurnId": change.record.parent_turn_id,
                "detached": change.record.detached,
                "evidence": change.record.evidence.value,
                "phase": change.record.phase.value,
                "summary": change.record.summary,
                "startedAt": change.record.started_at,
                "updatedAt": change.record.updated_at,
                "controls": {
                    "status": _support_payload(change.record.controls.status),
                    "steering": _support_payload(change.record.controls.steering),
                    "followUp": _support_payload(change.record.controls.follow_up),
                    "cancellation": _support_payload(change.record.controls.cancellation),
                },
            },
        },
    )


def control_session_event(
    session_id: str, turn_id: int, command: AgentControl
) -> SessionEvent:
    if isinstance(command, ResolvePermission):
        return _adapter_v1_event(
            session_id,
            turn_id,
            command.kind,
            request_id=command.request_id,
            decision=command.decision.value,
        )
    if isinstance(command, AnswerClarification):
        return _adapter_v1_event(
            session_id,
            turn_id,
            command.kind,
            request_id=command.request_id,
            answer=command.answer,
        )
    if isinstance(command, Steer):
        fields = {"instruction": command.instruction}
        if command.target_id is not None:
            fields["target_id"] = command.target_id
        return _adapter_v1_event(session_id, turn_id, command.kind, **fields)
    if isinstance(command, StartVoiceSession):
        target = command.target
        if isinstance(target, WebVoiceTarget):
            target_payload: dict[str, Any] = {"kind": target.kind}
            if target.return_url is not None:
                target_payload["return_url"] = target.return_url
        else:
            target_payload = {
                "kind": target.kind,
                "guild_id": target.guild_id,
                "channel_id": target.channel_id,
            }
        return _adapter_v1_event(
            session_id,
            turn_id,
            command.kind,
            request_id=command.request_id,
            target=target_payload,
        )
    if isinstance(command, Cancel):
        return _adapter_v1_event(
            session_id, turn_id, command.kind, target_id=command.target_id
        )
    return _adapter_v1_event(session_id, turn_id, command.kind)


def tool_event(
    session_id: str,
    turn_id: int,
    kind: SessionEventType,
    *,
    name: str,
    call_id: str | None = None,
    **fields: Any,
) -> SessionEvent:
    if kind not in {
        SessionEventType.TOOL_STARTED,
        SessionEventType.TOOL_COMPLETED,
        SessionEventType.TOOL_FAILED,
    }:
        raise EventError(f"{kind.value} is not a tool event type")
    payload: dict[str, Any] = {"name": name, **fields}
    if call_id is not None:
        payload["callId"] = call_id
    return SessionEvent(type=kind, session_id=session_id, turn_id=turn_id, payload=payload)


def progress(session_id: str, turn_id: int, *, pool: str, **fields: Any) -> SessionEvent:
    """Tiered preamble / narration / knock progress."""
    return SessionEvent(
        type=SessionEventType.PROGRESS,
        session_id=session_id,
        turn_id=turn_id,
        payload={"pool": pool, **fields},
    )


def error(
    session_id: str, message: str, *, fatal: bool = False, **fields: Any
) -> SessionEvent:
    """Error event; ``fatal=True`` means the session leaves ACTIVE (ADR 0003 §2)."""
    return SessionEvent(
        type=SessionEventType.ERROR,
        session_id=session_id,
        payload={"message": message, "fatal": fatal, **fields},
    )


def warning(session_id: str, message: str, **fields: Any) -> SessionEvent:
    return SessionEvent(
        type=SessionEventType.WARNING,
        session_id=session_id,
        payload={"message": message, **fields},
    )


def usage_attempt_started(
    session_id: str,
    turn_id: int,
    attempt_id: str,
    *,
    provider: str,
    operation: str,
    payment_mode: PaymentMode,
    units: Mapping[str, Any] | None = None,
    **fields: Any,
) -> SessionEvent:
    """Usage attempt with identity of record and raw provider units preserved."""
    return SessionEvent(
        type=SessionEventType.USAGE_ATTEMPT_STARTED,
        session_id=session_id,
        turn_id=turn_id,
        attempt_id=attempt_id,
        payload={
            "provider": provider,
            "operation": operation,
            "paymentMode": payment_mode.value,
            "units": dict(units or {}),
            **fields,
        },
    )


def usage_attempt_finished(
    session_id: str,
    turn_id: int,
    attempt_id: str,
    *,
    provider: str,
    operation: str,
    payment_mode: PaymentMode,
    status: AttemptStatus,
    units: Mapping[str, Any] | None = None,
    **fields: Any,
) -> SessionEvent:
    return SessionEvent(
        type=SessionEventType.USAGE_ATTEMPT_FINISHED,
        session_id=session_id,
        turn_id=turn_id,
        attempt_id=attempt_id,
        payload={
            "provider": provider,
            "operation": operation,
            "paymentMode": payment_mode.value,
            "status": status.value,
            "units": dict(units or {}),
            **fields,
        },
    )


def usage_summary(
    session_id: str,
    *,
    duration_ms: float | None = None,
    totals: Mapping[str, Any] | None = None,
    attempts: Mapping[str, Any] | None = None,
    **fields: Any,
) -> SessionEvent:
    payload: dict[str, Any] = dict(fields)
    if duration_ms is not None:
        payload["durationMs"] = duration_ms
    if totals is not None:
        payload["totals"] = dict(totals)
    if attempts is not None:
        payload["attempts"] = dict(attempts)
    return SessionEvent(
        type=SessionEventType.USAGE_SUMMARY,
        session_id=session_id,
        payload=payload,
    )
