"""OpenRouter, Groq, Together, Venice as speech providers: one key, the
OpenAI-shaped audio routes, batch listening and decoded speaking."""
from __future__ import annotations

import asyncio
import io
import json
import wave
from contextlib import asynccontextmanager

import pytest
from aiohttp import web

from sleeptalk.config import STTConfig, TTSConfig
from sleeptalk.defaults import register_defaults
from sleeptalk.metering import UsageMeter
from sleeptalk.providers import ProviderSpec
from sleeptalk.speech.common import SpeechError
from sleeptalk.speech.openai_audio import PRESETS, OpenAIAudioSTT, OpenAIAudioTTS, check_key, model_for_voice, preset_for

register_defaults()


@asynccontextmanager
async def audio_server(*, transcribe=None, speak=None, models=None):
    app = web.Application()
    if transcribe is not None:
        app.router.add_post("/audio/transcriptions", transcribe)
    if speak is not None:
        app.router.add_post("/audio/speech", speak)
    if models is not None:
        app.router.add_get("/models", models)
    runner = web.AppRunner(app)
    await runner.setup()
    try:
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        yield f"http://127.0.0.1:{runner.addresses[0][1]}"
    finally:
        await runner.cleanup()


def pcm_seconds(seconds: float, rate: int = 48_000) -> bytes:
    return b"\x01\x00" * int(rate * seconds)


def wav_bytes(pcm: bytes, rate: int) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(pcm)
    return buffer.getvalue()


async def test_transcription_posts_a_wav_with_model_and_language_and_reads_cost(tmp_path):
    seen = {}

    async def transcribe(request):
        assert request.headers["Authorization"] == "Bearer or-key"
        assert request.headers["X-Title"] == "Sleeptalk"
        form = await request.post()
        seen["fields"] = {k: (v.filename if hasattr(v, "filename") else v) for k, v in form.items()}
        seen["wav"] = form["file"].file.read()
        return web.json_response({"text": "  hello there ", "usage": {"seconds": 1, "cost": 0.0000033}})

    async with audio_server(transcribe=transcribe) as base:
        meter = UsageMeter(log_path=tmp_path / 'usage.jsonl')
        stt = OpenAIAudioSTT(ProviderSpec(provider="openrouter", language="en-GB", options={"base_url": base}), api_key="or-key")
        stt.set_usage_meter(meter)
        assert await stt.transcribe_pcm(pcm_seconds(0.5)) == "hello there"
    assert seen["fields"] == {"file": "utterance.flac", "model": "nvidia/parakeet-tdt-0.6b-v3", "response_format": "json", "language": "en"}
    # Uploaded at 16 kHz as FLAC: lossless, and smaller than a WAV of the same half second.
    import av

    with av.open(io.BytesIO(seen["wav"])) as clip:
        stream = clip.streams.audio[0]
        assert (stream.codec_context.name, stream.rate, stream.channels) == ("flac", 16_000, 1)
        frames = sum(frame.samples for frame in clip.decode(audio=0))
    assert abs(frames - 8_000) < 80
    assert stt.last_cost_usd == pytest.approx(0.0000033)
    assert await stt.transcribe_pcm(b"") == ""


async def test_transcription_errors_are_sanitized_and_keyless_is_refused():
    async def transcribe(request):
        return web.json_response({"error": {"message": "bad key"}}, status=401)

    async with audio_server(transcribe=transcribe) as base:
        stt = OpenAIAudioSTT(ProviderSpec(provider="groq", options={"base_url": base}), api_key="k")
        with pytest.raises(SpeechError, match=r"Groq transcription failed \(401; model=whisper-large-v3-turbo\)"):
            await stt.transcribe_pcm(pcm_seconds(0.1))
    with pytest.raises(SpeechError, match="Missing GROQ_API_KEY"):
        await OpenAIAudioSTT(ProviderSpec(provider="groq"), api_key="").transcribe_pcm(pcm_seconds(0.1))


