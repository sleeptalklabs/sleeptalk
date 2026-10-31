import asyncio
import time
from types import SimpleNamespace

import pytest
from av import AudioFrame
from sleeptalk.call_provider import CallContext
from sleeptalk.config import AgentConfig, SleeptalkConfig
from sleeptalk.interrupt import AcousticKind, SpeakingState
from sleeptalk.policy import CapturePolicyValues, InterruptPolicyValues, TurnPolicyValues, VoicePolicy
from sleeptalk.routes.base import TurnOutcome
from sleeptalk.session_events import SessionEvent, SessionEventType, Speaker, agent_event, transcript

from sleeptalk.voice_loop.vad import VadObservation
from sleeptalk.web_call import _context_for_profile
from sleeptalk.web_voice import (
    BrowserTransportState,
    WebCallSession,
    WebRTCPlaybackController,
)


class FakeStream:
    def __init__(self):
        self.sent = []
        self.clears = 0

    async def send(self, frame):
        self.sent.append(frame)

    def clear_output(self):
        self.clears += 1
        count = len(self.sent)
        self.sent.clear()
        return count


class BlockingStream(FakeStream):
    def __init__(self):
        super().__init__()
        self.send_started = asyncio.Event()
        self.release_send = asyncio.Event()

    async def send(self, frame):
        self.sent.append(frame)
        self.send_started.set()
        await self.release_send.wait()


class FakeKernelSession:
    def __init__(self, policy=None):
        self.policy = policy or VoicePolicy()
        self.aborts = 0
        self.audio_output = None
        self.appended = []
        self.turns = []
        self.closed = 0
        self.subscribers = []
        self.session_id = ""
        self.config = SimpleNamespace()

    def set_audio_output(self, output):
        self.audio_output = output

    def subscribe(self, callback):
        self.subscribers.append(callback)

        def unsubscribe():
            self.subscribers.remove(callback)

        return unsubscribe

    async def append_audio(self, pcm, *, participant_id=0):
        self.appended.append((pcm, participant_id))

    async def run_turn(self, *, pcm=None, text=None, extras=None):
        self.turns.append({"pcm": pcm, "text": text, "extras": extras})
        return TurnOutcome(turn_id=len(self.turns), status="completed")

    async def interrupt_presentation(self):
        self.aborts += 1

    async def aclose(self):
        self.closed += 1


class ScriptedBargeVad:
    def __init__(self, observation):
        self.observation = observation
        self.calls = 0
        self.resets = 0

    async def observe(self, pcm):
        self.calls += 1
        self.last_pcm = pcm
        return self.observation

    def reset(self):
        self.resets += 1


@pytest.mark.parametrize("provider,model", [("cartesia", "ink-2"), ("cartesia", "ink-whisper"),
                                           ("elevenlabs", "scribe_v2"),
                                           ("deepgram", "nova-2"), ("deepgram", "nova-3")])
async def test_hosted_live_partials_follow_audio_without_committing_turns(monkeypatch, provider, model):
    from sleeptalk.voice.configs import TurnCompletionSignal

    monkeypatch.setattr("sleeptalk.web_voice.session.log_latency_event", lambda **_: None)
    kernel = FakeKernelSession()
    kernel.policy.local_responsiveness = True
    kernel.session_id = f"{provider}-partials"
    kernel.config = SimpleNamespace(stt=SimpleNamespace(provider=provider, model=model))
    signal = TurnCompletionSignal(transcript="I want", source=provider)
    kernel.turn_completion_signal = lambda _: signal
    events = []

    def emit(event):
        events.append(event)
        for callback in kernel.subscribers:
            callback(event)

    kernel.emit = emit
    session = WebCallSession(session=kernel, stream=FakeStream(), call_sid=provider)

    async def feed():
        frame = AudioFrame(format="s16", layout="stereo", samples=960)
        frame.sample_rate = 48_000
        frame.planes[0].update(bytes(3840))
        await session.feed_audio_frame(frame)

    try:
        assert not session.assembler.endpointing_config.local_streaming_input
        assert session.assembler.endpointing_config.fresh_voice_timing
        await feed()
        assert session._presentation.partial == "I want"
        assert events[-1].payload["source"] == provider
        assert events[-1].type is SessionEventType.TRANSCRIPT_PARTIAL
        count = len(events)
        await feed()
        assert len(events) == count

        # Deepgram's endpoint signal remains display-only in this UI path.
        signal = TurnCompletionSignal(transcript="I wanted streaming", source=provider,
                                      is_final=provider == "deepgram")
        await feed()
        assert session._presentation.partial == "I wanted streaming"
        assert not kernel.turns
        emit(transcript(kernel.session_id, 1, speaker=Speaker.USER,
                        text="I wanted streaming.", final=True))
        assert session._presentation.partial == ""

        # An answer in progress must not redisplay stale recognition text.
        session._turn_in_flight = True
        await feed()
        assert session._presentation.partial == ""
        session._turn_in_flight = False
        signal = TurnCompletionSignal(transcript="", source=provider)
        await feed()
        assert session._presentation.partial == ""
        # The same words in the next capture must render again.
        signal = TurnCompletionSignal(transcript="I wanted streaming", source=provider)
        await feed()
        assert session._presentation.partial == "I wanted streaming"
        assert not kernel.turns
        signal = TurnCompletionSignal(transcript="unrelated", source="pause_ahead")
        await feed()
        assert session._presentation.partial == "I wanted streaming"
    finally:
        session._turn_in_flight = False
        await session.close()


def make_web_barge_session(monkeypatch, observation, *, turns=None, interrupt=None):
    gate = ScriptedBargeVad(observation)
    monkeypatch.setattr("sleeptalk.web_voice.session.SileroVadGate", lambda _config: gate)
    stream = FakeStream()
    policy = VoicePolicy(
        resume_grace_ms=0,
        turns=turns or TurnPolicyValues(),
        interrupt=interrupt or InterruptPolicyValues(),
    )
    kernel = FakeKernelSession(policy)
    session = WebCallSession(
        stream=stream,
        call_sid="opaque",
        session=kernel,
    )
    return session, gate, kernel, stream


async def _watch_frames(session, n=8, pcm=None):
    frame = bytes(3_840) if pcm is None else pcm
    for _ in range(n):
        await session._watch_web_barge_in(frame)


class _SpeedKernelSession(FakeKernelSession):
    """Kernel that records absolute speed requests the way the real one snaps."""

    def __init__(self, policy=None):
        super().__init__(policy)
        self.speeds = []

    def set_tts_speed(self, value):
        from sleeptalk.speech_speed import snap_speed

        applied = snap_speed(value)
        self.speeds.append(applied)
        return applied

    def current_tts_speed(self):
        return self.speeds[-1] if self.speeds else 1.0


