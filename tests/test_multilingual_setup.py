"""A non-English profile hears, speaks, and acks in its language, from setup
alone: the wizard writes what cascade reads and never touches runtime."""

from __future__ import annotations

import tomllib
import pytest

from sleeptalk import doctor, wizard
from sleeptalk.config import SleeptalkConfig
from sleeptalk.spoken_lines import for_language
from sleeptalk.speech.elevenlabs import accepts_language_code
from sleeptalk.voice_discovery import DiscoveredVoice, voices_for_language
from sleeptalk.voice_list import deepgram_voices_from_models, list_deepgram_voices


def _answers(**overrides) -> wizard.WizardAnswers:
    answers = wizard.WizardAnswers(surface="web", stt_key="k", tts_key="k", tts_voice_id="voice-1")
    for key, value in overrides.items():
        setattr(answers, key, value)
    return answers


# --- the profile carries the localized lines --------------------------------

def test_non_english_cascade_profile_carries_pools_and_delayed_ack() -> None:
    parsed = tomllib.loads(wizard.profile_toml(_answers(language="es-ES")))
    policy = parsed["policy"]
    assert policy["instant_ack_mode"] == "delayed"
    table = for_language("es")
    assert policy["preamble_pools"] == {k: list(v) for k, v in table.preamble_pools.items()}
    assert policy["reassure_tool_pools"] == {k: list(v) for k, v in table.reassure_tool_pools.items()}
    assert policy["narration_phrases"] == table.narration_phrases
    config = SleeptalkConfig.model_validate(parsed)
    assert config.policy.preamble_pools["ack:empathy"] == list(table.preamble_pools["ack:empathy"])


def test_english_profile_carries_no_pools() -> None:
    parsed = tomllib.loads(wizard.profile_toml(_answers(language="en-US")))
    assert "preamble_pools" not in parsed.get("policy", {})
    assert parsed["policy"]["instant_ack_mode"] == "delayed"


def test_language_without_a_table_keeps_english_pools() -> None:
    parsed = tomllib.loads(wizard.profile_toml(_answers(language="sw-KE")))
    assert "preamble_pools" not in parsed.get("policy", {})
    assert parsed["stt"]["language"] == "sw-KE"


def test_hybrid_profile_never_carries_pools() -> None:
    parsed = tomllib.loads(wizard.profile_toml(_answers(language="es-ES", route_type="hybrid", realtime_key="x")))
    assert "preamble_pools" not in parsed.get("policy", {})


def test_pool_keys_with_colons_are_quoted_toml_keys() -> None:
    text = wizard.profile_toml(_answers(language="fr-FR"))
    assert '"ack:empathy" = [' in text
    assert '"action:task" = [' in text


# --- the voice follows the language -----------------------------------------

def test_one_key_setup_keeps_the_english_default_voice_for_english() -> None:
    answers = wizard.WizardAnswers(language="en-US")
    wizard.apply_speech_provider(answers, "deepgram")
    assert answers.tts_voice_id == wizard.DEFAULT_DEEPGRAM_VOICE


def test_one_key_setup_leaves_the_voice_to_the_pick_for_other_languages() -> None:
    answers = wizard.WizardAnswers(language="fr-FR")
    wizard.apply_speech_provider(answers, "deepgram")
    assert answers.tts_voice_id == ""
    answers = wizard.WizardAnswers(language="ur-PK")
    notes = wizard.apply_speech_provider(answers, "cartesia")
    assert answers.tts_voice_id == ""
    assert answers.stt_model == "ink-whisper" and notes


def test_changing_language_moves_the_voice_both_ways() -> None:
    answers = wizard.WizardAnswers(tts_provider="deepgram", tts_voice_id=wizard.DEFAULT_DEEPGRAM_VOICE)
    answers.language = "de-DE"
    wizard.refresh_voice_for_language(answers, "en-US")
    assert answers.tts_voice_id == ""
    answers.tts_voice_id = "aura-2-x-de"
    answers.language = "en-US"
    wizard.refresh_voice_for_language(answers, "de-DE")
    assert answers.tts_voice_id == wizard.DEFAULT_DEEPGRAM_VOICE


def test_a_hand_picked_voice_survives_a_language_change() -> None:
    answers = wizard.WizardAnswers(tts_provider="cartesia", tts_voice_id="my-voice")
    answers.language = "it-IT"
    wizard.refresh_voice_for_language(answers, "en-US")
    assert answers.tts_voice_id == "my-voice"


@pytest.mark.asyncio
async def test_auto_pick_prefers_a_voice_tagged_with_the_language(monkeypatch) -> None:
    voices = [
        DiscoveredVoice(provider="deepgram", voice_id="flux-haley-en", name="Haley", languages=["en"]),
        DiscoveredVoice(provider="deepgram", voice_id="aura-2-celeste-es", name="Celeste", languages=["es"]),
    ]

    class Catalog:
        name = "deepgram"

        async def list_voices(self):
            return voices

    monkeypatch.setattr("sleeptalk.voice_discovery.default_catalogs", lambda credentials=None: [Catalog()])
    answers = wizard.WizardAnswers(language="es-MX", tts_provider="deepgram", tts_key="k")
    notes, failure = await wizard.auto_pick_voice(answers)
    assert failure == "" and notes == []
    assert answers.tts_voice_id == "aura-2-celeste-es"

    answers = wizard.WizardAnswers(language="hi-IN", tts_provider="deepgram", tts_key="k")
    notes, failure = await wizard.auto_pick_voice(answers)
    assert failure == ""
    assert answers.tts_voice_id == "flux-haley-en"
    assert notes and "no voice tagged Hindi" in notes[0]


