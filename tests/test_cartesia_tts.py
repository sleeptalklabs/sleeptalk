import asyncio
import io
import json
import wave
from contextlib import asynccontextmanager

import pytest
from aiohttp import web

from sleeptalk.metering import UsageMeter
from sleeptalk.providers import ProviderSpec, collect_tts_audio
from sleeptalk.speech.cartesia import CartesiaTTS
from sleeptalk.speech.common import SpeechError
from sleeptalk.speech_speed import wire_speed


@asynccontextmanager
async def cartesia_server(handler):
    app = web.Application()
    app.router.add_post("/tts/bytes", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    try:
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        address = runner.addresses[0]
        yield f"http://127.0.0.1:{address[1]}"
    finally:
        await runner.cleanup()


def provider(base_url):
    return CartesiaTTS(
        ProviderSpec(
            provider="cartesia", model="sonic-3", voice_id="voice", speed=0.5,
            options={"base_url": base_url},
        ),
        api_key="key",
    )


@pytest.mark.asyncio
async def test_streams_before_eof_and_reuses_connection_for_wav() -> None:
    release = asyncio.Event()
    requests = []
    peers = []
    pcm = b"\x01\x00" * 2_401

    async def handle(request):
        requests.append((await request.json(), dict(request.headers)))
        peers.append(request.transport.get_extra_info("peername"))
        response = web.StreamResponse(headers={"Content-Type": "application/octet-stream"})
        await response.prepare(request)
        await response.write(pcm[:1_921])
        if len(requests) == 1:
            await release.wait()
        await response.write(pcm[1_921:])
        await response.write_eof()
        return response

    async with cartesia_server(handle) as base_url:
        tts = provider(base_url)
        try:
            await tts.warmup()
            assert requests == []
            stream = tts.stream_pcm("Hello.")
            first = await asyncio.wait_for(anext(stream), 1)
            assert first and not release.is_set()
            release.set()
            chunks = [first] + [chunk async for chunk in stream]
            assert b"".join(chunks) == pcm
            assert all(len(chunk) % 2 == 0 and len(chunk) <= 1_920 for chunk in chunks)
            audio = await collect_tts_audio(tts, "Hello again.")
            with wave.open(io.BytesIO(audio), "rb") as wav:
                assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) == (48_000, 1, 2)
                assert wav.readframes(wav.getnframes()) == pcm
            assert peers[0] == peers[1]
            body, headers = requests[0]
            assert body["generation_config"]["speed"] == wire_speed("cartesia", 0.5) == 0.6
            assert "speed" not in body
            assert body["output_format"] == {
                "container": "raw", "encoding": "pcm_s16le", "sample_rate": 48_000,
            }
            assert body["model_id"] == "sonic-3"
            assert body["voice"] == {"id": "voice"}
            assert headers["Authorization"] == "Bearer key"
            assert headers["Cartesia-Version"] == "2026-03-01"
        finally:
            release.set()
            await tts.close()
            await tts.close()
        with pytest.raises(SpeechError, match="closed"):
            await anext(tts.stream_pcm("After close."))


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_pending", [False, True])
async def test_cancel_settles_partial_audio_and_next_request_works(tmp_path, cancel_pending) -> None:
    release = asyncio.Event()
    request_count = 0
    pcm = b"\x01\x00" * 960

    async def handle(request):
        nonlocal request_count
        await request.read()
        request_count += 1
        response = web.StreamResponse()
        await response.prepare(request)
        await response.write(pcm)
        if request_count == 1:
            await release.wait()
        try:
            await response.write_eof()
        except ConnectionResetError:
            pass
        return response

    async with cartesia_server(handle) as base_url:
        tts = provider(base_url)
        log_path = tmp_path / "usage.jsonl"
        tts.set_usage_meter(UsageMeter(log_path=log_path))
        try:
            stream = tts.stream_pcm("First.")
            assert await asyncio.wait_for(anext(stream), 1) == pcm
            if cancel_pending:
                pending = asyncio.create_task(anext(stream))
                await asyncio.sleep(0)
                pending.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await pending
            await stream.aclose()
            next_audio = await asyncio.wait_for(tts.synthesize_wav_bytes("Next."), 1)
            assert next_audio.startswith(b"RIFF")
            assert request_count == 2
            events = [json.loads(line) for line in log_path.read_text().splitlines()]
            finished = [event for event in events if event["stage"] == "attempt_finished"]
            assert [event["status"] for event in finished] == ["cancelled", "ok"]
            assert [event["units"]["outputAudioBytes"] for event in finished] == [len(pcm), len(pcm)]
        finally:
            release.set()
            await tts.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "body", "message"),
    [(200, b"\x00\x00\x01", "truncated"), (200, b"", "no audio"), (401, b"Bad key", "401")],
)
async def test_invalid_response_is_not_retried_and_meter_is_settled(tmp_path, status, body, message):
    request_count = 0

    async def handle(request):
        nonlocal request_count
        await request.read()
        request_count += 1
        return web.Response(status=status, body=body)

    async with cartesia_server(handle) as base_url:
        tts = provider(base_url)
        log_path = tmp_path / "usage.jsonl"
        tts.set_usage_meter(UsageMeter(log_path=log_path))
        try:
            with pytest.raises(SpeechError, match=message):
                await tts.synthesize_wav_bytes("Hello.")
            assert request_count == 1
            events = [json.loads(line) for line in log_path.read_text().splitlines()]
            assert events[-1]["status"] == "error"
        finally:
            await tts.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("api_key", "voice_id", "text", "message"),
    [("", "voice", "Hi.", "CARTESIA_API_KEY"), ("key", "", "Hi.", "CARTESIA_VOICE_ID"),
     ("key", "voice", "...", "punctuation-only")],
)
async def test_validation_precedes_provider_request(api_key, voice_id, text, message):
    tts = CartesiaTTS(ProviderSpec(provider="cartesia", voice_id=voice_id), api_key=api_key)
    with pytest.raises(SpeechError, match=message):
        await anext(tts.stream_pcm(text))
    assert tts._http is None


@pytest.mark.asyncio
@pytest.mark.parametrize("language, expected", [("fr-FR", "fr"), ("pt_BR", "pt"), ("", None)])
async def test_language_reaches_sonic_as_a_primary_subtag(language, expected) -> None:
    """Sonic pronounces by language; the payload carries the primary subtag or nothing."""
    bodies = []

    async def handle(request):
        bodies.append(await request.json())
        response = web.StreamResponse(status=200, headers={"Content-Type": "application/octet-stream"})
        await response.prepare(request)
        await response.write(b"\x01\x00" * 480)
        await response.write_eof()
        return response

    async with cartesia_server(handle) as base_url:
        tts = CartesiaTTS(
            ProviderSpec(provider="cartesia", model="sonic-3", voice_id="voice", language=language,
                         options={"base_url": base_url}),
            api_key="key",
        )
        try:
            await collect_tts_audio(tts, "Bonjour.")
        finally:
            await tts.close()
    assert bodies and bodies[0].get("language") == expected
