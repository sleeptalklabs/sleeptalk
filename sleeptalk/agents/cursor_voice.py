"""Cursor-only voice helpers: tool-speak de-dupe and local SDK options."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from sleeptalk.adapter import ToolPhase

_T = TypeVar("_T")


@dataclass
class ToolSpeakGate:
    last_kind: str = ""

    def allow(self, name: str, phase: ToolPhase) -> bool:
        if phase is not ToolPhase.STARTED:
            return False
        return self.allow_legacy(name)

    def allow_legacy(self, name: str) -> bool:
        kind = name.strip().casefold()
        if not kind or kind == self.last_kind:
            return False
        self.last_kind = kind
        return True


def local_setting_sources() -> tuple[str, ...]:
    return ("project", "user", "plugins")


def build_local_agent_options(
    cwd: str,
    setting_sources: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    sources = setting_sources if setting_sources is not None else local_setting_sources()
    return {"cwd": cwd, "setting_sources": list(sources)}


def wrap_cursor_voice_sink(sink: Callable[..., _T] | None) -> Callable[..., _T] | None:
    if sink is None or getattr(sink, "_skip_legacy_fire", False):
        return sink
    gate = ToolSpeakGate()

    def wrapped(value: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(value, str):
            prefix, sep, kind = value.partition(":")
            if sep and prefix == "tool":
                if not gate.allow_legacy(kind.strip()):
                    return None
        return sink(value, *args, **kwargs)

    return wrapped
