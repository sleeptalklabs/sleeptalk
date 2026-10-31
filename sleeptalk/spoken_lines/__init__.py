"""Hand-written spoken lines per language: the first thing the assistant says.

Cascade has no model between a canned line and the voice, so the instant
ack, the reassure lines and the tool narration are plain strings. English
lives in ``sleeptalk.policy``; every other language lives here, one module
per primary BCP-47 subtag, with the same keys.

Rules the tables follow (``tests/test_spoken_lines.py`` enforces the
mechanical ones):

- Every pool key exists and is non-empty. ``VoicePolicy.from_config``
  replaces the pools wholesale, so a missing key is a KeyError mid-call.
- Lines are short: they play *before* the answer, so every syllable is
  latency. Nothing longer than 80 characters.
- Each line ends in the language's sentence mark, so the TTS engine gets a
  falling contour instead of a clipped one.
- The empathy pool stays neutral, never cheerful.
- Nothing is machine-translated. A line is written by, or checked with,
  a speaker of the language before it ships; the module docstring says
  which.

Delivery is through the profile: ``wizard.profile_toml`` writes a table's
pools into ``[policy]`` for a non-English language, and cascade runtime
reads them the way it reads any override. Runtime code never imports this
package.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
from types import ModuleType

#: Primary subtags with a table. Order is the order the wizard lists them.
AVAILABLE: tuple[str, ...] = ("es", "fr", "de", "pt", "it", "nl", "pl", "hi", "ur", "ja", "ko", "zh")

POOL_KEYS: tuple[str, ...] = (
    "ack", "action", "ack_action", "action:task", "ack:empathy",
    "working", "control", "greeting", "cron_offer", "reassure",
)
TOOL_KEYS: tuple[str, ...] = ("read", "edit", "write", "bash", "search", "other")
NARRATION_KEYS: tuple[str, ...] = ("read", "edit", "write", "bash", "search", "task", "mcp", "other")

MAX_LINE_CHARS = 80
#: Sentence-final marks by script; a line must end in one of them.
SENTENCE_MARKS = ".!?。！？।؟۔"  # includes the Urdu full stop U+06D4


@dataclass(frozen=True)
class SpokenLines:
    language: str
    preamble_pools: dict[str, tuple[str, ...]]
    reassure_tool_pools: dict[str, tuple[str, ...]]
    narration_phrases: dict[str, str]

    def as_policy_overrides(self) -> dict[str, dict]:
        """The ``[policy]`` sub-tables a profile carries for this language."""
        return {
            "preamble_pools": {key: list(lines) for key, lines in self.preamble_pools.items()},
            "reassure_tool_pools": {key: list(lines) for key, lines in self.reassure_tool_pools.items()},
            "narration_phrases": dict(self.narration_phrases),
        }


def primary(language: str) -> str:
    return (language or "").strip().lower().replace("_", "-").split("-", 1)[0]


def available() -> tuple[str, ...]:
    return AVAILABLE


def _module(tag: str) -> ModuleType:
    return import_module(f"sleeptalk.spoken_lines.{tag}")


def for_language(language: str) -> SpokenLines | None:
    """The table for a language, or None when there is none (English included).

    English is None on purpose: the policy defaults *are* the English table,
    and a profile that carries no override keeps them.
    """
    tag = primary(language)
    if tag not in AVAILABLE:
        return None
    module = _module(tag)
    return SpokenLines(
        language=tag,
        preamble_pools={key: tuple(lines) for key, lines in module.PREAMBLE_POOLS.items()},
        reassure_tool_pools={key: tuple(lines) for key, lines in module.REASSURE_TOOL_POOLS.items()},
        narration_phrases=dict(module.NARRATION_PHRASES),
    )


def english() -> SpokenLines:
    """The English table, as the policy defaults define it."""
    from sleeptalk.policy import (
        DEFAULT_NARRATION_PHRASES,
        DEFAULT_PREAMBLE_POOLS,
        DEFAULT_REASSURE_TOOL_POOLS,
    )

    return SpokenLines(
        language="en",
        preamble_pools=dict(DEFAULT_PREAMBLE_POOLS),
        reassure_tool_pools=dict(DEFAULT_REASSURE_TOOL_POOLS),
        narration_phrases=dict(DEFAULT_NARRATION_PHRASES),
    )


def check(lines: SpokenLines) -> list[str]:
    """Mechanical problems with a table; empty when it is well-formed."""
    problems: list[str] = []
    for key in POOL_KEYS:
        if not lines.preamble_pools.get(key):
            problems.append(f"{lines.language}: preamble pool '{key}' is missing or empty")
    for key in TOOL_KEYS:
        if not lines.reassure_tool_pools.get(key):
            problems.append(f"{lines.language}: reassure tool pool '{key}' is missing or empty")
    for key in NARRATION_KEYS:
        if not lines.narration_phrases.get(key):
            problems.append(f"{lines.language}: narration phrase '{key}' is missing or empty")
    spoken: list[tuple[str, str]] = []
    for key, pool in lines.preamble_pools.items():
        spoken += [(f"preamble_pools.{key}", line) for line in pool]
    for key, pool in lines.reassure_tool_pools.items():
        spoken += [(f"reassure_tool_pools.{key}", line) for line in pool]
    spoken += [(f"narration_phrases.{key}", line) for key, line in lines.narration_phrases.items()]
    for where, line in spoken:
        if line != line.strip():
            problems.append(f"{lines.language}: {where} has leading/trailing whitespace: {line!r}")
        if len(line) > MAX_LINE_CHARS:
            problems.append(f"{lines.language}: {where} is longer than {MAX_LINE_CHARS} chars: {line!r}")
        if not line or line[-1] not in SENTENCE_MARKS:
            problems.append(f"{lines.language}: {where} does not end in a sentence mark: {line!r}")
    return problems


__all__ = [
    "AVAILABLE",
    "NARRATION_KEYS",
    "POOL_KEYS",
    "SpokenLines",
    "TOOL_KEYS",
    "available",
    "check",
    "english",
    "for_language",
    "primary",
]
