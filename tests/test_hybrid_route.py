from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sleeptalk.adapter import (
    AgentRunState,
    AnswerClarification,
    ClarificationRequested,
    ControlAccepted,
    PermissionDecision,
    PermissionRequested,
    ResolvePermission,
    SessionIdentified,
    StatusChanged,
    ToolEvent,
    ToolPhase,
)
from sleeptalk.routes.audio_output import AudioOutputAccounting
from sleeptalk.routes.base import RecoveryOutcome, RouteInput, TurnContext
from sleeptalk.routes.hybrid import HybridRoute, RealtimeAudio, RealtimeTranscript
from sleeptalk.routes.hybrid_mouth import (
    BriefSnapshot,
    CHECK_PREAMBLES,
    check_preamble,
    material_spoken_text,
)
from sleeptalk.routes.hybrid_types import (
    CommittedIntent,
    Hand,
    HandsCatalog,
    HybridRoleContract,
    IntentId,
    PresentationEpoch,
    SemanticOwner,
    SpecialistOwnershipReason,
    TaskId,
    TaskPhase,
    assign_owner,
)


class FakeOutput:
    def __init__(self, calls: list[Any]) -> None:
        self.calls = calls
        self._accounting = AudioOutputAccounting(turn_id=0)

    async def write(self, pcm: bytes, turn_id: int) -> None:
        self.calls.append(("audio.write", turn_id, pcm))
        self._accounting.generated(len(pcm))
        self._accounting.written(len(pcm))

    async def discard_turn(self, turn_id: int, reason: str = "interrupted") -> None:
        self.calls.append(("audio.discard", turn_id, reason))

    def accounting(self) -> AudioOutputAccounting:
        return self._accounting

    async def close(self) -> None:
        self.calls.append(("audio.close",))


class FakeRealtime:
    def __init__(self, calls: list[Any]) -> None:
        self.calls = calls
        self.outputs: list[Any] = [
            RealtimeTranscript("Native answer."),
            RealtimeAudio(b"native"),
        ]
        self.recover_result: bool = True
        self.block_text: str | None = None
        self.brief_text_only = False
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.active = 0
        self.max_active = 0

    async def prepare(self, *, session_id: str, tools: tuple[()] = ()) -> None:
        self.calls.append(("realtime.prepare", session_id, tools))

    async def append_audio(self, pcm: bytes, *, participant_id: int = 0) -> None:
        self.calls.append(("realtime.append", participant_id, pcm))

    async def commit(self, route_input: RouteInput, *, turn_id: int) -> str:
        self.calls.append(("realtime.commit", turn_id))
        return "transcribed speech"

    def respond(self, transcript: str, *, turn_id: int) -> AsyncIterator[Any]:
        async def stream() -> AsyncIterator[Any]:
            self.calls.append(("realtime.respond", turn_id, transcript))
            for item in self.outputs:
                yield item

        return stream()

    async def discard_committed_input(self, *, turn_id: int) -> None:
        self.calls.append(("realtime.discard", turn_id))

    async def cancel_response(self, *, turn_id: int | None = None) -> None:
        self.calls.append(("realtime.cancel", turn_id))
        self.release.set()

    async def recover(self, *, session_id: str) -> bool:
        self.calls.append(("realtime.recover", session_id))
        return self.recover_result

    def tell(self, material: Any, *, turn_id: int) -> AsyncIterator[Any]:
        async def stream() -> AsyncIterator[Any]:
            text = material_spoken_text(material)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.calls.append(("realtime.tell", turn_id, text))
            try:
                if text == self.block_text:
                    self.entered.set()
                    await self.release.wait()
                if self.brief_text_only and isinstance(material, BriefSnapshot):
                    yield text
                else:
                    yield text
                    yield text.encode()
            finally:
                self.active -= 1

        return stream()

    async def close(self) -> None:
        self.calls.append(("realtime.close",))


class FakeAgent:
    def __init__(
        self,
        *,
        hands_catalog: HandsCatalog | None = None,
        hand_names: tuple[str, ...] = (),
    ) -> None:
        self.calls: list[str] = []
        self.abort_calls = 0
        self.controls: list[Any] = []
        self.events: list[Any] = []
        self.sentences: list[str] = ["Specialist answer."]
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.wait = False
        self.failure: Exception | None = None
        self.pause_after_first = False
        self.after_first = asyncio.Event()
        self.release_after_first = asyncio.Event()
        self.hold_after_sentences = False
        self.after_sentences = asyncio.Event()
        self.release_after_sentences = asyncio.Event()
        self.on_event: Any = None
        if hands_catalog is not None:
            self.hands_catalog = hands_catalog
        self.hand_names = hand_names

    async def warmup(self) -> None:
        return None

    def respond_streaming(self, text: str, *, on_event=None) -> AsyncIterator[str]:
        async def stream() -> AsyncIterator[str]:
            self.calls.append(text)
            self.started.set()
            self.on_event = on_event
            for event in self.events:
                on_event(event)
            if self.wait:
                await self.release.wait()
            for index, sentence in enumerate(self.sentences):
                yield sentence
                if index == 0 and self.pause_after_first:
                    self.after_first.set()
                    await self.release_after_first.wait()
            if self.hold_after_sentences:
                self.after_sentences.set()
                await self.release_after_sentences.wait()
            if self.failure is not None:
                raise self.failure

        return stream()

    async def control(self, command: Any) -> ControlAccepted:
        self.controls.append(command)
        return ControlAccepted()

    async def abort(self) -> None:
        self.abort_calls += 1
        self.release.set()

    async def close(self) -> None:
        return None


def _context(calls: list[Any], turn_id: int) -> TurnContext:
    def emit(event: Any) -> None:
        calls.append(("event", event))

    return TurnContext(session_id="session-1", turn_id=turn_id, emit=emit)


async def _route(
    *,
    agent: FakeAgent | None = None,
) -> tuple[HybridRoute, FakeAgent, FakeRealtime, FakeRealtime, list[Any]]:
    calls: list[Any] = []
    selected_agent = agent or FakeAgent()
    realtime = FakeRealtime(calls)
    route = HybridRoute(
        role_contract=HybridRoleContract("Codex"),
        agent=selected_agent,
        realtime=realtime,
        audio_output=FakeOutput(calls),
    )
    await route.prepare(SimpleNamespace(session_id="session-1", emit=lambda event: None))
    return route, selected_agent, realtime, realtime, calls


@pytest.mark.asyncio
async def test_owner_is_committed_before_native_response_and_provider_has_no_tools() -> None:
    route, _agent, _realtime, _speech, calls = await _route()
    outcome = await route.run_turn(RouteInput(text="How are you?"), _context(calls, 1))

    committed_at = next(
        index
        for index, call in enumerate(calls)
        if call[0] == "event"
        and call[1].payload.get("name") == "hybrid.intent.committed"
    )
    native_at = next(index for index, call in enumerate(calls) if call[0] == "realtime.respond")
    assert committed_at < native_at
    assert calls[0] == ("realtime.prepare", "session-1", ())
    assert route.reducer.events[0].owner is SemanticOwner.REALTIME
    assert outcome.status == "completed"
    assert outcome.generated == ["Native answer."]


@pytest.mark.asyncio
async def test_assistant_state_emits_reason_before_fixed_specialist_acknowledgment() -> None:
    route, agent, _realtime, _speech, calls = await _route()
    agent.wait = True

    outcome = await route.run_turn(
        RouteInput(text="Do you have any memory of me?"), _context(calls, 1)
    )
    await agent.started.wait()

    committed_at = next(
        index
        for index, call in enumerate(calls)
        if call[0] == "event"
        and call[1].payload.get("name") == "hybrid.intent.committed"
    )
    speech_at = next(
        index for index, call in enumerate(calls) if call[0] == "realtime.tell"
    )
    committed = calls[committed_at][1]
    assert committed.payload["owner"] == "specialist"
    assert (
        committed.payload["reason"]
        == SpecialistOwnershipReason.ASSISTANT_MEMORY.value
    )
    assert committed_at < speech_at
    assert outcome.generated == [
        "I'll check what the connected agent remembers about you."
    ]
    agent.release.set()
    await route.wait_for_detached()