def _speed_session():
    kernel = _SpeedKernelSession()
    return WebCallSession(stream=FakeStream(), call_sid="opaque", session=kernel), kernel


@pytest.mark.asyncio
async def test_settings_apply_narration_style_to_the_live_policy():
    session, _kernel = _speed_session()
    assert session.policy.narration_style == "silence"
    session.apply_settings({"narration": "keyboard"})
    assert session.policy.narration_style == "keyboard"
    session.apply_settings({"narration": "silence"})
    assert session.policy.narration_style == "silence"


@pytest.mark.asyncio
async def test_settings_reject_an_unknown_narration_style():
    """A bad value falls back to the default rather than sticking."""
    session, _kernel = _speed_session()
    session.apply_settings({"narration": "keyboard"})
    session.apply_settings({"narration": "interpretive dance"})
    assert session.policy.narration_style == "silence"


@pytest.mark.asyncio
async def test_settings_carry_speed_and_narration_together():
    session, kernel = _speed_session()
    session.apply_settings({"narration": "silence", "ttsSpeed": 1.25})
    assert session.policy.narration_style == "silence"
    assert kernel.speeds == [1.25]


@pytest.mark.asyncio
async def test_settings_apply_reply_length_to_the_live_policy():
    session, _kernel = _speed_session()
    assert session.policy.reply_length == "full"
    session.apply_settings({"replyLength": "summarized"})
    assert session.policy.reply_length == "summarized"
    session.apply_settings({"replyLength": "full"})
    assert session.policy.reply_length == "full"


@pytest.mark.asyncio
async def test_settings_reject_an_unknown_reply_length():
    """A bad value must not silently start dropping half the answer."""
    session, _kernel = _speed_session()
    session.apply_settings({"replyLength": "tweet"})
    assert session.policy.reply_length == "full"


@pytest.mark.asyncio
async def test_settings_carry_every_control_together():
    session, kernel = _speed_session()
    session.apply_settings(
        {"narration": "silence", "replyLength": "summarized", "ttsSpeed": 1.25}
    )
    assert session.policy.narration_style == "silence"
    assert session.policy.reply_length == "summarized"
    assert kernel.speeds == [1.25]


@pytest.mark.asyncio
async def test_settings_apply_playback_speed_to_the_kernel():
    session, kernel = _speed_session()
    session.apply_settings({"ttsSpeed": 1.5})
    assert kernel.speeds == [1.5]
    assert session._presentation.tts_speed == 1.5


@pytest.mark.asyncio
async def test_settings_snap_an_off_staircase_speed():
    """The browser and host share a staircase; the host is the one that decides."""
    session, kernel = _speed_session()
    session.apply_settings({"ttsSpeed": 1.7})
    assert kernel.speeds == [1.5]
    assert session._presentation.tts_speed == 1.5


@pytest.mark.asyncio
async def test_settings_ignore_unknown_and_malformed_keys():
    """The menu ships controls before they are all wired; a live call must not break."""
    session, kernel = _speed_session()
    for payload in (
        {},
        {"narration": "silent"},
        {"ttsSpeed": None},
        {"ttsSpeed": "fast"},
        {"ttsSpeed": {}},
    ):
        session.apply_settings(payload)
    assert kernel.speeds == []


@pytest.mark.asyncio
async def test_settings_handler_is_bound_to_the_stream():
    class _BindingStream(FakeStream):
        def __init__(self):
            super().__init__()
            self.settings_handler = None

        def set_settings_handler(self, handler):
            self.settings_handler = handler

    stream = _BindingStream()
    kernel = _SpeedKernelSession()
    WebCallSession(stream=stream, call_sid="opaque", session=kernel)
    assert stream.settings_handler is not None
    stream.settings_handler({"ttsSpeed": 1.5})
    assert kernel.speeds == [1.5]


@pytest.mark.asyncio
@pytest.mark.parametrize("route_type", ["cascade", "hybrid"])
async def test_web_call_publishes_the_profile_controls_on_join(route_type):
    policy = VoicePolicy(reply_length="summarized", narration_style="keyboard")
    kernel = _SpeedKernelSession(policy)
    kernel.route_type = SimpleNamespace(value=route_type)
    kernel.set_tts_speed(1.25)
    stream = FakeStream()
    stream.presentations = []

    async def send_presentation(payload):
        stream.presentations.append(payload)

    stream.send_presentation = send_presentation
    session = WebCallSession(stream=stream, call_sid="opaque", session=kernel)
    await asyncio.gather(*session._agent_state_tasks)

    assert stream.presentations[-1]["ttsSpeed"] == 1.25
    assert stream.presentations[-1]["narrationStyle"] == "keyboard"
    assert stream.presentations[-1]["replyLength"] == "summarized"


@pytest.mark.asyncio
async def test_web_call_uses_tuned_capture_stack_by_default():
    policy = VoicePolicy(capture=CapturePolicyValues(rms_threshold=120))
    stream = FakeStream()
    session = WebCallSession(
        stream=stream,
        call_sid="opaque",
        session=FakeKernelSession(policy),
    )
    assert session.assembler.vad_mode == policy.capture.vad_mode == "hybrid"
    assert session.assembler.silence_seconds == policy.capture.silence_ms / 1000
    assert session.assembler.speech_rms_threshold == 120
    assert session.assembler.endpointing_config.enabled is True
    assert session.assembler.smart_turn_scorer is not None
    # Turns are judged by words, as on the terminal and Discord: the live
    # transcript signal, and a batch listener's early transcript at a pause.
    assert session.assembler.turn_completion_provider == session._turn_completion_signal
    assert session.assembler.early_transcripts == session._listens_ahead
    # A soft barge pauses the answer and lets the caller's words decide.
    assert session._gate.calibration.supports_resume is True
    assert session._gate.kind is AcousticKind.isolated
    assert session._gate.detector == "silero"
    assert session._gate.policy.min_volume == policy.interrupt.min_volume
    assert session._web_barge_rms.threshold == policy.turns.vad_threshold

    await session.close()


def _loud_speech(**fields):
    return VadObservation(
        is_speech=True,
        started=True,
        state="SPEAKING",
        confidence=0.99,
        volume=0.5,
        **fields,
    )


@pytest.mark.asyncio
async def test_web_barge_in_keeps_2_5_gain_in_the_adapter(monkeypatch):
    session, gate, agent, _stream = make_web_barge_session(
        monkeypatch,
        VadObservation(is_speech=False),
        interrupt=InterruptPolicyValues(grace_ms=0),
    )
    session.playback.active = True
    session._clock_ms = 0
    quiet_browser_pcm = (300).to_bytes(2, "little", signed=True) * 1_920

    await _watch_frames(session, n=2, pcm=quiet_browser_pcm)

    sample = int.from_bytes(gate.last_pcm[:2], "little", signed=True)
    assert sample == 750
    assert agent.aborts == 0
    await session.close()


