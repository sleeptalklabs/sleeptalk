"""Tests for sleeptalk.routes.cascade (r2-cascade-core).

Drives the cascade route with deterministic fake STT/agent/TTS streaming
per the card verification. Covers: happy-path streaming through the route
protocol, normalized event boundary (response lifecycle + transcript),
agent/provider identity observability, no-surface-import isolation,
lifecycle determinism.
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

from sleeptalk.routes.base import (
    InterruptionGranularity,
    RouteInput,
    RouteStateError,
    RouteType,
    TurnContext,
)
from sleeptalk.routes.cascade import CascadeRoute
from sleeptalk.session_events import SessionEventType, Speaker


class FakeAgent:
    """Deterministic streaming agent: yields sentences with small delays."""

    name = "FakeAgent"

    def __init__(self, sentences: list[str], *, delay: float = 0.001) -> None:
        self._sentences = sentences
        self._delay = delay
        self.aborted = 0
        self.closed = False
        self.warmed_up = False

    async def respond_streaming(self, text: str, *, on_event=None):
        for sentence in self._sentences:
            await asyncio.sleep(self._delay)
            yield sentence

    async def abort(self) -> None:
        self.aborted += 1

    async def warmup(self) -> None:
        self.warmed_up = True

    async def close(self) -> None:
        self.closed = True


class FakeTTS:
    name = "FakeTTS"
    capabilities = None

    async def synthesize(self, text: str):
        yield text.encode()


def test_cascade_route_has_no_surface_imports() -> None:
    import ast
    import sleeptalk.routes.cascade as mod

    tree = ast.parse(inspect.getsource(mod))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    allowed_roots = (
        "sleeptalk",
        "asyncio",
        "collections",
        "contextlib",
        "time",
        "logging",
        "inspect",
        "typing",
        "__future__",
    )
    assert all(imp.split(".")[0] in allowed_roots for imp in imported), (
        f"unexpected imports: {sorted(imported)}"
    )


def make_ctx(route: CascadeRoute, turn_id: int = 1) -> TurnContext:
    events: list = []

    def emit(event) -> None:
        events.append(event)

    ctx = TurnContext(session_id="cascade-s-1", turn_id=turn_id, emit=emit)
    ctx.extras["events"] = events
    route.bind("cascade-s-1", emit)
    return ctx


def assistant_texts(ctx: TurnContext) -> list[str]:
    return [
        e.payload["text"]
        for e in ctx.extras["events"]
        if e.type == SessionEventType.TRANSCRIPT_FINAL
        and e.payload.get("speaker") == Speaker.ASSISTANT.value
    ]


def interruption_payloads(ctx: TurnContext) -> list[dict]:
    return [
        e.payload
        for e in ctx.extras["events"]
        if e.type == SessionEventType.INTERRUPTION
    ]


# -- isolation -------------------------------------------------------------


# -- happy-path streaming --------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_streams_through_protocol() -> None:
    agent = FakeAgent(["First sentence.", "Second sentence."])
    spoken: list[str] = []
    route = CascadeRoute(
        agent=agent,
        tts=FakeTTS(),
        speak=lambda sentence, turn: _record(spoken, sentence),
    )
    await route.prepare(session=None)
    assert agent.warmed_up

    ctx = make_ctx(route)
    outcome = await route.run_turn(RouteInput(text="hi there"), ctx)

    assert outcome.status == "completed"
    assert outcome.played == ["First sentence.", "Second sentence."]
    assert outcome.generated == ["First sentence.", "Second sentence."]
    assert spoken == ["First sentence.", "Second sentence."]

    types = [e.type for e in ctx.extras["events"]]
    assert types == [
        SessionEventType.TRANSCRIPT_FINAL,  # user turn committed
        SessionEventType.RESPONSE_STARTED,  # turn-level response lifecycle
        SessionEventType.USAGE_ATTEMPT_STARTED,  # agent.stream metering (r2-cascade-usage)
        SessionEventType.AGENT_EVENT,  # first sentence painted on the Frame
        SessionEventType.RESPONSE_FIRST_OUTPUT,  # first answer crossed the route boundary
        SessionEventType.AGENT_EVENT,  # second sentence painted on the Frame
        SessionEventType.TRANSCRIPT_FINAL,  # assistant played
        SessionEventType.RESPONSE_COMPLETED,
        SessionEventType.USAGE_ATTEMPT_FINISHED,
    ]
    user_event = ctx.extras["events"][0]
    assert user_event.payload["speaker"] == Speaker.USER.value
    await route.close()
    assert agent.closed


async def _record(sink: list, item) -> None:
    sink.append(item)


@pytest.mark.asyncio
async def test_partial_output_accounting_when_cancelled() -> None:
    """Played vs generated at sentence granularity (ADR 0003 §4 clause 3)."""
    agent = FakeAgent(["One.", "Two.", "Three."], delay=0.05)
    spoken: list[str] = []
    route = CascadeRoute(
        agent=agent,
        tts=FakeTTS(),
        speak=lambda sentence, turn: _record(spoken, sentence),
    )
    await route.prepare(session=None)
    ctx = make_ctx(route)
    turn_task = asyncio.create_task(route.run_turn(RouteInput(text="q"), ctx))
    await asyncio.sleep(0.12)  # let ~2 sentences play
    await route.cancel_turn(1)
    outcome = await turn_task

    assert outcome.status == "cancelled"
    assert outcome.played == spoken
    assert len(outcome.generated) >= len(outcome.played)
    # never report generated as played: same list identity is fine only when
    # nothing was cut; here the cut guarantees the distinction is testable
    interruption_events = [
        e for e in ctx.extras["events"] if e.type == SessionEventType.INTERRUPTION
    ]
    assert interruption_events, "cancel must emit an interruption event"
    assert agent.aborted == 1  # upstream work aborted


@pytest.mark.asyncio
async def test_cancel_commits_played_sentences_not_generated() -> None:
    agent = FakeAgent(["One.", "Two."], delay=0.01)
    second_started = asyncio.Event()

    async def speak(sentence: str, turn: int) -> None:
        if sentence == "Two.":
            second_started.set()
            await asyncio.sleep(3600)

    route = CascadeRoute(agent=agent, tts=FakeTTS(), speak=speak)
    await route.prepare(session=None)
    ctx = make_ctx(route)
    turn_task = asyncio.create_task(route.run_turn(RouteInput(text="q"), ctx))
    await asyncio.wait_for(second_started.wait(), timeout=3)
    await route.cancel_turn(1)
    outcome = await asyncio.wait_for(turn_task, timeout=3)

    assert outcome.status == "cancelled"
    assert outcome.generated == ["One.", "Two."]
    assert outcome.played == ["One."]
    assert assistant_texts(ctx) == ["One."]
    await route.close()


@pytest.mark.asyncio
async def test_complete_commits_all_played_sentences() -> None:
    agent = FakeAgent(["One.", "Two."])
    route = CascadeRoute(
        agent=agent,
        tts=FakeTTS(),
        speak=lambda sentence, turn: _record([], sentence),
    )
    await route.prepare(session=None)
    ctx = make_ctx(route)
    outcome = await route.run_turn(RouteInput(text="q"), ctx)

    assert outcome.status == "completed"
    assert outcome.played == ["One.", "Two."]
    assert outcome.generated == ["One.", "Two."]
    assert assistant_texts(ctx) == ["One. Two."]
    await route.close()


@pytest.mark.asyncio
async def test_interruption_payload_includes_spoken_prefix() -> None:
    agent = FakeAgent(["One.", "Two."], delay=0.01)

    async def speak(sentence: str, turn: int) -> None:
        if sentence == "Two.":
            await asyncio.sleep(3600)

    route = CascadeRoute(agent=agent, tts=FakeTTS(), speak=speak)
    await route.prepare(session=None)
    ctx = make_ctx(route)
    turn_task = asyncio.create_task(route.run_turn(RouteInput(text="q"), ctx))
    await asyncio.sleep(0.05)
    await route.cancel_turn(1)
    outcome = await asyncio.wait_for(turn_task, timeout=3)

    payloads = interruption_payloads(ctx)
    assert payloads
    assert payloads[0]["played"] == ["One."]
    assert payloads[0]["generated"] == ["One.", "Two."]
    assert outcome.played == ["One."]
    await route.close()


@pytest.mark.asyncio
async def test_new_turn_after_interruption() -> None:
    agent = FakeAgent(["Slow one."], delay=0.05)
    route = CascadeRoute(agent=agent, tts=FakeTTS(), speak=None)
    await route.prepare(session=None)

    ctx1 = make_ctx(route, 1)
    turn_task = asyncio.create_task(route.run_turn(RouteInput(text="a"), ctx1))
    await asyncio.sleep(0.02)
    await route.cancel_turn(1)
    await turn_task

    ctx2 = make_ctx(route, 2)
    outcome = await route.run_turn(RouteInput(text="b"), ctx2)
    assert outcome.status == "completed"
    assert outcome.turn_id == 2


# -- identity observability ------------------------------------------------


@pytest.mark.asyncio
async def test_agent_identity_observable() -> None:
    """Acceptance: agent and provider identities remain observable."""
    agent = FakeAgent(["Sentence."])
    route = CascadeRoute(agent=agent, tts=FakeTTS())
    await route.prepare(session=None)
    # the route exposes the assembled pieces for metering/diagnostics
    assert route._agent is agent
    assert route._tts.name == "FakeTTS"
    assert route.capabilities.usage_operations == (
        "stt.stream",
        "agent.stream",
        "tts.stream",
    )
    await route.run_turn(RouteInput(text="x"), make_ctx(route))
    await route.close()


# -- lifecycle determinism --------------------------------------------------


@pytest.mark.asyncio
async def test_turn_before_prepare_rejected() -> None:
    route = CascadeRoute(agent=FakeAgent(["x"]), tts=FakeTTS())
    with pytest.raises(RouteStateError):
        await route.run_turn(RouteInput(text="x"), make_ctx(route))


@pytest.mark.asyncio
async def test_failed_warmup_still_prepares_the_route() -> None:
    agent = FakeAgent(["OK."])

    async def failing_warmup() -> None:
        raise RuntimeError("Pi response exceeded maximum time.")

    agent.warmup = failing_warmup
    route = CascadeRoute(agent=agent, tts=FakeTTS())
    await route.prepare(session=None)
    assert route._prepared is True
    assert route._policy_runtime is not None
    await route.close()


@pytest.mark.asyncio
async def test_turn_after_close_rejected() -> None:
    route = CascadeRoute(agent=FakeAgent(["x"]), tts=FakeTTS())
    await route.prepare(session=None)
    await route.close()
    await route.close()  # idempotent
    with pytest.raises(RouteStateError):
        await route.run_turn(RouteInput(text="x"), make_ctx(route))


@pytest.mark.asyncio
async def test_cancel_when_idle_is_noop() -> None:
    agent = FakeAgent(["x"])
    route = CascadeRoute(agent=agent, tts=FakeTTS())
    await route.prepare(session=None)
    await route.cancel_turn(42)  # no active turn — no error, no abort
    assert agent.aborted == 0


def test_capabilities_declare_cascade_differences() -> None:
    caps = CascadeRoute(agent=FakeAgent([]), tts=FakeTTS()).capabilities
    assert caps.route_type is RouteType.CASCADE
    assert caps.interruption_granularity is InterruptionGranularity.SENTENCE
    assert caps.resume_after_interruption is True


# -- r2-cascade-interruption: race-oriented cancel tests -------------------


class SlowAgent(FakeAgent):
    """Agent whose stream stalls at a chosen sentence index."""

    def __init__(self, sentences, *, delay=0.05, stall_at=None):
        super().__init__(sentences, delay=delay)
        self._stall_at = stall_at
        self.seen = 0

    async def respond_streaming(self, text: str, *, on_event=None):
        for i, sentence in enumerate(self._sentences):
            await asyncio.sleep(self._delay)
            if self._stall_at is not None and i == self._stall_at:
                await asyncio.sleep(3600)  # stalled mid-stream
            self.seen += 1
            yield sentence


@pytest.mark.asyncio
async def test_cancel_during_agent_output() -> None:
    """Barge-in while the agent is mid-stream: future output stops, queued
    audio discarded, upstream aborted, cleanup fires exactly once."""
    agent = SlowAgent(["One.", "Two.", "Three."], stall_at=1)
    cleanups: list[tuple[int, str]] = []
    discarded: list[int] = []
    route = CascadeRoute(
        agent=agent,
        tts=FakeTTS(),
        on_cleanup=lambda turn, status: _record2(cleanups, (turn, status)),
        discard_queue=discarded.append,
    )
    await route.prepare(session=None)
    ctx = make_ctx(route)
    turn_task = asyncio.create_task(route.run_turn(RouteInput(text="q"), ctx))
    await asyncio.sleep(0.08)  # first sentence played, second stalled
    await route.cancel_turn(1)
    outcome = await turn_task

    assert outcome.status == "cancelled"
    assert outcome.played == ["One."]
    assert discarded == [1]  # queued audio for the turn dropped
    assert agent.aborted == 1  # upstream aborted
    assert cleanups == [(1, "cancelled")]  # exactly once
    await route.close()


async def _record2(sink, item) -> None:
    sink.append(item)


@pytest.mark.asyncio
async def test_cancel_during_tts_output() -> None:
    """Barge-in while TTS is speaking: speak is awaited, then nothing more
    crosses the boundary; the queue is discarded."""
    spoken: list[str] = []
    agent = FakeAgent(["A.", "B.", "C."], delay=0.03)

    async def slow_speak(sentence: str, turn: int) -> None:
        await asyncio.sleep(0.06)  # TTS/playback in flight
        spoken.append(sentence)

    discarded: list[int] = []
    route = CascadeRoute(
        agent=agent, tts=FakeTTS(), speak=slow_speak, discard_queue=discarded.append
    )
    await route.prepare(session=None)
    ctx = make_ctx(route)
    turn_task = asyncio.create_task(route.run_turn(RouteInput(text="q"), ctx))
    await asyncio.sleep(0.10)  # first speak in flight
    await route.cancel_turn(1)
    outcome = await turn_task

    assert outcome.status == "cancelled"
    assert len(outcome.played) <= 1  # nothing more crossed the boundary
    assert 1 in discarded
    await route.close()


@pytest.mark.asyncio
async def test_cancel_during_stt_epoch() -> None:
    """Cancel before any agent output exists (STT committed, agent not yet
    streaming): turn settles cancelled, no output, cleanup fires."""
    agent = SlowAgent(["Only."], stall_at=0)
    cleanups: list[tuple[int, str]] = []
    route = CascadeRoute(
        agent=agent,
        tts=FakeTTS(),
        on_cleanup=lambda turn, status: _record2(cleanups, (turn, status)),
    )
    await route.prepare(session=None)
    ctx = make_ctx(route)
    turn_task = asyncio.create_task(route.run_turn(RouteInput(text="q"), ctx))
    await asyncio.sleep(0.02)  # agent not yet producing
    await route.cancel_turn(1)
    outcome = await turn_task
    assert outcome.status == "cancelled"
    assert outcome.played == []
    assert cleanups == [(1, "cancelled")]


@pytest.mark.asyncio
async def test_repeated_cancel_and_close_are_safe() -> None:
    agent = SlowAgent(["X."], stall_at=0)
    route = CascadeRoute(agent=agent, tts=FakeTTS())
    await route.prepare(session=None)
    ctx = make_ctx(route)
    turn_task = asyncio.create_task(route.run_turn(RouteInput(text="q"), ctx))
    await asyncio.sleep(0.02)
    await route.cancel_turn(1)
    await route.cancel_turn(1)  # repeated — no error
    outcome = await turn_task
    assert outcome.status == "cancelled"
    await route.close()
    await route.close()  # idempotent
    await route.cancel_turn(1)  # after close — still no error
    assert agent.aborted == 1  # abort happened exactly once


@pytest.mark.asyncio
async def test_new_turn_begins_after_interruption_with_fresh_epoch() -> None:
    """A fresh substantive turn clears the interruption epoch: it runs to
    completion and its output is not dropped as stale."""
    agent = SlowAgent(["Old.", "New answer one.", "New answer two."], delay=0.01)
    spoken: list[str] = []
    route = CascadeRoute(
        agent=agent,
        tts=FakeTTS(),
        speak=lambda s, t: _sync_record(spoken, s),
    )
    await route.prepare(session=None)

    ctx1 = make_ctx(route, 1)
    turn_task = asyncio.create_task(route.run_turn(RouteInput(text="a"), ctx1))
    await asyncio.sleep(0.03)
    await route.cancel_turn(1)
    first = await turn_task
    assert first.status == "cancelled"

    ctx2 = make_ctx(route, 2)
    second = await route.run_turn(RouteInput(text="b"), ctx2)
    assert second.status == "completed"
    assert second.played  # fresh epoch not dropped
    await route.close()


async def _sync_record(sink: list, item) -> None:
    sink.append(item)


# -- audit rework: the real STT -> agent -> TTS -> audio pipeline ------------


class FakeSTT:
    """Records PCM; returns a canned transcript."""

    def __init__(self, transcript="transcribed words") -> None:
        self.transcript = transcript
        self.calls: list[bytes] = []

    async def transcribe_pcm(self, pcm: bytes, *, purpose: str = "") -> str:
        self.calls.append(pcm)
        return self.transcript


class FakeStreamingTTS:
    """Records sentences; yields PCM chunks."""

    def __init__(self) -> None:
        self.synthesized: list[str] = []
        self.chunks = [b"\x01" * 480, b"\x02" * 480]

    async def synthesize(self, text: str):
        self.synthesized.append(text)
        for chunk in self.chunks:
            yield chunk


class RecordingAudioOutput:
    """AudioOutput implementation: records writes + accounting."""

    def __init__(self) -> None:
        self.writes: list[tuple[bytes, int]] = []
        self.closed = False
        from sleeptalk.routes.audio_output import AudioOutputAccounting

        self._accounting = AudioOutputAccounting(turn_id=-1)

    async def write(self, pcm: bytes, turn_id: int) -> None:
        if self.closed:
            return
        self.writes.append((pcm, turn_id))
        self._accounting.written(len(pcm))

    def accounting(self):
        return self._accounting

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_pipeline_exercises_stt_agent_tts_audio() -> None:
    """Audit: PCM input -> STT -> agent -> TTS -> AudioOutput — every
    adapter is exercised and asserted."""
    stt = FakeSTT(transcript="hello there")
    tts = FakeStreamingTTS()
    output = RecordingAudioOutput()
    agent = FakeAgent(["First sentence.", "Second sentence."])
    route = CascadeRoute(agent=agent, tts=tts, stt=stt, audio_output=output)
    await route.prepare(session=None)
    ctx = make_ctx(route)

    pcm = b"\x00" * 960  # 20ms canonical
    outcome = await route.run_turn(RouteInput(pcm=pcm), ctx)

    # STT exercised: PCM reached the adapter, transcript returned
    assert stt.calls == [pcm]
    # agent consumed the TRANSCRIPT, not empty text
    # (FakeAgent echoes input through its sentences indirectly — assert via
    # the emitted user transcript event)
    user_events = [e for e in ctx.extras["events"] if e.type == SessionEventType.TRANSCRIPT_FINAL]
    assert user_events[0].payload["text"] == "hello there"
    # TTS exercised: every speakable sentence synthesized
    assert tts.synthesized == ["First sentence.", "Second sentence."]
    # audio delivered through the output boundary with turn correlation
    assert len(output.writes) == 4  # 2 sentences x 2 chunks
    assert all(turn == 1 for _, turn in output.writes)
    assert all(pcm_chunk in (b"\x01" * 480, b"\x02" * 480) for pcm_chunk, _ in output.writes)
    assert outcome.status == "completed"
    await route.close()


@pytest.mark.asyncio
async def test_text_input_skips_stt() -> None:
    """Committed-text input (surface-side turn detection) goes straight
    to the agent; the STT adapter is untouched."""
    stt = FakeSTT()
    route = CascadeRoute(agent=FakeAgent(["Reply."]), tts=None, stt=stt)
    await route.prepare(session=None)
    ctx = make_ctx(route)
    await route.run_turn(RouteInput(text="typed input"), ctx)
    assert stt.calls == []  # no PCM: no STT work
    user_events = [e for e in ctx.extras["events"] if e.type == SessionEventType.TRANSCRIPT_FINAL]
    assert user_events[0].payload["text"] == "typed input"
    await route.close()


@pytest.mark.asyncio
async def test_barge_in_drops_mid_stream_tts_audio() -> None:
    """Cancellation between TTS chunks drops the rest of the audio."""
    stt = FakeSTT()
    tts = FakeStreamingTTS()
    output = RecordingAudioOutput()

    class SlowAgent(FakeAgent):
        async def respond_streaming(self, text: str, *, on_event=None):
            yield "Sentence one."
            await asyncio.sleep(0.05)
            yield "Sentence two."

    route = CascadeRoute(agent=SlowAgent(["x"]), tts=tts, stt=stt, audio_output=output)
    await route.prepare(session=None)
    ctx = make_ctx(route)
    turn_task = asyncio.create_task(route.run_turn(RouteInput(pcm=b"\x00" * 96), ctx))
    await asyncio.sleep(0.01)
    await route.cancel_turn(1)
    outcome = await asyncio.wait_for(turn_task, timeout=3)

    assert outcome.status == "cancelled"
    # only the pre-cancel audio crossed the boundary
    assert len(output.writes) <= 2
    await route.close()


@pytest.mark.asyncio
async def test_no_stt_adapter_pcm_becomes_empty_prompt_guard() -> None:
    """Without any STT adapter, PCM input cannot produce a prompt — the
    turn completes but the transcript marks it (documented limitation,
    not a silent agent call with garbage)."""
    route = CascadeRoute(agent=FakeAgent(["Fallback."]), tts=None, stt=None)
    await route.prepare(session=None)
    ctx = make_ctx(route)
    outcome = await route.run_turn(RouteInput(pcm=b"\x00" * 96), ctx)
    assert outcome.status == "completed"
    user_events = [e for e in ctx.extras["events"] if e.type == SessionEventType.TRANSCRIPT_FINAL]
    assert user_events[0].payload["text"] == "(speech)"  # marked, not faked
    await route.close()


# -- audit rework: route-owned task scope + TTS abort, public APIs only ------


class AbortableTTS:
    """TTS adapter with an abort hook (provider work cancellation)."""

    def __init__(self) -> None:
        self.aborted = 0
        self.synthesized: list[str] = []

    async def abort(self) -> None:
        self.aborted += 1

    async def synthesize(self, text: str):
        self.synthesized.append(text)
        yield b"\x00" * 96


@pytest.mark.asyncio
async def test_cancel_through_public_api_only() -> None:
    """The audit's core finding: cancellation works with ZERO private
    field assignment — the route registers its own task in run_turn."""
    agent = FakeAgent(["One.", "Two.", "Three."], delay=0.03)
    tts = AbortableTTS()
    route = CascadeRoute(agent=agent, tts=tts)
    await route.prepare(session=None)
    ctx = make_ctx(route)

    turn_task = asyncio.create_task(route.run_turn(RouteInput(text="q"), ctx))
    await asyncio.sleep(0.05)
    await route.cancel_turn(1)  # public API; route finds its own task
    outcome = await asyncio.wait_for(turn_task, timeout=3)

    assert outcome.status == "cancelled"
    assert agent.aborted == 1  # upstream aborted
    assert tts.aborted == 1  # provider work aborted
    await route.close()


@pytest.mark.asyncio
async def test_clean_next_turn_after_public_cancel() -> None:
    """A fresh turn after a public-API cancellation runs to completion."""

    class ScriptedAgent(FakeAgent):
        """Answers each call with the next scripted sentence list, so the
        route is exercised through its public API with no field swaps."""

        def __init__(self, scripts: list[list[str]], *, delay: float) -> None:
            super().__init__(scripts[0], delay=delay)
            self._scripts = scripts
            self._calls = 0

        async def respond_streaming(self, text: str, *, on_event=None):
            sentences = self._scripts[min(self._calls, len(self._scripts) - 1)]
            self._calls += 1
            for sentence in sentences:
                await asyncio.sleep(self._delay)
                yield sentence

    agent = ScriptedAgent([["Stalled."], ["Fresh answer."]], delay=0.05)
    route = CascadeRoute(agent=agent, tts=None)
    await route.prepare(session=None)

    ctx1 = make_ctx(route, 1)
    t1 = asyncio.create_task(route.run_turn(RouteInput(text="a"), ctx1))
    await asyncio.sleep(0.02)
    await route.cancel_turn(1)
    await asyncio.wait_for(t1, timeout=3)

    ctx2 = make_ctx(route, 2)
    outcome = await asyncio.wait_for(route.run_turn(RouteInput(text="b"), ctx2), timeout=3)
    assert outcome.status == "completed"
    assert outcome.played == ["Fresh answer."]  # fresh epoch not dropped
    await route.close()


@pytest.mark.asyncio
async def test_close_cancels_inflight_through_owned_scope() -> None:
    """close() on an in-flight turn cancels it via the route's own task
    registration — again with zero caller-side field assignment."""
    agent = FakeAgent(["Slow."], delay=0.05)
    route = CascadeRoute(agent=agent, tts=None)
    await route.prepare(session=None)
    ctx = make_ctx(route)
    turn_task = asyncio.create_task(route.run_turn(RouteInput(text="q"), ctx))
    await asyncio.sleep(0.02)

    await route.close()
    outcome = await asyncio.wait_for(turn_task, timeout=3)
    assert outcome.status == "cancelled"


@pytest.mark.asyncio
async def test_active_turn_accessor_tracks_the_pipeline() -> None:
    """The public active_turn() (from the extended protocol) reflects the
    in-flight turn while it runs."""
    route = CascadeRoute(agent=FakeAgent(["x"], delay=0.05), tts=None)
    await route.prepare(session=None)
    ctx = make_ctx(route)
    assert route.active_turn() is None
    turn_task = asyncio.create_task(route.run_turn(RouteInput(text="q"), ctx))
    await asyncio.sleep(0.02)
    in_flight = route.active_turn()
    assert in_flight is not None and in_flight.turn_id == 1
    await asyncio.wait_for(turn_task, timeout=3)
    assert route.active_turn() is None  # cleared after the turn settles
    await route.close()


class _FailingAgent(FakeAgent):
    def __init__(self, error: Exception | None) -> None:
        super().__init__([])
        self._error = error

    async def respond_streaming(self, text: str, *, on_event=None):
        if self._error is not None:
            raise self._error
        return
        yield ""  # pragma: no cover - makes this an async generator


@pytest.mark.asyncio
async def test_an_agent_failure_is_said_out_loud_before_it_is_raised() -> None:
    from sleeptalk.routes.cascade import AGENT_FAILED_LINE

    spoken: list[str] = []
    route = CascadeRoute(agent=_FailingAgent(RuntimeError("session not found")), tts=FakeTTS(),
                         speak=lambda sentence, turn: _record(spoken, sentence))
    await route.prepare(session=None)
    ctx = make_ctx(route)
    with pytest.raises(RuntimeError) as raised:
        await route.run_turn(RouteInput(text="hi"), ctx)
    assert spoken == [AGENT_FAILED_LINE]
    assert assistant_texts(ctx) == [AGENT_FAILED_LINE]  # shown on the page as well
    # Discord's own apology is skipped for an error the caller already heard about.
    assert raised.value.caller_told is True


@pytest.mark.asyncio
async def test_an_agent_that_answers_nothing_is_not_silence() -> None:
    from sleeptalk.routes.cascade import AGENT_SILENT_LINE

    spoken: list[str] = []
    route = CascadeRoute(agent=_FailingAgent(None), tts=FakeTTS(),
                         speak=lambda sentence, turn: _record(spoken, sentence))
    await route.prepare(session=None)
    outcome = await route.run_turn(RouteInput(text="hi"), make_ctx(route))
    assert outcome.status == "completed" and outcome.generated == []
    assert spoken == [AGENT_SILENT_LINE]


@pytest.mark.asyncio
async def test_a_rejected_agent_login_is_named_to_the_caller() -> None:
    from sleeptalk.routes.cascade import AGENT_LOGIN_LINE

    spoken: list[str] = []
    error = RuntimeError("Authentication failed (provider returned HTTP 401).")
    route = CascadeRoute(agent=_FailingAgent(error), tts=FakeTTS(),
                         speak=lambda sentence, turn: _record(spoken, sentence))
    await route.prepare(session=None)
    with pytest.raises(RuntimeError):
        await route.run_turn(RouteInput(text="hi"), make_ctx(route))
    assert spoken == [AGENT_LOGIN_LINE]


@pytest.mark.asyncio
async def test_a_listening_failure_is_said_out_loud() -> None:
    """A rate-limited or failing speech service used to end the turn in silence."""
    from sleeptalk.routes.cascade import LISTEN_FAILED_LINE

    class FailingSTT(FakeSTT):
        async def transcribe_pcm(self, pcm: bytes, *, purpose: str = "") -> str:
            raise RuntimeError("OpenRouter transcription failed (429)")

    tts = FakeStreamingTTS()
    route = CascadeRoute(agent=FakeAgent(["unused."]), tts=tts, stt=FailingSTT(), audio_output=RecordingAudioOutput())
    await route.prepare(session=None)
    with pytest.raises(RuntimeError) as raised:
        await route.run_turn(RouteInput(pcm=b"\x00" * 960), make_ctx(route))
    assert tts.synthesized == [LISTEN_FAILED_LINE]
    assert raised.value.caller_told is True
    await route.close()


@pytest.mark.asyncio
async def test_when_the_voice_fails_the_page_shows_the_unspoken_answer() -> None:
    """Live (fault proxy): TTS 500 or a stalled voice left the caller with
    silence and an empty page; the failure line needs the same voice."""
    from sleeptalk.routes.cascade import VOICE_FAILED_LINE

    tts_calls: list[str] = []

    class BrokenTTS(FakeTTS):
        async def synthesize(self, text):
            tts_calls.append(text)
            raise RuntimeError("OpenRouter TTS failed (500)")
            yield b""  # pragma: no cover

    played: list = []

    class Output(RecordingAudioOutput):
        async def play_file(self, path):
            played.append(path.name)

    route = CascadeRoute(agent=FakeAgent(["Two plus two is four."]), tts=BrokenTTS(),
                         audio_output=Output())
    await route.prepare(session=None)
    ctx = make_ctx(route)
    with pytest.raises(RuntimeError):
        await route.run_turn(RouteInput(text="what's two plus two"), ctx)
    assert assistant_texts(ctx)[-2:] == ["Two plus two is four.", VOICE_FAILED_LINE]
    assert played == ["voice-unavailable.wav"]  # a tone instead of silence
    assert tts_calls == ["Two plus two is four."]  # no apology attempted on the failed voice