@pytest.mark.asyncio
async def test_capability_question_speaks_the_agent_hands() -> None:
    catalog = HandsCatalog(
        hands=(Hand.named("web_search", "tool", "web", "browser", "search"),),
    )
    route, agent, _realtime, _speech, calls = await _route(
        agent=FakeAgent(hands_catalog=catalog)
    )

    outcome = await route.run_turn(
        RouteInput(text="What tools do you have?"), _context(calls, 1)
    )

    assert outcome.generated == [route.role_contract.hands_answer(catalog)]
    assert agent.calls == []
    assert not any(call[0] == "realtime.respond" for call in calls)


@pytest.mark.asyncio
async def test_web_work_does_not_ack_as_a_capability_inventory() -> None:
    agent = FakeAgent(
        hands_catalog=HandsCatalog(
            hands=(Hand.named("web_search", "tool", "web", "browser", "search"),),
        )
    )
    agent.wait = True
    route, agent, _realtime, _speech, calls = await _route(agent=agent)

    outcome = await route.run_turn(
        RouteInput(text="can you search the web to see market news"),
        _context(calls, 1),
    )
    await agent.started.wait()

    committed = next(
        call[1]
        for call in calls
        if call[0] == "event"
        and call[1].payload.get("name") == "hybrid.intent.committed"
    )
    assert committed.payload["owner"] == "specialist"
    assert committed.payload["reason"] == SpecialistOwnershipReason.HANDS.value
    assert outcome.generated == [check_preamble(TaskId(committed.payload["taskId"]))]
    assert agent.calls == ["can you search the web to see market news"]
    agent.release.set()
    await route.wait_for_detached()


@pytest.mark.asyncio
async def test_route_answers_narrow_topology_from_immutable_contract() -> None:
    route, agent, _realtime, _speech, calls = await _route()

    outcome = await route.run_turn(RouteInput(text="Who are you?"), _context(calls, 1))

    assert outcome.generated == [route.role_contract.topology_answer]
    assert agent.calls == []
    assert not any(call[0] == "realtime.respond" for call in calls)


@pytest.mark.asyncio
async def test_specialist_is_detached_and_tells_through_realtime() -> None:
    agent = FakeAgent()
    agent.events = [SessionIdentified("agent-session-7", resumable=True)]
    route, _agent, _realtime, _speech, calls = await _route(agent=agent)

    outcome = await route.run_turn(
        RouteInput(text="Research the latest database release"), _context(calls, 1)
    )
    await route.wait_for_detached()

    task_id = route.reducer.events[0].task_id
    assert outcome.status == "completed"
    assert task_id is not None
    assert route.reducer.task(task_id).agent_session_ref == "agent-session-7"
    assert route.reducer.task(task_id).phase is TaskPhase.COMPLETED
    assert any(call[0] == "realtime.tell" and call[2] in CHECK_PREAMBLES for call in calls)
    assert any(
        (call[0] == "realtime.tell" and call[2] == "Specialist answer.")
        or (call[0] == "realtime.respond" and "Specialist answer." in str(call))
        for call in calls
    )
    notes = [
        call[1]
        for call in calls
        if call[0] == "event"
        and call[1].type.value == "transcript.final"
        and call[1].payload.get("owner") == "specialist"
    ]
    spoken = [
        call[1]
        for call in calls
        if call[0] == "event"
        and call[1].type.value == "transcript.final"
        and call[1].payload.get("owner") == "realtime"
    ]
    assert not notes, "specialist findings must not masquerade as spoken conversation"
    assert any(event.payload["text"] == "Specialist answer." for event in spoken)


@pytest.mark.asyncio
async def test_specialist_facts_are_held_until_the_task_completes() -> None:
    agent = FakeAgent()
    agent.sentences = ["First update.", "Debra is still pulling."]
    agent.hold_after_sentences = True
    route, _agent, _realtime, _speech, calls = await _route(agent=agent)
    await route.run_turn(RouteInput(text="Research current prices"), _context(calls, 1))
    await agent.after_sentences.wait()
    await route._mouth.wait()

    assert route.tasks[0].phase is TaskPhase.RUNNING
    told = [call[2] for call in calls if call[0] == "realtime.tell"]
    assert not any("First update." in text for text in told)
    assert not any("Debra is still pulling." in text for text in told)
    agent.release_after_sentences.set()
    await route.wait_for_detached()

    told = [call[2] for call in calls if call[0] == "realtime.tell"]
    assert any("First update." in text and "Debra is still pulling." in text for text in told)


@pytest.mark.asyncio
async def test_tell_me_more_after_brief_stays_realtime_and_does_not_ack_on_it() -> None:
    agent = FakeAgent()
    agent.sentences = ["Specialist answer."]
    route, agent, _realtime, _speech, calls = await _route(agent=agent)

    first = await route.run_turn(
        RouteInput(text="Research current prices"), _context(calls, 1)
    )
    await route.wait_for_detached()

    assert first.generated[0] in CHECK_PREAMBLES
    assert agent.calls == ["Research current prices"]

    follow = await route.run_turn(RouteInput(text="tell me more"), _context(calls, 2))

    committed = [
        call[1]
        for call in calls
        if call[0] == "event"
        and call[1].payload.get("name") == "hybrid.intent.committed"
    ][-1]
    assert committed.payload["owner"] == "realtime"
    assert agent.calls == ["Research current prices"]
    assert follow.generated == ["Native answer."]
    assert any(call == ("realtime.respond", 2, "tell me more") for call in calls)
    assert not any(
        call[0] == "realtime.tell" and call[1] == 2 and call[2] in CHECK_PREAMBLES
        for call in calls
    )


@pytest.mark.asyncio
async def test_silent_realtime_text_is_told_through_the_mouth() -> None:
    route, _agent, realtime, _speech, calls = await _route(
        agent=FakeAgent(hands_catalog=HandsCatalog())
    )
    realtime.outputs = [
        "Ah, got it. Thanks for telling me. I cannot open Safari from here."
    ]
    outcome = await route.run_turn(
        RouteInput(text="It didn't open up in Safari."), _context(calls, 1)
    )

    spoken = "Ah, got it. Thanks for telling me. I cannot open Safari from here."
    assert outcome.generated == [spoken]
    assert any(call[0] == "realtime.tell" and call[2] == spoken for call in calls)
    assert ("audio.write", 1, spoken.encode()) in calls


@pytest.mark.asyncio
async def test_silent_specialist_tell_is_spoken_as_a_script() -> None:
    agent = FakeAgent()
    agent.sentences = ["The agent remembers you like apples."]
    route, _agent, realtime, _speech, calls = await _route(agent=agent)
    realtime.brief_text_only = True
    await route.run_turn(RouteInput(text="Research current prices"), _context(calls, 1))
    await route.wait_for_detached()
    await route._mouth.wait()

    spoken = "The agent remembers you like apples."
    assert any(call[0] == "realtime.tell" and call[2] == spoken for call in calls)
    assert ("audio.write", 1, spoken.encode()) in calls


@pytest.mark.asyncio
async def test_only_one_top_level_specialist_task_runs_and_busy_is_honest() -> None:
    agent = FakeAgent()
    agent.wait = True
    route, _agent, _realtime, _speech, calls = await _route(agent=agent)
    await route.run_turn(RouteInput(text="Research current prices"), _context(calls, 1))
    await agent.started.wait()

    second = await route.run_turn(
        RouteInput(text="Debug this repository too"), _context(calls, 2)
    )

    assert second.status == "completed"
    assert len(agent.calls) == 1
    assert any(
        call[0] == "realtime.tell" and call[2] == HybridRoute.BUSY_SENTENCE for call in calls
    )
    failed = [task for task in route.tasks if task.phase is TaskPhase.FAILED]
    assert len(failed) == 1
    agent.release.set()
    await route.wait_for_detached()


