import asyncio
import io
import json
import wave
from contextlib import asynccontextmanager

import pytest
from aiohttp import web

from sleeptalk.builder import tts_model_for_provider
from sleeptalk.metering import UsageMeter
from sleeptalk.provider_factory import build_tts
from sleeptalk.providers import ProviderSpec, collect_tts_audio
from sleeptalk.speech.common import SpeechError
from sleeptalk.speech.deepgram_tts import DeepgramTTS


@asynccontextmanager
async def deepgram_server(*, flux=None, aura=None):
    app = web.Application()
    if flux is not None:
        app.router.add_get("/v2/speak", flux)
    if aura is not None:
        app.router.add_post("/v1/speak", aura)
    runner = web.AppRunner(app)
    await runner.setup()
    try:
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        yield f"http://127.0.0.1:{runner.addresses[0][1]}"
    finally:
        await runner.cleanup()


def provider(base_url, *, model="flux-hannah-en", voice_id="", speed=1.0):
    return DeepgramTTS(
        ProviderSpec(
            provider="deepgram", model=model, voice_id=voice_id, speed=speed,
            options={"base_url": base_url},
        ),
        api_key="key",
    )


async def read_client_messages(ws, until):
    """Collect client JSON until a message of type `until` arrives."""
    seen = []
    async for message in ws:
        seen.append(json.loads(message.data))
        if seen[-1]["type"] == until:
            return seen
    return seen


