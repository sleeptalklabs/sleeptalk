"""The Route protocol: one lifecycle contract for every route type (ADR 0003).

A *route* owns audio-to-audio assembly for one session family:

- ``cascade``  — STT → agent → TTS, sentence-at-a-time (registries + adapters).
- ``native``   — a single speech-to-speech model (e.g. OpenAI Realtime).
- ``hybrid``   — a native route delegating turns to an agent worker.

The protocol is surface- and provider-agnostic on purpose: no Discord or other
platform SDK may be imported from here, and no ``if provider ==`` conditionals
belong in route code (ADR 0003 §5). Providers, agents, and transports plug in
through the existing seams (``provider_factory``, ``AgentAdapter``,
``TTSAdapter``).

This module defines the contract only. The live implementation is
``cascade.py``. The shared contract suite lives in
``sleeptalk/tests/route_contract.py``.

Handoff contract for implementers (ADR 0003 §6):

- ``prepare(session)``  — bind to a session context; acquire route resources.
- ``run_turn(input, ctx)`` — assemble one turn from committed user input.
- ``interrupt_presentation(turn_id)`` — revoke audible output without
  implicitly aborting detached specialist work.
- ``cancel_task(task_id)`` — explicitly abort one specialist task.
- ``cancel_turn(turn_id)`` — legacy cascade cancellation during migration.
- ``close()`` — idempotent teardown; nothing may emit audio after it returns.

Cancelling with no active turn is always a no-op, never an error (ADR 0003
§4 clause 5). Attempting an operation a route does not support must raise
``RouteCapabilityError`` — capability failures are loud, at the earliest
possible boundary, never silent runtime surprises (ADR 0003 §5).
"""

from __future__ import annotations

import enum
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from sleeptalk.routes.audio_output import (
    AudioOutput,
    AudioOutputAccounting,
    InFlightTurn,
)
from sleeptalk.session_events import SessionEvent

__all__ = [
    "AudioOutput",
    "AudioOutputAccounting",
    "InFlightTurn",
    "InterruptionGranularity",
    "OutputEventStream",
    "RecoveryOutcome",
    "RecoveryResult",
    "Route",
    "RouteCapabilities",
    "RouteCapabilityError",
    "RouteError",
    "RouteInput",
    "RouteRuntimeError",
    "RouteStateError",
    "RouteType",
    "TurnContext",
    "TurnOutcome",
]


class RouteType(enum.Enum):
    """The assembly strategy (ADR 0003 §5)."""

    CASCADE = "cascade"
    NATIVE = "native"
    HYBRID = "hybrid"


class InterruptionGranularity(enum.Enum):
    """How finely a route can account partially-delivered output."""

    SENTENCE = "sentence"
    PLAYOUT_FRACTION = "playout_fraction"


@dataclass(frozen=True)
class RouteCapabilities:
    """What a route can do — declared, not discovered at runtime.

    Mirrors the "declared material differences" table (ADR 0003 §5). Ask for
    an unsupported capability and the route must fail loudly
    (:class:`RouteCapabilityError`), not degrade silently.
    """

    route_type: RouteType
    #: Emits output incrementally (sentence/audio chunks as produced).
    streaming_output: bool = True
    #: Provider owns conversation timing and consumes audio during playback.
    continuous_input: bool = False
    #: Can cancel in-flight upstream work on barge-in.
    interruption: bool = True
    #: Accounting granularity for partially-delivered output.
    interruption_granularity: InterruptionGranularity = InterruptionGranularity.SENTENCE
    #: Can offer a resume after interrupting an answering turn.
    resume_after_interruption: bool = False
    #: Operations this route meters (``stt.stream``/``tts.stream``/...).
    usage_operations: tuple[str, ...] = ()


@dataclass(frozen=True)
class RouteInput:
    """One committed user input for a turn.

    Exactly one payload field is non-empty. Routes read the field their
    turn-detection source produces; they must not inspect provider names.
    """

    pcm: bytes = b""
    text: str = ""

    def __post_init__(self) -> None:
        if bool(self.pcm) == bool(self.text):
            raise ValueError("RouteInput needs exactly one of pcm or text")


@dataclass
class TurnContext:
    """Identity and event sink for one turn (ADR 0003 §1)."""

    session_id: str
    turn_id: int
    emit: Any  # Callable[[SessionEvent], None]; typed loosely to avoid cycles
    #: Route-specific extras (e.g. resume offers, latency trace handles).
    extras: dict[str, Any] = field(default_factory=dict)
    #: Kernel-owned entrypoint for deferred policy work that must become a
    #: complete, monotonic turn rather than emitting late output on this one.
    schedule_followup: Callable[[str, dict[str, Any]], Awaitable[Any]] | None = None


