from __future__ import annotations

import json
import stat
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from sleeptalk import language_support as ls
from sleeptalk import speech_catalog as catalog
from sleeptalk.config import TTSConfig
from sleeptalk.providers import ProviderSpec
from sleeptalk.speech.openai_audio import PRESETS, OpenAIAudioSTT, model_for_voice, sends_language_hint

FIXTURES = Path(__file__).parent / "fixtures" / "speech_catalog"


def _fixture(name: str):
    return json.loads((FIXTURES / name).read_text())


# ---------------------------------------------------------------------------
# One source of truth: every surface's English default is the catalog's.
# ---------------------------------------------------------------------------


def test_every_recommendation_is_a_curated_model():
    for (provider, role), model in catalog._RECOMMENDED.items():
        assert catalog.find(provider, role, model, cached=False) is not None, (provider, role, model)
    for (provider, role, _tag), model in catalog._LANGUAGE_PICKS.items():
        assert catalog.find(provider, role, model, cached=False) is not None, (provider, role, model)


def test_english_defaults_do_not_drift_from_the_catalog():
    from sleeptalk.builder import stt_model_for_provider, tts_model_for_provider
    from sleeptalk.voice_catalog import default_voice_id
    from sleeptalk.wizard import STT_DEFAULT_MODELS

    for name, preset in PRESETS.items():
        assert preset.stt_model == catalog.launch_model(name, catalog.STT)
        assert preset.tts_model == catalog.launch_model(name, catalog.TTS)
        assert preset.tts_voice == catalog.recommended_voice(name) == default_voice_id(name)
    from sleeptalk.config import STTConfig

    for provider, model in ls._STT_LAUNCH_MODELS.items():
        # Deepgram's registration leaves the default to the config (nova-3).
        registered = stt_model_for_provider(provider) or STTConfig().model
        assert catalog.launch_model(provider, catalog.STT) == model == registered
    for provider, model in ls._TTS_LAUNCH_MODELS.items():
        assert catalog.launch_model(provider, catalog.TTS) == model == tts_model_for_provider(provider)
    for provider, model in STT_DEFAULT_MODELS.items():
        assert catalog.launch_model(provider, catalog.STT) == model
    assert catalog.recommended_voice("deepgram") == default_voice_id("deepgram")


def test_model_choices_shown_by_the_wizard_are_in_the_catalog():
    for provider, choices in ls.STT_MODEL_CHOICES.items():
        for model, _note in choices:
            assert catalog.find(provider, catalog.STT, model, cached=False) is not None
    for provider, choices in ls.TTS_MODEL_CHOICES.items():
        for model, _note in choices:
            assert catalog.find(provider, catalog.TTS, model, cached=False) is not None


# ---------------------------------------------------------------------------
# Language-aware picks.
# ---------------------------------------------------------------------------


def test_english_picks_the_launch_default_everywhere():
    for (provider, role), model in catalog._RECOMMENDED.items():
        assert catalog.recommended(provider, role, "en-US").id == model


def test_chinese_picks_the_measured_models():
    hears = catalog.recommended("openrouter", catalog.STT, "zh-CN")
    assert hears.id == "microsoft/mai-transcribe-2" and hears.detects_language
    speaks = catalog.recommended("openrouter", catalog.TTS, "zh-CN")
    assert speaks.id == "qwen/qwen-audio-3.0-tts-flash"
    assert catalog.recommended_voice("openrouter", "zh-CN") == "longanhuan_v3.6"
    # Parakeet has no Chinese; MiniMax can't be driven; neither is ever picked.
    assert catalog.find("openrouter", catalog.STT, "nvidia/parakeet-tdt-0.6b-v3").covers("zh") is False
    assert not catalog.find("openrouter", catalog.TTS, "minimax/speech-2.8-turbo").usable
    assert catalog.recommended("together", catalog.STT, "zh").id == "openai/whisper-large-v3"
    assert catalog.recommended("venice", catalog.STT, "zh").id == "openai/whisper-large-v3"
    assert catalog.recommended("groq", catalog.STT, "zh").id == "whisper-large-v3-turbo"
    # Deepgram's public list streams Chinese on nova-3.
    assert catalog.recommended("deepgram", catalog.STT, "zh-CN").id == "nova-3"
    assert ls.resolve_stt_model("deepgram", "zh-CN") == "nova-3"
    assert catalog.recommended("cartesia", catalog.STT, "zh").id == "ink-whisper"


