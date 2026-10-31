"""Local-intent presence checks: classifier table and cascade skip-agent path."""

from __future__ import annotations

import asyncio
import time

import pytest

from sleeptalk.routes.base import RouteInput, TurnContext
from sleeptalk.routes.cascade import CascadeRoute
from sleeptalk.session_events import SessionEventType, Speaker
from sleeptalk.voice.speech_patterns import classify_local_intent


IDLE_CASES = (
    ("hello", "Hi, I'm here."),
    ("Hello?", "Hi, I'm here."),
    ("  HELLO  ", "Hi, I'm here."),
    ("can you hear me", "Yep, I can hear you."),
    ("Can you hear me?", "Yep, I can hear you."),
    ("can   you  hear  me", "Yep, I can hear you."),
    ("are you there", "Yep, I'm here."),
    ("Are you there?", "Yep, I'm here."),
    ("you there", "Yep, I'm here."),
    ("You there?", "Yep, I'm here."),
    ("is this working", "Yep, I'm here and I can hear you."),
    ("Is this working?", "Yep, I'm here and I can hear you."),
    ("hello? are you there?", "Yep, I'm here."),
)

PASS_THROUGH = (
    "Can you hear me out?",
    "Can you hear what I said about the deployment?",
    "Are you there yet in the file?",
    "Hello? Can you explain this function?",
    "Is this working directory clean?",
    "stop the deployment",
    "cancel the last commit please",
    "wait what does this function do",
    "don't stop",
)

HALT_CASES = (
    "stop",
    "Stop!",
    "please stop",
    "okay stop",
    "cancel",
    "never mind",
    "that's enough",
    "slash stop",
    "Pipeline control slash Stop.",
    "pi bot control slash stop",
)

HALT_REPLY = "Stopped."

BUSY_REPLY = "Yep, I'm here, still working on it"


class FakeAgent:
    name = "FakeAgent"

    def __init__(self, sentences: list[str], *, delay: float = 0.001) -> None:
        self._sentences = sentences
        self._delay = delay
        self.respond_calls: list[str] = []

    async def respond_streaming(self, text: str, *, on_event=None):
        self.respond_calls.append(text)
        for sentence in self._sentences:
            await asyncio.sleep(self._delay)
            yield sentence

    async def abort(self) -> None:
        return None

    async def warmup(self) -> None:
        return None

    async def close(self) -> None:
        return None


class GuardAgent(FakeAgent):
    async def respond_streaming(self, text: str, *, on_event=None):
        self.respond_calls.append(text)
        raise AssertionError("agent should not run for local intent")
        yield "unreachable"


class FakeTTS:
    def __init__(self) -> None:
        self.texts: list[str] = []

    async def synthesize(self, text: str):
        self.texts.append(text)
        yield b"\x01" * 96


class FakeOutput:
    def __init__(self) -> None:
        self.writes: list[tuple[bytes, int]] = []

    async def write(self, pcm, turn_id):
        self.writes.append((pcm, turn_id))

    async def close(self):
        return None


def make_ctx(route: CascadeRoute, turn_id: int = 1) -> TurnContext:
    events: list = []

    def emit(event) -> None:
        events.append(event)

    ctx = TurnContext(session_id="cascade-s-1", turn_id=turn_id, emit=emit)
    ctx.extras["events"] = events
    route.bind("cascade-s-1", emit)
    return ctx


async def _speak_noop(sentence: str, turn: int) -> None:
    return None


def _user_transcripts(ctx: TurnContext) -> list:
    return [
        e
        for e in ctx.extras["events"]
        if e.type == SessionEventType.TRANSCRIPT_FINAL
        and e.payload.get("speaker") == Speaker.USER.value
    ]


