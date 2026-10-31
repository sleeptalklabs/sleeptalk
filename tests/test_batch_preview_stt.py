import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sleeptalk.providers import ProviderSpec
from sleeptalk.speech.batch_preview_stt import BatchPreviewSTT, _MAX_BYTES, _MAX_REQUESTS
from sleeptalk.voice_policy_runtime import VoicePolicyRuntime

FRAME = b'\xe8\x03' * 960  # voiced 20 ms, 48 kHz mono
SILENCE = bytes(len(FRAME))


def adapter(provider='openrouter'):
    return BatchPreviewSTT(ProviderSpec(provider=provider), api_key='test')


def feed(stt, count, frame=FRAME, user=1):
    for _ in range(count):
        stt.append_preview(user, frame)


async def settle(stt):
    async with asyncio.timeout(1):
        while stt._preview_tasks:
            await asyncio.sleep(0)


@pytest.mark.parametrize('provider,model', [
    ('groq', 'whisper-large-v3-turbo'), ('together', 'nvidia/parakeet-tdt-0.6b-v3'),
    ('local', 'moonshine-tiny-en'), ('elevenlabs', 'scribe_v1'),
])
async def test_batch_factories_offer_previews_and_keep_final_recognition(provider, model):
    from sleeptalk.provider_factory import build_stt_components
    components = build_stt_components(ProviderSpec(provider=provider, model=model),
        {'GROQ_API_KEY': 'test', 'TOGETHER_API_KEY': 'test', 'ELEVENLABS_API_KEY': 'test'})
    assert components.streaming is None
    stt = components.rest
    stt.transcribe_pcm = AsyncMock(return_value='The final sentence.')
    if isinstance(stt, BatchPreviewSTT):
        stt._transcribe_once = AsyncMock(return_value='Preview text')
    runtime = VoicePolicyRuntime(agent=None, stt=stt,
        config=SimpleNamespace(transport=SimpleNamespace(type='web')))
    try:
        for _ in range(100):
            await runtime.append_audio(FRAME, participant_id=1)
        await settle(stt)
        assert runtime.input_transcript_preview(1)
        assert runtime.turn_completion_signal(1) is None
        result = await runtime.transcribe(FRAME * 150, participant_id=1)
        assert result.text == 'The final sentence.'
        assert stt.transcribe_pcm.call_args.args == (FRAME * 150,)
        assert not runtime.input_transcript_preview(1)
    finally:
        await runtime.close()


async def test_local_cancelled_preview_cannot_overlap_final_native_decode(tmp_path):
    import threading
    from sleeptalk.speech.local_stt import LocalSTT
    from sleeptalk.metering import UsageMeter

    stt = LocalSTT(ProviderSpec(provider='local', model='moonshine-tiny-en'))
    stt.set_usage_meter(UsageMeter(log_path=tmp_path / 'usage.jsonl'))
    stt._ready = AsyncMock(return_value=object())
    entered, release = threading.Event(), threading.Event()
    calls = []
    active = 0

    def decode(_, pcm):
        nonlocal active
        active += 1
        try:
            assert active == 1
            calls.append(pcm)
            if len(calls) == 1:
                entered.set()
                assert release.wait(2)
            return 'Recognized text'
        finally:
            active -= 1

    stt._decode = decode
    final = None
    try:
        feed(stt, 100)
        assert await asyncio.to_thread(entered.wait, 2)
        stt.clear_preview(1)
        final = asyncio.create_task(stt.transcribe_pcm(FRAME * 150))
        await asyncio.sleep(0.02)
        assert len(calls) == 1 and not final.done()
        release.set()
        assert await asyncio.wait_for(final, 2) == 'Recognized text'
        await settle(stt)
        assert not stt.usage_meter.snapshot()['openAttemptIds']
        assert len(calls) == 2 and not stt.input_transcript_preview(1)
    finally:
        release.set()
        if final is not None:
            await asyncio.gather(final, return_exceptions=True)
        await stt.close()


