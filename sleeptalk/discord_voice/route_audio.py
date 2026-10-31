"""Discord route audio output boundary and PCM normalization."""
from __future__ import annotations

import audioop  # noqa: DEP002 - Discord's PCM bridge still runs on Python 3.11
import asyncio
import contextlib
from pathlib import Path

from sleeptalk.discord_native.playback import DiscordPlaybackController
from sleeptalk.stream_tts import StreamPCMAudioSource
from sleeptalk.media_audio import AudioDecodeError, decode_audio_bytes
from sleeptalk.routes.audio_output import AudioOutputAccounting, TurnPlaybackError

class DiscordRouteAudioOutput:
    """Route audio-output boundary backed by native Discord playback."""

    MAX_BUFFER_MS = 500
    MAX_BUFFER_BYTES = StreamPCMAudioSource.FRAME_BYTES * (MAX_BUFFER_MS // 20)

    def __init__(self, playback: DiscordPlaybackController, *, on_write=None, on_read=None) -> None:
        self.playback = playback
        self.on_write = on_write
        self.on_read = on_read
        self._closed = False
        self._latest_turn = -1
        self._cancelled_through = -1
        self._state_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._accounting = AudioOutputAccounting(turn_id=-1)
        self._source: StreamPCMAudioSource | None = None
        self._draining_source: StreamPCMAudioSource | None = None
        self._play_task: asyncio.Task | None = None
        self._buffer_changed: asyncio.Event | None = None
        self._notified_turn = -1

    @property
    def buffered_ms(self) -> float:
        return sum(
            source.buffered_ms
            for source in (self._source, self._draining_source)
            if source is not None
        )

    async def write(self, pcm: bytes, turn_id: int) -> None:
        async with self._write_lock:
            stream_pcm = _discord_stream_pcm(pcm)
            byte_count = len(stream_pcm) // 2
            prior_task = None
            async with self._state_lock:
                if self._closed or turn_id <= self._cancelled_through:
                    self._accounting.dropped_stale(byte_count)
                    return
                if turn_id < self._latest_turn:
                    self._accounting.dropped_stale(byte_count)
                    return
                if turn_id != self._latest_turn:
                    prior_task = self._detach_current_source(finish=True)
            if prior_task is not None:
                try:
                    with contextlib.suppress(asyncio.CancelledError):
                        await prior_task
                finally:
                    self._draining_source = None
            async with self._state_lock:
                if self._closed or turn_id <= self._cancelled_through:
                    self._accounting.dropped_stale(byte_count)
                    return
                if turn_id != self._latest_turn:
                    self._latest_turn = turn_id
                    self._accounting = AudioOutputAccounting(turn_id=turn_id)
                if self._source is None:
                    accounting = self._accounting
                    loop = asyncio.get_running_loop()
                    changed = asyncio.Event()
                    on_read = self.on_read

                    def consumed(count: int) -> None:
                        accounting.written(count // 2)
                        if on_read is not None:
                            with contextlib.suppress(RuntimeError):
                                loop.call_soon_threadsafe(on_read, turn_id)

                    def wake_writer() -> None:
                        # Discord may destroy its source after the event loop closes.
                        with contextlib.suppress(RuntimeError):
                            loop.call_soon_threadsafe(changed.set)

                    self._source = StreamPCMAudioSource(
                        on_read=consumed,
                        on_buffer_change=wake_writer,
                    )
                    self._buffer_changed = changed
                    self._play_task = None
                source = self._source
                changed = self._buffer_changed
                self._accounting.generated(byte_count)
            assert changed is not None
            for offset in range(0, len(stream_pcm), StreamPCMAudioSource.FRAME_BYTES):
                frame = stream_pcm[offset : offset + StreamPCMAudioSource.FRAME_BYTES]
                while True:
                    changed.clear()
                    async with self._state_lock:
                        if self._closed or turn_id <= self._cancelled_through or source is not self._source:
                            self._accounting.dropped_stale((len(stream_pcm) - offset) // 2)
                            return
                        if self._play_task is not None and self._play_task.done():
                            self._cancelled_through = max(self._cancelled_through, turn_id)
                            task = self._detach_current_source(finish=False)
                            assert task is not None
                            task.result()
                            raise TurnPlaybackError("Discord playback ended before audio delivery finished")
                        if source.buffered_bytes + len(frame) <= self.MAX_BUFFER_BYTES:
                            if not source.feed(frame):
                                self._accounting.dropped_stale((len(stream_pcm) - offset) // 2)
                                return
                            if self.on_write is not None and self._notified_turn != turn_id:
                                self._notified_turn = turn_id
                                self.on_write(turn_id)
                            if self._play_task is None:
                                self._play_task = asyncio.create_task(
                                    self.playback.play_source(source, detail="route-stream")
                                )
                                self._play_task.add_done_callback(lambda _task: changed.set())
                            break
                    await changed.wait()

    async def play_file(self, path: Path, *, turn_id: int) -> bool:
        """Play a narration clip inside the turn's own audio stream.

        Fed through write() rather than the playback controller's play_file:
        that one stops whatever is playing to take the device, which would
        cut off an answer already streaming. Here the clip is one more chunk
        in the turn's persistent source, so it queues behind spoken audio and
        is fenced by the same cancellation. Returns whether it was accepted.
        """
        try:
            audio = decode_audio_bytes(path.read_bytes())
        except (OSError, AudioDecodeError):
            return False
        if not audio:
            return False
        before = self._accounting.bytes_generated if self._accounting.turn_id == turn_id else 0
        await self.write(audio, turn_id)
        accounting = self._accounting
        return (
            accounting.turn_id == turn_id
            and turn_id > self._cancelled_through
            and accounting.bytes_generated > before
        )

    async def discard_turn(self, turn_id: int, reason: str = "interrupted") -> None:
        """Fence and drain a cancelled response before stopping Discord.

        The fence is monotonic: after turn N is discarded, delayed writes for
        N or any older turn are rejected even if they race transport cleanup.
        """
        async with self._state_lock:
            if turn_id <= self._cancelled_through:
                return
            self._cancelled_through = max(self._cancelled_through, turn_id)
            task = (
                self._detach_current_source(finish=False)
                if self._latest_turn <= turn_id
                else None
            )
        await self.playback.stop(reason)
        if task is not None and task is not asyncio.current_task():
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def finish_turn(self, turn_id: int) -> None:
        async with self._write_lock:
            async with self._state_lock:
                if turn_id != self._latest_turn or turn_id <= self._cancelled_through:
                    return
                task = self._detach_current_source(finish=True)
            if task is not None:
                try:
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
                finally:
                    self._draining_source = None

    def _detach_current_source(self, *, finish: bool) -> asyncio.Task | None:
        source = self._source
        task = self._play_task
        if source is not None:
            if finish:
                source.finish()
                self._draining_source = source
            else:
                source.discard()
        if not finish and self._draining_source is not None:
            self._draining_source.discard()
            self._draining_source = None
        self._source = None
        self._play_task = None
        self._buffer_changed = None
        return task

    def accounting(self) -> AudioOutputAccounting:
        return self._accounting

    async def close(self) -> None:
        async with self._state_lock:
            self._closed = True
            turn_id = self._latest_turn
        if turn_id >= 0:
            await self.discard_turn(turn_id, "session-closed")
        else:
            await self.playback.stop("session-closed")


def _discord_stream_pcm(pcm: bytes) -> bytes:
    """Convert canonical mono PCM to Discord's stereo device format."""
    return audioop.tostereo(pcm, 2, 1.0, 1.0)
