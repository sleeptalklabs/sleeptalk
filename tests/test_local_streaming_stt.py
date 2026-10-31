from __future__ import annotations

import asyncio
import hashlib
import io
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from sleeptalk.providers import ProviderSpec
from sleeptalk.speech import local_models as models
from sleeptalk.speech.local_streaming_stt import LocalStreamingSTT, MODEL_ID, SOURCE
from sleeptalk.voice.configs import AdaptiveEndpointingConfig, TurnCompletionSignal
from sleeptalk.voice_loop.endpointing import decide_endpoint


class FakeStream:
    def __init__(self, owner):
        self.owner = owner
        self.listener = None
        self.closed = False
        self.samples = 0
    def add_listener(self, callback):
        self.listener = callback
    def start(self):
        pass
    def add_audio(self, samples, sample_rate):
        self.owner.entered.set()
        if self.owner.block:
            self.owner.release.wait(3)
        assert not self.closed
        self.samples += len(samples)
        self.listener(SimpleNamespace(line=SimpleNamespace(line_id=1, text='live words',
                                    is_complete=False, duration=self.samples/sample_rate)))
    def stop(self):
        self.listener(SimpleNamespace(line=SimpleNamespace(line_id=1, text='live words and final tail',
                                    is_complete=True, duration=self.samples/48000)))
    def close(self):
        self.closed = True


class FakeTranscriber:
    def __init__(self, block=False):
        self.streams = []
        self.batches = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self.block = block
        self.closed = False
    def create_stream(self, **_):
        stream = FakeStream(self)
        self.streams.append(stream)
        return stream
    def transcribe_without_streaming(self, samples, sample_rate):
        self.batches.append((len(samples), sample_rate))
        return SimpleNamespace(lines=[SimpleNamespace(text='batch recovery text')])
    def close(self):
        self.closed = True


def adapter(fake):
    return LocalStreamingSTT(ProviderSpec(provider='local', model=MODEL_ID), transcriber_factory=lambda: fake)


async def eventually(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.005)


async def test_partial_precedes_finish_and_final_tail_is_preserved():
    fake = FakeTranscriber()
    stt = adapter(fake)
    try:
        await stt.append_pcm(0, bytes(1920))
        await eventually(lambda: stt.turn_completion_signal(0).transcript)
        assert stt.turn_completion_signal(0).transcript == 'live words'
        assert not stt.turn_completion_signal(0).is_final
        assert await stt.finish_transcription(0) == 'live words and final tail'
        assert not stt.turn_completion_signal(0).transcript
        assert fake.streams[0].closed
        assert stt.last_turn_stats(0)['transcript_audio_ms'] == 20
        await stt.append_pcm(0, bytes(1920))
        assert not stt.turn_completion_signal(0).transcript
        assert await stt.finish_transcription(0) == 'live words and final tail'
        assert len(fake.streams) == 2
    finally:
        await stt.close()
    assert fake.closed


async def test_ingress_does_not_wait_for_decode_and_reset_fences_late_native_result():
    fake = FakeTranscriber(block=True)
    stt = adapter(fake)
    try:
        await asyncio.wait_for(stt.append_pcm(0, bytes(1920)), 0.1)
        await eventually(fake.entered.is_set)
        reset = asyncio.create_task(stt.reset())
        await asyncio.sleep(0)
        assert not stt.turn_completion_signal(0).transcript
        fake.release.set()
        await reset
        assert not stt.turn_completion_signal(0).transcript
        assert all(s.closed for s in fake.streams)
    finally:
        fake.release.set()
        await stt.close()


async def test_backlog_is_bounded_and_can_be_discarded_for_recovery():
    fake = FakeTranscriber(block=True)
    stt = adapter(fake)
    try:
        await stt.append_pcm(0, bytes(96000))
        await eventually(fake.entered.is_set)
        with pytest.raises(Exception, match='fell behind'):
            await stt.append_pcm(0, bytes(3*96000))
        fake.release.set()
        await stt.discard_transcription(0)
        assert not stt._turns
        assert await stt.transcribe_pcm(bytes(1920)) == 'batch recovery text'
    finally:
        fake.release.set()
        await stt.close()


async def test_recovery_reads_a_long_turn_in_one_pass_not_a_live_replay():
    """A minute-long turn is recovered by one batch read. Replaying it through
    a live stream decoded the same audio every 200 ms, which could not finish
    inside the route's batch budget: the caller's turn was dropped whole
    although every word of it had streamed past as a caption."""
    fake = FakeTranscriber()
    stt = adapter(fake)
    try:
        assert await stt.transcribe_pcm(bytes(60 * 96_000)) == 'batch recovery text'
        assert fake.batches == [(60 * 48_000, 48_000)]
        assert fake.streams == []  # nothing that could overwrite a live partial
    finally:
        await stt.close()


async def test_empty_batch_does_not_load_and_closed_adapter_cannot_reopen():
    fake = FakeTranscriber()
    stt = adapter(fake)
    assert await stt.transcribe_pcm(b'') == ''
    assert not fake.streams
    await stt.close()
    await stt.close()
    with pytest.raises(Exception, match='closed'):
        await stt.append_pcm(0, bytes(20))


