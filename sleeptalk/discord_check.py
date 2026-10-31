"""Discord validation for `sleeptalk doctor` and the setup wizard.

Three live checks, all fix-stating:

1. Token: GET /users/@me with the bot token -> echo the bot's name back.
2. Guild: GET /users/@me/guilds -> confirm the bot joined transport.guild_id,
   else print an invite URL with the permissions the runtime needs.
3. Intents: a one-shot gateway IDENTIFY carrying the *exact* intents the
   runtime will request (sourced from config via requested_intents_value),
   watching for close code 4013 (invalid intent — a sleeptalk-side bug) vs
   4014 (disallowed intent — the Developer Portal toggle is off, fix-stating).

CAVEAT — session start limit: every IDENTIFY consumes one session start from
the bot's daily bucket (GET /gateway/bot -> session_start_limit; default
1000/24h, resets daily). Doctor and init runs are rare, so this is fine — but
never call probe_gateway_intents in a retry loop or at every `sleeptalk run`
boot. This is also why the probe is NOT wired into run-time connectivity
checks.
"""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass
from urllib.parse import urlencode

import aiohttp

API = "https://discord.com/api/v10"
GATEWAY_URL = "wss://gateway.discord.gg"

# Intent bit values (stable Discord constants).
INTENT_BITS = {
    "guilds": 1 << 0,
    "guild_members": 1 << 1,  # privileged
    "guild_bans": 1 << 2,
    "guild_expressions": 1 << 3,
    "guild_integrations": 1 << 4,
    "guild_webhooks": 1 << 5,
    "guild_invites": 1 << 6,
    "guild_voice_states": 1 << 7,
    "guild_presences": 1 << 8,  # privileged
    "guild_messages": 1 << 9,
    "guild_message_reactions": 1 << 10,
    "guild_message_typing": 1 << 11,
    "direct_messages": 1 << 12,
    "direct_message_reactions": 1 << 13,
    "direct_message_typing": 1 << 14,
    "message_content": 1 << 15,  # privileged
    "guild_scheduled_events": 1 << 16,
    "auto_moderation_configuration": 1 << 20,
    "auto_moderation_execution": 1 << 21,
}
PRIVILEGED_INTENTS = ("guild_members", "guild_presences", "message_content")

# discord.py 2.x Intents.default() = all non-privileged intents. Used only
# when discord.py is not importable (standalone package install); when it is,
# requested_intent_names() mirrors PiDiscordBot exactly.
_FALLBACK_DEFAULT_NAMES = [
    name for name in INTENT_BITS if name not in PRIVILEGED_INTENTS
]

# Permissions the voice runtime needs, for invite URLs.
_PERMISSION_VIEW_CHANNEL = 1 << 10
_PERMISSION_SEND_MESSAGES = 1 << 11
_PERMISSION_READ_HISTORY = 1 << 16
_PERMISSION_CONNECT = 1 << 20
_PERMISSION_SPEAK = 1 << 21
_PERMISSION_USE_VAD = 1 << 25
INVITE_PERMISSIONS = (
    _PERMISSION_VIEW_CHANNEL
    | _PERMISSION_SEND_MESSAGES
    | _PERMISSION_READ_HISTORY
    | _PERMISSION_CONNECT
    | _PERMISSION_SPEAK
    | _PERMISSION_USE_VAD
)


class DiscordCheckError(RuntimeError):
    """One Discord validation failure, with the fix in the message."""


@dataclass(frozen=True)
class BotIdentity:
    name: str
    id: str


@dataclass(frozen=True)
class Guild:
    id: str
    name: str


class InvalidIntentError(DiscordCheckError):
    """Close 4013: intent bits Discord rejects outright (sleeptalk-side bug)."""


class DisallowedIntentsError(DiscordCheckError):
    """Close 4014: privileged intents requested but not enabled in the portal."""

    def __init__(self, privileged_names: list[str]) -> None:
        self.privileged_names = privileged_names
        toggles = ", ".join(privileged_names) or "(none sleeptalk requests)"
        super().__init__(
            "Discord refused the bot's gateway intents (close 4014: disallowed "
            f"intents). Privileged intent(s) requested: {toggles}. Fix: "
            "discord.com/developers/applications -> your app -> Bot -> "
            "Privileged Gateway Intents -> enable the matching toggle(s), save, "
            "and re-run doctor."
        )