@pytest.mark.asyncio
async def test_barge_in_revokes_audio_but_preserves_specialist_work_and_text() -> None:
    route, agent, _realtime, speech, calls = await _route()
    speech.block_text = "Specialist answer."
    await route.run_turn(RouteInput(text="Research current prices"), _context(calls, 1))
    await speech.entered.wait()

    await route.interrupt_presentation(1)
    await route.wait_for_detached()

    task = route.tasks[0]
    assert agent.abort_calls == 0
    assert task.phase is TaskPhase.COMPLETED
    assert task.result == ("Specialist answer.",)
    assert any(call[0] == "realtime.cancel" for call in calls)
    assert any(call[:2] == ("audio.discard", 1) for call in calls)


class LateBargeRealtime(FakeRealtime):
    """Barges in a beat after the answer stops, not over it."""

    def __init__(self, calls: list[Any]) -> None:
        super().__init__(calls)
        self.route: Any = None

    def tell(self, material: Any, *, turn_id: int) -> AsyncIterator[Any]:
        inner = super().tell(material, turn_id=turn_id)

        async def stream() -> AsyncIterator[Any]:
            async for item in inner:
                yield item
            if isinstance(material, BriefSnapshot) and self.route is not None:
                await self.route.interrupt_presentation(turn_id)

        return stream()


@pytest.mark.asyncio
async def test_a_barge_after_the_last_frame_does_not_replay_a_finished_answer() -> None:
    """The answer had already been heard in full.

    Measured on web: the user starts their next question a beat after the
    answer stops. That barge revoked nothing, but the presentation was
    still recorded as interrupted, so it was never marked told and the next
    turn said the whole thing over again — reworded, because the wording is
    regenerated from the same findings.
    """
    calls: list[Any] = []
    realtime = LateBargeRealtime(calls)
    route = HybridRoute(
        role_contract=HybridRoleContract("Codex"),
        agent=FakeAgent(),
        realtime=realtime,
        audio_output=FakeOutput(calls),
    )
    await route.prepare(SimpleNamespace(session_id="session-1", emit=lambda event: None))
    realtime.route = route

    await route.run_turn(RouteInput(text="Research current prices"), _context(calls, 1))
    await route.wait_for_detached()

    answers = [
        call[1]
        for call in calls
        if call[0] == "event" and call[1].payload.get("speechKind") == "answer"
    ]
    assert answers and all(event.payload["status"] == "delivered" for event in answers)

    await route.run_turn(RouteInput(text="Tell me a short joke"), _context(calls, 2))
    told = [call for call in calls if call[0] == "realtime.tell" and call[1] == 2]
    assert not any(call[2] == "Specialist answer." for call in told)


@pytest.mark.parametrize("when", ["during_ack", "while_working"])
@pytest.mark.asyncio
async def test_a_barge_nobody_follows_up_still_owes_the_answer(when: str) -> None:
    """On speakers the acknowledgement's own audio trips the barge detector.

    `pause` mutes the mouth until the next turn's `resume`, on the
    assumption that a barge is someone about to speak. Nothing follows a
    self-interrupt, so the mute never lifted: the user heard "I'll look
    that up" and then nothing, for the rest of the session. Reported on
    terminal speakers, and reproduced against the code as it stood before
    any of the hold work.
    """
    agent = FakeAgent()
    agent.wait = True
    route, _agent, _realtime, _speech, calls = await _route(agent=agent)
    turn = asyncio.create_task(
        route.run_turn(RouteInput(text="Tell me the weather in New York"), _context(calls, 1))
    )
    if when == "during_ack":
        await asyncio.sleep(0)
        await route.interrupt_presentation(1)
        await turn
    else:
        await turn
        await agent.started.wait()
        await route.interrupt_presentation(1)
    agent.release.set()
    await route.wait_for_detached()

    told = [call[2] for call in calls if call[0] == "realtime.tell"]
    assert "Specialist answer." in told, told


@pytest.mark.asyncio
async def test_barged_answer_is_held_back_from_the_next_question() -> None:
    """Talking over an answer means you have moved on.

    Reproduced on web, Discord and terminal: barge in during "I'll look
    that up", ask about something else, and the abandoned question got
    answered on top of the new one. Being talked over is not the same
    instruction as pressing Stop, so the findings survive — but nothing
    volunteers them into a question they do not answer.
    """
    route, _agent, _realtime, speech, calls = await _route()
    speech.block_text = "Specialist answer."
    await route.run_turn(RouteInput(text="Research current prices"), _context(calls, 1))
    await speech.entered.wait()

    await route.interrupt_presentation(1)
    await route.wait_for_detached()
    await route.run_turn(RouteInput(text="Tell me a short joke"), _context(calls, 2))

    assert ("audio.write", 2, b"Specialist answer.") not in calls
    assert not any(event.kind == "presentation.resumed" for event in route.reducer.events)
    assert any(event.kind == "presentation.held" for event in route.reducer.events)

    await route.run_turn(RouteInput(text="Repeat that"), _context(calls, 3))
    assert ("audio.write", 3, b"Specialist answer.") in calls


@pytest.mark.asyncio
async def test_deliberate_cancel_does_not_replay_into_the_next_question() -> None:
    """Stop has to mean stop.

    Talking over an answer leaves the specialist's work to be resumed later,
    which is the point of resume_after_interrupt. Pressing Stop is a
    different instruction, and answering the previous question during the
    next one reads as the assistant ignoring you.
    """
    from sleeptalk.session_events import InterruptionSource

    route, _agent, _realtime, speech, calls = await _route()
    speech.block_text = "Specialist answer."
    await route.run_turn(RouteInput(text="Research current prices"), _context(calls, 1))
    await speech.entered.wait()

    await route.interrupt_presentation(1, source=InterruptionSource.CANCEL)
    await route.wait_for_detached()
    await route.run_turn(RouteInput(text="Tell me a short joke"), _context(calls, 2))

    told = sum(
        call[0] == "realtime.tell" and call[2] == "Specialist answer." for call in calls
    )
    assert told <= 1, "the cancelled answer was delivered during the next turn"
    assert not any(event.kind == "presentation.resumed" for event in route.reducer.events)
    assert any(event.kind == "presentation.withheld" for event in route.reducer.events)


@pytest.mark.asyncio
async def test_repeat_after_interrupt_tells_leftover_facts_once() -> None:
    route, _agent, _realtime, speech, calls = await _route()
    speech.block_text = "Specialist answer."
    await route.run_turn(RouteInput(text="Research current prices"), _context(calls, 1))
    await speech.entered.wait()

    await route.interrupt_presentation(1)
    await route.wait_for_detached()
    await route.run_turn(RouteInput(text="Repeat that"), _context(calls, 2))

    assert (
        sum(call[0] == "realtime.tell" and call[2] == "Specialist answer." for call in calls)
        == 2
    )
    assert not any(call[0] == "realtime.respond" for call in calls)


@pytest.mark.asyncio
async def test_explicit_cancel_uses_targeted_control_once_and_records_late_events() -> None:
    agent = FakeAgent()
    agent.wait = True
    agent.sentences = ["Late secret result."]
    route, _agent, _realtime, _speech, calls = await _route(agent=agent)
    await route.run_turn(RouteInput(text="Research current prices"), _context(calls, 1))
    await agent.started.wait()
    task_id = route.active_task_id
    assert task_id is not None

    assert await route.cancel_task(task_id.value) is True
    assert await route.cancel_task(task_id.value) is False
    await route.wait_for_detached()
    assert agent.on_event is not None
    agent.on_event(SessionIdentified("late-session", resumable=True))

    task = route.reducer.task(task_id)
    assert agent.abort_calls == 0
    assert task.phase is TaskPhase.CANCELLED
    assert not any(
        call[0] == "realtime.tell" and call[2] == "Late secret result." for call in calls
    )
    assert any(event.kind == "task.late_event" for event in route.reducer.events)


