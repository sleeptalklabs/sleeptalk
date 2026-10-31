"""Primary-only speech for positively identified workers.

The roster is the store. This module decides what, if anything, the primary
voice says when a worker changes. A child never gets the microphone.
"""

from __future__ import annotations

import enum
import re
from collections.abc import Callable
from dataclasses import dataclass, field

from sleeptalk.adapter import WorkerEvent, WorkerPhase
from sleeptalk.session_events import SessionEvent, roster_session_event
from sleeptalk.subagent_roster import RosterChange, RosterSnapshot, SubagentRoster, WorkerRecord

QUIET_INTERVAL_S = 10.0
_TERMINAL = frozenset(
    {WorkerPhase.COMPLETED, WorkerPhase.FAILED, WorkerPhase.CANCELLED}
)
_STATUS_RE = re.compile(
    r"(?:"
    r"what(?:'s| is) (?:still )?running"
    r"|what(?:'s| is) (?:in )?the (?:background|roster)"
    r"|how(?:'s| is) .+ going"
    r"|status of"
    r"|any updates on"
    r")",
    re.IGNORECASE,
)


class FloorState(enum.Enum):
    LISTENING = "listening"
    USER_SPEAKING = "user_speaking"
    BOT_SPEAKING = "bot_speaking"
    THINKING = "thinking"


class SpeechKind(enum.Enum):
    SILENT = "silent"
    ACK = "ack"
    PROGRESS = "progress"
    COMPLETION = "completion"
    STATUS = "status"


@dataclass(frozen=True)
class SpeakDecision:
    kind: SpeechKind
    text: str = ""
    queue: bool = False


@dataclass
class WorkerVoiceState:
    detached_acks: int = 0
    queued: list[SpeakDecision] = field(default_factory=list)
    immediate: list[str] = field(default_factory=list)
    started_mono: dict[str, float] = field(default_factory=dict)


def worker_job_name(record: WorkerRecord) -> str:
    label = record.label.strip()
    if label:
        return label
    summary = (record.summary or "").strip()
    if summary:
        return summary
    return "a worker"


def is_status_question(text: str) -> bool:
    return bool(_STATUS_RE.search((text or "").strip()))


# Asked while the agent works ("What are you doing right now?", "How are
# the sub agents doing?"): answered from what the call knows, without
# interrupting the work. Discord's mid-turn phrasing plus the workers'.
_MID_TURN_STATUS_RE = re.compile(
    r"\b(?:what are you (?:doing|working on)|what'?s (?:taking (?:so long|forever)|happening|going on|the status|the hold ?up)"
    r"|are you (?:still )?(?:working|there|stuck|ok|okay)|how'?s it (?:going|coming)|any (?:progress|updates?)"
    r"|status(?: update| check)?|give me a status|where are you at"
    r"|how\b.{0,30}?\b(?:sub[- ]?agents|agents|workers|checks|tasks) (?:doing|going|coming along))\b",
    re.IGNORECASE,
)


def is_mid_turn_status_question(text: str) -> bool:
    """A status question asked while the agent works, answered without stopping it."""
    words = (text or "").strip()
    return bool(_MID_TURN_STATUS_RE.search(words)) or is_status_question(words)


def status_line(snapshot: RosterSnapshot) -> str:
    live = [row for row in snapshot.workers if row.phase not in _TERMINAL]
    if live:
        names = ", ".join(worker_job_name(row) for row in live)
        return f"Still running: {names}."
    done = [row for row in snapshot.workers if row.phase is WorkerPhase.COMPLETED]
    if done:
        return f"{worker_job_name(done[-1])} is ready."
    failed = [row for row in snapshot.workers if row.phase is WorkerPhase.FAILED]
    if failed:
        return f"{worker_job_name(failed[-1])} failed."
    return "Nothing in the background."


def decide_worker_speech(
    change: RosterChange,
    *,
    snapshot: RosterSnapshot,
    floor: FloorState,
    state: WorkerVoiceState,
    quiet_elapsed_s: float | None = None,
    user_asked_status: bool = False,
) -> SpeakDecision:
    if user_asked_status:
        return SpeakDecision(SpeechKind.STATUS, status_line(snapshot))
    record = change.record
    name = worker_job_name(record)
    first_seen = change.previous_phase is None
    if first_seen and record.phase not in _TERMINAL:
        if record.detached:
            live = sum(1 for row in snapshot.workers if row.phase not in _TERMINAL)
            others = live - 1
            if others == 0:
                return _maybe_queue(
                    SpeakDecision(
                        SpeechKind.ACK,
                        f"I've sent {name} to work on that. We can keep talking.",
                    ),
                    floor,
                )
            if live >= 3:
                return _maybe_queue(
                    SpeakDecision(
                        SpeechKind.ACK,
                        f"I have {live} checks running.",
                    ),
                    floor,
                )
            return SpeakDecision(SpeechKind.SILENT)
        return _maybe_queue(
            SpeakDecision(
                SpeechKind.ACK,
                f"I'm asking {name} to check that now.",
            ),
            floor,
        )
    if record.phase in _TERMINAL:
        if record.phase is WorkerPhase.FAILED:
            text = f"{name} failed. Want me to retry?"
        elif record.phase is WorkerPhase.CANCELLED:
            text = f"{name} was cancelled."
        else:
            text = f"{name} is ready. Want the short version?"
        return _maybe_queue(SpeakDecision(SpeechKind.COMPLETION, text), floor)
    if first_seen:
        return SpeakDecision(SpeechKind.SILENT)
    material = (record.summary or "").lower()
    blocked = any(token in material for token in ("blocked", "failed", "retrying"))
    overdue = quiet_elapsed_s is not None and quiet_elapsed_s >= QUIET_INTERVAL_S
    if blocked or overdue:
        summary = (record.summary or "").strip() or "still working"
        return _maybe_queue(
            SpeakDecision(SpeechKind.PROGRESS, f"{name} is {summary}."),
            floor,
        )
    return SpeakDecision(SpeechKind.SILENT)


def admit_worker(
    roster: SubagentRoster,
    emit: Callable[[SessionEvent], object],
    event: WorkerEvent,
) -> RosterChange | None:
    change = roster.apply(event)
    if change is not None:
        emit(roster_session_event(change))
    return change


def take_queued(state: WorkerVoiceState) -> list[SpeakDecision]:
    items = list(state.queued)
    state.queued.clear()
    return items


def take_immediate(state: WorkerVoiceState) -> list[str]:
    items = list(state.immediate)
    state.immediate.clear()
    return items


def note_start(state: WorkerVoiceState, change: RosterChange, now: float) -> None:
    if change.previous_phase is None:
        state.started_mono.setdefault(change.worker_id, now)


def remember_ack(state: WorkerVoiceState, change: RosterChange) -> None:
    if change.record.detached and change.previous_phase is None:
        state.detached_acks += 1


def _maybe_queue(decision: SpeakDecision, floor: FloorState) -> SpeakDecision:
    if floor is FloorState.USER_SPEAKING:
        return SpeakDecision(decision.kind, decision.text, queue=True)
    if floor is FloorState.BOT_SPEAKING and decision.kind is not SpeechKind.ACK:
        return SpeakDecision(decision.kind, decision.text, queue=True)
    return decision