@pytest.mark.parametrize('text,final,local,source,silence,expected', [
    ('Tell me more', True, True, SOURCE, 600, True),
    ('Tell me more', True, False, SOURCE, 600, False),
    ('Tell me more', False, True, SOURCE, 600, False),
    ('Tell me more', True, True, 'deepgram', 600, False),
    ('I was going to', True, True, SOURCE, 600, False),
    ('I was going to', True, True, SOURCE, 1200, True),
    ('and also', True, True, SOURCE, 1200, False),
])
def test_local_segment_completion_shortens_only_opted_in_pauses(text, final, local, source, silence, expected):
    decision = decide_endpoint(signal=TurnCompletionSignal(transcript=text,is_final=final,source=source),
        speech_duration_ms=2000, silence_ms=silence, base_silence_ms=120,
        config=AdaptiveEndpointingConfig(local_streaming_input=local))
    assert decision.should_endpoint is expected


def test_streaming_model_files_are_pinned_and_failed_download_keeps_existing_model(tmp_path):
    data=b'model content'
    model=replace(models.get_model(MODEL_ID),files=(('encoder.ort',hashlib.sha256(data).hexdigest(),len(data)),))
    target=models.model_path(model,tmp_path)
    target.mkdir();(target/'old').write_text('keep')
    with pytest.raises(models.LocalModelError,match='checksum'):
        models.ensure_model(model,root=tmp_path,opener=lambda _:io.BytesIO(b'corrupt'))
    assert (target/'old').read_text()=='keep'
    models.ensure_model(model,root=tmp_path,opener=lambda _:io.BytesIO(data))
    assert models.is_ready(model,tmp_path)
    assert (target/'encoder.ort').read_bytes()==data


def test_registry_keeps_batch_baseline_and_reuses_streaming_model_for_local_recovery():
    from sleeptalk.defaults import _build_local_stt
    from sleeptalk.speech.local_stt import LocalSTT
    batch=_build_local_stt(ProviderSpec(provider='local',model='moonshine-tiny-en'),{})
    assert isinstance(batch.rest,LocalSTT) and batch.streaming is None
    streaming=_build_local_stt(ProviderSpec(provider='local',model=MODEL_ID),{})
    assert isinstance(streaming.streaming,LocalStreamingSTT)
    assert streaming.rest is streaming.streaming
    streaming.streaming._executor.shutdown()


def test_settings_reports_streaming_runtime_separately(monkeypatch):
    from sleeptalk import sysdeps
    from sleeptalk.settings_page import local_speech_status
    monkeypatch.setattr(sysdeps,'missing_local_extra',lambda:None)
    monkeypatch.setattr(sysdeps,'missing_streaming_extra',lambda:'missing')
    assert local_speech_status(stt_model='moonshine-tiny-en')['runtime']
    status=local_speech_status(stt_model=MODEL_ID)
    assert not status['runtime']
    assert 'moonshine-voice==0.1.5' in status['install_command']


@pytest.mark.parametrize("model", [MODEL_ID, "", "nova-3"])
async def test_browser_renders_revisions_before_committing_a_turn(monkeypatch, model):
    from tests.runtime.test_web_call import FakeKernelSession, FakeStream
    from sleeptalk.web_voice.session import WebCallSession
    from sleeptalk.session_events import transcript, Speaker
    monkeypatch.setattr('sleeptalk.web_voice.session.log_latency_event', lambda **_:None)
    kernel=FakeKernelSession()
    kernel.session_id="streaming-test"
    kernel.config=SimpleNamespace(stt=SimpleNamespace(provider='local',model=model))
    signal=TurnCompletionSignal(transcript='I want',source=SOURCE)
    kernel.turn_completion_signal=lambda _:signal
    kernel.emit=lambda event:[callback(event) for callback in kernel.subscribers]
    web=WebCallSession(session=kernel,stream=FakeStream(),call_sid='streaming')
    try:
        assert web.assembler.endpointing_config.local_streaming_input
        web._sync_streaming_partial()
        assert web._presentation.partial=='I want'
        signal=TurnCompletionSignal(transcript='I want streaming',source=SOURCE)
        web._sync_streaming_partial()
        assert web._presentation.partial=='I want streaming'
        assert not kernel.turns
        kernel.emit(transcript('streaming-test',1,speaker=Speaker.USER,text='I want streaming',final=True))
        assert not web._presentation.partial
        web._sync_streaming_partial()
        assert web._presentation.partial=='I want streaming'  # repeated words in a new capture render again
    finally:
        await web.close()


async def test_streaming_endpoint_clock_excludes_silero_hangover():
    from tests.test_utterance_gate_hold import Clock, FRAME
    from sleeptalk.voice_loop.utterance import UtteranceAssembler
    clock=Clock(asyncio.get_running_loop())
    async def receive(*_): pass
    capture=UtteranceAssembler(loop=clock,min_bytes=1,silence_seconds=0.12,
        should_ignore_audio=lambda:False,on_utterance=receive,speech_rms_threshold=100,
        endpointing_config=AdaptiveEndpointingConfig(local_streaming_input=True),
        turn_completion_provider=lambda _:TurnCompletionSignal(transcript='Tell me more',is_final=True,source=SOURCE))
    capture.observe(0,object(),FRAME)
    clock.now+=0.65
    capture.last_audio_at[0]=clock.now  # Silero wind-down, not new voice
    assert capture._endpoint_decision(0).should_endpoint
    capture._cleanup_in_loop()