def needs_message_content(config) -> bool:
    """Voice needs no privileged intent. Message Content is read only for
    text summons (``voice_only``) and for room echo posts from other bots
    (``participation_mode`` other than conversationalist)."""
    if config is None:
        return False
    transport = getattr(config, "transport", None)
    agent = getattr(config, "agent", None)
    voice_only = bool(getattr(transport, "voice_only", False))
    mode = (getattr(agent, "participation_mode", "") or "conversationalist").strip()
    return voice_only or mode != "conversationalist"


def runtime_discord_intents(config=None):
    """The intents PiDiscordBot requests for this config: discord.py defaults
    plus voice_states, plus message_content only when a feature reads text."""
    import discord

    intents = discord.Intents.default()
    intents.voice_states = True
    intents.message_content = needs_message_content(config)
    return intents


def _compute_intents(config) -> tuple[int, list[str]]:
    try:
        intents = runtime_discord_intents(config)
        return intents.value, [name for name, enabled in intents if enabled]
    except ImportError:
        names = list(_FALLBACK_DEFAULT_NAMES)
        if needs_message_content(config):
            names.append("message_content")
        return sum(INTENT_BITS[name] for name in names), names


def requested_intent_names(config) -> list[str]:
    """The exact intents the runtime bot will request.

    Mirrors ``PiDiscordBot``: discord.py default intents + voice_states +
    message_content. When discord.py is importable we compute from it so
    doctor tracks the runtime's discord.py version; otherwise we fall back
    to the pinned 2.x default set above.
    """
    return _compute_intents(config)[1]


def requested_intents_value(config) -> int:
    return _compute_intents(config)[0]


def requested_intents_value_for(*, voice_only: bool = False) -> int:
    """Value without a loaded config (wizard: transport fields not set yet)."""
    from sleeptalk.config import SleeptalkConfig

    config = SleeptalkConfig()
    config.transport.voice_only = voice_only
    return requested_intents_value(config)


# discord.py attribute names for the privileged intents (INTENT_BITS keys
# differ: guild_members/guild_presences vs discord.py's members/presences).
_PRIVILEGED_ALIASES = {
    "guild_members": ("members",),
    "guild_presences": ("presences",),
    "message_content": (),
}


def privileged_requested(config) -> list[str]:
    names = set(requested_intent_names(config))
    return [
        canonical
        for canonical, aliases in _PRIVILEGED_ALIASES.items()
        if names & ({canonical} | set(aliases))
    ]


def invite_url(client_id: str) -> str:
    return "https://discord.com/oauth2/authorize?" + urlencode(
        {
            "client_id": client_id,
            "scope": "bot applications.commands",
            "permissions": str(INVITE_PERMISSIONS),
        }
    )


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bot {token.strip()}"}


def _translate_connect_error(exc: BaseException) -> DiscordCheckError:
    """Turn aiohttp/TLS failures into a fix-stating DiscordCheckError."""
    if isinstance(exc, aiohttp.ClientConnectorCertificateError) or (
        "CERTIFICATE_VERIFY_FAILED" in str(exc)
    ):
        major, minor = sys.version_info[:2]
        certs = f"/Applications/Python {major}.{minor}/Install Certificates.command"
        return DiscordCheckError(
            "Could not verify HTTPS to discord.com — this Python is missing CA "
            f"certificates. On macOS with the python.org installer, run: {certs} "
            "Then re-run sleeptalk init."
        )
    return DiscordCheckError(
        "Could not reach discord.com. Check the network and retry."
    )


async def fetch_bot(token: str) -> BotIdentity:
    """Validate the bot token; returns display name and user id."""
    if not token.strip():
        raise DiscordCheckError(
            "Discord bot token is empty — copy one from "
            "discord.com/developers/applications -> your app -> Bot -> Reset Token."
        )
    try:
        async with aiohttp.ClientSession() as session, session.get(
            f"{API}/users/@me",
            headers=_headers(token),
            timeout=aiohttp.ClientTimeout(total=8),
        ) as response:
            if response.status == 401:
                raise DiscordCheckError(
                    "Discord rejected the bot token (401 Unauthorized) — generate a "
                    "fresh one at discord.com/developers/applications -> your app -> "
                    "Bot -> Reset Token, and update .env."
                )
            if response.status != 200:
                raise DiscordCheckError(
                    f"Discord token check failed (HTTP {response.status}) — is "
                    "discord.com reachable from here?"
                )
            payload = await response.json()
    except DiscordCheckError:
        raise
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        raise _translate_connect_error(exc) from exc
    if not payload.get("bot"):
        raise DiscordCheckError(
            "This token belongs to a user account, not a bot — sleeptalk needs a "
            "bot token (discord.com/developers/applications -> your app -> Bot)."
        )
    return BotIdentity(
        name=str(payload.get("username", "unknown")),
        id=str(payload.get("id", "")),
    )