@pytest.mark.asyncio
async def test_cancel_falls_back_to_abort_when_targeted_control_raises() -> None:
    agent = FakeAgent()
    agent.wait = True

    async def broken_control(command: Any) -> None:
        agent.controls.append(command)
        raise RuntimeError("control transport failed")

    agent.control = broken_control  # type: ignore[method-assign]
    route, _agent, _realtime, _speech, calls = await _route(agent=agent)
    await route.run_turn(RouteInput(text="Research current prices"), _context(calls, 1))
    await agent.started.wait()
    task_id = route.active_task_id
    assert task_id is not None

    assert await route.cancel_task(task_id.value) is True
    await route.wait_for_detached()

    assert agent.abort_calls == 1
    assert route.reducer.task(task_id).phase is TaskPhase.CANCELLED
    assert any(
        call[0] == "event" and call[1].payload.get("code") == "control"
        for call in calls
    )


@pytest.mark.asyncio
async def test_completed_adapter_status_does_not_drop_buffered_tail() -> None:
    agent = FakeAgent()
    agent.events = [StatusChanged(AgentRunState.COMPLETED)]
    route, _agent, _realtime, _speech, calls = await _route(agent=agent)

    await route.run_turn(RouteInput(text="Research current prices"), _context(calls, 1))
    await route.wait_for_detached()

    assert any(call[0] == "realtime.tell" and call[2] == "Specialist answer." for call in calls)
    assert route.tasks[0].phase is TaskPhase.COMPLETED
    assert any(
        call[0] == "event" and call[1].type.value == "response.completed"
        for call in calls
    )


@pytest.mark.asyncio
async def test_specialist_output_rebinds_to_the_latest_floor_turn() -> None:
    agent = FakeAgent()
    agent.sentences = ["First sentence.", "Second sentence."]
    agent.pause_after_first = True
    route, _agent, _realtime, _speech, calls = await _route(agent=agent)
    await route.run_turn(RouteInput(text="Research current prices"), _context(calls, 1))
    await agent.after_first.wait()

    await route.run_turn(RouteInput(text="Tell me a short joke"), _context(calls, 2))
    agent.release_after_first.set()
    await route.wait_for_detached()

    assert ("audio.write", 2, b"First sentence. Second sentence.") in calls


@pytest.mark.asyncio
async def test_cancelled_task_cannot_replay_deferred_audio() -> None:
    agent = FakeAgent()
    agent.sentences = ["Deferred sentence.", "Never spoken."]
    agent.hold_after_sentences = True
    route, _agent, _realtime, _speech, calls = await _route(agent=agent)
    await route.run_turn(RouteInput(text="Research current prices"), _context(calls, 1))
    await agent.after_sentences.wait()
    task_id = route.active_task_id
    assert task_id is not None

    assert await route.cancel_task(task_id.value) is True
    await route.run_turn(RouteInput(text="Tell me a short joke"), _context(calls, 2))

    assert not any(
        call[0] == "realtime.tell"
        and "Deferred sentence." in str(call[2])
        for call in calls
    )


@pytest.mark.asyncio
async def test_cancel_task_stops_active_specialist_speech() -> None:
    agent = FakeAgent()
    agent.sentences = ["Specialist answer.", "More."]
    agent.hold_after_sentences = True
    route, _agent, _realtime, _speech, calls = await _route(agent=agent)
    await route.run_turn(RouteInput(text="Research current prices"), _context(calls, 1))
    await agent.after_sentences.wait()
    task_id = route.active_task_id
    assert task_id is not None

    assert await route.cancel_task(task_id.value) is True
    await route.wait_for_detached()

    assert not any(
        call[0] == "realtime.tell" and "Specialist answer." in str(call[2])
        for call in calls
    )
    assert any(call[0] == "realtime.cancel" for call in calls)


@pytest.mark.asyncio
async def test_discard_failure_terminalizes_committed_specialist_task() -> None:
    route, _agent, realtime, _speech, calls = await _route()

    async def commit_specialist(route_input: RouteInput, *, turn_id: int) -> str:
        return "research current prices"

    async def fail_discard(*, turn_id: int) -> None:
        raise RuntimeError(f"delete failed for {turn_id}")

    realtime.commit = commit_specialist  # type: ignore[method-assign]
    realtime.discard_committed_input = fail_discard  # type: ignore[method-assign]
    outcome = await route.run_turn(
        RouteInput(pcm=b"\x00\x00"), _context(calls, 1)
    )

    assert outcome.status == "failed"
    assert route.active_task_id is None
    assert route.tasks[0].phase is TaskPhase.FAILED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("adapter_request", "answer", "command_type"),
    [
        (
            PermissionRequested(
                request_id="permission-1",
                prompt="May I write the file?",
                options=frozenset({PermissionDecision.ALLOW_ONCE, PermissionDecision.DENY}),
            ),
            "yes",
            ResolvePermission,
        ),
        (
            ClarificationRequested(
                request_id="clarify-1",
                prompt="Which package?",
                choices=("api", "web"),
            ),
            "the api package",
            AnswerClarification,
        ),
    ],
)
async def test_permission_and_clarification_replies_are_local_controls(
    adapter_request: Any, answer: str, command_type: type
) -> None:
    agent = FakeAgent()
    agent.events = [adapter_request]
    agent.wait = True
    route, _agent, _realtime, _speech, calls = await _route(agent=agent)
    await route.run_turn(RouteInput(text="Edit the repository"), _context(calls, 1))
    await agent.started.wait()
    await asyncio.sleep(0)

    outcome = await route.run_turn(RouteInput(text=answer), _context(calls, 2))

    assert outcome.status == "completed"
    assert isinstance(agent.controls[0], command_type)
    assert route.reducer.events[-1].owner is SemanticOwner.LOCAL or any(
        event.owner is SemanticOwner.LOCAL for event in route.reducer.events
    )
    assert not any(call[0] == "realtime.respond" for call in calls)
    assert any(
        call[0] == "realtime.tell" and call[2] == adapter_request.prompt for call in calls
    )
    agent.release.set()
    await route.wait_for_detached()


@pytest.mark.asyncio
async def test_specialist_failure_keeps_streamed_text_and_has_typed_error() -> None:
    agent = FakeAgent()
    agent.sentences = ["Useful partial result."]
    agent.failure = RuntimeError("worker disconnected")
    route, _agent, _realtime, _speech, calls = await _route(agent=agent)
    await route.run_turn(RouteInput(text="Research current prices"), _context(calls, 1))
    await route.wait_for_detached()

    task = route.tasks[0]
    assert task.phase is TaskPhase.FAILED
    assert task.result == ("Useful partial result.",)
    assert task.error.startswith("specialist:")
    assert any(
        call[0] == "event" and call[1].payload.get("code") == "specialist"
        for call in calls
    )
    assert not any(
        call[0] == "realtime.tell" and call[2] == "Useful partial result." for call in calls
    )
    assert any(
        call[0] == "realtime.tell"
        and call[2] == HybridRoute.SPECIALIST_FAILED_SENTENCE
        for call in calls
    )
    assert route._brief is not None
    assert "Useful partial result." in route._brief.findings


