"""Local speech: pinned models, a checksummed download, and Moonshine + Piper/Kokoro through the registries."""

from __future__ import annotations

import dataclasses
import hashlib
import io
import os
import tarfile
import tomllib
import wave
from pathlib import Path

import pytest

from sleeptalk import wizard
from sleeptalk.config import STTConfig, SleeptalkConfig, TTSConfig, _validate_component_choices
from sleeptalk.defaults import register_defaults
from sleeptalk.speech import local_models as lm
from sleeptalk.speech.common import SpeechError
from sleeptalk.wizard import WizardAnswers, answers_from_mapping, env_entries, profile_toml, required_answer_fields

register_defaults()

#: Real inference runs only when the models are already on disk (set by the spike or a real setup).
MODELS_ROOT = Path(os.environ["SLEEPTALK_TEST_MODELS_DIR"]) if os.environ.get("SLEEPTALK_TEST_MODELS_DIR") else None


# ---------------------------------------------------------------------------
# Catalog and download
# ---------------------------------------------------------------------------


def test_catalog_is_pinned_and_permissive():
    assert set(lm.MODELS) == {"moonshine-tiny-en", "moonshine-small-streaming-en", "piper-lessac-medium", "kokoro-en", "pocket-tts-en"}
    for model in lm.MODELS.values():
        assert len(model.sha256) == 64
        assert model.url.startswith(("https://github.com/k2-fsa/sherpa-onnx/releases/", "https://download.moonshine.ai/model/"))
        for name, digest, size in model.files:
            assert Path(name).name == name and len(digest) == 64 and size > 0
        assert model.license in {"MIT", "Apache-2.0", "CC-BY-4.0"}
        assert model.kind in {"stt", "tts"}
    assert lm.get_model("moonshine-tiny-en").kind == "stt"
    with pytest.raises(lm.LocalModelError, match="choose one of"):
        lm.get_model("moonshine-tiny-en", "tts")
    with pytest.raises(lm.LocalModelError):
        lm.get_model("whisper-large")


def test_models_dir_honours_overrides(tmp_path):
    assert lm.models_dir({"SLEEPTALK_MODELS_DIR": str(tmp_path / "m")}) == tmp_path / "m"
    assert lm.models_dir({"XDG_CACHE_HOME": str(tmp_path)}) == tmp_path / "sleeptalk" / "models"
    assert lm.models_dir({}).parts[-2:] == ("sleeptalk", "models")


class _Archive:
    """A fake urlopen response over an in-memory tar.bz2."""

    def __init__(self, data: bytes):
        self._buf = io.BytesIO(data)
        self.headers = {"Content-Length": str(len(data))}

    def read(self, n: int) -> bytes:
        return self._buf.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _tarball(entries: dict[str, bytes]) -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w:bz2") as archive:
        for name, payload in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    return raw.getvalue()


def _fake_model(data: bytes, **overrides) -> lm.LocalModel:
    base = lm.MODELS["piper-lessac-medium"]
    sha256 = overrides.pop("sha256", hashlib.sha256(data).hexdigest())
    return dataclasses.replace(base, sha256=sha256, url="https://example.test/piper.tar.bz2", **overrides)


def test_ensure_model_downloads_verifies_unpacks_and_is_idempotent(tmp_path):
    data = _tarball({"vits-piper-en_US-lessac-medium-int8/tokens.txt": b"a\n", "vits-piper-en_US-lessac-medium-int8/x.onnx": b"\x00" * 3000})
    model = _fake_model(data)
    ticks: list[tuple[int, int]] = []
    opened: list[str] = []

    def opener(url):
        opened.append(url)
        return _Archive(data)

    path = lm.ensure_model(model, root=tmp_path, progress=lambda d, t: ticks.append((d, t)), opener=opener)
    assert path == tmp_path / "piper-lessac-medium"
    assert (path / "tokens.txt").read_bytes() == b"a\n" and (path / "x.onnx").stat().st_size == 3000
    assert lm.is_ready(model, tmp_path) and (path / lm.READY_MARKER).read_text().strip() == model.sha256
    assert ticks and ticks[-1] == (len(data), len(data))
    assert not [p for p in tmp_path.iterdir() if p.name.startswith("piper-lessac-medium-")]  # scratch cleaned
    assert lm.ensure_model(model, root=tmp_path, opener=opener) == path
    assert opened == [model.url]  # the second call never fetched