async def test_speech_asks_for_pcm_and_upsamples_it_to_canonical_audio():
    seen = {}

    async def speak(request):
        seen["body"] = await request.json()
        # 24 kHz raw PCM, one second, as OpenRouter returns it.
        return web.Response(body=pcm_seconds(1.0, 24_000), content_type="audio/pcm")

    async with audio_server(speak=speak) as base:
        tts = OpenAIAudioTTS(ProviderSpec(provider="openrouter", voice_id="flux-kit-en", options={"base_url": base}), api_key="or-key")
        pcm = await tts.synthesize_pcm_bytes("Hello there.")
    assert seen["body"] == {"model": "deepgram/flux-tts:free", "input": "Hello there.", "voice": "flux-kit-en", "response_format": "pcm"}
    assert abs(len(pcm) - 48_000 * 2) < 48_000 * 2 * 0.02


async def test_speech_decodes_containers_and_honours_a_rate_in_the_content_type():
    async def speak(request):
        body = await request.json()
        if body["response_format"] == "wav":
            return web.Response(body=wav_bytes(pcm_seconds(0.5, 24_000), 24_000), content_type="audio/wav")
        return web.Response(body=pcm_seconds(0.5, 16_000), headers={"Content-Type": "audio/pcm;rate=16000"})

    async with audio_server(speak=speak) as base:
        together = OpenAIAudioTTS(ProviderSpec(provider="together", options={"base_url": base}), api_key="k")
        assert abs(len(await together.synthesize_pcm_bytes("Hi.")) - 48_000) < 48_000 * 0.02
        venice = OpenAIAudioTTS(ProviderSpec(provider="venice", options={"base_url": base}), api_key="k")
        assert abs(len(await venice.synthesize_pcm_bytes("Hi.")) - 48_000) < 48_000 * 0.02


async def test_speech_failures_name_the_model_voice_and_format_and_keep_the_key_out():
    async def speak(request):
        return web.Response(text="voice not found sk-secret-echo", status=404)

    async with audio_server(speak=speak) as base:
        tts = OpenAIAudioTTS(ProviderSpec(provider="openrouter", voice_id="nope", options={"base_url": base}), api_key="or-key")
        with pytest.raises(SpeechError) as failure:
            await tts.synthesize_pcm_bytes("Hello.")
    message = str(failure.value)
    assert "404" in message and "voice=nope" in message and "or-key" not in message
    with pytest.raises(SpeechError, match="empty or punctuation-only"):
        await tts.synthesize_pcm_bytes("...")


def test_a_voice_name_picks_its_model_on_openrouter():
    preset = PRESETS["openrouter"]
    assert model_for_voice(preset, "flux-hannah-en", "") == "deepgram/flux-tts:free"
    assert model_for_voice(preset, "aura-2-thalia-en", "deepgram/flux-tts:free") == "deepgram/aura-2"
    assert model_for_voice(preset, "en-US-Harper:MAI-Voice-2", "") == "microsoft/mai-voice-2-flash"
    assert model_for_voice(preset, "eve", "") == "x-ai/grok-voice-tts-1.0"
    assert model_for_voice(preset, "af_sky", "") == "hexgrad/kokoro-82m"
    assert model_for_voice(preset, "Zephyr", "google/gemini-3.1-flash-tts-preview") == "google/gemini-3.1-flash-tts-preview"
    assert model_for_voice(PRESETS["groq"], "troy", "") == "canopylabs/orpheus-v1-english"
    assert model_for_voice(PRESETS["venice"], "eve", "") == "tts-xai-v1"
    assert model_for_voice(PRESETS["venice"], "tara", "") == "tts-orpheus"
    assert model_for_voice(PRESETS["venice"], "af_sky", "") == "tts-kokoro"
    assert model_for_voice(PRESETS["together"], "db6b0ed5-d5d3-463d-ae85-518a07d3c2b4", "") == "cartesia/sonic-3"
    assert model_for_voice(PRESETS["together"], "tara", "") == "canopylabs/orpheus-3b-0.1-ft"
    for preset in PRESETS.values():  # Kokoro is never a default
        assert not preset.tts_model.lower().startswith(("hexgrad", "tts-kokoro"))
    tts = OpenAIAudioTTS(ProviderSpec(provider="openrouter", voice_id="eve", speed=1.25), api_key="k")
    assert tts.request_body("Hi.")["model"] == "x-ai/grok-voice-tts-1.0" and tts.request_body("Hi.")["speed"] == 1.25


