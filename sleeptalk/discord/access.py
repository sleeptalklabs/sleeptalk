"""Explicit Discord identity permissions; configuration is the trust boundary."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sleeptalk.discord.helpers import parse_allowed_channel_ids


@dataclass(frozen=True)
class DiscordAccess:
    required: bool = True
    guild_id: int = 0
    channels: frozenset[int] = frozenset()
    operators: frozenset[int] = frozenset()
    speakers: frozenset[int] = frozenset()
    listeners: frozenset[int] = frozenset()

    @classmethod
    def from_surface(cls, surface: Any) -> DiscordAccess:
        def ids(name: str) -> frozenset[int]:
            return frozenset(parse_allowed_channel_ids(getattr(surface, name, "")))

        raw_guild = str(getattr(surface, "guild_id", "") or "").strip()
        if raw_guild and not raw_guild.isdigit():
            raise ValueError(
                f"transport.guild_id must be the numeric Discord server ID, not {raw_guild!r}. "
                "Copy it from Discord with Developer Mode on: right-click the server, Copy Server ID."
            )
        return cls(
            required=bool(getattr(surface, "authorization_required", True)),
            guild_id=int(raw_guild or 0),
            channels=ids("allowed_channel_ids"),
            operators=ids("operator_ids"),
            speakers=ids("speaker_ids"),
            listeners=ids("listener_ids"),
        )

    def validate(self) -> None:
        if self.required and not (self.guild_id and self.channels and self.operators):
            raise ValueError(
                "Discord authorization requires a guild, allowed_channel_ids, and "
                "operator_ids confirmed by the operator. Automatic pairing is unavailable. "
                "To let anyone in the server talk to the agent instead, set "
                "authorization_required = false in the profile's [transport]."
            )

    def operator(self, user_id: int | None) -> bool:
        return not self.required or user_id in self.operators

    def speaker(self, user_id: int | None) -> bool:
        return not self.required or user_id in self.operators | self.speakers

    def listener(self, user_id: int | None) -> bool:
        return not self.required or user_id in self.operators | self.speakers | self.listeners

    def channel(self, guild_id: int | None, channel_id: int | None) -> bool:
        if self.required:
            return bool(self.guild_id and guild_id == self.guild_id and channel_id in self.channels)
        # An open bot is still the configured server's: a public bot added to
        # someone else's server, or a DM, never reaches the agent.
        if self.guild_id and guild_id != self.guild_id:
            return False
        return not self.channels or channel_id in self.channels

    def audience(self, channel: Any, *, bot_id: int | None) -> bool:
        if not self.required:
            return self.channel(getattr(getattr(channel, "guild", None), "id", None), getattr(channel, "id", None))
        if not self.channel(getattr(getattr(channel, "guild", None), "id", None), getattr(channel, "id", None)):
            return False
        members = getattr(channel, "members", None)
        return members is not None and all(
            member.id == bot_id or self.listener(member.id) for member in members
        )


def access_for(bot: Any) -> DiscordAccess:
    access = getattr(bot, "access", None)
    if isinstance(access, DiscordAccess):
        return access
    return DiscordAccess.from_surface(getattr(getattr(bot, "profile", None), "discord", None))


def audience_allowed(bot: Any, voice: Any) -> bool:
    if getattr(voice, "_sleeptalk_access_revoked", False):
        return False
    return access_for(bot).audience(
        getattr(voice, "channel", None), bot_id=getattr(getattr(bot, "user", None), "id", None)
    )


def speaker_allowed(bot: Any, voice: Any, user: Any) -> bool:
    access = access_for(bot)
    if not (access.speaker(getattr(user, "id", None)) and audience_allowed(bot, voice)):
        return False
    # Only operators can answer a pending tool permission request. Other
    # authorized speakers wait until the operator resolves it.
    route = getattr(bot, "session_route", None)
    if access.required and getattr(route, "_pending_permission", None) is not None:
        return access.operator(getattr(user, "id", None))
    return True
