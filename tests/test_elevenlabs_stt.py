"""ElevenLabs Scribe as an STT provider: REST batch, one key with the existing TTS."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager

import pytest
from aiohttp import web

from sleeptalk.config import STTConfig
from sleeptalk.defaults import register_defaults
from sleeptalk.providers import ProviderSpec
from sleeptalk.speech.common import SpeechError
from sleeptalk.speech.elevenlabs_stt import ElevenLabsSTT
from sleeptalk.metering import UsageMeter

register_defaults()


@asynccontextmanager
async def scribe_server(handler):
    app = web.Application()
    app.router.add_post("/v1/speech-to-text", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    try:
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        yield f"http://127.0.0.1:{runner.addresses[0][1]}"
    finally:
        await runner.cleanup()


def spec(base_url, **overrides):
    return ProviderSpec(
        provider="elevenlabs",
        model=overrides.pop("model", "scribe_v2"),
        language=overrides.pop("language", "en-US"),
        options={"base_url": base_url, **overrides},
    )


async def _fields(request):
    reader = await request.multipart()
    fields = {}
    async for part in reader:
        fields[part.name] = (part.filename, await part.read())
    return fields


@pytest.mark.asyncio
async def test_rest_transcribes_wav_with_speed_flags(tmp_path):
    seen = {}

    async def handler(request):
        seen["headers"] = dict(request.headers)
        seen["fields"] = await _fields(request)
        return web.json_response({"text": "  hello there ", "language_code": "en", "words": []})

    async with scribe_server(handler) as base_url:
        stt = ElevenLabsSTT(spec(base_url), api_key="el-key")
        stt.set_usage_meter(UsageMeter(log_path=tmp_path / "usage.jsonl"))
        pcm = b"\x00\x01" * 4800
        assert await stt.transcribe_pcm(pcm) == "hello there"
        assert await stt.transcribe_pcm(b"") == ""
    assert seen["headers"]["xi-api-key"] == "el-key"
    fields = seen["fields"]
    assert fields["file"][0] == "utterance.wav" and fields["file"][1].startswith(b"RIFF")
    assert fields["model_id"][1] == b"scribe_v2"
    assert fields["language_code"][1] == b"en"
    assert fields["diarize"][1] == b"false"
    assert fields["timestamps_granularity"][1] == b"none"
    lines = [json.loads(line) for line in (tmp_path / "usage.jsonl").read_text().splitlines()]
    assert lines[-1]["provider"] == "elevenlabs" and lines[-1]["status"] == "ok"


@pytest.mark.asyncio
async def test_rest_error_is_actionable_and_settles_meter(tmp_path):
    async def handler(request):
        return web.Response(status=401, text="bad key")

    async with scribe_server(handler) as base_url:
        stt = ElevenLabsSTT(spec(base_url), api_key="el-key")
        stt.set_usage_meter(UsageMeter(log_path=tmp_path / "usage.jsonl"))
        with pytest.raises(SpeechError, match="401"):
            await stt.transcribe_pcm(b"\x00\x01" * 100)
    lines = [json.loads(line) for line in (tmp_path / "usage.jsonl").read_text().splitlines()]
    assert lines[-1]["status"] == "error"


@pytest.mark.asyncio
async def test_missing_key_is_reported_before_any_request():
    stt = ElevenLabsSTT(spec("http://127.0.0.1:1"), api_key="")
    with pytest.raises(SpeechError, match="ELEVENLABS_API_KEY"):
        await stt.transcribe_pcm(b"\x00\x01" * 100)


def test_registration_streams_scribe_v2_and_preserves_explicit_batch_models():
    from sleeptalk.builder import stt_spec
    from sleeptalk.provider_factory import build_stt_components

    components = build_stt_components(stt_spec(STTConfig(provider="elevenlabs")), {"ELEVENLABS_API_KEY": "key"})
    assert isinstance(components.rest, ElevenLabsSTT)
    from sleeptalk.speech.elevenlabs_streaming_stt import ElevenLabsStreamingSTT
    assert isinstance(components.streaming, ElevenLabsStreamingSTT)
    batch = build_stt_components(stt_spec(STTConfig(provider="elevenlabs", model="scribe_v1")), {})
    assert batch.streaming is None
    assert components.rest.spec.model == "scribe_v2"  # Deepgram's default nova-3 is replaced
    assert stt_spec(STTConfig(provider="elevenlabs", model="scribe_v1")).model == "scribe_v1"


@pytest.mark.asyncio
async def test_roundtrip_posts_the_bundled_clip():
    from sleeptalk.stt_roundtrip import stt_roundtrip

    seen = {}

    async def handler(request):
        seen["fields"] = await _fields(request)
        return web.json_response({"text": "sleeptalk check"})

    async with scribe_server(handler) as base_url:
        transcript, latency_ms = await stt_roundtrip("elevenlabs", spec(base_url), "el-key")
    assert transcript == "sleeptalk check" and latency_ms >= 0
    assert seen["fields"]["file"][0] == "sleeptalk-check.wav"
    assert seen["fields"]["model_id"][1] == b"scribe_v2"

    async def empty(request):
        return web.json_response({"text": ""})

    async with scribe_server(empty) as base_url:
        with pytest.raises(SpeechError, match="empty"):
            await stt_roundtrip("elevenlabs", spec(base_url), "el-key")


@pytest.mark.asyncio
async def test_connectivity_check_uses_the_roundtrip(monkeypatch):
    from sleeptalk.registries import STT_PROVIDERS

    calls = []

    async def roundtrip(spec, api_key, *, timeout):
        calls.append((spec.provider, api_key))
        if api_key == "bad":
            raise SpeechError("ElevenLabs STT rejected the key (HTTP 401)")
        return "ok", 1.0

    monkeypatch.setattr("sleeptalk.stt_roundtrip.elevenlabs_roundtrip", roundtrip)
    config = STTConfig(provider="elevenlabs")
    check = STT_PROVIDERS.get("elevenlabs").check
    await check(config, {"ELEVENLABS_API_KEY": "good"})
    with pytest.raises(RuntimeError, match="401"):
        await check(config, {"ELEVENLABS_API_KEY": "bad"})
    assert calls == [("elevenlabs", "good"), ("elevenlabs", "bad")]


def test_one_key_wizard_wiring():
    from sleeptalk import wizard
    from sleeptalk.language_support import lookup

    answers = wizard.WizardAnswers()
    notes = wizard.apply_speech_provider(answers, "elevenlabs")
    assert (answers.stt_provider, answers.stt_model, answers.tts_provider) == ("elevenlabs", "scribe_v2", "elevenlabs")
    assert answers.tts_voice_id == wizard.DEFAULT_ELEVENLABS_VOICE
    assert wizard.capability_providers(answers) == ["elevenlabs-stt", "elevenlabs"]
    assert lookup("en-US", "elevenlabs-stt").tier == lookup("en-US", "elevenlabs").tier
    assert isinstance(notes, list)
