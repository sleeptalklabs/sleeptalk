from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest
from sleeptalk.routes.base import RouteInput
from sleeptalk.routes.hybrid import HybridRoute, RealtimeAudio, RealtimeTranscript
from sleeptalk.routes.hybrid_types import (
    DeliveryPhase,
    HandsCatalog,
    HybridRoleContract,
    TaskPhase,
)
from sleeptalk.session_events import InterruptionSource, SessionEventType

from tests.test_hybrid_route import (
    FakeAgent,
    FakeOutput,
    FakeRealtime,
    _context,
    _route,
)


@dataclass(frozen=True)
class ToolCall:
    call_id: str
    question: str


class ToolRealtime(FakeRealtime):
    async def resolve_specialist(self, call_id, output):
        self.calls.append(("resolve", call_id, output))


@pytest.mark.asyncio
async def test_tool_duplicate_and_busy_share_admission_and_spoken_ack():
    calls = []
    provider = ToolRealtime(calls)
    provider.outputs = [ToolCall("one", "Read README"), ToolCall("one", "Read README")]
    agent = FakeAgent()
    agent.wait = True
    route = HybridRoute(role_contract=HybridRoleContract("Sleeptalk"), agent=agent,
                        realtime=provider, audio_output=FakeOutput(calls))
    await route.prepare(type("Session", (), {"session_id": "s", "emit": staticmethod(lambda e: None)})())
    first = await route.run_turn(RouteInput(text="How are you?"), _context(calls, 1))
    await asyncio.wait_for(agent.started.wait(), 2)
    provider.outputs = [ToolCall("two", "Read another file")]
    second = await route.run_turn(RouteInput(text="What do you think about jazz?"), _context(calls, 2))
    assert first.played and second.played
    assert len(agent.calls) == 1
    assert len([c for c in calls if c[0] == "resolve"]) == 2
    assert len([e for e in route.reducer.events if e.kind == "intent.committed"]) == 2
    assert any(c[0] == "realtime.tell" and "still working" in c[2] for c in calls)
    agent.release.set()
    await route.wait_for_detached()
    await route.close()


@pytest.mark.asyncio
async def test_stop_suppresses_findings_produced_after_stop():
    agent = FakeAgent()
    agent.pause_after_first = True
    agent.sentences = ["First finding.", "Finding produced after stop."]
    route, _, provider, _, calls = await _route(agent=agent)
    await route.run_turn(RouteInput(text="Research the latest release"), _context(calls, 1))
    await asyncio.wait_for(agent.after_first.wait(), 2)
    await route.interrupt_presentation(1, source=InterruptionSource.CANCEL)
    agent.release_after_first.set()
    await route.wait_for_detached()
    await route.run_turn(RouteInput(text="How are you?"), _context(calls, 2))
    assert route.tasks[0].delivery is DeliveryPhase.SUPPRESSED
    assert not any(c[0] == "realtime.tell" and "Finding produced after stop" in c[2] for c in calls)
    assert "Finding produced after stop." in route.tasks[0].result
    await route.close()


@pytest.mark.asyncio
async def test_silent_script_does_not_publish_delivered_text():
    calls = []
    class Silent(FakeRealtime):
        async def tell(self, material, *, turn_id):
            yield material.text
    route = HybridRoute(role_contract=HybridRoleContract("Sleeptalk"), agent=FakeAgent(),
                        realtime=Silent(calls), audio_output=FakeOutput(calls))
    await route.prepare(type("Session", (), {"session_id": "s", "emit": staticmethod(lambda e: None)})())
    outcome = await route.run_turn(RouteInput(text="Are you there?"), _context(calls, 1))
    assert not outcome.played
    assert not any(c[0] == "event" and c[1].payload.get("delivered") for c in calls)
    await route.close()


@pytest.mark.asyncio
async def test_delivered_specialist_summary_enters_next_context_once():
    route, agent, _, _, calls = await _route()
    await route.run_turn(RouteInput(text="Research latest release"), _context(calls, 1))
    await route.wait_for_detached()
    assert route.tasks[0].delivery is DeliveryPhase.DELIVERED
    await route.run_turn(RouteInput(text="Research another release"), _context(calls, 2))
    await route.wait_for_detached()
    assert "Sleeptalk: Specialist answer." in agent.calls[-1] or ": Specialist answer." in agent.calls[-1]
    spoken = [c[1] for c in calls if c[0] == "event" and c[1].payload.get("delivered")
              and c[1].payload.get("text") == "Specialist answer."]
    assert len(spoken) == 2
    assert spoken[0].payload["taskId"] != spoken[1].payload["taskId"]
    await route.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["How are you?", "Stop", "Cancel the task"])
