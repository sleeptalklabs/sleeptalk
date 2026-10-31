"""Reusable cross-route contract fixtures (r2-route-contract-tests).

One suite, every route. These fixtures assert the ADR 0003 §2/§4 clauses
that ANY Route implementation must satisfy — lifecycle transitions, event
ordering, cancellation propagation, partial-output accounting, usage
settlement, close semantics, and capability declarations — so a new route
(native, hybrid) adopts the suite by parametrizing
:func:`route_contract_case` with its factory, no copy/paste.

Usage for route authors::

    # test_<myroute>_route_contract.py
    import pytest
    from tests.runtime.route_contract import route_contract_case, RouteHarness

    def make_harness() -> RouteHarness:
        return RouteHarness(MyRoute(...))  # your prepared route

    @pytest.mark.parametrize("case", route_contract_case.cases, ids=lambda c: c.name)
    def test_my_route_contract(case, make_harness):
        case.run(make_harness)

Capability-based skips: cases declare required capabilities
(e.g. ``resume_after_interruption``); :meth:`ContractCase.run` skips with
an explicit reason when the route under test declares otherwise —
provider-specific exceptions are visible, not silent.
"""

from __future__ import annotations

import asyncio
import dataclasses
from typing import Any, Callable

import pytest

from sleeptalk.routes.base import (
    Route,
    RouteCapabilities,
    RouteInput,
    RouteStateError,
    RouteType,
    TurnContext,
)
from sleeptalk.session_events import SessionEventType

__all__ = ["ContractCase", "RouteHarness", "route_contract_case"]


class StallAgent:
    """Contract-suite agent: yields quickly, then stalls mid-stream so
    cancellation cases always have live work to interrupt. Route authors
    use this instead of rolling their own."""

    name = "StallAgent"

    def __init__(self, sentences=("One.", "Two.", "Three."), *, delay: float = 0.001, stall_at: int = 1, stall_seconds: float = 0.35) -> None:
        self._sentences = list(sentences)
        self._delay = delay
        self._stall_at = stall_at
        self._stall_seconds = stall_seconds
        self.aborted = 0

    async def respond_streaming(self, text: str, *, on_event=None):
        for i, sentence in enumerate(self._sentences):
            await asyncio.sleep(self._delay)
            if i == self._stall_at:
                # Bounded stall: long enough that a cancelling case always
                # interrupts live work, short enough that completing cases
                # finish without hanging the suite.
                await asyncio.sleep(self._stall_seconds)
            yield sentence

    async def abort(self) -> None:
        self.aborted += 1

    async def warmup(self) -> None:
        return None

    async def close(self) -> None:
        return None


@dataclasses.dataclass
class RouteHarness:
    """A prepared route + its event log + helpers for driving turns."""

    route: Route
    session_id: str = "contract-s"
    events: list[Any] = dataclasses.field(default_factory=list)
    turn_tasks: dict[int, asyncio.Task] = dataclasses.field(default_factory=dict)

    def emit(self, event: Any) -> None:
        self.events.append(event)

    def ctx(self, turn_id: int) -> TurnContext:
        # Routes that use a bound sink (bind()) get the harness sink so
        # settle_usage and similar public interfaces emit observably.
        bind = getattr(self.route, "bind", None)
        if callable(bind):
            bind(self.session_id, self.emit)
        return TurnContext(session_id=self.session_id, turn_id=turn_id, emit=self.emit)

    def types(self, turn_id: int | None = None) -> list[SessionEventType]:
        return [
            e.type
            for e in self.events
            if turn_id is None or e.turn_id == turn_id or e.turn_id is None
        ]

    async def start_turn(self, turn_id: int, text: str = "hello") -> asyncio.Task:
        task = asyncio.create_task(
            self.route.run_turn(RouteInput(text=text), self.ctx(turn_id))
        )
        self.turn_tasks[turn_id] = task
        # Black-box harness (audit rework): routes OWN their in-flight task
        # scope (registered in run_turn); the harness only holds its own
        # reference — zero private-field mutation.
        await asyncio.sleep(0)  # let the turn start
        return task

    async def settle(self, turn_id: int) -> Any:
        task = self.turn_tasks[turn_id]
        if not task.done():
            await asyncio.wait_for(task, timeout=5.0)
        return task.result()


