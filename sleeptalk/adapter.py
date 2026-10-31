"""The seam every agent plugs into.

The voice runtime (STT, TTS, preambles, barge-in, endpointing) talks to any
agent through this protocol and nothing else. An agent that can stream
sentences and be interrupted is talkable; everything else about the agent —
its tools, its harness, its provider, whether it runs locally or on a VPS —
is its own business.

Current implementations:
- `PiRPCClient` (pi coding agent over --mode rpc)

Event names an adapter may surface through `on_event` during
respond_streaming (all optional; consumers must tolerate any subset):
- "thinking"      — the agent started reasoning (first thinking delta)
- "text"          — the agent started producing visible text
- "tool"          — the agent started any tool call
- "tool:<name>"   — a specific tool call started (e.g. tool:bash); may repeat
- "workbench:<json>" — optional raw text/tool data for rich terminal clients;
  voice-only consumers ignore it
"""

from __future__ import annotations

import enum
import inspect
import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol, TypeAlias

#: Fired through on_event as agent work progresses.
AgentEventCallback = Callable[[str], None]

WORKBENCH_EVENT_PREFIX = "workbench:"
CONTRACT_VERSION = 1

_TOOL_PREFIX = "tool:"
_TOOLDETAIL_PREFIX = "tooldetail:"


def workbench_event(kind: str, **payload: Any) -> str:
    """Encode optional rich terminal output through the string event seam.

    Voice-only consumers already ignore unknown events. Terminal consumers can
    decode these records to display raw text, code, and tool activity without
    changing or duplicating the speakable sentence stream.
    """

    return WORKBENCH_EVENT_PREFIX + json.dumps(
        {"kind": kind, **payload},
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )


class SupportLevel(enum.Enum):
    UNSUPPORTED = "unsupported"
    SUPPORTED = "supported"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class CapabilitySupport:
    level: SupportLevel
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.level is SupportLevel.UNAVAILABLE and not self.reason:
            raise ValueError("unavailable capability requires a reason")
        if self.level is SupportLevel.SUPPORTED and self.reason is not None:
            raise ValueError("supported capability forbids a reason")


class WorkerEvidence(enum.Enum):
    NONE = "none"
    NAMED_TOOL = "named_tool"
    CORRELATED_CHILD = "correlated_child"
    LIFECYCLE_NATIVE = "lifecycle_native"


@dataclass(frozen=True)
class WorkerCapability(CapabilitySupport):
    evidence: WorkerEvidence = WorkerEvidence.NONE


class VoiceSessionKind(enum.Enum):
    WEB_LINK = "web_link"
    DISCORD_CHANNEL = "discord_channel"


@dataclass(frozen=True)
class VoiceSessionCapability(CapabilitySupport):
    kinds: frozenset[VoiceSessionKind] = frozenset()


@dataclass(frozen=True)
class AgentCapabilityManifest:
    contract_version: int
    structured_events: CapabilitySupport
    tool_lifecycle: CapabilitySupport
    permissions: CapabilitySupport
    clarification: CapabilitySupport
    session_identity: CapabilitySupport
    status: CapabilitySupport
    usage: CapabilitySupport
    steering: CapabilitySupport
    targeted_cancellation: CapabilitySupport
    workers: WorkerCapability
    voice_sessions: VoiceSessionCapability

    @classmethod
    def legacy_floor(cls) -> AgentCapabilityManifest:
        unsupported = CapabilitySupport(SupportLevel.UNSUPPORTED)
        return cls(
            contract_version=CONTRACT_VERSION,
            structured_events=unsupported,
            tool_lifecycle=unsupported,
            permissions=unsupported,
            clarification=unsupported,
            session_identity=unsupported,
            status=unsupported,
            usage=unsupported,
            steering=unsupported,
            targeted_cancellation=unsupported,
            workers=WorkerCapability(SupportLevel.UNSUPPORTED),
            voice_sessions=VoiceSessionCapability(SupportLevel.UNSUPPORTED),
        )


@dataclass(frozen=True)
class ThinkingStarted:
    kind: Literal["thinking.started"] = "thinking.started"


@dataclass(frozen=True)
class ReasoningDelta:
    """Text of the model's reasoning as it streams. Display only: never
    spoken, and speech policy ignores it (ThinkingStarted marks thinking)."""
    text: str = ""
    kind: Literal["reasoning.delta"] = "reasoning.delta"