async def test_speculation_runs_before_transcript_but_gates_audio_and_tools(text):
    calls = []
    class Speculative(ToolRealtime):
        def __init__(self):
            super().__init__(calls)
            self.response_started = asyncio.Event()
        async def commit_audio(self, route_input, *, turn_id):
            return "item-1"
        async def transcript_for(self, *, turn_id):
            await asyncio.wait_for(self.response_started.wait(), 1)
            assert not any(c[0] == "audio.write" for c in calls)
            return text
        async def respond(self, transcript, *, turn_id):
            self.response_started.set()
            yield ToolCall("one", "Read README")
    agent = FakeAgent()
    route = HybridRoute(role_contract=HybridRoleContract("Sleeptalk"), agent=agent,
                        realtime=Speculative(), audio_output=FakeOutput(calls), speculative_audio=True)
    await route.prepare(type("Session", (), {"session_id": "s", "emit": staticmethod(lambda e: None)})())
    outcome = await route.run_turn(RouteInput(pcm=b"audio"), _context(calls, 1))
    await route.wait_for_detached()
    assert bool(agent.calls) == (text == "How are you?")
    assert outcome.heard == text
    await route.close()


@pytest.mark.asyncio
async def test_speculative_audio_preserves_the_committed_specialist_owner():
    calls = []
    question = "Tell me what intelligence layer you're using, like large language model."

    class SpeculativeAnswer(FakeRealtime):
        def __init__(self):
            super().__init__(calls)
            self.response_started = asyncio.Event()

        async def commit_audio(self, route_input, *, turn_id):
            return "item-1"

        async def transcript_for(self, *, turn_id):
            await asyncio.wait_for(self.response_started.wait(), 1)
            return question

        async def respond(self, transcript, *, turn_id):
            self.response_started.set()
            yield RealtimeTranscript("Generic mouth answer.")
            yield RealtimeAudio(b"generic-mouth-audio")

    agent = FakeAgent(hands_catalog=HandsCatalog(open=True))
    route = HybridRoute(
        role_contract=HybridRoleContract("Sleeptalk"),
        agent=agent,
        realtime=SpeculativeAnswer(),
        audio_output=FakeOutput(calls),
        speculative_audio=True,
    )
    await route.prepare(
        type("Session", (), {"session_id": "s", "emit": staticmethod(lambda e: None)})()
    )

    outcome = await route.run_turn(RouteInput(pcm=b"audio"), _context(calls, 1))
    await route.wait_for_detached()

    assert outcome.pending_task_id is not None
    assert agent.calls and agent.calls[-1].endswith(question)
    assert not any(
        call[0] == "audio.write" and call[2] == b"generic-mouth-audio"
        for call in calls
    )
    await route.close()


@pytest.mark.asyncio
async def test_specialist_tool_call_stops_later_mouth_audio_before_dispatch():
    calls = []
    provider = ToolRealtime(calls)
    provider.outputs = [
        ToolCall("one", "Read README"),
        RealtimeAudio(b"audio-after-tool-call"),
    ]
    agent = FakeAgent()
    route = HybridRoute(
        role_contract=HybridRoleContract("Sleeptalk"),
        agent=agent,
        realtime=provider,
        audio_output=FakeOutput(calls),
    )
    await route.prepare(
        type("Session", (), {"session_id": "s", "emit": staticmethod(lambda e: None)})()
    )

    outcome = await route.run_turn(RouteInput(text="How are you?"), _context(calls, 1))
    await route.wait_for_detached()

    assert outcome.pending_task_id is not None
    assert agent.calls
    assert not any(
        call[0] == "audio.write" and call[2] == b"audio-after-tool-call"
        for call in calls
    )
    await route.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_work", [False, True])
