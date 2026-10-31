from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from sleeptalk.adapter import (
    AgentCapabilityManifest,
    CapabilitySupport,
    SupportLevel,
    ToolEvent,
    ToolPhase,
    VoiceSessionCapability,
    WorkerCapability,
    WorkerEvent,
    WorkerEvidence,
    WorkerPhase,
    adapter_capabilities,
    publish_event,
)
from sleeptalk.routes.cascade import CascadeRoute
from sleeptalk.session import RealtimeSession
from sleeptalk.session_event_log import ENV as SESSION_EVENT_LOG_ENV
from sleeptalk.session_events import SessionEventType, SessionState, session_state
from sleeptalk.subagent_roster import SubagentRoster
from sleeptalk.voice.subagent import (
    QUIET_INTERVAL_S,
    FloorState,
    SpeechKind,
    WorkerVoiceState,
    admit_worker,
    decide_worker_speech,
    is_status_question,
    remember_ack,
    status_line,
    take_queued,
)


def _manifest(*, evidence: WorkerEvidence = WorkerEvidence.LIFECYCLE_NATIVE) -> AgentCapabilityManifest:
    supported = CapabilitySupport(SupportLevel.SUPPORTED)
    unsupported = CapabilitySupport(SupportLevel.UNSUPPORTED)
    return AgentCapabilityManifest(
        contract_version=1,
        structured_events=supported,
        tool_lifecycle=supported,
        permissions=unsupported,
        clarification=unsupported,
        session_identity=unsupported,
        status=unsupported,
        usage=unsupported,
        steering=unsupported,
        targeted_cancellation=unsupported,
        workers=WorkerCapability(SupportLevel.SUPPORTED, evidence=evidence),
        voice_sessions=VoiceSessionCapability(SupportLevel.UNSUPPORTED),
    )


def _roster() -> SubagentRoster:
    return SubagentRoster.from_manifest("s1", _manifest())


def _apply(roster: SubagentRoster, **fields: object):
    return roster.apply(WorkerEvent(**fields))  # type: ignore[arg-type]


def test_first_detached_start_acks_and_invites_more_talk() -> None:
    roster = _roster()
    change = _apply(
        roster,
        worker_id="w1",
        phase=WorkerPhase.STARTED,
        label="researcher",
        detached=True,
    )
    assert change is not None
    decision = decide_worker_speech(
        change,
        snapshot=roster.snapshot(),
        floor=FloorState.THINKING,
        state=WorkerVoiceState(),
    )
    assert decision.kind is SpeechKind.ACK
    assert decision.queue is False
    assert "researcher" in decision.text
    assert "keep talking" in decision.text.lower()


def test_second_detached_start_stays_quiet() -> None:
    roster = _roster()
    _apply(
        roster,
        worker_id="w1",
        phase=WorkerPhase.STARTED,
        label="researcher",
        detached=True,
    )
    change = _apply(
        roster,
        worker_id="w2",
        phase=WorkerPhase.STARTED,
        label="reviewer",
        detached=True,
    )
    assert change is not None
    state = WorkerVoiceState(detached_acks=1)
    decision = decide_worker_speech(
        change,
        snapshot=roster.snapshot(),
        floor=FloorState.THINKING,
        state=state,
    )
    assert decision.kind is SpeechKind.SILENT


def test_sequential_detached_start_acks_after_prior_worker_completes() -> None:
    roster = _roster()
    _apply(
        roster,
        worker_id="w1",
        phase=WorkerPhase.STARTED,
        label="Commit and merge branch",
        detached=True,
    )
    _apply(roster, worker_id="w1", phase=WorkerPhase.COMPLETED)
    change = _apply(
        roster,
        worker_id="w2",
        phase=WorkerPhase.STARTED,
        label="Research sub-agent voice adapters",
        detached=True,
    )
    assert change is not None
    decision = decide_worker_speech(
        change,
        snapshot=roster.snapshot(),
        floor=FloorState.BOT_SPEAKING,
        state=WorkerVoiceState(detached_acks=1),
    )
    assert decision.kind is SpeechKind.ACK
    assert decision.queue is False
    assert "Research sub-agent voice adapters" in decision.text
    assert "keep talking" in decision.text.lower()