@dataclass(frozen=True)
class TurnOutcome:
    """Result of ``run_turn``.

    ``status`` is one of ``completed`` | ``cancelled`` | ``failed``. Output is
    accounted by what crossed the playout boundary: ``played`` measures what
    the user heard, ``generated`` what the route produced; a route must never
    report generated output as played (ADR 0003 §4 clause 3).
    """

    status: str
    turn_id: int
    #: What the user actually heard (sentences, or playout fraction 0..1).
    played: Any = None
    #: Everything the route generated for the turn before it ended.
    generated: Any = None
    #: Terminal error for ``failed`` turns; diagnostic only.
    error: str | None = None
    #: What the user said, when the route transcribed it rather than the
    #: surface. Cascade transcribes before calling the route and already holds
    #: this; hybrid hands over raw audio, so without it a surface has no text
    #: to show for the turn.
    heard: str = ""
    #: Milliseconds spent turning that audio into text, for routes that must.
    #: Diagnostic only: it splits a slow turn into the transcription wait and
    #: everything after it, which are fixed by different things.
    heard_ms: int | None = None
    #: Detached work accepted by this turn; completion arrives as a task event.
    pending_task_id: str | None = None


class RecoveryOutcome(enum.Enum):
    """Result of ``recover()`` (ADR 0003 §2)."""

    RESTORED = "restored"
    RESTARTED = "restarted"
    FAILED = "failed"


@dataclass(frozen=True)
class RecoveryResult:
    outcome: RecoveryOutcome
    #: Same ``session_id`` as before the failure — identity is stable (§1).
    session_id: str
    detail: str | None = None


class RouteError(Exception):
    """Base class for route contract violations."""


class RouteStateError(RouteError):
    """A lifecycle transition was illegal for the route's current state."""


class RouteRuntimeError(RouteError):
    """A route operation failed at runtime (recoverable or fatal)."""


class RouteCapabilityError(RouteError):
    """The route was asked for a capability it declared unsupported."""


#: The output event stream type: normalized events, in order (ADR 0003 §3).
OutputEventStream = AsyncIterator[SessionEvent]


@runtime_checkable
class Route(Protocol):
    """The route contract every assembly strategy implements (ADR 0003 §6).

    Audit-rework additions (2026-08-21): routes OWN their in-flight turn
    task via :class:`InFlightTurn` (callers never assign private fields),
    deliver audio through the enforceable :class:`AudioOutput` boundary,
    expose ``recover()`` per ADR 0003 §2, and settle usage attempts
    through ``settle_usage`` so cancellation paths cannot leak open
    attempts.
    """

    capabilities: RouteCapabilities

    async def prepare(self, session: Any) -> None:
        """Bind to a session (identity, event sink, config) and acquire
        route resources. Called once before the first turn."""
        ...

    async def run_turn(self, route_input: RouteInput, ctx: TurnContext) -> TurnOutcome:
        """Assemble one turn from committed user input. Emits normalized
        events through ``ctx.emit`` as the turn progresses. The route owns
        the in-flight task; the outcome carries played-vs-generated
        accounting from the output boundary."""
        ...

    async def cancel_turn(self, turn_id: int) -> None:
        """Cancel the current turn under ADR 0003 §4 semantics: stop
        playback of the old turn, cancel route-internal tasks, abort
        upstream work, settle open attempts as cancelled. Cancelling a turn
        that is not active is a no-op, never an error."""
        ...

    async def interrupt_presentation(self, turn_id: int | None = None) -> None:
        """Revoke the current presentation epoch and clear queued audio.

        Hybrid routes keep detached specialist work running. Cascade routes
        have no detached task, so presentation interruption also cancels the
        in-flight cascade turn.
        """
        ...

    async def cancel_task(self, task_id: str) -> bool:
        """Explicitly abort ``task_id`` once; return whether it was active."""
        ...

    async def recover(self) -> RecoveryResult:
        """Attempt restoration after a recoverable failure (ADR 0003 §2).
        Returns restored | restarted | failed; never silently resumes
        mid-utterance audio. Routes that cannot recover return FAILED
        with detail."""
        ...

    def active_turn(self) -> InFlightTurn | None:
        """The route-owned in-flight turn, if any. Public accessor —
        callers inspect, never assign."""
        ...

    def output(self) -> AudioOutput | None:
        """The enforceable audio-output boundary this route delivers into
        (None when the surface bound no output yet)."""
        ...

    async def settle_usage(self, turn_id: int, *, status: str) -> None:
        """Settle every open usage attempt for a turn exactly once with
        the given status (ok | cancelled | error | session_closed).
        Idempotent per turn — the concrete usage settlement interface."""
        ...

    async def close(self) -> None:
        """Idempotent teardown. After ``close()`` returns, the route emits
        no further audio or agent output (the "fresh floor" rule)."""
        ...
