"""Session-scoped provider usage accounting.

The meter records attempts and billable input units without storing prompts,
transcripts, audio, credentials, or provider response bodies. Adapters start an
attempt immediately before a real provider operation, add streamed units as
they are sent, and finish it with an explicit status. A session summary makes
pricing analysis possible without a billing system or hosted keys.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from sleeptalk.usage import UsageRecord


@dataclass
class _Attempt:
    provider: str
    operation: str
    units: Counter[str] = field(default_factory=Counter)
    metadata: dict[str, Any] = field(default_factory=dict)
    #: wall-clock start for normalized usage records (r2-usage-schema).
    started_at: str = ""
    #: normalized quantity/unit when the caller supplied one; the meter never
    # invents quantities it cannot derive (raw-unit events remain the source
    # of truth for legacy readers).
    normalized: dict[str, Any] = field(default_factory=dict)


class UsageMeter:
    def __init__(
        self,
        *,
        log_path: str | Path,
        session_id: str | None = None,
        surface: str = "",
    ) -> None:
        self.log_path = Path(log_path)
        self.session_id = session_id or uuid.uuid4().hex[:12]
        self.surface = surface
        self._started_at = time.perf_counter()
        self._lock = threading.Lock()
        self._attempts: dict[str, _Attempt] = {}
        self._totals: Counter[str] = Counter()
        self._provider_attempts: Counter[str] = Counter()
        self._finished = False

    @classmethod
    def for_config(cls, config) -> UsageMeter:
        configured = os.getenv("SLEEPTALK_USAGE_LOG", "").strip()
        if configured:
            path = Path(configured).expanduser()
        else:
            source = Path(getattr(config, "source_path", "") or "sleeptalk.toml")
            source = source.expanduser().resolve()
            parent = source.parent
            if parent.name == "profiles":
                parent = parent.parent.parent if parent.parent.name == "config" else parent.parent
            path = parent / "logs" / "provider_usage.jsonl"
        transport = getattr(getattr(config, "transport", None), "type", "")
        agent = getattr(getattr(config, "agent", None), "slug", "")
        surface = f"{agent}-{transport}" if agent and transport else transport
        return cls(log_path=path, surface=surface)

    def start_attempt(
        self,
        *,
        provider: str,
        operation: str,
        attempt_id: str,
        metadata: dict[str, Any] | None = None,
        record: UsageRecord | None = None,
        **units: float,
    ) -> None:
        """Start metering one provider attempt.

        ``record`` (r2-usage-schema) optionally carries the normalized,
        catalog-priceable view of the same attempt; it is emitted verbatim on
        the matching ``attempt_finished`` event. Raw ``**units`` remain the
        legacy source of truth, so existing JSONL readers are unaffected.
        """
        numeric_units = Counter(units)
        with self._lock:
            if attempt_id in self._attempts:
                return
            attempt = _Attempt(
                provider=provider,
                operation=operation,
                metadata=dict(metadata or {}),
                started_at=datetime.now(UTC).isoformat(),
                normalized=record.to_json() if record is not None else {},
            )
            attempt.units.update(numeric_units)
            self._attempts[attempt_id] = attempt
            self._totals.update(numeric_units)
            self._provider_attempts[f"{provider}:{operation}"] += 1
        self._emit(
            "attempt_started",
            provider=provider,
            operation=operation,
            attemptId=attempt_id,
            units=dict(numeric_units),
            **(metadata or {}),
        )

    def add_usage(self, attempt_id: str, **units: float) -> None:
        numeric_units = {
            key: value
            for key, value in units.items()
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        }
        with self._lock:
            attempt = self._attempts.get(attempt_id)
            if attempt is None:
                return
            attempt.units.update(numeric_units)
            self._totals.update(numeric_units)

    def finish_attempt(
        self,
        attempt_id: str,
        *,
        status: str,
        **fields: Any,
    ) -> None:
        with self._lock:
            attempt = self._attempts.pop(attempt_id, None)
        if attempt is None:
            return
        fields["usage"] = attempt.normalized or None
        self._emit(
            "attempt_finished",
            provider=attempt.provider,
            operation=attempt.operation,
            attemptId=attempt_id,
            status=status,
            units=dict(attempt.units),
            **attempt.metadata,
            **fields,
        )

    def finish_session(self, *, status: str = "closed") -> None:
        with self._lock:
            if self._finished:
                return
            self._finished = True
            open_attempts = list(self._attempts)
            totals = dict(self._totals)
            attempts = dict(self._provider_attempts)
        for attempt_id in open_attempts:
            self.finish_attempt(attempt_id, status="session_closed")
        self._emit(
            "session_summary",
            status=status,
            durationMs=round((time.perf_counter() - self._started_at) * 1000),
            totals=totals,
            attempts=attempts,
        )

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "sessionId": self.session_id,
                "totals": dict(self._totals),
                "attempts": dict(self._provider_attempts),
                "openAttemptIds": sorted(self._attempts),
            }

    def _emit(self, stage: str, **fields: Any) -> None:
        event = {
            "metric": "provider_usage",
            "loggedAt": datetime.now(UTC).isoformat(),
            "sessionId": self.session_id,
            "surface": self.surface,
            "stage": stage,
            **fields,
        }
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            # Usage lines name what was said to whom: this user's eyes only.
            fd = os.open(self.log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, sort_keys=True, default=str) + "\n")
        except OSError:
            # Metering cannot take down a live voice session.
            return


def attach_usage_meter(component: Any, meter: UsageMeter) -> None:
    if component is None:
        return
    setter = getattr(component, "set_usage_meter", None)
    if callable(setter):
        setter(meter)
    else:
        component.usage_meter = meter
