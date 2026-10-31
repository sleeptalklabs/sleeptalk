"""Thin Discord transport adapter.

The transport contract surface for Discord: guild/channel lifecycle mapped
onto the session (connect on start, clean teardown on exit) and the
session's registry-built components (agent/STT/TTS/policy/meter + the
cascade route) injected into the packaged bot. Nothing here constructs a
provider or calls the agent.
"""

from __future__ import annotations

from typing import Any

import discord

from sleeptalk.discord import DiscordEnv, PiDiscordBot, load_opus
from sleeptalk.discord_check import DiscordCheckError
from sleeptalk.session import RealtimeSession

__all__ = [
    "DiscordTransport",
    "transport_summary",
]


class DiscordTransport:
    """Session-kernel transport for the Discord surface."""

    def __init__(self, config, env: dict[str, str]) -> None:
        self.config = config
        self.env = env

    async def run(self, session: Any) -> None:
        discord_env = DiscordEnv.from_mapping(self.config, self.env)
        load_opus(discord_env.opus_library or self.config.transport.opus_library)
        bot = PiDiscordBot(
            self.config,
            discord_env,
            policy=session.policy,
            realtime_session=RealtimeSession(session),
            stt_rest_timeout_seconds=getattr(
                self.config.stt, "rest_timeout_seconds", None
            ),
        )
        async with bot:
            try:
                await bot.start(discord_env.token)
            except discord.PrivilegedIntentsRequired as exc:
                raise DiscordCheckError(
                    "Discord refused a privileged intent this profile needs "
                    "(Message Content, for text summons or room echo). Enable it at "
                    "discord.com/developers/applications -> your app -> Bot -> "
                    "Privileged Gateway Intents, save, then re-run."
                ) from exc
            except discord.LoginFailure as exc:
                raise DiscordCheckError(
                    "Discord rejected this bot's token. In the Discord Developer Portal, "
                    "open the bot, Reset Token, and paste the new one in sleeptalk's settings."
                ) from exc


def transport_summary(config) -> dict[str, Any]:
    return {
        "guild_id": config.transport.guild_id,
        "voice_only": config.transport.voice_only,
        "native_voice_enabled": config.transport.native_voice_enabled,
    }