def test_ensure_model_rejects_a_bad_checksum_and_traversal(tmp_path):
    data = _tarball({"vits-piper-en_US-lessac-medium-int8/tokens.txt": b"a\n"})
    wrong = _fake_model(data, sha256="0" * 64)
    with pytest.raises(lm.LocalModelError, match="checksum"):
        lm.ensure_model(wrong, root=tmp_path, opener=lambda url: _Archive(data))
    assert not (tmp_path / "piper-lessac-medium").exists()
    evil = _tarball({"vits-piper-en_US-lessac-medium-int8/../escape.txt": b"no"})
    with pytest.raises(lm.LocalModelError):
        lm.ensure_model(_fake_model(evil), root=tmp_path, opener=lambda url: _Archive(evil))
    outside = _tarball({"other-dir/tokens.txt": b"no"})
    with pytest.raises(lm.LocalModelError, match="outside"):
        lm.ensure_model(_fake_model(outside), root=tmp_path, opener=lambda url: _Archive(outside))


def test_download_failure_is_named_without_partial_install(tmp_path):
    class Broken:
        headers = {}

        def read(self, n):
            raise OSError("boom")

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    with pytest.raises(lm.LocalModelError, match="could not download"):
        lm.ensure_model(_fake_model(b"x"), root=tmp_path, opener=lambda url: Broken())
    assert list(tmp_path.iterdir()) == []


def test_resample_and_voice_resolution():
    pcm = b"\x00\x10" * 4800  # 100 ms at 48 kHz
    assert len(lm.resample_int16(pcm, 48_000, 16_000)) == 3200
    assert lm.resample_int16(pcm, 48_000, 48_000) is pcm
    assert lm.resolve_tts_model("", "").id == "kokoro-en"
    assert lm.resolve_tts_model("", "lessac").id == "piper-lessac-medium"  # an older profile's Piper voice
    assert lm.resolve_tts_model("piper-lessac-medium", "af_sarah").id == "kokoro-en"
    assert lm.resolve_tts_model("", "bm_george").id == "kokoro-en"
    assert lm.resolve_tts_model("kokoro-en", "").id == "kokoro-en"
    kokoro = lm.get_model("kokoro-en")
    assert lm.voice_index(kokoro, "af_sarah") == 9 and lm.voice_index(kokoro, "") == 3
    assert lm.voice_index(kokoro, "af_heart") == 3
    # The v0.19 blend "af" speaks as Bella rather than failing a call.
    assert lm.voice_index(kokoro, "af") == lm.voice_index(kokoro, "af_bella") == 2
    assert lm.resolve_tts_model("", "af").id == "kokoro-en"
    with pytest.raises(lm.LocalModelError, match="no voice"):
        lm.voice_index(lm.get_model("piper-lessac-medium"), "af_sarah")
    assert [m.id for m in lm.required_models("", "", "af_sky")] == ["moonshine-small-streaming-en", "kokoro-en"]


def test_kokoro_v1_uses_new_storage_and_verifies_the_ready_marker(tmp_path):
    model = lm.get_model("kokoro-en")
    old = tmp_path / "kokoro-en"
    old.mkdir()
    (old / lm.READY_MARKER).write_text("v0.19 checksum\n")
    (old / "model.onnx").write_bytes(b"old model")
    assert not lm.is_ready(model, tmp_path)
    assert lm.model_path(model, tmp_path) == tmp_path / "kokoro-en-v1_0"

    data = _tarball({model.archive_dir + "/model.onnx": b"v1 model"})
    fixture_model = dataclasses.replace(model, sha256=hashlib.sha256(data).hexdigest())
    new = lm.model_path(model, tmp_path)
    new.mkdir()
    (new / lm.READY_MARKER).write_text("wrong version\n")
    assert not lm.is_ready(fixture_model, tmp_path)
    installed = lm.ensure_model(fixture_model, root=tmp_path, opener=lambda _: _Archive(data))
    assert installed == new and lm.is_ready(fixture_model, tmp_path)
    assert (new / "model.onnx").read_bytes() == b"v1 model"
    assert (old / "model.onnx").read_bytes() == b"old model"
    assert not lm.is_ready(model, tmp_path)  # a different digest is not ready