@pytest.mark.asyncio
async def test_web_barge_in_uses_interrupt_min_volume(monkeypatch):
    quiet_echo = VadObservation(
        is_speech=True,
        started=True,
        state="SPEAKING",
        confidence=0.99,
        volume=0.01,
    )
    policy = VoicePolicy(
        resume_grace_ms=0,
        capture=CapturePolicyValues(vad_min_volume=0.003),
        interrupt=InterruptPolicyValues(grace_ms=0, min_duration_ms=80, min_volume=0.03),
    )
    gate = ScriptedBargeVad(quiet_echo)
    monkeypatch.setattr("sleeptalk.web_voice.session.SileroVadGate", lambda _config: gate)
    kernel = FakeKernelSession(policy)
    session = WebCallSession(
        stream=FakeStream(),
        call_sid="opaque",
        session=kernel,
    )
    session.playback.active = True
    session._clock_ms = 0

    await _watch_frames(session)

    assert session._gate.policy.min_volume == 0.03
    assert kernel.aborts == 0
    await session.close()


@pytest.mark.asyncio
async def test_web_barge_in_holds_leftover_frame_300ms_after_drain(monkeypatch):
    session, gate, agent, _stream = make_web_barge_session(monkeypatch, _loud_speech())
    session._turn_in_flight = True
    session.playback.active = True
    session._clock_ms = 0
    await session._watch_web_barge_in(bytes(3_840))

    session.playback.active = False
    session._clock_ms = 1500
    await session._watch_web_barge_in(bytes(3_840))
    session._clock_ms = 1800
    await _watch_frames(session)

    assert gate.calls >= 1
    assert agent.aborts == 0
    assert session._gate.floor_state is SpeakingState.hold
    assert session.playback.active is False
    session._turn_in_flight = False
    await session.close()


@pytest.mark.asyncio
async def test_web_barge_in_fires_stop_800ms_into_first_clip(monkeypatch):
    session, gate, agent, stream = make_web_barge_session(monkeypatch, _loud_speech())
    session.playback.active = True
    session._clock_ms = 0
    await session._watch_web_barge_in(bytes(3_840))
    session._clock_ms = 800
    await _watch_frames(session)

    assert gate.calls >= 1
    assert agent.aborts == 1
    assert stream.clears == 1
    assert session.playback.cancelled is True
    await session.close()


@pytest.mark.asyncio
async def test_web_barge_in_forwards_continuation_audio_while_task_is_detached(
    monkeypatch,
):
    session, _gate, kernel, _stream = make_web_barge_session(
        monkeypatch, _loud_speech()
    )
    session._turn_in_flight = True
    session._web_barge_capture_active = True
    session.playback.active = False
    frame = AudioFrame(format="s16", layout="stereo", samples=960)
    frame.sample_rate = 48_000
    frame.planes[0].update(b"\x01\x00\x03\x00" * 960)

    await session.feed_audio_frame(frame)

    assert kernel.appended == [(b"\x02\x00" * 960, session.PARTICIPANT_ID)]
    session._turn_in_flight = False
    await session.close()


@pytest.mark.asyncio
async def test_web_playback_active_is_not_the_floor(monkeypatch):
    session, _gate, agent, _stream = make_web_barge_session(monkeypatch, _loud_speech())
    session._turn_in_flight = True
    session.playback.active = True
    session._clock_ms = 0
    await session._watch_web_barge_in(bytes(3_840))
    session.playback.active = False
    session._clock_ms = 1500
    await session._watch_web_barge_in(bytes(3_840))

    assert session.playback.active is False
    assert session._gate.floor_state is SpeakingState.hold
    session._clock_ms = 1800
    await _watch_frames(session)
    assert agent.aborts == 0
    session._turn_in_flight = False
    await session.close()


@pytest.mark.asyncio
async def test_web_barge_in_post_commit_grace_rejects_trailing_audio(monkeypatch):
    session, gate, agent, _stream = make_web_barge_session(
        monkeypatch,
        _loud_speech(),
        interrupt=InterruptPolicyValues(grace_ms=60_000, min_duration_ms=80),
    )
    session._turn_in_flight = True
    session.playback.active = True
    session._clock_ms = 0

    await _watch_frames(session, n=20)

    assert gate.calls >= 1
    assert agent.aborts == 0
    session._turn_in_flight = False
    await session.close()


@pytest.mark.asyncio
async def test_web_bot_grace_arms_on_playback_not_turn_start(monkeypatch):
    session, gate, agent, stream = make_web_barge_session(monkeypatch, _loud_speech())
    session._turn_in_flight = True
    session.playback.active = False
    session._clock_ms = 0
    await session._watch_web_barge_in(bytes(3_840))
    session.playback.active = True
    session._clock_ms = 30_000
    await _watch_frames(session)

    assert agent.aborts == 0
    assert stream.clears == 0
    session._turn_in_flight = False
    await session.close()


@pytest.mark.asyncio
async def test_web_bot_grace_rearms_when_playback_resumes_after_gap(monkeypatch):
    session, gate, agent, stream = make_web_barge_session(monkeypatch, _loud_speech())
    session._turn_in_flight = True
    session.playback.active = True
    session._clock_ms = 0
    await session._watch_web_barge_in(bytes(3_840))
    session.playback.active = False
    session._clock_ms = 1_500
    await session._watch_web_barge_in(bytes(3_840))
    session.playback.active = True
    session._clock_ms = 1_500
    await _watch_frames(session)

    assert agent.aborts == 0
    assert stream.clears == 0
    session._turn_in_flight = False
    await session.close()


@pytest.mark.asyncio
async def test_web_barge_in_rejects_quiet_confident_echo(monkeypatch):
    echo = VadObservation(
        is_speech=True,
        started=True,
        state="SPEAKING",
        confidence=0.99,
        volume=0.001,
    )
    session, gate, agent, _stream = make_web_barge_session(
        monkeypatch,
        echo,
        interrupt=InterruptPolicyValues(grace_ms=0, min_duration_ms=80),
    )
    session.playback.active = True
    session._clock_ms = 0

    await _watch_frames(session, n=20)

    assert gate.calls >= 1
    assert agent.aborts == 0
    await session.close()