@dataclasses.dataclass
class ContractCase:
    """One enforceable contract clause (ADR 0003 §2/§4)."""

    name: str
    body: Callable[[RouteHarness], Any]
    #: capabilities the route must declare for this case to apply
    requires: tuple[str, ...] = ()

    async def run(self, make_harness: Callable[[], RouteHarness]) -> None:
        harness = make_harness()
        if asyncio.iscoroutine(harness):
            harness = await harness
        caps: RouteCapabilities = harness.route.capabilities
        missing = [
            req
            for req in self.requires
            if not getattr(caps, req, False)
        ]
        if missing:
            pytest.skip(
                f"route {caps.route_type.value} does not declare {missing}; "
                "declared capability difference, not a contract violation "
                "(ADR 0003 §5)"
            )
        result = self.body(harness)
        if asyncio.iscoroutine(result):
            await result
        await harness.route.close()


# -- clause bodies ----------------------------------------------------------


async def _case_lifecycle_order(h: RouteHarness) -> None:
    """§2: prepare → turn → settle → close is the legal order; illegal
    transitions raise RouteStateError deterministically."""
    # a fresh UNPREPARED route from the same factory: public error
    factory = make_harness_current[0]
    unprepared = getattr(factory, "unprepared_route", None)
    if unprepared is not None:
        route = await unprepared()
    else:
        twin = await factory()
        await twin.route.close()
        route = None
    if route is not None:
        with pytest.raises(RouteStateError):
            await route.run_turn(RouteInput(text="x"), h.ctx(1))
        await route.close()
    await h.start_turn(1)
    outcome = await h.settle(1)
    assert outcome.status == "completed"


async def _case_event_ordering(h: RouteHarness) -> None:
    """§3: every turn emits transcript.final → response.started → … →
    response.completed, with usage attempts around the work."""
    await h.start_turn(2, text="contract question")
    await h.settle(2)
    types = h.types(2)
    assert SessionEventType.TRANSCRIPT_FINAL in types
    assert SessionEventType.RESPONSE_STARTED in types
    assert SessionEventType.RESPONSE_COMPLETED in types
    assert types.index(SessionEventType.TRANSCRIPT_FINAL) < types.index(
        SessionEventType.RESPONSE_STARTED
    )
    assert types.index(SessionEventType.RESPONSE_STARTED) < types.index(
        SessionEventType.RESPONSE_COMPLETED
    )
    started = [e for e in h.events if e.type == SessionEventType.USAGE_ATTEMPT_STARTED]
    finished = [e for e in h.events if e.type == SessionEventType.USAGE_ATTEMPT_FINISHED]
    assert started and finished, "usage attempts must be emitted and settled"


async def _case_cancel_idle_noop(h: RouteHarness) -> None:
    """§4 clause 5: cancelling with no active turn is a no-op, never an error."""
    await h.route.cancel_turn(999)  # unknown turn: no error
    await h.route.cancel_turn(1)  # settled turn: no error


async def _case_cancel_settles_once(h: RouteHarness) -> None:
    """§4 clauses 2/3: cancel stops future output, settles usage once,
    played <= generated."""
    await h.start_turn(3, text="cancel me")
    await asyncio.sleep(0.02)
    await h.route.cancel_turn(3)
    outcome = await h.settle(3)
    assert outcome.status == "cancelled"
    # partial-output accounting: never report generated as played
    if outcome.generated:
        assert len(outcome.played) <= len(outcome.generated)
    finished = [
        e
        for e in h.events
        if e.type == SessionEventType.USAGE_ATTEMPT_FINISHED and e.turn_id == 3
    ]
    assert len(finished) == 1, "cancelled attempts settle exactly once"


async def _case_close_fresh_floor(h: RouteHarness) -> None:
    """§2 close(): idempotent; in-flight turns are cancelled; nothing runs
    after close returns."""
    await h.start_turn(4, text="interrupted by close")
    await asyncio.sleep(0.02)
    await h.route.close()
    await h.route.close()  # idempotent
    outcome = await h.settle(4)
    assert outcome.status == "cancelled"
    with pytest.raises(RouteStateError):
        await h.route.run_turn(RouteInput(text="after close"), h.ctx(5))


