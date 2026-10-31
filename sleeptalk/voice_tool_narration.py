"""Tool-start narration: detail sentences and speak-path helpers."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from sleeptalk.opening_preamble_gate import OpeningPreambleGate
from sleeptalk.session_events import progress


_SHELL_VERBS = frozenset(
    "cd ls cat grep rg sed awk echo git npm npx uv pip python python3 node "
    "make rm mv cp mkdir touch curl wget chmod export source pytest".split()
)


def looks_like_invocation(detail: str) -> bool:
    """True when a detail is a command or path rather than a description.

    Adapters differ: some report "checking new uploads", others hand over the
    literal shell line. Spoken aloud the second kind becomes "cd && git status
    -sb && echo && git log -20", which is what hybrid was reading out.
    """
    text = detail.strip()
    if not text:
        return False
    if any(token in text for token in ("&&", "||", " | ", ";", "`", "$(")):
        return True
    if " --" in text or text.startswith("-"):
        return True
    if "/" in text.split(" ", 1)[0] and " " not in text:
        return True
    return text.split(" ", 1)[0].strip().lower() in _SHELL_VERBS


def _detail_sentence(detail: str) -> str:
    """Gerund detail -> spoken sentence: 'checking new uploads' ->
    'Checking new uploads.' Empty when there's nothing worth saying."""
    detail = detail.strip().rstrip(".")
    if not detail or looks_like_invocation(detail):
        return ""
    return detail[0].upper() + detail[1:] + "."


_KIND_HINTS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("search", "grep", "glob", "find", "fetch", "web"), "search"),
    (("read", "view", "open", "cat"), "read"),
    (("edit", "patch", "apply"), "edit"),
    (("write", "create"), "write"),
    (("bash", "shell", "exec", "run", "terminal", "command"), "bash"),
    (("task", "agent", "subagent", "delegate"), "task"),
    (("mcp",), "mcp"),
)


def narration_kind(tool: str) -> str:
    """Map an adapter's tool name onto a narration phrase key.

    Adapters name the same act differently — WebSearch, Grep, Glob are all
    searching — and an unmapped name falls through to "Working on it", which
    says nothing.
    """
    kind = tool.strip().lower()
    if not kind:
        return "other"
    for needles, mapped in _KIND_HINTS:
        if any(needle in kind for needle in needles):
            return mapped
    return kind


def tool_status_line(tool: str, detail: str = "") -> str:
    """A hearable line for a tool start, never the raw invocation."""
    from sleeptalk.policy import DEFAULT_NARRATION_PHRASES

    sentence = _detail_sentence(detail)
    if sentence:
        return sentence
    kind = narration_kind(tool)
    return DEFAULT_NARRATION_PHRASES.get(
        kind, DEFAULT_NARRATION_PHRASES.get("other", "")
    )


def choose_tool_narration(
    tool: str,
    detail: str,
    phrases: Mapping[str, str],
    spoken_keys: set[str],
    generic_spoken: bool,
) -> tuple[str, str] | None:
    """Pick a hearable line for this tool event, or None to stay quiet.

    Same kind+detail is spoken once per turn. A second empty-detail canned
    line is also skipped, so start/complete pairs do not stutter.
    """
    kind = tool.strip().lower()
    if kind not in phrases:
        kind = "other" if "other" in phrases else kind
    sentence = _detail_sentence(detail)
    if not sentence:
        sentence = str(phrases.get(kind, phrases.get("other", "")))
    if not sentence:
        return None
    key = f"{kind}:{detail.strip().lower()}"
    if key in spoken_keys:
        return None
    if not detail.strip() and generic_spoken:
        return None
    return kind, sentence


async def play_tool_keyboard(
    opening_gate: OpeningPreambleGate,
    *,
    ctx: Any,
    tool: str,
    play_clip: Callable[[str, Any], Awaitable[bool]] | None,
) -> None:
    """Keyboard style: a typing clip stands in for the spoken line.

    Surfaces without clip playback pass ``play_clip=None`` and the tool call
    passes in silence — the same outcome the user would get from the silence
    style, never a crash or a half-played turn.
    """
    if play_clip is None:
        return
    await opening_gate.claim("tool_narration")
    played = await play_clip(tool, ctx)
    if not played:
        return
    ctx.emit(
        progress(
            ctx.session_id,
            ctx.turn_id,
            pool="tool_keyboard",
            text="",
            tool=tool,
        )
    )


async def speak_tool_progression(
    opening_gate: OpeningPreambleGate,
    *,
    ctx: Any,
    narration: str,
    tool: str,
    speak_policy: Callable[[str, Any], Awaitable[None]],
) -> None:
    """Claim the opening slot if free, then speak this tool line.

    Callers decide whether the event is new. This helper only owns the
    opening-slot claim and the speak.
    """
    await opening_gate.claim("tool_narration")
    ctx.emit(
        progress(
            ctx.session_id,
            ctx.turn_id,
            pool="tool_narration",
            text=narration,
            tool=tool,
        )
    )
    await speak_policy(narration, ctx)
