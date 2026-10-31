from __future__ import annotations

import pytest

from sleeptalk.builder import stt_spec, tts_spec
from sleeptalk.config import STTConfig, TTSSpecConfig
from sleeptalk.defaults import register_defaults
from sleeptalk.registries import TTS_PROVIDERS


@pytest.mark.parametrize(
    ("provider", "model", "expected_model", "expected_format"),
    [
        ("cartesia", "", "sonic-3.6", "wav"),
        ("elevenlabs", "foreign-model", "eleven_multilingual_v2", "wav_24000"),
        ("playai", "PlayDialog", "PlayDialog", "wav"),
        ("rime", "arcana-v2", "arcana-v2", "wav"),
        ("hume", "octave", "octave", "wav"),
    ],
)
def test_tts_specs_use_registration_owned_defaults(
    provider: str,
    model: str,
    expected_model: str,
    expected_format: str,
) -> None:
    register_defaults()
    registration = TTS_PROVIDERS.get(provider)
    spec = tts_spec(TTSSpecConfig(provider=provider, model=model))
    assert spec.model == expected_model
    assert spec.options["output_format"] == expected_format
    assert registration.provider_defaults["output_format"] == expected_format


def test_provider_specs_preserve_private_options() -> None:
    stt = stt_spec(STTConfig(options={"region": "eu"}))
    tts = tts_spec(TTSSpecConfig(provider="cartesia", options={"quality": "fast"}))
    assert stt.options["region"] == "eu"
    assert tts.options["quality"] == "fast"


def test_provider_specs_reject_common_fields_inside_options() -> None:
    with pytest.raises(ValueError, match="duplicate common fields.*model"):
        stt_spec(STTConfig(options={"model": "shadow-model"}))
