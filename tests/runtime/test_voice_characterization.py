"""PR 1 (mt-pr1-characterization): characterization tests for the canonical
native Discord voice stack.

These tests PIN current behavior at the seams the multi-transport extraction
(ADR 0001, roadmap phase 9) will cut in PRs 2-3. They are written against
unmodified production code; where current behavior is a known bug we assert
the actual behavior (with a comment) or mark the desired behavior xfail.

Covered seams:
  A. VAD + utterance boundaries (fixed PCM fixtures)     — sink.py
  B. Per-participant separation                          — sink.py
  C. Fragment merging window                             — pi_discord_bot.py
  D. Control words / status queries                      — pi_discord_bot.py
  E. Normal response playback                            — playback.py
  F. Barge-in + current cancellation behavior            — turns.py, pi_discord_bot.py
  G. Session/sink cleanup                                — sink.py, pi_discord_bot.py
  H. Concurrent/duplicate stop semantics                 — pi_discord_bot.py
  I. Buffer + task behavior                              — pi_discord_bot.py, stream_tts.py, sink.py
  J. Text/voice conversation association                 — identity.py

Known-bug documentation lives in the xfail tests at the bottom.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from types import SimpleNamespace

import pytest

from sleeptalk.discord_native.playback import DiscordPlaybackController
from sleeptalk.discord_native.sink import NativeDiscordOpusSink
from sleeptalk.discord_native.timeline import VoiceTimeline
from sleeptalk.discord_native.turns import DiscordTurnCoordinator
from sleeptalk.voice_loop.lifecycle import LifecycleSupervisor
from sleeptalk.voice_loop.playback import ResponsePlayback
from sleeptalk.discord.helpers import actor_id as discord_actor_id
from sleeptalk.discord.session_helpers import _STATUS_QUERY_RE
from sleeptalk.discord_voice.utterance_patterns import _CONTROL_WORD_RE
from sleeptalk.discord_voice import PiDiscordLiveSession
from sleeptalk.stream_tts import StreamPCMAudioSource
from sleeptalk.interrupt import InterruptPolicy
from sleeptalk.voice.configs import AdaptiveEndpointingConfig, DiscordTurnConfig
from sleeptalk.discord.access import DiscordAccess

OPEN_ACCESS = DiscordAccess(required=False)

# --- Fixed PCM fixtures: 20 ms @ 48 kHz stereo s16le = 3840 bytes -----------
FRAME_BYTES = 3840
SILENT_FRAME = b"\x00" * FRAME_BYTES
LOUD_FRAME = (16000).to_bytes(2, "little", signed=True) * 1920  # rms 16000
RMS_THRESHOLD = 500

USER_A = SimpleNamespace(id=101, bot=False, name="A", display_name="A")
USER_B = SimpleNamespace(id=202, bot=False, name="B", display_name="B")


def _make_sink(monkeypatch, *, decode=None, decode_return=LOUD_FRAME, **overrides):
    """Real NativeDiscordOpusSink with the Opus decoder faked.

    decode(frame) -> pcm lets tests make PCM content depend on wire bytes
    (per-speaker fixtures). DAVE is passthrough when no voice client is set.
    """
    calls = {"fec": [], "decoded": 0}

    class FakeDecoder:
        def decode(self, packet, *, fec):
            calls["fec"].append(fec)
            calls["decoded"] += 1
            if decode is not None:
                return decode(packet)
            return decode_return

    monkeypatch.setattr("sleeptalk.discord_native.sink.Decoder", FakeDecoder)

    kwargs = dict(
        loop=asyncio.get_running_loop(),
        min_bytes=8,
        silence_seconds=10.0,  # no timer flush unless a test asks for it
        on_utterance=None,  # filled below
        should_ignore_audio=lambda: False,
        vad_mode="rms",
        speech_rms_threshold=RMS_THRESHOLD,
        endpointing_config=AdaptiveEndpointingConfig(enabled=False),
    )
    kwargs.update(overrides)
    utterances = []

    async def on_utterance(user, pcm):
        utterances.append((user.id, pcm))

    if kwargs["on_utterance"] is None:
        kwargs["on_utterance"] = on_utterance
    sink = NativeDiscordOpusSink(**kwargs)
    return sink, utterances, calls


async def _feed(sink, user, payload: bytes, count: int = 1) -> None:
    for _ in range(count):
        sink.write(user, SimpleNamespace(opus=payload))
        await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# A. VAD + utterance boundaries
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_char_silence_never_opens_utterance_buffer(monkeypatch):
    sink, utterances, _ = _make_sink(monkeypatch, decode_return=SILENT_FRAME)
    await _feed(sink, USER_A, b"wire", count=5)
    assert sink.buffers == {}
    assert utterances == []


@pytest.mark.asyncio
async def test_char_loud_speech_buffers_and_flushes_as_one_utterance(monkeypatch):
    sink, utterances, _ = _make_sink(monkeypatch)
    await _feed(sink, USER_A, b"wire", count=3)
    sink._flush_user_id(USER_A.id)
    await asyncio.sleep(0)
    assert utterances == [(USER_A.id, LOUD_FRAME * 3)]
    # Flush consumed all per-user state (decoder, timestamps, vad state).
    assert sink.buffers == {}
    assert sink.users == {}
    assert sink.decoders == {}


@pytest.mark.asyncio
async def test_char_utterance_below_min_bytes_is_dropped(monkeypatch):
    sink, utterances, _ = _make_sink(
        monkeypatch, min_bytes=FRAME_BYTES * 2 + 1
    )
    await _feed(sink, USER_A, b"wire", count=2)
    sink._flush_user_id(USER_A.id)
    await asyncio.sleep(0)
    assert utterances == []


@pytest.mark.asyncio
async def test_char_speech_started_fires_once_per_utterance(monkeypatch):
    started = []
    sink, utterances, _ = _make_sink(
        monkeypatch, on_speech_started=lambda user: started.append(user.id)
    )
    await _feed(sink, USER_A, b"wire", count=3)
    sink._flush_user_id(USER_A.id)
    await asyncio.sleep(0)
    await _feed(sink, USER_A, b"wire", count=2)
    sink._flush_user_id(USER_A.id)
    await asyncio.sleep(0)
    assert started == [USER_A.id, USER_A.id]  # once per utterance, not per frame
    assert len(utterances) == 2


@pytest.mark.asyncio
async def test_char_utterance_waits_for_streaming_frame_delivery(monkeypatch):
    delivered = []
    release = asyncio.Event()
    utterance_delivered = asyncio.Event()

    async def on_frame(_user, pcm):
        await release.wait()
        delivered.append(("frame", pcm))

    async def on_utterance(_user, pcm):
        delivered.append(("utterance", pcm))
        utterance_delivered.set()

    sink, _utterances, _calls = _make_sink(
        monkeypatch,
        on_pcm_frame=on_frame,
        on_utterance=on_utterance,
    )
    await _feed(sink, USER_A, b"wire")
    sink._flush_user_id(USER_A.id)
    await asyncio.sleep(0)

    assert delivered == []
    release.set()
    await asyncio.wait_for(utterance_delivered.wait(), timeout=0.1)
    assert [kind for kind, _pcm in delivered] == ["frame", "utterance"]


@pytest.mark.asyncio
async def test_char_silence_timer_flushes_utterance(monkeypatch):
    """The real endpoint timer path: with adaptive endpointing off and an
    80 ms silence floor, a burst flushes on its own shortly after speech ends."""
    sink, utterances, _ = _make_sink(monkeypatch, silence_seconds=0.08)
    await _feed(sink, USER_A, b"wire", count=3)
    await asyncio.sleep(0.35)
    assert len(utterances) == 1
    assert utterances[0][1] == LOUD_FRAME * 3


@pytest.mark.asyncio
async def test_char_ignore_gate_drops_audio(monkeypatch):
    ignoring = {"on": True}
    sink, utterances, _ = _make_sink(
        monkeypatch, should_ignore_audio=lambda: ignoring["on"]
    )
    await _feed(sink, USER_A, b"wire", count=3)
    assert sink.buffers == {}  # audio dropped, not buffered
    ignoring["on"] = False
    await _feed(sink, USER_A, b"wire", count=2)
    assert len(sink.buffers.get(USER_A.id, ())) == FRAME_BYTES * 2
    assert utterances == []


# ---------------------------------------------------------------------------
# B. Per-participant separation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_char_two_speakers_have_independent_utterance_buffers(monkeypatch):
    sink, utterances, _ = _make_sink(monkeypatch, decode=lambda packet: packet)
    await _feed(sink, USER_A, b"A" * 16, count=3)
    await _feed(sink, USER_B, b"B" * 16, count=2)

    sink._flush_user_id(USER_A.id)
    await asyncio.sleep(0)
    # Flushing A must not disturb B's in-progress utterance.
    assert utterances == [(USER_A.id, b"A" * 16 * 3)]
    assert bytes(sink.buffers[USER_B.id]) == b"B" * 16 * 2
    assert USER_B.id in sink.decoders

    sink._flush_user_id(USER_B.id)
    await asyncio.sleep(0)
    assert utterances[1] == (USER_B.id, b"B" * 16 * 2)


@pytest.mark.asyncio
async def test_char_decode_failure_drops_only_that_frame(monkeypatch):
    attempts = {"n": 0}

    def decode(packet):
        attempts["n"] += 1
        if attempts["n"] == 2:
            raise ValueError("corrupt opus")
        return packet

    sink, utterances, _ = _make_sink(monkeypatch, decode=decode)
    await _feed(sink, USER_A, b"x" * 16, count=3)
    assert sink.decode_failures == 1
    assert sink.frames_received == 3
    assert sink.frames_decoded == 2
    assert bytes(sink.buffers[USER_A.id]) == b"x" * 16 * 2


# ---------------------------------------------------------------------------
# C. Fragment merging (hold/merge paths live in test_fragment_hold.py; this
#    pins the barge-window gate that decides whether holding applies at all)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_char_fragment_outside_barge_window_is_still_held():
    # Behavior change (hesitant-speaker fix): incomplete fragments are held
    # for their continuation even WITHOUT a recent barge-in — mid-monologue
    # pauses fragment identically to barge-ins.
    from sleeptalk.policy import VoicePolicy

    policy = VoicePolicy(fragment_hold_ms=60, fragment_barge_window_ms=10_000)
    session = SimpleNamespace(
        bot=SimpleNamespace(policy=policy),
        _last_barge_at=time.monotonic() - 20.0,  # barge-in was 20 s ago
        _pending_fragments={},
    )
    marks = []
    trace = SimpleNamespace(mark=lambda stage, **_: marks.append(stage))

    out = await PiDiscordLiveSession._maybe_hold_fragment(
        session, USER_A, "can you tell me about the", trace
    )
    assert out == "can you tell me about the"
    assert "fragment_held" in marks
    assert "fragment_released" in marks


# ---------------------------------------------------------------------------
# D. Control words + status queries (regex seam; the intercept path itself is
#    exercised in test_pi_discord_bot.py)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    ["stop", "Stop.", "please stop", "never mind", "nevermind", "hold on",
     "wait", "okay stop", "stop stop", "be quiet", "that's enough",
     "slash stop", "Pipeline control slash Stop."],
)
def test_char_control_words_match(text):
    assert _CONTROL_WORD_RE.match(text), text


@pytest.mark.parametrize(
    "text",
    [
        "stop the deployment",      # object-bearing request goes to the agent
        "yes",                      # answers reach the agent
        "no thanks",
        "can you wait for the build",
        "stopwatch",
    ],
)
def test_char_control_words_reject_conversation(text):
    assert not _CONTROL_WORD_RE.match(text), text


@pytest.mark.parametrize(
    "text,expected",
    [
        ("what are you doing?", True),
        ("are you still working?", True),
        ("any progress?", True),
        ("deploy the app", False),
    ],
)
def test_char_status_query_regex(text, expected):
    assert bool(_STATUS_QUERY_RE.search(text)) is expected


# ---------------------------------------------------------------------------
# E. Normal response playback
# ---------------------------------------------------------------------------


class _FakeVoice:
    """Minimal stand-in for discord.VoiceClient at the playback seam."""

    def __init__(self):
        self.played = []
        self.stopped = 0

    def is_playing(self):
        return False

    def is_connected(self):
        return True

    def play(self, source, *, after):
        self.played.append(source)
        asyncio.get_running_loop().call_soon(after, None)

    def stop(self):
        self.stopped += 1


@pytest.mark.asyncio
async def test_char_play_source_completes_and_fires_hooks():
    voice = _FakeVoice()
    events = []
    controller = DiscordPlaybackController(
        voice,
        VoiceTimeline(),
        on_started=lambda: events.append(("started", controller.active)),
        on_stopped=lambda: events.append(("stopped", controller.active)),
    )
    source = SimpleNamespace()

    result = await controller.play_source(source)

    assert voice.played == [source]
    assert result.completed is True
    assert result.cancelled is False
    # on_started fires while active, on_stopped after active clears.
    assert events == [("started", True), ("stopped", False)]
    assert controller.last_result is result


# ---------------------------------------------------------------------------
# F. Barge-in + current cancellation behavior
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_char_coordinator_stops_playback_before_interrupt_callback(monkeypatch):
    order = []

    class FakeSilero:
        def __init__(self, _config):
            pass

        def reset(self):
            return None

        async def observe(self, _pcm):
            from sleeptalk.voice_loop.vad import VadObservation

            return VadObservation(
                is_speech=True,
                started=True,
                state="SPEAKING",
                confidence=0.95,
                volume=0.5,
            )

    class FakePlayback:
        active = True

        async def stop(self, reason):
            order.append(("playback_stop", reason))
            self.active = False
            return True

    async def on_interrupt(user):
        order.append(("on_interrupt", user.id))

    monkeypatch.setattr("sleeptalk.discord_native.turns.SileroVadGate", FakeSilero)
    coordinator = DiscordTurnCoordinator(
        config=DiscordTurnConfig(),
        playback=FakePlayback(),
        timeline=VoiceTimeline(),
        on_interrupt=on_interrupt,
        interrupt=InterruptPolicy(grace_ms=0, min_duration_ms=20),
    )
    coordinator.bot_playback_started()
    assert await coordinator.observe_pcm(USER_A, LOUD_FRAME) is True

    assert order == [("playback_stop", "barge-in"), ("on_interrupt", USER_A.id)]
    assert coordinator.stats.interruptions == 1


def _barge_session(*, live_turn, barge_in="immediate"):
    """SimpleNamespace with exactly the attributes _handle_barge_in touches."""

    class SinkRec:
        def __init__(self):
            self.injected = []

        def inject_preroll(self, user, pcm):
            self.injected.append((user.id, pcm))

    class SttRec:
        def __init__(self):
            self.appended = []

        async def append_pcm(self, user_id, pcm):
            self.appended.append((user_id, pcm))

    class PlaybackRec:
        def __init__(self):
            self.stops = []

        async def stop(self, reason):
            self.stops.append(reason)
            return True

    class AgentRec:
        def __init__(self):
            self.aborts = 0

        async def abort(self):
            self.aborts += 1

    session = SimpleNamespace(
        _preroll={USER_A.id: deque([b"pre1", b"pre2"])},
        _barge_listen=set(),
        _last_barge_at=0.0,
        sink=SinkRec(),
        streaming_stt=SttRec(),
        playback=PlaybackRec(),
        _live_turn=live_turn,
        _soft_barge_pending=False,
        _speak_tasks=[asyncio.create_task(asyncio.sleep(60))],
        _in_flight_prompts={},
        bot=SimpleNamespace(
            access=OPEN_ACCESS,
            agent=AgentRec(),
            kernel=None,
            policy=SimpleNamespace(barge_in=barge_in),
        ),
    )
    session._cancel_pending_utterances = (
        lambda **kwargs: PiDiscordLiveSession._cancel_pending_utterances(
            session, **kwargs
        )
    )
    session._remember_interrupted_context = (
        lambda user_id: PiDiscordLiveSession._remember_interrupted_context(
            session, user_id
        )
    )
    async def append_audio(pcm, *, participant_id):
        await session.streaming_stt.append_pcm(participant_id, pcm)

    async def cancel_active_turn(*, owner=None):
        await session.bot.agent.abort()

    session.bot.realtime_session = SimpleNamespace(append_audio=append_audio)
    session.bot.cancel_active_turn = cancel_active_turn
    return session


@pytest.mark.asyncio
async def test_char_hard_barge_in_delegates_cancellation_to_session():
    """Barge-in delegates cancellation without dropping sibling utterances."""
    session = _barge_session(live_turn=None)
    task = session._speak_tasks[0]

    await PiDiscordLiveSession._handle_barge_in(session, USER_A)

    assert not task.cancelling()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert session.bot.agent.aborts == 1
    assert session.playback.stops == []  # coordinator already stopped audio
    assert session._soft_barge_pending is False
    assert USER_A.id in session._barge_listen
    assert session._last_barge_at > 0


@pytest.mark.asyncio
async def test_char_soft_barge_in_pauses_knock_and_defers_abort():
    """Working phase (no answer audio yet): pause the knock and let the
    transcript decide — the abort is deferred to handle_utterance."""
    live_turn = {"answer_queued": asyncio.Event()}  # not set: still working
    session = _barge_session(live_turn=live_turn, barge_in="soft")
    task = session._speak_tasks[0]

    await PiDiscordLiveSession._handle_barge_in(session, USER_A)

    assert session._soft_barge_pending is True
    assert session.playback.stops == []  # coordinator paused playback already
    assert session.bot.agent.aborts == 0       # deferred
    assert not task.cancelled()                # turn still alive
    task.cancel()


@pytest.mark.asyncio
async def test_char_barge_in_reinjects_preroll_into_sink_and_streaming_stt():
    """The sentence that CAUSED the interruption is captured in full:
    pre-roll frames flow to both transcript paths before anything else."""
    session = _barge_session(live_turn=None)

    await PiDiscordLiveSession._handle_barge_in(session, USER_A)

    assert session.sink.injected == [(USER_A.id, b"pre1pre2")]
    from sleeptalk.discord_voice.live_session import _stereo_to_mono_s16

    assert session.streaming_stt.appended == [
        (USER_A.id, _stereo_to_mono_s16(b"pre1pre2"))
    ]
    assert USER_A.id not in session._preroll  # consumed


# ---------------------------------------------------------------------------
# G. Cleanup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_char_sink_cleanup_cancels_timers_and_clears_all_state(monkeypatch):
    sink, _, _ = _make_sink(monkeypatch)
    await _feed(sink, USER_A, b"wire", count=2)
    assert sink.buffers and sink.handles and sink.decoders
    handle = sink.handles[USER_A.id]

    sink.cleanup()
    await asyncio.sleep(0)

    assert handle.cancelled()
    for attr in ("buffers", "users", "decoders", "handles", "last_audio_at",
                 "silero_vads", "silero_locks"):
        assert getattr(sink, attr) == {}, attr
    assert sink.speech_reported == set()


# ---------------------------------------------------------------------------
# H. Stop semantics (concurrent/duplicate callers)
# ---------------------------------------------------------------------------


def _stoppable_session(*, stt_close_delay: float = 0.0):
    """Namespace with exactly the attributes PiDiscordLiveSession.stop touches."""

    class VoiceRec:
        def __init__(self):
            self._listening = True
            self.stop_calls = 0

        def is_listening(self):
            return self._listening

        def stop_listening(self):
            self.stop_calls += 1
            self._listening = False

    class SttRec:
        def __init__(self):
            self.close_calls = 0
            self.closed = False

        async def close(self):
            self.close_calls += 1
            if stt_close_delay:
                await asyncio.sleep(stt_close_delay)
            self.closed = True

    class AgentRec:
        def __init__(self):
            self.abort_calls = 0

        async def abort(self):
            self.abort_calls += 1

    class SinkRec:
        def __init__(self):
            self.drop_all_calls = 0

        def drop_all(self):
            self.drop_all_calls += 1

    session = SimpleNamespace(
        voice=VoiceRec(),
        streaming_stt=SttRec(),
        _lifecycle=LifecycleSupervisor(),
        # P0 teardown contract: stop() flushes every in-flight/pending piece.
        _turn_generation=0,
        _speak_tasks=[],
        _pending_fragments={},
        _barge_listen=set(),
        _preroll={},
        _in_flight_prompts={},
        _interrupted_context=None,
        _resume_offered=False,
        _resume_offer_task=None,
        sink=SinkRec(),
        bot=SimpleNamespace(agent=AgentRec()),
    )
    session._unsubscribe_hybrid_events = (
        lambda: PiDiscordLiveSession._unsubscribe_hybrid_events(session)
    )
    session._cancel_pending_utterances = (
        lambda **kwargs: PiDiscordLiveSession._cancel_pending_utterances(
            session, **kwargs
        )
    )
    async def cancel_active_turn(*, owner=None):
        await session.bot.agent.abort()

    session.bot.cancel_active_turn = cancel_active_turn
    session.bot.realtime_session = SimpleNamespace(reset_capture=session.streaming_stt.close)
    return session


@pytest.mark.asyncio
async def test_char_session_stop_is_synchronous_and_repeatable():
    """Two stop() calls (e.g. voice-state event + /leave in the same tick):
    the FIRST caller owns shutdown; the second is a no-op on every side
    (PR 3: LifecycleSupervisor)."""
    session = _stoppable_session()
    PiDiscordLiveSession.stop(session)
    PiDiscordLiveSession.stop(session)
    await asyncio.sleep(0)

    assert session.voice.stop_calls == 1
    assert session.streaming_stt.close_calls == 1  # PR 3: idempotent
    # P0: teardown also invalidated in-flight responses and flushed queues.
    assert session._turn_generation == 1
    assert session.sink.drop_all_calls == 1
    assert session.bot.agent.abort_calls == 1


@pytest.mark.asyncio
async def test_char_session_stop_does_not_await_streaming_stt_close():
    """stop() fire-and-forgets streaming_stt.close(): it returns before the
    close completes. A session restarted immediately can race the old close."""
    session = _stoppable_session(stt_close_delay=0.05)
    PiDiscordLiveSession.stop(session)

    assert session.streaming_stt.closed is False  # close still in flight
    await asyncio.sleep(0.1)
    assert session.streaming_stt.closed is True


@pytest.mark.asyncio
async def test_char_session_stop_is_idempotent():
    """Fixed in PR 3 (was xfail in PR 1): duplicate stop() is a no-op."""
    session = _stoppable_session()
    PiDiscordLiveSession.stop(session)
    PiDiscordLiveSession.stop(session)
    await asyncio.sleep(0)
    assert session.streaming_stt.close_calls == 1


@pytest.mark.asyncio
async def test_char_session_stop_cancels_in_flight_response():
    """P0: stop() cancels the in-flight speak pipeline so a dead session's
    response can never play after a reconnect."""
    session = _stoppable_session()
    speak_task = asyncio.create_task(asyncio.sleep(60))
    session._speak_tasks = [speak_task]
    PiDiscordLiveSession.stop(session)
    await asyncio.sleep(0)
    assert speak_task.cancelled()
    assert session._speak_tasks == []
    assert session._turn_generation == 1


@pytest.mark.asyncio
async def test_char_concurrent_stops_run_shutdown_exactly_once():
    """Chaos case from the PR 3 spec: transport failure, timeout, and a
    /leave command all trigger teardown in the same loop tick."""
    session = _stoppable_session()

    async def trigger():
        PiDiscordLiveSession.stop(session)

    await asyncio.gather(*(trigger() for _ in range(5)))
    await asyncio.sleep(0)

    assert session.voice.stop_calls == 1
    assert session.streaming_stt.close_calls == 1
    assert session._lifecycle.is_shutting_down


# ---------------------------------------------------------------------------
# I. Buffer + task behavior
# ---------------------------------------------------------------------------


class _PcmFrameSession:
    """Namespace with exactly the attributes _handle_pcm_frame touches."""

    def __init__(self, *, bot_speaking: bool):
        self._preroll = {}
        self.bot = SimpleNamespace(
            access=OPEN_ACCESS,
            policy=SimpleNamespace(turns=SimpleNamespace(vad_threshold=RMS_THRESHOLD))
        )
        self._last_user_voice_at = 0.0
        self.observed = []
        self.turns = SimpleNamespace(observe_pcm=self._observe)
        self.bot_speaking = bot_speaking
        self.stt_appended = []
        self.bot.realtime_session = SimpleNamespace(append_audio=self._append)
        self.text_channel = None
        self.voice = None

    async def _observe(self, user, pcm):
        self.observed.append(user.id)

    async def _append(self, pcm, *, participant_id):
        self.stt_appended.append(participant_id)


def test_speech_latency_trace_starts_at_vad_and_survives_to_endpoint(monkeypatch):
    traces = []

    class _Trace:
        trace_id = "trace123"

        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.marks = []
            traces.append(self)

        def mark(self, stage, **fields):
            self.marks.append((stage, fields))

    attempts = []
    monkeypatch.setattr("sleeptalk.discord_voice.live_session.VoiceLatencyTrace", _Trace)
    session = SimpleNamespace(
        turn_lock=asyncio.Lock(),
        bot=SimpleNamespace(
            access=OPEN_ACCESS,
            surface="discord",
            persona="test",
            policy=SimpleNamespace(capture=SimpleNamespace(silence_ms=700)),
            config=SimpleNamespace(agent=SimpleNamespace(type="pi")),
            agent=None,
        ),
        voice=SimpleNamespace(guild=SimpleNamespace(id=42)),
        streaming_stt=SimpleNamespace(
            begin_latency_attempt=lambda user_id, **kwargs: attempts.append(
                (user_id, kwargs)
            )
        ),
        _speech_latency_traces={},
    )
    session._trace_metadata = lambda user: PiDiscordLiveSession._trace_metadata(
        session, user
    )

    PiDiscordLiveSession._speech_started(session, USER_A)
    trace = PiDiscordLiveSession._new_trace(session, USER_A, LOUD_FRAME)

    assert trace is traces[0]
    assert [stage for stage, _ in trace.marks] == [
        "speech_detected",
        "utterance_ready",
    ]
    assert attempts[0][0] == USER_A.id
    assert attempts[0][1]["attempt_id"] == "trace123:stt:ws:1"
    assert session._speech_latency_traces == {}


@pytest.mark.asyncio
async def test_char_preroll_is_bounded_at_75_frames():
    """The barge-in pre-roll is the one bounded audio history: 75 frames
    (~1.5 s). This bound must survive the PR 2 extraction."""
    session = _PcmFrameSession(bot_speaking=False)
    for _ in range(100):
        await PiDiscordLiveSession._handle_pcm_frame(session, USER_A, SILENT_FRAME)
    assert len(session._preroll[USER_A.id]) == 75


@pytest.mark.asyncio
async def test_char_bot_speaking_gates_stt_but_not_preroll_or_barge_in():
    """Echo guard: while the bot speaks, frames still feed the preroll and the
    barge-in VAD, but are kept out of the streaming STT."""
    session = _PcmFrameSession(bot_speaking=True)
    for _ in range(3):
        await PiDiscordLiveSession._handle_pcm_frame(session, USER_A, SILENT_FRAME)
    assert len(session._preroll[USER_A.id]) == 3
    assert session.observed == [USER_A.id] * 3
    assert session.stt_appended == []


@pytest.mark.asyncio
async def test_char_sink_spawns_one_unawaited_task_per_pcm_frame(monkeypatch):
    """CURRENT BEHAVIOR (risk, not endorsement): every decoded frame becomes
    an unawaited asyncio task. N frames in flight => N concurrent callbacks;
    there is no processing queue and no drop policy at this seam today."""
    started = []
    release = asyncio.Event()

    async def on_pcm_frame(user, pcm):
        started.append(user.id)
        await release.wait()

    sink, _, _ = _make_sink(monkeypatch, on_pcm_frame=on_pcm_frame)
    await _feed(sink, USER_A, b"wire", count=5)
    await asyncio.sleep(0.01)

    assert started == [USER_A.id] * 5  # all five spawned, none awaited
    release.set()
    await asyncio.sleep(0)


def test_char_stream_source_buffer_has_no_capacity_cap():
    """CURRENT BEHAVIOR: if TTS outpaces the 20 ms player thread, the source
    buffer grows without bound. Bounded writes/backpressure arrive with the
    epoch-scoped ResponsePlayback in PR 3 — this pins today's shape."""
    source = StreamPCMAudioSource()
    for _ in range(20):
        source.feed(b"\x00" * FRAME_BYTES)  # ~400 ms of audio, nothing read
    assert len(source._buf) == 20 * FRAME_BYTES


