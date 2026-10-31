from __future__ import annotations

import contextlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sleeptalk.agents.pi_rpc import PiRPCError
from sleeptalk.discord.helpers import format_error
from sleeptalk.routes.base import TurnOutcome
from sleeptalk.providers import collect_tts_audio

_STATUS_QUERY_RE = re.compile(
    r"\b(what are you (doing|working on)|what'?s (taking (so long|forever)|happening|going on|the status|the hold ?up)|are you (still )?(working|there|stuck|ok|okay)|how'?s it (going|coming)|any progress|status( update| check)?|give me a status|where are you at?)\b",
    re.IGNORECASE,
)

_VOICE_TURN_MARKER = "sleeptalk-voice/1"

_VOICE_ECHO_RE = re.compile(
    rf"^{re.escape(_VOICE_TURN_MARKER)}\n\*\*(.+):\*\* (.*)\n\*\*(.+):\*\* (.*)$",
    re.DOTALL,
)

_ACK_POOL_FALLBACKS = {
    "ack_action": "action",
    "action:task": "action",
    "ack:empathy": "ack",
}


@dataclass(frozen=True)
class VoiceEcho:
    human_name: str
    human_text: str
    bot_name: str
    bot_text: str


def _is_status_query(text: str) -> bool:
    """Status questions may arrive wrapped in greetings/fillers
    ('hello? are you there? what's going on'). Checked only mid-turn."""
    return bool(_STATUS_QUERY_RE.search(text.strip()))


def _outcome_text(outcome: TurnOutcome) -> str:
    generated = outcome.generated
    if isinstance(generated, str):
        return generated.strip()
    if isinstance(generated, (list, tuple)):
        return " ".join(str(item).strip() for item in generated if str(item).strip())
    return ""


def _format_voice_turn(
    name: str, user_text: str, reply: str, *, speaker: str = "You"
) -> str:
    """The written record of a voice turn, ChatGPT-voice-mode style."""
    body = f"**{speaker}:** {user_text.strip()}\n**{name}:** {reply.strip()}"
    if len(body) > 1900:
        body = body[:1899].rstrip() + "\u2026"
    return f"{_VOICE_TURN_MARKER}\n{body}"


def parse_voice_echo(content: str) -> VoiceEcho | None:
    match = _VOICE_ECHO_RE.fullmatch(content.strip())
    if match is None:
        return None
    return VoiceEcho(match.group(1), match.group(2), match.group(3), match.group(4))


async def _dynamic_clip(bot, text: str) -> Path | bytes | None:
    """Synthesize (or reuse) a one-off spoken line for progress narration."""
    from sleeptalk.discord_voice.constants import COMMAND_ERRORS

    cached = bot._dynamic_clips.get(text)
    if isinstance(cached, bytes):
        return cached
    if isinstance(cached, Path) and cached.is_file():
        return cached
    if len(bot._dynamic_clips) >= 24:  # cap the one-off clip cache
        _old_text, old_path = next(iter(bot._dynamic_clips.items()))
        bot._dynamic_clips.pop(_old_text, None)
        if isinstance(old_path, Path):
            with contextlib.suppress(FileNotFoundError):
                old_path.unlink()
    try:
        audio = await collect_tts_audio(bot.tts, text)
    except COMMAND_ERRORS:
        return None
    bot._dynamic_clips[text] = audio
    return audio


def _next_preamble_clip(bot, pool: str) -> Path | bytes | None:
    """Rotate through a pool without repeating the same clip consecutively."""
    clips = bot.preamble_clips.get(pool) or []
    if not clips:
        return None
    last = bot._preamble_rotation.get(pool, -1)
    idx = (last + 1) % len(clips)
    bot._preamble_rotation[pool] = idx
    return clips[idx]


def _pick_ack_clip(bot: Any, pool: str) -> Path | bytes | None:
    clip = _next_preamble_clip(bot, pool)
    if clip is None:
        fallback = _ACK_POOL_FALLBACKS.get(pool)
        if fallback:
            clip = _next_preamble_clip(bot, fallback)
    return clip


def _user_speaking(session: Any, user_id: int) -> bool:
    """True when the user has an open capture buffer — they resumed (or
    never stopped) speaking. Duck-typed: test sessions may lack a sink."""
    buffers = getattr(getattr(session, "sink", None), "buffers", None)
    try:
        return bool(buffers) and user_id in buffers
    except TypeError:
        return False


async def _cancel_session_turn(bot: Any, *, owner=None) -> None:
    await bot.cancel_active_turn(owner=owner)


async def _abort_kernel_agent(bot: Any) -> None:
    agent = bot.agent
    abort = getattr(agent, "abort", None)
    if callable(abort):
        with contextlib.suppress(Exception):
            await abort()


async def _prepare_kernel_for_voice(bot: Any) -> None:
    """Best-effort session prewarm that never leaks a background exception."""
    try:
        await bot.kernel.prepare()
    except (PiRPCError, TimeoutError) as exc:
        await _abort_kernel_agent(bot)
        print(f"Voice session prepare failed: {format_error(exc)}")