def test_aggregator_resolution_follows_the_language_but_keeps_a_choice():
    assert ls.resolve_stt_model("openrouter", "en-US", "") == "nvidia/parakeet-tdt-0.6b-v3"
    assert ls.resolve_stt_model("openrouter", "zh-CN", "nvidia/parakeet-tdt-0.6b-v3") == "microsoft/mai-transcribe-2"
    assert ls.resolve_stt_model("openrouter", "zh-CN", "nova-3") == "microsoft/mai-transcribe-2"
    assert ls.resolve_stt_model("openrouter", "zh-CN", "openai/whisper-large-v3-turbo") == "openai/whisper-large-v3-turbo"
    assert ls.resolve_tts_model("openrouter", "en-US") == "x-ai/grok-voice-tts-1.0"
    assert ls.resolve_tts_model("openrouter", "zh-CN", "x-ai/grok-voice-tts-1.0") == "qwen/qwen-audio-3.0-tts-flash"


def test_aggregators_are_judged_instead_of_assumed():
    hears = ls.lookup("zh-CN", "openrouter-stt")
    assert hears.tier == ls.TIER_DEGRADED and "mai-transcribe-2" in hears.note
    assert ls.lookup("en-US", "openrouter-stt").tier == ls.TIER_FULL
    speaks = ls.lookup("zh", "openrouter")
    assert speaks.tier == ls.TIER_DEGRADED and "qwen-audio-3.0-tts-flash" in speaks.note
    assert ls.lookup("en-US", "openrouter").tier == ls.TIER_FULL
    assert ls.lookup("zh", "groq").tier == ls.TIER_UNSUPPORTED  # Orpheus is English only
    assert ls.lookup("zh", "somethingelse").tier == ls.TIER_FULL


# ---------------------------------------------------------------------------
# The call path: explicit models, voices, and the language hint.
# ---------------------------------------------------------------------------


def test_an_explicit_model_wins_when_the_voice_is_its_own():
    preset = PRESETS["openrouter"]
    assert model_for_voice(preset, "leo", "canopylabs/orpheus-3b-0.1-ft") == "canopylabs/orpheus-3b-0.1-ft"
    assert model_for_voice(preset, "English_radiant_girl", "minimax/speech-2.8-turbo") == "minimax/speech-2.8-turbo"
    assert model_for_voice(preset, "loongjohn", "qwen/qwen-audio-3.0-tts-flash") == "qwen/qwen-audio-3.0-tts-flash"
    # A voice picked from another family on the call page still brings its model.
    assert model_for_voice(preset, "eve", "qwen/qwen-audio-3.0-tts-flash") == "x-ai/grok-voice-tts-1.0"
    assert model_for_voice(preset, "flux-hannah-en", "x-ai/grok-voice-tts-1.0") == "deepgram/flux-tts:free"
    assert model_for_voice(PRESETS["venice"], "leo", "tts-orpheus") == "tts-orpheus"


def test_language_detecting_models_get_no_language_hint():
    assert sends_language_hint("openrouter", "nvidia/parakeet-tdt-0.6b-v3")
    assert not sends_language_hint("openrouter", "microsoft/mai-transcribe-2", language="zh-CN")
    assert not sends_language_hint("openrouter", "nvidia/parakeet-tdt-0.6b-v3", {"language_hint": "auto"})
    # Whisper keeps its English hint but is never told "zh".
    assert sends_language_hint("groq", "whisper-large-v3-turbo", language="en-US")
    assert not sends_language_hint("groq", "whisper-large-v3-turbo", language="zh-CN")

    def fields(spec):
        form = OpenAIAudioSTT(spec, api_key="k").transcription_form(b"\0\0" * 480)
        return {field[0]["name"] for field in form._fields}

    zh = ProviderSpec(provider="openrouter", model="microsoft/mai-transcribe-2", language="zh-CN")
    assert "language" not in fields(zh)
    en = ProviderSpec(provider="openrouter", language="en-US")
    assert "language" in fields(en)