async def check_token(token: str) -> str:
    """Validate the bot token; returns the bot's display name."""
    return (await fetch_bot(token)).name


async def list_guilds(token: str) -> list[Guild]:
    """Guilds the bot has already joined."""
    try:
        async with aiohttp.ClientSession() as session, session.get(
            f"{API}/users/@me/guilds",
            headers=_headers(token),
            timeout=aiohttp.ClientTimeout(total=8),
        ) as response:
            if response.status != 200:
                raise DiscordCheckError(
                    f"Discord guild check failed (HTTP {response.status})."
                )
            payload = await response.json()
    except DiscordCheckError:
        raise
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        raise _translate_connect_error(exc) from exc
    return [
        Guild(id=str(guild.get("id", "")), name=str(guild.get("name") or guild.get("id")))
        for guild in payload
        if guild.get("id")
    ]


async def check_guild_membership(token: str, guild_id: str, client_id: str) -> None:
    """Confirm the bot is a member of the configured guild."""
    if not guild_id.strip():
        return
    bot = await fetch_bot(token)
    if guild_id.strip() == bot.id:
        raise DiscordCheckError(
            f"{guild_id} is this bot's user id, not a server id. "
            "Pick the guild from `sleeptalk connect hermes` or copy the server id "
            "(right-click the server -> Copy Server ID)."
        )
    guilds = await list_guilds(token)
    if any(guild.id == guild_id.strip() for guild in guilds):
        return
    invite = invite_url(client_id) if client_id.strip() else invite_url("YOUR_CLIENT_ID")
    names = ", ".join(f"{guild.name} ({guild.id})" for guild in guilds) or "none"
    raise DiscordCheckError(
        f"The bot has not joined guild {guild_id} — it is in: {names}. "
        f"Invite it with: {invite}"
    )


