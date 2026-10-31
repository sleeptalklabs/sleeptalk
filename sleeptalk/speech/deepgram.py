"""Deepgram STT provider, extracted from discord_agent (spec + api_key construction)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import tempfile
import time
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
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
    _deepgram_query_params_from_spec,
    _elevenlabs_wav_bytes,
    _has_speakable_content,
    _http_error_message,
    _normalize_wav_bytes,
    _pcm_to_wav,
    _read_deepgram_transcript,
)
from sleeptalk.voice.configs import TurnCompletionSignal


def _remaining_timeout(started_at: float, timeout_seconds: float, *, cap: float) -> float:
    elapsed = time.monotonic() - started_at
    remaining = max(0.05, timeout_seconds - elapsed)
    return min(cap, remaining)


def _streaming_url(spec: ProviderSpec) -> str:
    params = _deepgram_query_params_from_spec(spec)
    # endpointing=300 was too eager: speech_final on a 300ms mid-thought
    # pause boosted the sink's completion score and flushed turns after
    # ~650ms of silence. 1000ms keeps finals for genuine pauses; finish()'s
    # Finalize covers the rest. Profiles tune it via stt options
    # (endpointing_ms); Deepgram's own default is effectively hair-trigger.
    endpointing_ms = spec.options.get("endpointing_ms", 1000)
    params.extend(
        [
            ("encoding", "linear16"),
            ("sample_rate", "48000"),
            ("channels", "1"),
            ("interim_results", "true"),
            ("endpointing", str(endpointing_ms)),
        ]
    )
    url = provider_websocket_url(
        spec,
        "https://api.deepgram.com",
        "/v1/listen",
    )
    return f"{url}?{urlencode(params)}"


@dataclass(frozen=True)
class StreamingSttTurnStats:
    source_bytes_sent: int = 0
    socket_bytes_received: int = 0
    reconnects: int = 0
    had_socket: bool = False
    finalize_wait_ms: int = 0
    finalize_exit: str = ""
    sent_audio_ms: int = 0
    transcript_audio_ms: int = 0


class DeepgramSTT:
    sample_rate = 48000
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
        silence = b"\x00" * int(
            self.sample_rate * self.channels * self.sample_width * silence_ms / 1000
        )
        await self.transcribe_pcm(silence, purpose="warmup")

    async def transcribe_pcm(self, pcm: bytes, *, purpose: str = "") -> str:
        if not self.api_key.strip():
            raise SpeechError("Missing DEEPGRAM_API_KEY.")
        if not pcm:
            return ""

        params = _deepgram_query_params_from_spec(self.spec)
        headers = {
            "Authorization": f"Token {self.api_key}",
            "Content-Type": "audio/wav",
        }
        wav_audio = _pcm_to_wav(pcm, self.channels, self.sample_width, self.sample_rate)
        base = provider_http_url(
            self.spec,
            "https://api.deepgram.com",
            "/v1/listen",
        )
        url = f"{base}?{urlencode(params)}"
        attempt_id = f"{uuid.uuid4().hex[:12]}:stt:rest:1"
        if self.usage_meter is not None:
            self.usage_meter.start_attempt(
                provider="deepgram",
                operation="stt.rest",
                attempt_id=attempt_id,
                metadata={"purpose": purpose} if purpose else None,
                audioMs=round(
                    len(pcm)
                    / (self.sample_rate * self.channels * self.sample_width)
                    * 1000
                ),
                audioBytes=len(pcm),
            )
        try:
            async with aiohttp.ClientSession() as session, session.post(
                url,
                data=wav_audio,
                headers=headers,
            ) as response:
                if response.status != 200:
                    body = await response.text()
                    raise SpeechError(
                        _http_error_message(
                            "Deepgram STT",
                            response,
                            body,
                            f"model={self.spec.model}, language={self.spec.language}",
                        )
                    )
                payload = await response.json()
        except Exception:
            if self.usage_meter is not None:
                self.usage_meter.finish_attempt(attempt_id, status="error")
            raise
        if self.usage_meter is not None:
            self.usage_meter.finish_attempt(attempt_id, status="ok")

        return _read_deepgram_transcript(payload)


class DeepgramStreamingSTT:
    sample_rate = 48000
    channels = 1

    def __init__(self, spec: ProviderSpec, *, api_key: str) -> None:
        self.spec = spec
        self.api_key = api_key
        self._turns: dict[int, _DeepgramStreamingTurn] = {}
        self._last_turn_stats: dict[int, StreamingSttTurnStats] = {}
        self._pending_latency_attempts: dict[
            int, tuple[str, Callable[[str, dict[str, Any]], None]]
        ] = {}
        self.usage_meter: UsageMeter | None = None
        self._lock = asyncio.Lock()

    def set_usage_meter(self, meter: UsageMeter) -> None:
        self.usage_meter = meter
        for turn in self._turns.values():
            turn.usage_meter = meter

    def begin_latency_attempt(
        self,
        user_id: int,
        *,
        attempt_id: str,
        on_event: Callable[[str, dict[str, Any]], None],
    ) -> None:
        """Correlate the next transcript result with one local utterance.

        This method is synchronous because local VAD calls it from the capture
        callback. If the user's persistent Deepgram socket has not been built
        yet, the binding is applied when the first PCM frame creates it.
        """
        turn = self._turns.get(user_id)
        if turn is not None:
            turn.begin_latency_attempt(attempt_id=attempt_id, on_event=on_event)
        else:
            self._pending_latency_attempts[user_id] = (attempt_id, on_event)

    async def warmup(self) -> None:
        silence_ms = 80
        silence = b"\x00" * int(48000 * 2 * silence_ms / 1000)
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
        self,
        user_id: int,
        *,
        fallback_pcm: bytes = b"",
        timeout_seconds: float = 3.0,
    ) -> str:
        async with self._lock:
            turn = self._turns.get(user_id)
        if not turn:
            self._pending_latency_attempts.pop(user_id, None)
            return ""
        transcript = await turn.finish(
            fallback_pcm=fallback_pcm,
            timeout_seconds=timeout_seconds,
        )
        self._last_turn_stats[user_id] = turn.last_finish_stats
        turn.end_latency_attempt()
        self._pending_latency_attempts.pop(user_id, None)
        return transcript

    def turn_completion_signal(self, user_id: int) -> TurnCompletionSignal:
        turn = self._turns.get(user_id)
        if not turn:
            return TurnCompletionSignal(source="deepgram")
        return turn.turn_completion_signal()

    async def flush_at_pause(self, user_id: int) -> None:
        """Finalize what was heard so far; the stream stays open for more."""
        turn = self._turns.get(user_id)
        if turn is not None:
            await turn.flush_at_pause()

    def last_turn_stats(self, user_id: int) -> "StreamingSttTurnStats":
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

    async def _turn_for_user(self, user_id: int) -> "_DeepgramStreamingTurn":
        async with self._lock:
            turn = self._turns.get(user_id)
            if not turn:
                turn = _DeepgramStreamingTurn(
                    self.spec,
                    api_key=self.api_key,
                    usage_meter=self.usage_meter,
                )
                self._turns[user_id] = turn
            pending = self._pending_latency_attempts.pop(user_id, None)
            if pending is not None:
                attempt_id, on_event = pending
                turn.begin_latency_attempt(
                    attempt_id=attempt_id,
                    on_event=on_event,
                )
            return turn


class _DeepgramStreamingTurn:
    _KEEPALIVE_SECONDS = 5.0
    _SOURCE_BYTES_PER_MS = 48_000 * 2 / 1000

    def __init__(
        self,
        spec: ProviderSpec,
        *,
        api_key: str,
        usage_meter: UsageMeter | None = None,
    ) -> None:
        self.spec = spec
        self.api_key = api_key
        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._reader_task: asyncio.Task | None = None
        self._keepalive_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._final_parts: list[str] = []
        self._latest_interim = ""
        self._speech_final = False
        self._sent_source_bytes = 0
        self._finished_source_bytes = 0
        self._consumed_final_parts = 0
        self._reconnects = 0
        self._finished_reconnects = 0
        self.last_finish_stats = StreamingSttTurnStats()
        self._transcript_event = asyncio.Event()
        # `_final_event` is a segment-final event (`is_final=true`), not an
        # end-of-utterance event. Deepgram can emit many such events while a
        # person is still speaking. `_finalize_event` is reserved for a fresh
        # response to our explicit Finalize control message.
        self._final_event = asyncio.Event()
        self._finalize_event = asyncio.Event()
        self._latest_result_end_seconds = 0.0
        self._finished_result_end_seconds = 0.0
        self._latest_interim_start_seconds = 0.0
        self._closed = False
        self._latency_attempt_id = ""
        self._latency_on_event: Callable[[str, dict[str, Any]], None] | None = None
        self._latency_first_partial_emitted = False
        self.usage_meter = usage_meter
        self._usage_attempt_base = ""
        self._usage_attempt_id = ""
        self._usage_retry = 0

    def begin_latency_attempt(
        self,
        *,
        attempt_id: str,
        on_event: Callable[[str, dict[str, Any]], None],
    ) -> None:
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
                # First audio of a new utterance. Any transcript parts buffered
                # since the last finish() are late arrivals for the PREVIOUS
                # utterance (no new audio has been sent since). Drop them —
                # otherwise finish() can return last turn's text and the bot
                # answers the previous question.
                self._consumed_final_parts = len(self._final_parts)
                self._latest_interim = ""
                self._speech_final = False
                self._transcript_event.clear()
                self._final_event.clear()
                self._finalize_event.clear()
                self._usage_retry = 0
                self._start_usage_attempt()
            await self._connect()
            assert self._ws is not None
            payload = pcm
            self._add_stream_usage(pcm)
            try:
                await self._ws.send_bytes(payload)
            except (aiohttp.ClientError, RuntimeError, ConnectionError):
                self._finish_usage_attempt("retry")
                await self._reconnect()
                assert self._ws is not None
                self._usage_retry += 1
                self._start_usage_attempt(retry=self._usage_retry)
                self._add_stream_usage(pcm)
                await self._ws.send_bytes(payload)
            self._sent_source_bytes += len(pcm)

    async def flush_at_pause(self) -> None:
        """Ask for the words held back at a pause.

        Deepgram keeps the last words of an utterance as an unpunctuated
        interim until its own endpointing (1 s of silence) or a Finalize; a
        live call judged "What's two plus two, just the" incomplete and
        waited 3.5 s. Finalize returns them at once; more audio may follow.
        """
        async with self._lock:
            if self._closed or not self._ws or self._ws.closed:
                return
            if self._sent_source_bytes == self._finished_source_bytes:
                return
            with contextlib.suppress(aiohttp.ClientError, RuntimeError, ConnectionError):
                await self._ws.send_json({"type": "Finalize"})

    async def finish(self, *, fallback_pcm: bytes, timeout_seconds: float) -> str:
        started_at = time.monotonic()
        async with self._lock:
            if self._closed:
                return self.transcript
            # Do not splice `fallback_pcm` into the live stream by byte
            # offset. The live path is ungated decoded PCM while the fallback
            # buffer is utterance-scoped capture audio, so their offsets are
            # not interchangeable (and `_sent_source_bytes` is lifetime
            # connection state). REST STT already transcribes fallback_pcm in
            # parallel when the live result is missing or incomplete.
            if self._ws:
                # These events may have been set by a segment finalized many
                # seconds ago. Clear them BEFORE Finalize so the wait below is
                # for a response newer than this request.
                self._final_event.clear()
                self._finalize_event.clear()
                await self._ws.send_json({"type": "Finalize"})

                sent_audio_seconds = self._sent_source_bytes / (
                    self._SOURCE_BYTES_PER_MS * 1000
                )
                result_lag_seconds = max(
                    sent_audio_seconds - self._latest_result_end_seconds, 0.0
                )
                # A visible interim is direct evidence that the finalized
                # prefix is missing a tail. Otherwise use the audio/result
                # cursor gap. When neither indicates lag, a short grace is
                # enough because Deepgram may omit from_finalize when there is
                # no meaningful buffered audio.
                needs_drain = (
                    bool(self._latest_interim.strip()) or result_lag_seconds > 0.2
                )
                drain_cap = 0.7 if needs_drain else 0.18
                try:
                    await asyncio.wait_for(
                        self._finalize_event.wait(),
                        timeout=_remaining_timeout(
                            started_at, timeout_seconds, cap=drain_cap
                        ),
                    )
                except asyncio.TimeoutError:
                    pass
                if self._finalize_event.is_set():
                    finalize_exit = "from_finalize"
                else:
                    finalize_exit = "timeout"
            else:
                finalize_exit = "no_socket"

            transcript = self._current_transcript()
            source_bytes = max(
                self._sent_source_bytes - self._finished_source_bytes, 0
            )
            transcript_seconds = max(
                self._latest_result_end_seconds
                - self._finished_result_end_seconds,
                0.0,
            )
            self.last_finish_stats = StreamingSttTurnStats(
                source_bytes_sent=source_bytes,
                # Kept for compatibility with existing telemetry. This field
                # historically meant bytes accepted by the websocket, not
                # bytes acknowledged by Deepgram.
                socket_bytes_received=source_bytes,
                reconnects=max(self._reconnects - self._finished_reconnects, 0),
                had_socket=bool(self._ws and not self._ws.closed),
                finalize_wait_ms=round((time.monotonic() - started_at) * 1000),
                finalize_exit=finalize_exit,
                sent_audio_ms=round(source_bytes / self._SOURCE_BYTES_PER_MS),
                transcript_audio_ms=round(transcript_seconds * 1000),
            )
            self._finished_source_bytes = self._sent_source_bytes
            self._finished_reconnects = self._reconnects
            self._finished_result_end_seconds = self._latest_result_end_seconds
            self._consumed_final_parts = len(self._final_parts)
            self._latest_interim = ""
            self._latest_interim_start_seconds = 0.0
            self._speech_final = False
            self._transcript_event.clear()
            self._final_event.clear()
            self._finalize_event.clear()
            self._finish_usage_attempt("ok")
            return transcript

    async def close(self) -> None:
        self._closed = True
        self._finish_usage_attempt("session_closed")
        if self._keepalive_task and not self._keepalive_task.done():
            self._keepalive_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._keepalive_task
        self._keepalive_task = None
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader_task
        self._reader_task = None
        if self._ws:
            with contextlib.suppress(Exception):
                await self._ws.send_json({"type": "CloseStream"})
            await self._ws.close()
            self._ws = None
        if self._session:
            await self._session.close()
            self._session = None

    @property
    def transcript(self) -> str:
        final = " ".join(part.strip() for part in self._final_parts if part.strip()).strip()
        return _merge_stable_and_interim(final, self._latest_interim)

    def turn_completion_signal(self) -> TurnCompletionSignal:
        return TurnCompletionSignal(
            transcript=self._current_transcript(),
            # `is_final` chunks only stabilize one time range. Only
            # `speech_final` is Deepgram's acoustic endpoint signal.
            is_final=self._speech_final,
            source="deepgram",
        )

    async def _connect(self) -> None:
        if self._ws and not self._ws.closed:
            return
        if self._session:
            await self._session.close()
            self._session = None
        if not self.api_key.strip():
            raise SpeechError("Missing DEEPGRAM_API_KEY.")
        self._session = aiohttp.ClientSession()
        self._ws = await self._session.ws_connect(
            _streaming_url(self.spec),
            headers={"Authorization": f"Token {self.api_key}"},
            heartbeat=20,
        )
        if self._keepalive_task is None or self._keepalive_task.done():
            self._keepalive_task = asyncio.create_task(self._send_keepalives())
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
            if payload.get("from_finalize"):
                # Deepgram may acknowledge Finalize with no transcript. This
                # must be observed before the empty-transcript early return.
                self._finalize_event.set()
            transcript = _read_deepgram_transcript(payload)
            if not transcript:
                continue
            if (
                self._latency_on_event is not None
                and not self._latency_first_partial_emitted
            ):
                self._latency_first_partial_emitted = True
                try:
                    self._latency_on_event(
                        "stt_first_partial",
                        {
                            "provider": "deepgram",
                            "operation": "stt.websocket",
                            "attemptId": self._latency_attempt_id,
                            "transcriptChars": len(transcript),
                            "isFinal": bool(payload.get("is_final")),
                            "speechFinal": bool(payload.get("speech_final")),
                        },
                    )
                except Exception:  # noqa: BLE001,S110 — telemetry is best effort
                    # Instrumentation must never break transcription.
                    pass
            try:
                result_start = float(payload.get("start") or 0.0)
                result_duration = float(payload.get("duration") or 0.0)
            except (TypeError, ValueError):
                result_start = 0.0
                result_duration = 0.0
            result_end = max(result_start + result_duration, 0.0)
            if result_end:
                self._latest_result_end_seconds = max(
                    self._latest_result_end_seconds, result_end
                )
            self._transcript_event.set()
            if payload.get("is_final") or payload.get("speech_final"):
                self._final_parts.append(transcript)
                self._speech_final = self._speech_final or bool(payload.get("speech_final"))
                self._final_event.set()
                if (
                    not self._latest_interim_start_seconds
                    or not result_end
                    or result_end >= self._latest_interim_start_seconds
                ):
                    self._latest_interim = ""
                    self._latest_interim_start_seconds = 0.0
            else:
                self._latest_interim = transcript
                self._latest_interim_start_seconds = result_start

    async def _send_keepalives(self) -> None:
        try:
            while not self._closed:
                await asyncio.sleep(self._KEEPALIVE_SECONDS)
                async with self._lock:
                    if self._closed or not self._ws or self._ws.closed:
                        continue
                    await self._ws.send_json({"type": "KeepAlive"})
        except (aiohttp.ClientError, RuntimeError, ConnectionError):
            return

    def _current_transcript(self) -> str:
        final = " ".join(
            part.strip()
            for part in self._final_parts[self._consumed_final_parts :]
            if part.strip()
        ).strip()
        return _merge_stable_and_interim(final, self._latest_interim)

    def _start_usage_attempt(self, *, retry: int = 0) -> None:
        if self.usage_meter is None:
            return
        base = self._usage_attempt_base or f"{uuid.uuid4().hex[:12]}:stt:ws"
        attempt_id = f"{base}:retry:{retry}" if retry else base
        self._usage_attempt_id = attempt_id
        self.usage_meter.start_attempt(
            provider="deepgram",
            operation="stt.websocket",
            attempt_id=attempt_id,
            metadata={"retryIndex": retry},
        )

    def _add_stream_usage(self, pcm: bytes) -> None:
        if self.usage_meter is None or not self._usage_attempt_id:
            return
        self.usage_meter.add_usage(
            self._usage_attempt_id,
            audioMs=len(pcm) / self._SOURCE_BYTES_PER_MS,
            audioBytes=len(pcm),
        )

    def _finish_usage_attempt(self, status: str) -> None:
        if self.usage_meter is None or not self._usage_attempt_id:
            return
        self.usage_meter.finish_attempt(self._usage_attempt_id, status=status)
        self._usage_attempt_id = ""


def _merge_stable_and_interim(stable: str, interim: str) -> str:
    """Join Deepgram's stable chunks with the current non-final tail.

    Deepgram normally starts the interim at the end of the last stable time
    range, but reconnects and model revisions can repeat a few words. Remove
    the largest exact word overlap so the tail is preserved without echoing
    it in endpoint scoring or the committed prompt.
    """
    stable = (stable or "").strip()
    interim = (interim or "").strip()
    if not stable:
        return interim
    if not interim:
        return stable
    stable_words = stable.split()
    interim_words = interim.split()
    max_overlap = min(len(stable_words), len(interim_words), 20)
    overlap = 0
    for size in range(max_overlap, 0, -1):
        left = [
            word.strip(".,!?;:\"'()[]{}").lower()
            for word in stable_words[-size:]
        ]
        right = [
            word.strip(".,!?;:\"'()[]{}").lower()
            for word in interim_words[:size]
        ]
        if left == right:
            overlap = size
            break
    tail = " ".join(interim_words[overlap:]).strip()
    return f"{stable} {tail}".strip()
