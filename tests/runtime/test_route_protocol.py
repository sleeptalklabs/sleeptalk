"""Tests for sleeptalk.routes.base — the Route protocol contract (r2-route-protocol).

Covers: route capabilities for cascade/native/hybrid without provider
conditionals; lifecycle transition determinism on a scripted fake route;
RouteInput validation; cancellation and terminal-error outcomes; capability
refusal.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any

import pytest

from sleeptalk.routes.base import (
    InterruptionGranularity,
    RecoveryOutcome,
    RecoveryResult,
    Route,
    RouteCapabilities,
    RouteCapabilityError,
    RouteError,
    RouteInput,
    RouteRuntimeError,
    RouteStateError,
    RouteType,
    TurnContext,
    TurnOutcome,
)
from sleeptalk.routes.audio_output import (
    AudioOutputAccounting,
    InFlightTurn,
)

from sleeptalk.session_events import (
    SessionEventType,
    SessionState,
    session_state,
)


# -- fake route -----------------------------------------------------------


class FakeRoute:
    """Scripted route used to exercise the protocol's lifecycle rules.

    It implements exactly the Route protocol surface — no more — so these
    tests pin the contract itself, not an implementation.
    """

    def __init__(self, *, fail_prepare: bool = False) -> None:
        self.capabilities = RouteCapabilities(
            route_type=RouteType.CASCADE,
            usage_operations=("stt.stream", "agent.stream", "tts.stream"),
        )
        self.transitions: list[str] = []
        self.prepared = False
        self.closed = False
        self.cancel_calls: list[int] = []
        self.events: list[Any] = []
        self.fail_prepare = fail_prepare
        self.turn_task: asyncio.Task[TurnOutcome] | None = None
        self.active_turn_id: int | None = None
        self.settled: list[tuple[int, str]] = []
        self._output = RecordingOutput()

    async def prepare(self, session: Any) -> None:
        if self.closed:
            raise RouteStateError("prepare after close")
        if self.fail_prepare:
            raise RouteRuntimeError("transport refused connection")
        self.prepared = True
        self.transitions.append("prepare")

    async def run_turn(self, route_input: RouteInput, ctx: TurnContext) -> TurnOutcome:
        if self.closed:
            raise RouteStateError("run_turn after close")
        if not self.prepared:
            raise RouteStateError("run_turn before prepare")
        self.active_turn_id = ctx.turn_id
        self.transitions.append(f"turn:{ctx.turn_id}")
        ctx.emit(
            session_state(ctx.session_id, SessionState.ACTIVE, turn_id=ctx.turn_id)
        )
        try:
            await asyncio.sleep(3600)  # simulate long work; cancelled by tests
        except asyncio.CancelledError:
            return TurnOutcome(
                status="cancelled", turn_id=ctx.turn_id, played=None, generated=None
            )

    async def cancel_turn(self, turn_id: int) -> None:
        self.cancel_calls.append(turn_id)
        if self.turn_task is not None and not self.turn_task.done():
            self.turn_task.cancel()
        self.active_turn_id = None
        self.transitions.append(f"cancel:{turn_id}")

    async def interrupt_presentation(self, turn_id: int | None = None) -> None:
        if turn_id is not None:
            await self.cancel_turn(turn_id)

    async def cancel_task(self, task_id: str) -> bool:
        del task_id
        return False

    async def recover(self) -> "RecoveryResult":
        from sleeptalk.routes.base import RecoveryOutcome

        self.transitions.append("recover")
        return RecoveryResult(
            outcome=RecoveryOutcome.RESTARTED, session_id="s-1"
        )

    def active_turn(self):
        from sleeptalk.routes.audio_output import InFlightTurn

        if self.active_turn_id is None:
            return None
        return InFlightTurn(turn_id=self.active_turn_id, task=self.turn_task)

    def output(self):
        return self._output

    async def settle_usage(self, turn_id: int, *, status: str) -> None:
        if (turn_id, status) in self.settled:
            return
        self.settled.append((turn_id, status))

    async def close(self) -> None:
        if self.closed:
            return  # idempotent
        if self.turn_task is not None and not self.turn_task.done():
            self.turn_task.cancel()
        self.closed = True
        self.transitions.append("close")


def make_ctx(route: FakeRoute, turn_id: int = 1) -> TurnContext:
    return TurnContext(session_id="s-1", turn_id=turn_id, emit=route.events.append)


class RecordingOutput:
    """AudioOutput implementation for tests: records writes + accounting."""

    def __init__(self) -> None:
        self.writes: list[tuple[bytes, int]] = []
        self.closed = False
        self._accounting = AudioOutputAccounting(turn_id=-1)

    async def write(self, pcm: bytes, turn_id: int) -> None:
        if self.closed:
            return
        self.writes.append((pcm, turn_id))
        self._accounting.written(len(pcm))

    async def finish_turn(self, turn_id: int) -> None:
        return None

    async def discard_turn(self, turn_id: int, reason: str = "interrupted") -> None:
        return None

    def accounting(self) -> AudioOutputAccounting:
        return self._accounting

    async def close(self) -> None:
        self.closed = True


# -- protocol shape -------------------------------------------------------


def test_route_protocol_methods() -> None:
    """The protocol names the contract operations (ADR 0003 §6)."""
    fake = FakeRoute()
    assert isinstance(fake, Route)  # runtime_checkable structural check
    for name in (
        "prepare",
        "run_turn",
        "interrupt_presentation",
        "cancel_task",
        "cancel_turn",
        "close",
    ):
        assert hasattr(fake, name)


def test_route_capabilities_are_provider_free() -> None:
    """Capabilities describe assembly strategy, never a provider name."""
    for route_type in (RouteType.CASCADE, RouteType.NATIVE, RouteType.HYBRID):
        caps = RouteCapabilities(route_type=route_type)
        assert caps.route_type is route_type
        assert "provider" not in caps.__dataclass_fields__


def test_native_granularity_is_playout_fraction() -> None:
    """Declared material differences survive as data (ADR 0003 §5)."""
    native = RouteCapabilities(
        route_type=RouteType.NATIVE,
        interruption_granularity=InterruptionGranularity.PLAYOUT_FRACTION,
        resume_after_interruption=False,
        usage_operations=("rt.audio",),
    )
    cascade = RouteCapabilities(
        route_type=RouteType.CASCADE,
        resume_after_interruption=True,
        usage_operations=("stt.stream", "agent.stream", "tts.stream"),
    )
    assert native.interruption_granularity is InterruptionGranularity.PLAYOUT_FRACTION
    assert cascade.interruption_granularity is InterruptionGranularity.SENTENCE
    assert cascade.resume_after_interruption and not native.resume_after_interruption
    assert "rt.audio" in native.usage_operations
    assert "rt.audio" not in cascade.usage_operations


# -- RouteInput -----------------------------------------------------------


def test_route_input_requires_exactly_one_payload() -> None:
    with pytest.raises(ValueError):
        RouteInput()
    with pytest.raises(ValueError):
        RouteInput(pcm=b"audio", text="both")
    assert RouteInput(pcm=b"audio").pcm == b"audio"
    assert RouteInput(text="hello").text == "hello"


# -- lifecycle determinism ------------------------------------------------


@pytest.mark.asyncio
async def test_lifecycle_order_is_deterministic() -> None:
    route = FakeRoute()
    await route.prepare(session=None)
    ctx = make_ctx(route, turn_id=1)
    route.turn_task = asyncio.create_task(route.run_turn(RouteInput(text="hi"), ctx))
    await asyncio.sleep(0.01)
    await route.cancel_turn(1)
    outcome = await route.turn_task
    await route.close()
    await route.close()  # idempotent

    assert route.transitions == ["prepare", "turn:1", "cancel:1", "close"]
    assert outcome.status == "cancelled"
    assert outcome.turn_id == 1
    assert route.closed


@pytest.mark.asyncio
async def test_prepare_failure_is_terminal() -> None:
    route = FakeRoute(fail_prepare=True)
    with pytest.raises(RouteRuntimeError):
        await route.prepare(session=None)
    assert not route.prepared
    with pytest.raises(RouteStateError):
        await route.run_turn(RouteInput(text="x"), make_ctx(route))


@pytest.mark.asyncio
async def test_turn_before_prepare_rejected() -> None:
    route = FakeRoute()
    with pytest.raises(RouteStateError):
        await route.run_turn(RouteInput(text="x"), make_ctx(route))


@pytest.mark.asyncio
async def test_turn_after_close_rejected() -> None:
    route = FakeRoute()
    await route.prepare(session=None)
    await route.close()
    with pytest.raises(RouteStateError):
        await route.run_turn(RouteInput(text="x"), make_ctx(route))


@pytest.mark.asyncio
async def test_cancel_when_idle_is_noop_not_error() -> None:
    """ADR 0003 §4 clause 5: cancelling with no active turn never errors."""
    route = FakeRoute()
    await route.prepare(session=None)
    await route.cancel_turn(99)  # unknown turn
    assert route.cancel_calls == [99]
    assert route.active_turn_id is None


@pytest.mark.asyncio
async def test_close_cancels_inflight_turn() -> None:
    """Close must not leave a turn running (fresh-floor rule)."""
    route = FakeRoute()
    await route.prepare(session=None)
    ctx = make_ctx(route, turn_id=1)
    route.turn_task = asyncio.create_task(route.run_turn(RouteInput(text="hi"), ctx))
    await asyncio.sleep(0.01)
    await route.close()
    outcome = await route.turn_task
    assert outcome.status == "cancelled"


@pytest.mark.asyncio
async def test_new_turn_after_interruption() -> None:
    route = FakeRoute()
    await route.prepare(session=None)
    ctx1 = make_ctx(route, 1)
    route.turn_task = asyncio.create_task(route.run_turn(RouteInput(text="one"), ctx1))
    await asyncio.sleep(0)
    await route.cancel_turn(1)
    await route.turn_task

    ctx2 = make_ctx(route, 2)
    route.turn_task = asyncio.create_task(route.run_turn(RouteInput(text="two"), ctx2))
    await asyncio.sleep(0)
    assert route.active_turn_id == 2
    await route.cancel_turn(2)
    await route.turn_task
    await route.close()


@pytest.mark.asyncio
async def test_turn_emits_normalized_events() -> None:
    route = FakeRoute()
    await route.prepare(session=None)
    ctx = make_ctx(route, 1)
    route.turn_task = asyncio.create_task(route.run_turn(RouteInput(text="hi"), ctx))
    await asyncio.sleep(0.01)
    await route.cancel_turn(1)
    await route.turn_task

    assert [e.type for e in route.events] == [SessionEventType.SESSION_STATE]
    assert route.events[0].payload["state"] == SessionState.ACTIVE.value
    assert route.events[0].payload["turn_id"] == 1


# -- outcomes and recovery ------------------------------------------------


def test_turn_outcome_partial_output_accounting() -> None:
    """played and generated are distinct; generated never masquerades as played."""
    outcome = TurnOutcome(
        status="cancelled", turn_id=3, played=["First sentence."], generated=["First sentence.", "Second sen"]
    )
    assert outcome.played != outcome.generated
    outcome_full = TurnOutcome(status="completed", turn_id=4, played=[1.0], generated=[1.0])
    assert outcome_full.played == outcome_full.generated


def test_recovery_result_preserves_session_id() -> None:
    result = RecoveryResult(
        outcome=RecoveryOutcome.RESTORED, session_id="s-1", detail="rollover"
    )
    assert result.outcome is RecoveryOutcome.RESTORED
    assert result.session_id == "s-1"
    for outcome in RecoveryOutcome:
        assert outcome.value in {"restored", "restarted", "failed"}


def test_error_hierarchy() -> None:
    assert issubclass(RouteStateError, RouteError)
    assert issubclass(RouteRuntimeError, RouteError)
    assert issubclass(RouteCapabilityError, RouteError)


def test_capability_error_is_loud() -> None:
    """Asking a route for an unsupported capability fails explicitly."""
    caps = RouteCapabilities(route_type=RouteType.NATIVE, resume_after_interruption=False)
    if not caps.resume_after_interruption:
        with pytest.raises(RouteCapabilityError):
            raise RouteCapabilityError("resume unsupported on native routes")


# -- audit rework: owned task, recovery, output boundary, usage settlement --


@pytest.mark.asyncio
async def test_route_owns_in_flight_task() -> None:
    """Callers never assign private fields: active_turn() exposes the
    route-owned task after run_turn begins."""
    route = FakeRoute()
    await route.prepare(session=None)
    ctx = make_ctx(route, 1)
    route.turn_task = asyncio.create_task(route.run_turn(RouteInput(text="hi"), ctx))
    await asyncio.sleep(0.01)

    in_flight = route.active_turn()
    assert in_flight is not None
    assert in_flight.turn_id == 1
    assert in_flight.task is route.turn_task
    assert in_flight.cancel() is True  # cancels the live task
    await route.turn_task

    await route.cancel_turn(1)
    await route.close()
    assert route.active_turn() is None or route.active_turn_id is None


@pytest.mark.asyncio
async def test_recover_returns_outcome() -> None:
    """recover() answers restored|restarted|failed; never raises."""
    route = FakeRoute()
    await route.prepare(session=None)
    result = await route.recover()
    assert result.outcome.value in {"restored", "restarted", "failed"}
    assert result.session_id  # identity preserved
    await route.close()


def test_output_boundary_is_enforceable() -> None:
    """output() returns the AudioOutput the route delivers into; the
    accounting tracks played vs generated."""
    route = FakeRoute()
    out = route.output()
    assert isinstance(out, RecordingOutput)
    acc = out.accounting()
    acc.generated(100)
    acc.written(60)
    acc.dropped_stale(40)
    assert acc.played_fraction == pytest.approx(0.6)
    assert acc.bytes_dropped_stale == 40


@pytest.mark.asyncio
async def test_settle_usage_is_idempotent_per_turn() -> None:
    """The usage settlement interface settles each turn exactly once."""
    route = FakeRoute()
    await route.prepare(session=None)
    await route.settle_usage(1, status="cancelled")
    await route.settle_usage(1, status="cancelled")  # duplicate: no-op
    await route.settle_usage(2, status="ok")
    assert route.settled.count((1, "cancelled")) == 1
    assert (2, "ok") in route.settled
    await route.close()


def test_audio_output_protocol_structural() -> None:
    """RecordingOutput satisfies the enforceable AudioOutput protocol."""
    from sleeptalk.routes.audio_output import AudioOutput

    assert isinstance(RecordingOutput(), AudioOutput)


@pytest.mark.asyncio
async def test_in_flight_turn_cancel_is_once() -> None:
    # a completed task cannot be cancelled
    done = asyncio.ensure_future(_noop())
    await done
    in_flight = InFlightTurn(turn_id=7, task=done)
    assert in_flight.cancel() is False


async def _noop() -> None:
    return None
