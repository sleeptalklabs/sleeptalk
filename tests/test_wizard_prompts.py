"""Text prompts go back on Esc; the hint is printed once, not per prompt."""

from __future__ import annotations

import questionary
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
import pytest

from sleeptalk import wizard


async def _ask(keys: str, *, password: bool = False, monkeypatch=None) -> tuple[str, list[str]]:
    messages: list[str] = []
    original = questionary.password if password else questionary.text

    def record(message, *args, **kwargs):
        messages.append(message)
        return original(message, *args, **kwargs)

    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        monkeypatch.setattr(questionary, "password" if password else "text", record)
        pipe.send_text(keys)
        return await wizard._ask_text("Bot token:", password=password), messages


@pytest.mark.parametrize("password", [False, True])
async def test_escape_goes_back(monkeypatch, password):
    value, messages = await _ask("\x1b", password=password, monkeypatch=monkeypatch)
    assert value is wizard._BACK
    assert messages == ["Bot token:"], "the per-prompt hint is gone"


async def test_typed_back_still_goes_back(monkeypatch):
    value, _ = await _ask("back\r", monkeypatch=monkeypatch)
    assert value is wizard._BACK


async def test_answer_is_returned_stripped(monkeypatch):
    value, _ = await _ask("  hello  \r", monkeypatch=monkeypatch)
    assert value == "hello"


def test_hint_names_the_escape_key():
    assert "Esc" in wizard.BACK_HINT
