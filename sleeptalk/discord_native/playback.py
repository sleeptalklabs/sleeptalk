from __future__ import annotations

import asyncio
import contextlib
import io
import time
import wave
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import discord

from sleeptalk.discord.helpers import format_error as _format_error
from sleeptalk.discord_native.timeline import VoiceTimeline


class DiscordPlaybackError(RuntimeError):
    pass


@dataclass(frozen=True)
class PlaybackResult:
    path: Path
    completed: bool
    cancelled: bool
    elapsed_ms: int
    duration_ms: int

    @property
    def played_fraction(self) -> float:
        if self.completed or self.duration_ms <= 0:
            return 1.0 if self.completed else 0.0
        return min(max(self.elapsed_ms / self.duration_ms, 0.0), 1.0)


class AuthorizedAudioSource(discord.AudioSource):
    """Recheck the current audience on every player-thread frame read."""

    def __init__(self, source: discord.AudioSource, authorized: Callable[[], bool]) -> None:
        self.source = source
        self.authorized = authorized
        self.revoked = False

    def read(self) -> bytes:
        if self.revoked or not self.authorized():
            self.revoked = True
            return b""
        frame = self.source.read()
        if not self.authorized():
            self.revoked = True
            return b""
        return frame

    def is_opus(self) -> bool:
        return self.source.is_opus()

    def cleanup(self) -> None:
        self.source.cleanup()

    def discard(self) -> None:
        self.revoked = True
        discard = getattr(self.source, "discard", None)
        if callable(discard):
            discard()
        else:
            self.source.cleanup()