def test_three_live_workers_can_name_the_count() -> None:
    roster = _roster()
    _apply(roster, worker_id="a", phase=WorkerPhase.RUNNING, detached=True, label="one")
    _apply(roster, worker_id="b", phase=WorkerPhase.RUNNING, detached=True, label="two")
    change = _apply(
        roster, worker_id="c", phase=WorkerPhase.STARTED, detached=True, label="three"
    )
    assert change is not None
    decision = decide_worker_speech(
        change,
        snapshot=roster.snapshot(),
        floor=FloorState.THINKING,
        state=WorkerVoiceState(detached_acks=2),
    )
    assert decision.kind is SpeechKind.ACK
    assert "3 checks" in decision.text


def test_blocking_start_does_not_claim_keep_talking() -> None:
    roster = _roster()
    change = _apply(
        roster,
        worker_id="w1",
        phase=WorkerPhase.STARTED,
        label="reviewer",
        detached=False,
    )
    assert change is not None
    decision = decide_worker_speech(
        change,
        snapshot=roster.snapshot(),
        floor=FloorState.THINKING,
        state=WorkerVoiceState(),
    )
    assert decision.kind is SpeechKind.ACK
    assert "keep talking" not in decision.text.lower()
    assert "reviewer" in decision.text


def test_completion_queues_when_user_or_bot_has_the_floor() -> None:
    roster = _roster()
    _apply(roster, worker_id="w1", phase=WorkerPhase.STARTED, label="research", detached=True)
    change = _apply(roster, worker_id="w1", phase=WorkerPhase.COMPLETED, summary="done")
    assert change is not None
    for floor in (FloorState.USER_SPEAKING, FloorState.BOT_SPEAKING):
        decision = decide_worker_speech(
            change,
            snapshot=roster.snapshot(),
            floor=floor,
            state=WorkerVoiceState(detached_acks=1),
        )
        assert decision.kind is SpeechKind.COMPLETION
        assert decision.queue is True


def test_completion_speaks_at_a_listening_gap() -> None:
    roster = _roster()
    _apply(roster, worker_id="w1", phase=WorkerPhase.STARTED, label="research", detached=True)
    change = _apply(roster, worker_id="w1", phase=WorkerPhase.COMPLETED)
    assert change is not None
    decision = decide_worker_speech(
        change,
        snapshot=roster.snapshot(),
        floor=FloorState.LISTENING,
        state=WorkerVoiceState(detached_acks=1),
    )
    assert decision.queue is False
    assert "research is ready" in decision.text.lower()


def test_progress_stays_quiet_until_blocked_or_overdue() -> None:
    roster = _roster()
    _apply(roster, worker_id="w1", phase=WorkerPhase.STARTED, label="research", detached=True)
    change = _apply(
        roster, worker_id="w1", phase=WorkerPhase.RUNNING, summary="reading files"
    )
    assert change is not None
    quiet = decide_worker_speech(
        change,
        snapshot=roster.snapshot(),
        floor=FloorState.THINKING,
        state=WorkerVoiceState(detached_acks=1),
        quiet_elapsed_s=3.0,
    )
    assert quiet.kind is SpeechKind.SILENT
    overdue = decide_worker_speech(
        change,
        snapshot=roster.snapshot(),
        floor=FloorState.THINKING,
        state=WorkerVoiceState(detached_acks=1),
        quiet_elapsed_s=QUIET_INTERVAL_S,
    )
    assert overdue.kind is SpeechKind.PROGRESS
    blocked = _apply(
        roster, worker_id="w1", phase=WorkerPhase.RUNNING, summary="blocked on auth"
    )
    assert blocked is not None
    spoken = decide_worker_speech(
        blocked,
        snapshot=roster.snapshot(),
        floor=FloorState.THINKING,
        state=WorkerVoiceState(detached_acks=1),
    )
    assert spoken.kind is SpeechKind.PROGRESS
    assert "blocked" in spoken.text.lower()


def test_status_question_reads_the_roster() -> None:
    assert is_status_question("what's still running?")
    assert is_status_question("how is the research going")
    roster = _roster()
    _apply(roster, worker_id="w1", phase=WorkerPhase.RUNNING, label="research", detached=True)
    assert status_line(roster.snapshot()) == "Still running: research."


