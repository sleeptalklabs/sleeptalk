"""Join/speak playback helpers for native Discord voice."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from sleeptalk.providers import collect_tts_audio
from sleeptalk.discord.helpers import format_error as _format_error
from sleeptalk.speech.common import SpeechError
from sleeptalk.discord_native.playback import DiscordPlaybackController
from sleeptalk.voice_loop.telemetry import VoiceLatencyTrace


async def _play_reply(
    bot: Any,
    playback: DiscordPlaybackController,
    reply: str,
    trace: VoiceLatencyTrace | None = None,
) -> None:
    if trace:
        trace.mark("tts_started", textChars=len(reply))
    try:
        audio = await collect_tts_audio(bot.tts, reply)
    except SpeechError as exc:
        if trace:
            trace.mark("tts_failed", error=_format_error(exc))
        raise
    if trace:
        trace.mark("tts_completed", audioFileBytes=len(audio))
        trace.mark("discord_playback_started")
    await _play_clip(playback, audio)
    if trace:
        trace.finish("discord_playback_completed")


async def _play_clip(
    playback: DiscordPlaybackController,
    clip: Path | bytes,
):
    if isinstance(clip, bytes):
        return await playback.play_wav_bytes(clip)
    return await playback.play_file(clip)