@pytest.mark.asyncio
async def test_web_barge_in_honors_post_interrupt_cooldown(monkeypatch):
    session, _gate, agent, stream = make_web_barge_session(
        monkeypatch,
        _loud_speech(),
        interrupt=InterruptPolicyValues(grace_ms=0, min_duration_ms=80, cooldown_ms=60_000),
    )
    session.playback.active = True
    session._clock_ms = 0
    await _watch_frames(session)
    assert agent.aborts == 1

    session._barge_fired_for_response = False
    session.playback.active = True
    session._clock_ms = 100
    await _watch_frames(session)

    assert agent.aborts == 1
    assert stream.clears == 1
    await session.close()


@pytest.mark.asyncio
async def test_isolated_web_does_not_duck_on_detector_speech(monkeypatch):
    speech = _loud_speech()
    turns = TurnPolicyValues(
        duck_on_vad=True,
        duck_min_ms=80,
        duck_release_ms=80,
        duck_confidence=0.7,
        duck_min_volume=0.03,
    )
    session, _gate, agent, _stream = make_web_barge_session(
        monkeypatch,
        speech,
        turns=turns,
        interrupt=InterruptPolicyValues(grace_ms=0, min_duration_ms=10_000),
    )
    session.playback.active = True
    session._clock_ms = 0

    await _watch_frames(session, n=8)

    assert session._gate.kind is AcousticKind.isolated
    assert session.playback._resume_event.is_set() is True
    assert not hasattr(session, "_web_ducked")
    assert agent.aborts == 0
    await session.close()


def test_browser_transport_state_distinguishes_turn_from_direct_webrtc():
    state = BrowserTransportState()
    assert state.transport == "web-webrtc"

    state.update(
        {
            "stage": "transport_stats",
            "usesRelay": True,
            "currentRttMs": 80,
            "jitterBufferMs": 15,
        }
    )

    assert state.transport == "web-turn"
    assert state.fields()["estimatedDownstreamMs"] == 55.0

    state.update({"stage": "transport_stats", "usesRelay": False})
    assert state.transport == "web-direct"


@pytest.mark.asyncio
async def test_webrtc_playback_publishes_runtime_pcm_as_timed_frames():
    stream = FakeStream()
    playback = WebRTCPlaybackController(stream)
    pcm = bytes(range(256)) * 15

    await playback._send_pcm(pcm)

    assert len(stream.sent) == 1
    frame = stream.sent[0]
    assert isinstance(frame, AudioFrame)
    assert frame.sample_rate == 48_000
    assert frame.samples == 960
    assert frame.pts == 0
    assert bytes(frame.planes[0])[: len(pcm)] == pcm


@pytest.mark.asyncio
async def test_webrtc_playback_adapts_kernel_mono_pcm_to_stereo():
    stream = FakeStream()
    playback = WebRTCPlaybackController(stream)
    mono = (1).to_bytes(2, "little", signed=True) + (2).to_bytes(2, "little", signed=True)

    await playback.write(mono, turn_id=3)
    await playback.finish_turn(3)
    await playback.wait_until_idle()

    assert len(stream.sent) == 1
    assert bytes(stream.sent[0].planes[0])[:8] == (b"\x01\x00\x01\x00\x02\x00\x02\x00")
    assert playback.accounting().bytes_generated == len(mono)
    assert playback.accounting().bytes_written == len(mono)


@pytest.mark.asyncio
async def test_webrtc_playback_preserves_pcm_across_producer_chunks():
    stream = FakeStream()
    playback = WebRTCPlaybackController(stream)
    first = (1).to_bytes(2, "little", signed=True) * 480
    second = (2).to_bytes(2, "little", signed=True) * 480

    await playback.write(first, turn_id=3)
    await playback.wait_until_idle()
    await playback.write(second, turn_id=3)
    await playback.wait_until_idle()

    assert len(stream.sent) == 1
    pcm = bytes(stream.sent[0].planes[0])
    assert pcm[:1_920] == b"\x01\x00\x01\x00" * 480
    assert pcm[1_920:3_840] == b"\x02\x00\x02\x00" * 480


@pytest.mark.asyncio
async def test_webrtc_playback_finish_turn_pads_tail_once_without_counting_padding():
    stream = FakeStream()
    playback = WebRTCPlaybackController(stream)
    mono = b"\x01\x00\x02\x00\x03\x00"

    await playback.write(mono, turn_id=5)
    assert stream.sent == []

    await playback.finish_turn(5)
    await playback.finish_turn(5)
    await playback.wait_until_idle()

    assert len(stream.sent) == 1
    pcm = bytes(stream.sent[0].planes[0])
    assert pcm[:12] == b"\x01\x00\x01\x00\x02\x00\x02\x00\x03\x00\x03\x00"
    assert pcm[12:3_840] == bytes(3_828)
    assert playback.accounting().bytes_generated == len(mono)
    assert playback.accounting().bytes_written == len(mono)


@pytest.mark.asyncio
async def test_webrtc_playback_accepts_another_segment_after_same_turn_finish():
    stream = FakeStream()
    playback = WebRTCPlaybackController(stream)

    await playback.write(b"\x01\x00", turn_id=5)
    await playback.finish_turn(5)
    await playback.wait_until_idle()
    await playback.write(b"\x02\x00", turn_id=5)
    await playback.finish_turn(5)
    await playback.wait_until_idle()

    assert len(stream.sent) == 2
    assert bytes(stream.sent[0].planes[0])[:4] == b"\x01\x00\x01\x00"
    assert bytes(stream.sent[1].planes[0])[:4] == b"\x02\x00\x02\x00"
    assert playback.accounting().bytes_generated == 4
    assert playback.accounting().bytes_written == 4


@pytest.mark.asyncio
async def test_webrtc_playback_queues_followup_audio_while_current_audio_plays():
    stream = BlockingStream()
    playback = WebRTCPlaybackController(stream, max_queue_chunks=2)
    first = (1).to_bytes(2, "little", signed=True) * 960
    second = (2).to_bytes(2, "little", signed=True) * 960

    await asyncio.wait_for(playback.write(first, turn_id=7), timeout=0.1)
    await asyncio.wait_for(stream.send_started.wait(), timeout=0.1)
    await asyncio.wait_for(playback.write(second, turn_id=7), timeout=0.1)

    assert playback.active is True
    assert playback._audio_queue.qsize() == 1

    stream.release_send.set()
    await asyncio.wait_for(playback.wait_until_idle(), timeout=0.1)

    assert len(stream.sent) == 2
    assert bytes(stream.sent[0].planes[0])[:4] == b"\x01\x00\x01\x00"
    assert bytes(stream.sent[1].planes[0])[:4] == b"\x02\x00\x02\x00"