def test_venice_is_asked_to_stream_and_the_others_get_no_extra_field():
    # Without it Venice sends nothing until the whole reply is rendered.
    venice = OpenAIAudioTTS(ProviderSpec(provider="venice"), api_key="k")
    assert venice.request_body("Hi.")["streaming"] is True
    for name in ("openrouter", "groq", "together"):
        assert "streaming" not in OpenAIAudioTTS(ProviderSpec(provider=name), api_key="k").request_body("Hi.")


async def test_key_check_uses_the_models_route():
    calls = []

    async def models(request):
        calls.append(request.headers.get("Authorization"))
        return web.json_response({"data": []}) if request.headers.get("Authorization") == "Bearer good" else web.Response(status=401)

    async with audio_server(models=models) as base:
        await check_key(PRESETS["openrouter"], "good", base)
        with pytest.raises(SpeechError, match="rejected the API key"):
            await check_key(PRESETS["openrouter"], "bad", base)
    assert calls == ["Bearer good", "Bearer bad"]


def test_every_preset_is_registered_for_listening_and_speaking_with_its_own_key():
    from sleeptalk.builder import stt_spec, tts_spec
    from sleeptalk.config import _CREDENTIAL_PROVIDERS
    from sleeptalk.private_input import ENV_NAMES
    from sleeptalk.registries import STT_PROVIDERS, TTS_PROVIDERS
    from sleeptalk.wizard import DEFAULT_VOICES, STT_DEFAULT_MODELS, _STT_KEY_ENV, _TTS_KEY_ENV, env_entries, WizardAnswers
    from sleeptalk import voice_catalog

    for name, preset in PRESETS.items():
        assert STT_PROVIDERS.get(name).required_env_for(STTConfig(provider=name)) == {preset.env: STT_PROVIDERS.get(name).required_env_for(STTConfig(provider=name))[preset.env]}
        assert TTS_PROVIDERS.get(name).required_env_for(TTSConfig(provider=name)) .keys() == {preset.env}
        assert stt_spec(STTConfig(provider=name)).model == preset.stt_model
        assert tts_spec(TTSConfig(provider=name)).model == preset.tts_model
        assert stt_spec(STTConfig(provider=name, model="vendor/other-model")).model == "vendor/other-model"
        assert _CREDENTIAL_PROVIDERS[preset.env] == name and ENV_NAMES[name] == preset.env
        assert _STT_KEY_ENV[name] == preset.env and _TTS_KEY_ENV[name] == preset.env
        assert STT_DEFAULT_MODELS[name] == preset.stt_model and DEFAULT_VOICES[name] == preset.tts_voice
        assert voice_catalog.default_voice_id(name) == preset.tts_voice
        assert env_entries(WizardAnswers(stt_provider=name, tts_provider=name, stt_key="k", tts_key="k")) == {preset.env: "k"}
        built = TTS_PROVIDERS.build(name, tts_spec(TTSConfig(provider=name)), {preset.env: "k"})
        assert isinstance(built, OpenAIAudioTTS) and built.voice_id == preset.tts_voice
        components = STT_PROVIDERS.build(name, stt_spec(STTConfig(provider=name)), {preset.env: "k"})
        assert isinstance(components.rest, OpenAIAudioSTT) and components.streaming is None
    assert preset_for("OpenRouter").name == "openrouter"
    with pytest.raises(SpeechError):
        preset_for("nope")


async def test_roundtrip_dispatches_to_the_shared_adapter(monkeypatch):
    from sleeptalk import stt_roundtrip

    async def transcribe(request):
        form = await request.post()
        assert form["model"] == "nvidia/parakeet-tdt-0.6b-v3"
        return web.json_response({"text": "the quick brown fox"})

    async with audio_server(transcribe=transcribe) as base:
        transcript, latency = await stt_roundtrip.stt_roundtrip(
            "openrouter", ProviderSpec(provider="openrouter", options={"base_url": base}), "k")
    assert transcript == "the quick brown fox" and latency >= 0