@dataclass(frozen=True)
class AssistantDelta:
    text: str = ""
    kind: Literal["assistant.delta"] = "assistant.delta"


class ToolPhase(enum.Enum):
    STARTED = "started"
    UPDATED = "updated"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True)
class ToolEvent:
    name: str
    call_id: str | None = None
    phase: ToolPhase = ToolPhase.STARTED
    detail: str | None = None
    result_summary: str | None = None
    # For display only, never spoken: the harness's own tool name ("grep",
    # "write_file") when ``name`` is a narration kind, and a short label of
    # what it is doing ("Searching the web for three.js", "ls -la").
    display_name: str | None = None
    label: str | None = None
    kind: Literal["tool"] = "tool"


class PermissionDecision(enum.Enum):
    DENY = "deny"
    ALLOW_ONCE = "allow_once"
    ALLOW_SESSION = "allow_session"


@dataclass(frozen=True)
class PermissionRequested:
    request_id: str
    prompt: str
    options: frozenset[PermissionDecision]
    tool_call_id: str | None = None
    kind: Literal["permission.requested"] = "permission.requested"


@dataclass(frozen=True)
class ClarificationRequested:
    request_id: str
    prompt: str
    choices: tuple[str, ...] = ()
    kind: Literal["clarification.requested"] = "clarification.requested"


@dataclass(frozen=True)
class SessionIdentified:
    upstream_session_id: str
    resumable: bool
    kind: Literal["session.identified"] = "session.identified"


class AgentRunState(enum.Enum):
    IDLE = "idle"
    WORKING = "working"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass(frozen=True)
class StatusChanged:
    state: AgentRunState
    summary: str | None = None
    kind: Literal["status.changed"] = "status.changed"


@dataclass(frozen=True)
class UsageReported:
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_minor_units: int | None = None
    currency: str | None = None
    kind: Literal["usage.reported"] = "usage.reported"


@dataclass(frozen=True)
class AgentError:
    code: str
    message: str
    retryable: bool
    fatal: bool = False
    kind: Literal["error"] = "error"


class WorkerPhase(enum.Enum):
    STARTED = "started"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class WorkerEvent:
    worker_id: str
    phase: WorkerPhase
    label: str | None = None
    parent_turn_id: str | None = None
    detached: bool | None = None
    summary: str | None = None
    kind: Literal["worker"] = "worker"


@dataclass(frozen=True)
class LegacyAgentEvent:
    value: str
    kind: Literal["legacy"] = "legacy"


AdapterEvent: TypeAlias = (
    ThinkingStarted
    | ReasoningDelta
    | AssistantDelta
    | ToolEvent
    | PermissionRequested
    | ClarificationRequested
    | SessionIdentified
    | StatusChanged
    | UsageReported
    | AgentError
    | WorkerEvent
    | LegacyAgentEvent
)


@dataclass(frozen=True)
class ResolvePermission:
    request_id: str
    decision: PermissionDecision
    kind: Literal["permission.resolve"] = "permission.resolve"


@dataclass(frozen=True)
class AnswerClarification:
    request_id: str
    answer: str
    kind: Literal["clarification.answer"] = "clarification.answer"


@dataclass(frozen=True)
class Steer:
    instruction: str
    target_id: str | None = None
    kind: Literal["steer"] = "steer"


@dataclass(frozen=True)
class Cancel:
    target_id: str
    kind: Literal["cancel"] = "cancel"


@dataclass(frozen=True)
class WebVoiceTarget:
    return_url: str | None = None
    kind: Literal["web_link"] = "web_link"


@dataclass(frozen=True)
class DiscordVoiceTarget:
    guild_id: str
    channel_id: str
    kind: Literal["discord_channel"] = "discord_channel"


VoiceTarget: TypeAlias = WebVoiceTarget | DiscordVoiceTarget


@dataclass(frozen=True)
class StartVoiceSession:
    request_id: str
    target: VoiceTarget
    kind: Literal["voice_session.start"] = "voice_session.start"


AgentControl: TypeAlias = (
    ResolvePermission
    | AnswerClarification
    | Steer
    | Cancel
    | StartVoiceSession
)


@dataclass(frozen=True)
class ControlAccepted:
    request_id: str | None = None
    resource: str | None = None


@dataclass(frozen=True)
class ControlRejected:
    reason: str
    retryable: bool = False


ControlResult: TypeAlias = ControlAccepted | ControlRejected


