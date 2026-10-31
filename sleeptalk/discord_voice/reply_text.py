"""The spoken call, written into the voice channel's own chat.

The words come from where the web call page's keyed transcript gets them:
the session event stream, folded by the shared presentation reducer
(``sleeptalk/presentation.py``). Discord renders that snapshot instead of
assembling a second transcript of its own, so a reply reads in the channel
exactly as it reads on the page — phrases merged, the closing rollup not
repeated, an interrupted answer marked where the audio stopped. The same
reducer also catches replies no Discord turn returns: a background job's
announcement, or the line the route speaks when a turn fails.

Discord I/O is queued and drained in one task. A slow, rate-limited or
forbidden channel never holds up microphone input, a turn, or teardown.
"""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

import discord

from sleeptalk.discord.session_helpers import _format_voice_turn
from sleeptalk.presentation import PresentationState, reduce
from sleeptalk.session_events import SessionEvent, SessionEventType

logger = logging.getLogger(__name__)

#: Discord's hard per-message ceiling.
MESSAGE_LIMIT = 2000

#: Turns still open at once. Barge-in overlaps two; anything beyond that is a
#: turn whose end never arrived, and must not accumulate over a days-long call.
_MAX_OPEN_TURNS = 8

#: Queued messages kept while the channel is slow. A call that runs for days
#: with a broken channel must not grow without bound.
_MAX_QUEUED = 32

#: Consecutive send failures after which this call stops trying. A channel the
#: bot cannot post in (Send Messages revoked) would otherwise fail every turn.
_MAX_FAILURES = 3

_CLOSING = frozenset({
    SessionEventType.RESPONSE_COMPLETED,
    SessionEventType.RESPONSE_CANCELLED,
})

# Lines of the page's feed that belong in a chat message. Tool, progress and
# worker rows are live scaffolding, not the conversation.
_SPOKEN_KINDS = ("assistant", "notes")


@dataclass
class _Turn:
    state: PresentationState = field(default_factory=PresentationState)
    speaker: str = ""
    heard: str = ""


