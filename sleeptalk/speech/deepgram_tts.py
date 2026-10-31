"""Deepgram speech with streamed 48 kHz mono PCM: Flux over websocket, Aura over REST.

The model *is* the voice (``flux-haley-en``, ``aura-2-thalia-en``). Flux is
turn-based, which matches the cascade's sentence-at-a-time synthesis: one
socket stays open across sentences and each ``stream_pcm`` call is one turn
(Speak + Flush, audio until SpeechMetadata). Aura models take the chunked
``/v1/speak`` REST path, the same shape as Cartesia.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import aclosing
from pathlib import Path
from urllib.parse import urlencode

import aiohttp

from sleeptalk.metering import UsageMeter
from sleeptalk.providers import (
    ProviderCapabilities,
    ProviderSpec,
    provider_http_url,
    provider_websocket_url,
)
from sleeptalk.speech.common import (
    SpeechError,
    _has_speakable_content,
    _http_error_message,
    _pcm_to_wav,
)
from sleeptalk.speech.lexicon import apply_pronunciation
from sleeptalk.speech.temp_audio import write_tts_temp_file
from sleeptalk.speech_speed import wire_speed

logger = logging.getLogger(__name__)

_SAMPLE_RATE = 48_000
_CHUNK_BYTES = 1_920  # 20 ms of mono signed 16-bit PCM.
_DEFAULT_BASE = "https://api.deepgram.com"
# Flux closes idle sockets after 60 s; pings well inside that keep it warm.
_HEARTBEAT_SECONDS = 20
_TERMINAL_EVENTS = frozenset({"SpeechMetadata", "SpeechInterrupted"})


def is_flux_model(model: str) -> bool:
    return model.strip().lower().startswith("flux-")


class _Rechunker:
    """Cut arbitrary PCM frames into sample-aligned pieces of at most 20 ms."""

    def __init__(self) -> None:
        self.tail = b""

    def push(self, data: bytes) -> list[bytes]:
        data = self.tail + data
        aligned = len(data) - len(data) % 2
        self.tail = data[aligned:]
        data = data[:aligned]
        return [data[index : index + _CHUNK_BYTES] for index in range(0, len(data), _CHUNK_BYTES)]


class DeepgramTTS:
    capabilities = ProviderCapabilities(
        supports_streaming=True,
        supports_word_timings=False,
        supports_interruption=True,
    )

    def __init__(self, spec: ProviderSpec, *, api_key: str) -> None:
        # A picked voice is a model name here; it wins over the configured model.
        if spec.voice_id.strip():
            from dataclasses import replace

            spec = replace(spec, model=spec.voice_id.strip(), voice_id="")
        self.spec = spec
        self.api_key = api_key
        self.usage_meter: UsageMeter | None = None
        self._http: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._connecting: asyncio.Task | None = None
        self._turn_lock = asyncio.Lock()
        self._background: set[asyncio.Task] = set()
        self._closed = False

    def set_usage_meter(self, meter: UsageMeter) -> None:
        self.usage_meter = meter

    def set_voice(self, voice_id: str) -> None:
        """Switch voices live. Deepgram's model is the voice, and a Flux socket is
        bound to one model, so the current socket is dropped; the next
        sentence reconnects with the new one."""
        from dataclasses import replace

        self.spec = replace(self.spec, model=voice_id.strip(), voice_id="")
        self._detach_socket()

    @property
    def model(self) -> str:
        return self.spec.model.strip()

    def _session(self) -> aiohttp.ClientSession:
        if self._closed:
            raise SpeechError("Deepgram TTS is closed.")
        if self._http is None:
            self._http = aiohttp.ClientSession()
        return self._http

    async def warmup(self) -> None:
        self._session()
        if is_flux_model(self.model) and self.api_key.strip():
            # Opening the socket here moves ~100 ms of connect off the first sentence.
            try:
                await self._socket()
            except (aiohttp.ClientError, OSError) as exc:
                logger.warning("Deepgram Flux warmup could not connect: %s", exc)

    async def close(self) -> None:
        self._closed = True
        if self._connecting is not None and not self._connecting.done():
            self._connecting.cancel()
        self._detach_socket()
        if self._background:
            await asyncio.gather(*self._background, return_exceptions=True)
        if self._http is not None:
            await self._http.close()

    async def synthesize_wav_bytes(self, text: str) -> bytes:
        async with aclosing(self.stream_pcm(text)) as stream:
            pcm = b"".join([chunk async for chunk in stream])
        return _pcm_to_wav(pcm, channels=1, sample_width=2, sample_rate=_SAMPLE_RATE)

    async def synthesize_wav(self, text: str) -> Path:
        return write_tts_temp_file(await self.synthesize_wav_bytes(text), prefix="deepgram")

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        async with aclosing(self.stream_pcm(text)) as stream:
            async for pcm in stream:
                yield pcm

    async def stream_pcm(self, text: str) -> AsyncIterator[bytes]:
        """Yield sample-aligned 48 kHz mono PCM, at most 20 ms per chunk.

        Closing the iterator early interrupts that turn. For Flux the socket is
        dropped rather than drained, so the sentence after a barge-in pays one
        reconnect (~100 ms) instead of racing stale frames.
        """
        if not self.api_key.strip():
            raise SpeechError("Missing DEEPGRAM_API_KEY.")
        if not self.model:
            raise SpeechError("Missing Deepgram TTS model (the voice, e.g. flux-haley-en).")
        if not _has_speakable_content(text):
            raise SpeechError("Deepgram TTS transcript was empty or punctuation-only.")
        self._session()

        attempt_id = f"{uuid.uuid4().hex[:12]}:tts:stream:1"
        if self.usage_meter is not None:
            self.usage_meter.start_attempt(
                provider="deepgram",
                operation="tts.stream",
                attempt_id=attempt_id,
                inputCharacters=len(text),
            )
        status = "cancelled"
        transcript = apply_pronunciation(text, getattr(self.spec, "language", "") or "")
        stream = self._flux_turn(transcript, attempt_id) if is_flux_model(self.model) else self._aura_request(transcript, attempt_id)
        try:
            async with aclosing(stream) as pcm_stream:
                async for pcm in pcm_stream:
                    yield pcm
            status = "ok"
        except Exception:
            status = "error"
            raise
        finally:
            if self.usage_meter is not None:
                self.usage_meter.finish_attempt(attempt_id, status=status)

    # ---- Flux: one websocket, one turn per sentence ----------------------

    def _flux_url(self) -> str:
        params = urlencode(
            [("model", self.model), ("encoding", "linear16"), ("sample_rate", str(_SAMPLE_RATE))]
        )
        return f"{provider_websocket_url(self.spec, _DEFAULT_BASE, '/v2/speak')}?{params}"

    async def _socket(self) -> aiohttp.ClientWebSocketResponse:
        if self._ws is not None and not self._ws.closed:
            return self._ws
        # One connect at a time; a waiter cancelled mid-connect leaves it running
        # so the socket still lands for the next sentence.
        if self._connecting is None or self._connecting.done():
            self._connecting = asyncio.ensure_future(self._connect())
        return await asyncio.shield(self._connecting)

    async def _connect(self) -> aiohttp.ClientWebSocketResponse:
        ws = await self._session().ws_connect(
            self._flux_url(),
            headers={"Authorization": f"Token {self.api_key}"},
            heartbeat=_HEARTBEAT_SECONDS,
        )
        speed = wire_speed("deepgram", self.spec.speed, model=self.spec.model)
        if speed != 1.0:
            await ws.send_json({"type": "Configure", "speed": speed})
        self._ws = ws
        return ws

    def _spawn(self, coroutine) -> None:
        task = asyncio.ensure_future(coroutine)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    def _detach_socket(self) -> None:
        """Forget the socket now; close it in the background so the next turn never waits."""
        ws, self._ws = self._ws, None
        if ws is None or ws.closed:
            return
        self._spawn(ws.close())

    async def _prewarm(self) -> None:
        """Reconnect while the user is still talking so the reply after a barge-in starts warm."""
        try:
            await self._socket()
        except (aiohttp.ClientError, OSError, asyncio.CancelledError, SpeechError) as exc:
            logger.debug("Deepgram Flux prewarm skipped: %s", exc)

    async def _flux_turn(self, text: str, attempt_id: str) -> AsyncIterator[bytes]:
        async with self._turn_lock:
            complete = False
            ws: aiohttp.ClientWebSocketResponse | None = None
            try:
                while True:
                    ws, reused = await self._open_turn(text)
                    rechunk = _Rechunker()
                    received = False
                    ended = False
                    async for message in ws:
                        if message.type == aiohttp.WSMsgType.BINARY:
                            for chunk in rechunk.push(message.data):
                                if self.usage_meter is not None:
                                    self.usage_meter.add_usage(attempt_id, outputAudioBytes=len(chunk))
                                received = True
                                yield chunk
                            continue
                        if message.type != aiohttp.WSMsgType.TEXT:
                            break
                        event = _parse_event(message.data)
                        kind = event.get("type")
                        if kind == "Error":
                            raise SpeechError(
                                f"Deepgram Flux failed ({event.get('code', '?')}; model={self.model}): "
                                f"{event.get('description', '')}"
                            )
                        if kind == "Warning":
                            logger.warning("Deepgram Flux warning %s: %s", event.get("code"), event.get("description"))
                        elif kind in _TERMINAL_EVENTS:
                            ended = True
                            break
                    if ended:
                        break
                    # A warm socket the server already dropped (idle or session cap)
                    # only shows up as an empty turn; replace it once and resend.
                    self._detach_socket()
                    if received or not reused:
                        raise SpeechError(f"Deepgram Flux socket closed mid-turn (model={self.model}).")
                if rechunk.tail:
                    raise SpeechError("Deepgram Flux returned a truncated PCM sample.")
                if not received:
                    raise SpeechError("Deepgram Flux returned no audio.")
                complete = True
            finally:
                if not complete and ws is not None and ws is self._ws and not ws.closed:
                    with contextlib.suppress(Exception):
                        await ws.send_json({"type": "Interrupt"})
                    self._detach_socket()
                    if not self._closed:
                        self._spawn(self._prewarm())

    async def _open_turn(self, text: str) -> tuple[aiohttp.ClientWebSocketResponse, bool]:
        """Send Speak + Flush on the warm socket (reused=True) or a fresh one."""
        for retry in (False, True):
            reused = self._ws is not None and not self._ws.closed
            ws = await self._socket()
            try:
                await ws.send_json({"type": "Speak", "text": text})
                await ws.send_json({"type": "Flush"})
                return ws, reused
            except (aiohttp.ClientError, ConnectionResetError) as exc:
                self._detach_socket()
                if retry:
                    raise SpeechError(f"Deepgram Flux socket send failed: {exc}") from exc
        raise AssertionError("unreachable")

    # ---- Aura: chunked REST, same shape as Cartesia ----------------------

    async def _aura_request(self, text: str, attempt_id: str) -> AsyncIterator[bytes]:
        params = [
            ("model", self.model),
            ("encoding", "linear16"),
            ("sample_rate", str(_SAMPLE_RATE)),
            ("container", "none"),
        ]
        speed = wire_speed("deepgram", self.spec.speed, model=self.spec.model)
        if speed != 1.0:
            params.append(("speed", f"{speed:g}"))
        async with self._session().post(
            provider_http_url(self.spec, _DEFAULT_BASE, "/v1/speak"),
            params=params,
            json={"text": text},
            headers={"Authorization": f"Token {self.api_key}"},
        ) as response:
            if response.status != 200:
                body = await response.text()
                raise SpeechError(
                    _http_error_message("Deepgram TTS", response, body, f"model={self.model}")
                )
            rechunk = _Rechunker()
            received = False
            async for data in response.content.iter_chunked(_CHUNK_BYTES):
                for chunk in rechunk.push(data):
                    if self.usage_meter is not None:
                        self.usage_meter.add_usage(attempt_id, outputAudioBytes=len(chunk))
                    received = True
                    yield chunk
            if rechunk.tail:
                raise SpeechError("Deepgram TTS returned a truncated PCM sample.")
            if not received:
                raise SpeechError("Deepgram TTS returned no audio.")


def _parse_event(raw: str) -> dict:
    try:
        event = json.loads(raw)
    except ValueError:
        return {}
    return event if isinstance(event, dict) else {}
