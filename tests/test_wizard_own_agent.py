"""sleeptalk init: the "My own agent" choice writes working bring-your-own profiles."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from sleeptalk import wizard
from sleeptalk.config import load_resolved_config
from sleeptalk.defaults import register_defaults

register_defaults()


def _answers(**overrides) -> wizard.WizardAnswers:
    answers = wizard.WizardAnswers(surface="web", name="Own Bot", slug="own-bot", stt_key="k", tts_key="k")
    for key, value in overrides.items():
        setattr(answers, key, value)
    return answers


def _write_profile(tmp_path: Path, answers: wizard.WizardAnswers) -> Path:
    (tmp_path / "sleeptalk.toml").write_text('[transport]\ntype = "web"\n[agent]\ntype = "pi"\n')
    profiles = tmp_path / "config" / "profiles"
    profiles.mkdir(parents=True, exist_ok=True)
    path = profiles / f"{answers.slug}.toml"
    path.write_text(wizard.profile_toml(answers))
    return path


def test_own_agent_settings_cover_every_kind() -> None:
    assert wizard.own_agent_settings("command", command="node 'my agent.mjs'", env_names="OPENAI_API_KEY, OTHER") == (
        "command",
        {"command": ["node", "my agent.mjs"], "env": ["OPENAI_API_KEY", "OTHER"]},
    )
    assert wizard.own_agent_settings("acp", command="gemini --experimental-acp") == (
        "command",
        {"command": ["gemini", "--experimental-acp"], "protocol": "acp", "inherit_env": True},
    )
    assert wizard.own_agent_settings("http", url="https://a.example/turn") == ("http", {"url": "https://a.example/turn"})
    assert wizard.own_agent_settings("bridge", url="wss://a.example/v1/agent-bridge", token_env="T") == (
        "bridge",
        {"url": "wss://a.example/v1/agent-bridge", "token_env": "T"},
    )


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"kind": "command"}, "enter the command"),
        ({"kind": "command", "command": "node 'unclosed"}, "could not read"),
        ({"kind": "http", "url": "http://a.example/turn"}, "loopback"),
        ({"kind": "bridge", "url": "wss://a.example/v1/agent-bridge"}, "token_env"),
    ],
)
def test_own_agent_settings_explain_mistakes(kwargs: dict, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        wizard.own_agent_settings(**kwargs)


def test_own_agent_is_offered_separately_from_the_harness_list() -> None:
    offered = {value for _label, value in wizard.agent_menu_entries(detected={})}
    assert wizard.OWN_AGENT_CHOICE not in offered
    assert not offered & set(wizard.BYO_AGENT_TYPES)


@pytest.mark.parametrize(
    ("agent_type", "options", "permission_mode"),
    [
        ("command", {"command": ["node", "agent.mjs"], "env": ["OPENAI_API_KEY"]}, ""),
        ("command", {"command": ["gemini", "--experimental-acp"], "protocol": "acp"}, "pending"),
        ("http", {"url": "https://a.example/turn", "token_env": "MY_AGENT_TOKEN"}, ""),
        ("bridge", {"url": "wss://a.example/v1/agent-bridge", "token_env": "MY_BRIDGE_TOKEN"}, ""),
    ],
)
def test_profile_round_trips_through_the_loader_and_edit_mode(tmp_path: Path, agent_type, options, permission_mode) -> None:
    answers = _answers(agent_type=agent_type, agent_options=options, agent_permission_mode=permission_mode)
    path = _write_profile(tmp_path, answers)
    config = load_resolved_config(path, check_env=False, check_binaries=False)
    assert config.agent.type == agent_type
    assert config.agent.options == {**config.agent.options, **options}
    if permission_mode:
        assert config.agent.permission_mode == permission_mode

    reloaded = wizard.load_existing_answers(answers.slug, path.parent)
    assert reloaded is not None
    assert reloaded.agent_options == options
    assert reloaded.agent_permission_mode == permission_mode
    assert wizard.profile_toml(reloaded) == wizard.profile_toml(answers)


def test_editing_another_section_keeps_plugin_options(tmp_path: Path) -> None:
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    (profiles / "plug.toml").write_text(
        '[agent]\ntype = "orchard"\nslug = "plug"\n\n[agent.options]\nendpoint = "https://o.example"\n\n'
        '[transport]\ntype = "web"\n'
    )
    answers = wizard.load_existing_answers("plug", profiles)
    assert answers is not None
    assert "[agent.options]" in wizard.profile_toml(answers)
    assert 'endpoint = "https://o.example"' in wizard.profile_toml(answers)


def test_agent_token_goes_to_env_under_token_env_only() -> None:
    answers = _answers(agent_type="http", agent_options={"url": "https://a.example/turn", "token_env": "MY_AGENT_TOKEN"}, agent_token="s3cret")
    entries = wizard.env_entries(answers)
    assert entries["MY_AGENT_TOKEN"] == "s3cret"
    assert "s3cret" not in wizard.profile_toml(answers)
    hydrated = _answers(agent_type="http", agent_options={"url": "https://a.example/turn", "token_env": "MY_AGENT_TOKEN"})
    wizard.hydrate_answers_from_env(hydrated, {"MY_AGENT_TOKEN": "from-env"})
    assert hydrated.agent_token == "from-env"
    assert "MY_AGENT_TOKEN" not in wizard.env_entries(_answers(agent_type="pi", agent_token="stray"))


def test_answers_file_accepts_agent_options_table() -> None:
    answers = wizard.answers_from_mapping(
        {"name": "Own", "agent_type": "command", "agent_options": {"command": ["node", "agent.mjs"]}}
    )
    assert answers.agent_options == {"command": ["node", "agent.mjs"]}
    with pytest.raises(ValueError, match="agent_options must be a table"):
        wizard.answers_from_mapping({"name": "Own", "agent_options": "node agent.mjs"})


async def test_validate_answers_names_bad_options_and_missing_tokens(monkeypatch) -> None:
    monkeypatch.delenv("MY_BRIDGE_TOKEN", raising=False)
    bad = _answers(agent_type="command", agent_options={"protocol": "acp"})
    problems = await wizard.validate_answers(bad, live=False)
    assert any(problem.startswith("agent_options: command") for problem in problems)

    tokenless = _answers(agent_type="bridge", agent_options={"url": "wss://a.example/v1/agent-bridge", "token_env": "MY_BRIDGE_TOKEN"})
    assert any("MY_BRIDGE_TOKEN is not set" in problem for problem in await wizard.validate_answers(tokenless, live=False))
    tokenless.agent_token = "t"
    assert await wizard.validate_answers(tokenless, live=False) == []

    good = _answers(agent_type="command", agent_options={"command": [sys.executable, "agent.py"]})
    assert await wizard.validate_answers(good, live=False) == []


# ---- interactive step ------------------------------------------------------

TEMPLATES = Path(__file__).resolve().parents[1] / "sleeptalk" / "templates" / "byo"


def _script(monkeypatch, *, selects: list, texts: list) -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import questionary

    select = SimpleNamespace(ask_async=AsyncMock(side_effect=selects))
    monkeypatch.setattr(questionary, "select", lambda *_args, **_kwargs: select)
    remaining = list(texts)

    async def ask_text(prompt: str, *, default: str = "", password: bool = False) -> str:
        return remaining.pop(0)

    monkeypatch.setattr(wizard, "_ask_text", ask_text)


class _Console:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def print(self, text: str = "") -> None:
        self.lines.append(str(text))


async def test_interactive_command_agent_is_checked_then_saved(monkeypatch, tmp_path: Path) -> None:
    command = f"{sys.executable} {TEMPLATES / 'command-python' / 'agent.py'}"
    _script(monkeypatch, selects=["command"], texts=[command, "OPENAI_API_KEY"])
    answers = _answers(workspace_dir=str(tmp_path))
    console = _Console()
    assert await wizard.ask_own_agent(answers, console) is None
    assert answers.agent_type == "command"
    assert answers.agent_options == {
        "command": [sys.executable, str(TEMPLATES / "command-python" / "agent.py")],
        "env": ["OPENAI_API_KEY"],
    }
    assert any("contract checks" in line for line in console.lines)


async def test_interactive_failed_check_can_be_changed(monkeypatch, tmp_path: Path) -> None:
    good = f"{sys.executable} {TEMPLATES / 'command-python' / 'agent.py'}"
    _script(monkeypatch, selects=["command", "change"], texts=["no-such-agent-xyz", "", good, ""])
    answers = _answers(workspace_dir=str(tmp_path))
    console = _Console()
    assert await wizard.ask_own_agent(answers, console) is None
    assert answers.agent_options["command"][0] == sys.executable
    assert any("✗" in line for line in console.lines)


async def test_interactive_back_leaves_answers_untouched(monkeypatch) -> None:
    _script(monkeypatch, selects=[wizard._BACK], texts=[])
    answers = _answers(agent_type="codex")
    assert await wizard.ask_own_agent(answers, _Console()) == wizard._BACK
    assert answers.agent_type == "codex" and answers.agent_options == {}


async def test_interactive_starter_scaffolds_into_the_home(monkeypatch, tmp_path: Path) -> None:
    from sleeptalk.home import Layout

    monkeypatch.setattr(wizard, "_layout", lambda: Layout(root=tmp_path, tier=2, reason="test"))
    _script(monkeypatch, selects=["starter", "python", "keep"], texts=[""])
    answers = _answers(workspace_dir=str(tmp_path))
    assert await wizard.ask_own_agent(answers, _Console()) is None
    agent_file = tmp_path / "agents" / "own-bot" / "agent.py"
    assert agent_file.is_file()
    assert answers.agent_type == "command"
    assert answers.agent_options["command"] == ["python3", str(agent_file)]


async def test_interactive_http_stores_token_for_env(monkeypatch, tmp_path: Path, unused_tcp_port: int) -> None:
    monkeypatch.delenv("MY_AGENT_TOKEN", raising=False)
    url = f"http://127.0.0.1:{unused_tcp_port}/turn"  # nothing listens; no network use
    _script(monkeypatch, selects=["http", "keep"], texts=[url, "MY_AGENT_TOKEN", "s3cret"])
    answers = _answers(workspace_dir=str(tmp_path))
    assert await wizard.ask_own_agent(answers, _Console()) is None
    assert answers.agent_options == {"url": url, "token_env": "MY_AGENT_TOKEN"}
    assert wizard.env_entries(answers)["MY_AGENT_TOKEN"] == "s3cret"


async def test_interactive_known_acp_agent_entered_as_a_program_is_switched(monkeypatch, tmp_path: Path) -> None:
    import shutil
    import stat

    fake = tmp_path / "bin" / "opencode"  # stands in for the real CLI
    fake.parent.mkdir()
    shutil.copy(TEMPLATES / "acp-python" / "agent.py", fake)
    fake.write_text(fake.read_text().replace("#!/usr/bin/env python3", f"#!{sys.executable}", 1))
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)

    _script(monkeypatch, selects=["command", "fix", "pending"], texts=[str(fake), ""])
    answers = _answers(workspace_dir=str(tmp_path))
    console = _Console()
    assert await wizard.ask_own_agent(answers, console) is None
    assert any("is an ACP agent" in line for line in console.lines)
    assert answers.agent_options == {"command": [str(fake), "acp"], "protocol": "acp", "inherit_env": True}
    assert answers.agent_permission_mode == "pending"
    assert any("contract checks" in line for line in console.lines)
