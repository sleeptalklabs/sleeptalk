"""Full-screen terminal Frame for the Sleeptalk voice being.

Instrument, not chat. Near-monochrome, one amber accent for whatever is
live, a speaker gutter, two rules. The unpinned orb stays geometry.
"""

from __future__ import annotations

import math
import os
import re
import shutil
import sys
import textwrap
import threading
import time
from dataclasses import dataclass, replace
from typing import Literal, NamedTuple, TextIO

from sleeptalk.presentation import Line, PresentationState
from sleeptalk.speech_speed import format_speed
from sleeptalk.transports.terminal_controls import SettingsMenuView

# Status-bar words for reply_length. "summarized" is too long to sit in a
# footer, which is the whole reason the display vocabulary is its own map.
_REPLY_TOKENS = {"full": "full", "summarized": "brief", "concise": "concise"}


def reply_token(length: object) -> str:
    return _REPLY_TOKENS.get(str(length or "").strip().lower(), "full")


# Footer key hints, in the order they are shown.
_KEY_HINTS = (
    ("Space", "stop"),
    ("m", "mute"),
    ("s", "settings"),
    ("q", "quit"),
)


STATES = {"idle", "listening", "thinking", "speaking"}
_DENSITY = " .:-=+*#%@"
_ANSI_RE = re.compile(r"\033\[[0-9;]*m")
_RESET = "\033[0m"
_LABEL = 6
_GAP = 2
_FEED_MAX = 80
_PRODUCT = "sleeptalk"

# Dimness is the type scale. Amber is reserved for now.
_SPEECH = ""
_USER = "\033[2m"
_TOOL = "\033[2;3m"
_META = "\033[2m"
_LIVE = "\033[3;33m"
_AMBER = "\033[33m"
_KEY = ""
_HINT = "\033[2m"
ENTER_S = 0.28

UserSpeakingKind = Literal["none", "dots", "partial"]


@dataclass(frozen=True)
class UserSpeakingHint:
    kind: UserSpeakingKind
    text: str = ""


def user_speaking_hint(*, partial: str, hearing: bool) -> UserSpeakingHint:
    text = (partial or "").strip()
    if text:
        return UserSpeakingHint("partial", text)
    if hearing:
        return UserSpeakingHint("dots")
    return UserSpeakingHint("none")


def listening_dots(phase: float, *, ascii_only: bool | None = None) -> str:
    if ascii_only is None:
        ascii_only = _ascii()
    frames = (".  ", " . ", "  .") if ascii_only else ("●··", "·●·", "··●")
    return frames[int(phase * 3) % 3]


def render_frame(
    state: str,
    phase: float,
    *,
    active: bool = False,
    detail: str = "",
    width: int = 29,
    height: int = 11,
) -> tuple[str, ...]:
    """Return one ASCII frame; geometry, not colour, communicates state."""

    if state not in STATES:
        raise ValueError(f"unknown being state: {state}")
    width = max(width, 21)
    height = max(height, 7)
    center_x = (width - 1) / 2
    center_y = (height - 1) / 2
    base = {
        "idle": 3.55,
        "listening": 2.25 if not active else 2.65,
        "thinking": 3.45,
        "speaking": 4.25,
    }[state]
    breathe = 0.10 * math.sin(phase * (0.8 if state == "idle" else 1.7))
    pulse_radius = base + 1.1 + (phase * 0.75) % 2.2
    rows: list[str] = []

    for row in range(height):
        chars: list[str] = []
        for column in range(width):
            x = (column - center_x) * 0.52
            y = row - center_y
            distance = math.hypot(x, y)
            angle = math.atan2(y, x)
            deformation = 0.0
            if state == "thinking":
                deformation = (
                    0.24 * math.sin(3 * angle + phase * 1.3)
                    + 0.13 * math.sin(5 * angle - phase * 0.8)
                )
            elif state == "speaking":
                syllable = max(0.0, math.sin(phase * 6.8))
                deformation = syllable * (
                    0.42 * math.sin(2 * angle + phase)
                    + 0.24 * math.sin(4 * angle - phase * 1.7)
                )
            radius = base + breathe + deformation
            edge = radius - distance
            level = 0.0
            if edge >= 0:
                level = min(1.0, 0.22 + edge / max(radius, 0.1))
                if state == "thinking":
                    level *= 0.72 + 0.28 * math.sin(angle * 2 - phase * 1.6) ** 2
                elif state == "speaking":
                    level *= 0.78 + 0.22 * max(0.0, math.sin(phase * 7.1 + angle * 3))
            elif state == "listening":
                ring_distance = abs(distance - pulse_radius)
                if ring_distance < 0.18:
                    level = 0.34 * (1 - ring_distance / 0.18)
            index = min(
                len(_DENSITY) - 1,
                max(0, round(level * (len(_DENSITY) - 1))),
            )
            chars.append(_DENSITY[index])
        rows.append("".join(chars).rstrip().ljust(width))

    label = state.upper() + (
        " / HEARING YOU" if state == "listening" and active else ""
    )
    rows.append(label.center(width)[:width].ljust(width))
    rows.append(detail.center(width)[:width].ljust(width) if detail else " " * width)
    return tuple(rows)


