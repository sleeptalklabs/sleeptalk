"""Background jobs: a <job> handoff runs in a second agent while the call goes on."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest
from sleeptalk.adapter import (
    AssistantDelta,
    ControlAccepted,
    PermissionDecision,
    PermissionRequested,
    ResolvePermission,
    ToolEvent,
    ToolPhase,
)
from sleeptalk.jobs import (
    KEEP_TALKING_LINE,
    HANDOFF_INSTRUCTION,
    JOB_CONTRACT,
    STARTED_LINE,
    CancelJob,
    JobManager,
    JobMarkerFilter,
    JobNote,
    StartJob,
    extract_links,
    is_job_cancel,
    is_job_status_question,
)
from sleeptalk.policy import VoicePolicy
from sleeptalk.routes.audio_output import AudioOutputAccounting
from sleeptalk.routes.cascade import CascadeRoute
from sleeptalk.session import RealtimeSession
from sleeptalk.session_events import SessionEvent, SessionEventType


# -- the tag filter ---------------------------------------------------------


def test_filter_takes_a_block_spread_over_sentences_out_of_speech() -> None:
    marker = JobMarkerFilter()
    spoken, actions = marker.feed('<job title="todo app">Build a todo app in ./todo.')
    assert (spoken, actions) == ("", [])
    assert marker.holding
    spoken, actions = marker.feed("Serve it and report the link.</job> I'll build that in the background.")
    assert spoken == "I'll build that in the background."
    assert actions == [StartJob(title="todo app", brief="Build a todo app in ./todo. Serve it and report the link.")]
    assert not marker.holding


def test_filter_reads_notes_cancel_and_untitled_jobs() -> None:
    marker = JobMarkerFilter()
    spoken, actions = marker.feed("Sure. <job-note>Make it dark mode too.</job-note> Passing that on.")
    assert spoken == "Sure. Passing that on."
    assert actions == [JobNote("Make it dark mode too.")]
    assert marker.feed("<job-cancel/> Stopped it.") == ("Stopped it.", [CancelJob()])
    _spoken, actions = marker.feed("<job>Research three note apps and compare them.</job>")
    assert actions == [StartJob(title="research three note", brief="Research three note apps and compare them.")]


def test_filter_flush_keeps_an_unclosed_block() -> None:
    marker = JobMarkerFilter()
    marker.feed("<job title='site'>Make a landing page.")
    assert marker.flush() == [StartJob(title="site", brief="Make a landing page.")]
    assert marker.flush() == []


def test_phrases_and_links() -> None:
    assert is_job_status_question("How's it going?")
    assert is_job_status_question("hey how is the todo app coming along")
    assert is_job_status_question("Is it done yet?")
    assert not is_job_status_question("What's the capital of France?")
    assert is_job_cancel("Cancel the job.")
    assert is_job_cancel("okay stop the build")
    assert not is_job_cancel("stop talking about the build")
    assert extract_links("Open http://localhost:8000/ or /tmp/todo/index.html.") == (
        "http://localhost:8000/",
        "/tmp/todo/index.html",
    )


# -- the manager --------------------------------------------------------------


class JobAgent:
    """A fresh agent instance for the job: works until released."""

    def __init__(self, reply: str = "Built the app with local storage.\nhttp://localhost:8000/") -> None:
        self.reply = reply
        self.prompts: list[str] = []
        self.release = asyncio.Event()
        self.started = asyncio.Event()
        self.aborted = 0
        self.closed = 0
        self.controls: list[Any] = []
        self.ask_permission = False

    async def warmup(self) -> None:
        return None

    async def respond_streaming(self, text: str, *, on_event=None):
        self.prompts.append(text)
        if on_event is not None:
            on_event(ToolEvent(name="bash", phase=ToolPhase.STARTED, detail="Running the tests"))
            if self.ask_permission:
                on_event(PermissionRequested("perm-1", "Run npm install", frozenset(PermissionDecision)))
        self.started.set()
        await self.release.wait()
        if on_event is not None:
            on_event(AssistantDelta(self.reply))
        for line in self.reply.split("\n"):
            yield line

    async def control(self, command: Any):
        self.controls.append(command)
        return ControlAccepted() if isinstance(command, ResolvePermission) else None

    async def abort(self) -> None:
        self.aborted += 1
        self.release.set()

    async def close(self) -> None:
        self.closed += 1


def _manager(agent: JobAgent) -> tuple[JobManager, list[str], list[tuple[str, dict]]]:
    said: list[str] = []
    events: list[tuple[str, dict]] = []
    def announce(line) -> None:
        # The session asks a callable for its words when it is about to speak.
        said.append(line() if callable(line) else line)

    manager = JobManager(lambda: agent, emit=lambda name, fields: events.append((name, fields)), announce=announce)
    return manager, said, events


@pytest.mark.asyncio
async def test_job_runs_reports_and_announces_once_done() -> None:
    agent = JobAgent()
    manager, said, events = _manager(agent)
    started = manager.start("todo app", "Build a todo app.")
    assert started.job is not None and started.line == ""
    await asyncio.wait_for(agent.started.wait(), 1)
    assert agent.prompts == [f"{JOB_CONTRACT}\n\nBuild a todo app."]
    assert manager.active is not None
    assert "Right now it's running the tests." in manager.status_line()
    assert 'Background job "todo app" is running' in manager.context_block()

    agent.release.set()
    await asyncio.sleep(0.05)
    job = manager.job
    assert job is not None and job.phase == "completed"
    assert job.links == ("http://localhost:8000/",)
    assert said == ["The todo app job is done. Built the app with local storage. The link is in the chat."]
    # The call's first job is selected for the page as it starts.
    assert [name for name, _ in events] == [
        "job.selected", "job.started", "job.running", "job.step", "job.said", "job.completed"]
    assert events[3][1]["text"] == "bash · Running the tests" and events[3][1]["steps"] == 1
    assert events[4][1]["text"] == "Built the app with local storage."
    # The reply's bare link line is not said again; the Done event carries it.
    assert events[-1][1]["links"] == ["http://localhost:8000/"]
    assert agent.closed == 1
    # The foreground agent hears the result once, then only that nothing runs.
    assert "finished. Its report" in manager.context_block()
    later = manager.context_block()
    assert later.startswith("[No background job is running.") and "Its report" not in later


@pytest.mark.asyncio
async def test_a_note_to_a_finished_job_runs_as_a_follow_up() -> None:
    first, second = JobAgent(), JobAgent(reply="Added the card.")
    agents = iter([first, second])
    said: list[str] = []
    manager = JobManager(lambda: next(agents), emit=lambda *_: None,
                         announce=lambda line: said.append(line() if callable(line) else line))
    manager.start("todo app", "Build a todo app.")
    first.release.set()
    await asyncio.sleep(0.05)
    assert manager.job is not None and manager.job.phase == "completed"

    await manager.note("Add a card called renew license.")
    await asyncio.wait_for(second.started.wait(), 1)
    follow_up = manager.active
    assert follow_up is not None and follow_up.title == "todo app"
    assert "The earlier brief: Build a todo app." in second.prompts[0]
    assert "Built the app with local storage." in second.prompts[0]
    assert second.prompts[0].endswith("Now the person on the call wants: Add a card called renew license.")
    second.release.set()
    await asyncio.sleep(0.05)
    assert said[-1].startswith("The todo app job is done.")


@pytest.mark.asyncio
async def test_a_note_with_no_job_ever_is_dropped() -> None:
    agent = JobAgent()
    manager, _said, events = _manager(agent)
    await manager.note("Make it blue.")
    assert manager.job is None and events == [] and agent.prompts == []


@pytest.mark.asyncio
async def test_cancel_stops_the_job_without_an_announcement() -> None:
    agent = JobAgent()
    manager, said, events = _manager(agent)
    manager.start("todo app", "Build it.")
    await asyncio.wait_for(agent.started.wait(), 1)
    assert await manager.cancel()
    assert manager.job is not None and manager.job.phase == "cancelled"
    assert agent.aborted == 1 and agent.closed == 1
    assert said == []
    assert events[-1][0] == "job.cancelled"
    assert not await manager.cancel()


@pytest.mark.asyncio
async def test_notes_queue_as_follow_ups_when_the_agent_cannot_steer() -> None:
    agent = JobAgent()
    manager, _said, _events = _manager(agent)
    manager.start("todo app", "Build it.")
    await asyncio.wait_for(agent.started.wait(), 1)
    await manager.note("Make it dark mode too.")
    agent.release.set()
    await asyncio.sleep(0.05)
    assert agent.prompts[-1] == "The person on the call adds: Make it dark mode too."
    assert manager.job is not None and manager.job.phase == "completed"


@pytest.mark.asyncio
async def test_a_failing_agent_fails_the_job_not_the_call() -> None:
    class Broken(JobAgent):
        async def respond_streaming(self, text: str, *, on_event=None):
            raise RuntimeError("model not supported")
            yield ""  # pragma: no cover

    agent = Broken()
    manager, said, _events = _manager(agent)
    manager.start("todo app", "Build it.")
    await asyncio.sleep(0.05)
    assert manager.job is not None and manager.job.phase == "failed"
    assert said == ["The todo app job stopped: model not supported."]


@pytest.mark.asyncio
async def test_permission_is_announced_and_answered(monkeypatch) -> None:
    monkeypatch.setattr("sleeptalk.jobs._ASK_GRACE_S", 0.01)
    agent = JobAgent()
    agent.ask_permission = True
    manager, said, _events = _manager(agent)
    manager.start("todo app", "Build it.")
    await asyncio.wait_for(agent.started.wait(), 1)
    await asyncio.sleep(0.05)
    assert said == ["The todo app job asks: Run npm install? Say yes or no."]
    assert manager.permission_pending
    assert await manager.answer_permission(PermissionDecision.ALLOW_ONCE)
    assert isinstance(agent.controls[-1], ResolvePermission)
    assert not manager.permission_pending
    await manager.close()


# -- a call ---------------------------------------------------------------


class ForegroundAgent:
    """The caller's agent: hands the todo app off, answers anything else."""

    def __init__(self) -> None:
        self.prompts: list[str] = []
        self.closed = 0

    async def warmup(self) -> None:
        return None

    async def respond_streaming(self, text: str, *, on_event=None):
        self.prompts.append(text)
        if "todo app" in text.split("\n")[-1]:
            yield '<job title="todo app">Build a todo app.'
            yield "Report the link.</job>"
            yield "I'll build that in the background."
        else:
            yield "Paris is the capital of France."

    async def abort(self) -> None:
        return None

    async def close(self) -> None:
        self.closed += 1


