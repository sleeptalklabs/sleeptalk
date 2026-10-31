"""Response-scoped playback handle (PR 3, ADR 0001).

One agent response = one epoch. The handle owns cancellation semantics for
that response: bumping the epoch rejects late frames from cancelled TTS
production, attached playback sources are drained locally, and the transport
is asked to stop. How much of the already-queued audio a platform can truly
retract differs per transport, so cancel() reports honestly via ClearResult:

  confirmed   — transport acknowledged the buffer clear (e.g. Twilio clear)
  best_effort — local buffers drained; anything already on the wire is gone
  unsupported — no clear primitive exists on this transport

Discord today is best_effort: the playback source buffer is drained
synchronously, but frames already handed to the UDP socket cannot be
recalled.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ClearResult:
    status: str  # "confirmed" | "best_effort" | "unsupported"
    detail: str = ""


class _PlaybackController(Protocol):
    async def stop(self, reason: str = "interrupted") -> bool: ...


class ResponsePlayback:
    """Epoch-scoped cancellation over one response's playback."""

    def __init__(
        self,
        *,
        epoch: int,
        controller: _PlaybackController,
        clear_status: str = "best_effort",
    ) -> None:
        self.epoch = epoch
        self._controller = controller
        self._clear_status = clear_status
        self._cancelled = False
        self._sources: list[object] = []

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def attach_source(self, source: object) -> None:
        """Register a live playback source (e.g. StreamPCMAudioSource) so
        cancel() can drain its buffered frames synchronously."""
        self._sources.append(source)

    def drain_local(self) -> None:
        """Synchronously discard every attached source's buffered audio
        without touching the transport. Used on hard barge-in, where the
        turn coordinator has already stopped transport playback."""
        for source in self._sources:
            discard = getattr(source, "discard", None)
            if callable(discard):
                discard()

    async def cancel(self, reason: str) -> ClearResult:
        if self._cancelled:
            return ClearResult(self._clear_status, "already cancelled")
        self._cancelled = True
        self.drain_local()
        await self._controller.stop(reason)
        return ClearResult(
            self._clear_status,
            "local buffers drained; in-flight wire audio not recallable",
        )