async def test_speech_streams_pcm_as_it_arrives_in_20ms_pieces():
    """The first audio plays before the service has finished sending."""
    import asyncio
    import time

    async def speak(request):
        response = web.StreamResponse(headers={"Content-Type": "audio/pcm"})
        await response.prepare(request)
        for _ in range(4):
            await response.write(pcm_seconds(0.25, 24_000))  # 250 ms of audio per chunk
            await asyncio.sleep(0.15)
        await response.write_eof()
        return response

    async with audio_server(speak=speak) as base:
        tts = OpenAIAudioTTS(ProviderSpec(provider="openrouter", options={"base_url": base}), api_key="k")
        started = time.monotonic()
        arrivals, total = [], 0
        async for chunk in tts.stream_pcm("Hello there, this is streamed."):
            arrivals.append(time.monotonic() - started)
            assert len(chunk) <= 1_920 and len(chunk) % 2 == 0
            total += len(chunk)
    assert arrivals[0] < 0.2, "first audio must arrive before the body completes (~0.45 s)"
    assert abs(total - 48_000 * 2) < 48_000 * 2 * 0.02  # one second at 48 kHz


async def test_speech_streams_wav_bodies_and_downmixes_stereo():
    async def speak(request):
        stereo = b"".join(b"\x10\x00\x30\x00" for _ in range(24_000))  # 1 s stereo at 24 kHz
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as out:
            out.setnchannels(2); out.setsampwidth(2); out.setframerate(24_000); out.writeframes(stereo)
        return web.Response(body=buffer.getvalue(), content_type="audio/wav")

    async with audio_server(speak=speak) as base:
        tts = OpenAIAudioTTS(ProviderSpec(provider="groq", options={"base_url": base}), api_key="k")
        chunks = [chunk async for chunk in tts.stream_pcm("Hi.")]
    total = sum(len(c) for c in chunks)
    assert abs(total - 48_000 * 2) < 48_000 * 2 * 0.02 and all(len(c) <= 1_920 for c in chunks)
    assert 0x18 <= int.from_bytes(chunks[5][:2], "little") <= 0x28  # the mean of the two channels


async def test_mp3_bodies_are_decoded_once_complete():
    from sleeptalk.media_audio import pcm_to_wav

    async def speak(request):
        # Not streamable by shape: an MP3 sync word, so the adapter must read it whole and decode.
        return web.Response(body=b"\xff\xfb\x90\x00" + b"\x00" * 100, content_type="audio/mpeg")

    async with audio_server(speak=speak) as base:
        tts = OpenAIAudioTTS(ProviderSpec(provider="openrouter", voice_id="en_paul_neutral", options={"base_url": base, "output_format": "mp3"}), api_key="k")
        with pytest.raises(SpeechError, match="could not decode"):
            [chunk async for chunk in tts.stream_pcm("Hi.")]


@pytest.mark.parametrize("provider", list(PRESETS))
def test_the_call_path_builds_every_preset_through_the_provider_factory(provider, tmp_path):
    """`host start-voice` failed on the VPS with "STT provider 'venice' must return
    STTComponents" (2026-09-22): the registry test passed while the factory
    the call uses did not. Build through that factory, for listening and speaking."""
    from sleeptalk.builder import stt_spec, tts_spec
    from sleeptalk.provider_factory import build_stt_components, build_tts, build_tts_chain
    from sleeptalk.metering import UsageMeter

    preset = PRESETS[provider]
    env = {preset.env: "k"}
    meter = UsageMeter(log_path=tmp_path / "usage.jsonl")
    components = build_stt_components(stt_spec(STTConfig(provider=provider)), env, meter=meter)
    assert isinstance(components.rest, OpenAIAudioSTT) and components.streaming is None
    assert components.rest.usage_meter is meter
    tts = build_tts(tts_spec(TTSConfig(provider=provider)), env, meter=meter)
    assert isinstance(tts, OpenAIAudioTTS) and callable(tts.stream_pcm) and tts.usage_meter is meter
    chain = build_tts_chain([tts_spec(TTSConfig(provider=provider))], env, meter=meter)
    assert len(chain) == 1 and isinstance(chain[0], OpenAIAudioTTS)


from tests.test_component_extensions import components  # noqa: E402,F401 — the fixture agent and surface