# ---------------------------------------------------------------------------
# I2. Response-scoped playback (PR 3): epoch cancellation + honest clearing
# ---------------------------------------------------------------------------


def test_char_stream_source_rejects_feeds_after_discard():
    """Late frames from a cancelled response epoch are rejected, not queued."""
    source = StreamPCMAudioSource()
    assert source.feed(b"\x00" * FRAME_BYTES) is True
    source.discard()
    assert source.feed(b"\x00" * FRAME_BYTES) is False
    assert len(source._buf) == 0
    assert source.read() == b""  # discarded source ends playback immediately


@pytest.mark.asyncio
async def test_char_response_playback_cancel_drains_and_reports_best_effort():
    controller = DiscordPlaybackController(_FakeVoice(), VoiceTimeline())
    response = ResponsePlayback(epoch=1, controller=controller)
    source = StreamPCMAudioSource()
    source.feed(b"\x01" * FRAME_BYTES * 3)
    response.attach_source(source)

    result = await response.cancel("barge-in")

    assert response.cancelled is True
    assert result.status == "best_effort"  # Discord cannot recall wire audio
    assert len(source._buf) == 0
    assert source.feed(b"\x01" * FRAME_BYTES) is False  # epoch rejects late frames

    again = await response.cancel("barge-in")
    assert again.detail == "already cancelled"