def test_admit_worker_emits_turnless_roster_changed() -> None:
    roster = _roster()
    events: list[object] = []
    change = admit_worker(
        roster,
        events.append,
        WorkerEvent(worker_id="w1", phase=WorkerPhase.STARTED, label="research"),
    )
    assert change is not None
    assert events[0].type is SessionEventType.ROSTER_CHANGED
    assert events[0].turn_id is None
    assert events[0].payload["workerId"] == "w1"


class ToolOnlyAgent:
    def __init__(self) -> None:
        self.agent_capabilities = _manifest()

    async def respond_streaming(self, text: str, *, on_event=None):
        del text
        publish_event(on_event, ToolEvent(name="bash", phase=ToolPhase.STARTED))
        yield "Ran bash."

    async def abort(self) -> None:
        return None

    async def warmup(self) -> None:
        return None

    async def close(self) -> None:
        return None


class _Route:
    def bind(self, session_id: str, emit: object) -> None:
        del session_id, emit

    async def prepare(self, session: object) -> None:
        del session

    async def close(self) -> None:
        return None

    def active_turn(self) -> None:
        return None


@pytest.mark.asyncio
async def test_session_prepare_hangs_roster_from_manifest() -> None:
    agent = SimpleNamespace(agent_capabilities=_manifest())
    session = RealtimeSession(SimpleNamespace(route=_Route(), agent=agent))
    await session.prepare()
    assert session.roster is not None
    assert session.roster.snapshot().capability.evidence is WorkerEvidence.LIFECYCLE_NATIVE
    events: list[object] = []
    session.subscribe(events.append)
    session.admit_worker(
        WorkerEvent(worker_id="w1", phase=WorkerPhase.STARTED, label="research", detached=True)
    )
    assert any(event.type is SessionEventType.ROSTER_CHANGED for event in events)
    await session.aclose()
    assert session.roster.snapshot().closed is True


def test_session_event_log_writes_jsonl_when_env_is_set(
    tmp_path, monkeypatch
) -> None:
    import json

    path = tmp_path / "events.jsonl"
    monkeypatch.setenv(SESSION_EVENT_LOG_ENV, str(path))
    agent = SimpleNamespace(agent_capabilities=_manifest())
    session = RealtimeSession(SimpleNamespace(route=_Route(), agent=agent))
    session.emit(session_state(session.session_id, SessionState.CONNECTING))
    session.admit_worker(
        WorkerEvent(worker_id="w1", phase=WorkerPhase.STARTED, label="research")
    )
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    types = [row["type"] for row in rows]
    assert "session.state" in types
    assert "roster.changed" in types
    roster = next(row for row in rows if row["type"] == "roster.changed")
    assert roster["payload"]["workerId"] == "w1"


def test_session_event_log_is_off_without_env(tmp_path, monkeypatch) -> None:
    path = tmp_path / "events.jsonl"
    monkeypatch.delenv(SESSION_EVENT_LOG_ENV, raising=False)
    session = RealtimeSession(SimpleNamespace(route=_Route(), agent=object()))
    session.emit(session_state(session.session_id, SessionState.CONNECTING))
    assert not path.exists()


def test_legacy_floor_session_has_an_empty_unsupported_roster() -> None:
    session = RealtimeSession(SimpleNamespace(route=_Route(), agent=object()))
    roster = session.attach_roster()
    assert adapter_capabilities(object()).workers.level is SupportLevel.UNSUPPORTED
    assert roster.apply(WorkerEvent(worker_id="ghost", phase=WorkerPhase.STARTED)) is None


def test_queued_decisions_drain_once() -> None:
    roster = _roster()
    change = _apply(roster, worker_id="w1", phase=WorkerPhase.COMPLETED, label="research")
    assert change is not None
    state = WorkerVoiceState()
    first = decide_worker_speech(
        change,
        snapshot=roster.snapshot(),
        floor=FloorState.USER_SPEAKING,
        state=state,
    )
    assert first.queue is True
    state.queued.append(first)
    drained = take_queued(state)
    assert drained == [first]
    assert take_queued(state) == []


