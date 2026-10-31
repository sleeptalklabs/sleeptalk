"""CPU Moonshine Streaming input. No microphone access or runtime downloads.

Audio ingestion is bounded and non-blocking. One native worker owns the model
and every stream, including teardown after cancellation. Results belong to a
capture generation, so a late worker cannot restore an old partial transcript.
"""
from __future__ import annotations

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any

from sleeptalk.speech.common import SpeechError
from sleeptalk.speech.local_models import get_model, is_ready, model_path
from sleeptalk.voice.configs import TurnCompletionSignal

MODEL_ID = "moonshine-small-streaming-en"
SOURCE = "moonshine_streaming"
_BYTES_PER_SECOND = 48_000 * 2
_MAX_QUEUED_BYTES = 3 * _BYTES_PER_SECOND


@dataclass
class _Turn:
    queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    pending_bytes: int = 0
    task: asyncio.Task | None = None
    signal: TurnCompletionSignal = field(default_factory=lambda: TurnCompletionSignal(source=SOURCE))
    error: Exception | None = None
    stream: Any = None
    lines: dict = field(default_factory=dict)
    transcript_audio_ms: float = 0


class LocalStreamingSTT:
    sample_rate = 48_000
    channels = 1
    sample_width = 2
    # Seconds of decoding this machine needs per second of recovered audio.
    # Measured 0.073 (10 s to 82 s clips, Apple Silicon); the budget the route
    # derives from it keeps a four-fold margin, so a monologue is never
    # dropped for want of time the way a fixed timeout dropped it.
    rest_seconds_per_audio_second = 0.3

    def __init__(self, spec, *, models_root: Path | None = None, transcriber_factory=None):
        self.spec = spec
        self.model = get_model(spec.model or MODEL_ID, "stt")
        self.models_root = models_root
        self._factory = transcriber_factory
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="moonshine-streaming")
        self._transcriber = None
        self._turns: dict[int, _Turn] = {}
        self._stats: dict[int, dict] = {}
        self._closed = False
        self._tasks: set[asyncio.Task] = set()

    async def _native(self, function, *args):
        return await asyncio.get_running_loop().run_in_executor(self._executor, partial(function, *args))

    def _load(self):
        if self._transcriber is not None:
            return self._transcriber
        if self._factory is not None:
            self._transcriber = self._factory()
            return self._transcriber
        if not is_ready(self.model, self.models_root):
            raise SpeechError("Moonshine Streaming is not downloaded; select it in Local speech settings first.")
        try:
            from moonshine_voice import ModelArch, Transcriber
        except (ImportError, OSError) as exc:
            raise SpeechError("Moonshine Streaming needs sleeptalk[local-streaming]. Install it in settings.") from exc
        self._transcriber = Transcriber(
            model_path(self.model, self.models_root), ModelArch.SMALL_STREAMING,
            update_interval=0.2,
            options={"return_audio_data": "false", "identify_speakers": "false",
                     "transcription_interval": "0.2"},
        )
        return self._transcriber

    async def warmup(self):
        if self._closed:
            raise SpeechError("Moonshine Streaming is closed")
        await self._native(self._load)

    def _start(self, turn):
        turn.stream = self._load().create_stream(update_interval=0.2)
        def changed(event):
            if hasattr(event, "error"):
                turn.error = SpeechError(str(event.error))
            line = getattr(event, "line", None)
            if line is not None:
                turn.lines[line.line_id] = (str(line.text or "").strip(), bool(line.is_complete),
                                            float(line.duration))
        turn.stream.add_listener(changed)
        turn.stream.start()

    def _feed(self, turn, pcm):
        import numpy as np
        if turn.stream is None:
            self._start(turn)
        # Let Moonshine's persistent resampler handle boundaries between frames.
        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
        turn.stream.add_audio(samples.tolist(), sample_rate=self.sample_rate)
        return self._snapshot(turn)

    @staticmethod
    def _snapshot(turn):
        lines = list(turn.lines.values())
        text = " ".join(line[0] for line in lines if line[0])
        turn.transcript_audio_ms = sum(line[2] for line in lines) * 1000
        return TurnCompletionSignal(transcript=text, is_final=bool(text) and all(line[1] for line in lines), source=SOURCE)

    def _finish(self, turn):
        if turn.stream is not None:
            turn.stream.stop()  # flush the decoder's final words
        if turn.error:
            raise turn.error
        return self._snapshot(turn)

    @staticmethod
    def _dispose(turn):
        if turn.stream is not None:
            turn.stream.close()
            turn.stream = None

    async def _run(self, user_id, turn):
        try:
            while True:
                pcm = await turn.queue.get()
                if pcm is None:
                    return (await self._native(self._finish, turn)).transcript
                # Coalesce queued frames without allowing an unbounded backlog.
                pieces = [pcm]
                count = len(pcm)
                while not turn.queue.empty() and count < _BYTES_PER_SECOND // 5:
                    item = turn.queue.get_nowait()
                    if item is None:
                        turn.queue.put_nowait(None)
                        break
                    pieces.append(item)
                    count += len(item)
                pcm = b"".join(pieces)
                signal = await self._native(self._feed, turn, pcm)
                turn.pending_bytes -= len(pcm)
                if turn.error:
                    raise turn.error
                if self._turns.get(user_id) is turn:
                    turn.signal = signal
        except Exception as exc:
            turn.error = exc
            raise
        finally:
            # Native inference may outlive asyncio cancellation. FIFO ownership
            # ensures disposal runs after it, never concurrently with it.
            await self._native(self._dispose, turn)

    async def append_pcm(self, user_id: int, pcm: bytes):
        if self._closed:
            raise SpeechError("Moonshine Streaming is closed")
        if not pcm:
            return
        if len(pcm) % 2:
            raise SpeechError("Moonshine Streaming requires complete int16 samples")
        turn = self._turns.get(user_id)
        if turn is None:
            turn = self._turns[user_id] = _Turn()
            turn.task = asyncio.create_task(self._run(user_id, turn))
            self._tasks.add(turn.task)
            turn.task.add_done_callback(self._tasks.discard)
            turn.task.add_done_callback(lambda task: None if task.cancelled() else task.exception())
        if turn.error:
            raise SpeechError(f"Moonshine Streaming failed: {turn.error}")
        if turn.pending_bytes + len(pcm) > _MAX_QUEUED_BYTES:
            turn.error = SpeechError("Moonshine Streaming fell behind live audio; using local batch recovery")
            turn.task.cancel()
            raise turn.error
        turn.pending_bytes += len(pcm)
        turn.queue.put_nowait(pcm)

    def turn_completion_signal(self, user_id: int):
        turn = self._turns.get(user_id)
        return turn.signal if turn is not None and turn.error is None else TurnCompletionSignal(source=SOURCE)

    async def finish_transcription(self, user_id: int, *, fallback_pcm=b"", timeout_seconds=3.0):
        turn = self._turns.pop(user_id, None)
        if turn is None:
            return ""
        try:
            if turn.error:
                raise turn.error
            turn.queue.put_nowait(None)
            text = await asyncio.wait_for(turn.task, timeout=timeout_seconds)
            self._stats[user_id] = {"transcript_audio_ms": turn.transcript_audio_ms}
            return text
        finally:
            if not turn.task.done():
                turn.task.cancel()
            await asyncio.gather(turn.task, return_exceptions=True)

    def last_turn_stats(self, user_id):
        return self._stats.get(user_id, {})

    async def transcribe_pcm(self, pcm: bytes, *, purpose=""):
        # Same local model is the recovery path, read in one pass. Replaying
        # the utterance through a live stream decoded it again every 200 ms
        # (0.26x real time), so a minute-long turn could not finish inside
        # stt.rest_timeout_seconds and the whole turn was dropped; one pass
        # reads the same audio at 0.07x. Moonshine's batch entry point owns no
        # stream, so it cannot overwrite the live caller's partial either.
        if not pcm:
            return ""
        if self._closed:
            raise SpeechError("Moonshine Streaming is closed")
        def batch():
            import numpy as np
            # The array itself, not a Python list of floats: a minute of audio
            # costs 5 MB this way and 190 MB as a list, on a machine that is
            # also running the model, the voice and the browser.
            samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
            transcript = self._load().transcribe_without_streaming(
                samples, sample_rate=self.sample_rate)
            lines = (str(line.text or "").strip() for line in transcript.lines)
            return " ".join(line for line in lines if line)
        started = time.perf_counter()
        result = await self._native(batch)
        self.last_decode_ms = (time.perf_counter() - started) * 1000
        return result

    async def discard_transcription(self, user_id):
        turn = self._turns.pop(user_id, None)
        if turn is not None:
            turn.task.cancel()
            await asyncio.gather(turn.task, return_exceptions=True)

    async def reset(self):
        self._turns.clear()
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._stats.clear()

    async def close(self):
        if self._closed:
            return
        self._closed = True
        await self.reset()
        def dispose():
            if self._transcriber is not None:
                self._transcriber.close()
                self._transcriber = None
        await self._native(dispose)
        self._executor.shutdown(wait=False)