def test_a_language_swap_brings_a_voice_of_the_new_model():
    from sleeptalk.builder import tts_spec

    english = tts_spec(TTSConfig(provider="openrouter", voice_id="eve", language="en-US"))
    assert (english.model, english.voice_id) == ("x-ai/grok-voice-tts-1.0", "eve")
    chinese = tts_spec(TTSConfig(provider="openrouter", voice_id="eve", language="zh-CN"))
    assert (chinese.model, chinese.voice_id) == ("qwen/qwen-audio-3.0-tts-flash", "longanhuan_v3.6")
    # The swapped pair survives the voice-family routing in the adapter.
    assert model_for_voice(PRESETS["openrouter"], chinese.voice_id, chinese.model) == chinese.model
    chosen = tts_spec(TTSConfig(provider="openrouter", model="canopylabs/orpheus-3b-0.1-ft", voice_id="tara", language="zh-CN"))
    assert (chosen.model, chosen.voice_id) == ("canopylabs/orpheus-3b-0.1-ft", "tara")


# ---------------------------------------------------------------------------
# Live lists: parsing recorded responses, merging, and the cache.
# ---------------------------------------------------------------------------


def test_openrouter_lists_parse_with_prices_per_hour():
    stt = {o.id: o for o in catalog.parse_openrouter(_fixture("openrouter_stt.json"), catalog.STT)}
    assert stt["nvidia/parakeet-tdt-0.6b-v3"].usd_per_hour == pytest.approx(0.09)
    assert stt["microsoft/mai-transcribe-2"].usd_per_hour == pytest.approx(0.1)  # priced per hour
    assert stt["openai/gpt-4o-mini-transcribe"].usd_per_hour is None  # priced per token
    tts = {o.id: o for o in catalog.parse_openrouter(_fixture("openrouter_tts.json"), catalog.TTS)}
    assert tts["x-ai/grok-voice-tts-1.0"].usd_per_hour == pytest.approx(0.81)
    assert "eve" in tts["x-ai/grok-voice-tts-1.0"].voices
    assert tts["fish-audio/s2-pro"].voices is None  # any voice ID


def test_venice_and_deepgram_lists_parse():
    venice = {o.id: o for o in catalog.parse_venice(_fixture("venice_stt.json"), catalog.STT)}
    assert venice["nvidia/parakeet-tdt-0.6b-v3"].usd_per_hour == pytest.approx(0.36)
    venice_tts = catalog.parse_venice(_fixture("venice_tts.json"), catalog.TTS)
    assert all(o.usd_per_hour is not None for o in venice_tts)
    deepgram = {o.id: o for o in catalog.parse_deepgram(_fixture("deepgram_models.json"), catalog.STT)}
    assert "zh" in deepgram["nova-3"].languages and deepgram["nova-3"].transport == catalog.STREAMING
    assert "nova-3-medical" not in deepgram
    aura = catalog.parse_deepgram(_fixture("deepgram_models.json"), catalog.TTS)[0]
    assert aura.id == "aura-2" and "aura-2-thalia-en" in aura.voices and aura.model_is_voice


def test_live_entries_add_models_but_never_override_curated_facts():
    live = catalog.parse_openrouter(_fixture("openrouter_stt.json"), catalog.STT)
    merged = {o.id: o for o in catalog._merge(catalog.curated("openrouter", catalog.STT), live)}
    assert "assemblyai/universal-3-5-pro" in merged
    parakeet = merged["nvidia/parakeet-tdt-0.6b-v3"]
    assert parakeet.source == "curated" and parakeet.languages == catalog._PARAKEET_V3


class _Response:
    def __init__(self, payload, status=200):
        self.status, self._payload = status, payload

    async def json(self, content_type=None):
        return self._payload


class _Session:
    def __init__(self, payload=None, fail=False):
        self.payload, self.fail, self.calls = payload, fail, 0

    @asynccontextmanager
    async def _get(self):
        self.calls += 1
        if self.fail:
            raise OSError("offline")
        yield _Response(self.payload)

    def get(self, *args, **kwargs):
        return self._get()


