from sleeptalk.discord.env import DiscordBotProfile, DiscordBotSettings, DiscordEnv
from sleeptalk.discord.helpers import (
    actor_id,
    format_error,
    load_opus,
    parse_allowed_channel_ids,
)

__all__ = [
    "DiscordBotProfile",
    "DiscordBotSettings",
    "DiscordEnv",
    "DiscordSessionKernel",
    "PiDiscordBot",
    "actor_id",
    "build_slash_group",
    "format_error",
    "load_opus",
    "parse_allowed_channel_ids",
]


def __getattr__(name: str):
    if name in {"DiscordSessionKernel", "PiDiscordBot", "build_slash_group"}:
        from sleeptalk.discord import bot as discord_bot

        return getattr(discord_bot, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
