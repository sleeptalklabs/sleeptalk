"""Exact-match local intents for cascade, before the agent runs."""

from __future__ import annotations

import re
from dataclasses import dataclass

_WRAPPERS = frozenset({"hello", "hey", "hi", "um", "uh", "so"})

_IDLE_REPLIES: dict[str, str] = {
    "hello": "Hi, I'm here.",
    "can you hear me": "Yep, I can hear you.",
    "are you there": "Yep, I'm here.",
    "you there": "Yep, I'm here.",
    "is this working": "Yep, I'm here and I can hear you.",
}

_BUSY_REPLY = "Yep, I'm here, still working on it"
_HALT_REPLY = "Stopped."

# Same bare-control family Discord already intercepts, plus STT chrome for
# spoken "/stop" ("slash stop", "pipeline control slash stop").
_HALT_CORE = (
    r"(?:stop|cancel|never ?mind|forget it|hold on|hold up|enough|"
    r"thats enough|be quiet|stop talking|wait|quit)"
)
_HALT_FILLER = (
    r"(?:please|okay|ok|hey|just|now|it|pipeline|pi|bot|control|"
    r"slash|forward|command)"
)
_HALT_RE = re.compile(
    rf"^(?:{_HALT_FILLER}[\s,]+)*{_HALT_CORE}"
    rf"(?:[\s,]+(?:{_HALT_CORE}|{_HALT_FILLER}))*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class LocalIntent:
    intent: str
    reply: str


def _normalize(user_text: str) -> str:
    lowered = user_text.lower()
    kept = "".join(ch for ch in lowered if ch.isalnum() or ch.isspace())
    return " ".join(kept.split())


def _presence_key(normalized: str) -> str | None:
    if normalized in _IDLE_REPLIES:
        return normalized
    parts = normalized.split()
    if len(parts) >= 2 and parts[0] in _WRAPPERS:
        remainder = " ".join(parts[1:])
        if remainder in _IDLE_REPLIES:
            return remainder
    return None


def classify_local_intent(user_text: str, *, busy: bool = False) -> LocalIntent | None:
    normalized = _normalize(user_text)
    if _HALT_RE.fullmatch(normalized):
        return LocalIntent(intent="halt", reply=_HALT_REPLY)
    key = _presence_key(normalized)
    if key is None:
        return None
    reply = _BUSY_REPLY if busy else _IDLE_REPLIES[key]
    return LocalIntent(intent="presence_check", reply=reply)