@pytest.mark.asyncio
async def test_flux_streams_turns_over_one_socket() -> None:
    release = asyncio.Event()
    connections = []
    turns = []
    # One frame of 7102 B (Flux's first-frame size) then 7680 B: neither is 20 ms aligned.
    frames = [b"\x01\x00" * 3551, b"\x02\x00" * 3840]

    async def flux(request):
        connections.append((dict(request.query), request.headers.get("Authorization")))
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json({"type": "Connected", "request_id": "r1"})
        while True:
            messages = await read_client_messages(ws, "Flush")
            if not messages:
                break
            turns.append(messages)
            await ws.send_json({"type": "SpeechStarted", "speech_id": "dg_sp_1"})
            await ws.send_bytes(frames[0])
            if len(turns) == 1:
                await release.wait()
            await ws.send_json({"type": "Warning", "code": "INPUT_MARKUP_STRIPPED", "description": "x"})
            await ws.send_bytes(frames[1])
            await ws.send_json({"type": "Flushed", "speech_id": "dg_sp_1"})
            await ws.send_json({"type": "SpeechMetadata", "speech_id": "dg_sp_1", "audio_duration_ms": 160})
        return ws

    async with deepgram_server(flux=flux) as base_url:
        tts = provider(base_url, speed=0.5)
        try:
            await tts.warmup()
            assert len(connections) == 1  # warmup opened the socket
            stream = tts.stream_pcm("Hello.")
            first = await asyncio.wait_for(anext(stream), 1)
            assert first and not release.is_set()
            release.set()
            chunks = [first] + [chunk async for chunk in stream]
            assert b"".join(chunks) == b"".join(frames)
            assert all(len(chunk) % 2 == 0 and len(chunk) <= 1_920 for chunk in chunks)
            audio = await asyncio.wait_for(collect_tts_audio(tts, "Hello again."), 1)
            with wave.open(io.BytesIO(audio), "rb") as wav:
                assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) == (48_000, 1, 2)
                assert wav.readframes(wav.getnframes()) == b"".join(frames)
            assert len(connections) == 1  # both turns rode the warm socket
            query, auth = connections[0]
            assert query == {"model": "flux-hannah-en", "encoding": "linear16", "sample_rate": "48000"}
            assert auth == "Token key"
            assert turns[0] == [
                {"type": "Configure", "speed": 0.7},
                {"type": "Speak", "text": "Hello."},
                {"type": "Flush"},
            ]
            assert turns[1] == [{"type": "Speak", "text": "Hello again."}, {"type": "Flush"}]
        finally:
            release.set()
            await tts.close()
            await tts.close()
        with pytest.raises(SpeechError, match="closed"):
            await anext(tts.stream_pcm("After close."))


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_pending", [False, True])
async def test_flux_cancel_interrupts_and_next_turn_reconnects(tmp_path, cancel_pending) -> None:
    release = asyncio.Event()
    connections = 0
    client_messages = []
    pcm = b"\x01\x00" * 960

    async def flux(request):
        nonlocal connections
        connections += 1
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json({"type": "Connected"})
        while True:
            messages = await read_client_messages(ws, "Flush")
            client_messages.extend(messages)
            if not messages:
                break
            await ws.send_bytes(pcm)
            if connections == 1:
                # Hold the turn open until the client gives up on it.
                client_messages.extend(await read_client_messages(ws, "Interrupt"))
                await release.wait()
                break
            await ws.send_json({"type": "SpeechMetadata"})
        return ws

    async with deepgram_server(flux=flux) as base_url:
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
            await asyncio.sleep(0.05)
            # The replacement socket opened in the background, before the next sentence.
            assert connections == 2 and tts._ws is not None and not tts._ws.closed
            next_audio = await asyncio.wait_for(tts.synthesize_wav_bytes("Next."), 1)
            assert next_audio.startswith(b"RIFF")
            assert connections == 2
            assert {"type": "Interrupt"} in client_messages
            events = [json.loads(line) for line in log_path.read_text().splitlines()]
            finished = [event for event in events if event["stage"] == "attempt_finished"]
            assert [event["status"] for event in finished] == ["cancelled", "ok"]
            assert [event["units"]["outputAudioBytes"] for event in finished] == [len(pcm), len(pcm)]
        finally:
            release.set()
            await tts.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("server_events", "message"),
    [
        ([{"type": "Error", "code": "SPEAK-0001", "description": "bad model"}], "SPEAK-0001"),
        ([{"type": "SpeechMetadata"}], "no audio"),
        ([], "closed mid-turn"),
    ],
)
async def test_flux_server_failures_settle_the_meter(tmp_path, server_events, message) -> None:
    async def flux(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await read_client_messages(ws, "Flush")
        for event in server_events:
            await ws.send_json(event)
        await ws.close()
        return ws

    async with deepgram_server(flux=flux) as base_url:
        tts = provider(base_url)
        log_path = tmp_path / "usage.jsonl"
        tts.set_usage_meter(UsageMeter(log_path=log_path))
        try:
            with pytest.raises(SpeechError, match=message):
                await asyncio.wait_for(tts.synthesize_wav_bytes("Hello."), 2)
            events = [json.loads(line) for line in log_path.read_text().splitlines()]
            assert events[-1]["status"] == "error"
        finally:
            await tts.close()


@pytest.mark.asyncio
async def test_flux_replaces_a_socket_the_server_dropped() -> None:
    connections = 0
    pcm = b"\x01\x00" * 960

    async def flux(request):
        nonlocal connections
        connections += 1
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        if connections == 1:
            await ws.close()  # e.g. the 60 s idle timeout fired between sentences
            return ws
        await read_client_messages(ws, "Flush")
        await ws.send_bytes(pcm)
        await ws.send_json({"type": "SpeechMetadata"})
        return ws

    async with deepgram_server(flux=flux) as base_url:
        tts = provider(base_url)
        try:
            await tts.warmup()
            await asyncio.sleep(0.05)  # let the server's close reach the client
            assert await asyncio.wait_for(tts.synthesize_wav_bytes("Hello."), 2)
            assert connections == 2
        finally:
            await tts.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(("speed", "expected_speed"), [(1.0, None), (0.5, "0.7"), (2.0, "1.5")])
async def test_aura_streams_chunked_rest(speed, expected_speed) -> None:
    requests = []
    pcm = b"\x01\x00" * 2_401

    async def aura(request):
        requests.append((dict(request.query), await request.json(), request.headers.get("Authorization")))
        response = web.StreamResponse()
        await response.prepare(request)
        await response.write(pcm[:1_921])
        await response.write(pcm[1_921:])
        await response.write_eof()
        return response

    async with deepgram_server(aura=aura) as base_url:
        tts = provider(base_url, model="aura-2-thalia-en", speed=speed)
        try:
            chunks = [chunk async for chunk in tts.stream_pcm("Hello.")]
            assert b"".join(chunks) == pcm
            assert all(len(chunk) % 2 == 0 and len(chunk) <= 1_920 for chunk in chunks)
            query, body, auth = requests[0]
            expected = {"model": "aura-2-thalia-en", "encoding": "linear16", "sample_rate": "48000", "container": "none"}
            if expected_speed is not None:
                expected["speed"] = expected_speed
            assert query == expected
            assert body == {"text": "Hello."}
            assert auth == "Token key"
        finally:
            await tts.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "body", "message"),
    [(200, b"\x00\x00\x01", "truncated"), (200, b"", "no audio"), (401, b"Bad key", "401")],
)
async def test_aura_invalid_response_is_not_retried(tmp_path, status, body, message):
    request_count = 0

    async def aura(request):
        nonlocal request_count
        await request.read()
        request_count += 1
        return web.Response(status=status, body=body)

    async with deepgram_server(aura=aura) as base_url:
        tts = provider(base_url, model="aura-2-thalia-en")
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
    ("api_key", "model", "text", "message"),
    [("", "flux-hannah-en", "Hi.", "DEEPGRAM_API_KEY"), ("key", "", "Hi.", "model"),
     ("key", "flux-hannah-en", "...", "punctuation-only")],
)
async def test_validation_precedes_provider_request(api_key, model, text, message):
    tts = DeepgramTTS(ProviderSpec(provider="deepgram", model=model), api_key=api_key)
    with pytest.raises(SpeechError, match=message):
        await anext(tts.stream_pcm(text))
    assert tts._http is None


def test_registration_defaults_and_voice_as_model() -> None:
    assert tts_model_for_provider("deepgram", "") == "flux-hannah-en"
    assert tts_model_for_provider("deepgram", "aura-2-thalia-en") == "aura-2-thalia-en"
    assert tts_model_for_provider("deepgram", "sonic-3") == "flux-hannah-en"
    tts = build_tts(
        ProviderSpec(provider="deepgram", voice_id="flux-kit-en"),
        {"DEEPGRAM_API_KEY": "key"},
    )
    assert isinstance(tts, DeepgramTTS)
    assert tts.model == "flux-kit-en"
    assert tts.spec.voice_id == ""
    assert tts.capabilities.supports_streaming and tts.capabilities.supports_interruption