class TTS:
    def __init__(self) -> None:
        self.texts: list[str] = []

    async def synthesize(self, text: str):
        self.texts.append(text)
        yield f"PCM<{text}>".encode()

    async def abort(self) -> None:
        return None

    async def close(self) -> None:
        return None


class Output:
    def __init__(self) -> None:
        self.account = AudioOutputAccounting(turn_id=-1)

    async def write(self, pcm: bytes, turn_id: int) -> None:
        self.account.generated(len(pcm))
        self.account.written(len(pcm))

    async def discard_turn(self, turn_id: int, reason: str = "interrupted") -> None:
        return None

    def accounting(self) -> AudioOutputAccounting:
        return self.account

    async def close(self) -> None:
        return None


@dataclass
class Assembly:
    route: CascadeRoute
    policy: VoicePolicy
    config: Any
    agent: Any
    job_agent_factory: Any = None
    usage_meter: Any = None
    transport: Any = None
    stt: Any = None
    streaming_stt: Any = None
    tts: Any = None
    tts_fallbacks: tuple[Any, ...] = ()


def _call(job_agent: JobAgent, foreground: Any = None) -> tuple[RealtimeSession, ForegroundAgent, TTS]:
    foreground = foreground or ForegroundAgent()
    tts = TTS()
    route = CascadeRoute(agent=foreground, tts=tts, audio_output=Output())
    policy = VoicePolicy(instant_ack=False, keepalive_interval_s=0)
    config = SimpleNamespace(stt=SimpleNamespace(streaming_timeout_seconds=0.1, rest_timeout_seconds=0.2))
    session = RealtimeSession(Assembly(
        route=route, policy=policy, config=config, agent=foreground, job_agent_factory=lambda: job_agent,
    ))
    return session, foreground, tts


