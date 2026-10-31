"""The installed Hermes is the default runtime; the pinned isolated one is opt-in."""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat

import pytest

from sleeptalk.agents import hermes_bridge as bridge
from sleeptalk.agents import onboarding_rpc as rpc
from sleeptalk.config import AgentConfig


def fake_hermes(tmp_path: Path, *, protocol_version: int = 1) -> Path:
    """A ``hermes`` stand-in whose ``acp`` mode answers one ACP initialize."""
    script = tmp_path / "bin" / "hermes"
    script.parent.mkdir(exist_ok=True)
    script.write_text(f'''#!/usr/bin/env python3
import json, sys
assert sys.argv[1:] == ["acp"], sys.argv
request = json.loads(sys.stdin.readline())
print(json.dumps({{"jsonrpc": "2.0", "id": request["id"], "result": {{"protocolVersion": {protocol_version}}}}}), flush=True)
''')
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def env_with(tmp_path: Path) -> dict[str, str]:
    return {"PATH": str(tmp_path / "bin") + os.pathsep + os.environ.get("PATH", "")}


def test_installed_hermes_is_the_default_and_isolation_is_explicit(tmp_path):
    installed = AgentConfig(type="acp", workspace_dir=str(tmp_path), permission_mode="pending")
    assert bridge.protected_config(installed) is None
    pinned = installed.model_copy(update={"options": {"hermes_root": str(tmp_path)}})
    assert bridge.protected_config(pinned) is pinned
    with pytest.raises(ValueError, match="pending or deny"):
        bridge.protected_config(pinned.model_copy(update={"permission_mode": "auto"}))


def test_explicit_model_auth_requires_the_isolated_runtime(tmp_path):
    from sleeptalk.agent_auth import AgentAuthConfig
    auth = AgentAuthConfig(mode="api_key", protocol="openai-completions", base_url="https://api.openai.com/v1",
                           credential_ref="credential:" + "0" * 32)
    installed = AgentConfig(type="acp", workspace_dir=str(tmp_path), permission_mode="pending", auth=auth)
    with pytest.raises(ValueError, match="hermes_root"):
        bridge.protected_config(installed)


async def test_probe_accepts_the_installed_hermes_over_acp(tmp_path):
    fake_hermes(tmp_path)
    agent = AgentConfig(type="acp", workspace_dir=str(tmp_path), permission_mode="pending")
    result = await rpc.probe(agent, "acp", env_with(tmp_path))
    assert result["status"] == "ready", result
    assert result["protocol"] == "available"
    assert result["permissions"] == result["workspace"] == result["login"] == "inherited"


async def test_probe_without_approvals_never_launches_the_installed_hermes(tmp_path, monkeypatch):
    fake_hermes(tmp_path)
    from unittest.mock import AsyncMock
    spawn = AsyncMock(side_effect=AssertionError("must not spawn"))
    monkeypatch.setattr(rpc, "_stdio", spawn)
    agent = AgentConfig(type="acp", workspace_dir=str(tmp_path), permission_mode="")
    result = await rpc.probe(agent, "acp", env_with(tmp_path))
    assert result["status"] == "needs_input" and result["permissions"] == "unavailable"
    spawn.assert_not_awaited()


async def test_probe_names_the_missing_hermes_and_the_alternative(tmp_path, monkeypatch):
    def missing(name, env):
        raise FileNotFoundError(name)
    monkeypatch.setattr(rpc, "_binary", missing)  # the real fallback would find a Hermes in ~/.local/bin
    agent = AgentConfig(type="acp", workspace_dir=str(tmp_path), permission_mode="deny")
    result = await rpc.probe(agent, "acp", {"PATH": str(tmp_path / "empty")})
    assert result["status"] == "needs_input" and result["protocol"] == "unavailable"
    assert "hermes acp" in result["next_action"] and "hermes_root" in result["next_action"]


async def test_probe_rejects_an_unsupported_acp_version(tmp_path):
    fake_hermes(tmp_path, protocol_version=2)
    agent = AgentConfig(type="acp", workspace_dir=str(tmp_path), permission_mode="pending")
    result = await rpc.probe(agent, "acp", env_with(tmp_path))
    assert result["status"] == "needs_input" and result["protocol"] == "unavailable"


async def test_hermes_root_selects_the_pinned_runtime_and_names_the_release(tmp_path):
    fake_hermes(tmp_path)
    agent = AgentConfig(type="acp", workspace_dir=str(tmp_path), permission_mode="pending",
                        options={"hermes_root": str(tmp_path / "not-hermes")}, pi_bin="")
    result = await rpc.probe(agent, "acp", env_with(tmp_path))
    assert result["status"] == "needs_input"
    assert bridge.HERMES_TAG in result["next_action"] and "git clone" in result["next_action"]


async def test_gateway_without_url_or_root_points_at_both_options(tmp_path):
    agent = AgentConfig(type="hermes_gateway", workspace_dir=str(tmp_path), permission_mode="pending")
    result = await rpc.probe(agent, "hermes_gateway", {})
    assert result["status"] == "needs_input"
    assert "--gateway-url" in result["next_action"] and "--agent hermes" in result["next_action"]


@pytest.mark.parametrize("stage", ["initialize", "session/new", "session/load"])
@pytest.mark.parametrize("behavior", ["exit", "stall"])
async def test_failed_acp_startup_settles_and_can_retry(tmp_path, monkeypatch, stage, behavior):
    import asyncio
    import sys
    from sleeptalk.agents import hermes_adapter as adapter

    script = tmp_path / "failed-hermes"
    requests = tmp_path / "requests.log"
    script.write_text(f'''#!{sys.executable}
import json, sys, time
for line in sys.stdin:
    request = json.loads(line)
    with open({str(requests)!r}, "a") as log:
        log.write(request["method"] + "\\n")
    if request["method"] == {stage!r}:
        if {behavior!r} == "exit":
            sys.exit(2)
        time.sleep(60)
    print(json.dumps({{"jsonrpc": "2.0", "id": request["id"],
                      "result": {{"protocolVersion": 1,
                                   "agentCapabilities": {{"loadSession": True}}}}}}), flush=True)
''')
    script.chmod(0o700)
    monkeypatch.setattr(adapter, "ACP_REQUEST_TIMEOUT", 0.3)
    client = adapter.HermesACPClient(binary=str(script), cwd=str(tmp_path), permission_mode="deny")
    if stage == "session/load":
        assert client.use_saved_session(tmp_path)
        client._session_file.write_text("saved-conversation")
    expected = "connection closed" if behavior == "exit" else "timed out"
    try:
        for attempt in range(2):
            with pytest.raises(adapter.HermesACPError, match=expected):
                await asyncio.wait_for(client.warmup(), 3)
            assert client._proc is None
            assert client._reader_task is None
            assert client._pending == {}
            assert client._session_id == ""
            if stage == "session/load":
                assert client._session_file.read_text() == "saved-conversation"
                assert requests.read_text().splitlines() == ["initialize", "session/load"] * (attempt + 1)
    finally:
        await client.close()