@pytest.mark.asyncio
async def test_char_playback_stop_drains_source_buffer_synchronously():
    """stop() no longer waits for the player thread's cleanup(): buffered
    PCM is gone before stop() returns."""
    source = StreamPCMAudioSource()
    source.feed(b"\x02" * FRAME_BYTES * 5)

    class VoiceWithSource(_FakeVoice):
        def __init__(self, src):
            super().__init__()
            self._player = SimpleNamespace(source=src)
            self._playing = True

        def is_playing(self):
            return self._playing

    voice = VoiceWithSource(source)
    controller = DiscordPlaybackController(voice, VoiceTimeline())
    controller.active = True

    stopped = await controller.stop("barge-in")

    assert stopped is True
    assert len(source._buf) == 0  # drained synchronously, not by the player thread


# ---------------------------------------------------------------------------
# J. Text/voice conversation association
# ---------------------------------------------------------------------------


def test_char_actor_id_is_stable_scoped_and_platform_prefixed():
    """Voice turns and text messages land in the same conversation via the
    durable actor id; it is derived only from the platform user id."""
    first = discord_actor_id(USER_A.id)
    assert first == discord_actor_id(USER_A.id)          # stable
    assert first != discord_actor_id(USER_B.id)          # per-user
    assert "discord" in first and str(USER_A.id) in first  # platform-scoped