@pytest.mark.asyncio
async def test_a_call_hands_off_keeps_talking_and_hears_the_result(monkeypatch) -> None:
    monkeypatch.setattr("sleeptalk.session._ANNOUNCE_GAP_S", 0.05)
    job_agent = JobAgent()
    session, foreground, tts = _call(job_agent)
    events: list[SessionEvent] = []
    session.subscribe(events.append)

    await session.run_turn(text="Make a todo app and give me a link.")
    assert HANDOFF_INSTRUCTION in foreground.prompts[0]
    # The first handoff of the call says talking won't interrupt the work.
    assert tts.texts == ["I'll build that in the background.", KEEP_TALKING_LINE]
    await asyncio.wait_for(job_agent.started.wait(), 1)
    assert job_agent.prompts == [f"{JOB_CONTRACT}\n\nBuild a todo app. Report the link."]

    # The conversation carries on while the job works.
    await session.run_turn(text="What's the capital of France?")
    assert tts.texts[-1] == "Paris is the capital of France."
    assert 'Background job "todo app" is running' in foreground.prompts[-1]

    # Asked how it's going: answered from the job, not the agent.
    asked = len(foreground.prompts)
    await session.run_turn(text="How's it going?")
    assert len(foreground.prompts) == asked
    assert tts.texts[-1].startswith("The todo app job is still going")

    job_agent.release.set()
    for _ in range(40):
        if tts.texts[-1].startswith("The todo app job is done."):
            break
        await asyncio.sleep(0.05)
    assert tts.texts[-1] == "The todo app job is done. Built the app with local storage. The link is in the chat."
    # The call page shows the worker's own lines, ending with its links.
    from sleeptalk.presentation import PresentationState, reduce

    state = PresentationState()
    for event in events:
        state = reduce(state, event)
    worker = [line for line in state.lines if line.kind == "worker"]
    assert [(line.phase, line.text) for line in worker] == [("started", "Working on todo app."), ("completed", "Done.")]
    assert worker[-1].links == ("http://localhost:8000/",)
    assert [(entry.kind, entry.text) for entry in state.jobs[0].entries] == [
        ("step", "bash · Running the tests"),
        ("said", "Built the app with local storage."),
        ("done", "Done."),
    ]
    assert not any(e.payload.get("owner") == "specialist" for e in events if e.type is SessionEventType.AGENT_EVENT)

    await session.run_turn(text="What's the capital of France?")
    assert 'Background job "todo app" finished' in foreground.prompts[-1]
    await session.aclose()


@pytest.mark.asyncio
async def test_hanging_up_cancels_the_job() -> None:
    job_agent = JobAgent()
    session, _foreground, _tts = _call(job_agent)
    await session.run_turn(text="Make a todo app.")
    await asyncio.wait_for(job_agent.started.wait(), 1)
    await session.aclose()
    assert session.jobs.job.phase == "cancelled"
    assert job_agent.closed == 1


@pytest.mark.asyncio
async def test_cancel_by_voice() -> None:
    job_agent = JobAgent()
    session, _foreground, tts = _call(job_agent)
    await session.run_turn(text="Make a todo app.")
    await asyncio.wait_for(job_agent.started.wait(), 1)
    await session.run_turn(text="Cancel the job.")
    assert tts.texts[-1] == "Okay, I stopped the todo app job."
    assert session.jobs.job.phase == "cancelled"
    await session.aclose()


@pytest.mark.asyncio
async def test_a_handoff_with_nothing_to_say_still_speaks() -> None:
    class Quiet(ForegroundAgent):
        async def respond_streaming(self, text: str, *, on_event=None):
            self.prompts.append(text)
            yield '<job title="todo app">Build a todo app.</job>'

    job_agent = JobAgent()
    session, _foreground, tts = _call(job_agent, Quiet())
    await session.run_turn(text="Make a todo app.")
    assert tts.texts == [STARTED_LINE, KEEP_TALKING_LINE]
    await session.aclose()


@pytest.mark.asyncio
async def test_no_factory_means_no_jobs_and_no_instruction() -> None:
    foreground = ForegroundAgent()
    tts = TTS()
    route = CascadeRoute(agent=foreground, tts=tts, audio_output=Output())
    session = RealtimeSession(Assembly(
        route=route, policy=VoicePolicy(instant_ack=False, keepalive_interval_s=0),
        config=SimpleNamespace(stt=SimpleNamespace(streaming_timeout_seconds=0.1, rest_timeout_seconds=0.2)),
        agent=foreground,
    ))
    await session.run_turn(text="What's the capital of France?")
    assert session.jobs is None
    assert HANDOFF_INSTRUCTION not in foreground.prompts[0]
    await session.aclose()


@pytest.mark.asyncio
async def test_a_request_the_agent_answers_itself_is_never_said(monkeypatch) -> None:
    monkeypatch.setattr("sleeptalk.jobs._ASK_GRACE_S", 0.05)

    class SelfApproving(JobAgent):
        async def respond_streaming(self, text: str, *, on_event=None):
            on_event(PermissionRequested("p1", "`lsof -iTCP:5173`", frozenset(PermissionDecision)))
            on_event(ToolEvent(name="bash", phase=ToolPhase.STARTED))  # it went ahead
            await asyncio.sleep(0.1)
            on_event(AssistantDelta("Built it."))
            yield "Built it."

    manager, said, _events = _manager(SelfApproving())
    manager.start("todo app", "Build it.")
    await asyncio.sleep(0.3)
    assert said == ["The todo app job is done. Built it."]


@pytest.mark.asyncio
async def test_the_report_is_what_follows_the_last_tool_call() -> None:
    class Narrating(JobAgent):
        async def respond_streaming(self, text: str, *, on_event=None):
            on_event(AssistantDelta("Let me check the CLI first. "))
            on_event(ToolEvent(name="bash", phase=ToolPhase.STARTED))
            on_event(AssistantDelta("Now fixing a bug. "))
            on_event(ToolEvent(name="edit", phase=ToolPhase.STARTED))
            on_event(AssistantDelta("Your todo app is ready and saves to local storage.\n/tmp/todo/index.html"))
            yield "Your todo app is ready and saves to local storage."

    manager, said, _events = _manager(Narrating())
    manager.start("todo app", "Build it.")
    await asyncio.sleep(0.05)
    job = manager.job
    assert job.result == "Your todo app is ready and saves to local storage.\n/tmp/todo/index.html"
    assert job.links == ("/tmp/todo/index.html",)
    assert said == ["The todo app job is done. Your todo app is ready and saves to local storage. The link is in the chat."]


def test_a_long_report_is_not_read_out() -> None:
    import time

    from sleeptalk.jobs import Job

    manager, _said, _events = _manager(JobAgent())
    manager._jobs = [Job(id="job-1", title="todo app", brief="", started_at=0.0, phase="completed",
                         finished_at=time.monotonic(),
                         result="Built a single-file todo app with add, toggle, delete, filters, a clear-completed "
                                "button and local storage, served on port 5173 and verified with a headless browser.",
                         links=("http://localhost:5173",))]
    assert manager.status_line() == "The todo app job is done. The link is in the chat."


@pytest.mark.asyncio
async def test_a_half_request_job_is_replaced_by_the_whole_one() -> None:
    """The caller's sentence went on after the agent had handed off its first
    half: the turn was cut short and asked again, so the second handoff
    replaces the first job instead of being refused."""
    first, second = JobAgent(), JobAgent()
    agents = iter([first, second])
    manager = JobManager(lambda: next(agents), emit=lambda *_: None, announce=lambda _line: None)
    assert manager.start("todo app", "Build a todo app.", turn_id=1, asked="Build me a todo app").line == ""
    await asyncio.wait_for(first.started.wait(), 1)
    manager.turn_cancelled(1)
    replacement = manager.start("local todo app", "Build a todo app. Keep it local.", turn_id=2,
                                asked="Build me a todo app, and keep it all local.")
    assert replacement.line == "" and replacement.job.title == "local todo app"
    await asyncio.wait_for(second.started.wait(), 1)
    await asyncio.sleep(0.05)
    assert first.aborted == 1 and first.closed == 1
    assert manager.active is not None and manager.active.brief == "Build a todo app. Keep it local."
    assert second.prompts == [f"{JOB_CONTRACT}\n\nBuild a todo app. Keep it local."]
    second.release.set()
    await asyncio.sleep(0.05)
    assert manager.job.phase == "completed"
    await manager.close()