@pytest.mark.asyncio
async def test_specialist_buffer_error_tells_the_user() -> None:
    agent = FakeAgent()
    agent.sentences = ["I've read all the code."]
    agent.failure = RuntimeError(
        "Failed to decode JSON: JSON message exceeded maximum buffer size of 1048576 bytes"
    )
    route, _agent, _realtime, _speech, calls = await _route(agent=agent)
    await route.run_turn(RouteInput(text="Research current prices"), _context(calls, 1))
    await route.wait_for_detached()

    assert any(
        call[0] == "realtime.tell"
        and call[2] == HybridRoute.SPECIALIST_IMAGE_FAILED_SENTENCE
        for call in calls
    )


@pytest.mark.asyncio
async def test_specialist_timeout_tells_the_timeout_line() -> None:
    agent = FakeAgent()
    agent.sentences = ["Useful partial result."]
    agent.failure = RuntimeError("Claude Code response exceeded maximum time.")
    route, _agent, _realtime, _speech, calls = await _route(agent=agent)
    await route.run_turn(RouteInput(text="Research current prices"), _context(calls, 1))
    await route.wait_for_detached()

    told = [call[2] for call in calls if call[0] == "realtime.tell"]
    assert HybridRoute.SPECIALIST_TIMEOUT_SENTENCE in told
    assert HybridRoute.SPECIALIST_FAILED_SENTENCE not in told


@pytest.mark.asyncio
async def test_keep_going_after_failure_answers_from_the_brief() -> None:
    agent = FakeAgent()
    agent.sentences = ["The desktop already shows several issues."]
    agent.failure = RuntimeError("Claude Code response exceeded maximum time.")
    route, _agent, _realtime, _speech, calls = await _route(agent=agent)
    await route.run_turn(RouteInput(text="Research current prices"), _context(calls, 1))
    await route.wait_for_detached()

    follow = await route.run_turn(
        RouteInput(text="Yeah, keep going. What happened?"), _context(calls, 2)
    )

    committed = [
        call[1]
        for call in calls
        if call[0] == "event"
        and call[1].payload.get("name") == "hybrid.intent.committed"
    ][-1]
    assert committed.payload["owner"] == "realtime"
    assert agent.calls == ["Research current prices"]
    assert follow.generated == ["Native answer."]
    assert any(call == ("realtime.respond", 2, "Yeah, keep going. What happened?") for call in calls)


@pytest.mark.asyncio
async def test_tool_status_is_told_while_findings_wait() -> None:
    agent = FakeAgent()
    agent.events = [
        ToolEvent(name="read", phase=ToolPhase.STARTED, detail="reading playwright.md")
    ]
    agent.sentences = ["The desktop already shows several issues."]
    agent.hold_after_sentences = True
    route, _agent, _realtime, _speech, calls = await _route(agent=agent)
    await route.run_turn(RouteInput(text="Research current prices"), _context(calls, 1))
    await agent.after_sentences.wait()
    await route._mouth.wait()

    told = [call[2] for call in calls if call[0] == "realtime.tell"]
    assert any("playwright" in text.lower() for text in told)
    assert not any("desktop already" in text for text in told)
    agent.release_after_sentences.set()
    await route.wait_for_detached()


@pytest.mark.asyncio
async def test_hybrid_tool_narration_can_be_silent() -> None:
    agent = FakeAgent()
    agent.events = [ToolEvent(name="read", phase=ToolPhase.STARTED, detail="reading notes")]
    agent.sentences = ["Done."]
    agent.hold_after_sentences = True
    calls: list[Any] = []
    route = HybridRoute(
        role_contract=HybridRoleContract("Codex"),
        agent=agent,
        realtime=FakeRealtime(calls),
        audio_output=FakeOutput(calls),
        narration_style="silence",
    )
    await route.prepare(SimpleNamespace(session_id="s", emit=lambda event: None))
    await route.run_turn(RouteInput(text="Research current prices"), _context(calls, 1))
    await agent.after_sentences.wait()
    await route._mouth.wait()
    agent.release_after_sentences.set()
    await route.wait_for_detached()

    told = [call[2] for call in calls if call[0] == "realtime.tell"]
    assert not any("notes" in text.lower() for text in told)


@pytest.mark.asyncio
async def test_hybrid_tool_narration_can_play_the_keyboard_clip(monkeypatch) -> None:
    monkeypatch.setattr(
        "sleeptalk.routes.hybrid_mouth.narration_clip_path",
        lambda _style: Path("keyboard.wav"),
    )

    class ClipOutput(FakeOutput):
        async def play_file(self, path, *, turn_id):
            self.calls.append(("audio.play_file", turn_id, path.name))
            return True

    agent = FakeAgent()
    agent.events = [ToolEvent(name="read", phase=ToolPhase.STARTED, detail="reading notes")]
    agent.sentences = ["Done."]
    agent.hold_after_sentences = True
    calls: list[Any] = []
    route = HybridRoute(
        role_contract=HybridRoleContract("Codex"),
        agent=agent,
        realtime=FakeRealtime(calls),
        audio_output=ClipOutput(calls),
        narration_style="keyboard",
    )
    await route.prepare(SimpleNamespace(session_id="s", emit=lambda event: None))
    await route.run_turn(RouteInput(text="Research current prices"), _context(calls, 1))
    await agent.after_sentences.wait()
    await route._mouth.wait()
    agent.release_after_sentences.set()
    await route.wait_for_detached()

    assert ("audio.play_file", 1, "keyboard.wav") in calls
    told = [call[2] for call in calls if call[0] == "realtime.tell"]
    assert not any("notes" in text.lower() for text in told)


@pytest.mark.asyncio
async def test_recovery_keeps_the_canonical_reducer() -> None:
    route, _agent, _realtime, _speech, _calls = await _route()
    reducer = route.reducer

    result = await route.recover()

    assert result.outcome is RecoveryOutcome.RESTORED
    assert result.session_id == "session-1"
    assert route.reducer is reducer


@pytest.mark.asyncio
async def test_keepalive_speaks_while_a_silent_specialist_works() -> None:
    """A working agent that reports nothing must not sound like a dead one.

    Measured live: 49 seconds of silence after "I'll look that up", ended by
    the user interrupting to check whether it was still alive. Cascade
    narrates on a timer; hybrid only spoke when an adapter reported a tool.
    """
    agent = FakeAgent()
    agent.wait = True  # blocks without emitting any tool event
    calls: list[Any] = []
    realtime = FakeRealtime(calls)
    route = HybridRoute(
        role_contract=HybridRoleContract("Codex"),
        agent=agent,
        realtime=realtime,
        audio_output=FakeOutput(calls),
        keepalive_interval_s=0.05,
    )
    await route.prepare(
        SimpleNamespace(session_id="session-1", emit=lambda event: None)
    )
    try:
        await route.run_turn(
            RouteInput(text="Research current prices"), _context(calls, 1)
        )
        await agent.started.wait()
        await asyncio.sleep(0.25)
        spoken = [call[2] for call in calls if call[0] == "realtime.tell"]
        assert spoken, "the user heard nothing while the specialist worked"
        assert any("still" in line.lower() for line in spoken), spoken
    finally:
        agent.release.set()
        await route.wait_for_detached()


@pytest.mark.asyncio
async def test_a_barge_holds_the_answer_but_not_the_running_task_narration() -> None:
    """Holding is about the answer, not about the work.

    Talking over "I'll look that up" must not put the rest of a long task
    back into silence — that silence is exactly what the keepalive exists
    to fill.
    """
    agent = FakeAgent()
    agent.wait = True
    calls: list[Any] = []
    realtime = FakeRealtime(calls)
    route = HybridRoute(
        role_contract=HybridRoleContract("Codex"),
        agent=agent,
        realtime=realtime,
        audio_output=FakeOutput(calls),
        keepalive_interval_s=0.05,
    )
    await route.prepare(
        SimpleNamespace(session_id="session-1", emit=lambda event: None)
    )
    try:
        await route.run_turn(
            RouteInput(text="Research current prices"), _context(calls, 1)
        )
        await agent.started.wait()
        await route.interrupt_presentation(1)
        await route.run_turn(
            RouteInput(text="Tell me a short joke"), _context(calls, 2)
        )
        mark = len(calls)
        await asyncio.sleep(0.25)
        spoken = [call[2] for call in calls[mark:] if call[0] == "realtime.tell"]
        assert any("still" in line.lower() for line in spoken), spoken
    finally:
        agent.release.set()
        await route.wait_for_detached()