async def test_ack_interruption_cannot_start_cancelled_work_but_preserves_barge_in(cancel_work):
    calls = []
    class BlockingAck(FakeRealtime):
        async def tell(self, material, *, turn_id):
            if material.reason == "ack":
                self.entered.set()
                await self.release.wait()
            async for item in super().tell(material, turn_id=turn_id):
                yield item
    provider = BlockingAck(calls)
    agent = FakeAgent()
    agent.wait = True
    route = HybridRoute(role_contract=HybridRoleContract("Sleeptalk"), agent=agent,
                        realtime=provider, audio_output=FakeOutput(calls))
    await route.prepare(type("Session", (), {"session_id": "s", "emit": staticmethod(lambda e: None)})())
    turn = asyncio.create_task(route.run_turn(RouteInput(text="Research this repository"), _context(calls, 1)))
    try:
        async with asyncio.timeout(2):
            await provider.entered.wait()
            task_id = route.tasks[0].task_id
            if cancel_work:
                assert await route.cancel_task(task_id.value)
            else:
                await route.interrupt_presentation(1, source=InterruptionSource.USER_SPEECH)
            outcome = await turn
            assert outcome.status == "cancelled"
            if cancel_work:
                assert not agent.calls
                assert route.reducer.task(task_id).phase is TaskPhase.CANCELLED
            else:
                await agent.started.wait()
                assert len(agent.calls) == 1
                assert route.reducer.task(task_id).phase is TaskPhase.RUNNING
    finally:
        turn.cancel()
        provider.release.set()
        agent.release.set()
        await asyncio.gather(turn, return_exceptions=True)
        await asyncio.wait_for(route.close(), 1)


@pytest.mark.asyncio
async def test_interrupt_snapshots_played_duration_before_output_discard_resets_accounting():
    calls = []
    class ResettingOutput(FakeOutput):
        async def discard_turn(self, turn_id, reason="interrupted"):
            await super().discard_turn(turn_id, reason)
            self._accounting.bytes_written = 0
    class Truncating(FakeRealtime):
        async def truncate_output(self, played_ms):
            self.calls.append(("truncate", played_ms))
    output = ResettingOutput(calls)
    route = HybridRoute(role_contract=HybridRoleContract("Sleeptalk"), agent=FakeAgent(),
                        realtime=Truncating(calls), audio_output=output)
    await route.prepare(type("Session", (), {"session_id": "s", "emit": staticmethod(lambda e: None)})())
    try:
        async with asyncio.timeout(2):
            route._set_presenting(1)
            await output.write(b"\0" * 9_600, 1)
            await route.interrupt_presentation(1)
            assert ("truncate", 100) in calls
    finally:
        await asyncio.wait_for(route.close(), 1)


@pytest.mark.asyncio
async def test_tool_dispatch_context_keeps_delivered_preamble_and_ack_has_task_identity():
    calls = []
    provider = ToolRealtime(calls)
    provider.outputs = [RealtimeTranscript("I will inspect the project."), RealtimeAudio(b"preamble"),
                        ToolCall("inspect", "Read README")]
    agent = FakeAgent()
    agent.wait = True
    route = HybridRoute(role_contract=HybridRoleContract("Sleeptalk"), agent=agent,
                        realtime=provider, audio_output=FakeOutput(calls))
    await route.prepare(type("Session", (), {"session_id": "s", "emit": staticmethod(lambda e: None)})())
    try:
        async with asyncio.timeout(2):
            await route.run_turn(RouteInput(text="How are you?"), _context(calls, 1))
            await agent.started.wait()
            assert "Sleeptalk: I will inspect the project." in agent.calls[0]
            assert "User: How are you?" in agent.calls[0]
            acknowledgements = [c[1] for c in calls if c[0] == "event"
                                and c[1].type is SessionEventType.TRANSCRIPT_FINAL
                                and c[1].payload.get("speechKind") == "ack"]
            assert len(acknowledgements) == 1
            assert acknowledgements[0].payload["taskId"] == route.tasks[0].task_id.value
            assert route.tasks[0].delivery is DeliveryPhase.PENDING
    finally:
        agent.release.set()
        await asyncio.wait_for(route.close(), 1)