class ReplyFeed:
    """One chat message per completed agent turn, in the channel's text side."""

    def __init__(
        self,
        channel: Any,
        *,
        name: str,
        caller_speech: bool = False,
        echo_marker: bool = False,
        authorized: Callable[[], bool] | None = None,
        limit: int = MESSAGE_LIMIT,
    ) -> None:
        self.channel = channel
        self.name = name or "Agent"
        # Posting what the caller said is opt-in: on a shared channel it
        # doubles the volume, and the words are already in the room.
        self.caller_speech = caller_speech
        # ``sleeptalk-voice/1``, which another bot in the same room parses
        # back into its own conversation (parse_voice_echo). Only profiles
        # that read peer messages at all emit it; for the rest it is noise.
        self.echo_marker = echo_marker
        self.limit = max(int(limit), 1)
        self._authorized = authorized
        self._turns: dict[int, _Turn] = {}
        self._pending_speaker = ""
        self._pending_heard = ""
        self._queue: deque[str] = deque()
        self._task: asyncio.Task | None = None
        self._failures = 0
        self._sent = False
        self._disabled = False
        self._closed = False

    # -- input ------------------------------------------------------------

    def speaking(self, speaker: str, heard: str = "") -> None:
        """Attribute the next turn to ``speaker``, who said ``heard``.

        Discord's own final transcript, not the route's user event: the text
        handed to the route is a prompt, which may carry room context or a
        system note the channel should never show.
        """
        self._pending_speaker = str(speaker or "").strip()
        self._pending_heard = " ".join(str(heard or "").split())

    def handle(self, event: SessionEvent) -> None:
        """Fold one session event in; post the turn when it ends. Never raises."""
        if self._closed or self._disabled or event.turn_id is None:
            return
        turn_id = event.turn_id
        turn = self._turns.get(turn_id)
        if turn is None:
            turn = _Turn(speaker=self._pending_speaker, heard=self._pending_heard)
            # Consumed once: a later turn the caller did not start (a
            # background job's announcement) must not borrow their words.
            self._pending_speaker = self._pending_heard = ""
            self._turns[turn_id] = turn
            while len(self._turns) > _MAX_OPEN_TURNS:
                self._turns.pop(next(iter(self._turns)))
        try:
            turn.state = reduce(turn.state, event)
        except Exception:  # noqa: BLE001 - display only; a turn never fails for text
            logger.debug("reply text could not fold %s", event.type, exc_info=True)
        if event.type in _CLOSING:
            self._post(self._turns.pop(turn_id, None))

    # -- rendering --------------------------------------------------------

    def content(self, turn: _Turn) -> str:
        """The chat message for a finished turn, or "" when it said nothing."""
        reply = _paragraphs(turn.state.lines)
        if not reply:
            return ""
        speaker = turn.speaker or "You"
        heard = turn.heard if self.caller_speech else ""
        if self.echo_marker and heard:
            # Kept byte-for-byte: a peer bot parses this shape back out.
            return _format_voice_turn(self.name, heard, reply, speaker=speaker)
        if not heard:
            # Discord already names the bot above its message.
            return reply
        return f"**{speaker}:** {heard}\n**{self.name}:** {reply}"

    def _post(self, turn: _Turn | None) -> None:
        if turn is None:
            return
        content = self.content(turn)
        if not content:
            return
        for chunk in split_message(content, self.limit):
            self._queue.append(chunk)
        while len(self._queue) > _MAX_QUEUED:
            self._queue.popleft()
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._drain())

    # -- delivery ---------------------------------------------------------

    async def _drain(self) -> None:
        while self._queue and not self._closed and not self._disabled:
            if self._authorized is not None and not self._authorized():
                # Access was revoked mid-call: the rest of the call is not
                # this channel's to read.
                logger.info("Call text held back: someone in the voice channel isn't an allowed listener")
                self._queue.clear()
                return
            content = self._queue.popleft()
            try:
                async with asyncio.timeout(15):
                    await self.channel.send(
                        content, allowed_mentions=discord.AllowedMentions.none()
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - display only; never unraisable
                self._failures += 1
                # Said once per run of failures, at a level the log keeps:
                # a call that is never written must say why.
                if self._failures == 1:
                    logger.info("Call text not sent: %s", _send_error(exc))
                if self._failures >= _MAX_FAILURES:
                    self._disabled = True
                    self._queue.clear()
                    logger.info(
                        "Call text stopped: %s cannot take messages", type(self.channel).__name__
                    )
                else:
                    logger.debug("call text delivery failed", exc_info=True)
                continue
            if not self._sent:
                logger.info("Call text: first message posted")
            self._sent = True
            self._failures = 0

    def close(self) -> None:
        self._closed = True
        self._turns.clear()
        self._queue.clear()
        if self._task is not None:
            self._task.cancel()

    async def wait_closed(self) -> None:
        task = self._task
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)


def _paragraphs(lines: Any) -> str:
    """A reply as it reads: the sentences of one stretch of speech run on in
    one paragraph (the reducer keeps each spoken sentence as its own line);
    a specialist's notes, or speech after them, start a new one."""
    paragraphs: list[list[str]] = []
    kind = ""
    for line in lines:
        text = line.text.strip()
        if line.kind not in _SPOKEN_KINDS or not text:
            continue
        if line.kind != kind or not paragraphs:
            paragraphs.append([])
            kind = line.kind
        paragraphs[-1].append(text)
    return "\n".join(" ".join(parts) for parts in paragraphs)


def _send_error(exc: BaseException) -> str:
    """Why a send failed, without the message text: the HTTP status and
    Discord's own code and reason ("403 Forbidden (50013): Missing
    Permissions"), or the exception's class."""
    status = getattr(exc, "status", None)
    code = getattr(exc, "code", None)
    reason = str(getattr(exc, "text", "") or "")[:120]
    if status is not None:
        return f"{status} {type(exc).__name__} ({code}): {reason}".strip()
    return type(exc).__name__


def split_message(content: str, limit: int = MESSAGE_LIMIT) -> list[str]:
    """``content`` cut into messages Discord will accept, on the widest break.

    Prefers a line break, then a sentence end, then a space, and only cuts a
    word when a single word is longer than a whole message.
    """
    rest = content.strip()
    chunks: list[str] = []
    while len(rest) > limit:
        window = rest[: limit + 1]
        cut = window.rfind("\n")
        if cut <= 0:
            sentence = max(
                window.rfind(". "), window.rfind("? "), window.rfind("! ")
            )
            cut = sentence + 1 if sentence > 0 else window.rfind(" ")
        if cut <= 0:
            cut = limit
        chunks.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    if rest:
        chunks.append(rest)
    return chunks