# ---------------------------------------------------------------------------
# Known-bug documentation (expected failures)
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=False,
    reason="verified 2026-08: sink decodes with fec=False and the "
    "wants_opus=True path skips voice_recv's FEC/PLC fake-packet decode, so "
    "packet-loss concealment is disabled. Not approved for PR 1; revisit "
    "when the media layer is extracted.",
)
@pytest.mark.asyncio
async def test_char_decoder_uses_fec_for_lost_packets(monkeypatch):
    sink, _, calls = _make_sink(monkeypatch)
    await _feed(sink, USER_A, b"wire", count=1)
    assert calls["fec"] and all(calls["fec"]), (
        f"expected fec=True somewhere, got {calls['fec']}"
    )


@pytest.mark.asyncio
async def test_a_soft_barge_with_no_words_puts_the_answer_back_on(monkeypatch):
    """Discord, synthetic: "Mm-hmm" paused the answer and its words never
    arrived; nothing resumed it, and the call sat silent until the next turn.
    The watchdog resumes it, unless the caller is still talking."""
    monkeypatch.setattr(PiDiscordLiveSession, "SOFT_BARGE_TIMEOUT_SECONDS", 0.01)
    resumed: list[str] = []

    async def resume_after_soft_barge(reason):
        resumed.append(reason)

    session = _barge_session(live_turn={"answer_queued": asyncio.Event()}, barge_in="soft")
    session.turns = SimpleNamespace(resume_after_soft_barge=resume_after_soft_barge)
    session.sink.buffers = {USER_A.id: bytearray(b"still talking")}
    session._speak_tasks[0].cancel()

    await PiDiscordLiveSession._handle_barge_in(session, USER_A)
    await asyncio.sleep(0.05)
    assert resumed == [] and session._soft_barge_pending is True  # still talking: wait
    session.sink.buffers = {}
    await asyncio.sleep(0.05)
    assert resumed == ["no words"]
    assert session._soft_barge_pending is False


