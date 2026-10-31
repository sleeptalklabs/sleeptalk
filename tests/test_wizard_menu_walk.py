"""Open every speech setting in the terminal for every speech service.

A menu whose preselected default isn't one of its choices crashes questionary
("Invalid `default` value passed"). The terminal's menus fell behind the
settings page that way: a profile set up with OpenRouter crashed Voice. This
walks each section once per service with a chooser that checks every menu.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import questionary

from sleeptalk import setup_core
from sleeptalk import wizard

SERVICES = [*wizard.SPEECH_SERVICES, "realtime"]
SECTIONS = [("voice", None), ("speech", None), *(("advanced", key) for key in ("language", "models", "speed", "turns", "delivery"))]


class Chooser:
    def __init__(self, section: tuple[str, str | None]) -> None:
        self.section = section
        self.stage = "main"
        self.problems: list[str] = []
        self.prompts = 0

    def select(self, message, choices=(), default=None, **_kwargs):
        self.prompts += 1
        assert self.prompts < 40, f"runaway prompts at {message!r}"
        values = [c.value if isinstance(c, questionary.Choice) else c for c in choices
                  if not isinstance(c, questionary.Separator)]
        if isinstance(default, questionary.Choice):
            default = default.value
        if default is not None and default not in values:
            self.problems.append(f"{message!r}: default {default!r} not in {values}")
        return SimpleNamespace(ask_async=AsyncMock(return_value=self.pick(message, values, default)))

    def pick(self, message, values, default):
        if message == "What do you want to change?":
            if self.stage == "main":
                self.stage = "sub" if self.section[1] is None else "advanced"
                return self.section[0]
            return wizard.EDIT_DONE
        if message == "Advanced settings" and self.stage == "advanced":
            self.stage = "sub"
            return self.section[1]
        if message.startswith("Start a call now?"):
            return wizard.LAUNCH_LATER
        real = [value for value in values if value != wizard._BACK]
        if default in real:
            return default
        return real[0] if real else wizard._BACK

    async def text(self, prompt, *, default="", password=False):
        self.prompts += 1
        return "sk-test-123456" if password else (default or "x")


@pytest.fixture
def home(tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    monkeypatch.setenv("SLEEPTALK_HOME", str(tmp_path / "home"))
    monkeypatch.setattr("sleeptalk.registries.locate_binary", lambda binary, fallbacks=(): f"/usr/bin/{binary}")
    monkeypatch.setattr("sleeptalk.sysdeps.ensure_required_for_surface", lambda _surface, *, repair: [])
    monkeypatch.setattr("sleeptalk.setup.ensure_dependencies", Mock(return_value=()))
    monkeypatch.setattr("sleeptalk.stt_roundtrip.stt_roundtrip", AsyncMock(return_value=("hello", 10)))
    monkeypatch.setattr(setup_core, "hybrid_connectivity_check", AsyncMock(return_value=None))
    monkeypatch.setattr(wizard, "_play_sample", AsyncMock(return_value=True))
    monkeypatch.setattr("sleeptalk.voice_list.list_voices", AsyncMock(return_value=[]))

    async def prove(answers):
        sample = tmp_path / "sample.wav"
        sample.write_bytes(b"sample")
        return sample

    monkeypatch.setattr(setup_core, "_prove_selected_voice", prove)
    return workspace


async def _profile(service: str, workspace: Path) -> str:
    mapping = {"name": f"Walk {service}", "surface": "web", "workspace_dir": str(workspace)}
    if service == "realtime":
        mapping.update(route_type="hybrid", realtime_key="sk-openai-1")
    elif service == "local":
        mapping.update(speech_provider="local")
    else:
        mapping.update(speech_provider=service, speech_key="k-123456")
    answers = wizard.answers_from_mapping(mapping)
    lines: list[str] = []
    assert await wizard.run_non_interactive(answers, live=False, out=lines.append) == 0, lines
    return answers.slug


@pytest.mark.parametrize("service", SERVICES)
async def test_every_speech_setting_opens_for_every_service(home, monkeypatch, service):
    slug = await _profile(service, home)
    if service == "local":
        monkeypatch.setattr("sleeptalk.sysdeps.missing_local_extra", lambda: None)
        monkeypatch.setattr("sleeptalk.speech.local_models.is_ready", lambda model, root=None: True)
    problems = []
    for section in SECTIONS:
        chooser = Chooser(section)
        monkeypatch.setattr(questionary, "select", chooser.select)
        monkeypatch.setattr(questionary, "confirm", lambda *a, **k: SimpleNamespace(ask_async=AsyncMock(return_value=True)))
        monkeypatch.setattr(wizard, "_ask_text", chooser.text)
        result = await wizard._configure(live=False, profile=slug, settings=True, workspace=str(home))
        assert result == 0, (section, result)
        problems += [f"{section}: {problem}" for problem in chooser.problems]
    assert problems == []
