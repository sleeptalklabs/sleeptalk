"""Atomic turn-local ownership for the single opening voice cue."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from sleeptalk.session_events import progress


class OpeningPreambleGate:
    """Atomic, turn-local ownership for the single opening voice cue."""

    def __init__(self, *, already_claimed_by: str = "") -> None:
        self._lock = asyncio.Lock()
        self.claimed_by = already_claimed_by
        self.audio_started = False
        self.audio_committing = False

    async def claim(self, owner: str) -> bool:
        async with self._lock:
            if self.claimed_by:
                return False
            self.claimed_by = owner
            return True

    async def speak(
        self,
        *,
        owner: str,
        pool: str,
        line: str,
        ctx: Any,
        speak: Callable[[str, Any], Awaitable[None]],
    ) -> bool:
        if not await self.claim(owner):
            return False
        ctx.emit(progress(ctx.session_id, ctx.turn_id, pool=pool, text=line))
        await speak(line, ctx)
        return True