class _TypedEventSink:
    _skip_legacy_fire = True

    def __init__(self, callback: Callable[[AdapterEvent], None]) -> None:
        self._callback = callback

    def __call__(self, event: AdapterEvent | str) -> None:
        if isinstance(event, str):
            self._callback(event_from_legacy(event))
        else:
            self._callback(event)


def event_from_legacy(value: str) -> AdapterEvent:
    if value == "thinking":
        return ThinkingStarted()
    if value == "text":
        return AssistantDelta(text="")
    if value == "tool":
        return ToolEvent(name="")
    if value.startswith(_TOOL_PREFIX):
        return ToolEvent(name=value[len(_TOOL_PREFIX) :])
    if value.startswith(_TOOLDETAIL_PREFIX):
        return ToolEvent(
            name="",
            detail=value[len(_TOOLDETAIL_PREFIX) :],
            phase=ToolPhase.UPDATED,
        )
    return LegacyAgentEvent(value=value)


def event_to_legacy(event: AdapterEvent) -> str | None:
    if isinstance(event, ThinkingStarted):
        return "thinking"
    if isinstance(event, AssistantDelta):
        return "text"
    if isinstance(event, ToolEvent):
        if event.phase is ToolPhase.UPDATED or (event.detail is not None and not event.name):
            return f"{_TOOLDETAIL_PREFIX}{event.detail or ''}"
        if event.phase is ToolPhase.STARTED:
            if event.name:
                return f"{_TOOL_PREFIX}{event.name}"
            return "tool"
        return None
    if isinstance(event, LegacyAgentEvent):
        return event.value
    return None


def agent_signal(event: AdapterEvent) -> tuple[str, dict[str, Any]] | None:
    """Normalize adapter activity for speech policy without string round-trips."""
    if isinstance(event, ThinkingStarted):
        return "thinking", {}
    if isinstance(event, AssistantDelta):
        return "text", {"text": event.text} if event.text else {}
    if isinstance(event, ToolEvent):
        if event.phase is ToolPhase.UPDATED or (event.detail is not None and not event.name):
            return "tooldetail", {"detail": event.detail or ""}
        if event.phase is ToolPhase.STARTED:
            return "tool", {"tool": event.name} if event.name else {}
        return None
    if not isinstance(event, LegacyAgentEvent):
        return None
    value = event.value
    if value.startswith(WORKBENCH_EVENT_PREFIX):
        try:
            fields = json.loads(value[len(WORKBENCH_EVENT_PREFIX):])
        except (TypeError, ValueError):
            return "workbench", {"value": value}
        if isinstance(fields, dict):
            return str(fields.get("kind") or "workbench"), fields
    return value or "activity", {}


def publish_event(sink: Callable[..., Any] | None, event: AdapterEvent) -> None:
    if sink is None:
        return
    if isinstance(sink, _TypedEventSink):
        sink(event)
        return
    legacy = event_to_legacy(event)
    if legacy is not None:
        sink(legacy)


def typed_events(callback: Callable[[AdapterEvent], None]) -> _TypedEventSink:
    return _TypedEventSink(callback)


def adapter_capabilities(agent: object) -> AgentCapabilityManifest:
    manifest = getattr(agent, "agent_capabilities", None)
    if isinstance(manifest, AgentCapabilityManifest):
        return manifest
    return AgentCapabilityManifest.legacy_floor()


async def adapter_control(agent: object, command: AgentControl) -> ControlResult:
    control = getattr(agent, "control", None)
    if inspect.iscoroutinefunction(control):
        return await control(command)
    return ControlRejected(reason="agent does not implement control", retryable=False)


class AgentAdapter(Protocol):
    """What the voice runtime needs from an agent: streaming text and a brake."""

    def respond_streaming(
        self,
        text: str,
        *,
        on_event: AgentEventCallback | None = None,
    ) -> AsyncIterator[str]:
        """Stream a response to `text`, yielding speakable sentences as they
        complete. Consumers synthesize and play each sentence as it arrives,
        so yield early and often."""
        ...

    async def respond(self, text: str, timeout: float = 300.0) -> str:
        """Full-text response for non-voice paths (slash commands, briefings)."""
        ...

    async def abort(self) -> None:
        """Stop current work immediately (barge-in). Must be safe to call when idle."""
        ...

    async def warmup(self) -> None:
        """Pre-warm process, model, and connections. Called once at bot boot."""
        ...

    async def close(self) -> None:
        """Shut the agent down. Called at bot shutdown."""
        ...