@pytest.mark.asyncio
async def test_a_new_request_over_the_handoff_reply_starts_beside_it() -> None:
    """A test call: "create tic tac toe", then, while the agent was still
    saying it had started, "another job: six flags stock valuation". The
    barge-in cut the first handoff turn short, and the second handoff stopped
    the tic tac toe job "for your newer request". A different request runs
    beside it."""
    first, second = JobAgent(), JobAgent()
    agents = iter([first, second])
    manager = JobManager(lambda: next(agents), emit=lambda *_: None, announce=lambda _line: None)
    manager.start("tic tac toe", "Create a tic tac toe game.", turn_id=1, asked="Create a tic tac toe game.")
    await asyncio.wait_for(first.started.wait(), 1)
    manager.turn_cancelled(1)
    manager.start("six flags valuation", "Value Six Flags stock.", turn_id=2,
                  asked="Another job: six flags stock valuation.")
    await asyncio.wait_for(second.started.wait(), 1)
    await asyncio.sleep(0.05)
    assert first.aborted == 0
    assert [(job.title, job.phase) for job in manager.live] == [("tic tac toe", "running"), ("six flags valuation", "running")]
    first.release.set()
    second.release.set()
    await asyncio.sleep(0.05)
    await manager.close()


@pytest.mark.asyncio
async def test_no_announcement_after_a_status_reply_gave_the_news(monkeypatch) -> None:
    monkeypatch.setattr("sleeptalk.session._ANNOUNCE_GAP_S", 0.2)
    job_agent = JobAgent()
    session, _foreground, tts = _call(job_agent)
    await session.run_turn(text="Make a todo app.")
    await asyncio.wait_for(job_agent.started.wait(), 1)
    job_agent.release.set()
    await asyncio.sleep(0.02)
    session.note_caller_speech()  # the caller asks before the announcement plays
    await session.run_turn(text="Is it done yet?")
    assert tts.texts[-1].startswith("The todo app job is done.")
    await asyncio.sleep(0.6)
    assert sum(text.startswith("The todo app job is done.") for text in tts.texts) == 1
    await session.aclose()


@pytest.mark.asyncio
async def test_the_announcement_is_shown_even_right_after_another_reply(monkeypatch) -> None:
    from sleeptalk.presentation import PresentationState, reduce

    monkeypatch.setattr("sleeptalk.session._ANNOUNCE_GAP_S", 0.05)
    job_agent = JobAgent()
    session, _foreground, tts = _call(job_agent)
    state = [PresentationState()]
    session.subscribe(lambda event: state.__setitem__(0, reduce(state[0], event)))
    await session.run_turn(text="Make a todo app.")
    await asyncio.wait_for(job_agent.started.wait(), 1)
    await session.run_turn(text="What's the capital of France?")
    job_agent.release.set()
    for _ in range(40):
        if tts.texts[-1].startswith("The todo app job is done."):
            break
        await asyncio.sleep(0.05)
    shown = [line.text for line in state[0].lines if line.kind == "assistant"]
    assert shown[-1] == "The todo app job is done. Built the app with local storage. The link is in the chat."
    await session.aclose()


def test_narration_glued_to_the_report_is_cut_off() -> None:
    from sleeptalk.jobs import _last_message

    assert _last_message("Continuing the delete/clear checks:Done. Your to-do app is running.") == (
        "Done. Your to-do app is running."
    )
    assert _last_message("Built it, e.g. Tokyo style. See http://localhost:8000/A.") == (
        "Built it, e.g. Tokyo style. See http://localhost:8000/A."
    )


def test_a_bare_done_is_not_the_summary() -> None:
    from sleeptalk.jobs import first_sentences

    assert first_sentences("Done. I built a clean to-do app with local storage.", count=1) == (
        "I built a clean to-do app with local storage."
    )



def test_a_brief_keeps_what_speech_shaping_drops() -> None:
    from sleeptalk.jobs import with_raw_bodies

    raw = (
        "<job title=\"todo app\">Build it in /tmp/claude-501/716355ce-5e90-48cb-aa7b-9e4993ce2917/ws.\n"
        "Use `npm run dev` and **keep it local**.</job> On it. <job-note>Port 8765, commit a1b2c3d4.</job-note>"
    )
    shaped = [StartJob(title="todo app", brief="Build it in /tmp/claude-501/-5e90-48cb-aa7b-/ws. Use and keep it local."),
              JobNote("Port 8765, commit.")]
    taken = [0, 0]
    assert with_raw_bodies(shaped, raw, taken) == [
        StartJob(title="todo app", brief="Build it in /tmp/claude-501/716355ce-5e90-48cb-aa7b-9e4993ce2917/ws.\n"
                                         "Use `npm run dev` and **keep it local**."),
        JobNote("Port 8765, commit a1b2c3d4."),
    ]
    assert taken == [1, 1]
    # No raw text (an adapter without deltas): the shaped brief stands.
    assert with_raw_bodies(shaped[:1], "", [0, 0]) == shaped[:1]



@pytest.mark.asyncio
async def test_a_handoff_passes_the_raw_brief_to_the_job() -> None:
    path = "/tmp/claude-501/716355ce-5e90-48cb-aa7b-9e4993ce2917/ws"

    class Shaping(ForegroundAgent):
        async def respond_streaming(self, text: str, *, on_event=None):
            self.prompts.append(text)
            raw = f"<job title=\"todo app\">Build it in {path} and run `npm test`.</job> On it."
            for piece in (raw[:30], raw[30:]):
                on_event(AssistantDelta(piece))
            # What an adapter's speech shaping yields.
            yield "<job title=\"todo app\">Build it in /tmp/claude-501/-5e90-48cb-aa7b-/ws and run.</job>"
            yield "On it."

    job_agent = JobAgent()
    session, _foreground, _tts = _call(job_agent, Shaping())
    await session.run_turn(text="Make a todo app.")
    await asyncio.wait_for(job_agent.started.wait(), 1)
    assert job_agent.prompts == [f"{JOB_CONTRACT}\n\nBuild it in {path} and run `npm test`."]
    job_agent.release.set()
    await session.aclose()


