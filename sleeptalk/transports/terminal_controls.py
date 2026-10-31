"""Terminal-only state machine for the nested voice settings menu."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
from typing import Literal

from sleeptalk.policy import (
    NARRATION_STYLES,
    REPLY_LENGTHS,
    narration_style_or_default,
    reply_length_or_default,
)
from sleeptalk.presentation import PresentationState
from sleeptalk.speech_speed import SPEED_STEPS, format_speed, snap_speed


class TerminalKey(Enum):
    QUIT = auto()
    STOP = auto()
    MUTE = auto()
    SETTINGS = auto()
    UP = auto()
    DOWN = auto()
    ENTER = auto()
    ESCAPE = auto()


class GlobalControl(Enum):
    QUIT = auto()
    STOP = auto()
    TOGGLE_MUTE = auto()


@dataclass(frozen=True, slots=True)
class SetNarrationStyle:
    value: Literal["narrated", "silence", "keyboard"]


@dataclass(frozen=True, slots=True)
class SetReplyLength:
    value: Literal["full", "summarized", "concise"]


@dataclass(frozen=True, slots=True)
class SetSpeechSpeed:
    value: float


ControlEffect = GlobalControl | SetNarrationStyle | SetReplyLength | SetSpeechSpeed


@dataclass(frozen=True, slots=True)
class MenuRowView:
    label: str
    value: str = ""
    focused: bool = False
    current: bool = False


@dataclass(frozen=True, slots=True)
class SettingsMenuView:
    title: str
    rows: tuple[MenuRowView, ...]
    escape_label: Literal["close", "back"]
    enter_label: Literal["choose", "apply"]
    detail: str = ""


class _Page(Enum):
    ROOT = auto()
    NARRATION = auto()
    REPLY_LENGTH = auto()
    SPEED = auto()


_ROOT_PAGES = (_Page.NARRATION, _Page.REPLY_LENGTH, _Page.SPEED)
_NARRATION_LABELS = {
    "narrated": "Narrated",
    "silence": "Silent",
    "keyboard": "Keyboard sounds",
}
_NARRATION_DETAILS = {
    "narrated": "Speak a short update for each tool.",
    "silence": "Do not narrate tool activity.",
    "keyboard": "Play typing sounds during tool activity.",
}
_REPLY_LABELS = {"full": "Full", "summarized": "Brief", "concise": "Concise speech"}
_REPLY_DETAILS = {
    "full": "Speak the complete response.",
    "summarized": "Shorten long spoken responses.",
    "concise": "Speak briefly and keep the complete result in text.",
}


class TerminalControls:
    """Own menu navigation and return typed effects for the transport."""

    def __init__(self) -> None:
        self._page: _Page | None = None
        self._cursor = 0

    def handle(
        self, key: TerminalKey, current: PresentationState
    ) -> ControlEffect | None:
        global_effect = {
            TerminalKey.QUIT: GlobalControl.QUIT,
            TerminalKey.STOP: GlobalControl.STOP,
            TerminalKey.MUTE: GlobalControl.TOGGLE_MUTE,
        }.get(key)
        if global_effect is not None:
            return global_effect

        if key is TerminalKey.SETTINGS:
            self._page = _Page.ROOT if self._page is None else None
            self._cursor = 0
            return None
        if self._page is None:
            return None

        if key is TerminalKey.ESCAPE:
            if self._page is _Page.ROOT:
                self._page = None
                self._cursor = 0
            else:
                self._cursor = _ROOT_PAGES.index(self._page)
                self._page = _Page.ROOT
            return None

        row_count = self._row_count()
        if key is TerminalKey.UP:
            self._cursor = (self._cursor - 1) % row_count
            return None
        if key is TerminalKey.DOWN:
            self._cursor = (self._cursor + 1) % row_count
            return None
        if key is not TerminalKey.ENTER:
            return None

        if self._page is _Page.ROOT:
            self._page = _ROOT_PAGES[self._cursor]
            self._cursor = self._current_choice_index(current)
            return None

        page = self._page
        choice = self._choice_effect(page, self._cursor)
        self._page = _Page.ROOT
        self._cursor = _ROOT_PAGES.index(page)
        return choice

    def view(self, current: PresentationState) -> SettingsMenuView | None:
        if self._page is None:
            return None
        if self._page is _Page.ROOT:
            return SettingsMenuView(
                title="Voice settings",
                rows=(
                    MenuRowView(
                        "Tool narration",
                        _NARRATION_LABELS[
                            narration_style_or_default(current.narration_style)
                        ],
                        focused=self._cursor == 0,
                    ),
                    MenuRowView(
                        "Response length",
                        _REPLY_LABELS[reply_length_or_default(current.reply_length)],
                        focused=self._cursor == 1,
                    ),
                    MenuRowView(
                        "Speech speed",
                        format_speed(current.tts_speed),
                        focused=self._cursor == 2,
                    ),
                ),
                escape_label="close",
                enter_label="choose",
            )

        values = self._values()
        labels = tuple(self._value_label(value) for value in values)
        selected = self._current_choice_index(current)
        return SettingsMenuView(
            title=f"Voice settings / {self._page_label()}",
            rows=tuple(
                MenuRowView(
                    label,
                    focused=index == self._cursor,
                    current=index == selected,
                )
                for index, label in enumerate(labels)
            ),
            escape_label="back",
            enter_label="apply",
            detail=self._choice_detail(values[self._cursor]),
        )

    def _row_count(self) -> int:
        return len(_ROOT_PAGES) if self._page is _Page.ROOT else len(self._values())

    def _values(self) -> tuple[str | float, ...]:
        if self._page is _Page.NARRATION:
            return NARRATION_STYLES
        if self._page is _Page.REPLY_LENGTH:
            return REPLY_LENGTHS
        if self._page is _Page.SPEED:
            return SPEED_STEPS
        raise AssertionError(f"settings page has no values: {self._page}")

    def _current_choice_index(self, current: PresentationState) -> int:
        if self._page is _Page.NARRATION:
            return NARRATION_STYLES.index(
                narration_style_or_default(current.narration_style)
            )
        if self._page is _Page.REPLY_LENGTH:
            return REPLY_LENGTHS.index(reply_length_or_default(current.reply_length))
        if self._page is _Page.SPEED:
            return SPEED_STEPS.index(snap_speed(current.tts_speed))
        return 0

    def _page_label(self) -> str:
        if self._page is _Page.NARRATION:
            return "Tool narration"
        if self._page is _Page.REPLY_LENGTH:
            return "Response length"
        if self._page is _Page.SPEED:
            return "Speech speed"
        raise AssertionError(f"settings page has no label: {self._page}")

    def _value_label(self, value: str | float) -> str:
        if self._page is _Page.NARRATION:
            return _NARRATION_LABELS[str(value)]
        if self._page is _Page.REPLY_LENGTH:
            return _REPLY_LABELS[str(value)]
        if self._page is _Page.SPEED:
            return format_speed(float(value))
        raise AssertionError(f"settings page has no value labels: {self._page}")

    def _choice_detail(self, value: str | float) -> str:
        if self._page is _Page.NARRATION:
            return _NARRATION_DETAILS[str(value)]
        if self._page is _Page.REPLY_LENGTH:
            return _REPLY_DETAILS[str(value)]
        if self._page is _Page.SPEED:
            return f"Speak at {format_speed(float(value))}."
        return ""

    @staticmethod
    def _choice_effect(page: _Page, cursor: int) -> ControlEffect:
        if page is _Page.NARRATION:
            value = NARRATION_STYLES[cursor]
            if value in {"narrated", "silence", "keyboard"}:
                return SetNarrationStyle(value)
            raise AssertionError(f"unknown narration style: {value}")
        if page is _Page.REPLY_LENGTH:
            value = REPLY_LENGTHS[cursor]
            if value in {"full", "summarized", "concise"}:
                return SetReplyLength(value)
            raise AssertionError(f"unknown reply length: {value}")
        if page is _Page.SPEED:
            return SetSpeechSpeed(SPEED_STEPS[cursor])
        raise AssertionError(f"cannot apply settings page: {page}")
