"""Pocket local selection, callback streaming, and cancelled inference ownership."""
import asyncio
import threading
import wave
from contextlib import aclosing
from types import SimpleNamespace

import numpy as np
import pytest

from sleeptalk.providers import ProviderSpec
from sleeptalk.speech.common import SpeechError
from sleeptalk.speech.local_models import get_model, model_path, READY_MARKER
from sleeptalk.speech.local_tts import LocalTTS, load_synthesizer
from sleeptalk.speech import pocket_tts


def test_local_picker_resolves_pocket_without_changing_the_default():
    from sleeptalk import speech_catalog
    from sleeptalk.builder import tts_spec
    from sleeptalk.config import TTSConfig
    from sleeptalk.registries import TTS_PROVIDERS
    from sleeptalk.speech_speed import wire_speed
    model = speech_catalog.find('local', speech_catalog.TTS, 'pocket-tts-en')
    assert model.usable and model.default_voice == 'pocket-bria'
    assert speech_catalog.recommended('local', speech_catalog.TTS).id == 'kokoro-en'
    for voice in ('pocket-bria', 'pocket-loona'):
        spec = tts_spec(TTSConfig(provider='local', model='pocket-tts-en', voice_id=voice))
        tts = TTS_PROVIDERS.build('local', spec, {})
        assert tts.model.id == 'pocket-tts-en'
        tts.set_voice('af_heart')
        assert tts.model.id == 'kokoro-en'
        tts.set_voice(voice)
        assert tts.model.id == 'pocket-tts-en'
    assert wire_speed('local', 1.5, model='pocket-tts-en') == 1


def test_loader_uses_the_pinned_cpu_models_and_two_threads(tmp_path, monkeypatch):
    import sys
    model = get_model('pocket-tts-en')
    directory = model_path(model, tmp_path)
    directory.mkdir()
    (directory / READY_MARKER).write_text(model.sha256)
    from sleeptalk.speech import pocket_encoder
    monkeypatch.setattr(pocket_encoder, 'encoder_path', lambda d: d / 'encoder-dynamic-v1.onnx')
    constructor = lambda **kw: SimpleNamespace(**kw)
    monkeypatch.setitem(sys.modules, 'sherpa_onnx', SimpleNamespace(
        OfflineTtsPocketModelConfig=constructor, OfflineTtsModelConfig=constructor,
        OfflineTtsConfig=constructor, OfflineTts=lambda c:c))
    result = load_synthesizer(model, tmp_path)
    assert result.model.num_threads == 2 and result.model.provider == 'cpu'
    assert result.model.pocket.lm_main == str(directory / 'lm_main.int8.onnx')
    assert result.model.pocket.decoder == str(directory / 'decoder.int8.onnx')
    assert result.model.pocket.encoder == str(directory / 'encoder-dynamic-v1.onnx')


def test_reference_voice_is_local_and_cannot_be_a_path(tmp_path, monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, 'sherpa_onnx', SimpleNamespace(GenerationConfig=SimpleNamespace))
    folder = tmp_path / 'test_wavs'
    folder.mkdir()
    with wave.open(str(folder / 'bria.wav'), 'wb') as clip:
        clip.setparams((1, 2, 24_000, 0, 'NONE', 'not compressed'))
        clip.writeframes(np.array([-32768, 0, 16384], dtype=np.int16).tobytes())
    config = pocket_tts.generation_config(tmp_path, 'pocket-bria')
    np.testing.assert_array_equal(config.reference_audio, [-1, 0, 0.5])
    assert config.reference_sample_rate == 24_000 and config.num_steps == 5
    with pytest.raises(SpeechError, match='Unknown'):
        pocket_tts.generation_config(tmp_path, '../secret.wav')


class Engine:
    sample_rate = 24_000
    def __init__(self, *, fail=False, block=False):
        self.calls = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()
        self.fail, self.block = fail, block
        self.callback_results = []

    def generate(self, text, config, *, callback):
        self.calls.append((text, config))
        samples = np.full(2400, 0.2, dtype=np.float32)
        self.callback_results.append(callback(samples, 0.5))
        # Native callback memory is invalid after return.
        samples[:] = -1
        if len(self.calls) == 1 and self.block:
            self.entered.set()
            assert self.release.wait(5)
        try:
            if self.fail:
                raise RuntimeError('test inference error')
            self.callback_results.append(callback(np.full(2400, 0.3, dtype=np.float32), 1.0))
        finally:
            self.finished.set()
        # Full result must never be emitted again after callback audio.
        return SimpleNamespace(samples=np.full(4800, -1, dtype=np.float32), sample_rate=self.sample_rate)