async def test_refresh_caches_privately_and_reuses_the_cache_within_a_day(tmp_path):
    session = _Session(_fixture("openrouter_stt.json"))
    first = await catalog.refresh("openrouter", catalog.STT, cache_dir=tmp_path, session=session, now=1000.0)
    assert "assemblyai/universal-3-5-pro" in {o.id for o in first}
    cache = tmp_path / "openrouter-stt.json"
    assert stat.S_IMODE(cache.stat().st_mode) == 0o600
    again = await catalog.refresh("openrouter", catalog.STT, cache_dir=tmp_path, session=session, now=2000.0)
    assert session.calls == 1 and {o.id for o in again} == {o.id for o in first}
    # Offline cache reads for pages, with no network at all.
    assert catalog.find("openrouter", catalog.STT, "assemblyai/universal-3-5-pro", cache_dir=tmp_path) is not None


async def test_refresh_falls_back_to_cache_then_curated(tmp_path):
    session = _Session(_fixture("openrouter_stt.json"))
    await catalog.refresh("openrouter", catalog.STT, cache_dir=tmp_path, session=session, now=0.0)
    stale = catalog.CACHE_TTL_SECONDS + 1
    offline = _Session(fail=True)
    from_cache = await catalog.refresh("openrouter", catalog.STT, cache_dir=tmp_path, session=offline, now=stale)
    assert offline.calls == 1 and "assemblyai/universal-3-5-pro" in {o.id for o in from_cache}
    nothing = await catalog.refresh("openrouter", catalog.STT, cache_dir=tmp_path / "empty", session=offline, now=stale)
    assert {o.id for o in nothing} == {o.id for o in catalog.curated("openrouter", catalog.STT)}


async def test_key_only_lists_are_not_fetched_without_a_key(tmp_path):
    session = _Session({"data": []})
    result = await catalog.refresh("elevenlabs", catalog.TTS, cache_dir=tmp_path, session=session)
    assert session.calls == 0 and result == catalog.curated("elevenlabs", catalog.TTS)
    assert catalog.live_source("groq", catalog.STT) == ""


def test_page_payload_marks_the_recommended_model(tmp_path):
    data = catalog.payload("openrouter", catalog.STT, "zh-CN", cache_dir=tmp_path)
    assert data["recommended"] == "microsoft/mai-transcribe-2"
    marked = [m for m in data["models"] if m["recommended"]]
    assert len(marked) == 1 and marked[0]["covers_language"] is True


def test_local_picker_sizes_follow_the_download_catalog_and_cache(monkeypatch, tmp_path):
    from sleeptalk.speech import local_models as lm

    monkeypatch.setenv("SLEEPTALK_MODELS_DIR", str(tmp_path))
    kokoro = lm.get_model("kokoro-en")
    ready = lm.model_path(kokoro)
    ready.mkdir()
    (ready / lm.READY_MARKER).write_text(kokoro.sha256)
    tts = {m["id"]: m for m in catalog.payload("local", catalog.TTS)["models"]}
    assert tts["kokoro-en"]["download"] == {"bytes": 350_000_000, "approximate": True, "ready": True}
    assert tts["piper-lessac-medium"]["download"] == {"bytes": 21_000_000, "approximate": True, "ready": False}
    stt = {m["id"]: m for m in catalog.payload("local", catalog.STT)["models"]}
    streaming = lm.get_model("moonshine-small-streaming-en")
    assert stt[streaming.id]["download"] == {
        "bytes": sum(f[2] for f in streaming.files), "approximate": False, "ready": False,
    }
    assert all("download" not in m for m in catalog.payload("openrouter", catalog.TTS)["models"])


def test_every_venice_model_carries_its_privacy_label_to_the_page():
    """Venice's point is its Private / Anonymized split: every model it lists
    says which it is, and the settings page receives that label."""
    for role, count in ((catalog.STT, 5), (catalog.TTS, 11)):
        models = catalog.curated("venice", role)
        assert len(models) == count
        assert {m.privacy for m in models} == {"private", "anonymized"}
    assert catalog.find("venice", catalog.STT, "nvidia/parakeet-tdt-0.6b-v3").privacy == "private"
    assert catalog.find("venice", catalog.TTS, "tts-xai-v1").privacy == "anonymized"
    page = catalog.payload("venice", catalog.TTS, cache_dir=Path("/nonexistent"))
    assert page["measured_on"] == "2026-10-04"
    kokoro = next(m for m in page["models"] if m["id"] == "tts-kokoro")
    assert kokoro["privacy"] == "private" and kokoro["latency_ms"]


