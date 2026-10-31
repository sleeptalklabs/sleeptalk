"""Route-owned audio output boundary (r2-route-protocol audit rework).

ADR 0003 §6 requires routes to expose an *enforceable* output-audio
interface — not just a ``speak`` callable the surface hopes is wired. This
module defines that boundary:

- :class:`AudioOutput` — the sink routes push synthesized PCM into
  (``write(pcm, turn_id)``). Implementations own playout; routes only
  deliver bytes and observe acceptance.
- :class:`AudioOutputAccounting` — the played-vs-generated bookkeeping
  every route must report (ADR 0003 §4 clause 3): what crossed the
  playout boundary, never just what was generated.
- :class:`InFlightTurn` — route-owned in-flight task management: the
  protocol itself tracks the active turn task so callers never assign
  private ``_turn_task`` fields (the audit's explicit finding).

The boundary is deliberately small: routes stay surface-agnostic (no
Discord/terminal imports), surfaces implement :class:`AudioOutput`, and
the protocol can *verify* output compliance instead of trusting it.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "AudioOutput",
    "AudioOutputAccounting",
    "InFlightTurn",
    "TurnPlaybackError",
]


class TurnPlaybackError(RuntimeError):
    """The output boundary rejected audio for the given turn (stale turn,
    closed output, or underrun beyond policy)."""


@runtime_checkable
class AudioOutput(Protocol):
    """The enforceable output-audio interface routes deliver into.

    Implementations (surface-side) must:

    - accept PCM at the kernel's canonical format (48 kHz int16 mono);
    - drop audio for stale turns (``turn_id`` older than the current
      epoch) instead of playing it — the route relies on this for the
      monotonic-generation clause (ADR 0003 §4 clause 1);
    - never raise for late audio after close (log/drop instead);
    - report what actually played via :class:`AudioOutputAccounting`.
    """

    async def write(self, pcm: bytes, turn_id: int) -> None:
        """Deliver one PCM chunk for a turn. Stale-turn audio is dropped."""
        ...

    async def finish_turn(self, turn_id: int) -> None:
        """Finish this presentation segment without discarding queued audio.

        Flush buffered partial frames and signal that no more audio is pending
        for this presentation. Repeated calls without intervening writes must
        be safe. A detached route may later deliver another segment; it owns
        that segment's completion. Synchronous turns are finalized by the
        session, including when a route has already signalled completion.
        """
        ...

    async def discard_turn(self, turn_id: int, reason: str = "interrupted") -> None:
        """Permanently reject this turn's queued and future audio.

        Implementations must install the stale-output fence before clearing
        transport playback so a concurrent late write cannot restart the
        cancelled turn.
        """
        ...

    def accounting(self) -> "AudioOutputAccounting":
        """Played-vs-generated accounting for this output."""
        ...

    async def close(self) -> None:
        """Release playout resources; late writes are dropped, not raised."""
        ...


@dataclass
class AudioOutputAccounting:
    """Partial-output accounting by what crossed the playout boundary
    (ADR 0003 §4 clause 3). Routes report both measures; generated is
    never masqueraded as played."""

    turn_id: int
    bytes_generated: int = 0
    bytes_written: int = 0
    bytes_dropped_stale: int = 0
    chunks: int = 0

    def generated(self, byte_count: int) -> None:
        self.bytes_generated += byte_count

    def written(self, byte_count: int) -> None:
        self.bytes_written += byte_count
        self.chunks += 1

    def dropped_stale(self, byte_count: int) -> None:
        self.bytes_dropped_stale += byte_count

    @property
    def played_fraction(self) -> float:
        """0..1 — how much of the generated audio actually played."""
        if self.bytes_generated <= 0:
            return 0.0
        return min(1.0, self.bytes_written / self.bytes_generated)


@dataclass
class InFlightTurn:
    """Route-owned in-flight task management (audit finding: callers were
    assigning private ``_turn_task`` fields). The protocol tracks the
    active turn so cancel/close operate on real tasks through public
    state only."""

    turn_id: int
    task: asyncio.Task[Any] | None = None
    accounting: AudioOutputAccounting = field(default_factory=lambda: AudioOutputAccounting(turn_id=-1))

    def __post_init__(self) -> None:
        self.accounting = AudioOutputAccounting(turn_id=self.turn_id)

    def cancel(self) -> bool:
        """Cancel the in-flight task exactly once. Returns whether a live
        task was cancelled."""
        if self.task is not None and not self.task.done():
            self.task.cancel()
            return True
        return False