def test_voices_for_language_filters_by_primary_tag_and_keeps_untagged() -> None:
    voices = [
        DiscoveredVoice(provider="x", voice_id="a", name="a", languages=["en-US"]),
        DiscoveredVoice(provider="x", voice_id="b", name="b", languages=["fr"]),
        DiscoveredVoice(provider="x", voice_id="c", name="c", languages=[]),
    ]
    assert [v.voice_id for v in voices_for_language(voices, "fr-CA")] == ["b"]
    assert [v.voice_id for v in voices_for_language(voices, "ja-JP")] == ["c"]
    assert [v.voice_id for v in voices_for_language(voices, "")] == ["a", "b", "c"]


# --- Deepgram voices carry a language ----------------------------------------

def test_deepgram_seed_list_has_flux_first_and_aura2_tagged() -> None:
    voices = list_deepgram_voices()
    assert voices[0].id == "flux-haley-en" and voices[0].language == "en"
    spanish = [v for v in voices if v.language == "es"]
    assert spanish and all(v.id.startswith("aura-2-") and v.id.endswith("-es") for v in spanish)


def test_deepgram_models_document_becomes_tagged_voices() -> None:
    payload = {
        "stt": [{"name": "nova-3", "languages": ["en"]}],
        "tts": [
            {"canonical_name": "aura-2-lyra-fr", "languages": ["fr"], "metadata": {"accent": "Parisian"}},
            {"name": "flux-haley-en", "languages": ["en"]},
            {"name": "not-a-voice"},
        ],
    }
    voices = deepgram_voices_from_models(payload)
    assert [v.id for v in voices] == ["flux-haley-en", "aura-2-lyra-fr"]
    assert voices[1].language == "fr" and "Parisian" in voices[1].description
    assert deepgram_voices_from_models("garbage") == []


# --- providers get the language ---------------------------------------------

def test_elevenlabs_language_code_only_on_v2_5_models() -> None:
    assert accepts_language_code("eleven_flash_v2_5")
    assert accepts_language_code("eleven_turbo_v2_5")
    assert not accepts_language_code("eleven_multilingual_v2")
    assert not accepts_language_code("")


# --- doctor says what resolved ------------------------------------------------

def test_doctor_language_rung_reports_resolved_model_and_spoken_lines() -> None:
    # German: Cartesia's ink-2 covers French now, not German.
    profile = tomllib.loads(wizard.profile_toml(_answers(language="de-DE", stt_provider="cartesia", tts_provider="cartesia")))
    config = SleeptalkConfig.model_validate(profile)
    rung = doctor._language_rung(config)
    joined = "\n".join(rung.lines)
    assert "stt: cartesia ink-whisper" in joined
    assert "resolved from nova-3" in joined  # the fixture's default model belongs to Deepgram
    assert "spoken lines: localized in the profile" in joined


def test_doctor_flags_a_table_the_profile_does_not_carry() -> None:
    config = SleeptalkConfig.model_validate({
        "agent": {"type": "pi"},
        "transport": {"type": "web"},
        "stt": {"provider": "deepgram", "model": "nova-3", "language": "fr-FR"},
        "tts": {"provider": "cartesia", "voice_id": "v", "language": "fr-FR"},
    })
    rung = doctor._language_rung(config)
    assert any("carries no [policy.preamble_pools]" in line for line in rung.lines)


# --- the wizard surface ---------------------------------------------------------

def test_models_setting_sits_with_the_speech_settings() -> None:
    keys = [key for key, _ in wizard.advanced_section_labels()]
    assert keys[:3] == ["language", "models", "speed"]
    # One service for both directions, as on the settings page: no mixing.
    assert "stt" not in keys and "tts_key" not in keys
    assert "models" in dict(wizard.edit_section_labels())


def test_language_notes_say_what_the_language_gets() -> None:
    notes = wizard.language_notes(wizard.WizardAnswers(language="hi-IN", stt_provider="deepgram"))
    assert notes == ["Hindi: acks and tool narration will be spoken in Hindi; instant ack runs blind."]
    notes = wizard.language_notes(wizard.WizardAnswers(language="ur-PK", stt_provider="cartesia", stt_model="ink-2"))
    assert any("Cartesia will use ink-whisper" in note for note in notes)
    assert wizard.language_notes(wizard.WizardAnswers(language="en-US")) == []
    swahili = wizard.language_notes(wizard.WizardAnswers(language="sw-KE"))
    assert swahili and "stay in English" in swahili[0]


def test_language_label_names_common_tags_and_falls_back_to_the_tag() -> None:
    assert wizard.language_label("fr-FR") == "French"
    assert wizard.language_label("fr-CA") == "French"
    assert wizard.language_label("ur-PK") == "Urdu"
    assert wizard.language_label("sw-KE") == "sw-KE"
