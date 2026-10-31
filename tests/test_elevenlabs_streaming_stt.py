import asyncio
import base64
import json
from contextlib import asynccontextmanager

import pytest
from aiohttp import web

from sleeptalk.metering import UsageMeter
from sleeptalk.speech.common import SpeechError
from sleeptalk.speech.elevenlabs_streaming_stt import ElevenLabsStreamingSTT, streaming_url
from sleeptalk.speech.elevenlabs_stt import batch_model
from tests.test_elevenlabs_stt import spec


@asynccontextmanager
async def realtime_server(handler):
    app = web.Application()
    async def connect(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json({"message_type": "session_started"})
        await handler(request, ws)
        return ws
    app.router.add_get('/v1/speech-to-text/realtime', connect)
    runner = web.AppRunner(app)
    await runner.setup()
    try:
        await web.TCPSite(runner, '127.0.0.1', 0).start()
        yield f'http://127.0.0.1:{runner.addresses[0][1]}'
    finally:
        await runner.cleanup()


async def until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.001)


def test_url_and_batch_model_mapping():
    url = streaming_url(spec('https://api.elevenlabs.io', language='en-US'))
    assert url.startswith('wss://api.elevenlabs.io/v1/speech-to-text/realtime?')
    assert 'model_id=scribe_v2_realtime' in url
    assert 'commit_strategy=manual' in url and 'audio_format=pcm_48000' in url
    assert 'language_code=en' in url and 'api_key' not in url
    assert batch_model('scribe_v2_realtime') == 'scribe_v2'
    assert batch_model('scribe_v1') == 'scribe_v1'


async def test_partials_committed_segments_final_tail_and_next_capture(tmp_path):
    connections, audio, commits = [], [], []
    async def handler(request, ws):
        connections.append(dict(request.query))
        assert request.headers['xi-api-key'] == 'key'
        first = True
        async for msg in ws:
            data = msg.json()
            pcm = base64.b64decode(data['audio_base_64'])
            audio.append(pcm)
            assert data['sample_rate'] == 48000
            if data['commit']:
                commits.append(True)
                await ws.send_json({'message_type': 'committed_transcript', 'text': 'revised tail.'})
            elif first:
                first = False
                await ws.send_json({'message_type': 'partial_transcript', 'text': 'wrong'})
                await ws.send_json({'message_type': 'committed_transcript', 'text': 'Stable words'})
                await ws.send_json({'message_type': 'partial_transcript', 'text': 'revisable'})
    async with realtime_server(handler) as base:
        stt = ElevenLabsStreamingSTT(spec(base), api_key='key')
        stt.set_usage_meter(UsageMeter(log_path=tmp_path/'usage.jsonl'))
        events = []
        stt.begin_latency_attempt(1, attempt_id='a1', on_event=lambda *args: events.append(args))
        try:
            pcm = b'\x01\x00' * 4800
            await stt.append_pcm(1, pcm)
            await until(lambda: stt.turn_completion_signal(1).transcript == 'Stable words revisable')
            assert not stt.turn_completion_signal(1).is_final
            assert events[0][0] == 'stt_first_partial'
            assert events[0][1]['provider'] == 'elevenlabs'
            assert await stt.finish_transcription(1) == 'Stable words revised tail.'
            assert stt.turn_completion_signal(1).transcript == ''
            assert len(commits) == 1
            assert b''.join(audio) == pcm + bytes(192000-len(pcm))
            assert max(map(len, audio)) <= 9600
            assert stt.last_turn_stats(1).source_bytes_sent == len(pcm)
            assert stt.last_turn_stats(1).finalize_exit == 'from_finalize'
            await stt.append_pcm(1, bytes(1920))
            assert len(connections) == 2
            assert stt.turn_completion_signal(1).transcript == ''
        finally:
            await stt.close()
    usage = [json.loads(line) for line in (tmp_path/'usage.jsonl').read_text().splitlines()]
    assert any(event.get('status') == 'ok' for event in usage)


@pytest.mark.parametrize('failure', ['error', 'disconnect', 'timeout'])
async def test_failed_finalization_discards_partial_and_closes_capture(failure):
    closed = asyncio.Event()
    async def handler(request, ws):
        try:
            async for msg in ws:
                data = msg.json()
                if data['commit']:
                    if failure == 'error':
                        await ws.send_json({'message_type': 'quota_exceeded', 'error': 'secret key text'})
                    elif failure == 'disconnect':
                        await ws.close()
                else:
                    await ws.send_json({'message_type': 'partial_transcript', 'text': 'unfinished'})
        finally:
            closed.set()
    async with realtime_server(handler) as base:
        stt = ElevenLabsStreamingSTT(spec(base), api_key='key')
        try:
            await stt.append_pcm(1, bytes(9600))
            await until(lambda: stt.turn_completion_signal(1).transcript == 'unfinished')
            with pytest.raises((SpeechError, TimeoutError)) as error:
                await stt.finish_transcription(1, timeout_seconds=0.1)
            assert 'secret' not in str(error.value)
            assert stt.turn_completion_signal(1).transcript == ''
            assert not stt._turns
            await asyncio.wait_for(closed.wait(), 1)
        finally:
            await stt.close()


async def test_cancelling_finalization_and_reset_close_sockets():
    committing = asyncio.Event()
    async def handler(request, ws):
        async for msg in ws:
            if msg.json()['commit']:
                committing.set()
    async with realtime_server(handler) as base:
        stt = ElevenLabsStreamingSTT(spec(base), api_key='key')
        try:
            await stt.append_pcm(1, bytes(9600))
            task = asyncio.create_task(stt.finish_transcription(1))
            await committing.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not stt._turns
            await stt.append_pcm(2, bytes(9600))
            await stt.reset()
            assert not stt._turns
        finally:
            await stt.close()


async def test_missing_key_and_discard_are_safe():
    stt = ElevenLabsStreamingSTT(spec('http://127.0.0.1:1'), api_key='')
    with pytest.raises(SpeechError, match='ELEVENLABS_API_KEY'):
        await stt.append_pcm(1, bytes(9600))
    await stt.discard_transcription(1)
    assert await stt.finish_transcription(1) == ''
    await stt.close()


async def test_runtime_uses_full_utterance_rest_fallback_after_realtime_error():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from sleeptalk.voice_policy_runtime import VoicePolicyRuntime

    async def handler(request, ws):
        async for msg in ws:
            if msg.json()['commit']:
                await ws.send_json({'message_type': 'error', 'error': 'failed'})
    async with realtime_server(handler) as base:
        stt = ElevenLabsStreamingSTT(spec(base), api_key='key')
        rest = SimpleNamespace(transcribe_pcm=AsyncMock(return_value='Recovered full sentence.'))
        runtime = VoicePolicyRuntime(agent=None, stt=rest, streaming_stt=stt)
        pcm = bytes(9600)
        try:
            await runtime.append_audio(pcm, participant_id=1)
            result = await runtime.transcribe(pcm, participant_id=1)
            assert result.text == 'Recovered full sentence.'
            assert result.source == 'rest_stream_error'
            rest.transcribe_pcm.assert_awaited_once_with(pcm)
            assert not stt._turns
        finally:
            await stt.close()