@pytest.mark.parametrize("voice", ["0", "3", "27"])
def test_kokoro_old_numeric_ids_require_a_name(voice):
    from sleeptalk.providers import ProviderSpec
    from sleeptalk.speech.local_tts import LocalTTS

    with pytest.raises(SpeechError, match="use a voice name"):
        LocalTTS(ProviderSpec(provider="local", model="kokoro-en", voice_id=voice))


def test_kokoro_catalog_matches_native_english_speaker_ids():
    from sleeptalk.speech_catalog import TTS, curated

    model = lm.get_model("kokoro-en")
    assert len(model.voices) == 28
    assert {name: lm.voice_index(model, name) for name in (
        "af_bella", "af_heart", "af_sarah", "am_michael", "bf_emma", "bm_lewis"
    )} == {"af_bella": 2, "af_heart": 3, "af_sarah": 9, "am_michael": 16, "bf_emma": 21, "bm_lewis": 27}
    option = next(item for item in curated("local", TTS) if item.id == "kokoro-en")
    assert option.voices == model.voices


def test_british_kokoro_voices_read_english_the_british_way():
    from sleeptalk.providers import ProviderSpec
    from sleeptalk.speech.local_tts import LocalTTS

    assert lm.kokoro_accent("bf_emma") == lm.kokoro_accent("bm_george") == "en"
    assert lm.kokoro_accent("af_heart") == lm.kokoro_accent("am_michael") == lm.kokoro_accent("") == "en-us"
    tts = LocalTTS(ProviderSpec(provider="local", model="kokoro-en", voice_id="af_heart"))
    engine = tts._tts = object()
    tts.set_voice("af_bella")  # same model and accent: the loaded engine stays
    assert tts._tts is engine
    tts.set_voice("bf_emma")  # British: Kokoro loads again with British English
    assert (tts._tts, tts.accent) == (None, "en")


def test_kokoro_loader_uses_v1_lexicons(monkeypatch, tmp_path):
    import types
    import sys
    from sleeptalk.speech.local_tts import load_synthesizer

    model = lm.get_model("kokoro-en")
    directory = lm.model_path(model, tmp_path)
    directory.mkdir()
    (directory / lm.READY_MARKER).write_text(model.sha256)
    config_type = lambda **kwargs: types.SimpleNamespace(**kwargs)
    monkeypatch.setitem(sys.modules, "sherpa_onnx", types.SimpleNamespace(
        OfflineTtsKokoroModelConfig=config_type, OfflineTtsModelConfig=config_type,
        OfflineTtsConfig=config_type, OfflineTts=lambda config: config,
    ))
    config = load_synthesizer(model, tmp_path)
    assert config.model.kokoro.lang == "en-us"
    assert load_synthesizer(model, tmp_path, accent="en").model.kokoro.lang == "en"
    assert config.model.kokoro.lexicon.split(",") == [
        str(directory / "lexicon-us-en.txt"), str(directory / "lexicon-zh.txt"),
    ]
    assert config.model.kokoro.model == str(directory / "model.onnx")


# ---------------------------------------------------------------------------
# Registrations and config
# ---------------------------------------------------------------------------


def test_switching_a_cloud_profile_to_local_keeps_its_common_keys():
    # Settings preserves [stt]/[tts] keys across a provider switch; Local must accept them.
    document = {
        "stt": {"provider": "local", "keyterms": "Sleeptalk, Alice", "base_url": "", "endpointing_ms": 800},
        "tts": {"provider": "local", "voice_id": "lessac", "output_format": "wav_24000", "speed": 1.25},
    }
    assert _validate_component_choices(SleeptalkConfig.model_validate(document), env={}) == []