def _response_types(ctx: TurnContext) -> list[SessionEventType]:
    wanted = {
        SessionEventType.RESPONSE_STARTED,
        SessionEventType.RESPONSE_FIRST_OUTPUT,
        SessionEventType.RESPONSE_COMPLETED,
        SessionEventType.RESPONSE_CANCELLED,
    }
    return [e.type for e in ctx.extras["events"] if e.type in wanted]


def _agent_stream_attempts(ctx: TurnContext) -> list:
    return [
        e
        for e in ctx.extras["events"]
        if e.type
        in (SessionEventType.USAGE_ATTEMPT_STARTED, SessionEventType.USAGE_ATTEMPT_FINISHED)
        and e.payload.get("operation") == "agent.stream"
    ]


@pytest.mark.parametrize(("utterance", "reply"), IDLE_CASES)
def test_classify_idle_phrases(utterance: str, reply: str) -> None:
    local = classify_local_intent(utterance)
    assert local is not None
    assert local.intent == "presence_check"
    assert local.reply == reply


@pytest.mark.parametrize("utterance", PASS_THROUGH)
def test_classify_pass_through_is_none(utterance: str) -> None:
    assert classify_local_intent(utterance) is None


@pytest.mark.parametrize(("utterance", "_reply"), IDLE_CASES)
def test_classify_busy_uses_one_reply(utterance: str, _reply: str) -> None:
    local = classify_local_intent(utterance, busy=True)
    assert local is not None
    assert local.intent == "presence_check"
    assert local.reply == BUSY_REPLY


@pytest.mark.parametrize("utterance", PASS_THROUGH)
def test_classify_busy_pass_through_is_none(utterance: str) -> None:
    assert classify_local_intent(utterance, busy=True) is None


@pytest.mark.parametrize("utterance", HALT_CASES)
def test_classify_halt_phrases(utterance: str) -> None:
    local = classify_local_intent(utterance)
    assert local is not None
    assert local.intent == "halt"
    assert local.reply == HALT_REPLY


@pytest.mark.parametrize("utterance", HALT_CASES)
def test_classify_halt_ignores_busy(utterance: str) -> None:
    local = classify_local_intent(utterance, busy=True)
    assert local is not None
    assert local.intent == "halt"
    assert local.reply == HALT_REPLY


@pytest.mark.asyncio
async def test_cascade_presence_skips_agent() -> None:
    agent = GuardAgent([])
    spoken: list[str] = []
    route = CascadeRoute(
        agent=agent,
        tts=None,
        speak=lambda sentence, turn: _record(spoken, sentence),
    )
    await route.prepare(session=None)
    ctx = make_ctx(route)
    outcome = await route.run_turn(RouteInput(text="Hello?"), ctx)

    assert outcome.status == "completed"
    assert outcome.played == ["Hi, I'm here."]
    assert outcome.generated == ["Hi, I'm here."]
    assert spoken == ["Hi, I'm here."]
    assert agent.respond_calls == []
    assert _agent_stream_attempts(ctx) == []

    user = _user_transcripts(ctx)[0]
    assert user.payload["source"] == "local_intent"
    assert user.payload["intent"] == "presence_check"

    responses = [
        e
        for e in ctx.extras["events"]
        if e.type
        in {
            SessionEventType.RESPONSE_STARTED,
            SessionEventType.RESPONSE_FIRST_OUTPUT,
            SessionEventType.RESPONSE_COMPLETED,
        }
    ]
    assert [e.type for e in responses] == [
        SessionEventType.RESPONSE_STARTED,
        SessionEventType.RESPONSE_FIRST_OUTPUT,
        SessionEventType.RESPONSE_COMPLETED,
    ]
    for event in responses:
        assert event.payload["source"] == "local_intent"
        assert event.payload["intent"] == "presence_check"
    assert responses[1].payload["kind"] == "audio"
    await route.close()


async def _record(sink: list, item) -> None:
    sink.append(item)


