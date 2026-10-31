"""One golden default per provider, a few curated picks, and a library to browse."""
from sleeptalk import voice_catalog as catalog
from sleeptalk.wizard import DEFAULT_VOICES


def test_each_provider_has_exactly_one_default_and_a_library():
    for provider, voices in catalog.CURATED.items():
        assert sum(v.default for v in voices) == 1, provider
        assert catalog.library_url(provider).startswith("https://")
        assert len({v.voice_id for v in voices}) == len(voices)


def test_wizard_defaults_are_the_golden_voices():
    assert DEFAULT_VOICES["cartesia"] == catalog.default_voice_id("cartesia") == "db6b0ed5-d5d3-463d-ae85-518a07d3c2b4"
    assert DEFAULT_VOICES["deepgram"] == "flux-hannah-en"
    assert DEFAULT_VOICES["elevenlabs"] == "kPzsL2i3teMYv0FxEYQ6"


def test_payload_is_json_shaped_and_scoped(tmp_path, monkeypatch):
    monkeypatch.setenv('SLEEPTALK_HOME', str(tmp_path))
    payload = catalog.catalog_payload(("deepgram",))
    assert set(payload) == {"deepgram"}
    assert payload["deepgram"]["voices"][0] == {"name": "Hannah", "description": payload["deepgram"]["voices"][0]["description"],
                                               "voice_id": "flux-hannah-en", "default": True, "model": ""}
    local = [v["voice_id"] for v in catalog.catalog_payload(("local",))["local"]["voices"]]
    assert local[:2] == ["af_heart", "af_bella"] and local[-2:] == ["pocket-bria", "pocket-loona"]
    assert catalog.find("cartesia", "47c38ca4-5f35-497b-b1a3-415245fb35e1").name == "Daniel"
    assert catalog.find("cartesia", "nope") is None


def test_voices_and_samples_name_their_model_where_a_provider_reuses_voice_names():
    """Venice's Orpheus "leo" is not its Grok "leo": the settings page shows a
    pick's description and sample only for the model they were made with."""
    import json
    from pathlib import Path

    from sleeptalk import speech_catalog

    for provider in ("openrouter", "venice", "together"):
        models = {o.id for o in speech_catalog.options(provider, speech_catalog.TTS)}
        for voice in catalog.curated(provider):
            assert voice.model in models, (provider, voice.voice_id, voice.model)
    manifest = json.loads((Path(catalog.__file__).parent / "voice_samples" / "manifest.json").read_text())
    for provider in ("openrouter", "venice"):
        for key, item in manifest[provider].items():
            assert item.get("model"), (provider, key)
    assert manifest["venice"]["leo"]["model"] == "tts-xai-v1"


def test_a_local_call_can_switch_between_piper_and_kokoro_voices():
    from sleeptalk.providers import ProviderSpec
    from sleeptalk.speech.local_tts import LocalTTS

    tts = LocalTTS(ProviderSpec(provider="local", model="piper-lessac-medium", voice_id="lessac"))
    tts._tts = object()
    tts.set_voice("bf_emma")
    assert (tts.model.id, tts.speaker, tts.spec.voice_id, tts._tts) == ("kokoro-en", 21, "bf_emma", None)


def test_every_native_voice_has_a_shipped_sample_in_its_language():
    import json
    from pathlib import Path

    from sleeptalk import speech_catalog

    manifest = json.loads((Path(catalog.__file__).parent / "voice_samples" / "manifest.json").read_text())
    for (provider, tag) in catalog._NATIVE:
        for voice in catalog.native(provider, tag):
            sample = manifest[provider][voice.voice_id]
            assert sample["language"] == tag.split("-")[0], (provider, voice.name)
    assert speech_catalog.recommended_voice("elevenlabs", "es-MX") == "JYyJjNPfmNJdaby8LdZs"
    assert speech_catalog.recommended_voice("elevenlabs", "en-US") == "kPzsL2i3teMYv0FxEYQ6"


def test_other_languages_list_native_and_multilingual_voices_only():
    payload = catalog.catalog_payload(("cartesia", "openrouter"), "ja-JP")
    assert [v["name"] for v in payload["cartesia"]["voices"]][:2] == ["Naoki", "Yuzuki"]
    assert not {"Skylar", "Daniel"} & {v["name"] for v in payload["cartesia"]["voices"]}
    assert [v["voice_id"] for v in payload["openrouter"]["voices"]] == ["eve", "ara", "rex", "sal", "leo"]
    assert sum(v["default"] for v in payload["openrouter"]["voices"]) == 1


def test_multilingual_voices_have_a_sample_in_every_language():
    import json
    from pathlib import Path

    manifest = json.loads((Path(catalog.__file__).parent / "voice_samples" / "manifest.json").read_text())
    for provider, voices in catalog.MULTILINGUAL.items():
        for voice in voices:
            for language in ("es", "fr", "de", "it", "pt", "nl", "ja", "ko", "hi", "zh"):
                assert f"{voice}@{language}" in manifest[provider], (provider, voice, language)


def test_local_curated_samples_belong_to_their_selected_model(tmp_path, monkeypatch):
    monkeypatch.setenv('SLEEPTALK_HOME', str(tmp_path))
    from pathlib import Path
    import json
    from sleeptalk.speech.local_models import get_model
    from sleeptalk.settings_page import voice_samples
    from scripts.make_voice_samples import wanted

    root = Path(catalog.__file__).parent / 'voice_samples'
    manifest = json.loads((root / 'manifest.json').read_text())['local']
    for voice in catalog.curated('local'):
        assert voice.voice_id in get_model(voice.model).voices
        entry = manifest[voice.voice_id]
        assert entry['model'] == voice.model
        assert (root / entry['file']).stat().st_size > 1000
    planned = {v['voice_id']: v for v in wanted(('local',))}
    samples = voice_samples()['local']
    for voice in ('pocket-bria', 'pocket-loona'):
        assert samples[voice]['model'] == planned[voice]['model'] == 'pocket-tts-en'
        assert samples[voice]['url'] == f'/samples/local/{voice}.mp3'
