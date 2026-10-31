from __future__ import annotations

import importlib.util

import pytest

from sleeptalk.agents.readiness import extra_install_command, install_missing, probe
from sleeptalk.config import AgentConfig, SleeptalkConfig, collect_missing_runtime_extras
from sleeptalk.wizard import WizardAnswers, agent_menu_entries, validate_answers


def _hide_module(monkeypatch, hidden: str) -> None:
    real = importlib.util.find_spec

    def fake(name: str, package: str | None = None):
        if name == hidden:
            return None
        return real(name, package)

    monkeypatch.setattr(importlib.util, "find_spec", fake)


def _which(monkeypatch, present: dict[str, str]) -> None:
    # Binary lookup (PATH plus vendor fallback dirs) lives in the registry.
    monkeypatch.setattr(
        "sleeptalk.registries.locate_binary",
        lambda binary, fallbacks=(): present.get(binary),
    )


def _cursor_key(monkeypatch, present: bool) -> None:
    monkeypatch.setattr("sleeptalk.defaults._cursor_api_key_present", lambda: present)


def test_claude_extra_install_command_targets_this_python() -> None:
    cmd = extra_install_command("sleeptalk[claude]")
    assert "claude-agent-sdk==0.2.165" in cmd
    assert cmd[0] != "sudo"


def test_install_now_runs_the_extra_command(monkeypatch) -> None:
    seen: list[list[str]] = []

    def fake_install(cmd: list[str]) -> tuple[bool, str]:
        seen.append(cmd)
        return True, ""

    _which(monkeypatch, {"claude": "/usr/bin/claude"})
    _hide_module(monkeypatch, "claude_agent_sdk")
    monkeypatch.setattr("sleeptalk.sysdeps.run_install", fake_install)
    report = probe("claude_code")
    lines = install_missing(report)
    assert seen and "claude-agent-sdk==0.2.165" in seen[0]
    assert lines == ["installed claude_agent_sdk"]


def test_claude_cli_without_sdk_is_not_ready(monkeypatch) -> None:
    _which(monkeypatch, {"claude": "/usr/bin/claude"})
    _hide_module(monkeypatch, "claude_agent_sdk")
    report = probe("claude_code")
    assert report.ready is False
    assert [need.key for need in report.missing] == ["claude_agent_sdk"]
    assert any("claude-agent-sdk" in line for line in report.missing_lines())
    assert not any("sleeptalk[claude]" in line for line in report.missing_lines())
    assert report.installable() and report.installable()[0].install_cmd
    assert "claude found" in report.menu_suffix()


def test_claude_sdk_without_cli_is_not_ready(monkeypatch) -> None:
    _which(monkeypatch, {})
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name, package=None: object() if name == "claude_agent_sdk" else None,
    )
    report = probe("claude_code")
    assert report.ready is False
    assert [need.key for need in report.missing] == ["claude"]
    assert "npm i --engine-strict -g @anthropic-ai/claude-code@2.1.296" in report.missing_lines()[0]


def test_claude_ready_when_cli_and_sdk_present(monkeypatch) -> None:
    _which(monkeypatch, {"claude": "/usr/bin/claude"})
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name, package=None: object() if name == "claude_agent_sdk" else None,
    )
    report = probe("claude_code")
    assert report.ready is True
    assert report.menu_suffix() == "ready (claude, claude_agent_sdk)"


def test_pi_ready_is_binary_only(monkeypatch) -> None:
    _which(monkeypatch, {"pi": "/opt/homebrew/bin/pi"})
    report = probe("pi")
    assert report.ready is True
    assert report.menu_suffix() == "ready (pi)"


def test_cursor_without_api_key_needs_the_cli_not_the_sdk(monkeypatch) -> None:
    # No CURSOR_API_KEY means the launcher falls back to `agent acp`, so the
    # SDK being importable proves nothing. Readiness must say the CLI is missing
    # and offer the vendor installer, instead of "ready" then a launch crash.
    _which(monkeypatch, {})
    _cursor_key(monkeypatch, False)
    report = probe("cursor")
    assert report.ready is False
    assert [need.key for need in report.missing] == ["agent"]
    assert "curl https://cursor.com/install" in report.missing_lines()[0]
    assert "CURSOR_API_KEY" in report.missing_lines()[0]
    assert "cursor.com/install" in report.installable()[0].command_text


def test_cursor_with_api_key_needs_sdk_extra(monkeypatch) -> None:
    _which(monkeypatch, {})
    _cursor_key(monkeypatch, True)
    _hide_module(monkeypatch, "cursor_sdk")
    report = probe("cursor")
    assert report.ready is False
    assert [need.key for need in report.missing] == ["cursor_sdk"]
    assert any("cursor-sdk" in line for line in report.missing_lines())
    assert not any("sleeptalk[cursor]" in line for line in report.missing_lines())
    assert "cursor-sdk==1.0.37" in report.installable()[0].command_text


