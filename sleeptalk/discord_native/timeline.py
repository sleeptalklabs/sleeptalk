from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True)
class VoiceTimelineEvent:
    at: float
    type: str
    detail: str = ""


class VoiceTimeline:
    def __init__(self, *, max_events: int = 80) -> None:
        self._events: deque[VoiceTimelineEvent] = deque(maxlen=max_events)

    def record(self, event_type: str, detail: str = "") -> None:
        self._events.append(VoiceTimelineEvent(time.monotonic(), event_type, detail))

    def last(self, count: int = 8) -> list[VoiceTimelineEvent]:
        if count <= 0:
            return []
        return list(self._events)[-count:]

    def compact(self, count: int = 6) -> str:
        events = self.last(count)
        if not events:
            return "events none"
        return "events " + " | ".join(
            event.type if not event.detail else f"{event.type}:{event.detail}"
            for event in events
        )