@pytest.fixture
def make_tts(monkeypatch):
    monkeypatch.setattr(pocket_tts, 'generation_config', lambda directory, voice: voice)
    def make(engine):
        tts = LocalTTS(ProviderSpec(provider='local', model='pocket-tts-en', voice_id='pocket-bria'))
        tts._tts = engine
        return tts
    return make


async def test_callback_audio_arrives_before_completion_and_is_not_duplicated(make_tts):
    engine = Engine(block=True)
    tts = make_tts(engine)
    try:
        async with aclosing(tts.synthesize('Hello, local world.')) as stream:
            first = await asyncio.wait_for(anext(stream), 2)
            assert not engine.finished.is_set()
            engine.release.set()
            chunks = [first, *[chunk async for chunk in stream]]
        assert all(0 < len(c) <= 1920 and len(c) % 2 == 0 for c in chunks)
        pcm = b''.join(chunks)
        assert len(pcm) == 19_200  # 4800 samples doubled to 48 kHz, int16
        assert np.frombuffer(pcm, dtype=np.int16)[200] > 6000
        assert engine.callback_results == [1, 1]
    finally:
        engine.release.set()
        await tts.close()


@pytest.mark.parametrize('action', ['abort', 'close', 'iterator_close', 'cancel_waiter'])
async def test_cancel_discards_audio_and_new_turn_waits_for_old_native_work(make_tts, action):
    engine = Engine(block=True)
    tts = make_tts(engine)
    stream = tts.synthesize('Old turn.')
    pending = None
    try:
        await anext(stream)
        # Drain first callback so inference reaches its deliberate pause.
        while not engine.entered.is_set():
            await asyncio.sleep(0.001)
        if action == 'iterator_close':
            await stream.aclose()
        elif action == 'cancel_waiter':
            async def consume():
                async for _ in stream:
                    pass
            pending = asyncio.create_task(consume())
            await asyncio.sleep(0.01)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
        else:
            await asyncio.wait_for(getattr(tts, action)(), 1)
            with pytest.raises(asyncio.CancelledError):
                await anext(stream)
        await stream.aclose()
        if action != 'close':
            pending = asyncio.create_task(tts.synthesize_pcm('New turn.'))
            await asyncio.sleep(0.03)
            assert len(engine.calls) == 1
            engine.release.set()
            assert await asyncio.wait_for(pending, 2)
        else:
            engine.release.set()
            with pytest.raises(SpeechError, match='closed'):
                await tts.synthesize_pcm('Closed.')
        assert await asyncio.to_thread(engine.finished.wait, 2)
        assert engine.callback_results[1] == 0
        assert not tts._requests
    finally:
        engine.release.set()
        await stream.aclose()
        if pending is not None:
            await asyncio.gather(pending, return_exceptions=True)
        await tts.close()


async def test_native_failure_surfaces_and_next_turn_can_recover(make_tts):
    engine = Engine(fail=True)
    tts = make_tts(engine)
    try:
        with pytest.raises(SpeechError, match='test inference error'):
            await tts.synthesize_pcm('Fail.')
        engine.fail = False
        assert await tts.synthesize_pcm('Recover.')
        assert not tts._requests
    finally:
        await tts.close()


async def test_empty_native_audio_is_an_error(make_tts):
    tts = make_tts(SimpleNamespace(sample_rate=24_000, generate=lambda *a, **kw: None))
    try:
        with pytest.raises(SpeechError, match='no audio'):
            await tts.synthesize_pcm('Hello.')
    finally:
        await tts.close()


async def test_paused_consumer_bounds_native_output_and_abort_releases_callback(make_tts):
    finished = threading.Event()
    results = []
    def generate(text, config, *, callback):
        results.append(callback(np.full(240_000, 0.2, dtype=np.float32), 1))
        finished.set()
    tts = make_tts(SimpleNamespace(sample_rate=24_000, generate=generate))
    stream = tts.synthesize('Bounded playback.')
    try:
        assert await asyncio.wait_for(anext(stream), 2)
        await asyncio.sleep(0.03)
        assert not finished.is_set()  # producer blocked on bounded playback queue
        await asyncio.wait_for(stream.aclose(), 1)
        assert await asyncio.to_thread(finished.wait, 2)
        assert results == [0]
    finally:
        await stream.aclose()
        await tts.close()