@pytest.mark.parametrize("typed", [True, False])
@pytest.mark.asyncio
async def test_a_brief_reported_twice_reaches_the_job_once(typed: bool) -> None:
    """Claude Code (and Hermes, Codex, pi) report each chunk typed and as a
    workbench "text" event; a job got every chunk doubled. An adapter that
    sends only the workbench event still gives the raw brief."""
    from sleeptalk.adapter import workbench_event

    class Twice(ForegroundAgent):
        async def respond_streaming(self, text: str, *, on_event=None):
            self.prompts.append(text)
            raw = "<job title=\"merge\">Merge `voice-tool-cue` into main.</job> On it."
            for piece in (raw[:25], raw[25:]):
                if typed:
                    on_event(AssistantDelta(piece))
                on_event(workbench_event("text", text=piece))
            yield "<job title=\"merge\">Merge voice tool cue into main.</job>"
            yield "On it."

    job_agent = JobAgent()
    session, _foreground, _tts = _call(job_agent, Twice())
    await session.run_turn(text="Merge the branches.")
    await asyncio.wait_for(job_agent.started.wait(), 1)
    assert job_agent.prompts == [f"{JOB_CONTRACT}\n\nMerge `voice-tool-cue` into main."]
    job_agent.release.set()
    await session.aclose()


@pytest.mark.asyncio
async def test_the_page_sees_the_job_think_and_the_tools_it_uses() -> None:
    from sleeptalk.adapter import ReasoningDelta

    class Thinker(JobAgent):
        async def respond_streaming(self, text: str, *, on_event=None):
            self.prompts.append(text)
            on_event(ReasoningDelta("The user wants a claw machine. "))
            on_event(ReasoningDelta("Three.js and cannon-es should do."))
            on_event(ToolEvent(name="", phase=ToolPhase.UPDATED, display_name="write_file", label="writing…"))
            on_event(ToolEvent(name="edit", phase=ToolPhase.STARTED, detail="writing index.html",
                               display_name="write_file", label="claw/index.html"))
            self.started.set()
            await self.release.wait()
            yield "Built it."

    agent = Thinker()
    manager, _said, events = _manager(agent)
    manager.start("claw machine", "Build it.")
    await asyncio.wait_for(agent.started.wait(), 1)
    shown = [(name, fields.get("text"), fields.get("steps")) for name, fields in events
             if name in {"job.thinking", "job.step"}]
    # Thinking is shown at most every 1.5 s: the first piece, then the steps.
    assert shown == [
        ("job.thinking", "The user wants a claw machine.", None),
        ("job.step", "write_file · writing…", 0),
        ("job.step", "write_file · claw/index.html", 1),
    ]
    assert all(isinstance(fields.get("startedAt"), int) for name, fields in events if name == "job.step")
    assert "Recent steps: write_file · writing…; write_file · claw/index.html." in manager.context_block()
    agent.release.set()
    await asyncio.sleep(0.05)


def test_only_the_callers_words_can_cancel_a_job() -> None:
    from sleeptalk.jobs import cancel_meant

    assert not cancel_meant("Okay, okay, you can stop now.", "claw machine")
    assert not cancel_meant("Stop talking.", "claw machine")
    assert cancel_meant("Cancel it.", "claw machine")
    assert cancel_meant("Stop the job.", "claw machine")
    assert cancel_meant("Stop the claw machine.", "claw machine")
    assert is_job_status_question("What's the latest thing that the worker has done?")
    assert is_job_status_question("Did anything else come in from the worker?")
    assert not is_job_status_question("Can the worker also add sound?")


@pytest.mark.asyncio
async def test_a_cancel_tag_the_caller_did_not_ask_for_is_ignored() -> None:
    class Overeager(ForegroundAgent):
        async def respond_streaming(self, text: str, *, on_event=None):
            self.prompts.append(text)
            if "todo app" in text.split("\n")[-1]:
                yield "<job title=\"todo app\">Build a todo app.</job> Building it now."
            else:
                yield "<job-cancel/>"

    job_agent = JobAgent()
    session, _foreground, tts = _call(job_agent, Overeager())
    await session.run_turn(text="Make a todo app.")
    await asyncio.wait_for(job_agent.started.wait(), 1)
    await session.run_turn(text="Okay, okay, you can stop now.")
    assert session.jobs.active is not None and job_agent.aborted == 0
    assert tts.texts[-1] == "Okay. The todo app job keeps going."
    outcome = await session.run_turn(text="Actually, cancel the job.")
    assert session.jobs.active is None and job_agent.aborted == 1
    assert outcome.played == ["Stopped the todo app job."]
    await session.aclose()


@pytest.mark.asyncio
async def test_a_job_gets_its_own_time_not_a_spoken_replys() -> None:
    class Timed(JobAgent):
        async def respond_streaming(self, text: str, timeout: float = 300.0, on_event=None):
            self.timeout = timeout
            self.started.set()
            yield "Done with it."

    agent = Timed()
    manager, _said, _events = _manager(agent)
    manager.start("game", "Build a game.")
    await asyncio.wait_for(agent.started.wait(), 1)
    # No limit by default: a Claude Code job was cut off at the old half hour.
    assert agent.timeout > 24 * 3600
    await asyncio.sleep(0.05)

    capped = Timed()
    manager = JobManager(lambda: capped, emit=lambda *_: None, announce=lambda _line: None, max_seconds=600)
    manager.start("game", "Build a game.")
    await asyncio.wait_for(capped.started.wait(), 1)
    assert 590 < capped.timeout <= 600
    await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_a_capped_job_that_runs_out_says_how_long_it_had() -> None:
    agent = JobAgent()
    events: list[tuple[str, dict]] = []
    manager = JobManager(lambda: agent, emit=lambda name, fields: events.append((name, fields)),
                         announce=lambda _line: None, max_seconds=0.05)
    manager.start("game", "Build a game.")
    for _ in range(100):
        if any(name == "job.failed" for name, _ in events):
            break
        await asyncio.sleep(0.01)
    failed = [fields for name, fields in events if name == "job.failed"]
    assert failed and "ran out of time after under a minute" in failed[0]["error"]



def test_the_keep_talking_line_is_said_once_and_only_if_the_agent_did_not() -> None:
    manager, _said, _events = _manager(JobAgent())
    assert manager.keep_talking_line("I'll build that in the background.") == KEEP_TALKING_LINE
    assert manager.keep_talking_line("I'll build that in the background.") == ""  # once per call
    for said in ("On it, we can keep chatting while it builds.", "Handing that off. In the meantime, what's up?",
                 "It's running in the background, so we can keep talking."):
        fresh, _s, _e = _manager(JobAgent())
        assert fresh.keep_talking_line(said) == ""


