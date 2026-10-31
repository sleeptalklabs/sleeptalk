import asyncio

import pytest
from sleeptalk.adapter import AgentError
from sleeptalk.routes.base import RouteInput
from sleeptalk.routes.hybrid_types import TaskPhase
from sleeptalk.session_events import InterruptionSource

from tests.test_codex_app_server import FixtureCodex
from tests.test_hybrid_route import FakeAgent, _context, _route


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["event", "event_and_raise", "raise", "empty"])
async def test_specialist_failure_is_announced_once_after_ack(mode):
    agent = FakeAgent()
    agent.sentences = []
    message = "The 'gpt-6-astra' model requires a newer version of Codex."
    if mode in {"event", "event_and_raise"}:
        agent.events = [AgentError(code="turn_failed", message=message, retryable=False)]
    if mode in {"raise", "event_and_raise"}:
        agent.failure = RuntimeError(message)
    route, _, _, _, calls = await _route(agent=agent)
    try:
        outcome = await route.run_turn(RouteInput(text="Research the latest release"), _context(calls, 1))
        assert outcome.pending_task_id
        await route.wait_for_detached()
        assert route.tasks[0].phase is TaskPhase.FAILED
        tells = [call[2] for call in calls if call[0] == "realtime.tell"]
        assert len(tells) == 2, tells
        assert "couldn't" in tells[-1].lower() or "upgrade" in tells[-1].lower()
        events = [call[1] for call in calls if call[0] == "event"]
        failed = [e for e in events if e.payload.get("name") == "hybrid.task.failed"]
        assert len(failed) == 1
        assert failed[0].payload["taskId"] == outcome.pending_task_id
        spoken = [e for e in events if e.payload.get("delivered") and e.payload.get("text") == tells[-1]]
        assert len(spoken) == 1
    finally:
        await route.close()


@pytest.mark.asyncio
async def test_codex_failed_turn_crosses_adapter_and_is_spoken():
    class FailingCodex(FixtureCodex):
        def respond_streaming(self, text, *, on_event=None):
            return super().respond_streaming("error", on_event=on_event)

    route, _, _, _, calls = await _route(agent=FailingCodex(cwd="."))
    try:
        await route.run_turn(RouteInput(text="Research the latest release"), _context(calls, 1))
        await route.wait_for_detached()
        assert route.tasks[0].phase is TaskPhase.FAILED
        tells = [call[2] for call in calls if call[0] == "realtime.tell"]
        assert len(tells) == 2
        assert "couldn't complete" in tells[-1]
    finally:
        await route.close()


@pytest.mark.asyncio
async def test_stop_still_suppresses_a_late_failure_announcement():
    agent = FakeAgent()
    agent.sentences = []
    agent.wait = True
    agent.events = [AgentError(code="failed", message="Provider rejected the request", retryable=False)]
    route, _, _, _, calls = await _route(agent=agent)
    try:
        await route.run_turn(RouteInput(text="Research the latest release"), _context(calls, 1))
        await asyncio.wait_for(agent.started.wait(), 1)
        await route.interrupt_presentation(1, source=InterruptionSource.CANCEL)
        await route.run_turn(RouteInput(text="Are you there?"), _context(calls, 2))
        before = len([call for call in calls if call[0] == "realtime.tell"])
        agent.release.set()
        await route.wait_for_detached()
        assert len([call for call in calls if call[0] == "realtime.tell"]) == before
    finally:
        agent.release.set()
        await route.close()