@pytest.mark.asyncio
async def test_the_barge_preroll_starts_after_the_speakers_last_silence(monkeypatch):
    """Discord sends frames only while someone transmits, so the last 1.5 s of
    frames could be the previous question's tail: "Just explain it. Mm-hmm."
    turned a backchannel into a real turn that cancelled the answer."""
    from collections import deque

    clock = {"now": 100.0}
    monkeypatch.setattr("sleeptalk.discord_voice.live_session.time.monotonic", lambda: clock["now"])

    async def observe_pcm(_user, _pcm):
        return None

    session = SimpleNamespace(
        bot=SimpleNamespace(access=OPEN_ACCESS, policy=SimpleNamespace(turns=SimpleNamespace(vad_threshold=500))),
        voice=SimpleNamespace(channel=None),
        _preroll={}, _preroll_heard_at={}, _last_user_voice_at=0.0,
        turns=SimpleNamespace(observe_pcm=observe_pcm), bot_speaking=True,
    )
    for frame in (b"old1", b"old2"):
        await PiDiscordLiveSession._handle_pcm_frame(session, USER_A, frame)
        clock["now"] += 0.02
    clock["now"] += 3.0  # silence: Discord sent nothing
    for frame in (b"new1", b"new2"):
        await PiDiscordLiveSession._handle_pcm_frame(session, USER_A, frame)
        clock["now"] += 0.02
    assert list(session._preroll[USER_A.id]) == [b"new1", b"new2"]
    assert isinstance(session._preroll[USER_A.id], deque)
