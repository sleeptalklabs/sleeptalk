"""The shared speech-move rules for new profiles and model changes."""
from sleeptalk.speech_choice import SpeechChoice, for_new_profile, with_model


def test_a_new_profile_starts_on_the_languages_recommendation():
    choice = for_new_profile("openrouter", "zh-CN")
    assert (choice.stt_model, choice.tts_model, choice.voice) == (
        "microsoft/mai-transcribe-2", "qwen/qwen-audio-3.0-tts-flash", "")


def test_a_new_profile_keeps_the_models_and_voice_it_was_given():
    choice = for_new_profile("openrouter", "zh-CN", stt_model="qwen/qwen3-asr-flash-2026-02-10",
                             tts_model="x-ai/grok-voice-tts-1.0", voice="ara")
    assert (choice.stt_model, choice.tts_model, choice.voice) == (
        "qwen/qwen3-asr-flash-2026-02-10", "x-ai/grok-voice-tts-1.0", "ara")


def test_a_picked_voice_brings_a_model_that_can_speak_it():
    # zh-CN recommends Qwen, which can't speak Grok's "eve".
    assert for_new_profile("openrouter", "zh-CN", voice="eve").tts_model == "x-ai/grok-voice-tts-1.0"


def test_a_model_without_a_voice_list_does_not_keep_another_models_voice():
    grok = SpeechChoice("openrouter", "nvidia/parakeet-tdt-0.6b-v3", "openrouter", "x-ai/grok-voice-tts-1.0", "eve", "en-US")
    assert with_model(grok, "tts", "hexgrad/kokoro-82m").voice == "af_sky"


def test_a_library_voice_stays_on_a_model_that_takes_library_voices():
    # ElevenLabs lists no voices per model: any library voice is speakable by all of them.
    choice = SpeechChoice("elevenlabs", "scribe_v1", "elevenlabs", "eleven_multilingual_v2", "voice-123", "en-US")
    assert with_model(choice, "tts", "eleven_flash_v2_5").voice == "voice-123"


def test_a_deepgram_voice_family_keeps_a_voice_of_its_own():
    choice = SpeechChoice("deepgram", "nova-3", "deepgram", "", "aura-2-hector-fr", "fr-FR")
    assert with_model(choice, "tts", "aura-2").voice == "aura-2-hector-fr"
    assert with_model(choice, "tts", "flux").voice == "flux-hannah-en"
    flux = SpeechChoice("deepgram", "nova-3", "deepgram", "", "flux-kit-en", "en-US")
    assert with_model(flux, "tts", "flux").voice == "flux-kit-en"
    assert with_model(flux, "tts", "aura-2").voice == "aura-2-thalia-en"
