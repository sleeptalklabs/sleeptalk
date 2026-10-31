"""Tests for VoicePolicy construction from the [policy] section."""

from __future__ import annotations

from sleeptalk.config import PolicyConfig
from sleeptalk.policy import VoicePolicy


def make_policy(**overrides) -> VoicePolicy:
    return VoicePolicy.from_config(PolicyConfig(**overrides))


def test_defaults_match_legacy_env_defaults():
    policy = make_policy()
    assert policy.fillers_enabled is True
    assert policy.effective_filler_delay_ms == 800
    assert policy.allow_barge_in is True
    assert policy.interrupt.enabled is True
    assert policy.interrupt.mode == "immediate"
    assert policy.barge_in_mode == "immediate"
    assert policy.interrupt.min_duration_ms == 150
    assert policy.interrupt.grace_ms == 400
    assert policy.interrupt.gap_hold_ms == 600
    assert policy.interrupt.cooldown_ms == 1200
    assert policy.interrupt.min_volume == 0.03
    assert policy.barge_in_min_chars == 4
    assert policy.early_barge_in_ms == 900
    assert policy.turn_debounce_ms == 2000
    assert policy.post_bot_cooldown_ms == 1200
    assert policy.reply_max_sentences == 8
    assert policy.reply_max_chars == 1200
    assert policy.discord_reply_max_chars == 900


def test_barge_in_off_disables():
    policy = make_policy(interrupt={"mode": "off"})
    assert policy.allow_barge_in is False
    assert policy.interrupt.mode == "off"
    assert policy.barge_in_mode == "transcript"


def test_barge_in_immediate_maps_to_immediate_mode():
    assert make_policy(interrupt={"mode": "immediate"}).barge_in_mode == "immediate"


def test_soft_mode_maps_onto_barge_in_mode():
    policy = make_policy(interrupt={"mode": "soft"})
    assert policy.interrupt.mode == "soft"
    assert policy.barge_in == "soft"
    assert policy.barge_in_mode == "transcript"


def test_endpointing_presets_scale_thresholds():
    relaxed = make_policy(endpointing="relaxed").endpointing_config()
    balanced = make_policy(endpointing="balanced").endpointing_config()
    aggressive = make_policy(endpointing="aggressive").endpointing_config()
    assert relaxed.complete_silence_ms > balanced.complete_silence_ms
    assert aggressive.complete_silence_ms < balanced.complete_silence_ms
    assert relaxed.incomplete_silence_ms > aggressive.incomplete_silence_ms


def test_endpointing_overrides_beat_preset():
    policy = make_policy(
        endpointing="relaxed",
        endpointing_overrides={"complete_silence_ms": 1234},
    )
    config = policy.endpointing_config()
    assert config.complete_silence_ms == 1234
    # other preset values still apply
    assert config.uncertain_silence_ms == 2400


def test_filler_catalog_uses_configured_pools():
    policy = make_policy(filler_general=["One moment."], filler_web_search=["Searching."])
    catalog = policy.filler_catalog()
    assert catalog.select("general", 0) == "One moment."
    assert catalog.select("web_search", 0) == "Searching."


def test_clip_pools_default_to_legacy_lines():
    policy = make_policy()
    assert "greeting" in policy.preamble_pools
    assert "reassure" in policy.preamble_pools
    assert "bash" in policy.reassure_tool_pools
    assert policy.narration_phrases["read"] == "Reading a file."


def test_turn_config_maps_turns_section():
    policy = make_policy(turns={"vad_threshold": 700, "silero_start_ms": 400})
    config = policy.turn_config(surface="s", persona="p")
    assert config.vad_threshold == 700
    assert config.silero_start_ms == 400
    assert config.log_surface == "s"


def test_capture_maps_to_silero_config():
    policy = make_policy(capture={"vad_confidence": 0.9, "vad_start_ms": 300})
    silero = policy.silero_config()
    assert silero.confidence == 0.9
    assert silero.start_ms == 300


def test_keepalive_interval_from_config():
    from sleeptalk.config import PolicyConfig

    policy = VoicePolicy.from_config(PolicyConfig(keepalive_interval_s=7.5))
    assert policy.keepalive_interval_s == 7.5


def test_keepalive_interval_default():
    assert VoicePolicy().keepalive_interval_s == 10.0