@pytest.mark.asyncio
async def test_cascade_near_miss_calls_agent() -> None:
    agent = FakeAgent(["Looking at the file."])
    spoken: list[str] = []
    route = CascadeRoute(
        agent=agent,
        tts=None,
        speak=lambda sentence, turn: _record(spoken, sentence),
    )
    await route.prepare(session=None)
    ctx = make_ctx(route)
    outcome = await route.run_turn(
        RouteInput(text="Are you there yet in the file?"), ctx
    )

    assert agent.respond_calls == ["Are you there yet in the file?"]
    assert outcome.played == ["Looking at the file."]
    assert spoken == ["Looking at the file."]
    user = _user_transcripts(ctx)[0]
    assert user.payload.get("source") != "local_intent"
    assert "intent" not in user.payload
    assert _agent_stream_attempts(ctx)
    await route.close()


@pytest.mark.asyncio
async def test_cascade_busy_presence_skips_agent() -> None:
    agent = GuardAgent([])
    spoken: list[str] = []
    route = CascadeRoute(
        agent=agent,
        tts=None,
        speak=lambda sentence, turn: _record(spoken, sentence),
    )
    await route.prepare(session=None)
    ctx = make_ctx(route)
    ctx.extras["agent_task_active"] = True
    outcome = await route.run_turn(RouteInput(text="Are you there?"), ctx)

    assert outcome.played == [BUSY_REPLY]
    assert spoken == [BUSY_REPLY]
    assert agent.respond_calls == []
    assert _agent_stream_attempts(ctx) == []
    await route.close()


@pytest.mark.asyncio
async def test_cascade_presence_beats_slow_agent() -> None:
    agent = FakeAgent(["too late"], delay=2.0)
    route = CascadeRoute(agent=agent, tts=None, speak=_speak_noop)
    await route.prepare(session=None)
    ctx = make_ctx(route)
    started = time.perf_counter()
    outcome = await route.run_turn(RouteInput(text="Hello?"), ctx)
    elapsed = time.perf_counter() - started

    assert outcome.played == ["Hi, I'm here."]
    assert elapsed < 1.0
    assert agent.respond_calls == []
    assert _agent_stream_attempts(ctx) == []
    assert SessionEventType.RESPONSE_FIRST_OUTPUT in _response_types(ctx)
    await route.close()


@pytest.mark.asyncio
async def test_cascade_halt_skips_agent() -> None:
    agent = GuardAgent([])
    spoken: list[str] = []
    route = CascadeRoute(
        agent=agent,
        tts=None,
        speak=lambda sentence, turn: _record(spoken, sentence),
    )
    await route.prepare(session=None)
    ctx = make_ctx(route)
    outcome = await route.run_turn(
        RouteInput(text="Pipeline control slash Stop."), ctx
    )

    assert outcome.status == "completed"
    assert outcome.played == [HALT_REPLY]
    assert spoken == [HALT_REPLY]
    assert agent.respond_calls == []
    assert _agent_stream_attempts(ctx) == []
    user = _user_transcripts(ctx)[0]
    assert user.payload["source"] == "local_intent"
    assert user.payload["intent"] == "halt"
    await route.close()


@pytest.mark.asyncio
async def test_cascade_presence_tts_speaks_canned_reply() -> None:
    agent = GuardAgent([])
    tts = FakeTTS()
    route = CascadeRoute(agent=agent, tts=tts, audio_output=FakeOutput())
    await route.prepare(session=None)
    ctx = make_ctx(route)
    outcome = await route.run_turn(RouteInput(text="Hello?"), ctx)

    assert outcome.played == ["Hi, I'm here."]
    assert tts.texts == ["Hi, I'm here."]
    assert agent.respond_calls == []
    tts_started = [
        e
        for e in ctx.extras["events"]
        if e.type == SessionEventType.USAGE_ATTEMPT_STARTED
        and e.payload.get("operation") == "tts.stream"
    ]
    assert tts_started
    assert _agent_stream_attempts(ctx) == []
    await route.close()
