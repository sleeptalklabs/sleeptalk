"""Incremental PCM playback source for Discord audio output."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

import discord


class StreamPCMAudioSource(discord.AudioSource):
    """Discord AudioSource fed incrementally with 48kHz stereo PCM.

    The voice player thread calls read() every 20ms. Underruns return
    silence so playback stays alive while network audio is in flight.
    """

    FRAME_BYTES = 3840  # 20ms @ 48kHz stereo s16le

    def __init__(
        self, underrun_timeout: float | None = None, *,
        on_read: Callable[[int], None] | None = None,
        on_buffer_change: Callable[[], None] | None = None,
    ) -> None:
        self._buf = bytearray()
        self._lock = threading.Lock()
        self._finished = False
        self._discarded = False
        self._on_read = on_read
        self._on_buffer_change = on_buffer_change
        # Silence watchdog: if nothing is fed for this long, end the stream
        # instead of playing silence forever (a zombie stream gates the mic
        # and shows the bot as 'speaking' with no audio).
        self._underrun_timeout = underrun_timeout
        self._last_feed = time.monotonic()

    @property
    def buffered_bytes(self) -> int:
        with self._lock:
            return len(self._buf)

    @property
    def buffered_ms(self) -> float:
        return self.buffered_bytes / (self.FRAME_BYTES / 20)

    def feed(self, pcm: bytes) -> bool:
        """Append PCM for the player thread. Returns False when the source
        has been discarded (response cancelled) — late frames from a
        cancelled epoch are rejected, not queued."""
        with self._lock:
            if self._discarded:
                return False
            self._buf.extend(pcm)
            self._last_feed = time.monotonic()
            return True

    def discard(self) -> None:
        """Synchronously drop all buffered frames and refuse further feeds.
        Called on response cancellation (barge-in) so already-queued but
        not-yet-played audio is gone NOW, not whenever the player thread
        gets around to cleanup()."""
        with self._lock:
            self._buf.clear()
            self._discarded = True
        if self._on_buffer_change is not None:
            self._on_buffer_change()

    def finish(self) -> None:
        with self._lock:
            self._finished = True
        if self._on_buffer_change is not None:
            self._on_buffer_change()

    def read(self) -> bytes:
        with self._lock:
            if self._discarded:
                return b""  # cancelled epoch: end playback immediately
            if len(self._buf) >= self.FRAME_BYTES:
                frame = bytes(self._buf[: self.FRAME_BYTES])
                del self._buf[: self.FRAME_BYTES]
            elif self._finished:
                frame = bytes(self._buf)
                self._buf.clear()
                if not frame:
                    return b""
            elif (
                self._underrun_timeout is not None
                and time.monotonic() - self._last_feed > self._underrun_timeout
            ):
                self._finished = True
                return b""  # starved too long — end playback
            else:
                return b"\x00" * self.FRAME_BYTES
        if self._on_read is not None:
            self._on_read(len(frame))
        if self._on_buffer_change is not None:
            self._on_buffer_change()
        return frame.ljust(self.FRAME_BYTES, b"\x00")

    def is_opus(self) -> bool:
        return False

    def cleanup(self) -> None:
        with self._lock:
            self._buf.clear()
            self._finished = True
            self._discarded = True
        if self._on_buffer_change is not None:
            self._on_buffer_change()
