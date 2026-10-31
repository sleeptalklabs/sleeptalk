import asyncio
import json
from contextlib import asynccontextmanager

import pytest
from aiohttp import web

from sleeptalk.builder import stt_spec
from sleeptalk.config import STTConfig
from sleeptalk.metering import UsageMeter
from sleeptalk.provider_factory import build_stt_components
from sleeptalk.providers import ProviderSpec
from sleeptalk.speech.cartesia_stt import CartesiaSTT, CartesiaStreamingSTT, _streaming_url
from sleeptalk.speech.common import SpeechError


@asynccontextmanager
async def cartesia_server(*, ws=None, batch=None):
    app = web.Application()
    if ws is not None:
        app.router.add_get("/stt/websocket", ws)
    if batch is not None:
        app.router.add_post("/stt", batch)
    runner = web.AppRunner(app)
    await runner.setup()
    try:
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        yield f"http://127.0.0.1:{runner.addresses[0][1]}"
    finally:
        await runner.cleanup()


def spec(base_url, **overrides):
    return ProviderSpec(
        provider="cartesia", model=overrides.pop("model", "ink-2"),
        language=overrides.pop("language", "en-US"), options={"base_url": base_url, **overrides},
    )


def test_streaming_url_shapes_per_model() -> None:
    ink2 = _streaming_url(spec("https://api.cartesia.ai", keyterms=["Sleeptalk", "Hermes"]))
    assert ink2.startswith("wss://api.cartesia.ai/stt/websocket?model=ink-2&encoding=pcm_s16le&sample_rate=48000")
    assert "cartesia_version=2026-08-14" in ink2
    assert "keyterm=Sleeptalk&keyterm=Hermes" in ink2 and "language=" not in ink2
    whisper = _streaming_url(spec("https://api.cartesia.ai", model="ink-whisper", language="es-MX", keyterms=["x"]))
    assert "language=es" in whisper and "keyterm" not in whisper


@pytest.mark.asyncio
async def test_streaming_turn_accumulates_final_deltas_and_flushes(tmp_path) -> None:
    received = []
    connections = []

    async def handler(request):
        connections.append((dict(request.query), request.headers.get("X-API-Key")))
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for message in ws:
            if message.type == web.WSMsgType.BINARY:
                received.append(message.data)
                if len(received) == 1:
                    await ws.send_json({"type": "transcript", "is_final": False, "text": " Sleep talk"})
                    await ws.send_json({"type": "transcript", "is_final": True, "text": " Sleep"})
                    await ws.send_json({"type": "transcript", "is_final": True, "text": " talk check"})
            elif message.data == "finalize":
                await ws.send_json({"type": "transcript", "is_final": True, "text": "."})
                await ws.send_json({"type": "flush_done", "request_id": "r"})
            elif message.data == "close":
                await ws.send_json({"type": "done", "request_id": "r"})
                break
        return ws

    async with cartesia_server(ws=handler) as base_url:
        stt = CartesiaStreamingSTT(spec(base_url), api_key="key")
        stt.set_usage_meter(UsageMeter(log_path=tmp_path / "usage.jsonl"))
        events = []
        try:
            stt.begin_latency_attempt(7, attempt_id="a1", on_event=lambda name, data: events.append((name, data)))
            await stt.append_pcm(7, b"\x00" * 1920)
            await asyncio.sleep(0.05)
            signal = stt.turn_completion_signal(7)
            assert signal.transcript == "Sleep talk check" and signal.is_final is False and signal.source == "cartesia"
            transcript = await stt.finish_transcription(7, timeout_seconds=1.0)
            assert transcript == "Sleep talk check."
            stats = stt.last_turn_stats(7)
            assert stats.finalize_exit == "from_finalize" and stats.sent_audio_ms == 20
            assert events[0][0] == "stt_first_partial" and events[0][1]["provider"] == "cartesia"
            # The next utterance rides the same socket and does not inherit old text.
            await stt.append_pcm(7, b"\x00" * 1920)
            assert stt.turn_completion_signal(7).transcript == ""
            assert len(connections) == 1
            query, key = connections[0]
            assert key == "key" and query["model"] == "ink-2" and query["sample_rate"] == "48000"
        finally:
            await stt.close()
        finished = [json.loads(line) for line in (tmp_path / "usage.jsonl").read_text().splitlines()]
        assert [e["status"] for e in finished if e["stage"] == "attempt_finished"] == ["ok", "session_closed"]


@pytest.mark.asyncio
async def test_batch_rest_transcribes_wav(tmp_path) -> None:
    requests = []

    async def batch(request):
        reader = await request.multipart()
        fields = {}
        async for part in reader:
            fields[part.name] = (part.filename, await part.read())
        requests.append((fields, request.headers.get("X-API-Key"), request.headers.get("Cartesia-Version")))
        return web.json_response({"type": "transcript", "text": " Sleep Talk Check. ", "language": "en"})

    async with cartesia_server(batch=batch) as base_url:
        stt = CartesiaSTT(spec(base_url, language="es-ES"), api_key="key")
        stt.set_usage_meter(UsageMeter(log_path=tmp_path / "usage.jsonl"))
        assert await stt.transcribe_pcm(b"\x01\x00" * 4800) == "Sleep Talk Check."
        fields, key, version = requests[0]
        assert key == "key" and version == "2026-08-14"
        assert fields["model"][1] == b"ink-whisper"  # batch only accepts ink-whisper
        assert fields["language"][1] == b"es"
        assert fields["file"][1].startswith(b"RIFF")
        assert await stt.transcribe_pcm(b"") == ""


@pytest.mark.asyncio
async def test_batch_rest_error_settles_meter(tmp_path) -> None:
    async def batch(request):
        await request.read()
        return web.Response(status=401, text="bad key")

    async with cartesia_server(batch=batch) as base_url:
        stt = CartesiaSTT(spec(base_url), api_key="key")
        stt.set_usage_meter(UsageMeter(log_path=tmp_path / "usage.jsonl"))
        with pytest.raises(SpeechError, match="401"):
            await stt.transcribe_pcm(b"\x01\x00" * 48)
        assert json.loads((tmp_path / "usage.jsonl").read_text().splitlines()[-1])["status"] == "error"


@pytest.mark.asyncio
async def test_missing_key_is_reported_before_any_request() -> None:
    with pytest.raises(SpeechError, match="CARTESIA_API_KEY"):
        await CartesiaSTT(spec("http://127.0.0.1:9"), api_key="").transcribe_pcm(b"\x01\x00")
    stt = CartesiaStreamingSTT(spec("http://127.0.0.1:9"), api_key="")
    with pytest.raises(SpeechError, match="CARTESIA_API_KEY"):
        await stt.append_pcm(1, b"\x01\x00")
    await stt.close()


def test_registration_builds_both_components() -> None:
    components = build_stt_components(stt_spec(STTConfig(provider="cartesia")), {"CARTESIA_API_KEY": "key"})
    assert isinstance(components.rest, CartesiaSTT)
    assert isinstance(components.streaming, CartesiaStreamingSTT)
    # STTConfig's default model is Deepgram's; the registry maps it to Cartesia's launch model.
    assert components.streaming.spec.model == "ink-2"
    assert stt_spec(STTConfig(provider="cartesia", model="ink-whisper")).model == "ink-whisper"