@pytest.mark.asyncio
async def test_only_the_first_handoff_of_a_call_says_we_can_keep_talking(monkeypatch) -> None:
    first, second = JobAgent(), JobAgent()
    agents = iter([first, second])
    foreground = ForegroundAgent()
    tts = TTS()
    route = CascadeRoute(agent=foreground, tts=tts, audio_output=Output())
    session = RealtimeSession(Assembly(
        route=route, policy=VoicePolicy(instant_ack=False, keepalive_interval_s=0),
        config=SimpleNamespace(stt=SimpleNamespace(streaming_timeout_seconds=0.1, rest_timeout_seconds=0.2)),
        agent=foreground, job_agent_factory=lambda: next(agents),
    ))
    await session.run_turn(text="Make a todo app.")
    assert tts.texts[-1] == KEEP_TALKING_LINE
    first.release.set()
    await asyncio.sleep(0.05)
    spoken = len(tts.texts)
    await session.run_turn(text="Now make another todo app.")
    assert KEEP_TALKING_LINE not in tts.texts[spoken:]
    second.release.set()
    await session.aclose()


@pytest.mark.parametrize("mode", ["local", "snappy", "sentences"])
def test_a_handoff_survives_phrase_streaming_in_every_mode(mode) -> None:
    """Every adapter speaks through SentenceStreamer. Local voices and Snappy
    release ~64-character phrases, and a cut inside `<job title="...">`
    once left the tag unrecognised: no job, and the brief read aloud."""
    from sleeptalk.jobs import JobMarkerFilter
    from sleeptalk.streaming import LOCAL_PHRASE_STREAMING, SNAPPY_PHRASE_STREAMING, SentenceStreamer

    reply = ('<job title="eat work">Goal: pick up and complete any outstanding or queued work in the '
             "sleeptalk project at /Users/developer/projects/sleeptalk, run the tests, and report back what "
             "changed with the file paths.</job> Handed the work to a background job.")
    tokens = [LOCAL_PHRASE_STREAMING.set(mode == "local"), SNAPPY_PHRASE_STREAMING.set(mode == "snappy")]
    try:
        streamer = SentenceStreamer()
        pieces = [p for i in range(0, len(reply), 7) for p in streamer.feed(reply[i:i + 7])]
        pieces += [streamer.flush()] if streamer else []
    finally:
        SNAPPY_PHRASE_STREAMING.reset(tokens[1])
        LOCAL_PHRASE_STREAMING.reset(tokens[0])
    marker = JobMarkerFilter()
    spoken, actions = [], []
    for piece in filter(None, pieces):
        said, found = marker.feed(piece)
        spoken.append(said)
        actions += found
    actions += marker.flush()
    assert " ".join(s for s in spoken if s).strip() == "Handed the work to a background job."
    assert len(actions) == 1 and isinstance(actions[0], StartJob) and actions[0].title == "eat work"
    if mode != "sentences":
        assert any("<job" in p and ">" not in p.split("<job", 1)[1] for p in pieces) or len(pieces) > 2


# -- several jobs at once (docs/plans/concurrent-background-jobs.md) ---------


def _many(*agents: JobAgent, max_jobs: int = 3) -> tuple[JobManager, list[str], list[tuple[str, dict]]]:
    """A manager whose jobs get the given agents in order."""
    said: list[str] = []
    events: list[tuple[str, dict]] = []
    pool = iter(agents)
    manager = JobManager(lambda: next(pool), emit=lambda name, fields: events.append((name, fields)),
                         announce=lambda line, **_: said.append(line() if callable(line) else line),
                         max_jobs=max_jobs)
    return manager, said, events


async def _settle() -> None:
    await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_three_run_at_once_and_a_fourth_queues_until_one_ends() -> None:
    a, b, c, d = JobAgent("A done."), JobAgent("B done."), JobAgent("C done."), JobAgent("D done.")
    manager, _said, events = _many(a, b, c, d)
    for title in ("site", "valuation", "bug fix"):
        assert manager.start(title, f"Do the {title}.").line == ""
    await asyncio.wait_for(asyncio.gather(a.started.wait(), b.started.wait(), c.started.wait()), 1)
    fourth = manager.start("todo app", "Build a todo app.")
    assert fourth.job.queued and d.prompts == []
    assert fourth.line == "Three jobs are already running, so the todo app job will start when one of them finishes."
    assert ("job.queued", fourth.job.title) in [(name, fields["title"]) for name, fields in events]

    b.release.set()
    await asyncio.wait_for(d.started.wait(), 1)
    assert [job.title for job in manager.live] == ["site", "bug fix", "todo app"]
    order = [(name, fields["title"]) for name, fields in events]
    # The finished job's end is out before the queued one's start.
    assert order.index(("job.completed", "valuation")) < order.index(("job.started", "todo app"))
    for agent in (a, c, d):
        agent.release.set()
    await _settle()
    assert {job.title: job.result for job in manager.jobs} == {
        "site": "A done.", "valuation": "B done.", "bug fix": "C done.", "todo app": "D done."}
    await manager.close()


@pytest.mark.asyncio
async def test_the_agent_is_told_to_hand_off_even_at_the_limit() -> None:
    """On a local-speech profile (one job at a time) the agent was told "Up to
    1 jobs can run at once", refused a second handoff itself and promised to
    start it later; nothing did. It must hand off and let the queue wait."""
    for max_jobs, limit in ((1, "one job runs at once"), (3, "three jobs run at once")):
        manager, _said, _events = _many(JobAgent(), JobAgent(), max_jobs=max_jobs)
        manager.start("six flags", "Look up the ticker.")
        block = manager.context_block()
        assert limit in block and "Sleeptalk queues any more and starts each by itself" in block
        assert "Never hold a handoff back" in block and "1 jobs" not in block
        manager.start("repo check", "Check the repo.")
        assert "Never hold a handoff back" in manager.context_block()
        await manager.close()


@pytest.mark.asyncio
async def test_one_job_at_a_time_queues_behind_the_running_one() -> None:
    first, second = JobAgent(), JobAgent()
    manager, _said, _events = _many(first, second, max_jobs=1)
    manager.start("latency run", "Measure it.")
    queued = manager.start("summary bug", "Fix it.")
    assert queued.line == "I'm still working on the latency run job, so the summary bug job will start when it finishes."
    await manager.close()


@pytest.mark.asyncio
async def test_a_queued_job_can_be_cancelled_before_it_begins() -> None:
    first, never = JobAgent(), JobAgent()
    manager, _said, events = _many(first, never, max_jobs=1)
    manager.start("site", "Build it.")
    queued = manager.start("valuation", "Research it.").job
    assert await manager.cancel(queued)
    assert queued.phase == "cancelled" and events[-1][0] == "job.cancelled"
    first.release.set()
    await _settle()
    assert never.prompts == []
    await manager.close()