def test_remember_ack_counts_first_detached_start() -> None:
    roster = _roster()
    change = _apply(
        roster, worker_id="w1", phase=WorkerPhase.STARTED, label="research", detached=True
    )
    assert change is not None
    state = WorkerVoiceState()
    remember_ack(state, change)
    assert state.detached_acks == 1


class WorkerAgent:
    def __init__(self, *, hang: bool = False) -> None:
        self.hang = hang
        self.agent_capabilities = _manifest()
        self._abort = asyncio.Event()

    async def respond_streaming(self, text: str, *, on_event=None):
        del text
        publish_event(
            on_event,
            WorkerEvent(
                worker_id="researcher-1",
                phase=WorkerPhase.STARTED,
                label="researcher",
                detached=True,
            ),
        )
        if self.hang:
            await self._abort.wait()
            return
            yield  # make this an async generator
        yield "Working on the comparison."
        publish_event(
            on_event,
            WorkerEvent(
                worker_id="researcher-1",
                phase=WorkerPhase.COMPLETED,
                label="researcher",
            ),
        )

    async def abort(self) -> None:
        self._abort.set()

    async def warmup(self) -> None:
        return None

    async def close(self) -> None:
        return None


def _session(agent: object, spoken: list[str]) -> RealtimeSession:
    async def speak(line: str, turn_id: int) -> None:
        del turn_id
        spoken.append(line)

    route = CascadeRoute(agent=agent, tts=None, speak=speak)
    return RealtimeSession(SimpleNamespace(route=route, agent=agent, policy=None, config=None))


@pytest.mark.asyncio
async def test_ordinary_tool_events_do_not_enter_the_roster() -> None:
    spoken: list[str] = []
    session = _session(ToolOnlyAgent(), spoken)
    await session.run_turn(text="run bash")
    assert not any(event.type is SessionEventType.ROSTER_CHANGED for event in session.events)
    assert session.roster is not None
    assert session.roster.snapshot().workers == ()
    await session.aclose()


@pytest.mark.asyncio
async def test_cascade_admits_worker_and_speaks_primary_only() -> None:
    spoken: list[str] = []
    agent = WorkerAgent()
    session = _session(agent, spoken)
    events: list[object] = []
    session.subscribe(events.append)
    outcome = await session.run_turn(text="compare those frameworks")
    assert outcome.status == "completed"
    roster_events = [event for event in session.events if event.type is SessionEventType.ROSTER_CHANGED]
    assert [event.payload["phase"] for event in roster_events] == ["started", "completed"]
    assert all(event.turn_id is None for event in roster_events)
    assert any("researcher" in line and "keep talking" in line.lower() for line in spoken)
    assert any("researcher is ready" in line.lower() for line in spoken)
    row = session.roster.get("researcher-1") if session.roster else None
    assert row is not None
    assert row.phase is WorkerPhase.COMPLETED
    await session.aclose()


@pytest.mark.asyncio
async def test_status_question_answers_from_roster() -> None:
    spoken: list[str] = []
    agent = WorkerAgent(hang=True)
    session = _session(agent, spoken)
    session.attach_roster()
    session.admit_worker(
        WorkerEvent(
            worker_id="researcher-1",
            phase=WorkerPhase.RUNNING,
            label="researcher",
            detached=True,
        )
    )
    outcome = await session.run_turn(text="what's still running?")
    assert outcome.status == "completed"
    assert outcome.played == ["Still running: researcher."]
    await session.aclose()


@pytest.mark.asyncio
async def test_cancel_does_not_mark_detached_worker_cancelled() -> None:
    spoken: list[str] = []
    agent = WorkerAgent(hang=True)
    session = _session(agent, spoken)
    task = asyncio.create_task(session.run_turn(text="start the research"))
    for _ in range(50):
        if session.roster is not None and session.roster.get("researcher-1") is not None:
            break
        await asyncio.sleep(0.02)
    await session.cancel()
    await task
    row = session.roster.get("researcher-1") if session.roster else None
    assert row is not None
    assert row.phase is WorkerPhase.STARTED
    await session.aclose()
