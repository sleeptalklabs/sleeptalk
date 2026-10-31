"""Deterministic coverage for the production terminal voice being."""

from __future__ import annotations

import io
import os
import time

import pytest

from sleeptalk.transports.terminal_visual import TerminalBeing, render_frame


class TTY(io.StringIO):
    def isatty(self) -> bool:
        return True


def _ink(frame: tuple[str, ...]) -> int:
    return sum(character != " " for line in frame[:-2] for character in line)


def test_states_have_distinct_geometry_and_labels() -> None:
    listening = render_frame("listening", 0.0)
    idle = render_frame("idle", 0.0)
    speaking = render_frame("speaking", 0.0)

    assert _ink(listening) < _ink(idle) < _ink(speaking)
    assert "LISTENING" in listening[-2]
    assert "SPEAKING" in speaking[-2]
    assert render_frame("thinking", 0.0) != render_frame("thinking", 0.7)
    assert render_frame("speaking", 0.0) != render_frame("speaking", 0.4)


def test_active_listening_has_non_colour_language() -> None:
    frame = render_frame(
        "listening",
        0.3,
        active=True,
        detail="pause to send",
    )
    assert "HEARING YOU" in frame[-2]
    assert "pause to send" in frame[-1]
    assert len({len(line) for line in frame}) == 1
    assert all(ord(character) < 128 for line in frame for character in line)


def test_unknown_state_is_rejected() -> None:
    with pytest.raises(ValueError):
        render_frame("confused", 0.0)


def test_non_tty_renderer_emits_no_ansi_or_output() -> None:
    stream = io.StringIO()
    being = TerminalBeing(stream=stream)
    being.set_context(("SLEEPTALK",))
    being.pin_top()
    being.set("thinking")
    being.close()
    assert stream.getvalue() == ""


def test_pinned_stage_restores_scroll_region_cursor_and_thread(monkeypatch) -> None:
    monkeypatch.setattr(
        "sleeptalk.transports.terminal_visual.shutil.get_terminal_size",
        lambda fallback: os.terminal_size((100, 30)),
    )
    stream = TTY()
    being = TerminalBeing(stream=stream, fps=100)
    being.set_context(("PI-BOT · SHARED VOICE KERNEL", "Speak naturally"))
    being.set("thinking", detail="working")
    being.pin_top()

    output = stream.getvalue()
    assert "sleeptalk" in output
    assert "thinking" in output
    assert "mute" in output
    assert "\033[2J\033[H\033[?25l" in output
    thread = being._thread
    being.close()

    assert "\033[r\033[?25h" in stream.getvalue()
    assert thread is not None and not thread.is_alive()


def test_clear_stops_unpinned_redraw_and_restores_cursor() -> None:
    stream = TTY()
    being = TerminalBeing(stream=stream, fps=100)
    being.set("speaking")
    assert "\033[?25l" in stream.getvalue()
    being.clear()
    size_after_clear = len(stream.getvalue())
    time.sleep(0.03)
    assert len(stream.getvalue()) == size_after_clear
    assert stream.getvalue().endswith("\033[?25h")
    being.close()
