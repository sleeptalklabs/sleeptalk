"""Tests for CascadeRoute usage events (r2-cascade-usage).

Covers: success, cancellation, and duplicate-settling semantics of the
agent.stream usage attempts emitted by the cascade route; identity
correlation (session/turn/provider/operation/attempt); raw-unit
preservation; idempotency-key shape.
"""

from __future__ import annotations

import asyncio

import pytest

from sleeptalk.routes.base import RouteInput, TurnContext
from sleeptalk.routes.cascade import CascadeRoute
from sleeptalk.session_events import (
    AttemptStatus,
    SessionEventType,
    Speaker,
)


class FakeAgent:
    name = "FakeAgent"

    def __init__(self, sentences: list[str], *, delay: float = 0.001) -> None:
        self._sentences = sentences
        self._delay = delay

    async def respond_streaming(self, text: str, *, on_event=None):
        for sentence in self._sentences:
            await asyncio.sleep(self._delay)
            yield sentence

    async def abort(self) -> None:
        return None

    async def warmup(self) -> None:
        return None

    async def close(self) -> None:
        return None


def make_ctx(route: CascadeRoute, turn_id: int = 1) -> TurnContext:
    events: list = []

    def emit(event) -> None:
        events.append(event)

    ctx = TurnContext(session_id="cascade-s-1", turn_id=turn_id, emit=emit)
    ctx.extras["events"] = events
    route.bind("cascade-s-1", emit)
    return ctx


def usage_events(ctx) -> dict:
    started = [
        e for e in ctx.extras["events"] if e.type == SessionEventType.USAGE_ATTEMPT_STARTED
    ]
    finished = [
        e for e in ctx.extras["events"] if e.type == SessionEventType.USAGE_ATTEMPT_FINISHED
    ]
    return {"started": started, "finished": finished}


async def _speak_noop(sentence: str, turn: int) -> None:
    return None


@pytest.mark.asyncio
async def test_success_emits_started_then_finished_with_identity() -> None:
    route = CascadeRoute(agent=FakeAgent(["Hello.", "World."]), tts=None)
    await route.prepare(session=None)
    ctx = make_ctx(route)
    outcome = await route.run_turn(RouteInput(text="hi there"), ctx)

    assert outcome.status == "completed"
    events = usage_events(ctx)
    assert len(events["started"]) == 1
    assert len(events["finished"]) == 1

    started = events["started"][0]
    finished = events["finished"][0]
    # identity of record: session, turn, provider, operation, attempt
    assert started.session_id == "cascade-s-1"
    assert started.turn_id == 1
    assert started.attempt_id == finished.attempt_id
    assert "fakeagent" in started.attempt_id  # provider identity in idempotency key
    assert "agent.stream" in started.attempt_id
    assert started.payload["provider"] == "FakeAgent"
    assert started.payload["operation"] == "agent.stream"
    assert started.payload["paymentMode"] == "byok"
    # status + raw units on finish
    assert finished.payload["status"] == AttemptStatus.OK.value
    assert finished.payload["units"]["inputCharacters"] == len("hi there")
    assert finished.payload["units"]["outputCharacters"] == len("Hello.") + len("World.")
    await route.close()


@pytest.mark.asyncio
async def test_cancellation_settles_attempt_once_as_cancelled() -> None:
    class Slow(FakeAgent):
        async def respond_streaming(self, text: str, *, on_event=None):
            for sentence in self._sentences:
                await asyncio.sleep(0.05)
                yield sentence

    route = CascadeRoute(agent=Slow(["One.", "Two."]), tts=None, speak=_speak_noop)
    await route.prepare(session=None)
    ctx = make_ctx(route)
    turn_task = asyncio.create_task(route.run_turn(RouteInput(text="q"), ctx))
    await asyncio.sleep(0.08)
    await route.cancel_turn(1)
    outcome = await turn_task

    assert outcome.status == "cancelled"
    events = usage_events(ctx)
    # exactly one start and one finish — cancelled attempts settle once
    assert len(events["started"]) == 1
    assert len(events["finished"]) == 1
    assert events["finished"][0].payload["status"] == AttemptStatus.CANCELLED.value
    # raw units reflect partial generation
    assert events["finished"][0].payload["units"]["outputCharacters"] == len("One.")
    await route.close()