@pytest.mark.asyncio
async def test_webrtc_playback_discard_drops_buffered_and_future_turn_audio():
    stream = BlockingStream()
    playback = WebRTCPlaybackController(stream)
    pcm = b"\x01\x00" * 960

    await playback.write(pcm, turn_id=9)
    await asyncio.wait_for(stream.send_started.wait(), timeout=0.1)
    await playback.write(pcm, turn_id=9)
    await playback.discard_turn(9, "barge-in")
    stream.release_send.set()
    await asyncio.wait_for(playback.wait_until_idle(), timeout=0.1)
    await playback.write(pcm, turn_id=9)

    assert stream.clears == 1
    assert stream.sent == []
    assert playback._audio_queue.empty()
    assert playback.accounting().bytes_dropped_stale == len(pcm)


@pytest.mark.asyncio
async def test_webrtc_playback_discard_clears_tail_and_rejects_same_turn_writes():
    stream = FakeStream()
    playback = WebRTCPlaybackController(stream)
    stale = b"\x01\x00" * 480
    fresh = b"\x02\x00" * 480

    await playback.write(stale, turn_id=9)
    await playback.discard_turn(9, "barge-in")
    await playback.write(stale, turn_id=9)
    await playback.finish_turn(9)
    await playback.write(fresh, turn_id=11)
    await playback.finish_turn(11)
    await playback.wait_until_idle()

    assert len(stream.sent) == 1
    pcm = bytes(stream.sent[0].planes[0])
    assert pcm[:1_920] == b"\x02\x00\x02\x00" * 480
    assert b"\x01\x00\x01\x00" not in pcm


@pytest.mark.asyncio
async def test_webrtc_playback_next_turn_after_barge_in_is_audible():
    stream = FakeStream()
    playback = WebRTCPlaybackController(stream)
    stale = b"\x01\x00" * 480
    fresh = b"\x02\x00" * 480

    await playback.write(stale, turn_id=9)
    await playback.discard_turn(9, "barge-in")
    await playback.write(fresh, turn_id=10)
    await playback.finish_turn(10)
    await playback.wait_until_idle()

    assert len(stream.sent) == 1
    pcm = bytes(stream.sent[0].planes[0])
    assert pcm[:1_920] == b"\x02\x00\x02\x00" * 480
    assert playback.accounting().bytes_dropped_stale == 0


@pytest.mark.asyncio
async def test_webrtc_playback_epoch_advance_clears_the_previous_tail():
    stream = FakeStream()
    playback = WebRTCPlaybackController(stream)

    await playback.write(b"\x01\x00" * 480, turn_id=3)
    await playback.write(b"\x02\x00" * 480, turn_id=4)
    await playback.finish_turn(4)
    await playback.wait_until_idle()

    assert len(stream.sent) == 1
    pcm = bytes(stream.sent[0].planes[0])
    assert pcm[:1_920] == b"\x02\x00\x02\x00" * 480
    assert b"\x01\x00\x01\x00" not in pcm


@pytest.mark.asyncio
async def test_webrtc_playback_stop_fences_a_writer_blocked_on_the_queue():
    stream = BlockingStream()
    playback = WebRTCPlaybackController(stream, max_queue_chunks=1)
    frame = b"\x01\x00" * 960

    await playback.write(frame, turn_id=7)
    await asyncio.wait_for(stream.send_started.wait(), timeout=0.1)
    await playback.write(frame, turn_id=7)
    blocked = asyncio.create_task(playback.write(frame, turn_id=7))
    await asyncio.sleep(0)
    assert blocked.done() is False

    await asyncio.wait_for(playback.stop("barge-in"), timeout=0.1)
    await asyncio.wait_for(blocked, timeout=0.1)
    stream.release_send.set()
    await asyncio.wait_for(playback.wait_until_idle(), timeout=0.1)

    assert stream.sent == []
    assert playback._audio_queue.empty()


@pytest.mark.asyncio
async def test_webrtc_playback_drops_late_audio_after_close():
    stream = FakeStream()
    playback = WebRTCPlaybackController(stream)

    await playback.write(b"\x02\x00" * 480, turn_id=3)
    await playback.close()
    await playback.write(b"\x01\x00" * 480, turn_id=4)
    await playback.finish_turn(4)

    assert stream.sent == []
    assert playback.accounting().bytes_generated == 960
    assert playback.accounting().bytes_dropped_stale == 960


@pytest.mark.asyncio
async def test_webrtc_playback_stop_clears_queued_audio():
    stream = FakeStream()
    stream.sent.append(object())
    playback = WebRTCPlaybackController(stream)
    playback.active = True
    await playback.write(b"\x01\x00" * 480, turn_id=6)

    assert await playback.stop("barge-in") is True
    await playback.write(b"\x02\x00" * 480, turn_id=6)
    await playback.finish_turn(6)
    assert playback.cancelled is True
    assert stream.clears == 1
    assert stream.sent == []


@pytest.mark.asyncio
async def test_web_agent_state_does_not_return_to_working_after_answer_starts():
    stream = FakeStream()
    stream.agent_states = []

    async def send_agent_state(*, state, task="", detail=""):
        stream.agent_states.append({"state": state, "task": task, "detail": detail})

    stream.send_agent_state = send_agent_state
    session = WebCallSession(
        stream=stream,
        call_sid="opaque",
        session=FakeKernelSession(),
    )

    session._on_session_event(
        SessionEvent(SessionEventType.RESPONSE_STARTED, "session", turn_id=1)
    )
    session._on_session_event(
        SessionEvent(SessionEventType.RESPONSE_FIRST_OUTPUT, "session", turn_id=1)
    )
    session._on_session_event(
        SessionEvent(
            SessionEventType.TOOL_STARTED,
            "session",
            turn_id=1,
            payload={"name": "research"},
        )
    )
    await asyncio.gather(*session._agent_state_tasks)

    assert [item["state"] for item in stream.agent_states] == [
        "working",
        "answering",
        "answering",
    ]


@pytest.mark.asyncio
async def test_web_agent_state_stays_answering_until_queued_audio_finishes():
    stream = BlockingStream()
    stream.agent_states = []

    async def send_agent_state(*, state, task="", detail=""):
        stream.agent_states.append({"state": state, "task": task, "detail": detail})

    stream.send_agent_state = send_agent_state
    session = WebCallSession(
        stream=stream,
        call_sid="opaque",
        session=FakeKernelSession(),
    )
    session._on_session_event(
        SessionEvent(SessionEventType.RESPONSE_STARTED, "session", turn_id=1)
    )
    session._on_session_event(
        SessionEvent(SessionEventType.RESPONSE_FIRST_OUTPUT, "session", turn_id=1)
    )
    await session.playback.write(b"\x01\x00" * 960, turn_id=1)
    await asyncio.wait_for(stream.send_started.wait(), timeout=0.1)
    session._on_session_event(
        SessionEvent(SessionEventType.RESPONSE_COMPLETED, "session", turn_id=1)
    )
    await asyncio.sleep(0)

    assert "idle" not in [item["state"] for item in stream.agent_states]

    stream.release_send.set()
    await asyncio.wait_for(session.playback.wait_until_idle(), timeout=0.1)
    if session._agent_state_tasks:
        await asyncio.gather(*session._agent_state_tasks)

    assert [item["state"] for item in stream.agent_states][-1] == "idle"