def line_label(line: Line) -> str:
    parts = [line.text]
    if line.plus is not None:
        parts.append(f"+{line.plus}")
    if line.minus is not None:
        parts.append(f"-{line.minus}")
    return "  ".join(parts)


def visible_len(text: str) -> int:
    return len(_ANSI_RE.sub("", text))


def pad_visible(text: str, width: int) -> str:
    return text + " " * max(0, width - visible_len(text))


def center_visible(text: str, width: int) -> str:
    extra = max(0, width - visible_len(text))
    left = extra // 2
    return (" " * left) + text + (" " * (extra - left))


def color_enabled(stream: TextIO | None = None) -> bool:
    if os.environ.get("NO_COLOR", "").strip():
        return False
    if str(os.environ.get("SLEEPTALK_NO_ASCII", "")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return False
    if stream is not None and not getattr(stream, "isatty", lambda: False)():
        return False
    return True


class FeedRow(NamedTuple):
    kind: str
    text: str
    label: str = ""
    live: bool = False
    plus: int | None = None
    minus: int | None = None
    enter: int = 0


def short_model(value: str) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    text = text.rsplit("/", 1)[-1]
    text = re.sub(r"-\d{8}$", "", text)
    for prefix in ("claude-", "gpt-4o-", "gpt-"):
        if text.startswith(prefix):
            text = text[len(prefix) :]
            break
    return text[:24]


def _slug(value: str) -> str:
    return (value or "").strip().replace("_", "-")


def _pair(provider: str, model: str = "") -> str:
    left = _slug(provider)
    right = short_model(model)
    if left and right:
        return f"{left}/{right}"
    return left or right


def stack_from_config(config: object | None) -> tuple[str, ...]:
    """Compact running stack from the profile. Skip missing sections."""
    if config is None:
        return ()
    segments: list[str] = []
    agent = getattr(config, "agent", None)
    route = getattr(config, "route", None)
    if agent is not None:
        kind = _slug(str(getattr(agent, "type", "") or ""))
        model = short_model(str(getattr(agent, "model", "") or ""))
        thinking = str(getattr(agent, "thinking", "") or "").strip()
        if kind:
            piece = _pair(kind, model) if model else kind
            if thinking:
                piece = f"{piece}:{thinking}"
            segments.append(piece)
    route_type = str(getattr(route, "type", "") or "") if route is not None else ""
    if route_type == "hybrid":
        realtime = short_model(str(getattr(route, "model", "") or "")) or "realtime"
        if realtime.startswith("realtime-"):
            segments.append(f"realtime/{realtime.split('-', 1)[1]}")
        elif realtime == "realtime":
            segments.append("realtime")
        else:
            segments.append(_pair("realtime", realtime))
        return tuple(segments)
    stt = getattr(config, "stt", None)
    tts = getattr(config, "tts", None)
    if stt is not None:
        piece = _pair(
            str(getattr(stt, "provider", "") or ""),
            str(getattr(stt, "model", "") or ""),
        )
        if piece:
            segments.append(piece)
    if tts is not None:
        piece = _pair(
            str(getattr(tts, "provider", "") or ""),
            str(getattr(tts, "model", "") or ""),
        )
        if piece:
            segments.append(piece)
    return tuple(segments)


# Names that say nothing about which agent this is: a Hermes profile called
# "default", or the config default. The profile's own name is shown instead.
_PLACEHOLDER_NAMES = frozenset({"default", "sleeptalk", "agent", "main"})


def agent_display_name(agent: object) -> str:
    """The name a call shows for its agent: its name, unless that is a placeholder."""
    name = str(getattr(agent, "name", "") or "").strip()
    slug = str(getattr(agent, "slug", "") or "").strip()
    if slug and (not name or (name.lower() in _PLACEHOLDER_NAMES and slug.lower() not in _PLACEHOLDER_NAMES)):
        return slug
    return name or slug


def speaker_tag(name: str) -> str:
    raw = (name or "agent").strip().lower()
    parts = [part for part in re.split(r"[^a-z0-9]+", raw) if part]
    if not parts:
        return "agent"
    if len(parts) >= 2:
        return "".join(part[0] for part in parts)[:_LABEL]
    token = parts[0]
    return token if len(token) <= _LABEL else token[:3]


def _tint(code: str, text: str, *, color: bool) -> str:
    if not text or not color or not code:
        return text
    return f"{code}{text}{_RESET}"


def _count_plain(plus: int | None, minus: int | None) -> str:
    bits: list[str] = []
    if plus is not None:
        bits.append(f"+{plus}")
    if minus is not None:
        bits.append(f"-{minus}")
    return " ".join(bits)


def _gap_before(prev: str, kind: str) -> bool:
    if not prev:
        return False
    if kind in {"tool", "progress", "notes", "worker"} and prev in {
        "assistant",
        "tool",
        "progress",
        "notes",
        "worker",
    }:
        return False
    return True


@dataclass(frozen=True)
class SpeechBlock:
    kind: str
    text: str
    plus: int | None = None
    minus: int | None = None
    words: tuple[str, ...] = ()


def _line_words(line: Line) -> tuple[str, ...]:
    if line.words:
        return tuple(word.text for word in line.words)
    return tuple(line.text.strip().split())


def speech_blocks(state: PresentationState) -> tuple[SpeechBlock, ...]:
    blocks: list[SpeechBlock] = []
    for line in state.lines:
        text = " ".join([line.text.strip(), *line.links]).strip()
        if not text:
            continue
        tokens = _line_words(line)
        if blocks and blocks[-1].kind == line.kind and line.kind in {"assistant", "user"}:
            prev = blocks[-1]
            blocks[-1] = SpeechBlock(
                prev.kind,
                f"{prev.text} {text}",
                prev.plus,
                prev.minus,
                (*prev.words, *tokens),
            )
        else:
            blocks.append(
                SpeechBlock(line.kind, text, line.plus, line.minus, tokens)
            )
    # A running job's latest step or thought, under the conversation: the
    # terminal has no worker panel. With several, each line names its job,
    # two at most, and the rest are counted.
    running = [job for job in state.jobs if job.phase == "running" and job.entries]
    waiting = [job for job in state.jobs if job.phase == "queued"]
    several = len(running) + len(waiting) > 1
    for job in running[:2]:
        latest = job.entries[-1]
        count = f" ({job.steps} steps)" if job.steps > 1 else ""
        title = f"{job.title} · " if several else ""
        blocks.append(SpeechBlock("worker", f"{title}{latest.text}{count}"))
    more = max(0, len(running) - 2) + len(waiting)
    if running and more:
        blocks.append(SpeechBlock("worker", f"+{more} more"))
    return tuple(blocks)


def conversation_rows(
    state: PresentationState,
    width: int,
    *,
    agent: str = "agent",
) -> list[FeedRow]:
    """Group same-kind speech into paragraphs, then wrap to ``width``."""
    width = max(int(width), 8)
    tag = speaker_tag(agent)
    rows: list[FeedRow] = []
    prev_kind = ""
    for block in speech_blocks(state):
        if _gap_before(prev_kind, block.kind):
            rows.append(FeedRow("", ""))
        label = {"user": "you", "assistant": tag, "worker": "worker"}.get(block.kind, "")
        reserved = len(_count_plain(block.plus, block.minus))
        wrap_width = width if not reserved else max(8, width - reserved - 1)
        wrapped = textwrap.wrap(
            block.text,
            width=wrap_width,
            break_long_words=True,
            break_on_hyphens=False,
        ) or [""]
        last = len(wrapped) - 1
        for offset, piece in enumerate(wrapped):
            tail = offset == last
            rows.append(
                FeedRow(
                    block.kind,
                    piece,
                    label if offset == 0 else "",
                    False,
                    block.plus if tail else None,
                    block.minus if tail else None,
                )
            )
        prev_kind = block.kind
    partial = state.partial.strip()
    if partial:
        if rows and (rows[-1].kind or rows[-1].text):
            rows.append(FeedRow("", ""))
        wrapped = textwrap.wrap(
            partial,
            width=max(8, width - 1),
            break_long_words=True,
            break_on_hyphens=False,
        ) or [partial]
        last = len(wrapped) - 1
        for offset, piece in enumerate(wrapped):
            rows.append(
                FeedRow(
                    "user",
                    piece + ("|" if offset == last else ""),
                    "you" if offset == 0 else "",
                    True,
                )
            )
    return rows


@dataclass(frozen=True)
class ParaHold:
    settled: int
    hold_from: int
    started: float | None

    @staticmethod
    def settled_at(total: int) -> ParaHold:
        return ParaHold(settled=total, hold_from=total, started=None)


@dataclass(frozen=True)
class RevealView:
    paras: tuple[ParaHold, ...] = ()

    def entering_words(self, paragraph: int, total: int) -> int:
        if paragraph < 0 or paragraph >= len(self.paras):
            return 0
        hold = self.paras[paragraph]
        if hold.started is None:
            return 0
        return max(0, int(total) - hold.hold_from)


def step_reveal(
    prev: RevealView,
    state: PresentationState,
    *,
    now: float,
    enter_s: float = ENTER_S,
) -> RevealView:
    blocks = [block for block in speech_blocks(state) if block.kind == "assistant"]
    out: list[ParaHold] = []
    for index, block in enumerate(blocks):
        total = len(block.words)
        old = prev.paras[index] if index < len(prev.paras) else None
        if old is None or total < old.settled:
            out.append(ParaHold.settled_at(total))
            continue
        started = old.started
        hold_from = old.hold_from
        settled = old.settled
        if total > settled and started is None:
            hold_from = settled
            started = now
        if started is not None and (now - started) >= enter_s:
            out.append(ParaHold.settled_at(total))
        else:
            out.append(ParaHold(settled=settled, hold_from=hold_from, started=started))
    return RevealView(tuple(out))


def apply_frame_cues(
    rows: list[FeedRow],
    *,
    hint: UserSpeakingHint,
    reveal: RevealView,
    phase: float,
    ascii_only: bool | None = None,
) -> list[FeedRow]:
    rows = _mark_enter(rows, reveal)
    if hint.kind != "dots":
        return rows
    if rows and (rows[-1].kind or rows[-1].text):
        rows = [*rows, FeedRow("", "")]
    return [
        *rows,
        FeedRow("user", listening_dots(phase, ascii_only=ascii_only), "you", True),
    ]


def _mark_enter(rows: list[FeedRow], reveal: RevealView) -> list[FeedRow]:
    out = list(rows)
    paragraph = 0
    index = 0
    while index < len(out):
        if out[index].kind != "assistant":
            index += 1
            continue
        start = index
        while index < len(out) and out[index].kind == "assistant":
            index += 1
        total = sum(len(out[slot].text.split()) for slot in range(start, index))
        remaining = reveal.entering_words(paragraph, total)
        paragraph += 1
        if remaining <= 0:
            continue
        for slot in range(index - 1, start - 1, -1):
            if remaining <= 0:
                break
            take = min(remaining, len(out[slot].text.split()))
            if take:
                out[slot] = out[slot]._replace(enter=take)
                remaining -= take
    return out


def _split_trailing_words(text: str, count: int) -> tuple[str, str]:
    words = text.split()
    if count <= 0 or not words:
        return text, ""
    n = min(int(count), len(words))
    head = words[:-n]
    tail = " ".join(words[-n:])
    if not head:
        return "", tail
    return " ".join(head) + " ", tail


def timeline_rows(state: PresentationState) -> list[str]:
    return [line_label(line) for line in state.lines]


def _ascii() -> bool:
    return str(os.environ.get("SLEEPTALK_NO_ASCII", "")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _rule(width: int, *, color: bool) -> str:
    glyph = "-" if _ascii() else "─"
    return _tint(_META, glyph * width, color=color)


def _meter(phase: float, *, ascii_only: bool) -> str:
    if ascii_only:
        wave = ".=-="
        return "".join(wave[int(phase * 3 + index) % len(wave)] for index in range(5))
    glyphs = "▁▂▃▄▅"
    return "".join(
        glyphs[max(0, min(4, int(2.2 + 2.2 * math.sin(phase * 2.1 + index * 0.9))))]
        for index in range(5)
    )


def _paint_body(row: FeedRow, *, color: bool) -> str:
    if not row.text and row.plus is None and row.minus is None:
        return ""
    if row.enter:
        head, tail = _split_trailing_words(row.text, row.enter)
        dim = _tint(_USER, tail, color=color)
        return f"{head}{dim}" if head else dim
    if row.live:
        return _tint(_LIVE, row.text, color=color)
    if row.kind == "assistant":
        return row.text
    if row.kind == "user":
        return _tint(_USER, row.text, color=color)
    mark = "* " if _ascii() else "· "
    return _tint(_TOOL, f"{mark}{row.text}", color=color)


def _compose_feed(row: FeedRow, feed_width: int, *, color: bool) -> str:
    label = _tint(_META, (row.label or "").ljust(_LABEL)[:_LABEL], color=color)
    body = _paint_body(row, color=color)
    if row.kind in {"tool", "progress", "notes", "worker"}:
        body = f"  {body}"
    meta = _count_plain(row.plus, row.minus)
    if meta:
        meta_s = _tint(_META, meta, color=color)
        used = visible_len(body) + visible_len(meta_s)
        pad = max(1, feed_width - used)
        body = f"{body}{' ' * pad}{meta_s}"
    return f"{label}{' ' * _GAP}{body}"


def _header_bits(
    *,
    agent: str,
    surface: str,
    stack: tuple[str, ...],
) -> list[str]:
    bits: list[str] = []
    if agent:
        bits.append(agent)
    bits.extend(part for part in stack if part and part != agent)
    if not stack and surface:
        bits.append(surface)
    return bits


def _header(
    *,
    agent: str,
    surface: str,
    product: str,
    stack: tuple[str, ...] = (),
    latency_ms: int | None,
    columns: int,
    color: bool,
    live: bool,
) -> str:
    name = product or _PRODUCT
    shown = agent if agent and agent != name else ""
    extra = _header_bits(agent=shown, surface=surface, stack=stack)
    orb = "*" if _ascii() else "●"
    right_live = _tint(_AMBER if live else _META, f"{orb} live", color=color)
    latency = (
        _tint(_META, f" {latency_ms}ms", color=color) if latency_ms is not None else ""
    )
    right = f"{right_live}{latency}"
    kept = list(extra)
    while True:
        rest = _tint(_META, f" · {' · '.join(kept)}", color=color) if kept else ""
        pad = columns - visible_len(name) - visible_len(rest) - visible_len(right)
        if pad >= 1 or len(kept) <= 1:
            break
        kept.pop(0)
    if pad < 1:
        pad = 1
    return f"{name}{rest}{' ' * pad}{right}"


def _footer(
    state: PresentationState,
    phase: float,
    *,
    muted: bool,
    columns: int,
    color: bool,
    settings_open: bool = False,
) -> str:
    word = "muted" if muted else state.presence
    live = word in {"listening", "thinking", "speaking", "muted"}
    orb = "*" if _ascii() else "●"
    left = _tint(_AMBER if live else _META, f"{orb} {word}", color=color)
    meter = _tint(_META, f" {_meter(phase, ascii_only=_ascii())}", color=color)
    latency = (
        _tint(_META, f" {state.latency_ms}ms", color=color)
        if state.latency_ms is not None
        else ""
    )
    speaking = word in {"thinking", "speaking"}
    def keys(words: bool, *, keep_settings_label: bool = False) -> str:
        parts = []
        for key, hint in _KEY_HINTS:
            if key == "s" and settings_open:
                hint = "close"
            tinted = _tint(
                _AMBER if speaking and key == "Space" else _KEY,
                key,
                color=color,
            )
            show_hint = words or (keep_settings_label and key == "s")
            parts.append(
                f"{tinted} {_tint(_HINT, hint, color=color)}" if show_hint else tinted
            )
        return " · ".join(parts)

    mid = f"{left}{meter}{latency}"
    right = keys(True)
    if visible_len(mid) + visible_len(right) >= columns:
        right = keys(False, keep_settings_label=True)
    if visible_len(mid) + visible_len(right) >= columns:
        mid = f"{left}{latency}"
    pad = max(1, columns - visible_len(mid) - visible_len(right))
    return f"{mid}{' ' * pad}{right}"


def _settings_summary(
    state: PresentationState, *, columns: int, color: bool
) -> str:
    narration = {
        "narrated": "narrated",
        "silence": "silent",
        "keyboard": "keyboard",
    }.get(str(state.narration_style or "").strip().lower(), "narrated")
    reply = reply_token(state.reply_length)
    speed = format_speed(state.tts_speed)
    if columns >= 58:
        text = f"voice  tools: {narration} · response: {reply} · speed: {speed}"
    else:
        text = f"voice  {narration} · {reply} · {speed}"
    return _tint(_META, text, color=color)


def _menu_line(text: str, *, columns: int, color: bool, focused: bool = False) -> str:
    limit = max(1, columns - 2)
    if len(text) > limit:
        text = text[: max(1, limit - 1)].rstrip() + "…"
    return "  " + _tint(_AMBER if focused else _KEY, text, color=color)


def _settings_menu_rows(
    menu: SettingsMenuView, *, columns: int, color: bool
) -> tuple[str, ...]:
    rows = [_menu_line(menu.title, columns=columns, color=color), ""]
    value_width = max((len(row.value) for row in menu.rows), default=0)
    for row in menu.rows:
        marker = ">" if row.focused else " "
        current = " current" if row.current else ""
        left = f"{marker} {row.label}"
        room = max(2, columns - 4 - len(left) - value_width - len(current))
        value = row.value.rjust(value_width) if row.value else ""
        line = f"{left}{' ' * room}{value}{current}".rstrip()
        rows.append(
            _menu_line(
                line,
                columns=columns,
                color=color,
                focused=row.focused,
            )
        )
    if menu.detail:
        rows.extend(("", _menu_line(menu.detail, columns=columns, color=color)))
    rows.append("")
    if columns >= 54:
        help_text = (
            f"Up/Down move · Enter {menu.enter_label} · Esc {menu.escape_label}"
        )
    else:
        help_text = f"↑/↓ · Enter {menu.enter_label} · Esc {menu.escape_label}"
    rows.append(_menu_line(help_text, columns=columns, color=color))
    return tuple(rows)


def render_live_screen(
    state: PresentationState,
    phase: float,
    *,
    columns: int = 80,
    rows: int = 24,
    muted: bool = False,
    color: bool = False,
    agent: str = "agent",
    surface: str = "terminal",
    product: str = _PRODUCT,
    stack: tuple[str, ...] = (),
    hearing: bool = False,
    reveal: RevealView | None = None,
    settings: SettingsMenuView | None = None,
) -> tuple[str, ...]:
    """Instrument Frame: header, guttered transcript, console strip."""

    columns = max(int(columns), 40)
    rows = max(int(rows), 16)
    canvas = [" " * columns for _ in range(rows)]
    feed_width = min(_FEED_MAX, max(8, columns - _LABEL - _GAP - 2))

    def put(row: int, text: str) -> None:
        if 0 <= row < rows:
            canvas[row] = pad_visible(text, columns)

    live = (not muted) and state.presence in {"listening", "thinking", "speaking"}
    put(
        0,
        _header(
            agent=agent,
            surface=surface,
            product=product,
            stack=stack,
            latency_ms=state.latency_ms,
            columns=columns,
            color=color,
            live=live or muted,
        ),
    )
    put(1, _rule(columns, color=color))
    if settings is not None:
        for row, text in enumerate(
            _settings_menu_rows(settings, columns=columns, color=color), start=2
        ):
            if row >= rows - 3:
                break
            put(row, text)
    else:
        budget = max(1, rows - 5)
        hint = user_speaking_hint(partial=state.partial, hearing=hearing and not muted)
        visible = apply_frame_cues(
            conversation_rows(state, feed_width, agent=agent),
            hint=hint,
            reveal=reveal or RevealView(),
            phase=phase,
        )[-budget:]
        while visible and not visible[0].kind and not visible[0].text:
            visible = visible[1:]
        row = 2
        for feed in visible:
            put(row, _compose_feed(feed, feed_width, color=color))
            row += 1
            if row >= rows - 3:
                break
    put(rows - 3, _settings_summary(state, columns=columns, color=color))
    put(rows - 2, _rule(columns, color=color))
    put(
        rows - 1,
        _footer(
            state,
            phase,
            muted=muted,
            columns=columns,
            color=color,
            settings_open=settings is not None,
        ),
    )
    return tuple(canvas)


@dataclass
class TerminalBeing:
    """Animate the voice being as a pinned stage above terminal output."""

    stream: TextIO = sys.stdout
    enabled: bool | None = None
    fps: float = 8.0

    def __post_init__(self) -> None:
        if self.enabled is None:
            self.enabled = bool(getattr(self.stream, "isatty", lambda: False)())
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._closed = threading.Event()
        self._state = "idle"
        self._active = False
        self._detail = ""
        self._presentation = PresentationState()
        self._settings_menu: SettingsMenuView | None = None
        self._hearing = False
        self._reveal = RevealView()
        self._muted = False
        self._agent = "agent"
        self._surface = "terminal"
        self._product = _PRODUCT
        self._stack: tuple[str, ...] = ()
        self._context: tuple[str, ...] = ()
        self._visible = False
        self._drawn_lines = 0
        self._pinned = False
        self._pinned_rows = 0
        self._terminal_rows = 0
        self._started_at = time.perf_counter()
        self._thread: threading.Thread | None = None
        if self.enabled:
            self._thread = threading.Thread(
                target=self._animate,
                name="terminal-being",
                daemon=True,
            )
            self._thread.start()

    def set(self, state: str, *, active: bool = False, detail: str = "") -> None:
        if state not in STATES:
            raise ValueError(f"unknown being state: {state}")
        if not self.enabled:
            return
        with self._lock:
            self._state = state
            self._active = active
            self._detail = detail
            self._presentation = replace(self._presentation, presence=state)
            self._visible = True
            self._started_at = time.perf_counter()
            self._draw_locked()
        self._wake.set()

    def set_hearing(self, hearing: bool) -> None:
        if not self.enabled:
            return
        with self._lock:
            if self._hearing is hearing:
                return
            self._hearing = bool(hearing)
            if self._visible:
                self._draw_locked()
        self._wake.set()

    def set_presentation(self, state: PresentationState, *, muted: bool | None = None) -> None:
        if not self.enabled:
            return
        if state.presence not in STATES:
            raise ValueError(f"unknown being state: {state.presence}")
        with self._lock:
            self._presentation = state
            self._state = state.presence
            if muted is not None:
                self._muted = muted
            self._visible = True
            self._draw_locked()
        self._wake.set()

    def set_settings_menu(self, settings: SettingsMenuView | None) -> None:
        if not self.enabled:
            return
        with self._lock:
            if self._settings_menu == settings:
                return
            self._settings_menu = settings
            if self._visible:
                self._draw_locked()
        self._wake.set()

    def set_identity(
        self,
        *,
        agent: str = "",
        surface: str = "terminal",
        product: str = _PRODUCT,
        stack: tuple[str, ...] = (),
    ) -> None:
        if not self.enabled:
            return
        with self._lock:
            if agent.strip():
                self._agent = agent.strip()
            self._surface = surface.strip() or "terminal"
            self._product = product.strip() or _PRODUCT
            self._stack = tuple(part for part in stack if part)
            if self._visible:
                self._draw_locked()

    def set_context(self, lines: tuple[str, ...]) -> None:
        """Set persistent UI text rendered beside the animated being."""

        if not self.enabled:
            return
        with self._lock:
            self._context = lines
            if self._visible:
                self._draw_locked()

    def clear(self) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._visible = False
            if not self._pinned:
                self._clear_locked()

    def pin_top(self) -> None:
        """Reserve a sticky voice stage and a scrolling work region below it."""

        if not self.enabled:
            return
        with self._lock:
            self._clear_locked()
            size = shutil.get_terminal_size(fallback=(80, 24))
            self._pinned = True
            self._pinned_rows = size.lines
            self._terminal_rows = size.lines
            self._visible = True
            self.stream.write("\033[2J\033[H\033[?25l")
            self._draw_pinned_locked(self._current_frame())
            self.stream.flush()
        self._wake.set()

    def unpin_top(self, *, clear_screen: bool = False) -> None:
        """Restore the terminal's normal full-height scrolling region."""

        if not self.enabled:
            return
        with self._lock:
            if not self._pinned:
                return
            self._pinned = False
            self._visible = False
            self._pinned_rows = 0
            self.stream.write("\033[r\033[?25h")
            if clear_screen:
                self.stream.write("\033[2J\033[H")
            else:
                row = max(1, self._terminal_rows)
                self.stream.write(f"\033[{row};1H\n")
            self.stream.flush()
            self._terminal_rows = 0
            self._drawn_lines = 0

    def write_output(self, text: str, *, stream: TextIO | None = None) -> None:
        """Write output atomically with respect to animation redraws."""

        target = stream or self.stream
        with self._lock:
            self._visible = False
            if not self._pinned:
                self._clear_locked()
            target.write(text)
            target.flush()

    def close(self) -> None:
        if not self.enabled:
            return
        self._closed.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=1)
        with self._lock:
            self._visible = False
            if self._pinned:
                self.stream.write("\033[r\033[?25h")
                row = max(1, self._terminal_rows)
                self.stream.write(f"\033[{row};1H\n")
                self.stream.flush()
                self._pinned = False
                self._pinned_rows = 0
                self._terminal_rows = 0
                self._drawn_lines = 0
            else:
                self._clear_locked()

    def _animate(self) -> None:
        delay = 1 / max(self.fps, 1)
        while not self._closed.is_set():
            with self._lock:
                if self._visible:
                    self._draw_locked()
            self._wake.wait(delay)
            self._wake.clear()

    def _frame_width(self) -> int:
        columns = shutil.get_terminal_size(fallback=(80, 24)).columns
        if self._context and columns >= 68:
            return 25
        return max(21, min(35, columns - 2))

    def _compose(self, frame: tuple[str, ...]) -> tuple[str, ...]:
        if not self._context:
            return frame
        columns = shutil.get_terminal_size(fallback=(80, 24)).columns
        if columns < 68:
            return (*self._context, "", *frame)
        orb_width = len(frame[0])
        panel_width = min(56, max(38, columns - orb_width - 3))
        height = max(len(self._context), len(frame))
        frame_offset = max(0, (height - len(frame)) // 2)
        rows: list[str] = []
        for index in range(height):
            panel = self._context[index] if index < len(self._context) else ""
            frame_index = index - frame_offset
            orb = frame[frame_index] if 0 <= frame_index < len(frame) else " " * orb_width
            rows.append(f"{panel[:panel_width].ljust(panel_width)}   {orb}")
        return tuple(rows)

    def _current_frame(self, *, height: int = 11) -> tuple[str, ...]:
        phase = time.perf_counter() - self._started_at
        if self._pinned:
            size = shutil.get_terminal_size(fallback=(80, 24))
            self._reveal = step_reveal(
                self._reveal, self._presentation, now=time.perf_counter()
            )
            return render_live_screen(
                self._presentation,
                phase,
                columns=size.columns,
                rows=size.lines,
                muted=self._muted,
                color=color_enabled(self.stream),
                agent=self._agent,
                surface=self._surface,
                product=self._product,
                stack=self._stack,
                hearing=self._hearing and not self._muted,
                reveal=self._reveal,
                settings=self._settings_menu,
            )
        return self._compose(
            render_frame(
                self._state,
                phase,
                active=self._hearing,
                detail=self._detail,
                width=self._frame_width(),
                height=height,
            )
        )

    def _draw_locked(self) -> None:
        lines = self._current_frame(height=7 if self._pinned else 11)
        if self._pinned:
            self._draw_pinned_locked(lines)
            return
        if not self._drawn_lines:
            self.stream.write("\033[?25l")
        previous = self._drawn_lines
        if previous:
            self.stream.write(f"\033[{previous}A\r")
        for line in lines:
            self.stream.write(f"\033[2K{line}\n")
        leftover = previous - len(lines)
        if leftover > 0:
            for _ in range(leftover):
                self.stream.write("\033[2K\n")
            self.stream.write(f"\033[{leftover}A\r")
        self.stream.flush()
        self._drawn_lines = len(lines)

    def _draw_pinned_locked(self, lines: tuple[str, ...]) -> None:
        size = shutil.get_terminal_size(fallback=(80, 24))
        self._terminal_rows = size.lines
        self.stream.write("\033[H\033[?25l")
        for row, line in enumerate(lines[: size.lines], start=1):
            self.stream.write(f"\033[{row};1H\033[2K{line}")
        self.stream.flush()

    def _clear_locked(self) -> None:
        if not self._drawn_lines:
            return
        self.stream.write(f"\033[{self._drawn_lines}A\r")
        for _ in range(self._drawn_lines):
            self.stream.write("\033[2K\n")
        self.stream.write(f"\033[{self._drawn_lines}A\r")
        self.stream.flush()
        self._drawn_lines = 0
        self.stream.write("\033[?25h")
        self.stream.flush()
