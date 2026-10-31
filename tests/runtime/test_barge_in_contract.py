from __future__ import annotations

import inspect
import struct

from sleeptalk.discord_native.turns import DiscordTurnCoordinator
from sleeptalk.web_voice.playback import WebRTCPlaybackController
from sleeptalk.web_voice.session import WebCallSession
from sleeptalk.interrupt import AcousticKind, Floor, InterruptPolicy, SpeakingState
from sleeptalk.transports.terminal import _CaptureState, _pcm_rms


def _pcm(samples: int, amplitude: int) -> bytes:
    return b"".join(struct.pack("<h", amplitude) for _ in range(samples))


def test_web_playback_active_is_not_the_floor():
    watch = inspect.getsource(WebCallSession._watch_web_barge_in)
    # Playback being active is not the same thing as owning the floor, and
    # the transport no longer decides either: it hands evidence to the gate.
    assert "self._gate.idle" in watch
    assert "self._turn_in_flight or self.playback.active" not in watch
    assert "self._floor" not in inspect.getsource(WebCallSession)
    playout = inspect.getsource(WebRTCPlaybackController._run_playout)
    assert "self._mark_drain()" in playout
    floor = Floor(InterruptPolicy(), AcousticKind.isolated, detector="silero")
    floor.observe(0, "turn_start")
    floor.observe(0, "audio_start")
    floor.observe(1500, "drain")
    leftover = floor.observe(1800, "mic_speech")
    assert leftover.fire is False
    assert leftover.state is SpeakingState.hold


def test_discord_floor_holds_leftover_in_gap():
    observe = inspect.getsource(DiscordTurnCoordinator.observe_pcm)
    # The transport reports evidence and executes a decision. Reaching the
    # Floor directly is how the three surfaces drifted apart; only BargeGate
    # may admit speech to it.
    assert "self._gate.observe" in observe
    assert "self._floor" not in inspect.getsource(DiscordTurnCoordinator)
    assert "_EPISODE_GAP_SECONDS" not in inspect.getsource(DiscordTurnCoordinator)
    floor = Floor(InterruptPolicy(), AcousticKind.isolated, detector="silero")
    floor.observe(0, "turn_start")
    floor.observe(0, "audio_start")
    floor.observe(1500, "drain")
    leftover = floor.observe(1800, "mic_speech")
    assert leftover.fire is False
    assert leftover.state is SpeakingState.hold


def test_terminal_interrupt_uses_residual_rms_not_silero():
    source = inspect.getsource(_CaptureState.observe)
    assert "silero" not in source.lower()
    assert "_pcm_rms" in source
    loud = _pcm(960, 20_000)
    quiet = _pcm(960, 0)
    assert _pcm_rms(loud) > _pcm_rms(quiet)
    capture = _CaptureState(
        threshold=500,
        start_ms=250,
        silence_ms=400,
        interrupt_ms=250,
        bot_grace_ms=400,
    )
    capture.observe(loud, energy=loud)
    assert capture.speech_streak_ms > 0
    capture.reset()
    capture.observe(loud, energy=quiet)
    assert capture.speech_streak_ms == 0
