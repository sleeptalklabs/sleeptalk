import asyncio
import io
import struct
import wave
from contextlib import aclosing
from types import SimpleNamespace

import pytest

from sleeptalk.providers import ProviderSpec, collect_tts_audio
from sleeptalk.routes.base import TurnContext
from sleeptalk.routes.cascade import CascadeRoute
from sleeptalk.media_audio import AudioDecodeError, decode_audio_bytes, pcm_to_wav
from sleeptalk.speech.common import _pcm_to_wav
from sleeptalk.speech.deepgram import DeepgramSTT, _DeepgramStreamingTurn
from sleeptalk.speech.elevenlabs import ElevenLabsTTS
from sleeptalk.speech.fallback import FallbackTTS, tts_cache_identity
from sleeptalk.speech.hume import HumeTTS
from sleeptalk.speech.playht import PlayHTTTS
from sleeptalk.speech.rime import RimeTTS
from sleeptalk.voice_policy_runtime import VoicePolicyRuntime


PCM = struct.pack("<4h", 1000, -2000, 3000, -4000) * 480


class Provider:
    def __init__(self, chunks=(), error=None):
        self.chunks = chunks
        self.error = error
        self.calls = 0
        self.iterators_closed = 0
        self.closed = 0

    async def synthesize(self, text):
        self.calls += 1
        try:
            for chunk in self.chunks:
                yield chunk
            if self.error:
                raise self.error
        finally:
            self.iterators_closed += 1

    async def close(self):
        self.closed += 1


async def collect(provider):
    async with aclosing(provider.synthesize("hello")) as stream:
        return b"".join([chunk async for chunk in stream])


@pytest.mark.parametrize("error", [None, RuntimeError("upstream failure")])
async def test_empty_or_failed_primary_uses_next_provider_and_closes_iterator(error):
    primary = Provider([b""], error)
    backup = Provider([PCM])
    tts = FallbackTTS(primary, [backup])
    assert await collect(tts) == PCM
    assert primary.iterators_closed == backup.iterators_closed == 1
    await tts.close()
    await tts.close()
    assert primary.closed == backup.closed == 1


async def test_partial_audio_never_restarts_sentence_on_backup():
    primary = Provider([PCM], RuntimeError("failed after audio"))
    backup = Provider([PCM])
    tts = FallbackTTS(primary, [backup])
    async with aclosing(tts.synthesize("hello")) as stream:
        assert await anext(stream) == PCM
        with pytest.raises(RuntimeError, match="after audio"):
            await anext(stream)
    assert backup.calls == 0
    assert primary.iterators_closed == 1


async def test_cancellation_before_audio_never_uses_fallback():
    primary = Provider(error=asyncio.CancelledError())
    backup = Provider([PCM])
    with pytest.raises(asyncio.CancelledError):
        await collect(FallbackTTS(primary, [backup]))
    assert backup.calls == 0
    assert primary.iterators_closed == 1


async def test_output_sink_failure_does_not_trigger_fallback():
    primary = Provider([PCM, PCM])
    backup = Provider([PCM])

    class Sink:
        async def write(self, pcm, turn_id):
            raise RuntimeError("speaker failed")

    route = CascadeRoute(agent=object(), tts=FallbackTTS(primary, [backup]), audio_output=Sink())
    ctx = TurnContext(session_id="fallback", turn_id=1, emit=lambda event: None)
    with pytest.raises(RuntimeError, match="speaker failed"):
        await route._speak_audio("hello", ctx)
    assert backup.calls == 0
    assert primary.iterators_closed == 1
    await route.close()


async def test_close_cancels_active_request_before_closing_all_providers():
    started = asyncio.Event()

    class Blocking(Provider):
        async def synthesize(self, text):
            started.set()
            try:
                await asyncio.Event().wait()
                yield PCM
            finally:
                self.iterators_closed += 1

    primary = Blocking()
    backup = Provider([PCM])
    tts = FallbackTTS(primary, [backup])
    pending = asyncio.create_task(collect(tts))
    await started.wait()
    await tts.close()
    assert pending.cancelled()
    assert primary.iterators_closed == 1
    assert primary.closed == backup.closed == 1
    assert backup.calls == 0


async def test_cleanup_failure_does_not_skip_other_provider():
    class BadClose(Provider):
        async def close(self):
            raise RuntimeError("cleanup failed")

    backup = Provider()
    tts = FallbackTTS(BadClose(), [backup])
    with pytest.raises(ExceptionGroup, match="cleanup"):
        await tts.close()
    assert backup.closed == 1


async def test_route_closes_unprepared_stt_once_even_when_another_provider_fails():
    class BadClose(Provider):
        async def close(self):
            raise RuntimeError("cleanup failed")

    stt = Provider()
    agent = Provider()
    route = CascadeRoute(agent=agent, tts=BadClose(), stt=stt, stt_streaming=stt)
    with pytest.raises(ExceptionGroup, match="cleanup"):
        await route.close()
    assert stt.closed == agent.closed == 1