@pytest.mark.asyncio
async def test_web_presentation_sends_sentence_headline():
    stream = FakeStream()
    stream.presentations = []

    async def send_presentation(payload):
        stream.presentations.append(payload)

    stream.send_presentation = send_presentation
    session = WebCallSession(
        stream=stream,
        call_sid="opaque",
        session=FakeKernelSession(),
    )
    session._on_session_event(
        agent_event("session", 1, "sentence", text="Done, the board is running")
    )
    await asyncio.gather(*session._agent_state_tasks)
    line = stream.presentations[-1]["lines"][0]
    assert line["text"] == "Done, the board is running"
    assert line["spokenIndex"] == 0
    assert "words" not in line  # the page splits the text itself


def test_profile_context_defaults_to_persisted_hermes_session():
    config = SleeptalkConfig(
        agent=AgentConfig(
            type="hermes_gateway",
            gateway_session_id="telegram-session-42",
        )
    )
    context = CallContext("d3gen", "", "telegram-user")

    result = _context_for_profile(config, context)

    assert result.conversation_id == "telegram-session-42"
    assert config.agent.gateway_session_id == "telegram-session-42"


def test_explicit_conversation_rebinds_hermes_profile():
    config = SleeptalkConfig(
        agent=AgentConfig(type="hermes_gateway", gateway_session_id="old-session")
    )

    result = _context_for_profile(
        config,
        CallContext("d3gen", "originating-session", "telegram-user"),
    )

    assert result.conversation_id == "originating-session"
    assert config.agent.gateway_session_id == "originating-session"


def test_hermes_without_a_saved_session_starts_a_fresh_one():
    # Setup saves a Hermes started per call with no session to continue; the
    # web call starts anyway and the gateway opens a new session.
    config = SleeptalkConfig(agent=AgentConfig(type="hermes_gateway", slug="hermes-bot", gateway_session_id=""))

    result = _context_for_profile(config, CallContext("hermes-bot", "", "local-user"))

    assert result.conversation_id == "hermes:hermes-bot:new-web-call"
    assert config.agent.gateway_session_id == ""


def test_pi_profile_gets_a_fresh_web_call_conversation():
    config = SleeptalkConfig(agent=AgentConfig(type="pi", slug="pi-bot", gateway_session_id=""))

    result = _context_for_profile(config, CallContext("pi-bot", "", "user"))

    assert result.conversation_id == "pi:pi-bot:new-web-call"


def test_claude_profile_creates_fresh_web_call_session():
    config = SleeptalkConfig(agent=AgentConfig(type="claude_code", slug="deepre"))

    result = _context_for_profile(config, CallContext("deepre", "", "local-user"))

    assert result.conversation_id == "claude:deepre:new-web-call"


@pytest.mark.asyncio
async def test_web_turn_uses_shared_latency_boundaries(monkeypatch):
    traces = []

    class FakeTrace:
        def __init__(self, **fields):
            self.fields = fields
            self.marks = []
            traces.append(self)

        def mark(self, stage, **fields):
            self.marks.append((stage, fields))

        def finish(self, stage="turn_finished", **fields):
            self.mark(stage, **fields)

    monkeypatch.setattr("sleeptalk.web_voice.session.VoiceLatencyTrace", FakeTrace)
    kernel = FakeKernelSession()
    kernel.session_id = "web-sess"
    kernel.config = SimpleNamespace(
        agent=SimpleNamespace(type="pi", name="deepre", model=""),
        route=SimpleNamespace(type="cascade"),
        stt=SimpleNamespace(provider="deepgram", model="nova-3"),
        tts=SimpleNamespace(provider="cartesia", model="", voice_id=""),
        policy=SimpleNamespace(reply_length="full"),
    )
    web_session = WebCallSession(
        session=kernel,
        stream=FakeStream(),
        call_sid="opaque",
        surface="deepre-web",
        persona="deepre",
    )
    pcm = b"\x01\x00" * 3_840

    await web_session._handle_utterance({}, pcm)

    assert kernel.turns == [
        {
            "pcm": b"\x01\x00" * 1_920,
            "text": None,
            "extras": {"participant_id": 0, "latency_trace": traces[0]},
        }
    ]
    assert traces[0].fields == {
        "surface": "deepre-web",
        "transport": "web-webrtc",
        "persona": "deepre",
        "metadata": {
            "agent": "pi",
            "agentName": "deepre",
            "latencySchemaVersion": 2,
            "route": "cascade",
            "sttProvider": "deepgram",
            "sttModel": "nova-3",
            "ttsProvider": "cartesia",
            "replyLength": "full",
            "sessionId": "web-sess",
            "callProvider": "webrtc",
            "userId": "browser",
        },
    }
    stages = [stage for stage, _fields in traces[0].marks]
    assert stages == [
        "utterance_ready",
        "turn_complete",
    ]
    await web_session.close()


@pytest.mark.asyncio
async def test_web_session_publishes_agent_notes_beside_spoken():
    posts = []

    async def record(user_text, assistant_text, source_text="", spoken_text=""):
        posts.append((user_text, assistant_text, source_text, spoken_text))

    kernel = FakeKernelSession()
    kernel.events = []
    web_session = WebCallSession(
        session=kernel,
        stream=FakeStream(),
        call_sid="opaque",
        on_transcript=record,
    )
    emit = kernel.subscribers[0]
    emit(transcript("s", 1, speaker=Speaker.USER, text="research prices", final=True))
    emit(
        transcript(
            "s",
            1,
            speaker=Speaker.ASSISTANT,
            text="I'll check that now.",
            final=True,
            owner="realtime",
        )
    )
    emit(
        transcript(
            "s",
            1,
            speaker=Speaker.ASSISTANT,
            text="The agent found three prices.",
            final=True,
            owner="specialist",
        )
    )
    emit(
        transcript(
            "s",
            1,
            speaker=Speaker.ASSISTANT,
            text="Three prices found.",
            final=True,
            owner="realtime",
        )
    )
    if web_session._agent_state_tasks:
        await asyncio.gather(*tuple(web_session._agent_state_tasks))

    assert posts[-1] == (
        "research prices",
        "Three prices found.",
        "The agent found three prices.",
        "Three prices found.",
    )
    await web_session.close()


