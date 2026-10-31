"""Scribe v2 Realtime input with manual commits and isolated capture sockets.

A capture owns its socket until finalization. Closing it at the boundary prevents
late results (including a timed-out commit) from leaking into the next capture.
The voice runtime retains the complete utterance for its existing REST fallback.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import time
import uuid
from urllib.parse import urlencode

import aiohttp

from sleeptalk.providers import provider_websocket_url
from sleeptalk.speech.common import SpeechError
from sleeptalk.speech.deepgram import StreamingSttTurnStats
from sleeptalk.speech.elevenlabs_stt import DEFAULT_BASE, _primary_language
from sleeptalk.voice.configs import TurnCompletionSignal

REALTIME_MODEL = "scribe_v2_realtime"
_BYTES_PER_MS = 96  # 48 kHz, mono signed 16-bit PCM
_CHUNK_BYTES = 100 * _BYTES_PER_MS


def streaming_url(spec):
    params = {
        "model_id": REALTIME_MODEL,
        "audio_format": "pcm_48000",
        "commit_strategy": "manual",
        "include_timestamps": "false",
    }
    language = _primary_language(spec.language)
    if language:
        params["language_code"] = language
    return provider_websocket_url(spec, DEFAULT_BASE, "/v1/speech-to-text/realtime") + "?" + urlencode(params)


class ElevenLabsStreamingSTT:
    sample_rate = 48_000
    channels = 1

    def __init__(self, spec, *, api_key):
        self.spec, self.api_key = spec, api_key
        self.usage_meter = None
        self._turns = {}
        self._stats = {}
        self._pending_latency = {}
        self._lock = asyncio.Lock()

    def set_usage_meter(self, meter):
        self.usage_meter = meter
        for turn in self._turns.values():
            turn.meter = meter

    def begin_latency_attempt(self, user_id, *, attempt_id, on_event):
        turn = self._turns.get(user_id)
        if turn:
            turn.latency = (attempt_id, on_event)
            turn.partial_emitted = False
        else:
            self._pending_latency[user_id] = (attempt_id, on_event)

    async def warmup(self):
        # Validate the realtime handshake without synthesizing a microphone turn.
        turn = _ScribeStream(self.spec, self.api_key, None)
        try:
            await turn.connect()
        finally:
            await turn.close()

    async def append_pcm(self, user_id, pcm):
        if not pcm:
            return
        async with self._lock:
            turn = self._turns.get(user_id)
            if turn is None:
                turn = _ScribeStream(self.spec, self.api_key, self.usage_meter)
                turn.latency = self._pending_latency.pop(user_id, None)
                self._turns[user_id] = turn
            await turn.append(pcm)

    def turn_completion_signal(self, user_id):
        turn = self._turns.get(user_id)
        return TurnCompletionSignal(
            transcript=turn.text if turn and not turn.error else "",
            is_final=False, source="elevenlabs",
        )

    async def finish_transcription(self, user_id, *, fallback_pcm=b"", timeout_seconds=3.0):
        async with self._lock:
            turn = self._turns.get(user_id)
            if turn is None:
                self._pending_latency.pop(user_id, None)
                return ""
            started = time.monotonic()
            status = "error"
            try:
                result = await asyncio.wait_for(turn.finish(), max(0.05, timeout_seconds))
                status = "ok"
                return result
            finally:
                self._stats[user_id] = StreamingSttTurnStats(
                    source_bytes_sent=turn.source_bytes,
                    socket_bytes_received=turn.sent_bytes,
                    sent_audio_ms=round(turn.source_bytes / _BYTES_PER_MS),
                    had_socket=turn.ws is not None,
                    finalize_wait_ms=round((time.monotonic() - started) * 1000),
                    finalize_exit="from_finalize" if status == "ok" else "error",
                )
                await turn.close(status)
                self._turns.pop(user_id, None)
                self._pending_latency.pop(user_id, None)

    def last_turn_stats(self, user_id):
        return self._stats.get(user_id, StreamingSttTurnStats())

    async def discard_transcription(self, user_id):
        async with self._lock:
            turn = self._turns.pop(user_id, None)
            self._pending_latency.pop(user_id, None)
            if turn:
                await turn.close("error")

    async def reset(self):
        async with self._lock:
            turns = list(self._turns.values())
            self._turns.clear()
            self._stats.clear()
            self._pending_latency.clear()
            await asyncio.gather(*(turn.close() for turn in turns))

    async def close(self):
        await self.reset()


class _ScribeStream:
    def __init__(self, spec, api_key, meter):
        self.spec, self.api_key, self.meter = spec, api_key, meter
        self.session = self.ws = self.reader = None
        self.ready = asyncio.Event()
        self.committed = asyncio.Event()
        self.finalizing = False
        self.error = None
        self.parts = []
        self.partial = ""
        self.pending = bytearray()
        self.source_bytes = self.sent_bytes = 0
        self.usage_id = ""
        self.latency = None
        self.partial_emitted = False

    @property
    def text(self):
        # Committed messages are segments; partial messages replace the tail.
        return " ".join(part for part in [*self.parts, self.partial] if part).strip()

    def check(self):
        if self.error:
            raise self.error

    async def connect(self):
        self.check()
        if self.ws is not None:
            if self.ws.closed:
                raise SpeechError("ElevenLabs realtime STT connection closed.")
            return
        if not self.api_key.strip():
            raise SpeechError("Missing ELEVENLABS_API_KEY.")
        self.session = aiohttp.ClientSession()
        try:
            self.ws = await asyncio.wait_for(self.session.ws_connect(
                streaming_url(self.spec), headers={"xi-api-key": self.api_key}, heartbeat=5,
            ), 5.0)
            self.reader = asyncio.create_task(self.read_messages())
            await asyncio.wait_for(self.ready.wait(), 5.0)
            self.check()
        except BaseException:
            if not self.error:
                self.error = SpeechError("ElevenLabs realtime STT connection failed.")
            await self.close("error")
            raise

    async def append(self, pcm):
        await self.connect()
        if not self.usage_id and self.meter:
            self.usage_id = f"{uuid.uuid4().hex[:12]}:stt:ws"
            self.meter.start_attempt(provider="elevenlabs", operation="stt.websocket", attempt_id=self.usage_id)
        self.source_bytes += len(pcm)
        self.pending.extend(pcm)
        while len(self.pending) >= _CHUNK_BYTES:
            chunk = bytes(self.pending[:_CHUNK_BYTES])
            del self.pending[:_CHUNK_BYTES]
            await self.send(chunk)

    async def send(self, pcm, *, commit=False):
        self.check()
        await self.ws.send_json({
            "message_type": "input_audio_chunk",
            "audio_base_64": base64.b64encode(pcm).decode("ascii"),
            "sample_rate": 48_000,
            "commit": commit,
        })
        self.sent_bytes += len(pcm)
        if self.meter and self.usage_id:
            self.meter.add_usage(self.usage_id, audioMs=len(pcm) / _BYTES_PER_MS, audioBytes=len(pcm))

    async def finish(self):
        self.check()
        # Scribe starts processing after two seconds of audio. Pad short
        # captures with silence; send it immediately, without a real-time wait.
        tail = bytes(self.pending)
        self.pending.clear()
        tail += bytes(max(0, 2000 * _BYTES_PER_MS - self.sent_bytes - len(tail)))
        while len(tail) > _CHUNK_BYTES:
            await self.send(tail[:_CHUNK_BYTES])
            tail = tail[_CHUNK_BYTES:]
        self.finalizing = True
        self.committed.clear()
        await self.send(tail, commit=True)
        await self.committed.wait()
        self.check()
        return self.text

    async def read_messages(self):
        try:
            async for message in self.ws:
                if message.type != aiohttp.WSMsgType.TEXT:
                    continue
                payload = json.loads(message.data)
                kind = payload.get("message_type", "")
                if kind == "session_started":
                    self.ready.set()
                elif kind in {"partial_transcript", "committed_transcript"}:
                    value = payload.get("text")
                    text = value.strip() if isinstance(value, str) else ""
                    if kind == "partial_transcript":
                        self.partial = text
                    else:
                        if text:
                            self.parts.append(text)
                        self.partial = ""
                        if self.finalizing:
                            self.committed.set()
                    if text and self.latency and not self.partial_emitted:
                        self.partial_emitted = True
                        attempt_id, callback = self.latency
                        with contextlib.suppress(Exception):
                            callback("stt_first_partial", {
                                "provider": "elevenlabs", "operation": "stt.websocket",
                                "attemptId": attempt_id, "transcriptChars": len(text),
                                "isFinal": kind == "committed_transcript", "speechFinal": False,
                            })
                elif kind not in {"warning", "committed_transcript_with_timestamps"}:
                    # Do not log raw provider payloads: they can echo audio or keys.
                    self.error = SpeechError("ElevenLabs realtime STT rejected the stream.")
                    break
        except Exception:
            self.error = SpeechError("ElevenLabs realtime STT connection failed.")
        finally:
            if not self.error:
                self.error = SpeechError("ElevenLabs realtime STT connection closed.")
            self.ready.set()
            self.committed.set()

    async def close(self, status="session_closed"):
        if self.reader:
            self.reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.reader
            self.reader = None
        if self.ws:
            await self.ws.close()
            self.ws = None
        if self.session:
            await self.session.close()
            self.session = None
        if self.meter and self.usage_id:
            self.meter.finish_attempt(self.usage_id, status=status)
            self.usage_id = ""