@pytest.mark.asyncio
async def test_pending_status_is_dropped_if_origin_task_finishes_before_it_can_speak():
    agent = FakeAgent()
    agent.wait = True
    route, _, _, _, calls = await _route(agent=agent)
    try:
        async with asyncio.timeout(2):
            await route.run_turn(RouteInput(text="Research the latest release"), _context(calls, 1))
            await agent.started.wait()
            task_id = route.tasks[0].task_id
            async with route._presentation_lock:
                route._mouth.reassure("Still working on that.", task_id=task_id)
                agent.release.set()
                while route.reducer.task(task_id).phase is not TaskPhase.COMPLETED:
                    await asyncio.sleep(0)
            await route.wait_for_detached()
            assert not any(c[0] == "realtime.tell" and c[2] == "Still working on that." for c in calls)
            assert not any(event.kind == "task.transitioned" and dict(event.payload).get("phase") == "presenting"
                           for event in route.reducer.events)
    finally:
        agent.release.set()
        await asyncio.wait_for(route.close(), 1)


@pytest.mark.asyncio
async def test_stop_suppresses_results_before_waiting_for_provider_cancel_io():
    calls = []
    cancel_started = asyncio.Event()
    cancel_release = asyncio.Event()
    class SlowCancel(FakeRealtime):
        async def cancel_response(self, *, turn_id=None):
            cancel_started.set()
            await cancel_release.wait()
            await super().cancel_response(turn_id=turn_id)
    agent = FakeAgent()
    agent.wait = True
    route = HybridRoute(role_contract=HybridRoleContract("Sleeptalk"), agent=agent,
                        realtime=SlowCancel(calls), audio_output=FakeOutput(calls))
    await route.prepare(type("Session", (), {"session_id": "s", "emit": staticmethod(lambda e: None)})())
    interrupted = None
    try:
        async with asyncio.timeout(2):
            await route.run_turn(RouteInput(text="Research the latest release"), _context(calls, 1))
            await agent.started.wait()
            interrupted = asyncio.create_task(route.interrupt_presentation(1, source=InterruptionSource.CANCEL))
            await cancel_started.wait()
            assert route.tasks[0].delivery is DeliveryPhase.SUPPRESSED
            agent.release.set()
            await route.wait_for_detached()
            assert not any(c[0] == "realtime.tell" and c[2] == "Specialist answer." for c in calls)
            cancel_release.set()
            await interrupted
    finally:
        cancel_release.set()
        agent.release.set()
        if interrupted is not None:
            await asyncio.gather(interrupted, return_exceptions=True)
        await asyncio.wait_for(route.close(), 1)


@pytest.mark.asyncio
async def test_transcript_routed_busy_response_fails_when_it_has_no_audio():
    calls = []
    class SilentBusy(FakeRealtime):
        async def tell(self, material, *, turn_id):
            if material.text == HybridRoute.BUSY_SENTENCE:
                yield material.text
            else:
                async for item in super().tell(material, turn_id=turn_id):
                    yield item
    agent = FakeAgent()
    agent.wait = True
    route = HybridRoute(role_contract=HybridRoleContract("Sleeptalk"), agent=agent,
                        realtime=SilentBusy(calls), audio_output=FakeOutput(calls))
    await route.prepare(type("Session", (), {"session_id": "s", "emit": staticmethod(lambda e: None)})())
    try:
        async with asyncio.timeout(2):
            await route.run_turn(RouteInput(text="Research the latest release"), _context(calls, 1))
            await agent.started.wait()
            outcome = await route.run_turn(RouteInput(text="Research another project"), _context(calls, 2))
            assert outcome.status == "failed"
            assert not outcome.played
            assert "no audio" in outcome.error
            assert len(agent.calls) == 1
    finally:
        agent.release.set()
        await asyncio.wait_for(route.close(), 1)


@pytest.mark.asyncio
async def test_tool_resolution_failure_returns_a_failed_outcome_without_starting_work():
    calls = []
    class FailedResolve(ToolRealtime):
        async def resolve_specialist(self, call_id, output):
            raise RuntimeError("tool output socket failed")
    provider = FailedResolve(calls)
    provider.outputs = [ToolCall("one", "Read README")]
    agent = FakeAgent()
    route = HybridRoute(role_contract=HybridRoleContract("Sleeptalk"), agent=agent,
                        realtime=provider, audio_output=FakeOutput(calls))
    await route.prepare(type("Session", (), {"session_id": "s", "emit": staticmethod(lambda e: None)})())
    try:
        async with asyncio.timeout(2):
            outcome = await route.run_turn(RouteInput(text="How are you?"), _context(calls, 1))
            assert outcome.status == "failed"
            assert "tool output socket failed" in outcome.error
            assert not agent.calls
            assert route.tasks[0].phase is TaskPhase.CANCELLED
    finally:
        await asyncio.wait_for(route.close(), 1)