@pytest.mark.parametrize("provider", list(PRESETS))
async def test_a_whole_session_assembles_from_an_aggregator_profile(components, tmp_path, provider):
    """What `host start-voice` does with the profile the reuse path writes:
    the session builder wires the aggregator in for listening and speaking."""
    from sleeptalk.builder import build_session
    from sleeptalk.config import load_config

    preset = PRESETS[provider]
    path = tmp_path / "profile.toml"
    path.write_text(
        '[agent]\ntype = "fixture-agent"\n[agent.options]\nmarker = "agent"\n'
        '[transport]\ntype = "fixture-surface"\n[transport.options]\nmarker = "surface"\n'
        f'[stt]\nprovider = "{provider}"\nmodel = "{preset.stt_model}"\n'
        f'[tts]\nprovider = "{provider}"\nvoice_id = "{preset.tts_voice}"\n'
    )
    config = load_config(path, check_env=False, check_binaries=False)
    session = build_session(config, env={preset.env: "k"})
    assert isinstance(session.tts, OpenAIAudioTTS) and session.tts.voice_id == preset.tts_voice
    # A batch listener gets the tighter silence windows automatically.
    assert session.policy.endpointing == "batch"
    assert isinstance(session.stt, OpenAIAudioSTT) and session.stt.model == preset.stt_model
    await session.aclose()


async def test_batch_listener_endpointing_respects_an_explicit_choice(components, tmp_path):
    from sleeptalk.builder import build_session
    from sleeptalk.config import load_config
    from sleeptalk.policy import ENDPOINTING_PRESETS

    # Balanced's automatic batch compensation stays at its existing timings;
    # selecting Snappy explicitly now opts into a faster policy.
    assert ENDPOINTING_PRESETS["batch"]["semantic_complete_silence_ms"] == 450
    assert ENDPOINTING_PRESETS["aggressive"]["semantic_complete_silence_ms"] == 180
    relaxed = tmp_path / "relaxed.toml"
    relaxed.write_text(
        '[agent]\ntype = "fixture-agent"\n[agent.options]\nmarker = "agent"\n'
        '[transport]\ntype = "fixture-surface"\n[transport.options]\nmarker = "surface"\n'
        '[stt]\nprovider = "openrouter"\n[tts]\nprovider = "openrouter"\n[policy]\nendpointing = "relaxed"\n'
    )
    session = build_session(load_config(relaxed, check_env=False, check_binaries=False), env={"OPENROUTER_API_KEY": "k"})
    assert session.policy.endpointing == "relaxed"
    await session.aclose()
    streaming = tmp_path / "streaming.toml"
    streaming.write_text(
        '[agent]\ntype = "fixture-agent"\n[agent.options]\nmarker = "agent"\n'
        '[transport]\ntype = "fixture-surface"\n[transport.options]\nmarker = "surface"\n'
        '[stt]\nprovider = "deepgram"\n[tts]\nprovider = "deepgram"\n'
    )
    session = build_session(load_config(streaming, check_env=False, check_binaries=False), env={"DEEPGRAM_API_KEY": "k"})
    assert session.policy.endpointing == "balanced"
    await session.aclose()


def test_wizard_offers_found_keys_first():
    from types import SimpleNamespace
    from sleeptalk.key_discovery import ReusableKey
    from sleeptalk.wizard import found_key_choices

    found = {"openrouter": ReusableKey(provider="openrouter", env="OPENROUTER_API_KEY", kind="dotenv", location="/x/.env", source="~/.hermes/.env")}
    (choice,) = found_key_choices(found, lambda title, value: SimpleNamespace(title=title, value=value))
    assert choice.value == "found:openrouter" and "~/.hermes/.env" in choice.title and "nvidia/parakeet-tdt-0.6b-v3" in choice.title
    assert found_key_choices({}, SimpleNamespace) == []


