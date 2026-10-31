"""The language picks the model; the user never has to."""

from __future__ import annotations

from sleeptalk import language_support as ls
from sleeptalk.builder import stt_spec
from sleeptalk.config import STTConfig


def test_launch_model_swaps_to_the_wider_one_for_a_degraded_language() -> None:
    assert ls.resolve_stt_model("deepgram", "bg-BG") == "nova-2"
    assert ls.resolve_stt_model("deepgram", "bg-BG", "nova-3") == "nova-2"
    assert ls.resolve_stt_model("cartesia", "ur-PK", "ink-2") == "ink-whisper"


def test_full_tier_and_english_keep_the_launch_model() -> None:
    assert ls.resolve_stt_model("deepgram", "fr-FR") == "nova-3"
    assert ls.resolve_stt_model("deepgram", "en-US", "nova-3") == "nova-3"
    assert ls.resolve_stt_model("cartesia", "en-US") == "ink-2"


def test_an_explicit_non_default_model_is_the_users_choice() -> None:
    assert ls.resolve_stt_model("deepgram", "fr-FR", "nova-2") == "nova-2"
    assert ls.resolve_stt_model("cartesia", "en-US", "ink-whisper") == "ink-whisper"


def test_unsupported_language_keeps_the_launch_model_and_warns_elsewhere() -> None:
    # Urdu is not in nova-2 either: nothing to swap to, doctor/wizard warn.
    assert ls.resolve_stt_model("deepgram", "ur-PK") == "nova-3"
    assert ls.lookup("ur-PK", "deepgram").tier == ls.TIER_UNSUPPORTED


def test_builder_applies_the_swap_at_build_time() -> None:
    spec = stt_spec(STTConfig(provider="cartesia", model="ink-2", language="de-DE"))
    assert spec.model == "ink-whisper"
    # ink-2 covers French (with Spanish, Hindi, and Japanese) since Sept 2026.
    spec = stt_spec(STTConfig(provider="cartesia", model="ink-2", language="fr-FR"))
    assert spec.model == "ink-2"
    spec = stt_spec(STTConfig(provider="cartesia", model="ink-2", language="en-US"))
    assert spec.model == "ink-2"
    spec = stt_spec(STTConfig(provider="deepgram", model="nova-3", language="hi-IN"))
    assert spec.model == "nova-3"


def test_tts_model_resolution_swaps_only_where_the_default_misses_the_language() -> None:
    assert ls.resolve_tts_model("cartesia", "fr-FR") == "sonic-3.6"
    # Sonic 3.6 doesn't list Lithuanian; Sonic 3 does. A chosen model stays.
    assert ls.resolve_tts_model("cartesia", "lt-LT") == "sonic-3"
    assert ls.resolve_tts_model("cartesia", "lt-LT", "sonic-3.6") == "sonic-3"
    assert ls.resolve_tts_model("cartesia", "lt-LT", "sonic-preview") == "sonic-preview"
    assert ls.lookup("lt-LT", "cartesia").tier == ls.TIER_FULL
    assert ls.resolve_tts_model("elevenlabs", "ja-JP", "eleven_flash_v2_5") == "eleven_flash_v2_5"


def test_support_for_combines_hearing_speaking_and_spoken_lines() -> None:
    hindi_on_deepgram = ls.support_for("hi-IN", "deepgram", "deepgram")
    assert hindi_on_deepgram.stt.tier == ls.TIER_FULL
    assert hindi_on_deepgram.tts.tier == ls.TIER_UNSUPPORTED  # no Aura-2 Hindi voice
    assert hindi_on_deepgram.spoken_lines
    assert not hindi_on_deepgram.supported

    french_on_cartesia = ls.support_for("fr-FR", "cartesia", "cartesia")
    assert french_on_cartesia.supported
    assert not french_on_cartesia.is_english

    english = ls.support_for("en-US", "deepgram", "deepgram")
    assert english.is_english and english.stt.ok and english.tts.ok


def test_scribe_counts_measured_urdu_as_full() -> None:
    assert ls.lookup("ur-PK", "elevenlabs-stt").tier == ls.TIER_FULL


def test_model_choice_tables_cover_the_wizard_providers() -> None:
    assert set(ls.STT_MODEL_CHOICES) == {"deepgram", "cartesia", "elevenlabs", "local"}
    assert set(ls.TTS_MODEL_CHOICES) == {"cartesia", "elevenlabs"}
    for provider, choices in ls.STT_MODEL_CHOICES.items():
        assert ls.resolve_stt_model(provider, "en-US") in {model for model, _ in choices}
