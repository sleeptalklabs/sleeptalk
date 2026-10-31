"""Call lifecycle supervision (PR 3, ADR 0001).

Today this is deliberately small: a single-writer shutdown guard that makes
session teardown idempotent under simultaneous triggers (transport failure,
timeout, /leave command, last-human-left). The full state machine
(OFFERED/CONNECTING/ACTIVE/RECONNECTING/ENDING/ENDED) lands with the
CallSession extraction; this is the piece PR 3 needs to guarantee that
shutdown happens exactly once, whoever asks first.
"""

from __future__ import annotations


class LifecycleSupervisor:
    """Single owner of the shutdown transition for one call session.

    All teardown triggers funnel through begin_shutdown(); the first caller
    wins and performs the shutdown sequence, every later caller is a no-op.
    Thread-safety is not needed (single event loop) but reentrancy is safe.
    """

    def __init__(self) -> None:
        self._shutdown_started = False

    def begin_shutdown(self) -> bool:
        """Returns True exactly once — for the caller that owns shutdown."""
        if self._shutdown_started:
            return False
        self._shutdown_started = True
        return True

    @property
    def is_shutting_down(self) -> bool:
        return self._shutdown_started