async def test_route_prepares_streaming_provider_once_and_resets_capture():
    class Streaming(Provider):
        warmups = 0
        resets = 0

        async def warmup(self):
            self.warmups += 1

        async def reset(self):
            self.resets += 1

    stt = Streaming()
    route = CascadeRoute(agent=Provider(), tts=Provider([PCM]), stt=stt, stt_streaming=stt)
    await route.prepare(SimpleNamespace())
    await route.prepare(SimpleNamespace())
    await route.reset_capture()
    assert stt.warmups == stt.resets == 1
    await route.close()
    assert stt.closed == 1


def test_cache_identity_includes_fallback_voice_and_private_settings():
    primary = Provider()
    backup = Provider()
    backup.spec = SimpleNamespace(voice_id="first", options={"region": "eu"})
    tts = FallbackTTS(primary, [backup])
    original = tts_cache_identity(tts)
    backup.spec.voice_id = "second"
    assert tts_cache_identity(tts) != original
    original = tts_cache_identity(tts)
    backup.spec.options["region"] = "us"
    assert tts_cache_identity(tts) != original


def test_wav_decoder_preserves_canonical_samples():
    assert decode_audio_bytes(pcm_to_wav(PCM)) == PCM


def test_wav_decoder_resamples_stereo_at_provider_boundary():
    samples = struct.pack("<2h", 1500, 1500) * 2400
    wav = _pcm_to_wav(samples, channels=2, sample_width=2, sample_rate=24_000)
    output = decode_audio_bytes(wav)
    assert len(output) == 4800 * 2
    assert any(output)


def test_invalid_encoded_audio_fails_before_reaching_speaker():
    with pytest.raises(AudioDecodeError, match="invalid audio"):
        decode_audio_bytes(b"not an audio file")


@pytest.mark.parametrize("provider,adapter", [
    ("elevenlabs", ElevenLabsTTS), ("hume", HumeTTS),
    ("playht", PlayHTTTS), ("rime", RimeTTS),
])
async def test_buffered_provider_emits_pcm_but_clip_api_returns_wav(monkeypatch, provider, adapter):
    kwargs = {"api_key": "unused"}
    if provider == "playht":
        kwargs["user_id"] = "unused"
    tts = adapter(ProviderSpec(provider=provider), **kwargs)

    async def response(text):
        return pcm_to_wav(PCM)

    monkeypatch.setattr(tts, "synthesize_wav_bytes", response)
    assert await collect(tts) == PCM
    clip = await collect_tts_audio(tts, "hello")
    with wave.open(io.BytesIO(clip), "rb") as wav:
        assert (wav.getnchannels(), wav.getframerate(), wav.getsampwidth()) == (1, 48_000, 2)
        assert wav.readframes(wav.getnframes()) == PCM


async def test_deepgram_rest_declares_mono_and_preserves_pcm(monkeypatch):
    import sleeptalk.speech.deepgram as module

    requests = []

    class Response:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def json(self):
            return {"results": {"channels": [{"alternatives": [{"transcript": "hello"}]}]}}

    class Client(Response):
        def post(self, url, **kwargs):
            requests.append(kwargs)
            return Response()

    monkeypatch.setattr(module.aiohttp, "ClientSession", Client)
    tts = DeepgramSTT(ProviderSpec(provider="deepgram", model="nova-3"), api_key="mock")
    assert await tts.transcribe_pcm(PCM) == "hello"
    with wave.open(io.BytesIO(requests[0]["data"]), "rb") as wav:
        assert wav.getnchannels() == 1
        assert wav.readframes(wav.getnframes()) == PCM


async def test_deepgram_stream_sends_mono_without_halving_samples(monkeypatch):
    writes = []

    async def connect():
        pass

    async def send(pcm):
        writes.append(pcm)

    turn = _DeepgramStreamingTurn(ProviderSpec(provider="deepgram"), api_key="mock")
    turn._ws = SimpleNamespace(send_bytes=send)
    monkeypatch.setattr(turn, "_connect", connect)
    await turn.send_pcm(PCM)
    assert writes == [PCM]
    assert turn._sent_source_bytes / turn._SOURCE_BYTES_PER_MS == 40


async def test_shared_policy_passes_exact_mono_to_unknown_provider():
    calls = []

    class STT:
        async def transcribe_pcm(self, pcm):
            calls.append(pcm)
            return "hello"

    runtime = VoicePolicyRuntime(agent=object(), stt=STT(), streaming_stt=None)
    result = await runtime.transcribe(PCM)
    assert result.text == "hello"
    assert calls == [PCM]
    await runtime.close()


async def test_terminal_write_treats_every_sample_as_pcm():
    from sleeptalk.transports.terminal import _SpeakerOutput

    samples = b"RIFF" + PCM
    written = []
    output = _SpeakerOutput(SimpleNamespace(write=written.append))
    await output.write(samples, 1)
    assert written == [samples]


def test_file_playback_uses_same_decoder_for_terminal_discord_and_web(tmp_path):
    pytest.importorskip("discord")
    from sleeptalk.discord_voice.route_audio import _discord_stream_pcm
    from sleeptalk.transports.terminal import _wav_mono_pcm
    from sleeptalk.web_voice.playback import _file_frames

    path = tmp_path / "clip.wav"
    path.write_bytes(pcm_to_wav(PCM))
    mono = _wav_mono_pcm(path)
    assert mono == PCM
    assert b"".join(_file_frames(path.read_bytes())) == _discord_stream_pcm(mono)
