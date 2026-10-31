"""Keep runtime selection and onboarding dispatch in agreement."""
import json

import pytest

from sleeptalk.config import AgentConfig
from sleeptalk.defaults import register_defaults
from sleeptalk.registries import AGENTS
from sleeptalk.runtime_probe import PROBE_GROUPS, probe_runtime

register_defaults()
BUILTINS = tuple(AGENTS.registrations())
# Bring-your-own types require private options to form a valid profile.
SAMPLE_OPTIONS = {
    "command": {"command": ["my-agent"]},
    "http": {"url": "https://agent.example.com/turn"},
    "bridge": {"url": "wss://agent.example.com/v1/agent-bridge", "token_env": "MY_BRIDGE_TOKEN"},
}


def test_every_registered_adapter_has_an_onboarding_inspector():
    assert set(PROBE_GROUPS) == {item.name for item in BUILTINS}


@pytest.mark.parametrize("registration", BUILTINS, ids=lambda item: item.name)
async def test_aliases_dispatch_to_selected_registration_with_filtered_environment(registration, tmp_path, monkeypatch):
    from sleeptalk.agents import onboarding_cli, onboarding_rpc
    calls = []

    async def inspect(agent, name, environment):
        calls.append(name)
        assert "DEEPGRAM_API_KEY" not in environment
        assert environment["ADAPTER_AUTH_SENTINEL"] == "private"
        return {"status": "needs_input", "protocol": "available", "login": "unknown"}

    async def codex(agent, workspace, environment, result):
        return {**result, **await inspect(agent, "codex", environment)}

    monkeypatch.setattr(onboarding_cli, "probe", inspect)
    monkeypatch.setattr(onboarding_rpc, "probe", inspect)
    monkeypatch.setattr("sleeptalk.runtime_probe._probe_codex", codex)
    from sleeptalk.agents import byo
    for kind in ("command", "http", "bridge"):
        async def registration_probe(agent, environment, kind=kind):
            return await inspect(agent, kind, environment)
        monkeypatch.setattr(byo, f"probe_{kind}_registration", registration_probe)
    from sleeptalk.agents import model_only

    async def model_probe(agent, environment):
        return await inspect(agent, "model", environment)
    monkeypatch.setattr(model_only, "probe", model_probe)
    environment = {"DEEPGRAM_API_KEY": "voice-secret", "ADAPTER_AUTH_SENTINEL": "private", "CURSOR_API_KEY": "fixture"}
    for name in (registration.name, *registration.aliases):
        options = SAMPLE_OPTIONS.get(registration.name, {})
        result = await probe_runtime(AgentConfig(type=name, workspace_dir=str(tmp_path), options=options), env=environment)
        assert result["adapter"] == registration.name
        assert result["status"] == "needs_input"
        assert result["login"] == "unknown"
        assert "private" not in json.dumps(result)
    assert calls == [registration.name] * (1 + len(registration.aliases))


@pytest.mark.parametrize("surface", ["web", "terminal", "discord"])
@pytest.mark.parametrize("registration", BUILTINS, ids=lambda item: item.name)
async def test_private_input_preserves_adapter_gate_for_every_surface(surface, registration, tmp_path, monkeypatch):
    from sleeptalk.private_input import InputSpec, start
    async def incomplete(agent):
        assert agent.type == registration.name
        assert agent.on_incompatible == "fail"
        return {"status": "needs_input", "login": "unknown"}
    monkeypatch.setattr("sleeptalk.runtime_probe.probe_runtime", incomplete)
    monkeypatch.setattr("sleeptalk.setup.ensure_dependencies", lambda *args, **kwargs: ())
    monkeypatch.setenv("SLEEPTALK_HOME", str(tmp_path / "profiles"))
    spec = InputSpec(request_id="coverage-001", profile="test", workspace=str(tmp_path),
        agent=registration.name, surface=surface, tts_provider="cartesia",
        capture_mode="headphones" if surface == "terminal" else "", user_terminal_local=surface == "terminal")
    result = await start(spec, user_browser_local=True, root=tmp_path / "setup")
    assert result["status"] == "needs_input"
    assert "input_url" not in result
    assert not (tmp_path / "setup").exists()


async def test_cursor_selection_matches_call_environment(tmp_path, monkeypatch):
    from sleeptalk.agents import onboarding_cli
    async def inspect(agent, name, env):
        return {"status": "needs_input"}
    monkeypatch.setattr(onboarding_cli, "probe", inspect)
    agent = AgentConfig(type="cursor", workspace_dir=str(tmp_path))
    assert (await probe_runtime(agent, env={}))["adapter"] == "cursor_acp"
    assert (await probe_runtime(agent, env={"CURSOR_API_KEY": "fixture"}))["adapter"] == "cursor"


async def test_explicit_incompatible_runtime_never_silently_falls_back(tmp_path):
    result = await probe_runtime(AgentConfig(type="codex", workspace_dir=str(tmp_path), runtime="gateway"), env={})
    assert result["status"] == "needs_input"
    assert "ready" not in result.values()


async def test_irrelevant_gateway_setting_rejected_before_adapter_process(tmp_path, monkeypatch):
    async def forbidden(*args):
        pytest.fail("invalid settings must not launch an adapter")
    monkeypatch.setattr("sleeptalk.runtime_probe._probe_codex", forbidden)
    result = await probe_runtime(AgentConfig(type="codex", workspace_dir=str(tmp_path), gateway_url="wss://example.com"), env={})
    assert result["status"] == "needs_input"
    assert "do not apply" in result["next_action"]


def test_cli_default_probe_omits_unset_adapter_fields(tmp_path, monkeypatch, capsys):
    from sleeptalk.cli import main
    async def inspect(agent):
        assert agent.model_fields_set == {"type", "workspace_dir", "on_incompatible"}
        return {"status": "ready"}
    monkeypatch.setattr("sleeptalk.runtime_probe.probe_runtime", inspect)
    assert main(["host", "probe-runtime", "--agent", "codex", "--workspace", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "ready"


def test_cli_terminal_setup_forwards_user_choices(tmp_path, monkeypatch, capsys):
    from sleeptalk.cli import main
    async def inspect(spec, *, user_browser_local):
        assert user_browser_local and spec.user_terminal_local
        assert spec.surface == "terminal" and spec.capture_mode == "headphones"
        assert spec.runtime == "native" and spec.permission_mode == "pending"
        return {"status": "needs_input"}
    monkeypatch.setattr("sleeptalk.private_input.start", inspect)
    result = main(["host", "input", "start", "--request-id", "test-cli-001", "--profile", "test",
        "--agent", "codex", "--workspace", str(tmp_path), "--surface", "terminal",
        "--capture-mode", "headphones", "--runtime", "native", "--permission-mode", "pending",
        "--user-browser-local", "--user-terminal-local", "--tts-provider", "cartesia"])
    assert result != 0
    assert json.loads(capsys.readouterr().out)["status"] == "needs_input"
