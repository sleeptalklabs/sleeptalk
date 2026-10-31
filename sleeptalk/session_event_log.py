"""TEMPORARY test probe. Delete this module after sub-agent voice QA.

Set SLEEPTALK_SESSION_EVENT_LOG to a jsonl path. A write failure never
breaks the voice turn.
"""

from __future__ import annotations

import json
import os
from typing import Any

from sleeptalk.session_events import SessionEvent

ENV = "SLEEPTALK_SESSION_EVENT_LOG"


def _append_private(path: str, line: str) -> None:
    """Owner-only (0600), never through a symlink: rows carry transcripts."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def append_session_event(event: SessionEvent) -> None:
    path = os.environ.get(ENV, "").strip()
    if not path:
        return
    try:
        _append_private(path, json.dumps(event.to_json(), default=str))
    except Exception:
        return


def probe_enabled() -> bool:
    return bool(os.environ.get(ENV, "").strip())


def append_probe_record(fields: dict[str, Any]) -> None:
    """Extra rows that are not SessionEvents. Same file, type prefixed."""
    path = os.environ.get(ENV, "").strip()
    if not path:
        return
    try:
        _append_private(path, json.dumps(fields, default=str))
    except Exception:
        return