@pytest.mark.asyncio
async def test_failed_partial_script_retains_its_interrupted_transcript():
    calls = []
    class PartialFailure(FakeRealtime):
        async def tell(self, material, *, turn_id):
            yield "I am here."
            yield b"partial audio"
            raise RuntimeError("audio stream failed")
    route = HybridRoute(role_contract=HybridRoleContract("Sleeptalk"), agent=FakeAgent(),
                        realtime=PartialFailure(calls), audio_output=FakeOutput(calls))
    await route.prepare(type("Session", (), {"session_id": "s", "emit": staticmethod(lambda e: None)})())
    try:
        async with asyncio.timeout(2):
            outcome = await route.run_turn(RouteInput(text="Are you there?"), _context(calls, 1))
            assert outcome.status == "failed"
            delivered = [c[1] for c in calls if c[0] == "event"
                         and c[1].type is SessionEventType.TRANSCRIPT_FINAL
                         and c[1].payload.get("delivered")]
            assert len(delivered) == 1
            assert delivered[0].payload["text"] == "I am here."
            assert delivered[0].payload["interrupted"] is True
    finally:
        await asyncio.wait_for(route.close(), 1)


class AudioFirst(ToolRealtime):
    """Transcription lands only after the mouth's audio has been written,
    or before the reply starts when ``transcript_wins`` is set."""

    def __init__(self, calls, text, outputs, *, transcript_wins=False):
        super().__init__(calls)
        self.text = text
        self.replies = outputs
        self.transcript_wins = transcript_wins
        self.transcribed = asyncio.Event()

    async def commit_audio(self, route_input, *, turn_id):
        return "item-1"

    async def transcript_for(self, *, turn_id):
        if not self.transcript_wins:
            while not any(c[0] in {"audio.write", "resolve"} for c in self.calls):
                await asyncio.sleep(0)
        self.transcribed.set()
        return self.text

    async def respond(self, transcript, *, turn_id):
        if self.transcript_wins:
            await self.transcribed.wait()
            await asyncio.sleep(0)
        for item in self.replies:
            yield item


async def _audio_first_route(provider, calls, agent=None):
    agent = agent or FakeAgent()
    route = HybridRoute(role_contract=HybridRoleContract("Sleeptalk"), agent=agent,
                        realtime=provider, audio_output=FakeOutput(calls),
                        speculative_audio=True, audio_first=True)
    await route.prepare(type("Session", (), {"session_id": "s", "emit": staticmethod(lambda e: None)})())
    return route, agent


def _owner_reasons(calls):
    return [c[1].payload.get("reason") for c in calls if c[0] == "event"
            and c[1].payload.get("name") == "hybrid.intent.committed"]


@pytest.mark.asyncio
async def test_audio_first_plays_before_the_transcript_exists():
    calls = []
    provider = AudioFirst(calls, "How are you?",
                          [RealtimeAudio(b"mouth-audio"), RealtimeTranscript("Doing well.")])
    route, _ = await _audio_first_route(provider, calls)
    # Gating on the transcript would deadlock this fake, so time out instead.
    outcome = await asyncio.wait_for(
        route.run_turn(RouteInput(pcm=b"audio"), _context(calls, 1)), 2)
    assert outcome.status == "completed"
    assert outcome.heard == "How are you?"
    assert outcome.heard_ms is not None
    assert ("audio.write", 1, b"mouth-audio") in calls
    assert _owner_reasons(calls) == ["audio_first"]
    users = [c[1].payload["text"] for c in calls if c[0] == "event"
             and c[1].type is SessionEventType.TRANSCRIPT_FINAL
             and c[1].payload.get("speaker") == "user"]
    assert users == ["How are you?"]
    await route.close()