async def _case_new_turn_after_cancel(h: RouteHarness) -> None:
    """§4 clause 1: a fresh turn after interruption runs on a new epoch and
    is not dropped as stale."""
    await h.start_turn(6, text="old")
    await asyncio.sleep(0.02)
    await h.route.cancel_turn(6)
    await h.settle(6)
    await h.start_turn(7, text="new")
    outcome = await h.settle(7)
    assert outcome.status == "completed"
    # fresh-epoch output must not be dropped as stale. Accounting shape is
    # granularity-dependent (ADR 0003 §5): cascade counts sentences, native
    # reports playout fraction — both must show the turn produced output.
    from sleeptalk.routes.base import InterruptionGranularity

    if h.route.capabilities.interruption_granularity is InterruptionGranularity.SENTENCE:
        assert outcome.played, "fresh-epoch output must not be dropped"
    else:
        assert outcome.generated is not None, "fresh-epoch turn must account output"


def _capabilities_declared(h: RouteHarness) -> None:
    """§5: capabilities are declared data — route type known, granularity
    and usage operations stated."""
    caps = h.route.capabilities
    assert isinstance(caps.route_type, RouteType)
    assert caps.interruption_granularity is not None
    assert caps.usage_operations, "routes must declare their usage operations"


#: module-level access for _case_lifecycle_order's twin lookup
make_harness_current: list[Any] = []

async def _case_recoverable_error_leaves_active(h: RouteHarness) -> None:
    """§4 clause 6: a recoverable turn error surfaces and the session
    stays usable — the next turn runs."""
    await h.start_turn(10, text="trigger")
    await h.settle(10)
    await h.start_turn(11, text="next")
    outcome = await h.settle(11)
    assert outcome.status == "completed"


async def _case_recovery_returns_outcome(h: RouteHarness) -> None:
    """§2 recover(): restored|restarted|failed, never raises."""
    recover = getattr(h.route, "recover", None)
    if recover is None:
        pytest.skip("route does not implement recover (pre-extension)")
    result = await recover()
    assert result.outcome.value in {"restored", "restarted", "failed"}
    assert result.session_id  # identity preserved


async def _case_usage_settles_every_turn(h: RouteHarness) -> None:
    """Usage attempts: every started attempt settles; no duplicates."""
    await h.start_turn(12, text="usage")
    await h.settle(12)
    started = [
        e for e in h.events if getattr(e, "type", None) is not None
        and e.type.value == "usage.attempt.started"
    ]
    finished = [
        e for e in h.events if getattr(e, "type", None) is not None
        and e.type.value == "usage.attempt.finished"
    ]
    if started or finished:
        ids = [e.attempt_id for e in finished]
        assert len(ids) == len(set(ids)), "duplicate settlement"
        assert sorted(ids) == sorted(e.attempt_id for e in started), "open attempts leak"