async def probe_gateway_intents(
    token: str, intents_value: int, *, timeout: float = 10.0
) -> None:
    """One-shot IDENTIFY to prove the requested intents are allowed.

    Consumes one daily session start — see the module docstring caveat.
    Raises InvalidIntentError on close 4013, DisallowedIntentsError on 4014.
    """
    identify = {
        "op": 2,
        "d": {
            "token": token.strip(),
            "intents": intents_value,
            "properties": {
                "os": "sleeptalk-doctor",
                "browser": "sleeptalk-doctor",
                "device": "sleeptalk-doctor",
            },
        },
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(
                f"{GATEWAY_URL}/?v=10&encoding=json",
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as ws:
                await ws.send_str(json.dumps(identify))
                async for message in ws:
                    if message.type == aiohttp.WSMsgType.CLOSED:
                        break
                    if message.type != aiohttp.WSMsgType.TEXT:
                        continue
                    payload = json.loads(message.data)
                    if payload.get("op") == 0 and payload.get("t") == "READY":
                        return
                _raise_for_close(ws.close_code)
    except asyncio.TimeoutError as exc:
        raise DiscordCheckError(
            "Discord gateway intent probe timed out — the gateway did not "
            "confirm READY; re-run doctor or check discordstatus.com."
        ) from exc
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        raise _translate_connect_error(exc) from exc
    raise DiscordCheckError(
        "Discord gateway closed the intent probe without a close code — re-run "
        "doctor or check discordstatus.com."
    )


def _raise_for_close(close_code: int | None) -> None:
    if close_code == 4013:
        raise InvalidIntentError(
            "Discord rejected the requested intents outright (close 4013: "
            "invalid intent). This is a sleeptalk-side bug or a discord.py "
            "version mismatch, not a portal setting — please report it with "
            "this message."
        )
    if close_code == 4014:
        # Names are filled in by run_discord_ladder, which has the config.
        raise DisallowedIntentsError([])
    raise DiscordCheckError(
        f"Discord gateway closed the intent probe (code {close_code}) — re-run "
        "doctor or check discordstatus.com."
    )


async def run_discord_ladder(config, env: dict[str, str]) -> tuple[list[str], list[str]]:
    """Token -> guild -> intents, all failures fix-stating."""
    token = env.get(config.transport.token_env, "")
    oks: list[str] = []
    problems: list[str] = []
    try:
        bot_name = await check_token(token)
    except DiscordCheckError as exc:
        return oks, [str(exc)]
    oks.append(f"Discord token valid — bot: {bot_name}")
    try:
        await check_guild_membership(
            token, config.transport.guild_id, config.transport.client_id
        )
        if config.transport.guild_id.strip():
            oks.append(f"bot is a member of guild {config.transport.guild_id}")
    except DiscordCheckError as exc:
        problems.append(str(exc))
    try:
        await probe_gateway_intents(token, requested_intents_value(config))
        oks.append("gateway accepted the runtime's intents")
    except DisallowedIntentsError as exc:
        problems.append(str(DisallowedIntentsError(privileged_requested(config))))
    except DiscordCheckError as exc:
        problems.append(str(exc))
    return oks, problems


@dataclass(frozen=True)
class ApplicationIdentity:
    id: str
    name: str
    invite_url: str


@dataclass(frozen=True)
class VoiceChannel:
    id: str
    name: str
    eligible: bool
    missing_permissions: tuple[str, ...]


async def _rest_get(token: str, path: str):
    if not token.strip():
        raise DiscordCheckError("Discord bot token is missing. Supply it through private setup.")
    try:
        async with aiohttp.ClientSession() as session, session.get(
            f"{API}{path}", headers=_headers(token), timeout=aiohttp.ClientTimeout(total=8)
        ) as response:
            if response.status != 200:
                raise DiscordCheckError(f"Discord discovery failed (HTTP {response.status}). Check the bot token and server access.")
            return await response.json()
    except DiscordCheckError:
        raise
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        raise _translate_connect_error(exc) from exc


async def fetch_application(token: str) -> ApplicationIdentity:
    """Derive application identity from REST authentication, never token text."""
    payload = await _rest_get(token, "/applications/@me")
    application_id = str(payload.get("id", ""))
    if not application_id.isdecimal():
        raise DiscordCheckError("Discord returned no application identity for this bot.")
    return ApplicationIdentity(application_id, str(payload.get("name", "")), invite_url(application_id))


def _channel_permissions(guild_id: str, member_id: str, role_ids: set[str], roles: list[dict], channel: dict) -> int:
    everyone = next((role for role in roles if str(role["id"]) == guild_id), None)
    if everyone is None:
        raise DiscordCheckError("Discord did not return the server's default role permissions.")
    permissions = int(everyone["permissions"])
    for role in roles:
        if str(role["id"]) in role_ids:
            permissions |= int(role["permissions"])
    if permissions & (1 << 3):  # Discord Administrator bypasses overwrites.
        return INVITE_PERMISSIONS
    overwrites = channel.get("permission_overwrites", [])
    for overwrite in overwrites:
        if str(overwrite["id"]) == guild_id and overwrite["type"] == 0:
            permissions = (permissions & ~int(overwrite["deny"])) | int(overwrite["allow"])
    denied = allowed = 0
    for overwrite in overwrites:
        if overwrite["type"] == 0 and str(overwrite["id"]) in role_ids:
            denied |= int(overwrite["deny"])
            allowed |= int(overwrite["allow"])
    permissions = (permissions & ~denied) | allowed
    for overwrite in overwrites:
        if overwrite["type"] == 1 and str(overwrite["id"]) == member_id:
            permissions = (permissions & ~int(overwrite["deny"])) | int(overwrite["allow"])
    return permissions


async def list_voice_channels(token: str, guild_id: str) -> list[VoiceChannel]:
    """One bounded REST discovery pass; no Gateway IDENTIFY or server selection."""
    if not guild_id.isdecimal():
        raise DiscordCheckError("Select a Discord server before discovering voice channels.")
    bot = await fetch_bot(token)
    member = await _rest_get(token, f"/guilds/{guild_id}/members/{bot.id}")
    roles = await _rest_get(token, f"/guilds/{guild_id}/roles")
    channels = await _rest_get(token, f"/guilds/{guild_id}/channels")
    role_ids = {str(role_id) for role_id in member.get("roles", [])}
    required = {"view_channel": _PERMISSION_VIEW_CHANNEL, "connect": _PERMISSION_CONNECT,
                "speak": _PERMISSION_SPEAK, "use_voice_activity": _PERMISSION_USE_VAD}
    result = []
    for channel in channels:
        if channel.get("type") != 2:
            continue
        permissions = _channel_permissions(guild_id, bot.id, role_ids, roles, channel)
        missing = tuple(name for name, bit in required.items() if not permissions & bit)
        result.append(VoiceChannel(str(channel["id"]), str(channel.get("name", "")), not missing, missing))
    return result