@pytest.mark.asyncio
async def test_task_cancelled_path_settles_attempt() -> None:
    """External task cancellation (close during a turn) also settles the
    attempt — never leaves an open usage attempt behind."""

    class Stalled(FakeAgent):
        async def respond_streaming(self, text: str, *, on_event=None):
            await asyncio.sleep(3600)
            yield "never"

    route = CascadeRoute(agent=Stalled([]), tts=None)
    await route.prepare(session=None)
    ctx = make_ctx(route)
    turn_task = asyncio.create_task(route.run_turn(RouteInput(text="q"), ctx))
    await asyncio.sleep(0.02)
    await route.close()  # cancels the in-flight turn
    await turn_task

    events = usage_events(ctx)
    assert len(events["started"]) == 1
    assert len(events["finished"]) == 1
    assert events["finished"][0].payload["status"] == AttemptStatus.CANCELLED.value


@pytest.mark.asyncio
async def test_no_duplicate_events_across_turns() -> None:
    route = CascadeRoute(agent=FakeAgent(["A."]), tts=None)
    await route.prepare(session=None)
    ctx1 = make_ctx(route, 1)
    ctx2 = make_ctx(route, 2)
    await route.run_turn(RouteInput(text="one"), ctx1)
    await route.run_turn(RouteInput(text="two"), ctx2)

    for ctx in (ctx1, ctx2):
        events = usage_events(ctx)
        assert len(events["started"]) == 1
        assert len(events["finished"]) == 1
    # distinct attempt ids per turn (idempotency key carries turn)
    id1 = usage_events(ctx1)["started"][0].attempt_id
    id2 = usage_events(ctx2)["started"][0].attempt_id
    assert id1 != id2
    await route.close()


@pytest.mark.asyncio
async def test_usage_events_carry_no_content() -> None:
    """Usage records must never become a conversation log."""
    route = CascadeRoute(agent=FakeAgent(["Secret sentence."]), tts=None)
    await route.prepare(session=None)
    ctx = make_ctx(route)
    await route.run_turn(RouteInput(text="private prompt"), ctx)

    import json

    # Transcripts legitimately carry text; usage records must not.
    usage_only = [e for e in ctx.extras["events"] if e.type in (
        SessionEventType.USAGE_ATTEMPT_STARTED,
        SessionEventType.USAGE_ATTEMPT_FINISHED,
    )]
    serialized = json.dumps([e.to_json() for e in usage_only])
    assert usage_only, "expected usage events"
    assert "private prompt" not in serialized
    assert "Secret sentence" not in serialized
    await route.close()


# -- audit rework: STT/agent/TTS attempts, exactly-once settlement ------------


class UsageSTT:
    def __init__(self) -> None:
        self.calls = 0

    async def transcribe_pcm(self, pcm: bytes, *, purpose: str = "") -> str:
        self.calls += 1
        return "usage transcript"


class UsageTTS:
    def __init__(self, fail=False) -> None:
        self.fail = fail
        self.synthesized = 0

    async def synthesize(self, text: str):
        self.synthesized += 1
        if self.fail:
            raise RuntimeError("tts exploded")
        yield b"\x01" * 96


class UsageOutput:
    def __init__(self) -> None:
        self.writes = []
        from sleeptalk.routes.audio_output import AudioOutputAccounting

        self._accounting = AudioOutputAccounting(turn_id=-1)

    async def write(self, pcm, turn_id):
        self.writes.append((pcm, turn_id))
        self._accounting.written(len(pcm))

    def accounting(self):
        return self._accounting

    async def close(self):
        return None


class UsageAgent:
    name = "UsageAgent"

    def __init__(self, fail=False) -> None:
        self.fail = fail

    async def respond_streaming(self, text, *, on_event=None):
        if self.fail:
            raise RuntimeError("agent exploded")
        yield "Answer sentence."

    async def abort(self):
        return None

    async def warmup(self):
        return None

    async def close(self):
        return None


def usage_ctx(route, turn_id=1):
    from sleeptalk.routes.base import TurnContext

    events = []

    def emit(event):
        events.append(event)

    ctx = TurnContext(session_id="usage-s", turn_id=turn_id, emit=emit)
    ctx.extras["events"] = events
    route.bind("usage-s", emit)
    return ctx


