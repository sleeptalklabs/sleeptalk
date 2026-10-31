"""sleeptalk init offers one entry per agent product and picks the connection."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sleeptalk import setup_core
from sleeptalk import hermes_connect, wizard
from sleeptalk.defaults import register_defaults

register_defaults()


class _Console:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def print(self, text: str = "") -> None:
        self.lines.append(str(text))


def _script(monkeypatch, *, selects=(), texts=()) -> None:
    import questionary

    select = SimpleNamespace(ask_async=AsyncMock(side_effect=list(selects)))
    monkeypatch.setattr(questionary, "select", lambda *_args, **_kwargs: select)
    remaining = list(texts)

    async def ask_text(prompt: str, *, default: str = "", password: bool = False) -> str:
        return remaining.pop(0)

    monkeypatch.setattr(wizard, "_ask_text", ask_text)


def _attach(monkeypatch, results):
    calls: list[dict] = []
    queue = list(results)

    async def attach(answers, *, raw_url="", token="", env=None, handshake=None):
        calls.append({"raw_url": raw_url, "token": token})
        probe = queue.pop(0)
        if isinstance(probe, hermes_connect.Ready):
            hermes_connect.apply_dashboard(answers, probe, token)
        return probe

    monkeypatch.setattr(hermes_connect, "attach_hermes", attach)
    monkeypatch.setattr(hermes_connect, "running_dashboard_origins", lambda: [hermes_connect.DEFAULT_ORIGIN])
    return calls


def test_menu_lists_each_product_once_without_connection_variants() -> None:
    values = [value for _label, value in wizard.agent_menu_entries(detected={})]
    assert values[: len(wizard.PRODUCT_AGENTS)] == [name for name, _label in wizard.PRODUCT_AGENTS]
    assert not set(values) & (wizard.CONNECTION_REGISTRATIONS | set(wizard.BYO_AGENT_TYPES))
    labels = [label for label, _value in wizard.agent_menu_entries(detected={"acp": "/usr/local/bin/hermes"})]
    assert any(label.startswith("Hermes — detected on PATH") for label in labels)


def test_oh_my_pi_is_the_default_and_can_be_installed_for_you(monkeypatch, tmp_path) -> None:
    from sleeptalk import defaults
    from sleeptalk.registries import AGENTS

    register_defaults()
    # First in the list, so it's picked when nothing else is ready (pi has no web search).
    assert wizard.PRODUCT_AGENTS[0] == ("omp", "oh-my-pi (default)")
    values = [value for _label, value in wizard.agent_menu_entries(detected={})]
    assert values[0] == "omp"
    omp = AGENTS.get("omp")
    assert omp.binary_install == ("sh", "-c", defaults.OMP_INSTALL_COMMAND)
    # Its installer's folder counts even when it isn't on PATH yet.
    installed = tmp_path / ".local" / "bin" / "omp"
    installed.parent.mkdir(parents=True)
    installed.write_text("#!/bin/sh\n")
    installed.chmod(0o755)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert omp.find_binary(SimpleNamespace(pi_bin="")) == str(installed)
    assert defaults._locate_omp() == str(installed)


def test_installed_extension_agents_still_appear(monkeypatch) -> None:
    from sleeptalk.registries import AGENTS

    monkeypatch.setattr(AGENTS, "names", lambda: sorted({*(name for name, _ in wizard.PRODUCT_AGENTS), "acp", "cursor_acp", "orchard"}))
    values = [value for _label, value in wizard.agent_menu_entries(detected={})]
    assert values[-1] == "orchard"
    assert "cursor_acp" not in values


async def test_hermes_joins_a_running_dashboard(monkeypatch) -> None:
    ready = hermes_connect.Ready(origin="http://127.0.0.1:9119", ws_url="ws://127.0.0.1:9119/api/ws")
    calls = _attach(monkeypatch, [ready])
    _script(monkeypatch)
    answers = wizard.WizardAnswers()
    console = _Console()
    assert await wizard.choose_hermes_connection(answers, console) is None
    assert answers.agent_type == "hermes_gateway"
    assert answers.gateway_url == ready.ws_url
    assert calls[0]["raw_url"] == hermes_connect.DEFAULT_ORIGIN
    assert any("joining your running Hermes" in line for line in console.lines)


async def test_hermes_needing_a_token_can_join_or_start_per_call(monkeypatch) -> None:
    auth = hermes_connect.AuthRequired(origin="http://127.0.0.1:9119", ws_url="ws://127.0.0.1:9119/api/ws")
    ready = hermes_connect.Ready(origin=auth.origin, ws_url=auth.ws_url)

    calls = _attach(monkeypatch, [auth, ready])
    _script(monkeypatch, texts=["dashboard-token"])
    answers = wizard.WizardAnswers()
    assert await wizard.choose_hermes_connection(answers, _Console()) is None
    assert answers.gateway_url == ready.ws_url and answers.gateway_session_token == "dashboard-token"
    assert calls[1] == {"raw_url": auth.origin, "token": "dashboard-token"}

    _attach(monkeypatch, [auth])
    _script(monkeypatch, texts=[""])
    answers = wizard.WizardAnswers()
    console = _Console()
    assert await wizard.choose_hermes_connection(answers, console) is None
    assert answers.agent_type == "hermes_gateway" and answers.gateway_url == ""
    assert any("starts Hermes' gateway for each call" in line for line in console.lines)


async def test_hermes_without_a_dashboard_starts_the_local_gateway(monkeypatch) -> None:
    _attach(monkeypatch, [hermes_connect.Unreachable(), hermes_connect.Unreachable()])  # saved URL, then default
    _script(monkeypatch, selects=["local"])
    answers = wizard.WizardAnswers(gateway_url="ws://stale", gateway_session_token="stale")
    assert await wizard.choose_hermes_connection(answers, _Console()) is None
    assert (answers.agent_type, answers.gateway_url, answers.gateway_session_token) == ("hermes_gateway", "", "")


async def test_hermes_can_join_a_dashboard_at_another_address(monkeypatch) -> None:
    ready = hermes_connect.Ready(origin="http://127.0.0.1:8899", ws_url="ws://127.0.0.1:8899/api/ws")
    calls = _attach(monkeypatch, [hermes_connect.Unreachable(), ready])
    _script(monkeypatch, selects=["remote"], texts=["http://127.0.0.1:8899"])
    answers = wizard.WizardAnswers()
    assert await wizard.choose_hermes_connection(answers, _Console()) is None
    assert answers.gateway_url == ready.ws_url
    assert calls[1]["raw_url"] == "http://127.0.0.1:8899"


async def test_hermes_back_leaves_answers_unchanged(monkeypatch) -> None:
    _attach(monkeypatch, [hermes_connect.Unreachable()])
    _script(monkeypatch, selects=[wizard._BACK])
    answers = wizard.WizardAnswers(agent_type="codex")
    assert await wizard.choose_hermes_connection(answers, _Console()) == wizard._BACK
    assert answers.agent_type == "codex"


def test_local_hermes_gateway_profile_prefers_the_gateway_and_can_fall_back(tmp_path) -> None:
    from sleeptalk.agents.selection import RuntimeKind, request_from_config, select_runtime
    from sleeptalk.config import AgentConfig

    selection = select_runtime(request_from_config(AgentConfig(type="hermes_gateway", workspace_dir=str(tmp_path)), {}))
    assert selection.kind is RuntimeKind.GATEWAY
    pinned = select_runtime(request_from_config(AgentConfig(type="hermes", workspace_dir=str(tmp_path)), {}))
    assert pinned.kind is RuntimeKind.ACP


def test_openclaw_token_hint_avoids_the_redacted_command() -> None:
    from sleeptalk.config import AgentConfig
    from sleeptalk.registries import AGENTS

    hint = AGENTS.get("openclaw").required_env_for(AgentConfig(type="openclaw", gateway_url="ws://127.0.0.1:18789"))["OPENCLAW_GATEWAY_TOKEN"]
    assert "~/.openclaw/openclaw.json" in hint
    assert "redacted" in hint


@pytest.mark.parametrize("name", [name for name, _label in wizard.PRODUCT_AGENTS])
def test_every_product_entry_resolves_to_a_registration(name: str) -> None:
    from sleeptalk.registries import AGENTS

    assert AGENTS.get_optional(name) is not None


# ---- OpenClaw -----------------------------------------------------------------


def test_openclaw_token_is_read_from_its_config_file(tmp_path) -> None:
    config = tmp_path / "openclaw.json"
    config.write_text('{"gateway": {"auth": {"token": "  gateway-token  "}}}')
    assert wizard.read_openclaw_gateway_token(config) == "gateway-token"
    assert wizard.read_openclaw_gateway_token(tmp_path / "missing.json") == ""
    (tmp_path / "bad.json").write_text("{not json")
    assert wizard.read_openclaw_gateway_token(tmp_path / "bad.json") == ""


async def test_openclaw_reads_token_and_continues_the_main_conversation(monkeypatch, tmp_path) -> None:
    config = tmp_path / "openclaw.json"
    config.write_text('{"gateway": {"auth": {"token": "gateway-token"}}}')
    monkeypatch.setattr(setup_core, "OPENCLAW_CONFIG", config)
    monkeypatch.setattr(setup_core, "read_openclaw_gateway_token", lambda path=config: "gateway-token")
    monkeypatch.delenv("OPENCLAW_GATEWAY_TOKEN", raising=False)
    _script(monkeypatch, selects=["read", "main"])
    answers = wizard.WizardAnswers(agent_type="openclaw")
    console = _Console()
    assert await wizard.choose_openclaw_connection(answers, console) is None
    assert answers.openclaw_token == "gateway-token"
    assert answers.gateway_session_id == "agent:main:main"
    assert wizard.env_entries(answers)["OPENCLAW_GATEWAY_TOKEN"] == "gateway-token"
    profile = wizard.profile_toml(answers)
    assert 'gateway_session_id = "agent:main:main"' in profile
    assert "gateway-token" not in profile
    assert any("continue your main conversation" in line for line in console.lines)


async def test_openclaw_fresh_or_skip(monkeypatch) -> None:
    monkeypatch.setenv("OPENCLAW_GATEWAY_TOKEN", "from-env")
    _script(monkeypatch, selects=["fresh"])
    answers = wizard.WizardAnswers(agent_type="openclaw")
    assert await wizard.choose_openclaw_connection(answers, _Console()) is None
    assert answers.openclaw_token == "from-env" and answers.gateway_session_id == ""

    monkeypatch.delenv("OPENCLAW_GATEWAY_TOKEN")
    _script(monkeypatch, selects=["skip"])
    answers = wizard.WizardAnswers(agent_type="openclaw", gateway_session_id="agent:main:main")
    console = _Console()
    assert await wizard.choose_openclaw_connection(answers, console) is None
    assert answers.openclaw_token == "" and answers.gateway_session_id == ""
    assert any("openclaw acp" in line for line in console.lines)


def test_openclaw_profile_round_trips_its_session(tmp_path) -> None:
    from sleeptalk.config import load_resolved_config

    (tmp_path / "sleeptalk.toml").write_text('[transport]\ntype = "web"\n[agent]\ntype = "pi"\n')
    profiles = tmp_path / "config" / "profiles"
    profiles.mkdir(parents=True)
    answers = wizard.WizardAnswers(surface="web", name="Claw", slug="claw", agent_type="openclaw", gateway_session_id="agent:main:main")
    path = profiles / "claw.toml"
    path.write_text(wizard.profile_toml(answers))
    config = load_resolved_config(path, check_env=False, check_binaries=False)
    assert config.agent.gateway_session_id == "agent:main:main"
    assert wizard.load_existing_answers("claw", profiles).gateway_session_id == "agent:main:main"
    hydrated = wizard.WizardAnswers(agent_type="openclaw")
    wizard.hydrate_answers_from_env(hydrated, {"OPENCLAW_GATEWAY_TOKEN": "t"})
    assert hydrated.openclaw_token == "t"


def test_host_start_voice_reports_provider_errors_instead_of_crashing() -> None:
    import ast
    from pathlib import Path

    import sleeptalk.cli as cli

    tree = ast.parse(Path(cli.__file__).read_text())
    main = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_main")
    imported = {alias.asname or alias.name for node in ast.walk(main) if isinstance(node, ast.ImportFrom) for alias in node.names}
    assert "CallProviderError" in imported
