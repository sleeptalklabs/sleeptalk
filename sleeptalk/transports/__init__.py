"""Surface transports for the Sleeptalk runtime package."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from sleeptalk.session import RealtimeSession


class Transport(Protocol):
    async def run(self, session: RealtimeSession) -> None:
        """Run capture/playout against this session until the surface closes."""
        ...