async def test_scribe_batch_preview_cancellation_settles_request_meter(tmp_path):
    from aiohttp import web
    from sleeptalk.speech.elevenlabs_stt import ElevenLabsSTT
    from sleeptalk.metering import UsageMeter
    from tests.test_elevenlabs_stt import scribe_server, spec

    entered, release = asyncio.Event(), asyncio.Event()
    async def handler(request):
        await request.read()
        entered.set()
        await release.wait()
        return web.json_response({'text': 'old words'})

    async with scribe_server(handler) as base_url:
        stt = ElevenLabsSTT(spec(base_url, model='scribe_v1'), api_key='test')
        stt.set_usage_meter(UsageMeter(log_path=tmp_path / 'usage.jsonl'))
        try:
            feed(stt, 100)
            await asyncio.wait_for(entered.wait(), 2)
            stt.clear_preview(1)
            await settle(stt)
            assert not stt.usage_meter.snapshot()['openAttemptIds']
            assert not stt.input_transcript_preview(1)
        finally:
            release.set()
            await stt.close()


@pytest.mark.parametrize('provider', ['openrouter', 'venice', 'groq', 'together'])
async def test_revisions_replace_the_whole_preview_with_no_silence_requests(provider):
    stt = adapter(provider)
    call = AsyncMock(side_effect=['first words', 'first words corrected'])
    stt._transcribe_once = call
    try:
        feed(stt, 500, SILENCE)
        await asyncio.sleep(0)
        call.assert_not_called()
        assert len(stt._previews[1].pcm) <= 300 * 96
        feed(stt, 100)
        await settle(stt)
        assert stt.input_transcript_preview(1) == 'first words'
        assert call.call_args.args[2] == 'live_preview'
        feed(stt, 100)
        await settle(stt)
        assert stt.input_transcript_preview(1) == 'first words corrected'
        assert len(call.call_args_list[1].args[0]) > len(call.call_args_list[0].args[0])
        feed(stt, 500, SILENCE)
        await settle(stt)
        assert call.call_count == 2
    finally:
        await stt.close()


async def test_no_backlog_and_bounded_audio_and_request_budget():
    stt = adapter()
    started, release = asyncio.Event(), asyncio.Event()
    calls = []
    async def slow(pcm, *_):
        calls.append(pcm)
        started.set()
        await release.wait()
        return 'latest'
    stt._transcribe_once = slow
    try:
        feed(stt, 100)
        await started.wait()
        feed(stt, 200)
        assert len(calls) == 1 and len(stt._preview_tasks) == 1
        release.set()
        await settle(stt)
        for _ in range(20):
            feed(stt, 100)
            await settle(stt)
        assert len(calls) == _MAX_REQUESTS
        assert len(stt._previews[1].pcm) <= _MAX_BYTES
        assert max(map(len, calls)) <= _MAX_BYTES
    finally:
        await stt.close()


async def test_late_result_cannot_cross_capture_reset():
    stt = adapter()
    started, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def stubborn(*_):
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()
        return 'old words'
    stt._transcribe_once = stubborn
    try:
        feed(stt, 100)
        await started.wait()
        stt.clear_preview(1)
        await cancelled.wait()
        feed(stt, 1)
        release.set()
        await settle(stt)
        assert stt.input_transcript_preview(1) == ''
    finally:
        release.set()
        await stt.close()


async def test_preview_failure_disables_more_requests_but_final_batch_still_runs():
    stt = adapter()
    stt._transcribe_once = AsyncMock(side_effect=RuntimeError('preview failed'))
    stt.transcribe_pcm = AsyncMock(return_value='Complete final transcript.')
    runtime = VoicePolicyRuntime(agent=None, stt=stt,
        config=SimpleNamespace(transport=SimpleNamespace(type='web'), stt=SimpleNamespace(pause_ahead_ms=300)))
    try:
        for _ in range(100):
            await runtime.append_audio(FRAME, participant_id=1)
        await settle(stt)
        feed(stt, 100)
        await settle(stt)
        assert stt._transcribe_once.call_count == 1
        assert runtime.listens_ahead
        assert runtime.turn_completion_signal(1) is None
        pcm = FRAME * 200
        result = await runtime.transcribe(pcm, participant_id=1)
        assert result.text == 'Complete final transcript.'
        stt.transcribe_pcm.assert_awaited_once_with(pcm)
        assert not stt._previews
    finally:
        await runtime.close()