@pytest.mark.asyncio
async def test_all_three_operations_metered_with_units() -> None:
    """STT + agent + TTS each emit start/finish with correlated identity
    and raw units on success."""
    route = CascadeRoute(
        agent=UsageAgent(),
        tts=UsageTTS(),
        stt=UsageSTT(),
        audio_output=UsageOutput(),
    )
    await route.prepare(session=None)
    ctx = usage_ctx(route)
    outcome = await route.run_turn(RouteInput(pcm=b"\x00" * 192), ctx)
    assert outcome.status == "completed"

    started = [e for e in ctx.extras["events"] if e.type == SessionEventType.USAGE_ATTEMPT_STARTED]
    finished = [e for e in ctx.extras["events"] if e.type == SessionEventType.USAGE_ATTEMPT_FINISHED]
    operations_started = sorted({e.payload["operation"] for e in started})
    assert operations_started == ["agent.stream", "stt.stream", "tts.stream"]
    # one start + one finish per attempt
    assert len(started) == len(finished)
    # raw units preserved
    stt_finish = next(e for e in finished if e.payload["operation"] == "stt.stream")
    assert stt_finish.payload["units"]["inputCharacters"] == len("usage transcript")
    tts_finish = next(e for e in finished if e.payload["operation"] == "tts.stream")
    assert tts_finish.payload["units"]["outputAudioBytes"] == 96
    agent_finish = next(e for e in finished if e.payload["operation"] == "agent.stream")
    assert agent_finish.payload["status"] == "ok"
    # identity: session/turn/operation correlation in every attempt id
    for e in started:
        assert e.attempt_id.startswith("usage-s:1:")
        assert e.turn_id == 1
    await route.close()


@pytest.mark.asyncio
async def test_agent_error_settles_attempt() -> None:
    """Exceptions settle the open agent attempt with status error."""
    route = CascadeRoute(agent=UsageAgent(fail=True), tts=None)
    await route.prepare(session=None)
    ctx = usage_ctx(route)
    with pytest.raises(RuntimeError, match="agent exploded"):
        await route.run_turn(RouteInput(text="q"), ctx)
    finished = [e for e in ctx.extras["events"] if e.type == SessionEventType.USAGE_ATTEMPT_FINISHED]
    agent_finish = next(e for e in finished if e.payload["operation"] == "agent.stream")
    assert agent_finish.payload["status"] == "error"
    assert agent_finish.payload["units"]["errorType"] == "RuntimeError"
    await route.close()


@pytest.mark.asyncio
async def test_tts_error_settles_attempt() -> None:
    route = CascadeRoute(
        agent=UsageAgent(), tts=UsageTTS(fail=True), audio_output=UsageOutput()
    )
    await route.prepare(session=None)
    ctx = usage_ctx(route)
    with pytest.raises(RuntimeError, match="tts exploded"):
        await route.run_turn(RouteInput(text="q"), ctx)
    finished = [e for e in ctx.extras["events"] if e.type == SessionEventType.USAGE_ATTEMPT_FINISHED]
    tts_finish = next(e for e in finished if e.payload["operation"] == "tts.stream")
    assert tts_finish.payload["status"] == "error"
    await route.close()


@pytest.mark.asyncio
async def test_cancellation_settles_every_open_attempt_exactly_once() -> None:
    """Barge-in settles STT/agent/TTS attempts exactly once each."""
    stt = UsageSTT()

    class SlowAgent(UsageAgent):
        async def respond_streaming(self, text, *, on_event=None):
            yield "First."
            await asyncio.sleep(0.05)
            yield "Second."

    route = CascadeRoute(
        agent=SlowAgent(), tts=UsageTTS(), stt=stt, audio_output=UsageOutput()
    )
    await route.prepare(session=None)
    ctx = usage_ctx(route)
    turn_task = asyncio.create_task(route.run_turn(RouteInput(pcm=b"\x00" * 96), ctx))
    await asyncio.sleep(0.02)
    await route.cancel_turn(1)
    outcome = await asyncio.wait_for(turn_task, timeout=3)
    assert outcome.status == "cancelled"

    finished = [e for e in ctx.extras["events"] if e.type == SessionEventType.USAGE_ATTEMPT_FINISHED]
    # exactly-once: no attempt id appears twice
    ids = [e.attempt_id for e in finished]
    assert len(ids) == len(set(ids)), "duplicate settlement"
    # every started attempt was settled
    started = [e for e in ctx.extras["events"] if e.type == SessionEventType.USAGE_ATTEMPT_STARTED]
    assert sorted(ids) == sorted(e.attempt_id for e in started)
    # cancelled attempts carry the cancelled status
    for e in finished:
        if e.payload["status"] != "ok":
            assert e.payload["status"] in {"cancelled", "error"}
    await route.close()
