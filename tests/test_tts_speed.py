import pytest

from sleeptalk.defaults import register_defaults
from sleeptalk.registries import TTS_PROVIDERS
from sleeptalk.speech_speed import (
    SPEED_STEPS,
    SpeedProfile,
    declare_speed_profile,
    declared_speed_providers,
    format_speed,
    next_speed,
    snap_speed,
    speed_profile,
    wire_speed,
)


def test_snap_speed_none_is_one() -> None:
    assert snap_speed(None) == 1.0


def test_snap_speed_exact_steps() -> None:
    for step in SPEED_STEPS:
        assert snap_speed(step) == step


def test_snap_speed_off_grid_rounds_to_one() -> None:
    assert snap_speed(0.8) == 1.0


def test_snap_speed_tie_picks_higher() -> None:
    assert snap_speed(0.75) == 1.0


def test_snap_speed_is_idempotent() -> None:
    for value in (None, 0.5, 0.75, 0.8, 1.125, 1.8, 3.0):
        snapped = snap_speed(value)
        assert snap_speed(snapped) == snapped
        assert snapped in SPEED_STEPS


def test_staircase_stops_at_one_and_a_half() -> None:
    # 2x was removed: no supported cloud provider plays it.
    assert SPEED_STEPS == (0.5, 1.0, 1.25, 1.5)
    assert snap_speed(2.0) == 1.5


def test_next_speed_advances_and_wraps() -> None:
    assert next_speed(None) == 1.25
    assert next_speed(1.0) == 1.25
    assert next_speed(1.25) == 1.5
    assert next_speed(1.5) == 0.5
    assert next_speed(0.5) == 1.0


def test_wire_speed_cartesia_clamps() -> None:
    assert wire_speed("cartesia", 0.5) == 0.6
    assert wire_speed("cartesia", 1.5) == 1.5
    assert wire_speed("cartesia", 1.25) == 1.25
    assert wire_speed("Cartesia", None) == 1.0


def test_wire_speed_unknown_provider_snaps_only() -> None:
    assert wire_speed("unknown", 0.8) == 1.0
    assert wire_speed("nonexistent", 2.0) == 1.5


def test_wire_speed_deepgram_clamps_to_documented_range() -> None:
    # 0.7 to 1.5; Deepgram answers 400 outside it.
    assert wire_speed("deepgram", 0.5) == 0.7
    assert wire_speed("deepgram", 1.5) == 1.5
    assert wire_speed("deepgram", 1.25, model="aura-2-thalia-en") == 1.25


def test_wire_speed_elevenlabs_clamps_to_observed_range() -> None:
    # The API rejected 1.25 with "greater or equal to 0.7 and less or equal to 1.2".
    assert wire_speed("elevenlabs", 1.25, model="eleven_multilingual_v2") == 1.2
    assert wire_speed("elevenlabs", 1.5) == 1.2
    assert wire_speed("elevenlabs", 0.5) == 0.7
    assert wire_speed("elevenlabs", 1.0) == 1.0


def test_wire_speed_openai_realtime_clamps_to_its_supported_range() -> None:
    assert wire_speed("openai-realtime", 0.5) == 0.5
    assert wire_speed("openai-realtime", 1.5) == 1.5


def test_wire_speed_local_passes_every_step_for_piper_and_kokoro() -> None:
    for model in ("piper-lessac-medium", "kokoro-en"):
        for step in SPEED_STEPS:
            assert wire_speed("local", step, model=model) == step


def test_wire_speed_rime_reciprocates_for_mist_v2_only() -> None:
    # Mist and Mist v2: lower speedAlpha is faster. Mist v3, Arcana, Coda: higher is faster.
    assert wire_speed("rime", 1.25, model="mistv2") == 0.8
    assert wire_speed("rime", 1.5, model="mistv2") == 0.6667
    assert wire_speed("rime", 0.5, model="mist") == 2.0
    assert wire_speed("rime", 1.25, model="mistv3") == 1.25
    assert wire_speed("rime", 1.25, model="arcana") == 1.25
    assert wire_speed("rime", 1.0, model="mistv2") == 1.0


def test_every_step_lands_inside_each_declared_profile() -> None:
    register_defaults()
    for name in sorted(declared_speed_providers()):
        profile = speed_profile(name)
        assert profile is not None
        for step in SPEED_STEPS:
            wired = wire_speed(name, step)
            assert profile.low <= wired <= profile.high, (name, step, wired)


def test_every_registered_tts_provider_declares_a_speed_profile() -> None:
    """A new TTS adapter must declare its accepted interval in speech_speed.py.

    Without one, an operator step outside the provider's interval reaches the
    wire unchanged, the provider answers 400 on every sentence, and the caller
    hears nothing. See the module docstring in sleeptalk/speech_speed.py.
    """
    register_defaults()
    declared = declared_speed_providers()
    missing = sorted(
        registration.name
        for registration in TTS_PROVIDERS.registrations()
        if registration.name not in declared
    )
    assert not missing, (
        f"TTS providers without a speed profile in sleeptalk/speech_speed.py: {missing}"
    )


def test_declared_profiles_cite_a_source() -> None:
    for name in declared_speed_providers():
        profile = speed_profile(name)
        assert profile is not None and profile.source, name
        assert 0 < profile.low <= 1.0 <= profile.high, name


def test_declare_speed_profile_for_an_external_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    from sleeptalk import speech_speed

    monkeypatch.setattr(speech_speed, "_SPEED_PROFILES", dict(speech_speed._SPEED_PROFILES))
    declare_speed_profile("orchard", SpeedProfile(0.8, 1.1, source="fixture"))
    assert wire_speed("orchard", 1.5) == 1.1
    assert wire_speed("orchard", 0.5) == 0.8
    declare_speed_profile("orchard", SpeedProfile(0.5, 2.0, inverted=True, source="fixture"), model="slow")
    assert wire_speed("orchard", 0.5, model="slow") == 2.0
    assert wire_speed("orchard", 1.5, model="other") == 1.1


def test_format_speed_drops_trailing_zero() -> None:
    assert format_speed(1.0) == "1x"
    assert format_speed(1.25) == "1.25x"
    assert format_speed(0.5) == "0.5x"
    assert format_speed(None) == "1x"


def test_wire_speed_is_idempotent_for_direct_profiles() -> None:
    for provider, value in (
        ("cartesia", 0.5),
        ("cartesia", 1.5),
        ("elevenlabs", 0.8),
        ("elevenlabs", 1.25),
        ("deepgram", 0.5),
        ("hume", None),
        ("missing", 1.8),
    ):
        first = wire_speed(provider, value)
        assert wire_speed(provider, first) == first