@pytest.mark.asyncio
async def test_a_new_job_does_not_take_the_selection() -> None:
    manager, _said, _events = _many(JobAgent(), JobAgent())
    first = manager.start("latency run", "Measure it.").job
    manager.start("valuation", "Research it.")
    assert manager.selected is first
    second = manager.jobs[1]
    assert manager.select(second.id) and manager.selected is second
    await manager.close()


@pytest.mark.asyncio
async def test_the_page_hears_when_the_selection_moves() -> None:
    manager, _said, events = _many(JobAgent(), JobAgent())
    first = manager.start("latency run", "Measure it.").job
    second = manager.start("valuation", "Research it.").job
    selected = [fields["jobId"] for name, fields in events if name == "job.selected"]
    assert selected == [first.id], "a new job does not move the selection"
    manager.select(second.id)
    manager.select(second.id)  # no change, no event
    assert [fields["jobId"] for name, fields in events if name == "job.selected"] == [first.id, second.id]
    await manager.close()


@pytest.mark.asyncio
async def test_colliding_titles_are_told_apart() -> None:
    manager, _said, _events = _many(*(JobAgent() for _ in range(4)), max_jobs=4)
    manager.start("todo app", "Build a todo app.")
    assert manager.start("todo app", "Write tests for the todo app.").job.title == "todo app write"
    assert manager.start("todo app", "Build a todo app.").job.title == "todo app build"
    assert manager.start("todo app", "A todo app.").job.title == "todo app 2"
    await manager.close()


@pytest.mark.asyncio
async def test_routing_names_then_only_then_selected() -> None:
    manager, _said, _events = _many(JobAgent(), JobAgent())
    latency = manager.start("latency run", "Measure it.").job
    assert manager.resolve("make it faster") == (latency, "only")
    valuation = manager.start("six flags valuation", "Research it.").job
    assert manager.resolve("tell the valuation to use 2025 numbers") == (valuation, "named")
    assert manager.resolve("", named="latency run") == (latency, "named")
    # Nothing named, nobody asking, nothing just announced: the selected job.
    assert manager.resolve("make it blue") == (latency, "selected")
    assert manager.resolve("cancel the job", kind="cancel") == (latency, "selected")
    await manager.close()


@pytest.mark.asyncio
async def test_a_note_right_after_an_announcement_goes_to_that_job() -> None:
    site, other = JobAgent("The landing page is live."), JobAgent()
    manager, said, _events = _many(site, other)
    manager.start("landing page", "Build it.")
    manager.start("valuation", "Research it.")
    site.release.set()
    await _settle()
    assert said == ["The landing page job is done. The landing page is live."]
    landing = manager.jobs[0]
    assert manager.resolve("actually, make it blue") == (landing, "referent")
    await manager.close()


@pytest.mark.asyncio
async def test_jobs_ending_together_are_said_in_one_line() -> None:
    one, two = JobAgent("Built it.\nhttp://localhost:8000/"), JobAgent("Found it.")
    lines: list[Any] = []
    pool = iter([one, two])
    manager = JobManager(lambda: next(pool), emit=lambda *_: None, announce=lambda line, **_: lines.append(line))
    manager.start("todo app", "Build it.")
    manager.start("valuation", "Research it.")
    one.release.set()
    two.release.set()
    await _settle()
    # The session asks each line for its words only when it is about to speak.
    assert [line() for line in lines] == [
        "The todo app and the valuation jobs are both done. The links are in the chat.", ""]
    await manager.close()


@pytest.mark.asyncio
async def test_a_follow_up_takes_over_its_finished_tab_and_counts(monkeypatch) -> None:
    first, busy, follow = JobAgent("Built it."), JobAgent(), JobAgent("Made it blue.")
    manager, _said, events = _many(first, busy, follow, max_jobs=1)
    done = manager.start("todo app", "Build it.").job
    first.release.set()
    await _settle()
    manager.start("valuation", "Research it.")
    await asyncio.wait_for(busy.started.wait(), 1)
    await manager.note("Make it blue.", job=done)
    follow_up = next(job for job in manager.jobs if job.id == done.id)
    assert follow_up is not done and follow_up.title == "todo app" and follow_up.queued
    busy.release.set()
    await asyncio.wait_for(follow.started.wait(), 1)
    assert "Now the person on the call wants: Make it blue." in follow.prompts[0]
    assert [fields["jobId"] for name, fields in events if name == "job.started"] == [done.id, "job-2", done.id]
    await manager.close()


@pytest.mark.asyncio
async def test_status_for_one_two_and_four_jobs() -> None:
    clock = [1000.0]
    agents = [JobAgent() for _ in range(4)]
    pool = iter(agents)
    manager = JobManager(lambda: next(pool), emit=lambda *_: None, announce=lambda *_a, **_k: None,
                         clock=lambda: clock[0])
    manager.start("latency run", "Measure it.")
    await asyncio.wait_for(agents[0].started.wait(), 1)
    clock[0] += 31 * 60
    assert manager.status_line().startswith("The latency run job is still going, 31 minutes in.")
    manager.start("bug fix", "Fix it.")
    clock[0] += 4 * 60
    assert manager.status_line() == (
        "Two jobs are going: the latency run job, 35 minutes in and the bug fix job, 4 minutes in.")
    assert manager.status_line("how's the bug fix going?").startswith("The bug fix job is still going")
    manager.start("valuation", "Research it.")
    manager.start("todo app", "Build it.")
    status = manager.status_line()
    assert status.startswith("Three jobs are going: the valuation job, under a minute in and the bug fix job")
    assert status.endswith("Ask me about any of them by name. The todo app job is waiting to start.")
    await manager.close()


@pytest.mark.asyncio
async def test_stop_everything_cancels_running_and_queued() -> None:
    manager, _said, _events = _many(JobAgent(), JobAgent(), max_jobs=1)
    manager.start("site", "Build it.")
    manager.start("valuation", "Research it.")
    assert await manager.cancel_all() == 2
    assert {job.phase for job in manager.jobs} == {"cancelled"}
    from sleeptalk.jobs import is_job_cancel_all

    assert is_job_cancel_all("Stop everything.") and is_job_cancel_all("cancel both of them")
    assert is_job_cancel_all("okay cancel all the jobs") and not is_job_cancel_all("stop talking")


class _AsksPermission(JobAgent):
    def __init__(self, prompt: str) -> None:
        super().__init__()
        self.prompt = prompt

    async def respond_streaming(self, text: str, *, on_event=None):
        self.prompts.append(text)
        on_event(PermissionRequested(f"perm-{self.prompt}", self.prompt, frozenset(PermissionDecision)))
        self.started.set()
        await self.release.wait()
        yield "Done."


