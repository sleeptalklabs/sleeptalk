"""Cartesia Ink speech-to-text: batch REST fallback plus a manual-finalize websocket turn.

Mirrors the Deepgram adapter's shape. Sleeptalk owns VAD and endpointing, so the
manual ``/stt/websocket`` endpoint fits: audio streams in, ``finalize`` flushes
the tail, ``flush_done`` acknowledges it. ink-2 (en, es, fr, hi, ja) is the default model;
ink-whisper covers other languages and is the only model batch REST accepts.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode

import aiohttp

from sleeptalk.metering import UsageMeter
from sleeptalk.providers import ProviderSpec, provider_http_url, provider_websocket_url
from sleeptalk.speech.common import SpeechError, _http_error_message, _pcm_to_wav
from sleeptalk.speech.deepgram import StreamingSttTurnStats, _merge_stable_and_interim
from sleeptalk.voice.configs import TurnCompletionSignal

logger = logging.getLogger(__name__)

CARTESIA_VERSION = "2026-08-14"
DEFAULT_MODEL = "ink-2"
BATCH_MODEL = "ink-whisper"
_DEFAULT_BASE = "https://api.cartesia.ai"
_SAMPLE_RATE = 48_000
_SOURCE_BYTES_PER_MS = _SAMPLE_RATE * 2 / 1000


def _primary_language(language: str) -> str:
    return (language or "").strip().lower().replace("_", "-").split("-", 1)[0]


def _is_whisper(model: str) -> bool:
    return model.strip().lower().startswith("ink-whisper")


def _streaming_url(spec: ProviderSpec) -> str:
    model = spec.model.strip() or DEFAULT_MODEL
    params: list[tuple[str, str]] = [
        ("model", model),
        ("encoding", "pcm_s16le"),
        ("sample_rate", str(_SAMPLE_RATE)),
        ("cartesia_version", CARTESIA_VERSION),
    ]
    # ink-whisper takes a language; ink-2 / ink-preview detect it and take keyterms.
    if _is_whisper(model):
        language = _primary_language(spec.language)
        if language:
            params.append(("language", language))
    else:
        keyterms = spec.options.get("keyterms")
        if isinstance(keyterms, list):
            params.extend(("keyterm", str(term)) for term in keyterms if str(term).strip())
    url = provider_websocket_url(spec, _DEFAULT_BASE, "/stt/websocket")
    return f"{url}?{urlencode(params)}"


def _read_transcript_text(payload: dict) -> str:
    text = payload.get("text")
    return text if isinstance(text, str) else ""


class CartesiaSTT:
    """Batch transcription of one utterance (the REST fallback and the warmup probe)."""

    sample_rate = _SAMPLE_RATE
    channels = 1
    sample_width = 2

    def __init__(self, spec: ProviderSpec, *, api_key: str) -> None:
        self.spec = spec
        self.api_key = api_key
        self.usage_meter: UsageMeter | None = None

    def set_usage_meter(self, meter: UsageMeter) -> None:
        self.usage_meter = meter

    async def warmup(self) -> None:
        silence_ms = 100
        silence = b"\x00" * int(self.sample_rate * self.channels * self.sample_width * silence_ms / 1000)
        await self.transcribe_pcm(silence, purpose="warmup")

    async def transcribe_pcm(self, pcm: bytes, *, purpose: str = "") -> str:
        if not self.api_key.strip():
            raise SpeechError("Missing CARTESIA_API_KEY.")
        if not pcm:
            return ""
        model = self.spec.model.strip() if _is_whisper(self.spec.model) else BATCH_MODEL
        wav_audio = _pcm_to_wav(pcm, self.channels, self.sample_width, self.sample_rate)
        form = aiohttp.FormData()
        form.add_field("file", wav_audio, filename="utterance.wav", content_type="audio/wav")
        form.add_field("model", model)
        language = _primary_language(self.spec.language)
        if language:
            form.add_field("language", language)
        timeout = aiohttp.ClientTimeout(total=float(self.spec.options.get("rest_timeout_seconds") or 8.0))
        attempt_id = f"{uuid.uuid4().hex[:12]}:stt:rest:1"
        if self.usage_meter is not None:
            self.usage_meter.start_attempt(
                provider="cartesia",
                operation="stt.rest",
                attempt_id=attempt_id,
                metadata={"purpose": purpose} if purpose else None,
                audioMs=round(len(pcm) / _SOURCE_BYTES_PER_MS),
                audioBytes=len(pcm),
            )
        try:
            async with aiohttp.ClientSession() as session, session.post(
                provider_http_url(self.spec, _DEFAULT_BASE, "/stt"),
                data=form,
                headers={"X-API-Key": self.api_key, "Cartesia-Version": CARTESIA_VERSION},
                timeout=timeout,
            ) as response:
                if response.status != 200:
                    body = await response.text()
                    raise SpeechError(
                        _http_error_message(
                            "Cartesia STT", response, body, f"model={model}, language={language}"
                        )
                    )
                payload = await response.json()
        except Exception:
            if self.usage_meter is not None:
                self.usage_meter.finish_attempt(attempt_id, status="error")
            raise
        if self.usage_meter is not None:
            self.usage_meter.finish_attempt(attempt_id, status="ok")
        return _read_transcript_text(payload if isinstance(payload, dict) else {}).strip()


class CartesiaStreamingSTT:
    sample_rate = _SAMPLE_RATE
    channels = 1

    def __init__(self, spec: ProviderSpec, *, api_key: str) -> None:
        self.spec = spec
        self.api_key = api_key
        self._turns: dict[int, _CartesiaStreamingTurn] = {}
        self._last_turn_stats: dict[int, StreamingSttTurnStats] = {}
        self._pending_latency_attempts: dict[int, tuple[str, Callable[[str, dict[str, Any]], None]]] = {}
        self.usage_meter: UsageMeter | None = None
        self._lock = asyncio.Lock()

    @property
    def final_from_rest(self) -> bool:
        """ink-whisper's stream drops words at finalize (measured 2026-09-30:
        "…la capital de" for "…la capital de Francia?", Chinese clauses
        missing); batch REST hears the whole utterance in about 300 ms."""
        return _is_whisper(self.spec.model)

    def set_usage_meter(self, meter: UsageMeter) -> None:
        self.usage_meter = meter
        for turn in self._turns.values():
            turn.usage_meter = meter

    def begin_latency_attempt(
        self, user_id: int, *, attempt_id: str, on_event: Callable[[str, dict[str, Any]], None]
    ) -> None:
        turn = self._turns.get(user_id)
        if turn is not None:
            turn.begin_latency_attempt(attempt_id=attempt_id, on_event=on_event)
        else:
            self._pending_latency_attempts[user_id] = (attempt_id, on_event)

    async def warmup(self) -> None:
        silence_ms = 80
        silence = b"\x00" * int(_SAMPLE_RATE * 2 * silence_ms / 1000)
        await self.append_pcm(-1, silence)
        await self.finish_transcription(-1, fallback_pcm=silence, timeout_seconds=0.4)
        async with self._lock:
            warmup_turn = self._turns.pop(-1, None)
            self._last_turn_stats.pop(-1, None)
        if warmup_turn:
            await warmup_turn.close()

    async def append_pcm(self, user_id: int, pcm: bytes) -> None:
        if not pcm:
            return
        turn = await self._turn_for_user(user_id)
        await turn.send_pcm(pcm)

    async def finish_transcription(
        self, user_id: int, *, fallback_pcm: bytes = b"", timeout_seconds: float = 3.0
    ) -> str:
        async with self._lock:
            turn = self._turns.get(user_id)
        if not turn:
            self._pending_latency_attempts.pop(user_id, None)
            return ""
        transcript = await turn.finish(timeout_seconds=timeout_seconds)
        self._last_turn_stats[user_id] = turn.last_finish_stats
        turn.end_latency_attempt()
        self._pending_latency_attempts.pop(user_id, None)
        return transcript

    async def flush_at_pause(self, user_id: int) -> None:
        """Finalize what was heard so far; the stream stays open for more."""
        turn = self._turns.get(user_id)
        if turn is not None:
            await turn.flush_at_pause()

    def turn_completion_signal(self, user_id: int) -> TurnCompletionSignal:
        turn = self._turns.get(user_id)
        if not turn:
            return TurnCompletionSignal(source="cartesia")
        return turn.turn_completion_signal()

    def last_turn_stats(self, user_id: int) -> StreamingSttTurnStats:
        return self._last_turn_stats.get(user_id, StreamingSttTurnStats())

    async def reset(self) -> None:
        async with self._lock:
            turns = list(self._turns.values())
            self._turns.clear()
            self._last_turn_stats.clear()
            self._pending_latency_attempts.clear()
        await asyncio.gather(*(turn.close() for turn in turns), return_exceptions=True)

    async def close(self) -> None:
        await self.reset()

    async def _turn_for_user(self, user_id: int) -> _CartesiaStreamingTurn:
        async with self._lock:
            turn = self._turns.get(user_id)
            if not turn:
                turn = _CartesiaStreamingTurn(self.spec, api_key=self.api_key, usage_meter=self.usage_meter)
                self._turns[user_id] = turn
            pending = self._pending_latency_attempts.pop(user_id, None)
            if pending is not None:
                attempt_id, on_event = pending
                turn.begin_latency_attempt(attempt_id=attempt_id, on_event=on_event)
            return turn


class _CartesiaStreamingTurn:
    """One user's persistent socket. Final deltas accumulate; ``finalize`` flushes the tail.

    Cartesia's finals are deltas that carry their own leading whitespace, and the
    interim (``is_final=false``) text is cumulative since the last final, so the
    live transcript is ``"".join(finals) + interim`` with no separators added.
    """

    def __init__(self, spec: ProviderSpec, *, api_key: str, usage_meter: UsageMeter | None = None) -> None:
        self.spec = spec
        self.api_key = api_key
        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._reader_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._final_parts: list[str] = []
        self._consumed_final_parts = 0
        self._latest_interim = ""
        self._sent_source_bytes = 0
        self._finished_source_bytes = 0
        self._reconnects = 0
        self._finished_reconnects = 0
        self.last_finish_stats = StreamingSttTurnStats()
        self._flush_event = asyncio.Event()
        self._closed = False
        self._latency_attempt_id = ""
        self._latency_on_event: Callable[[str, dict[str, Any]], None] | None = None
        self._latency_first_partial_emitted = False
        self.usage_meter = usage_meter
        self._usage_attempt_base = ""
        self._usage_attempt_id = ""
        self._usage_retry = 0

    def begin_latency_attempt(self, *, attempt_id: str, on_event: Callable[[str, dict[str, Any]], None]) -> None:
        self._latency_attempt_id = attempt_id
        self._latency_on_event = on_event
        self._latency_first_partial_emitted = False
        self._usage_attempt_base = attempt_id

    def end_latency_attempt(self) -> None:
        self._latency_attempt_id = ""
        self._latency_on_event = None
        self._latency_first_partial_emitted = False
        self._usage_attempt_base = ""

    async def send_pcm(self, pcm: bytes) -> None:
        async with self._lock:
            if self._closed:
                return
            if self._sent_source_bytes == self._finished_source_bytes:
                # First audio of a new utterance: anything buffered since the
                # last finish() belongs to the previous one.
                self._consumed_final_parts = len(self._final_parts)
                self._latest_interim = ""
                self._flush_event.clear()
                self._usage_retry = 0
                self._start_usage_attempt()
            await self._connect()
            assert self._ws is not None
            self._add_stream_usage(pcm)
            try:
                await self._ws.send_bytes(pcm)
            except (aiohttp.ClientError, RuntimeError, ConnectionError):
                self._finish_usage_attempt("retry")
                await self._reconnect()
                assert self._ws is not None
                self._usage_retry += 1
                self._start_usage_attempt(retry=self._usage_retry)
                self._add_stream_usage(pcm)
                await self._ws.send_bytes(pcm)
            self._sent_source_bytes += len(pcm)

    async def flush_at_pause(self) -> None:
        """Ask for the words held back at a pause: Cartesia's interims lag up
        to a second and carry no end mark, so every turn waited the
        uncertain 1.8 s. ``finalize`` returns punctuated text at once."""
        async with self._lock:
            if self._closed or not self._ws or self._ws.closed:
                return
            if self._sent_source_bytes == self._finished_source_bytes:
                return
            with contextlib.suppress(aiohttp.ClientError, RuntimeError, ConnectionError):
                await self._ws.send_str("finalize")

    async def finish(self, *, timeout_seconds: float) -> str:
        started_at = time.monotonic()
        async with self._lock:
            if self._closed:
                return self.transcript
            if self._ws and not self._ws.closed:
                self._flush_event.clear()
                finalize_exit = "timeout"
                try:
                    await self._ws.send_str("finalize")
                except (aiohttp.ClientError, RuntimeError, ConnectionError):
                    finalize_exit = "send_failed"
                else:
                    # flush_done normally lands within ~100 ms; the cap only
                    # bounds a socket that has gone quiet.
                    cap = min(0.7, max(0.05, timeout_seconds - (time.monotonic() - started_at)))
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(self._flush_event.wait(), timeout=cap)
                    if self._flush_event.is_set():
                        finalize_exit = "from_finalize"
            else:
                finalize_exit = "no_socket"

            transcript = self._current_transcript()
            source_bytes = max(self._sent_source_bytes - self._finished_source_bytes, 0)
            self.last_finish_stats = StreamingSttTurnStats(
                source_bytes_sent=source_bytes,
                socket_bytes_received=source_bytes,
                reconnects=max(self._reconnects - self._finished_reconnects, 0),
                had_socket=bool(self._ws and not self._ws.closed),
                finalize_wait_ms=round((time.monotonic() - started_at) * 1000),
                finalize_exit=finalize_exit,
                sent_audio_ms=round(source_bytes / _SOURCE_BYTES_PER_MS),
            )
            self._finished_source_bytes = self._sent_source_bytes
            self._finished_reconnects = self._reconnects
            self._consumed_final_parts = len(self._final_parts)
            self._latest_interim = ""
            self._flush_event.clear()
            self._finish_usage_attempt("ok")
            return transcript

    async def close(self) -> None:
        self._closed = True
        self._finish_usage_attempt("session_closed")
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader_task
        self._reader_task = None
        if self._ws:
            with contextlib.suppress(Exception):
                await self._ws.send_str("close")
            await self._ws.close()
            self._ws = None
        if self._session:
            await self._session.close()
            self._session = None

    @property
    def transcript(self) -> str:
        return _merge_stable_and_interim("".join(self._final_parts), self._latest_interim)

    def turn_completion_signal(self) -> TurnCompletionSignal:
        # The manual endpoint has no acoustic end-of-turn; the local endpointer decides.
        return TurnCompletionSignal(transcript=self._current_transcript(), is_final=False, source="cartesia")

    async def _connect(self) -> None:
        if self._ws and not self._ws.closed:
            return
        if self._session:
            await self._session.close()
            self._session = None
        if not self.api_key.strip():
            raise SpeechError("Missing CARTESIA_API_KEY.")
        self._session = aiohttp.ClientSession()
        self._ws = await self._session.ws_connect(
            _streaming_url(self.spec), headers={"X-API-Key": self.api_key}, heartbeat=20
        )
        self._reader_task = asyncio.create_task(self._read_messages())

    async def _reconnect(self) -> None:
        self._reconnects += 1
        if self._ws:
            with contextlib.suppress(Exception):
                await self._ws.close()
            self._ws = None
        if self._session:
            with contextlib.suppress(Exception):
                await self._session.close()
            self._session = None
        await asyncio.sleep(min(0.25 * self._reconnects, 2.0))
        await self._connect()

    async def _read_messages(self) -> None:
        assert self._ws is not None
        async for message in self._ws:
            if message.type != aiohttp.WSMsgType.TEXT:
                continue
            try:
                payload = json.loads(message.data)
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            kind = payload.get("type")
            if kind == "flush_done":
                self._flush_event.set()
                continue
            if kind == "error":
                logger.warning(
                    "Cartesia STT error %s: %s", payload.get("error_code"), payload.get("message")
                )
                continue
            if kind != "transcript":
                continue
            text = _read_transcript_text(payload)
            if not text:
                continue
            self._emit_first_partial(text, payload)
            if payload.get("is_final"):
                self._final_parts.append(text)
                self._latest_interim = ""
            else:
                self._latest_interim = text

    def _emit_first_partial(self, text: str, payload: dict) -> None:
        if self._latency_on_event is None or self._latency_first_partial_emitted:
            return
        self._latency_first_partial_emitted = True
        try:
            self._latency_on_event(
                "stt_first_partial",
                {
                    "provider": "cartesia",
                    "operation": "stt.websocket",
                    "attemptId": self._latency_attempt_id,
                    "transcriptChars": len(text),
                    "isFinal": bool(payload.get("is_final")),
                    "speechFinal": False,
                },
            )
        except Exception:  # noqa: BLE001,S110 — telemetry is best effort
            pass

    def _current_transcript(self) -> str:
        stable = "".join(self._final_parts[self._consumed_final_parts :])
        return _merge_stable_and_interim(stable, self._latest_interim)

    def _start_usage_attempt(self, *, retry: int = 0) -> None:
        if self.usage_meter is None:
            return
        base = self._usage_attempt_base or f"{uuid.uuid4().hex[:12]}:stt:ws"
        attempt_id = f"{base}:retry:{retry}" if retry else base
        self._usage_attempt_id = attempt_id
        self.usage_meter.start_attempt(
            provider="cartesia",
            operation="stt.websocket",
            attempt_id=attempt_id,
            metadata={"retryIndex": retry},
        )

    def _add_stream_usage(self, pcm: bytes) -> None:
        if self.usage_meter is None or not self._usage_attempt_id:
            return
        self.usage_meter.add_usage(
            self._usage_attempt_id, audioMs=len(pcm) / _SOURCE_BYTES_PER_MS, audioBytes=len(pcm)
        )

    def _finish_usage_attempt(self, status: str) -> None:
        if self.usage_meter is None or not self._usage_attempt_id:
            return
        self.usage_meter.finish_attempt(self._usage_attempt_id, status=status)
        self._usage_attempt_id = ""