@pytest.mark.asyncio
async def test_relay_refusal_after_the_call_ends_is_one_log_line(caplog):
    """The relay revokes the TURN credential when a call ends; aioice's
    unawaited send then fails with 401. That becomes a log line, and any
    other unretrieved error still reaches the loop's handler."""
    import gc
    import logging

    from aioice import stun
    from sleeptalk.web_call import _quiet_relay_teardown

    seen = []
    loop = asyncio.get_running_loop()
    loop.set_exception_handler(lambda _loop, context: seen.append(context.get("exception")))

    async def send_data():
        raise stun.TransactionFailed(stun.Message(message_method=stun.Method.CHANNEL_BIND,
                                                  message_class=stun.Class.ERROR))

    async def something_else():
        raise RuntimeError("real bug")

    try:
        with caplog.at_level(logging.INFO, logger="sleeptalk.web_call"), _quiet_relay_teardown():
            for coro in (send_data(), something_else()):
                task = loop.create_task(coro)
                await asyncio.sleep(0)
                del task
            gc.collect()
            await asyncio.sleep(0)
        assert [type(exc).__name__ for exc in seen] == ["RuntimeError"]
        assert "TURN relay refused a send" in caplog.text
    finally:
        loop.set_exception_handler(None)


class _TranscribingKernel(FakeKernelSession):
    def __init__(self, policy, text):
        super().__init__(policy)
        self.text = text

    async def transcribe(self, pcm, *, participant_id=0):
        return SimpleNamespace(text=self.text)


def _soft_barge_session(monkeypatch, heard: str):
    gate = ScriptedBargeVad(_loud_speech())
    monkeypatch.setattr("sleeptalk.web_voice.session.SileroVadGate", lambda _config: gate)
    policy = VoicePolicy(resume_grace_ms=0, interrupt=InterruptPolicyValues(mode="soft", grace_ms=0))
    kernel = _TranscribingKernel(policy, heard)
    session = WebCallSession(stream=FakeStream(), call_sid="opaque", session=kernel)
    session.playback.active = True
    session._clock_ms = 0
    return session, kernel


@pytest.mark.asyncio
async def test_a_soft_barge_pauses_the_answer_and_a_backchannel_resumes_it(monkeypatch):
    """ "Mm-hmm" (or "Ajá", "嗯嗯") while the agent talks must not cancel the answer."""
    for heard in ("Mm-hmm.", "Ajá.", "嗯嗯。"):
        session, kernel = _soft_barge_session(monkeypatch, heard)
        session._turn_in_flight = True
        await session._watch_web_barge_in(bytes(3_840))
        session._clock_ms = 800
        await _watch_frames(session)
        assert session._soft_barge_pending is True
        assert kernel.aborts == 0 and session.playback.cancelled is False  # paused, not stopped
        await session._handle_utterance(object(), (2000).to_bytes(2, "little", signed=True) * 9_600)
        assert session._soft_barge_pending is False
        assert kernel.aborts == 0 and kernel.turns == []  # resumed; no new turn
        session._turn_in_flight = False
        await session.close()


@pytest.mark.asyncio
async def test_a_soft_barge_with_real_words_stops_the_answer_and_asks_them(monkeypatch):
    session, kernel = _soft_barge_session(monkeypatch, "Wait, stop. What's the capital of France?")
    session._turn_in_flight = True
    await session._watch_web_barge_in(bytes(3_840))
    session._clock_ms = 800
    await _watch_frames(session)
    await session._handle_utterance(object(), (2000).to_bytes(2, "little", signed=True) * 9_600)
    assert kernel.aborts == 1 and session.playback.cancelled is True
    # The words that decided it are the new turn, not transcribed a second time.
    assert [turn["text"] for turn in kernel.turns] == ["Wait, stop. What's the capital of France?"]
    await session.close()


class _SlowKernel(FakeKernelSession):
    """The first turn keeps working until interrupted; later ones return."""

    def __init__(self, policy):
        super().__init__(policy)
        self.release = asyncio.Event()

    async def run_turn(self, *, pcm=None, text=None, extras=None):
        self.turns.append({"pcm": pcm, "text": text, "extras": extras})
        if len(self.turns) == 1:
            await self.release.wait()
            return TurnOutcome(turn_id=1, status="cancelled")
        return TurnOutcome(turn_id=len(self.turns), status="completed")

    async def interrupt_presentation(self):
        self.aborts += 1
        self.release.set()


@pytest.mark.asyncio
async def test_speech_right_after_a_turn_ends_is_the_rest_of_it():
    """ "What's two plus two?" ends the turn; "Just the number, please." starts
    within the resume grace, before the agent is heard: the first answer is
    cancelled and the turn is asked again with both halves."""
    kernel = _SlowKernel(VoicePolicy(resume_grace_ms=750))
    session = WebCallSession(stream=FakeStream(), call_sid="opaque", session=kernel)
    first = (2000).to_bytes(2, "little", signed=True) * 9_600
    rest = (3000).to_bytes(2, "little", signed=True) * 9_600
    turn = asyncio.create_task(session._handle_utterance(object(), first))
    await asyncio.sleep(0)
    assert session._turn_in_flight is True
    assert session._collect_during_turn(session.PARTICIPANT_ID) is True  # in the grace
    await session._handle_utterance(object(), rest)
    await turn
    assert kernel.aborts == 1
    assert len(kernel.turns) == 2
    from sleeptalk.web_voice.session import _stereo_to_mono_s16

    assert kernel.turns[1]["pcm"] == _stereo_to_mono_s16(first + rest)
    assert session._turn_in_flight is False
    await session.close()


@pytest.mark.asyncio
async def test_speech_long_after_a_turn_ends_is_working_speech_not_a_continuation():
    kernel = _SlowKernel(VoicePolicy(resume_grace_ms=750))
    session = WebCallSession(stream=FakeStream(), call_sid="opaque", session=kernel)
    turn = asyncio.create_task(session._handle_utterance(object(), (2000).to_bytes(2, "little", signed=True) * 9_600))
    await asyncio.sleep(0)
    session._last_commit_at -= 2.0  # two seconds on, the agent still working in silence
    assert session._collect_during_turn(session.PARTICIPANT_ID) is True
    assert session._grace_continuation is False and session._working_speech is True
    kernel.release.set()
    await turn
    await session.close()


class _SlowTranscribingKernel(_SlowKernel):
    def __init__(self, policy, text):
        super().__init__(policy)
        self.text = text

    async def transcribe(self, pcm, *, participant_id=0):
        return SimpleNamespace(text=self.text)