@pytest.mark.asyncio
async def test_audio_first_lets_realtime_route_work_through_its_tool():
    calls = []
    provider = AudioFirst(calls, "What changed in the README?",
                          [ToolCall("one", "Read README and summarize the changes")])
    route, agent = await _audio_first_route(provider, calls)
    outcome = await asyncio.wait_for(
        route.run_turn(RouteInput(pcm=b"audio"), _context(calls, 1)), 2)
    await route.wait_for_detached()
    assert outcome.pending_task_id is not None
    assert agent.calls and "Read README and summarize the changes" in agent.calls[0]
    await route.close()


@pytest.mark.asyncio
async def test_audio_first_keeps_the_gate_when_the_transcript_is_first():
    calls = []
    question = "Tell me what intelligence layer you're using, like large language model."
    provider = AudioFirst(calls, question, [RealtimeAudio(b"generic-mouth-audio")],
                          transcript_wins=True)
    route, agent = await _audio_first_route(
        provider, calls, FakeAgent(hands_catalog=HandsCatalog(open=True)))
    outcome = await route.run_turn(RouteInput(pcm=b"audio"), _context(calls, 1))
    await route.wait_for_detached()
    assert outcome.pending_task_id is not None
    assert agent.calls and agent.calls[-1].endswith(question)
    assert not any(c[0] == "audio.write" and c[2] == b"generic-mouth-audio" for c in calls)
    await route.close()


@pytest.mark.asyncio
async def test_audio_first_keeps_the_gate_while_a_task_runs():
    calls = []
    provider = AudioFirst(calls, "Cancel the task", [RealtimeAudio(b"mouth-audio")])
    agent = FakeAgent()
    agent.wait = True
    route, _ = await _audio_first_route(provider, calls, agent)
    await route.run_turn(RouteInput(text="Research the latest release"), _context(calls, 1))
    await asyncio.wait_for(agent.started.wait(), 2)
    # Words arrive at once; the reply's audio is ready too. The gate decides.
    provider.transcript_for = _immediate(provider.text)
    await route.run_turn(RouteInput(pcm=b"audio"), _context(calls, 2))
    assert not any(c[0] == "audio.write" and c[2] == b"mouth-audio" for c in calls)
    assert route.tasks[0].phase is TaskPhase.CANCELLED
    agent.release.set()
    await route.close()


def _immediate(text):
    async def transcript_for(*, turn_id):
        return text
    return transcript_for


class CancelledSpeculation(AudioFirst):
    """Like OpenAI: cancelling the task cancels the live response, which is
    the speculative reply to "Wait, stop. <request>" itself."""

    def __init__(self, calls, text):
        super().__init__(calls, text, [])
        self.cancelled = asyncio.Event()
        self.replies_asked = 0

    async def cancel_response(self, *, turn_id=None):
        self.calls.append(("realtime.cancel", turn_id))
        self.cancelled.set()

    async def respond(self, transcript, *, turn_id):
        self.replies_asked += 1
        if self.replies_asked == 1:
            await self.cancelled.wait()
            raise RuntimeError("OpenAI Realtime response ended with cancelled")
        yield RealtimeAudio(b"paris-audio")
        yield RealtimeTranscript("Paris.")


@pytest.mark.asyncio
async def test_wait_stop_with_a_question_during_a_task_is_answered_not_failed():
    """Live: "Wait, stop. What's the capital of France?" during a task failed in
    84 ms and left the call silent. The redirect's cancel killed the reply
    being prepared; a fresh one now answers."""
    calls = []
    provider = CancelledSpeculation(calls, "Wait, stop. What's the capital of France?")
    agent = FakeAgent()
    agent.wait = True
    route, _ = await _audio_first_route(provider, calls, agent)
    await route.run_turn(RouteInput(text="Research the latest release"), _context(calls, 1))
    await asyncio.wait_for(agent.started.wait(), 2)
    provider.transcript_for = _immediate(provider.text)
    outcome = await asyncio.wait_for(route.run_turn(RouteInput(pcm=b"audio"), _context(calls, 2)), 2)
    assert outcome.status == "completed", outcome.error
    assert ("audio.write", 2, b"paris-audio") in calls
    assert route.tasks[0].phase is TaskPhase.CANCELLED
    agent.release.set()
    await route.close()