@pytest.mark.asyncio
async def test_one_question_at_a_time(monkeypatch) -> None:
    monkeypatch.setattr("sleeptalk.jobs._ASK_GRACE_S", 0.01)
    first, second = _AsksPermission("Run npm install"), _AsksPermission("Delete the build folder")
    manager, said, _events = _many(first, second)
    manager.start("site", "Build it.")
    manager.start("cleanup", "Clean it.")
    await _settle()
    # The second request waits while the first is open.
    assert said == ["The site job asks: Run npm install? Say yes or no."]
    assert manager.asking.title == "site"
    assert await manager.answer_permission(PermissionDecision.ALLOW_ONCE)
    await _settle()
    assert said[-1] == "The cleanup job asks: Delete the build folder? Say yes or no."
    assert manager.asking.title == "cleanup"
    await manager.close()


@pytest.mark.asyncio
async def test_a_held_question_is_dropped_when_its_job_moves_on(monkeypatch) -> None:
    monkeypatch.setattr("sleeptalk.jobs._ASK_GRACE_S", 0.01)
    first, second = _AsksPermission("Run npm install"), _AsksPermission("Delete the build folder")
    manager, said, _events = _many(first, second)
    manager.start("site", "Build it.")
    manager.start("cleanup", "Clean it.")
    await _settle()
    second.release.set()  # it carried on (and finished) by itself
    await _settle()
    assert await manager.answer_permission(PermissionDecision.ALLOW_ONCE)
    await _settle()
    assert not any("Delete the build folder" in line for line in said)
    await manager.close()


@pytest.mark.asyncio
async def test_a_cancel_confirmation_and_a_request_never_share_a_yes(monkeypatch) -> None:
    monkeypatch.setattr("sleeptalk.jobs._ASK_GRACE_S", 0.01)
    asking, quiet = _AsksPermission("Run npm install"), JobAgent()
    manager, _said, _events = _many(asking, quiet)
    manager.start("site", "Build it.")
    other = manager.start("valuation", "Research it.").job
    await _settle()
    # A request is open: the confirmation waits for it to be answered.
    assert manager.ask_cancel(other) == "First, the site job is waiting for your answer: it asks: Run npm install? Say yes or no."
    assert manager.confirming is None
    assert await manager.answer_permission(PermissionDecision.DENY)
    assert manager.ask_cancel(other) == "Cancel the valuation job?"
    assert await manager.answer_cancel(True) == "Okay, I stopped the valuation job."
    assert other.phase == "cancelled"
    await manager.close()


@pytest.mark.asyncio
async def test_a_request_waits_while_a_confirmation_is_open(monkeypatch) -> None:
    monkeypatch.setattr("sleeptalk.jobs._ASK_GRACE_S", 0.01)
    quiet, asking = JobAgent(), _AsksPermission("Run npm install")
    manager, said, _events = _many(quiet, asking)
    first = manager.start("valuation", "Research it.").job
    assert manager.ask_cancel(first) == "Cancel the valuation job?"
    manager.start("site", "Build it.")
    await _settle()
    assert said == []
    assert await manager.answer_cancel(False) == "Okay, the valuation job keeps going."
    await _settle()
    assert said == ["The site job asks: Run npm install? Say yes or no."]
    await manager.close()


def test_markers_name_their_job_and_jobs_do_not_commit() -> None:
    marker = JobMarkerFilter()
    spoken, actions = marker.feed('<job-note job="todo app">Make it blue.</job-note> <job-cancel job="site"/> Done.')
    assert spoken == "Done."
    assert actions == [JobNote("Make it blue.", "todo app"), CancelJob("site")]
    assert "Do not make git commits" in JOB_CONTRACT


def test_how_many_jobs_a_profile_runs() -> None:
    from sleeptalk.policy import max_background_jobs_for

    def config(stt: str = "deepgram", tts: str = "cartesia", configured: int | None = None) -> Any:
        return SimpleNamespace(stt=SimpleNamespace(provider=stt), tts=SimpleNamespace(provider=tts),
                               policy=SimpleNamespace(max_background_jobs=configured))

    assert max_background_jobs_for(config()) == 3
    # Local speech too: a planned default of 1 surprised a caller.
    assert max_background_jobs_for(config(stt="local", tts="local")) == 3
    assert max_background_jobs_for(config(stt="local", configured=2)) == 2


class TwoJobForeground(ForegroundAgent):
    """Hands off two jobs, passes unnamed notes on, cancels when told."""

    async def respond_streaming(self, text: str, *, on_event=None):
        self.prompts.append(text)
        asked = text.split("\n")[-1]
        if "todo app" in asked:
            yield '<job title="todo app">Build a todo app.</job> On it.'
        elif "valuation" in asked:
            yield '<job title="valuation">Value Six Flags.</job> Starting that.'
        elif "blue" in asked:
            yield "<job-note>Make it blue.</job-note> Passed that on."
        elif "cancel" in asked.lower():
            yield "<job-cancel/> Stopped it."
        else:
            yield "Paris is the capital of France."


@pytest.mark.asyncio
async def test_a_call_confirms_a_bare_cancel_and_names_where_a_note_went(monkeypatch) -> None:
    monkeypatch.setattr("sleeptalk.session._ANNOUNCE_GAP_S", 0.05)
    todo, valuation = JobAgent(), JobAgent()
    foreground = TwoJobForeground()
    tts = TTS()
    route = CascadeRoute(agent=foreground, tts=tts, audio_output=Output())
    agents = iter([todo, valuation])
    session = RealtimeSession(Assembly(
        route=route, policy=VoicePolicy(instant_ack=False, keepalive_interval_s=0),
        config=SimpleNamespace(stt=SimpleNamespace(streaming_timeout_seconds=0.1, rest_timeout_seconds=0.2)),
        agent=foreground, job_agent_factory=lambda: next(agents),
    ))
    await session.run_turn(text="Make a todo app.")
    await session.run_turn(text="And a valuation of Six Flags.")
    await asyncio.wait_for(asyncio.gather(todo.started.wait(), valuation.started.wait()), 1)
    assert [job.title for job in session.jobs.live] == ["todo app", "valuation"]

    # A note naming nothing goes to the selected job, and says which.
    await session.run_turn(text="Make it blue.")
    assert "That's for the todo app job." in tts.texts
    assert 'Background jobs, 2 going' in foreground.prompts[-1]

    # "Cancel the job" with two going: confirmed by name, and only a yes stops it.
    asked = len(foreground.prompts)
    await session.run_turn(text="Cancel the job.")
    assert len(foreground.prompts) == asked
    assert tts.texts[-1] == "Cancel the todo app job?"
    await session.run_turn(text="Yes.")
    assert tts.texts[-1] == "Okay, I stopped the todo app job."
    assert [job.title for job in session.jobs.live] == ["valuation"]

    # Stop everything: no question.
    await session.run_turn(text="Stop everything.")
    assert tts.texts[-1] == "Okay, I stopped the valuation job."
    assert session.jobs.live == []
    await session.aclose()