@pytest.mark.parametrize("status", [200, 401])
async def test_the_setup_check_closes_its_connection(monkeypatch, status):
    """A one-off key check has no next turn: leaving the keep-alive session
    open printed "Unclosed client session" when the terminal wizard exited."""
    from sleeptalk.stt_roundtrip import openai_audio_roundtrip

    sessions = []
    original = OpenAIAudioSTT.client

    def tracked(self):
        session = original(self)
        sessions.append(session)
        return session

    monkeypatch.setattr(OpenAIAudioSTT, "client", tracked)

    async def transcribe(request):
        await request.post()
        if status != 200:
            return web.json_response({"error": {"message": "bad key"}}, status=status)
        return web.json_response({"text": "hello"})

    async with audio_server(transcribe=transcribe) as base:
        spec = ProviderSpec(provider="openrouter", options={"base_url": base})
        if status == 200:
            assert (await openai_audio_roundtrip(spec, "or-key"))[0] == "hello"
        else:
            with pytest.raises(SpeechError):
                await openai_audio_roundtrip(spec, "or-key")
    assert sessions and all(session.closed for session in sessions)

def voiced_pcm(seconds: float = 1.0, quiet_ms: int = 300) -> bytes:
    """Room hush, a tone loud enough to be a voice, room hush: 48 kHz mono."""
    import math
    import struct

    def tone(ms, amplitude):
        count = 48 * ms
        return struct.pack(f"<{count}h", *(int(amplitude * math.sin(2 * math.pi * 220 * i / 48_000)) for i in range(count)))

    return tone(quiet_ms, 20) + tone(int(seconds * 1000), 6000) + tone(quiet_ms, 20)


async def test_a_stalled_transcription_gets_a_twin_and_the_first_answer_wins():
    calls = []

    async def transcribe(request):
        calls.append(len(calls))
        await request.post()
        if len(calls) == 1:
            await asyncio.sleep(1.0)  # the one-in-six stall
            return web.json_response({"text": "late"})
        return web.json_response({"text": "on time"})

    async with audio_server(transcribe=transcribe) as base:
        stt = OpenAIAudioSTT(ProviderSpec(provider="openrouter", options={"base_url": base, "hedge_after_ms": 100}), api_key="k")
        started = asyncio.get_running_loop().time()
        assert await stt.transcribe_pcm(voiced_pcm()) == "on time"
        assert asyncio.get_running_loop().time() - started < 0.8
        await stt.close()
    assert len(calls) == 2


async def test_no_twin_when_the_answer_comes_in_time_or_hedging_is_off():
    calls = []

    async def transcribe(request):
        calls.append(1)
        await request.post()
        await asyncio.sleep(0.2)
        return web.json_response({"text": "hello"})

    async with audio_server(transcribe=transcribe) as base:
        for options in ({"hedge_after_ms": 1000}, {"hedge_after_ms": 0}):
            stt = OpenAIAudioSTT(ProviderSpec(provider="openrouter", options={"base_url": base, **options}), api_key="k")
            assert await stt.transcribe_pcm(voiced_pcm()) == "hello"
            await stt.close()
    assert len(calls) == 2


async def test_an_empty_answer_for_a_voice_is_asked_of_whisper_once():
    models = []
    answers = {"nvidia/parakeet-tdt-0.6b-v3": "", "openai/whisper-large-v3-turbo": "Check the logs."}

    async def transcribe(request):
        form = await request.post()
        models.append(form["model"])
        return web.json_response({"text": answers[form["model"]]})

    async with audio_server(transcribe=transcribe) as base:
        stt = OpenAIAudioSTT(ProviderSpec(provider="openrouter", options={"base_url": base}), api_key="k")
        assert await stt.transcribe_pcm(voiced_pcm()) == "Check the logs."
        assert models == ["nvidia/parakeet-tdt-0.6b-v3", "openai/whisper-large-v3-turbo"]
        # Whisper's stock phrase for audio it can't read is not a turn.
        answers["openai/whisper-large-v3-turbo"] = "Thank you."
        assert await stt.transcribe_pcm(voiced_pcm()) == ""
        # Nothing but hush: the empty answer is right; nobody is asked again.
        models.clear()
        assert await stt.transcribe_pcm(b"\x05\x00" * 48_000) == ""
        assert models == ["nvidia/parakeet-tdt-0.6b-v3"]
        await stt.close()