def test_local_profile_defaults_to_streaming_and_keeps_batch_available(tmp_path):
    from sleeptalk.builder import stt_spec, tts_spec
    from sleeptalk.provider_factory import build_stt_components
    from sleeptalk.registries import TTS_PROVIDERS
    from sleeptalk.speech.local_stt import LocalSTT
    from sleeptalk.speech.local_tts import LocalTTS

    config = SleeptalkConfig.model_validate({"stt": {"provider": "local"}, "tts": {"provider": "local", "voice_id": "af_sarah"}})
    assert _validate_component_choices(config, env={}) == []
    components = build_stt_components(stt_spec(STTConfig(provider="local")), {})
    from sleeptalk.speech.local_streaming_stt import LocalStreamingSTT
    assert isinstance(components.rest, LocalStreamingSTT) and components.streaming is components.rest
    assert components.rest.model.id == "moonshine-small-streaming-en"
    components.rest._executor.shutdown()
    assert stt_spec(STTConfig(provider="local", model="nova-3")).model == "moonshine-small-streaming-en"
    batch = build_stt_components(stt_spec(STTConfig(provider="local", model="moonshine-tiny-en")), {})
    assert isinstance(batch.rest, LocalSTT) and batch.streaming is None
    tts = TTS_PROVIDERS.build("local", tts_spec(TTSConfig(provider="local", voice_id="af_sarah")), {})
    assert isinstance(tts, LocalTTS) and tts.model.id == "kokoro-en" and tts.speaker == 9
    with pytest.raises(SpeechError, match="no voice"):
        TTS_PROVIDERS.build("local", tts_spec(TTSConfig(provider="local", model="piper-lessac-medium", voice_id="nobody")), {})


async def test_missing_models_are_reported_not_fetched(tmp_path):
    from sleeptalk.builder import stt_spec, tts_spec
    from sleeptalk.speech.local_stt import LocalSTT
    from sleeptalk.speech.local_tts import LocalTTS

    stt = LocalSTT(stt_spec(STTConfig(provider="local", model="moonshine-tiny-en")), models_root=tmp_path)
    with pytest.raises(SpeechError, match="not downloaded"):
        await stt.transcribe_pcm(b"\x00\x01" * 100)
    tts = LocalTTS(tts_spec(TTSConfig(provider="local")), models_root=tmp_path)
    with pytest.raises(SpeechError, match="not downloaded"):
        await tts.synthesize_pcm("Hello there.")
    assert list(tmp_path.iterdir()) == []


async def test_connectivity_checks_name_the_missing_piece(monkeypatch, tmp_path):
    from sleeptalk.registries import STT_PROVIDERS, TTS_PROVIDERS

    monkeypatch.setenv("SLEEPTALK_MODELS_DIR", str(tmp_path))
    monkeypatch.setattr("sleeptalk.sysdeps.missing_local_extra", lambda: None)
    monkeypatch.setattr("sleeptalk.sysdeps.missing_streaming_extra", lambda: None)
    with pytest.raises(RuntimeError, match="not downloaded"):
        await STT_PROVIDERS.get("local").check(STTConfig(provider="local"), {})
    with pytest.raises(RuntimeError, match="Kokoro"):
        await TTS_PROVIDERS.get("local").check(TTSConfig(provider="local", voice_id="af_sky"), {})
    monkeypatch.setattr("sleeptalk.sysdeps.missing_local_extra", lambda: "needs the extra")
    with pytest.raises(RuntimeError, match="needs the extra"):
        await STT_PROVIDERS.get("local").check(STTConfig(provider="local"), {})


# ---------------------------------------------------------------------------
# Wizard and answers wiring
# ---------------------------------------------------------------------------


def test_one_key_wiring_needs_no_key_at_all():
    answers = WizardAnswers()
    wizard.apply_speech_provider(answers, "local")
    assert (answers.stt_provider, answers.stt_model, answers.tts_provider, answers.tts_voice_id) == (
        "local", "moonshine-small-streaming-en", "local", "af_heart")
    assert "stt_key" not in required_answer_fields(answers) and "tts_key" not in required_answer_fields(answers)
    assert env_entries(answers) == {}
    document = tomllib.loads(profile_toml(answers))
    assert (document["stt"]["provider"], document["stt"]["model"]) == ("local", "moonshine-small-streaming-en")
    assert (document["tts"]["provider"], document["tts"]["voice_id"]) == ("local", "af_heart")
    assert _validate_component_choices(SleeptalkConfig.model_validate(document), env={}) == []
    assert wizard.capability_providers(answers) == ["local-stt", "local"]
    mixed = WizardAnswers(stt_provider="local", tts_provider="deepgram")
    assert required_answer_fields(mixed) == ("name", "tts_key")


def test_answers_file_local_takes_no_key():
    answers = answers_from_mapping({"name": "bot", "speech_provider": "local"})
    assert answers.stt_provider == "local" and answers.tts_provider == "local"
    with pytest.raises(ValueError, match="no speech_key"):
        answers_from_mapping({"name": "bot", "speech_provider": "local", "speech_key": "x"})


