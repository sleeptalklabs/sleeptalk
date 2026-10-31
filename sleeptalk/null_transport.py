"""Null transport: logs instead of connecting.

Proves the registry works (new class + registration, zero builder edits) and
doubles as the permanent test harness — everything past registry assembly is
testable without Discord.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger("sleeptalk.null_transport")


class NullTransport:
    """A transport that records session composition and 'runs' by logging."""

    def __init__(self, config, env: dict[str, str]) -> None:
        self.config = config
        # Transport factories receive the full SleeptalkConfig.
        self.log_path = config.transport.log_path.strip()
        self.events: list[dict[str, Any]] = []

    def _record(self, event: str, **fields: Any) -> None:
        entry = {"event": event, **fields}
        self.events.append(entry)
        line = f"null-transport {event}: {fields}"
        if self.log_path:
            with Path(self.log_path).open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        logger.info(line)

    async def run(self, session) -> None:
        """'Run' a session: log what would have been connected, then stop."""
        self._record(
            "session_started",
            agent=type(session.agent).__name__ if session.agent else "none",
            stt=session.config.stt.provider,
            tts=session.config.tts.provider,
            tts_fallbacks=[s.provider for s in session.config.tts.fallback],
            endpointing=session.policy.endpointing,
            barge_in=session.policy.barge_in,
            fillers=session.policy.fillers_enabled,
        )
        self._record("session_stopped")
