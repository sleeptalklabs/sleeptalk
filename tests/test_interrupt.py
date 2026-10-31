from __future__ import annotations

import pytest
from sleeptalk.config import ConfigError, PolicyConfig, load_config
from sleeptalk.interrupt import AcousticKind, Floor, InterruptPolicy, SpeakingState
from sleeptalk.policy import VoicePolicy

MINIMAL = """\
[transport]
type = "null"

[agent]
type = "pi"
"""


def _drive(events, *, kind=AcousticKind.isolated, policy=None, detector="none"):
    floor = Floor(policy or InterruptPolicy(), kind, detector=detector)
    return [floor.observe(t, event) for t, event in events]


def test_hold_leftover_mic_300ms_after_drain():
    hits = _drive(
        [
            (0, "turn_start"),
            (0, "audio_start"),
            (1500, "drain"),
            (1800, "mic_speech"),
        ]
    )
    leftover = hits[-1]
    assert leftover.fire is False
    assert leftover.state is SpeakingState.hold
    assert leftover.reason == "held. leftover mic in the sentence gap"


def test_fire_stop_800ms_into_first_clip_after_grace():
    hits = _drive(
        [
            (0, "turn_start"),
            (0, "audio_start"),
            (800, "mic_speech"),
        ]
    )
    stop = hits[-1]
    assert stop.fire is True
    assert stop.reason == "interrupt while audio plays"


def test_later_clip_still_holds_through_start_grace():
    hits = _drive(
        [
            (0, "turn_start"),
            (0, "audio_start"),
            (1500, "drain"),
            (1600, "audio_start"),
            (1800, "mic_speech"),
        ]
    )
    leftover = hits[-1]
    assert leftover.fire is False
    assert leftover.reason == "held. still in start grace"


def test_mic_before_first_audio_does_not_fire():
    hits = _drive(
        [
            (0, "turn_start"),
            (200, "mic_speech"),
        ]
    )
    leftover = hits[-1]
    assert leftover.fire is False
    assert leftover.reason == "held. waiting for first audio"


def test_fire_after_gap_hold_expires():
    hits = _drive(
        [
            (0, "turn_start"),
            (0, "audio_start"),
            (1500, "drain"),
            (2200, "mic_speech"),
        ]
    )
    stop = hits[-1]
    assert stop.fire is True
    assert stop.reason == "interrupt in a long gap after hold"


def test_queue_empty_is_the_same_hold_as_drain():
    hits = _drive(
        [
            (0, "turn_start"),
            (0, "audio_start"),
            (1500, "queue_empty"),
            (1800, "mic_speech"),
        ]
    )
    assert hits[-1].fire is False
    assert hits[-1].state is SpeakingState.hold


def test_reject_silero_on_mixed():
    with pytest.raises(ValueError, match="Silero cannot run on mixed audio"):
        Floor(InterruptPolicy(), AcousticKind.mixed, detector="silero")


def test_isolated_silero_is_allowed():
    floor = Floor(InterruptPolicy(), AcousticKind.isolated, detector="silero")
    assert floor.kind is AcousticKind.isolated


def test_pre_release_turn_barge_keys_move_to_interrupt(tmp_path):
    # An old home sleeptalk.toml carried these; every call failed to start.
    path = tmp_path / "sleeptalk.toml"
    path.write_text(MINIMAL + "\n[policy.turns]\nsilero_barge_min_ms = 180\nsilero_barge_min_volume = 0.04\n")
    config = load_config(path, check_env=False, check_binaries=False)
    assert (config.policy.interrupt.min_duration_ms, config.policy.interrupt.min_volume) == (180, 0.04)
    assert "silero_barge_min_ms" not in config.policy.turns.model_dump()
    # A value set where it lives now wins over the old key.
    path.write_text(MINIMAL + "\n[policy.turns]\nsilero_barge_min_ms = 180\n[policy.interrupt]\nmin_duration_ms = 120\n")
    assert load_config(path, check_env=False, check_binaries=False).policy.interrupt.min_duration_ms == 120


def test_leftover_policy_barge_in_fails_load(tmp_path):
    path = tmp_path / "sleeptalk.toml"
    path.write_text(MINIMAL + '\n[policy]\nbarge_in = "soft"\n')
    with pytest.raises(ConfigError, match="barge_in") as excinfo:
        load_config(path, check_env=False, check_binaries=False)
    assert "Unknown key 'barge_in'" in str(excinfo.value)


def test_mode_adaptive_fails_load(tmp_path):
    path = tmp_path / "sleeptalk.toml"
    path.write_text(MINIMAL + '\n[policy.interrupt]\nmode = "adaptive"\n')
    with pytest.raises(ConfigError, match="policy.interrupt.mode"):
        load_config(path, check_env=False, check_binaries=False)


def test_soft_mode_maps_onto_barge_in_mode():
    policy = VoicePolicy.from_config(PolicyConfig(interrupt={"mode": "soft"}))
    assert policy.interrupt.mode == "soft"
    assert policy.barge_in == "soft"
    assert policy.barge_in_mode == "transcript"