@pytest.mark.asyncio
async def test_live_mic_does_not_share_the_mouth_while_a_specialist_works() -> None:
    """Discord keeps appending mic frames after the ack turn returns.

    Keepalive and the answer then tell() on that same OpenAI session.
    Measured live: 49 seconds of silence after "I'll look that up".
    """
    agent = FakeAgent()
    agent.wait = True
    calls: list[Any] = []
    realtime = FakeRealtime(calls)
    route = HybridRoute(
        role_contract=HybridRoleContract("Codex"),
        agent=agent,
        realtime=realtime,
        audio_output=FakeOutput(calls),
        keepalive_interval_s=0.05,
    )
    await route.prepare(
        SimpleNamespace(session_id="session-1", emit=lambda event: None)
    )
    try:
        await route.run_turn(
            RouteInput(text="Research current prices"), _context(calls, 1)
        )
        await agent.started.wait()
        before = [call for call in calls if call[0] == "realtime.append"]
        await route.append_audio(b"\x00\x00" * 10, participant_id=7)
        after = [call for call in calls if call[0] == "realtime.append"]
        assert after == before
    finally:
        agent.release.set()
        await route.wait_for_detached()
    await route.append_audio(b"\x01\x00" * 10, participant_id=7)
    assert ("realtime.append", 7, b"\x01\x00" * 10) in calls


def test_keepalive_can_be_switched_off() -> None:
    route = HybridRoute(
        role_contract=HybridRoleContract("Codex"),
        agent=FakeAgent(),
        realtime=FakeRealtime([]),
        keepalive_interval_s=0,
    )
    route._start_keepalive()
    assert route._keepalive is None


def test_tool_narration_never_reads_the_invocation() -> None:
    """Observed live: the assistant read a git command aloud, operators and all.

    Codex and cursor report the literal shell line as the tool detail, and the
    mouth spoke whatever it was handed. Cascade maps a tool onto a phrase
    instead; hybrid now uses the same mapper.
    """
    from sleeptalk.voice_tool_narration import tool_status_line

    command = "cd /home/user/project && git status -sb && echo '---'"
    spoken = tool_status_line("Bash", command)
    assert spoken == "Running a command."
    for fragment in ("&&", "git status", "/Users", "echo"):
        assert fragment not in spoken

    # A described action still speaks, because it was written to be heard.
    assert tool_status_line("Edit", "updating the config file") == (
        "Updating the config file."
    )
    # Names adapters spell differently still land somewhere useful.
    assert tool_status_line("WebSearch") == "Searching."
    assert tool_status_line("Grep") == "Searching."
    assert tool_status_line("Read") == "Reading a file."


@pytest.mark.asyncio
async def test_specialist_is_shown_the_conversation_it_never_heard() -> None:
    """Live: asked why the assistant had switched languages, the specialist
    answered about Van Gogh and stated nobody had switched — which had just
    happened, out loud, twice. It keeps a session, but that session holds only
    what was dispatched to it, so a partial record reads as a complete one.
    """
    route, agent, _realtime, _speech, calls = await _route()

    await route.run_turn(RouteInput(text="How are you?"), _context(calls, 1))
    await route.run_turn(
        RouteInput(text="Research current prices"), _context(calls, 2)
    )
    await route.wait_for_detached()

    dispatched = agent.calls[-1]
    assert "How are you?" in dispatched, "earlier turn missing from the context"
    assert dispatched.endswith("Research current prices")
    assert "context only" in dispatched


@pytest.mark.asyncio
async def test_the_first_dispatch_carries_no_context_preamble() -> None:
    """Nothing was said before it, so there is nothing to explain."""
    route, agent, _realtime, _speech, calls = await _route()
    await route.run_turn(
        RouteInput(text="Research current prices"), _context(calls, 1)
    )
    await route.wait_for_detached()

    assert agent.calls == ["Research current prices"]


def test_plain_requests_reach_the_specialist_and_chat_does_not() -> None:
    """Live: "can you search the internet?" dispatched and was answered yes;
    "Give me Bitcoin's live price" did not, so the mouth was left holding a
    question it had no tools for and improvised from stale findings.
    """
    catalog = HandsCatalog(open=True)

    def owner(text: str) -> str:
        intent = CommittedIntent(IntentId.mint(), 1, text, PresentationEpoch(1))
        return type(assign_owner(intent, catalog=catalog)).__name__

    for request in (
        "Give me Bitcoin's live price.",
        "Show me the latest commits.",
        "Summarize that document.",
        "Run the tests.",
        "Look up the weather.",
    ):
        assert owner(request) == "SpecialistOwned", request

    # Conversation still belongs to the mouth; sending it to a 15-50s
    # specialist would cost exactly what realtime is there to avoid.
    for chat in (
        "Thanks.",
        "How are you?",
        "Good morning",
        "Tell me a short joke",
        "I'm testing this out on web.",
    ):
        assert owner(chat) == "RealtimeOwned", chat


@pytest.mark.asyncio
async def test_run_turn_hands_back_what_it_heard() -> None:
    """Hybrid transcribes inside the realtime session, so a surface that
    passed raw audio has no text for the turn unless the route returns it.
    Discord's channel stayed empty for every hybrid turn because of this.
    """
    route, _agent, _realtime, _speech, calls = await _route()
    outcome = await route.run_turn(RouteInput(text="How are you?"), _context(calls, 1))
    assert outcome.heard == "How are you?"


@pytest.mark.asyncio
async def test_keepalive_stays_quiet_once_the_answer_is_ready() -> None:
    """Specialist turns ran ~10s against a 10s keepalive, so every answer
    arrived with "still working on it" stapled to the end of it.
    """
    agent = FakeAgent()
    agent.hold_after_sentences = True
    calls: list[Any] = []
    realtime = FakeRealtime(calls)
    route = HybridRoute(
        role_contract=HybridRoleContract("Codex"),
        agent=agent,
        realtime=realtime,
        audio_output=FakeOutput(calls),
        keepalive_interval_s=0.05,
    )
    await route.prepare(
        SimpleNamespace(session_id="session-1", emit=lambda event: None)
    )
    try:
        await route.run_turn(
            RouteInput(text="Research current prices"), _context(calls, 1)
        )
        await agent.after_sentences.wait()
        await asyncio.sleep(0.25)
        spoken = [call[2] for call in calls if call[0] == "realtime.tell"]
        assert not any("still" in line.lower() for line in spoken), spoken
    finally:
        agent.release_after_sentences.set()
        await route.wait_for_detached()


@pytest.mark.asyncio
async def test_a_slow_transcription_is_not_a_silent_one() -> None:
    """Hybrid needs text before it can route, so it pays an OpenAI round trip
    to transcribe and another to speak — 2.0 s median to first audio against
    cascade's 0.9 s, because cascade transcribes while the user is talking.

    The wait cannot be removed. It can stop being silent.
    """
    calls: list[Any] = []

    class SlowCommit(FakeRealtime):
        async def commit(self, route_input, *, turn_id):  # type: ignore[override]
            await asyncio.sleep(0.2)
            return "Research current prices"

    realtime = SlowCommit(calls)
    route = HybridRoute(
        role_contract=HybridRoleContract("Codex"),
        agent=FakeAgent(),
        realtime=realtime,
        audio_output=FakeOutput(calls),
        first_token_budget_ms=20,
    )
    await route.prepare(
        SimpleNamespace(session_id="session-1", emit=lambda event: None)
    )
    await route.run_turn(RouteInput(pcm=b"\x00\x00" * 100), _context(calls, 1))
    await route.wait_for_detached()

    spoken = [call[2] for call in calls if call[0] == "realtime.tell"]
    assert any("moment" in line.lower() for line in spoken), spoken


