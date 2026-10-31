"""Per-process room policy and local transcript.

One ``Room`` belongs to one ``sleeptalk run`` process. Another process owns
a different ``Room``. They never share a file. The Discord hook maps a
``User`` to ``Utterance`` and calls ``route`` then maybe ``prompt_for``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, NewType, Protocol, runtime_checkable

SpeakerId = NewType("SpeakerId", str)

_TRANSCRIPT_LIMIT = 50
_WORD = r"(?<![\w-]){}(?![\w-])"


class ParticipationMode(StrEnum):
    CONVERSATIONALIST = "conversationalist"
    ATTENDEE = "attendee"
    PROACTIVE = "proactive"


class ParticipantKind(StrEnum):
    HUMAN = "human"
    BOT = "bot"


class RouteAction(StrEnum):
    IGNORE = "ignore"
    ANSWER = "answer"


class ConversationScale(StrEnum):
    DYAD = "dyad"
    ROOM = "room"


@dataclass(frozen=True, slots=True)
class Miss:
    kind: Literal["miss"] = "miss"


@dataclass(frozen=True, slots=True)
class Named:
    token: str
    kind: Literal["name"] = "name"


@dataclass(frozen=True, slots=True)
class Directed:
    kind: Literal["directed"] = "directed"


@dataclass(frozen=True, slots=True)
class Triggered:
    token: str
    kind: Literal["trigger"] = "trigger"


@dataclass(frozen=True, slots=True)
class Floor:
    kind: Literal["floor"] = "floor"


@dataclass(frozen=True, slots=True)
class Elsewhere:
    """The turn names another participant, not this bot."""

    name: str
    kind: Literal["elsewhere"] = "elsewhere"


AddressHit = Miss | Named | Directed | Triggered | Floor | Elsewhere


@dataclass(frozen=True, slots=True)
class Speaker:
    id: SpeakerId
    display_name: str
    kind: ParticipantKind


@dataclass(frozen=True, slots=True)
class Utterance:
    speaker: Speaker
    text: str


@dataclass(frozen=True, slots=True)
class TranscriptLine:
    speaker: Speaker
    text: str


@dataclass(frozen=True, slots=True)
class Identity:
    id: SpeakerId
    display_name: str
    mode: ParticipationMode
    addressable_names: tuple[str, ...]
    proactive_triggers: tuple[str, ...]

    @property
    def speaker(self) -> Speaker:
        return Speaker(self.id, self.display_name, ParticipantKind.BOT)


@dataclass(frozen=True, slots=True)
class Decision:
    action: RouteAction
    hit: AddressHit


@runtime_checkable
class AddressResolver(Protocol):
    def resolve(
        self,
        utterance: Utterance,
        identity: Identity,
        *,
        recent: tuple[TranscriptLine, ...],
        known: tuple[Speaker, ...] = (),
    ) -> AddressHit: ...


class AgentIdentitySource(Protocol):
    name: str
    slug: str
    participation_mode: str
    addressable_name: str
    proactive_triggers: list[str]


def identity_from_agent(agent: AgentIdentitySource, self_id: SpeakerId) -> Identity:
    raw_mode = str(
        getattr(agent, "participation_mode", "") or ParticipationMode.CONVERSATIONALIST
    ).strip()
    try:
        mode = ParticipationMode(raw_mode)
    except ValueError as exc:
        raise ValueError(
            f"unknown participation_mode {raw_mode!r} — "
            f"valid: {', '.join(m.value for m in ParticipationMode)}"
        ) from exc
    names = []
    custom = str(getattr(agent, "addressable_name", "") or "").strip()
    if custom:
        names.append(custom.casefold())
    name = str(getattr(agent, "name", "") or "").strip()
    slug = str(getattr(agent, "slug", "") or "").strip()
    if name:
        names.append(name.casefold())
    if slug:
        names.append(slug.casefold())
    raw_triggers = getattr(agent, "proactive_triggers", None) or ()
    triggers = tuple(item.strip() for item in raw_triggers if item and str(item).strip())
    return Identity(
        id=self_id,
        display_name=name or slug or "bot",
        mode=mode,
        addressable_names=tuple(dict.fromkeys(names)),
        proactive_triggers=triggers,
    )


def addressed(hit: AddressHit) -> bool:
    return isinstance(hit, (Named, Directed, Triggered, Floor))


def _decide(
    mode: ParticipationMode,
    speaker_kind: ParticipantKind,
    hit: AddressHit,
) -> RouteAction:
    if speaker_kind is ParticipantKind.BOT and isinstance(hit, Miss):
        return RouteAction.IGNORE
    if isinstance(hit, Elsewhere):
        return RouteAction.IGNORE
    if addressed(hit):
        return RouteAction.ANSWER
    if mode is ParticipationMode.CONVERSATIONALIST and speaker_kind is ParticipantKind.HUMAN:
        return RouteAction.ANSWER
    return RouteAction.IGNORE


def _scale(identity: Identity, lines: tuple[TranscriptLine, ...]) -> ConversationScale:
    if identity.mode is not ParticipationMode.CONVERSATIONALIST:
        return ConversationScale.ROOM
    humans = {line.speaker.id for line in lines if line.speaker.kind is ParticipantKind.HUMAN}
    foreign_bot = any(
        line.speaker.kind is ParticipantKind.BOT and line.speaker.id != identity.id
        for line in lines
    )
    if len(humans) == 1 and not foreign_bot:
        return ConversationScale.DYAD
    return ConversationScale.ROOM


def _wrap(lines: tuple[TranscriptLine, ...], utterance: Utterance) -> str:
    rendered = [f"{line.speaker.display_name}: {line.text}" for line in lines]
    current = f"{utterance.speaker.display_name}: {utterance.text}"
    if rendered[-1:] == [current]:
        body = "\n".join(rendered)
    else:
        body = "\n".join([*rendered, current])
    return (
        "[ROOM TRANSCRIPT]\n"
        f"{body}\n"
        f"[CURRENT TURN from {utterance.speaker.display_name}]"
    )


def _word_match(haystack: str, needle: str) -> bool:
    if not needle:
        return False
    return re.search(_WORD.format(re.escape(needle)), haystack, flags=re.IGNORECASE) is not None


class NameSlugResolver:
    def resolve(
        self,
        utterance: Utterance,
        identity: Identity,
        *,
        recent: tuple[TranscriptLine, ...],
        known: tuple[Speaker, ...] = (),
    ) -> AddressHit:
        text = utterance.text
        for name in identity.addressable_names:
            if _word_match(text, name):
                return Named(token=name)
        for trigger in identity.proactive_triggers:
            if _word_match(text, trigger):
                return Triggered(token=trigger)
        seen: dict[SpeakerId, Speaker] = {speaker.id: speaker for speaker in known}
        for line in recent:
            seen.setdefault(line.speaker.id, line.speaker)
        for speaker in seen.values():
            if speaker.id == identity.id:
                continue
            if _word_match(text, speaker.display_name):
                return Elsewhere(name=speaker.display_name)
        return Miss()


class ScriptedResolver:
    def __init__(
        self,
        script: dict[str, AddressHit],
        *,
        inner: AddressResolver | None = None,
    ) -> None:
        self._script = script
        self._inner = inner or NameSlugResolver()

    def resolve(
        self,
        utterance: Utterance,
        identity: Identity,
        *,
        recent: tuple[TranscriptLine, ...],
        known: tuple[Speaker, ...] = (),
    ) -> AddressHit:
        hit = self._script.get(utterance.text)
        if hit is not None:
            return hit
        return self._inner.resolve(
            utterance, identity, recent=recent, known=known
        )


class Room:
    def __init__(
        self,
        identity: Identity,
        *,
        resolver: AddressResolver | None = None,
        transcript_limit: int = _TRANSCRIPT_LIMIT,
    ) -> None:
        self._identity = identity
        self._resolver = resolver or NameSlugResolver()
        self._limit = max(1, transcript_limit)
        self._lines: list[TranscriptLine] = []
        self._others: dict[SpeakerId, Speaker] = {}

    @property
    def lines(self) -> tuple[TranscriptLine, ...]:
        return tuple(self._lines)

    def notice(self, speaker: Speaker) -> None:
        if speaker.id == self._identity.id:
            return
        self._others[speaker.id] = speaker

    def route(self, utterance: Utterance) -> Decision:
        text = utterance.text.strip()
        if not text:
            return Decision(RouteAction.IGNORE, Miss())
        cleaned = Utterance(utterance.speaker, text)
        self.notice(cleaned.speaker)
        hit = self._resolver.resolve(
            cleaned,
            self._identity,
            recent=tuple(self._lines),
            known=tuple(self._others.values()),
        )
        action = _decide(self._identity.mode, cleaned.speaker.kind, hit)
        self._append(TranscriptLine(cleaned.speaker, cleaned.text))
        return Decision(action, hit)

    def prompt_for(self, utterance: Utterance, decision: Decision) -> str:
        if decision.action is RouteAction.IGNORE:
            raise ValueError("prompt_for was called on an IGNORE decision")
        text = utterance.text.strip()
        if _scale(self._identity, tuple(self._lines)) is ConversationScale.DYAD:
            return text
        return _wrap(tuple(self._lines), Utterance(utterance.speaker, text))

    def ingest_echo(self, speaker: Speaker, text: str) -> None:
        cleaned = text.strip()
        if not cleaned or speaker.id == self._identity.id:
            return
        self.notice(speaker)
        last = self._lines[-1] if self._lines else None
        if last is not None and last.speaker.id == speaker.id and last.text == cleaned:
            return
        self._append(TranscriptLine(speaker, cleaned))

    def record_reply(self, text: str) -> None:
        cleaned = text.strip()
        if not cleaned:
            return
        self._append(TranscriptLine(self._identity.speaker, cleaned))

    def _append(self, line: TranscriptLine) -> None:
        self._lines.append(line)
        overflow = len(self._lines) - self._limit
        if overflow > 0:
            del self._lines[:overflow]


__all__ = [
    "AddressHit",
    "AddressResolver",
    "AgentIdentitySource",
    "ConversationScale",
    "Decision",
    "Directed",
    "Elsewhere",
    "Floor",
    "Identity",
    "Miss",
    "NameSlugResolver",
    "Named",
    "ParticipantKind",
    "ParticipationMode",
    "Room",
    "RouteAction",
    "ScriptedResolver",
    "Speaker",
    "SpeakerId",
    "TranscriptLine",
    "Triggered",
    "Utterance",
    "addressed",
    "identity_from_agent",
]