def test_cursor_cli_alone_is_ready_without_api_key(monkeypatch) -> None:
    _which(monkeypatch, {"agent": "/Users/me/.local/bin/agent"})
    _cursor_key(monkeypatch, False)
    _hide_module(monkeypatch, "cursor_sdk")
    report = probe("cursor")
    assert report.ready is True
    assert report.menu_suffix() == "ready (agent)"


def test_cursor_sdk_pin_ignores_missing_cli(monkeypatch) -> None:
    _which(monkeypatch, {})
    _cursor_key(monkeypatch, False)
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name, package=None: object() if name == "cursor_sdk" else None,
    )
    report = probe("cursor", AgentConfig(type="cursor", runtime="sdk"))
    assert report.ready is True
    assert [need.key for need in report.needs] == ["cursor_sdk"]


def test_cursor_cli_found_in_local_bin_when_not_on_path(monkeypatch, tmp_path) -> None:
    from sleeptalk.registries import locate_binary

    fake = tmp_path / "agent"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.setattr("sleeptalk.registries.shutil.which", lambda name: None)
    assert locate_binary("agent", (str(tmp_path / "agent"),)) == str(fake)
    assert locate_binary("agent", (str(tmp_path / "missing"),)) is None


def test_cursor_acp_skips_sdk_and_needs_agent_cli(monkeypatch) -> None:
    from sleeptalk.defaults import register_defaults
    from sleeptalk.registries import AGENTS

    _which(monkeypatch, {"agent": "/usr/bin/agent"})
    _cursor_key(monkeypatch, True)
    _hide_module(monkeypatch, "cursor_sdk")
    section = AgentConfig(type="cursor", runtime="acp")
    register_defaults()
    assert AGENTS.get("cursor").python_module_for(section) == ""
    report = probe("cursor", section)
    assert report.ready is True


def test_cursor_readiness_names_the_runtime_construction_will_use(monkeypatch) -> None:
    _which(monkeypatch, {"agent": "/usr/bin/agent"})
    _hide_module(monkeypatch, "cursor_sdk")
    assert probe("cursor", AgentConfig(type="cursor"), env={}).name == "cursor_acp"
    assert (
        probe(
            "cursor",
            AgentConfig(type="cursor"),
            env={"CURSOR_API_KEY": "test-key"},
        ).name
        == "cursor"
    )


def test_local_hermes_gateway_requires_a_hermes_runtime(monkeypatch) -> None:
    _which(monkeypatch, {})
    report = probe("hermes_gateway", env={})
    assert report.ready is False
    assert [need.key for need in report.missing] == ["hermes"]
    assert "Hermes" in report.missing_lines()[0]


def test_local_hermes_gateway_accepts_installed_hermes(monkeypatch) -> None:
    _which(monkeypatch, {"hermes": "/usr/bin/hermes"})
    report = probe("hermes_gateway", env={})
    assert report.ready is True
    assert [need.key for need in report.needs] == ["hermes"]


def test_remote_hermes_gateway_has_no_local_needs(monkeypatch) -> None:
    _which(monkeypatch, {})
    section = AgentConfig(
        type="hermes_gateway",
        gateway_url="wss://hermes.example.invalid/api/ws",
    )
    report = probe("hermes_gateway", section, env={})
    assert report.ready is True
    assert report.needs == ()


def test_menu_uses_readiness_suffix(monkeypatch) -> None:
    _which(monkeypatch, {"pi": "/opt/pi"})
    _hide_module(monkeypatch, "claude_agent_sdk")
    labels = {name: label for label, name in agent_menu_entries()}
    assert "ready (pi)" in labels["pi"]
    assert "claude-agent-sdk" in labels["claude_code"] or "not installed" in labels["claude_code"]


def test_unknown_agent_does_not_crash_extras() -> None:
    config = SleeptalkConfig.model_validate(
        {"transport": {"type": "null"}, "agent": {"type": "pi"}}
    )
    config.agent.type = "nope"
    assert collect_missing_runtime_extras(config) == []


def test_doctor_extras_name_missing_claude_sdk(monkeypatch) -> None:
    _which(monkeypatch, {"claude": "/usr/bin/claude"})
    _hide_module(monkeypatch, "claude_agent_sdk")
    config = SleeptalkConfig.model_validate(
        {"transport": {"type": "null"}, "agent": {"type": "claude_code"}}
    )
    problems = collect_missing_runtime_extras(config)
    assert any("claude_agent_sdk" in item for item in problems)


@pytest.mark.asyncio
async def test_validate_answers_blocks_unready_agent_offline(monkeypatch) -> None:
    _which(monkeypatch, {})
    problems = await validate_answers(WizardAnswers(agent_type="codex"), live=False)
    assert problems
    assert any("codex" in item.lower() or "install" in item.lower() for item in problems)