def test_venice_live_list_reads_privacy_and_a_format_the_call_path_plays():
    payload = {"data": [
        {"id": "tts-new", "model_spec": {"name": "New", "privacy": "anonymized", "supported_formats": ["mp3", "wav", "pcm"]}},
        {"id": "tts-opus", "model_spec": {"name": "Opus only", "privacy": "private", "supported_formats": ["opus"]}},
    ]}
    found = {o.id: o for o in catalog.parse_venice(payload, catalog.TTS)}
    assert found["tts-new"].privacy == "anonymized" and found["tts-new"].audio_format == "wav"
    assert found["tts-opus"].privacy == "private" and not found["tts-opus"].usable


def test_venice_models_are_asked_for_a_format_they_accept():
    """Venice refuses "pcm" for Orpheus, Gradium, Inworld and Gemini (WAV
    only or WAV first) and for the MP3-only ones; the profile's own choice wins."""
    from sleeptalk.speech.openai_audio import OpenAIAudioTTS

    def fmt(model, voice, **options):
        return OpenAIAudioTTS(ProviderSpec(provider="venice", model=model, voice_id=voice, options=options),
                              api_key="k").output_format

    assert fmt("tts-xai-v1", "eve") == "pcm"
    assert fmt("tts-kokoro", "af_sky") == "pcm"
    assert fmt("tts-orpheus", "tara") == "wav"
    assert fmt("tts-gradium-v1", "Emma") == "wav"
    assert fmt("tts-elevenlabs-turbo-v2-5", "Rachel") == "mp3"
    assert fmt("tts-minimax-speech-02-hd", "CalmWoman") == "flac"
    assert fmt("tts-orpheus", "tara", output_format="mp3") == "mp3"


def test_venice_wizper_gets_the_wav_upload_it_accepts():
    def upload(model):
        form = OpenAIAudioSTT(ProviderSpec(provider="venice", model=model), api_key="k").transcription_form(b"\1\0" * 4800)
        field = next(f for f in form._fields if f[0]["name"] == "file")
        return field[0]["filename"], field[1]["Content-Type"]

    assert upload("fal-ai/wizper") == ("utterance.wav", "audio/x-wav")
    assert upload("nvidia/parakeet-tdt-0.6b-v3")[0] == "utterance.flac"


def test_every_live_cartesia_model_is_offered():
    """Sonic 3.6 (and its snapshot), Sonic Preview, Sonic 2, and Sonic Turbo
    all spoke through our adapter on 2026-10-04; the sunset Sonic 1 models
    (sonic, sonic-english, sonic-multilingual) answer 400 and stay out."""
    tts = {m.id for m in catalog.curated("cartesia", catalog.TTS)}
    assert tts == {"sonic-3", "sonic-3.6", "sonic-3.6-2026-08-27", "sonic-preview", "sonic-2", "sonic-turbo"}
    stt = {m.id: m for m in catalog.curated("cartesia", catalog.STT)}
    assert set(stt) == {"ink-2", "ink-preview", "ink-whisper"}
    assert stt["ink-2"].covers("ja-JP") and not stt["ink-2"].covers("de-DE")
    assert catalog.recommended("cartesia", catalog.TTS).id == "sonic-3.6"
    assert catalog.recommended("cartesia", catalog.STT, "es-MX").id == "ink-2"
    assert catalog.recommended("cartesia", catalog.STT, "de-DE").id == "ink-whisper"


def test_local_speech_defaults_to_kokoro_and_keeps_piper():
    from sleeptalk.builder import tts_spec
    from sleeptalk.config import TTSConfig
    from sleeptalk.speech import local_models

    assert catalog.launch_model("local", catalog.TTS) == "kokoro-en"
    assert catalog.recommended_voice("local") == "af_heart"
    assert tts_spec(TTSConfig(provider="local")).model == "kokoro-en"
    # An older profile on Piper's voice keeps speaking through Piper.
    assert local_models.resolve_tts_model(tts_spec(TTSConfig(provider="local", voice_id="lessac")).model, "lessac").id \
        == "piper-lessac-medium"
    assert catalog.find("local", catalog.TTS, "piper-lessac-medium").usable


