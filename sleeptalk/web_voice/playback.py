"""WebRTC playout queue and PCM helpers for browser voice."""
from __future__ import annotations

import asyncio
import contextlib
from array import array
from collections import deque
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from av import AudioFrame, AudioResampler

from sleeptalk.call_provider import CallEnded
from sleeptalk.media_core import (
    CORE_FRAME_BYTES,
    CORE_RATE,
    PlayResult as _PlayResult,
)
from sleeptalk.media_audio import AudioDecodeError, decode_audio_bytes
from sleeptalk.routes.audio_output import AudioOutputAccounting

_WEB_PLAYOUT_QUEUE_CHUNKS = 16

@dataclass(frozen=True, slots=True)
class _WebAudioChunk:
    turn_id: int
    frames: tuple[bytes, ...]
    producer_bytes: int
    accounting: AudioOutputAccounting


class WebRTCPlaybackController:
    """Queued AudioOutput plus asset playback for a browser WebRTC track.

    ``AudioOutput.write`` accepts a synthesized chunk into a bounded playout
    queue and returns. A background task feeds the transport's paced WebRTC
    track. This matches Discord's enqueue semantics, so the route can keep
    consuming agent/tool output while earlier speech is audible, without
    allowing an unbounded stale-audio backlog.
    """

    def __init__(
        self,
        stream: BidirectionalAudioStream,
        *,
        max_queue_chunks: int = _WEB_PLAYOUT_QUEUE_CHUNKS,
        on_audio_start=None,
        on_drain=None,
    ) -> None:
        self.stream = stream
        self.active = False
        self.cancelled = False
        self.on_audio_start = on_audio_start
        self.on_drain = on_drain
        self._closed = False
        self._stop_event = asyncio.Event()
        self._resume_event = asyncio.Event()
        self._resume_event.set()
        self._next_pts = 0
        self._epoch = -1
        self._cancelled_through = -1
        self._accounting = AudioOutputAccounting(turn_id=-1)
        self._audio_queue: asyncio.Queue[_WebAudioChunk] = asyncio.Queue(
            maxsize=max(1, max_queue_chunks)
        )
        self._state_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._play_lock = asyncio.Lock()
        self._playout_task: asyncio.Task[None] | None = None
        self._stereo_pcm_tail = bytearray()

    async def play_file(self, path: Path) -> object:
        try:
            frames = _file_frames(path.read_bytes())
        except (OSError, AudioDecodeError):
            return _PlayResult(cancelled=False)
        return await self._play_frames(frames)

    async def play_wav_bytes(self, audio: bytes) -> object:
        try:
            frames = _file_frames(audio)
        except AudioDecodeError:
            return _PlayResult(cancelled=False)
        return await self._play_frames(frames)

    async def _play_frames(self, frames) -> object:
        # Behind queued speech, not between its chunks: the playout task takes
        # the play lock one chunk at a time, so a clip that grabbed it mid-line
        # alternated with the words ("working on i-i-i-it").
        while True:
            await self.wait_until_idle()
            await self._play_lock.acquire()
            if self._audio_queue.empty():
                break
            self._play_lock.release()
        try:
            self._mark_audio_start()
            self.cancelled = False
            self._stop_event.clear()
            self._resume_event.set()
            try:
                await self._send_frames(frames)
            finally:
                task = self._playout_task
                playing = bool(task is not None and not task.done())
                if playing:
                    self.active = True
                else:
                    self._mark_drain()
        finally:
            self._play_lock.release()
        return _PlayResult(cancelled=self.cancelled)

    async def write(self, pcm: bytes, turn_id: int) -> None:
        """Accept one kernel-owned TTS chunk into bounded WebRTC playout."""
        if not pcm:
            return
        async with self._write_lock:
            async with self._state_lock:
                accounting = self._begin_write(turn_id, len(pcm))
                if accounting is None:
                    return
                self._stereo_pcm_tail.extend(_mono_to_stereo_s16(pcm))
                complete = len(self._stereo_pcm_tail) // CORE_FRAME_BYTES * CORE_FRAME_BYTES
                frames = tuple(
                    bytes(self._stereo_pcm_tail[offset : offset + CORE_FRAME_BYTES])
                    for offset in range(0, complete, CORE_FRAME_BYTES)
                )
                del self._stereo_pcm_tail[:complete]
                item = _WebAudioChunk(turn_id, frames, complete // 2, accounting)
            await self._enqueue(item, accounting)

    async def finish_turn(self, turn_id: int) -> None:
        """Flush the final frame and wait for this segment's paced consumption."""
        async with self._write_lock:
            item = None
            async with self._state_lock:
                if self._closed or turn_id != self._epoch or self._stop_event.is_set():
                    return
                if self._stereo_pcm_tail:
                    producer_bytes = len(self._stereo_pcm_tail) // 2
                    frame = bytes(self._stereo_pcm_tail)
                    self._stereo_pcm_tail.clear()
                    frame += bytes(CORE_FRAME_BYTES - len(frame))
                    accounting = self._accounting
                    item = _WebAudioChunk(turn_id, (frame,), producer_bytes, accounting)
            if item is not None:
                await self._enqueue(item, item.accounting)
            await self.wait_until_idle()

    def _begin_write(self, turn_id: int, producer_bytes: int) -> AudioOutputAccounting | None:
        if turn_id <= self._cancelled_through:
            self._accounting.dropped_stale(producer_bytes)
            return None
        if turn_id < self._epoch:
            self._accounting.dropped_stale(producer_bytes)
            return None
        if turn_id > self._epoch:
            previous_turn = self._epoch
            self._epoch = turn_id
            self._accounting = AudioOutputAccounting(turn_id=turn_id)
            self.cancelled = False
            self._stop_event.clear()
            self._resume_event.set()
            self._stereo_pcm_tail.clear()
            if previous_turn >= 0:
                self._clear_pending_audio()
                self.stream.clear_output()
        accounting = self._accounting
        accounting.generated(producer_bytes)
        if self._closed or self._stop_event.is_set():
            accounting.dropped_stale(producer_bytes)
            return None
        return accounting

    async def _enqueue(self, item: _WebAudioChunk, accounting: AudioOutputAccounting) -> None:
        if not item.frames:
            return
        await self._audio_queue.put(item)
        async with self._state_lock:
            if self._closed or item.turn_id != self._epoch or self._stop_event.is_set():
                accounting.dropped_stale(item.producer_bytes)
                self._clear_pending_audio()
                return
            self._mark_audio_start()
            if self._playout_task is None or self._playout_task.done():
                self._playout_task = asyncio.create_task(
                    self._run_playout(),
                    name=f"web-playout-{item.turn_id}",
                )
        await asyncio.sleep(0)

    async def _run_playout(self) -> None:
        current = asyncio.current_task()
        try:
            while True:
                item = await self._audio_queue.get()
                try:
                    if self._can_play(item.turn_id):
                        async with self._play_lock:
                            if self._can_play(item.turn_id):
                                await self._send_frames(
                                    item.frames, turn_id=item.turn_id,
                                    accounting=item.accounting, producer_bytes=item.producer_bytes,
                                )
                finally:
                    self._audio_queue.task_done()

                async with self._state_lock:
                    if self._audio_queue.empty():
                        if self._playout_task is current:
                            self._playout_task = None
                        self._mark_drain()
                        return
        except asyncio.CancelledError:
            raise
        except CallEnded:
            # The browser hung up or the connection dropped mid-answer: the
            # rest of the queue has nowhere to go.
            self._mark_drain()
        finally:
            if self._playout_task is current:
                self._playout_task = None

    def _mark_audio_start(self) -> None:
        was_active = self.active
        self.active = True
        if not was_active and callable(self.on_audio_start):
            self.on_audio_start()

    def _mark_drain(self) -> None:
        was_active = self.active
        self.active = False
        if was_active and callable(self.on_drain):
            self.on_drain()

    def _can_play(self, turn_id: int) -> bool:
        return (
            not self._closed
            and turn_id == self._epoch
            and not self._stop_event.is_set()
        )

    def _clear_pending_audio(self) -> None:
        while not self._audio_queue.empty():
            self._audio_queue.get_nowait()
            self._audio_queue.task_done()

    async def discard_turn(self, turn_id: int, reason: str = "interrupted") -> None:
        async with self._state_lock:
            self._cancelled_through = max(self._cancelled_through, turn_id)
        await self.stop(reason)

    def accounting(self) -> AudioOutputAccounting:
        return self._accounting

    async def stop(self, reason: str = "interrupted") -> bool:
        del reason
        async with self._state_lock:
            was_active = (
                self.active
                or not self._audio_queue.empty()
                or bool(self._stereo_pcm_tail)
            )
            self.cancelled = True
            self._stop_event.set()
            self._resume_event.set()
            self._accounting.dropped_stale(len(self._stereo_pcm_tail) // 2)
            self._stereo_pcm_tail.clear()
            self._clear_pending_audio()
            self.stream.clear_output()
            self._mark_drain()
            return was_active

    async def pause(self, reason: str = "duck-on-vad") -> bool:
        del reason
        if not self.active or self._stop_event.is_set():
            return False
        self._resume_event.clear()
        return True

    async def resume(self, reason: str = "duck-release") -> bool:
        del reason
        if not self.active or self._stop_event.is_set():
            return False
        self._resume_event.set()
        return True

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        # Closing the host-side adapter drops audio that never crossed the
        # boundary, but preserves frames already handed to WebRTC. Explicit
        # interruption still calls ``stop`` and clears transport playback.
        async with self._state_lock:
            self.cancelled = True
            self._stop_event.set()
            self._resume_event.set()
            self._accounting.dropped_stale(len(self._stereo_pcm_tail) // 2)
            self._stereo_pcm_tail.clear()
            self._clear_pending_audio()
        task = self._playout_task
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self.active = False

    async def wait_until_idle(self) -> None:
        """Wait for accepted route audio to finish playout."""
        while True:
            task = self._playout_task
            if task is None:
                return
            await asyncio.shield(task)

    async def _send_frames(
        self, frames, *, turn_id: int | None = None,
        accounting: AudioOutputAccounting | None = None, producer_bytes: int = 0,
    ) -> None:
        remaining = producer_bytes
        for pcm in frames:
            await self._resume_event.wait()
            if self._stop_event.is_set() or (
                turn_id is not None and turn_id != self._epoch
            ):
                return
            consumed = await self._send_pcm(pcm, turn_id=turn_id)
            if accounting is not None and consumed:
                count = min(remaining, consumed // 2)
                accounting.written(count)
                remaining -= count

    async def _send_pcm(self, pcm: bytes, *, turn_id: int | None = None) -> int:
        consumed = 0
        for offset in range(0, len(pcm), CORE_FRAME_BYTES):
            chunk = pcm[offset : offset + CORE_FRAME_BYTES]
            if (
                not chunk
                or self._stop_event.is_set()
                or (turn_id is not None and turn_id != self._epoch)
            ):
                return consumed
            content_bytes = len(chunk)
            if len(chunk) < CORE_FRAME_BYTES:
                chunk += bytes(CORE_FRAME_BYTES - len(chunk))
            samples = len(chunk) // 4
            frame = AudioFrame(format="s16", layout="stereo", samples=samples)
            frame.planes[0].update(chunk)
            frame.sample_rate = CORE_RATE
            frame.pts = self._next_pts
            frame.time_base = Fraction(1, CORE_RATE)
            self._next_pts += samples
            send_played = getattr(self.stream, "send_played", None)
            if callable(send_played):
                if not await send_played(frame):
                    return consumed
            else:
                await self.stream.send(frame)
                if self._stop_event.is_set() or (turn_id is not None and turn_id != self._epoch):
                    return consumed
            consumed += content_bytes
        return consumed



def _pcm_bytes(milliseconds: int) -> int:
    return int(CORE_RATE * 2 * 2 * max(milliseconds, 0) / 1000)


def _gain_pcm_s16(pcm: bytes, gain: float) -> bytes:
    if gain == 1.0 or not pcm:
        return pcm
    samples = array("h")
    samples.frombytes(pcm)
    for index, sample in enumerate(samples):
        samples[index] = max(-32768, min(32767, round(sample * gain)))
    return samples.tobytes()


def _stereo_to_mono_s16(pcm: bytes) -> bytes:
    """Adapt WebRTC stereo s16le capture to the kernel's mono PCM contract."""
    samples = array("h")
    samples.frombytes(pcm[: len(pcm) - len(pcm) % 4])
    mono = array("h")
    mono.extend(
        (int(samples[index]) + int(samples[index + 1])) // 2 for index in range(0, len(samples), 2)
    )
    return mono.tobytes()


def _mono_to_stereo_s16(pcm: bytes) -> bytes:
    """Adapt kernel mono s16le output to WebRTC stereo PCM frames."""
    samples = array("h")
    samples.frombytes(pcm[: len(pcm) - len(pcm) % 2])
    stereo = array("h")
    for sample in samples:
        stereo.extend((sample, sample))
    return stereo.tobytes()


def _file_frames(audio: bytes) -> tuple[bytes, ...]:
    pcm = _mono_to_stereo_s16(decode_audio_bytes(audio))
    return tuple(pcm[offset:offset + CORE_FRAME_BYTES] for offset in range(0, len(pcm), CORE_FRAME_BYTES))