async def test_a_stalled_first_audio_gets_a_twin_and_the_loser_is_closed():
    calls = []

    async def speak(request):
        calls.append(1)
        await request.json()
        if len(calls) == 1:
            await asyncio.sleep(1.0)
        return web.Response(body=pcm_seconds(0.5, 24_000), content_type="audio/pcm")

    async with audio_server(speak=speak) as base:
        tts = OpenAIAudioTTS(ProviderSpec(provider="openrouter", voice_id="eve", options={"base_url": base, "hedge_after_ms": 100}), api_key="k")
        started = asyncio.get_running_loop().time()
        pcm = await tts.synthesize_pcm_bytes("Hello there.")
        assert asyncio.get_running_loop().time() - started < 0.8
        assert abs(len(pcm) - 48_000) < 48_000 * 0.02
        await tts.close()
    assert len(calls) == 2


def test_trimming_keeps_a_soft_last_syllable_and_a_margin():
    from sleeptalk.speech.openai_audio import trim_quiet_ends
    import math
    import struct

    def tone(ms, amplitude, rate=16_000):
        count = rate * ms // 1000
        return struct.pack(f"<{count}h", *(int(amplitude * math.sin(2 * math.pi * 220 * i / rate)) for i in range(count)))

    speech, soft = tone(800, 6000), tone(200, 900)
    pcm = tone(600, 20) + speech + tone(300, 20) + soft + tone(900, 20)
    trimmed = trim_quiet_ends(pcm, 16_000)
    assert soft in trimmed and speech in trimmed
    # 250 ms of hush kept at each end; the rest of the 600 + 900 ms is gone.
    assert len(pcm) - len(trimmed) == pytest.approx(2 * 16 * (600 + 900 - 2 * 250), abs=2 * 16 * 40)


async def test_keep_warm_reconnects_only_after_idling(monkeypatch):
    stt = OpenAIAudioSTT(ProviderSpec(provider="openrouter"), api_key="k")
    warmed = []

    async def warmup():
        warmed.append(1)

    monkeypatch.setattr(stt, "warmup", warmup)
    stt.client()
    await stt.keep_warm(idle_seconds=20)
    assert warmed == []
    stt._last_used -= 30
    await stt.keep_warm(idle_seconds=20)
    assert warmed == [1]
    await stt.close()


async def test_a_long_sentence_is_not_cut_off_by_the_request_limit():
    """The call reads a sentence as fast as it plays. A limit on the whole
    request cut any sentence that played longer than it; only a stalled
    connection or read may time out."""
    import asyncio

    async def speak(request):
        response = web.StreamResponse(headers={"Content-Type": "audio/pcm"})
        await response.prepare(request)
        for _ in range(12):  # three seconds of audio, sent over ~1.8 s like a speech service
            await response.write(pcm_seconds(0.25, 24_000))
            await asyncio.sleep(0.15)
        await response.write_eof()
        return response

    async with audio_server(speak=speak) as base:
        options = {"base_url": base, "rest_timeout_seconds": 1.0}
        tts = OpenAIAudioTTS(ProviderSpec(provider="openrouter", options=options), api_key="k")
        total = 0
        async for chunk in tts.stream_pcm("A long sentence, spoken for three seconds."):
            total += len(chunk)
            await asyncio.sleep(len(chunk) / 96_000 * 0.5)  # consumed at twice real time: ~1.5 s
    assert abs(total - 48_000 * 2 * 3) < 48_000 * 2 * 3 * 0.02  # all three seconds arrived


async def test_a_voice_that_sends_nothing_fails_within_the_first_audio_limit():
    """Fault proxy, live: a stalled voice held the turn for the 20 s read
    timeout, then the fallback voice and the apology waited again (31 s)."""
    async def speak(request):
        await request.json()
        await asyncio.sleep(5)
        return web.Response(body=pcm_seconds(0.5, 24_000), content_type="audio/pcm")

    async with audio_server(speak=speak) as base:
        tts = OpenAIAudioTTS(ProviderSpec(provider="openrouter", voice_id="eve", options={
            "base_url": base, "hedge_after_ms": 100, "first_audio_timeout_seconds": 0.4}), api_key="k")
        started = asyncio.get_running_loop().time()
        with pytest.raises(SpeechError, match="no audio within"):
            await tts.synthesize_pcm_bytes("Hello there.")
        assert asyncio.get_running_loop().time() - started < 1.5
        await tts.close()