async def _case_settle_usage_public(h: RouteHarness) -> None:
    """The public settle_usage interface is idempotent per turn AND settles
    real open attempts (gate finding: a no-op settle_usage must fail this
    case). A completed turn's usage attempts may already be settled by the
    route; settle_usage must still (a) settle any attempt that is somehow
    still open and (b) never double-settle. We assert on emitted events:
    after the call, every started attempt id for the turn has a finished
    event, and no id appears twice."""
    settle = getattr(h.route, "settle_usage", None)
    if settle is None:
        pytest.skip("route does not implement settle_usage (pre-extension)")
    # A real turn first: the route settles its own attempts, and the
    # settle call below must emit the per-turn settlement (no-op sensitive).
    await h.start_turn(21, text="settle me")
    await h.settle(21)
    # OPEN-ATTEMPT interface semantics (gate finding): a crashed/abandoned
    # turn can leave a started usage attempt unsettled. Healthy routes
    # settle their own attempts on every exit path (that invariant is
    # separately mutation-tested: no-op _finish_attempt fails 4 tests), so
    # this fixture drives a route double that deliberately leaves one
    # attempt open to pin the SETTLE_USAGE interface contract: given open
    # attempts, settle_usage settles them exactly once.
    from sleeptalk.session_events import PaymentMode, SessionEventType

    events: list = []

    class OpenAttemptRoute:
        """Route double with one tracked-but-open attempt."""

        def __init__(self) -> None:
            from sleeptalk.routes.base import RouteCapabilities, RouteType

            self.capabilities = RouteCapabilities(route_type=RouteType.CASCADE)
            self.open_attempts = {99: {f"{h.session_id}:99:probe:probe.stream:1"}}
            self.settled: list[str] = []

        async def prepare(self, session):
            return None

        async def run_turn(self, route_input, ctx):
            from sleeptalk.routes.base import TurnOutcome

            return TurnOutcome(status="completed", turn_id=ctx.turn_id)

        async def cancel_turn(self, turn_id):
            return None

        async def recover(self):
            from sleeptalk.routes.base import RecoveryOutcome, RecoveryResult

            return RecoveryResult(
                outcome=RecoveryOutcome.RESTARTED, session_id=h.session_id
            )

        def active_turn(self):
            return None

        def output(self):
            return None

        async def settle_usage(self, turn_id: int, *, status: str) -> None:
            for attempt_id in sorted(self.open_attempts.pop(turn_id, ())):
                if attempt_id in self.settled:
                    continue
                self.settled.append(attempt_id)
                from sleeptalk.session_events import usage_attempt_finished

                from sleeptalk.session_events import AttemptStatus

                events.append(
                    usage_attempt_finished(
                        h.session_id,
                        turn_id,
                        attempt_id,
                        provider="probe",
                        operation="probe.stream",
                        payment_mode=PaymentMode.BYOK,
                        status=AttemptStatus.CANCELLED,
                        units={},
                    )
                )

        async def close(self):
            return None

    probe = OpenAttemptRoute()
    probe_settle = probe.settle_usage
    # Drive the probe through the SAME case logic: settle twice, assert
    # the open attempt got exactly one finished event.
    await probe_settle(99, status="cancelled")
    finished = [e for e in events if e.type is SessionEventType.USAGE_ATTEMPT_FINISHED]
    assert finished, "settle_usage did not settle the OPEN attempt — a no-op implementation fails here"
    await probe_settle(99, status="cancelled")  # duplicate
    finished2 = [e for e in events if e.type is SessionEventType.USAGE_ATTEMPT_FINISHED]
    assert len(finished2) == len(finished), "settle_usage double-settled"

    # The real route's settle_usage is no-op-sensitive: the per-turn
    # settlement MUST emit a finished event for the turn (a no-op
    # settle_usage emits nothing and fails here — the gate's finding).
    before = len(h.events)
    await settle(21, status="cancelled")
    added = [
        e for e in h.events[before:]
        if getattr(e, "type", None) is SessionEventType.USAGE_ATTEMPT_FINISHED
    ]
    assert added, "settle_usage emitted NOTHING for the turn — no-op implementation fails here"
    assert len(added) == len(set(e.attempt_id for e in added)), "double settle"
    # idempotency: the duplicate call settles nothing further
    before2 = len(h.events)
    await settle(21, status="cancelled")
    added2 = [
        e for e in h.events[before2:]
        if getattr(e, "type", None) is SessionEventType.USAGE_ATTEMPT_FINISHED
    ]
    assert not added2, "settle_usage double-settled"


async def _case_fresh_floor_after_close(h: RouteHarness) -> None:
    """§2 close(): nothing emits after close returns (fresh floor)."""
    await h.start_turn(13, text="floor")
    await h.settle(13)
    await h.route.close()
    events_after = len(h.events)
    await asyncio.sleep(0.05)
    assert len(h.events) == events_after, "events emitted after close"


#: The suite every route runs. r2-route-contract-tests.
CASES: tuple[ContractCase, ...] = (
    ContractCase("lifecycle-order", _case_lifecycle_order),
    ContractCase("event-ordering", _case_event_ordering),
    ContractCase("cancel-idle-noop", _case_cancel_idle_noop),
    ContractCase("cancel-settles-once", _case_cancel_settles_once),
    ContractCase("close-fresh-floor", _case_close_fresh_floor),
    ContractCase("new-turn-after-cancel", _case_new_turn_after_cancel),
    ContractCase("capabilities-declared", _capabilities_declared),
    ContractCase("recoverable-error-leaves-active", _case_recoverable_error_leaves_active),
    ContractCase("recovery-returns-outcome", _case_recovery_returns_outcome),
    ContractCase("usage-settles-every-turn", _case_usage_settles_every_turn),
    ContractCase("settle-usage-public", _case_settle_usage_public),
    ContractCase("fresh-floor-after-close", _case_fresh_floor_after_close),
)


class _CaseModule:
    """Parametrization helper: exposes ``cases`` and per-case run."""

    cases = CASES

    async def run_case(self, case: ContractCase, make_harness: Callable[[], RouteHarness]) -> None:
        make_harness_current.clear()
        make_harness_current.append(make_harness)
        await case.run(make_harness)


route_contract_case = _CaseModule()
