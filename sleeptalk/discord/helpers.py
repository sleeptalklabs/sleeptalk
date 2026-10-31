from __future__ import annotations

import ctypes.util
from typing import Any

import discord
from discord.ext import voice_recv

from sleeptalk.logging_setup import redact


def format_error(exc: BaseException) -> str:
    """One readable line for an error, safe to print, trace, or post.

    Redacted: provider errors quote request URLs and headers, and this text
    reaches a Discord channel as well as the log.
    """
    message = redact(str(exc).strip())
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


def discord_pcm_bytes(milliseconds: int) -> int:
    return int(48000 * 2 * 2 * max(milliseconds, 0) / 1000)


def parse_allowed_channel_ids(raw: str) -> set[int]:
    ids: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        ids.add(int(part))
    return ids


def actor_id(user_id: int | str) -> str:
    clean = str(user_id).strip()
    if not clean:
        return ""
    return f"discord:{clean}"


def guild_voice_client(interaction: discord.Interaction) -> discord.VoiceClient | None:
    guild = interaction.guild
    if not guild:
        return None
    voice = guild.voice_client
    return voice if isinstance(voice, discord.VoiceClient) else None


async def connected_voice_client(
    bot: Any,
    interaction: discord.Interaction,
    *,
    connect_if_needed: bool,
) -> voice_recv.VoiceRecvClient | None:
    from sleeptalk.discord.access import access_for, audience_allowed

    target_channel = None
    user = interaction.user
    if isinstance(user, discord.Member) and user.voice and user.voice.channel:
        target_channel = user.voice.channel

    access = access_for(bot)
    if not access.operator(getattr(user, "id", None)):
        raise PermissionError("This Discord operator is not authorized.")
    if target_channel is not None and not access.audience(
        target_channel, bot_id=getattr(getattr(bot, "user", None), "id", None)
    ):
        raise PermissionError("The voice channel contains an unauthorized listener or is not allowed.")
    voice = guild_voice_client(interaction)
    if voice is not None and not audience_allowed(bot, voice):
        voice._sleeptalk_access_revoked = True
        voice.stop()
        await voice.disconnect(force=True)
        voice = None
    if isinstance(voice, voice_recv.VoiceRecvClient):
        if not connect_if_needed or not target_channel:
            return voice
        current_channel_id = getattr(getattr(voice, "channel", None), "id", None)
        if current_channel_id == target_channel.id:
            return voice
        await voice.disconnect(force=True)
        voice = None
    if voice:
        await voice.disconnect(force=True)
        voice = None
    if not connect_if_needed or not interaction.guild or not target_channel:
        return voice

    connected = await target_channel.connect(cls=voice_recv.VoiceRecvClient, self_deaf=False)
    if not audience_allowed(bot, connected):
        await connected.disconnect(force=True)
        raise PermissionError("Voice participants changed; authorize the channel before retrying.")
    return connected if isinstance(connected, voice_recv.VoiceRecvClient) else None


def load_opus(opus_library: str = "") -> None:
    if discord.opus.is_loaded():
        return

    from sleeptalk.sysdeps import LIBOPUS_PATHS

    candidates = [
        opus_library.strip(),
        ctypes.util.find_library("opus") or "",
        *LIBOPUS_PATHS,
        "libopus.so.0",
        "libopus.so",
    ]
    for candidate in candidates:
        if not candidate:
            continue
        try:
            discord.opus.load_opus(candidate)
        except (OSError, discord.opus.OpusError):
            continue
        if discord.opus.is_loaded():
            return

    raise SystemExit(
        "Discord live voice needs libopus. Set DISCORD_OPUS_LIBRARY to the full libopus path."
    )