class DiscordPlaybackController:
    def __init__(
        self,
        voice: discord.VoiceClient,
        timeline: VoiceTimeline | None = None,
        *,
        authorized: Callable[[], bool] | None = None,
        on_started: Callable[[], None] | None = None,
        on_stopped: Callable[[], None] | None = None,
    ) -> None:
        self.voice = voice
        self.authorized = authorized
        self.timeline = timeline
        self.on_started = on_started
        self.on_stopped = on_stopped
        self.active = False
        self.cancelled = False
        self.last_result: PlaybackResult | None = None
        self._lock = asyncio.Lock()

    async def play_file(
        self,
        path: Path,
        *,
        on_started: Callable[[], None] | None = None,
    ) -> PlaybackResult:
        async with self._lock:
            if self.voice.is_playing():
                self._stop_playback_only()
            if not _voice_is_connected(self.voice):
                raise DiscordPlaybackError("Discord voice playback failed: voice client is not connected")

            loop = asyncio.get_running_loop()
            done = asyncio.Event()
            error: Exception | None = None
            self.cancelled = False
            duration_ms = _audio_duration_ms(path)

            def after(exc: Exception | None) -> None:
                nonlocal error
                error = exc
                loop.call_soon_threadsafe(done.set)

            source = discord.FFmpegOpusAudio(str(path))
            try:
                if self.authorized is not None:
                    source = AuthorizedAudioSource(source, self.authorized)
                self.voice.play(source, after=after)
            except discord.DiscordException as exc:
                self.active = False
                self.cancelled = False
                raise DiscordPlaybackError(f"Discord voice playback failed: {_format_error(exc)}") from exc

            self.active = True
            started_at = time.perf_counter()
            self._record("bot_playback_started", path.name)
            if self.on_started:
                self.on_started()
            if on_started:
                on_started()
            await done.wait()
            self.active = False
            if isinstance(source, AuthorizedAudioSource) and source.revoked:
                self.cancelled = True
            elapsed_ms = round((time.perf_counter() - started_at) * 1000)
            detail = "cancelled" if self.cancelled else path.name
            self._record("bot_playback_stopped", detail)
            if self.on_stopped:
                self.on_stopped()
            self.last_result = PlaybackResult(
                path=path,
                completed=not self.cancelled and error is None,
                cancelled=self.cancelled,
                elapsed_ms=elapsed_ms,
                duration_ms=duration_ms,
            )
            if error and not self.cancelled:
                raise DiscordPlaybackError(f"Discord voice playback failed: {_format_error(error)}")
            return self.last_result

    async def play_source(
        self,
        source: discord.AudioSource,
        *,
        on_started: Callable[[], None] | None = None,
        duration_ms: int = 0,
        detail: str = "stream",
    ) -> PlaybackResult:
        """Play a live AudioSource (e.g. streaming TTS) instead of a file."""
        async with self._lock:
            if self.voice.is_playing():
                self._stop_playback_only()
            if not _voice_is_connected(self.voice):
                raise DiscordPlaybackError("Discord voice playback failed: voice client is not connected")

            loop = asyncio.get_running_loop()
            done = asyncio.Event()
            error: Exception | None = None
            self.cancelled = False

            def after(exc: Exception | None) -> None:
                nonlocal error
                error = exc
                loop.call_soon_threadsafe(done.set)

            try:
                if self.authorized is not None:
                    source = AuthorizedAudioSource(source, self.authorized)
                self.voice.play(source, after=after)
            except discord.DiscordException as exc:
                self.active = False
                self.cancelled = False
                raise DiscordPlaybackError(f"Discord voice playback failed: {_format_error(exc)}") from exc

            self.active = True
            started_at = time.perf_counter()
            self._record("bot_playback_started", detail)
            if self.on_started:
                self.on_started()
            if on_started:
                on_started()
            await done.wait()
            self.active = False
            if isinstance(source, AuthorizedAudioSource) and source.revoked:
                self.cancelled = True
            elapsed_ms = round((time.perf_counter() - started_at) * 1000)
            stopped_detail = "cancelled" if self.cancelled else detail
            self._record("bot_playback_stopped", stopped_detail)
            if self.on_stopped:
                self.on_stopped()
            self.last_result = PlaybackResult(
                path=Path("<stream>"),
                completed=not self.cancelled and error is None,
                cancelled=self.cancelled,
                elapsed_ms=elapsed_ms,
                duration_ms=duration_ms,
            )
            if error and not self.cancelled:
                raise DiscordPlaybackError(f"Discord voice playback failed: {_format_error(error)}")
            return self.last_result

    async def play_wav_bytes(
        self,
        audio: bytes,
        *,
        on_started: Callable[[], None] | None = None,
    ) -> PlaybackResult:
        """Pipe an in-memory WAV to ffmpeg; no synthesized audio touches disk."""

        duration_ms = _wav_bytes_duration_ms(audio)
        source = discord.FFmpegOpusAudio(io.BytesIO(audio), pipe=True)
        return await self.play_source(
            source,
            on_started=on_started,
            duration_ms=duration_ms,
            detail="memory-wav",
        )

    async def stop(self, reason: str = "interrupted") -> bool:
        if not self.voice.is_playing() and not self.active and not _voice_is_paused(self.voice):
            return False
        self.cancelled = True
        self._record("playback_cancelled", reason)
        # Drain the current source's buffered frames SYNCHRONOUSLY. The
        # player thread would cleanup() the source eventually, but on a
        # barge-in 'eventually' is audible: up to a frame of stale audio
        # would still play. discard() (our streaming source) clears the
        # buffer under its lock; cleanup() is the ffmpeg/file fallback.
        player = getattr(self.voice, "_player", None)
        source = getattr(player, "source", None)
        for method_name in ("discard", "cleanup"):
            method = getattr(source, method_name, None)
            if callable(method):
                with contextlib.suppress(Exception):
                    method()
                break
        if self.voice.is_playing() or _voice_is_paused(self.voice):
            self._stop_playback_only()
        return True

    async def pause(self, reason: str = "duck-on-vad") -> bool:
        """Pause in-flight playback (duck-on-VAD: the reversible barge-in cue).
        Returns False when nothing is playing or the voice client can't pause."""
        if not self.active or not self.voice.is_playing():
            return False
        pause = getattr(self.voice, "pause", None)
        if not callable(pause):
            return False
        pause()
        self._record("playback_ducked", reason)
        return True

    async def resume(self, reason: str = "duck-release") -> bool:
        """Resume a ducked playback. Returns False unless actually paused."""
        if not self.active or not _voice_is_paused(self.voice):
            return False
        self.voice.resume()
        self._record("playback_resumed", reason)
        return True

    def _record(self, event_type: str, detail: str = "") -> None:
        if self.timeline:
            self.timeline.record(event_type, detail)

    def _stop_playback_only(self) -> None:
        stop_playing = getattr(self.voice, "stop_playing", None)
        if callable(stop_playing):
            stop_playing()
        else:
            self.voice.stop()


def _voice_is_connected(voice: discord.VoiceClient) -> bool:
    is_connected = getattr(voice, "is_connected", None)
    return bool(is_connected()) if callable(is_connected) else True


def _voice_is_paused(voice: discord.VoiceClient) -> bool:
    is_paused = getattr(voice, "is_paused", None)
    return bool(is_paused()) if callable(is_paused) else False


def _audio_duration_ms(path: Path) -> int:
    try:
        with wave.open(str(path), "rb") as wav:
            frame_rate = wav.getframerate()
            if frame_rate <= 0:
                return 0
            return round(wav.getnframes() / frame_rate * 1000)
    except (OSError, EOFError, wave.Error):
        return 0


def _wav_bytes_duration_ms(audio: bytes) -> int:
    try:
        with wave.open(io.BytesIO(audio), "rb") as wav:
            frame_rate = wav.getframerate()
            if frame_rate <= 0:
                return 0
            return round(wav.getnframes() / frame_rate * 1000)
    except (EOFError, OSError, wave.Error):
        return 0