@pytest.mark.asyncio
async def test_a_fast_transcription_says_nothing_extra() -> None:
    """The holding line only exists to cover a wait. No wait, no line."""
    route, _agent, _realtime, _speech, calls = await _route()
    await route.run_turn(
        RouteInput(text="Research current prices"), _context(calls, 1)
    )
    await route.wait_for_detached()

    spoken = [call[2] for call in calls if call[0] == "realtime.tell"]
    assert not any("moment" in line.lower() for line in spoken), spoken


@pytest.mark.asyncio
async def test_a_turn_reports_how_long_it_waited_for_text() -> None:
    """2004 ms to first audio, and no way to tell how much was transcription.

    The two halves are fixed by different things — routing on partials, or
    not needing the transcript at all — so optimising before splitting them
    is guesswork.
    """
    calls: list[Any] = []

    class SlowCommit(FakeRealtime):
        async def commit(self, route_input, *, turn_id):  # type: ignore[override]
            await asyncio.sleep(0.15)
            return "How are you?"

    route = HybridRoute(
        role_contract=HybridRoleContract("Codex"),
        agent=FakeAgent(),
        realtime=SlowCommit(calls),
        audio_output=FakeOutput(calls),
        first_token_budget_ms=0,
    )
    await route.prepare(
        SimpleNamespace(session_id="session-1", emit=lambda event: None)
    )
    outcome = await route.run_turn(RouteInput(pcm=b"\x00\x00" * 100), _context(calls, 1))

    assert outcome.heard_ms is not None
    assert outcome.heard_ms >= 140, outcome.heard_ms


@pytest.mark.asyncio
async def test_text_input_reports_no_transcription_wait() -> None:
    route, _agent, _realtime, _speech, calls = await _route()
    outcome = await route.run_turn(RouteInput(text="How are you?"), _context(calls, 1))
    assert outcome.heard_ms == 0 or outcome.heard_ms is None


def test_questions_about_state_the_mouth_cannot_see_go_to_the_specialist() -> None:
    """Live: "what are the latest commits" routed to the mouth, which has no
    repository, and it answered with an invented commit name. A confident
    fabrication is worse than a pause, and verb lists cannot enumerate how
    people ask about the world.
    """
    catalog = HandsCatalog(open=True)

    def owner(text: str) -> str:
        intent = CommittedIntent(IntentId.mint(), 1, text, PresentationEpoch(1))
        return type(assign_owner(intent, catalog=catalog)).__name__

    for question in (
        "what are the latest commits",
        "what's new in the repo",
        "any new merges?",
        "are there any failing tests",
        "how many commits today",
        "which files changed",
    ):
        assert owner(question) == "SpecialistOwned", question

    # Questions about the assistant itself are answered locally, and opinions
    # stay with the mouth — neither needs a 15-50 second specialist.
    assert owner("who are you") == "LocalOwned"
    assert owner("what's your name") == "LocalOwned"
    for chat in ("how are you", "what do you think about jazz", "what time is it"):
        assert owner(chat) == "RealtimeOwned", chat


@pytest.mark.asyncio
async def test_the_voice_can_dispatch_the_specialist_itself() -> None:
    """A word list cannot enumerate how people ask about the world. When the
    voice recognises it needs the working half, that call now starts the same
    specialist a pattern match would have.
    """
    from dataclasses import dataclass

    @dataclass(frozen=True)
    class ToolCall:
        call_id: str
        question: str

    resolved: list[tuple[str, str]] = []
    calls: list[Any] = []

    class ToolCallingRealtime(FakeRealtime):
        async def resolve_specialist(self, call_id, output):  # type: ignore[override]
            resolved.append((call_id, output))

        def respond(self, transcript, *, turn_id):  # type: ignore[override]
            async def stream():
                yield ToolCall("call-1", "what are the latest commits")

            return stream()

    agent = FakeAgent()
    route = HybridRoute(
        role_contract=HybridRoleContract("Codex"),
        agent=agent,
        realtime=ToolCallingRealtime(calls),
        audio_output=FakeOutput(calls),
    )
    await route.prepare(
        SimpleNamespace(session_id="session-1", emit=lambda event: None)
    )
    await route.run_turn(RouteInput(text="How are you?"), _context(calls, 1))
    await route.wait_for_detached()

    assert agent.calls, "the voice asked for the specialist and nothing ran"
    assert agent.calls[-1].endswith("what are the latest commits")
    assert resolved and resolved[0][0] == "call-1", (
        "an unanswered tool call stalls the model mid-turn"
    )


@pytest.mark.asyncio
async def test_a_failed_warmup_does_not_stop_the_route_preparing() -> None:
    """Warmup is "Say OK." — it primes the agent, it does not make the route
    work. Treating it as fatal turned one busy agent into a bot that would not
    start, and the retries reported "Agent is already processing" because the
    first attempt was still running.
    """
    agent = FakeAgent()

    async def failing_warmup():
        raise RuntimeError("Agent is already processing")

    agent.warmup = failing_warmup
    calls: list[Any] = []
    route = HybridRoute(
        role_contract=HybridRoleContract("Codex"),
        agent=agent,
        realtime=FakeRealtime(calls),
        audio_output=FakeOutput(calls),
    )

    await route.prepare(SimpleNamespace(session_id="s", emit=lambda event: None))

    outcome = await route.run_turn(RouteInput(text="How are you?"), _context(calls, 1))
    assert outcome.status == "completed"
    assert any(
        event.kind == "agent.warmup_failed" for event in route.reducer.events
    ), "a skipped warmup must still be on the record"


@pytest.mark.asyncio
async def test_a_holding_line_that_started_is_never_cut_off() -> None:
    """Live: preambles arrived in the Discord text feed and were silent.

    The line began at the budget and was cancelled when transcription landed a
    few hundred milliseconds later — mid-speech, with the presentation lock
    held, so the acknowledgement after it was silent too while its text still
    posted.
    """
    calls: list[Any] = []

    class SlowCommit(FakeRealtime):
        async def commit(self, route_input, *, turn_id):  # type: ignore[override]
            await asyncio.sleep(0.12)
            return "Research current prices"

    route = HybridRoute(
        role_contract=HybridRoleContract("Codex"),
        agent=FakeAgent(),
        realtime=SlowCommit(calls),
        audio_output=FakeOutput(calls),
        first_token_budget_ms=20,
    )
    await route.prepare(
        SimpleNamespace(session_id="session-1", emit=lambda event: None)
    )
    await route.run_turn(RouteInput(pcm=b"\x00\x00" * 100), _context(calls, 1))
    await route.wait_for_detached()

    spoken = [call[2] for call in calls if call[0] == "realtime.tell"]
    assert any("moment" in line.lower() for line in spoken), spoken
    # And the turn still speaks afterwards rather than going quiet.
    assert len(spoken) >= 1


@pytest.mark.asyncio
async def test_the_turn_reports_the_line_that_was_actually_spoken() -> None:
    """Discord read "I'll pull that up" while the voice said "One moment".

    The acknowledgement is skipped when the holding line already spoke, but the
    outcome still named it, and the channel posts the outcome.
    """
    calls: list[Any] = []

    class SlowCommit(FakeRealtime):
        async def commit(self, route_input, *, turn_id):  # type: ignore[override]
            await asyncio.sleep(0.12)
            return "Research current prices"

    route = HybridRoute(
        role_contract=HybridRoleContract("Codex"),
        agent=FakeAgent(),
        realtime=SlowCommit(calls),
        audio_output=FakeOutput(calls),
        first_token_budget_ms=20,
    )
    await route.prepare(
        SimpleNamespace(session_id="session-1", emit=lambda event: None)
    )
    outcome = await route.run_turn(
        RouteInput(pcm=b"\x00\x00" * 100), _context(calls, 1)
    )
    await route.wait_for_detached()

    spoken = [call[2] for call in calls if call[0] == "realtime.tell"]
    assert outcome.played, "the turn reported nothing spoken"
    assert outcome.played[0] in spoken, (
        f"reported {outcome.played[0]!r}, actually said {spoken}"
    )