@pytest.mark.asyncio
async def test_words_said_while_the_agent_works_in_silence_interrupt_it():
    """ "Wait, stop. What's the capital of France?" while the agent thinks used
    to be dropped; now it interrupts the work and becomes the turn."""
    kernel = _SlowTranscribingKernel(VoicePolicy(resume_grace_ms=750), "Wait, stop. What's the capital of France?")
    session = WebCallSession(stream=FakeStream(), call_sid="opaque", session=kernel)
    speech = (2000).to_bytes(2, "little", signed=True) * 9_600
    turn = asyncio.create_task(session._handle_utterance(object(), speech))
    await asyncio.sleep(0)
    session._last_commit_at -= 2.0
    assert session._collect_during_turn(session.PARTICIPANT_ID) is True
    await session._handle_utterance(object(), speech)
    await turn
    assert kernel.aborts == 1
    assert kernel.turns[1]["text"] == "Wait, stop. What's the capital of France?"
    await session.close()


@pytest.mark.asyncio
async def test_yes_to_the_agents_question_answers_it_and_leaves_the_work_alone():
    """A pi or Claude Code agent set to ask waits on a permission; "Yes, go
    ahead" used to interrupt (and so cancel) the very work it approved."""
    kernel = _SlowTranscribingKernel(VoicePolicy(resume_grace_ms=750), "Yes, go ahead.")
    answers: list[str] = []

    async def answer_pending(text):
        answers.append(text)
        return True

    kernel.answer_pending = answer_pending
    session = WebCallSession(stream=FakeStream(), call_sid="opaque", session=kernel)
    speech = (2000).to_bytes(2, "little", signed=True) * 9_600
    turn = asyncio.create_task(session._handle_utterance(object(), speech))
    await asyncio.sleep(0)
    session._last_commit_at -= 2.0
    session._collect_during_turn(session.PARTICIPANT_ID)
    await session._handle_utterance(object(), speech)
    assert answers == ["Yes, go ahead."]
    assert kernel.aborts == 0 and len(kernel.turns) == 1
    kernel.release.set()
    await turn
    await session.close()


@pytest.mark.asyncio
async def test_a_backchannel_while_the_agent_works_leaves_it_alone():
    kernel = _SlowTranscribingKernel(VoicePolicy(resume_grace_ms=750), "Mm-hmm.")
    session = WebCallSession(stream=FakeStream(), call_sid="opaque", session=kernel)
    speech = (2000).to_bytes(2, "little", signed=True) * 9_600
    turn = asyncio.create_task(session._handle_utterance(object(), speech))
    await asyncio.sleep(0)
    session._last_commit_at -= 2.0
    session._collect_during_turn(session.PARTICIPANT_ID)
    await session._handle_utterance(object(), speech)
    assert kernel.aborts == 0 and len(kernel.turns) == 1
    kernel.release.set()
    await turn
    await session.close()


@pytest.mark.asyncio
async def test_a_paused_answer_still_lets_the_callers_words_reach_the_listener(monkeypatch):
    """A soft barge pauses playback (still "active"); the streaming listener must
    hear the words that decide it, or it transcribes only the first of them."""
    session, _kernel = _soft_barge_session(monkeypatch, "")
    session._turn_in_flight = True
    session._web_barge_capture_active = True
    session._soft_barge_pending = True
    frame = AudioFrame(format="s16", layout="stereo", samples=960)
    frame.sample_rate = 48_000
    frame.planes[0].update(b"\x01\x00\x03\x00" * 960)

    await session.feed_audio_frame(frame)

    assert _kernel.appended == [(b"\x02\x00" * 960, session.PARTICIPANT_ID)]
    session._disarm_soft_barge()
    session._turn_in_flight = False
    await session.close()


@pytest.mark.asyncio
async def test_a_barge_right_after_a_turn_ends_is_asked_with_both_halves(monkeypatch):
    """The agent had only just started on "I'd like to know about" when the caller
    went on: the rest belongs to the same turn, so both halves are asked again."""
    from sleeptalk.web_voice.session import _stereo_to_mono_s16

    # (seconds since the commit, seconds of answer heard, merged). Live zh:
    # the caller went on 0.7 s into the answer, past the plain window after
    # the commit. 1.4 s into the answer is time enough to be replying to it.
    for gap, heard, merged in ((0.5, None, True), (2.0, 0.7, True), (2.0, 1.4, False), (5.0, None, False)):
        session, kernel = _soft_barge_session(monkeypatch, "the history of tea, in two sentences")
        session.assembler.resume_grace_ms = 750
        first = (2000).to_bytes(2, "little", signed=True) * 9_600
        rest = (3000).to_bytes(2, "little", signed=True) * 9_600
        await session._handle_utterance(object(), first)  # the first half, answered at once
        session._last_commit_at -= gap
        if heard is not None:
            session._first_output_at = time.monotonic() - heard
        session._turn_in_flight = True
        session.playback.active = True
        await session._watch_web_barge_in(bytes(3_840))
        session._clock_ms = 800
        await _watch_frames(session)
        await session._handle_utterance(object(), rest)
        last = kernel.turns[-1]
        if merged:
            assert last["pcm"] == _stereo_to_mono_s16(first + rest) and last["text"] is None
        else:
            assert last["text"] == "the history of tea, in two sentences"
        session._turn_in_flight = False
        await session.close()


@pytest.mark.asyncio
async def test_the_worker_panels_cancel_button_stops_the_job():
    import asyncio
    from types import SimpleNamespace

    session, kernel = _speed_session()
    cancelled = []

    async def cancel(job):
        cancelled.append(job.id)
        return True

    shown = SimpleNamespace(id="job-1", active=True, queued=False)
    other = SimpleNamespace(id="job-2", active=False, queued=True)
    kernel.jobs = SimpleNamespace(active=shown, jobs=(shown, other), cancel=cancel)
    session.apply_settings({"cancelJob": True})
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert cancelled == ["job-1"]
    # A panel that names its job cancels that one, queued or not.
    session.apply_settings({"cancelJob": "job-2"})
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert cancelled == ["job-1", "job-2"]
    kernel.jobs = SimpleNamespace(active=None, jobs=(), cancel=cancel)
    session.apply_settings({"cancelJob": True})  # nothing running: nothing to do
    await asyncio.sleep(0)
    assert cancelled == ["job-1", "job-2"]


@pytest.mark.asyncio
async def test_a_tab_tap_moves_the_hosts_selection():
    from types import SimpleNamespace

    session, kernel = _speed_session()
    picked = []
    kernel.jobs = SimpleNamespace(select=picked.append)
    session.apply_settings({"selectJob": "job-2"})
    session.apply_settings({"selectJob": ""})  # nothing named: ignored
    assert picked == ["job-2"]
