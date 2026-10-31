from __future__ import annotations

import time
from dataclasses import dataclass, field

DEFAULT_GENERAL_FILLERS = (
    "Let me check that.",
    "One second, I am looking.",
    "I am checking now.",
    "Let me pull that together.",
)

DEFAULT_WEB_SEARCH_FILLERS = (
    "Let me check that.",
    "Checking now.",
    "I'll look that up.",
    "One second, checking.",
    "Let me verify that.",
)


@dataclass(frozen=True)
class FillerCatalog:
    general: tuple[str, ...] = DEFAULT_GENERAL_FILLERS
    web_search: tuple[str, ...] = DEFAULT_WEB_SEARCH_FILLERS

    @classmethod
    def from_strings(
        cls,
        *,
        general: str = "",
        web_search: str = "",
    ) -> FillerCatalog:
        return cls(
            general=_parse_fillers(general, DEFAULT_GENERAL_FILLERS),
            web_search=_parse_fillers(web_search, DEFAULT_WEB_SEARCH_FILLERS),
        )

    def select(self, category: str, index: int = 0) -> str:
        choices = self._choices(category)
        return choices[index % len(choices)]

    def _choices(self, category: str) -> tuple[str, ...]:
        if category == "web_search":
            return self.web_search
        return self.general


@dataclass
class FirstTokenFiller:
    budget_ms: int = 800
    category: str = "general"
    filler_index: int = 0
    catalog: FillerCatalog = field(default_factory=FillerCatalog)
    started_at: float = field(default_factory=time.monotonic)
    emitted: bool = False

    @property
    def budget_seconds(self) -> float:
        return max(self.budget_ms, 0) / 1000

    def remaining_seconds(self, now: float | None = None) -> float:
        now = time.monotonic() if now is None else now
        return max(0.0, self.started_at + self.budget_seconds - now)

    def mark_first_token(self) -> None:
        self.emitted = True

    def due(self, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        return not self.emitted and now - self.started_at >= self.budget_seconds

    def emit(self) -> str:
        self.emitted = True
        return self.catalog.select(self.category, self.filler_index)


@dataclass
class SpeculationStats:
    attempts: int = 0
    accepted: int = 0
    discarded: int = 0
    token_overhead: int = 0

    @property
    def discard_rate(self) -> float:
        if not self.attempts:
            return 0.0
        return self.discarded / self.attempts

    def record_attempt(self, *, estimated_tokens: int = 0) -> None:
        self.attempts += 1
        self.token_overhead += max(estimated_tokens, 0)

    def record_accept(self) -> None:
        self.accepted += 1

    def record_discard(self) -> None:
        self.discarded += 1


def _parse_fillers(raw: str, fallback: tuple[str, ...]) -> tuple[str, ...]:
    parsed = tuple(part.strip() for part in raw.split("|") if part.strip())
    return parsed or fallback