class EarlyRealtime(FakeRealtime):
    """A Realtime that also takes early transcripts."""

    def __init__(self, calls: list[Any], early: Any) -> None:
        super().__init__(calls)
        self.early = early
        self.started: list[bytes] = []

    def transcribe_ahead(self, pcm: bytes, *, participant_id: int = 0) -> Any:
        self.started.append(pcm)
        return "task"

    def take_early(self, pcm: bytes, *, participant_id: int = 0) -> Any:
        return self.early


async def _early_route(early_text: str | None, *, early_delay: float, realtime_delay: float, audio_first: bool = True):
    calls: list[Any] = []

    async def early_task():
        await asyncio.sleep(early_delay)
        return early_text

    early = SimpleNamespace(task=asyncio.ensure_future(early_task())) if early_text is not None else None
    realtime = EarlyRealtime(calls, early)
    route = HybridRoute(role_contract=HybridRoleContract("Codex"), agent=FakeAgent(), realtime=realtime,
                        audio_output=FakeOutput(calls), speculative_audio=True, audio_first=audio_first)

    async def realtime_transcript():
        await asyncio.sleep(realtime_delay)
        return "realtime heard it"

    return route, realtime, asyncio.ensure_future(realtime_transcript()), _context(calls, 1)


@pytest.mark.asyncio
async def test_an_early_transcript_that_lands_first_answers_a_waiting_turn() -> None:
    route, _realtime, transcript, ctx = await _early_route("stop the task", early_delay=0, realtime_delay=0.5)
    started = asyncio.get_running_loop().time()
    assert await route._first_transcript(RouteInput(pcm=b"\0\0" * 480), ctx, transcript) == "stop the task"
    assert asyncio.get_running_loop().time() - started < 0.3
    transcript.cancel()


@pytest.mark.asyncio
async def test_realtime_first_or_an_empty_early_transcript_keeps_realtimes_text() -> None:
    route, _r, transcript, ctx = await _early_route("late words", early_delay=0.3, realtime_delay=0)
    assert await route._first_transcript(RouteInput(pcm=b"\0\0" * 480), ctx, transcript) == "realtime heard it"
    route, _r, transcript, ctx = await _early_route("", early_delay=0, realtime_delay=0.05)
    assert await route._first_transcript(RouteInput(pcm=b"\0\0" * 480), ctx, transcript) == "realtime heard it"
    route, _r, transcript, ctx = await _early_route(None, early_delay=0, realtime_delay=0)
    assert await route._first_transcript(RouteInput(pcm=b"\0\0" * 480), ctx, transcript) == "realtime heard it"


@pytest.mark.asyncio
async def test_early_transcripts_judge_every_turn_and_only_gated_ones_without_endpointing() -> None:
    route, realtime, transcript, _ctx = await _early_route(None, early_delay=0, realtime_delay=0)
    # Endpointing judges the turn by the pause's text (early_transcript, on by default).
    assert route.listens_ahead() is True
    assert route.transcribe_ahead(b"idle floor") == "task"
    # Off: audio-first turns don't wait for text; only gated ones spend the request.
    route._early_transcript = False
    assert route.listens_ahead() is False
    assert route.transcribe_ahead(b"idle floor, off") is None
    route._may_speak_first = lambda: False                 # a task is running
    assert route.transcribe_ahead(b"task running") == "task"
    assert realtime.started == [b"idle floor", b"task running"]
    transcript.cancel()


async def test_realtime_can_transcribe_a_soft_barge_s_words() -> None:
    """A soft barge on Realtime needs the caller's words to decide it; the route
    transcribes them over plain HTTP (it used to fail, and stop the answer)."""
    route, realtime, transcript, _ctx = await _early_route(None, early_delay=0, realtime_delay=0)

    async def transcribe_text(pcm):
        return " Mm-hmm. "

    realtime.transcribe_text = transcribe_text
    result = await route.transcribe(b"pcm")
    assert (result.text, result.source) == ("Mm-hmm.", "openai_http")
    transcript.cancel()


def test_a_stop_that_carries_a_request_is_a_redirect() -> None:
    from sleeptalk.routes.hybrid_types import redirect_request

    assert redirect_request("Wait, stop. What's the capital of France?") == "What's the capital of France?"
    assert redirect_request("Never mind, debug this repository instead.") == "debug this repository instead."
    assert redirect_request("Stop.") == ""                      # a bare control stays a control
    assert redirect_request("Stop the research task") == ""     # no stop mark: the cancel pattern's job
    assert redirect_request("What's the capital of France?") == ""


@pytest.mark.asyncio
async def test_wait_stop_with_a_new_request_cancels_the_task_and_takes_the_request() -> None:
    """ "Wait, stop. <request>" during a task used to get "I'm still working on
    the current task." Now the task is cancelled and the request is taken."""
    agent = FakeAgent()
    agent.wait = True
    route, _agent, _realtime, _speech, calls = await _route(agent=agent)
    await route.run_turn(RouteInput(text="Research current prices"), _context(calls, 1))
    await agent.started.wait()

    second = await route.run_turn(
        RouteInput(text="Wait, stop. Debug this repository instead."), _context(calls, 2)
    )

    assert second.status == "completed"
    assert not any(
        call[0] == "realtime.tell" and call[2] == HybridRoute.BUSY_SENTENCE for call in calls
    )
    assert any(task.phase is TaskPhase.CANCELLED for task in route.tasks)
    agent.release.set()
    await route.wait_for_detached()


@pytest.mark.parametrize(
    ("said", "tool_question"),
    [("What's the capital of France?", "What is the capital of France?"),
     # Realtime rewords it for the tool; the caller's words still count.
     ("I'd like to know about the history of tea, in two sentences.",
      "Provide a two-sentence history of tea.")],
)
async def test_a_general_knowledge_hand_off_goes_back_to_the_voice(said: str, tool_question: str) -> None:
    """Realtime handing the agent "what is the capital of France" is sent back:
    the voice answers it, no agent task starts. Asked again, the agent gets it."""
    from dataclasses import dataclass

    from sleeptalk.routes.hybrid import VOICE_ANSWERS_TOOL_OUTPUT

    @dataclass(frozen=True)
    class ToolCall:
        call_id: str
        question: str

    resolved: list[tuple[str, str]] = []
    calls: list[Any] = []
    asked: list[str] = []

    class ToolCallingRealtime(FakeRealtime):
        async def resolve_specialist(self, call_id, output):  # type: ignore[override]
            resolved.append((call_id, output))

        def respond(self, transcript, *, turn_id):  # type: ignore[override]
            asked.append(transcript)

            async def stream():
                if len(asked) == 1:
                    yield ToolCall("call-1", tool_question)
                else:
                    yield b"\x01\x00" * 480

            return stream()

    agent = FakeAgent()
    route = HybridRoute(
        role_contract=HybridRoleContract("Codex"),
        agent=agent,
        realtime=ToolCallingRealtime(calls),
        audio_output=FakeOutput(calls),
    )
    await route.prepare(SimpleNamespace(session_id="session-1", emit=lambda event: None))
    await route.run_turn(RouteInput(text=said), _context(calls, 1))

    assert resolved == [("call-1", VOICE_ANSWERS_TOOL_OUTPUT)]
    assert agent.calls == []           # no agent task
    assert asked[-1] == tool_question  # the voice was asked to answer it
    await route.close()