async def test_successful_preview_never_substitutes_for_final_text_or_endpoint_signal():
    stt = adapter('venice')
    stt._transcribe_once = AsyncMock(return_value='misheard preview')
    stt.transcribe_pcm = AsyncMock(return_value='Correct full sentence.')
    runtime = VoicePolicyRuntime(agent=None, stt=stt,
        config=SimpleNamespace(transport=SimpleNamespace(type='web')))
    try:
        for _ in range(100):
            await runtime.append_audio(FRAME, participant_id=1)
        await settle(stt)
        assert runtime.input_transcript_preview(1) == 'misheard preview'
        assert runtime.turn_completion_signal(1) is None
        result = await runtime.transcribe(FRAME * 100, participant_id=1)
        assert result.text == 'Correct full sentence.'
        assert runtime.input_transcript_preview(1) == ''
        feed(stt, 100)
        await runtime.reset_capture()
        assert not stt._previews
    finally:
        await runtime.close()


@pytest.mark.parametrize('transport', ['null'])
async def test_previews_skip_non_live_transports(transport):
    stt = adapter()
    stt._transcribe_once = AsyncMock()
    runtime = VoicePolicyRuntime(agent=None, stt=stt,
        config=SimpleNamespace(transport=SimpleNamespace(type=transport)))
    try:
        for _ in range(200):
            await runtime.append_audio(FRAME)
        assert not stt._previews
        stt._transcribe_once.assert_not_called()
    finally:
        await runtime.close()


@pytest.mark.parametrize('provider', ['openrouter', 'venice', 'groq', 'together', 'local', 'elevenlabs'])
async def test_batch_preview_reaches_browser_but_not_endpointing(monkeypatch, provider):
    from av import AudioFrame
    from tests.runtime.test_web_call import FakeKernelSession, FakeStream
    from sleeptalk.web_voice.session import WebCallSession
    from sleeptalk.session_events import transcript, Speaker

    monkeypatch.setattr('sleeptalk.web_voice.session.log_latency_event', lambda **_: None)
    kernel = FakeKernelSession()
    kernel.policy.local_responsiveness = True
    kernel.session_id = 'batch-preview-test'
    kernel.config = SimpleNamespace(stt=SimpleNamespace(provider=provider, model='moonshine-tiny-en' if provider == 'local' else 'scribe_v1'))
    current = 'A provisional sentence'
    kernel.input_transcript_preview = lambda _: current
    kernel.emit = lambda event: [callback(event) for callback in kernel.subscribers]
    session = WebCallSession(session=kernel, stream=FakeStream(), call_sid='batch-preview')
    async def feed_browser():
        frame = AudioFrame(format='s16', layout='stereo', samples=960)
        frame.sample_rate = 48000
        frame.planes[0].update(bytes(3840))
        await session.feed_audio_frame(frame)
    try:
        await feed_browser()
        assert session._presentation.partial == current
        assert session.assembler.endpointing_config.fresh_voice_timing
        assert session._turn_completion_signal(session.PARTICIPANT_ID) is None
        assert not kernel.turns
        current = 'A corrected provisional sentence'
        await feed_browser()
        assert session._presentation.partial == current
        kernel.emit(transcript(kernel.session_id, 1, speaker=Speaker.USER, text='Full final words.', final=True))
        assert session._presentation.partial == ''
        current = ''
        await feed_browser()
        assert session._presentation.partial == ''
    finally:
        await session.close()