def test_local_voices_languages_and_speed(tmp_path, monkeypatch):
    monkeypatch.setenv('SLEEPTALK_HOME', str(tmp_path))
    from sleeptalk.language_support import TIER_FULL, TIER_UNSUPPORTED, lookup
    from sleeptalk.speech_speed import wire_speed
    from sleeptalk.voice_list import list_local_voices

    voices = list_local_voices()
    assert [v.id for v in voices][:2] == ["af_alloy", "af_aoede"] and voices[-1].id == "pocket-loona" and len(voices) == 31
    assert "af_heart" in {v.id for v in voices}
    assert all("MB" in v.description for v in voices)
    assert lookup("en-US", "local-stt").tier == TIER_FULL and lookup("en-GB", "local").tier == TIER_FULL
    assert lookup("de-DE", "local").tier == TIER_UNSUPPORTED
    assert 0.5 <= wire_speed("local", 1.5) <= 1.5


def test_local_extra_is_declared_and_installable():
    from sleeptalk import sysdeps

    command = sysdeps.local_extra_install_command()
    assert any(part.startswith("sherpa-onnx") for part in command)
    assert sysdeps.missing_local_extra() is None or "sleeptalk[local]" in sysdeps.missing_local_extra()


# ---------------------------------------------------------------------------
# Real inference, when the models are on disk
# ---------------------------------------------------------------------------


def _need_models(*ids: str) -> Path:
    pytest.importorskip("sherpa_onnx")
    if MODELS_ROOT is None:
        pytest.skip("SLEEPTALK_TEST_MODELS_DIR not set")
    for model_id in ids:
        if not lm.is_ready(lm.get_model(model_id), MODELS_ROOT):
            pytest.skip(f"{model_id} not downloaded")
    return MODELS_ROOT


async def test_moonshine_transcribes_the_bundled_clip():
    from sleeptalk.builder import stt_spec
    from sleeptalk.speech.local_stt import LocalSTT
    from sleeptalk.stt_roundtrip import bundled_clip_bytes

    root = _need_models("moonshine-tiny-en")
    with wave.open(io.BytesIO(bundled_clip_bytes())) as clip:
        pcm = lm.resample_int16(clip.readframes(clip.getnframes()), clip.getframerate(), 48_000)
    stt = LocalSTT(stt_spec(STTConfig(provider="local", model="moonshine-tiny-en")), models_root=root)
    text = await stt.transcribe_pcm(pcm)
    assert "sleep" in text.lower() and "check" in text.lower()
    assert stt.last_decode_ms < 500
    assert await stt.transcribe_pcm(b"") == ""
    await stt.close()


@pytest.mark.parametrize("voice", ["lessac", "af_sarah", "af_heart", "af_bella", "bf_emma"])
async def test_local_tts_yields_48k_pcm(voice):
    from sleeptalk.builder import tts_spec
    from sleeptalk.speech.local_tts import LocalTTS

    model = lm.resolve_tts_model("", voice)
    root = _need_models(model.id)
    tts = LocalTTS(tts_spec(TTSConfig(provider="local", voice_id=voice)), models_root=root)
    chunks = [chunk async for chunk in tts.synthesize("Hey, what's up!")]
    assert all(0 < len(chunk) <= 1920 and len(chunk) % 2 == 0 for chunk in chunks)
    assert 0.5 < sum(map(len, chunks)) / 96_000 < 3.0  # seconds at 48 kHz int16 mono
    path = await tts.synthesize_wav("Sample.")
    with wave.open(str(path)) as sample:
        assert (sample.getframerate(), sample.getnchannels(), sample.getsampwidth()) == (48_000, 1, 2)
    path.unlink()
    with pytest.raises(SpeechError, match="empty"):
        await tts.synthesize_pcm("...")
    await tts.close()


async def test_local_roundtrip_reports_decode_time():
    from sleeptalk.builder import stt_spec
    from sleeptalk.stt_roundtrip import stt_roundtrip

    root = _need_models("moonshine-tiny-en")
    os.environ["SLEEPTALK_MODELS_DIR"] = str(root)
    try:
        transcript, latency_ms = await stt_roundtrip("local", stt_spec(STTConfig(provider="local", model="moonshine-tiny-en")), "")
    finally:
        os.environ.pop("SLEEPTALK_MODELS_DIR", None)
    assert "check" in transcript.lower() and 0 < latency_ms < 500
