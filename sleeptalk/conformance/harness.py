"""Capability gates for the AgentAdapter conformance suite.

Pytest-free so `sleeptalk adapter test` can run it from an installed package.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from sleeptalk.adapter import (
    AgentCapabilityManifest,
    CapabilitySupport,
    SupportLevel,
    adapter_capabilities,
    typed_events,
)

MANIFEST_FIELDS = frozenset(
    {
        "structured_events",
        "tool_lifecycle",
        "permissions",
        "clarification",
        "session_identity",
        "status",
        "usage",
        "steering",
        "targeted_cancellation",
        "workers",
        "voice_sessions",
    }
)

PROMPT_HELLO = "hello"
PROMPT_HANG = "hang"
PROMPT_SLOW = "slow"
PROMPT_TOOL = "tool"
PROMPT_PERMISSION = "permission"
PROMPT_CLARIFY = "clarify"

PREAMBLE_PHRASES = frozenset(
    {
        "give me a second",
        "one moment",
        "still working",
        "let me think",
        "i'm thinking",
        "hang on",
    }
)


class ConformanceSkip(Exception):
    """The case does not apply to this adapter."""


class ConformanceFailure(AssertionError):
    """The adapter broke the contract it advertised."""


def skip(reason: str) -> None:
    raise ConformanceSkip(reason)


def fail(reason: str) -> None:
    raise ConformanceFailure(reason)


def advertised(adapter: object) -> AgentCapabilityManifest:
    return adapter_capabilities(adapter)


def _support(adapter: object, field: str) -> CapabilitySupport:
    if field not in MANIFEST_FIELDS:
        raise AttributeError(f"unknown capability field: {field}")
    value = getattr(advertised(adapter), field)
    if not isinstance(value, CapabilitySupport):
        raise TypeError(f"{field} is not a CapabilitySupport")
    return value


def _skip_reason(field: str, support: CapabilitySupport) -> str:
    if support.reason:
        return f"{field} is {support.level.value}: {support.reason}"
    return f"{field} is {support.level.value}"


def skip_unless(adapter: object, field: str) -> CapabilitySupport:
    """Skip when the field is unsupported or unavailable.

    A SUPPORTED advertisement always continues, even if later assertions fail.
    """

    support = _support(adapter, field)
    if support.level is SupportLevel.SUPPORTED:
        return support
    skip(_skip_reason(field, support))


require = skip_unless


def missing(field: str) -> None:
    fail(f"{field} advertised SUPPORTED but behavior missing")


def looks_like_preamble(text: str) -> bool:
    lowered = text.strip().lower().rstrip(".!")
    return lowered in PREAMBLE_PHRASES


async def drain(
    adapter: Any,
    text: str,
    *,
    timeout: float = 8.0,
) -> tuple[list[object], list[str]]:
    events: list[object] = []
    sentences: list[str] = []
    sink = typed_events(events.append)

    async def _run() -> None:
        async for sentence in adapter.respond_streaming(text, on_event=sink):
            sentences.append(sentence)

    await asyncio.wait_for(_run(), timeout=timeout)
    return events, sentences


async def collect_until_done(
    stream: AsyncIterator[str],
    *,
    timeout: float = 8.0,
) -> list[str]:
    async def _run() -> list[str]:
        return [sentence async for sentence in stream]

    return await asyncio.wait_for(_run(), timeout=timeout)