def test_sonic_36_is_cartesias_default_and_sonic_2_and_turbo_say_they_retire():
    """Cartesia retires Sonic 2 and Sonic Turbo on 2026-10-20 and recommends 3.6."""
    from sleeptalk import language_support as ls
    from sleeptalk.builder import tts_spec
    from sleeptalk.config import TTSConfig

    assert catalog.launch_model("cartesia", catalog.TTS) == "sonic-3.6"
    assert tts_spec(TTSConfig(provider="cartesia")).model == "sonic-3.6"
    # A profile that chose Sonic 3 keeps it; Lithuanian, which only Sonic 3 lists, gets it.
    assert tts_spec(TTSConfig(provider="cartesia", model="sonic-3")).model == "sonic-3"
    assert tts_spec(TTSConfig(provider="cartesia", language="lt-LT")).model == "sonic-3"
    assert tts_spec(TTSConfig(provider="cartesia", language="ja-JP")).model == "sonic-3.6"
    assert catalog.recommended("cartesia", catalog.TTS, "ta-IN").id == "sonic-3.6"
    assert ls.lookup("ta-IN", "cartesia").tier == ls.TIER_FULL
    for model in ("sonic-2", "sonic-turbo"):
        assert "retires" in catalog.find("cartesia", catalog.TTS, model).notes
    # Moving a Cartesia profile to an aggregator drops its Cartesia default.
    assert ls.resolve_tts_model("openrouter", "en-US", "sonic-3.6") == "x-ai/grok-voice-tts-1.0"
    assert ls.resolve_tts_model("openrouter", "en-US", "sonic-3") == "x-ai/grok-voice-tts-1.0"


def test_together_moves_off_sonic_2():
    """Together lists Sonic 3 but not 3.6; profiles saved with its old Sonic 2
    default follow the new one instead of breaking when Sonic 2 retires."""
    from sleeptalk import language_support as ls

    assert catalog.launch_model("together", catalog.TTS) == "cartesia/sonic-3"
    assert "cartesia/sonic-2" not in {m.id for m in catalog.curated("together", catalog.TTS)}
    assert ls.resolve_tts_model("together", "en-US", "cartesia/sonic-2") == "cartesia/sonic-3"
    assert ls.resolve_tts_model("together", "en-US", "hexgrad/Kokoro-82M") == "hexgrad/Kokoro-82M"


def test_elevenlabs_on_openrouter():
    """Nine ElevenLabs speaking models and Scribe v2, live on OpenRouter 2026-10-07."""
    from sleeptalk.speech.openai_audio import PRESETS, model_for_voice, sends_language_hint

    tts = {m.id: m for m in catalog.curated("openrouter", catalog.TTS) if m.id.startswith("elevenlabs/")}
    assert len(tts) == 9
    for option in tts.values():
        assert option.usable and option.default_voice == "jessica" and "george" in option.voices
    assert tts["elevenlabs/eleven-flash-v2"].covers("fr-FR") is False
    assert tts["elevenlabs/eleven-flash-v2.5"].covers("vi-VN")
    assert tts["elevenlabs/eleven-v4-turbo"].usd_per_hour == 1.08
    assert catalog.latency_ms("openrouter", catalog.TTS, "elevenlabs/eleven-turbo-v2.5") == 350
    # Grok stays the default; an ElevenLabs voice alone picks Turbo v2.5 (measured quickest),
    # and a chosen model keeps it.
    assert catalog.launch_model("openrouter", catalog.TTS) == "x-ai/grok-voice-tts-1.0"
    preset = PRESETS["openrouter"]
    assert model_for_voice(preset, "george", "") == "elevenlabs/eleven-turbo-v2.5"
    assert model_for_voice(preset, "george", "elevenlabs/eleven-flash-v2.5") == "elevenlabs/eleven-flash-v2.5"
    assert model_for_voice(preset, "jess", "") == "canopylabs/orpheus-3b-0.1-ft"
    # A Grok voice left on an ElevenLabs model moves to Grok, not to a voice ElevenLabs lacks.
    assert model_for_voice(preset, "eve", "elevenlabs/eleven-v4-turbo") == "x-ai/grok-voice-tts-1.0"
    scribe = catalog.find("openrouter", catalog.STT, "elevenlabs/scribe-v2")
    assert scribe.detects_language and scribe.covers("ur-PK")
    assert not sends_language_hint("openrouter", "elevenlabs/scribe-v2", {}, "de-DE")
