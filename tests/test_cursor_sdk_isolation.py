from __future__ import annotations

from contextlib import asynccontextmanager
import json
import os
from importlib.metadata import version
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sleeptalk.agents import cursor_sdk_isolation as isolation, onboarding_cli
from sleeptalk.agents.cursor_sdk_adapter import CursorSdkError
from sleeptalk.config import AgentConfig
from sleeptalk.defaults import _build_cursor_agent
from sleeptalk.runtime_probe import probe_runtime


@pytest.mark.asyncio
async def test_installed_sdk_wire_preserves_empty_tool_policy_and_workspace(tmp_path):
    sdk = pytest.importorskip("cursor_sdk")
    created = SimpleNamespace(agent_id="private-id", model=sdk.ModelSelection(id="chosen"))
    client = SimpleNamespace(agents=SimpleNamespace(create=AsyncMock(return_value=created), get=AsyncMock(
        return_value=SimpleNamespace(runtime="local", cwd=str(tmp_path)))))
    assert await isolation.create_agent(client, str(tmp_path), "chosen", "synthetic-key") is created
    wire = client.agents.create.call_args.args[0].to_json()
    assert wire["tools"] == {"names": []}
    assert wire["disallowedTools"] == ["mcp", "task"]
    assert wire["local"].get("settingSources", []) == []
    assert wire["local"]["autoReview"] is False
    assert wire["local"]["cwd"] == [str(tmp_path)]
    assert not wire.get("mcpServers") and not wire.get("agents")


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["model", "workspace", "cloud"])
async def test_sdk_binding_failures_do_not_pass(bad, tmp_path):
    sdk = pytest.importorskip("cursor_sdk")
    created = SimpleNamespace(agent_id="private-id", model=sdk.ModelSelection(id="wrong" if bad == "model" else "chosen"))
    info = SimpleNamespace(runtime="cloud" if bad == "cloud" else "local", cwd="/wrong" if bad == "workspace" else str(tmp_path))
    client = SimpleNamespace(agents=SimpleNamespace(create=AsyncMock(return_value=created), get=AsyncMock(return_value=info)))
    with pytest.raises(ValueError, match="confirm"):
        await isolation.create_agent(client, str(tmp_path), "chosen", "synthetic-key")


@pytest.mark.asyncio
async def test_sdk_deny_probe_uses_me_and_new_agent_without_a_turn(monkeypatch, tmp_path):
    monkeypatch.setattr(isolation, "bundled_bridge", lambda: ("node", "entry"))
    user = SimpleNamespace(api_key_name="private-name", created_at="2026-09-01")
    client = SimpleNamespace(me=AsyncMock(return_value=user))
    closed = []
    @asynccontextmanager
    async def bridge(workspace):
        assert workspace == str(tmp_path)
        try: yield client
        finally: closed.append(True)
    monkeypatch.setattr(isolation, "bridge", bridge)
    create = AsyncMock()
    monkeypatch.setattr(isolation, "create_agent", create)
    result = await probe_runtime(AgentConfig(type="cursor", runtime="native", on_incompatible="fail", workspace_dir=str(tmp_path), permission_mode="deny", model="chosen"), env={"CURSOR_API_KEY": "synthetic-key"})
    assert result["status"] == "ready" and result["permissions"] == "available", result
    assert result["adapter"] == "cursor" and result["runtime"] == "native"
    client.me.assert_awaited_once_with(api_key="synthetic-key")
    create.assert_awaited_once_with(client, str(tmp_path), "chosen", "synthetic-key")
    assert closed and "private-name" not in json.dumps(result) and "synthetic-key" not in json.dumps(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["pending", "auto"])
async def test_sdk_probe_does_not_change_requested_policy(monkeypatch, tmp_path, mode):
    launch = AsyncMock()
    monkeypatch.setattr(isolation, "bridge", launch)
    result = await isolation.probe(AgentConfig(workspace_dir=str(tmp_path), permission_mode=mode), {})
    assert result["permissions"] == "unavailable"
    launch.assert_not_called()


@pytest.mark.asyncio
async def test_runtime_factory_uses_deny_bridge_and_rejects_pending(monkeypatch, tmp_path):
    entered = []
    @asynccontextmanager
    async def bridge(workspace):
        entered.append(workspace)
        try: yield "client"
        finally: entered.append("closed")
    monkeypatch.setattr(isolation, "bridge", bridge)
    agent = SimpleNamespace(agent_id="new", close=AsyncMock())
    create = AsyncMock(return_value=agent)
    monkeypatch.setattr(isolation, "create_agent", create)
    client = _build_cursor_agent(AgentConfig(workspace_dir=str(tmp_path), permission_mode="deny", model="chosen"), {"CURSOR_API_KEY": "synthetic-key"})
    await client.warmup()
    await client.close()
    assert entered == [str(tmp_path), "closed"]
    create.assert_awaited_once_with("client", str(tmp_path), "chosen", "synthetic-key")
    pending = _build_cursor_agent(AgentConfig(workspace_dir=str(tmp_path), permission_mode="pending"), {})
    with pytest.raises(CursorSdkError, match="pending"):
        await pending.warmup()
    await pending.close()


@pytest.mark.asyncio
async def test_real_installed_bridge_isolated_discovery_and_empty_store(monkeypatch, tmp_path):
    pytest.importorskip("cursor_sdk")
    try:
        isolation.bundled_bridge()
    except (ValueError, FileNotFoundError):
        if os.environ.get("SLEEPTALK_TEST_CURSOR_SDK_VERSION"):
            raise
        pytest.skip("Verified platform SDK wheel unavailable")
    monkeypatch.setenv("NODE_OPTIONS", "--require=/nonexistent-sentinel-must-not-load")
    monkeypatch.setenv("CURSOR_API_KEY", "must-not-inherit")
    monkeypatch.setenv("DEEPGRAM_API_KEY", "must-not-inherit")
    async with isolation.bridge(str(tmp_path)) as client:
        listing = await client.agents.list({"cwd": str(tmp_path)})
        assert not listing.items
    assert not list(tmp_path.iterdir())


def test_installed_bridge_converter_preserves_empty_tools_and_no_settings(tmp_path):
    pytest.importorskip("cursor_sdk")
    try:
        node, entry = isolation.bundled_bridge()
    except (ValueError, FileNotFoundError):
        if os.environ.get("SLEEPTALK_TEST_CURSOR_SDK_VERSION"):
            raise
        pytest.skip("Verified platform SDK wheel unavailable")
    root = Path(entry).parents[2]
    converter = (root / "dist/sdk-converters.js").as_uri()
    proto = (root / "node_modules/@anysphere/proto/dist/generated/sdk/v1/sdk_messages_pb.js").as_uri()
    script = f"""
    import {{ AgentOptions }} from {json.dumps(proto)};
    import {{ protoAgentOptionsToSdk }} from {json.dumps(converter)};
    const options = protoAgentOptionsToSdk(AgentOptions.fromJson({{
      tools: {{names: []}}, disallowedTools: ['mcp', 'task'],
      local: {{cwd: ['/workspace'], settingSources: [], autoReview: false}}
    }}));
    if (JSON.stringify(options.tools) !== '[]') throw Error('lost empty tools');
    if (JSON.stringify(options.disallowedTools) !== '["mcp","task"]') throw Error('lost exclusions');
    if ((options.local.settingSources || []).length) throw Error('ambient settings');
    if (options.local.cwd !== '/workspace' || options.local.autoReview !== false) throw Error('lost local binding');
    """
    completed = subprocess.run([node, "--input-type=module", "-e", script], env={"HOME": str(tmp_path)}, cwd=tmp_path,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
    assert completed.returncode == 0, completed.stderr.decode()[-1000:]


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["external_url", "wrong_workspace", "oversized"])
async def test_bad_bridge_discovery_is_rejected_and_owned_group_reaped(monkeypatch, tmp_path, bad):
    pytest.importorskip("cursor_sdk")
    import asyncio
    monkeypatch.setattr(isolation, "bundled_bridge", lambda: ("node", "bridge.js"))
    killed = []
    monkeypatch.setattr(isolation.os, "killpg", lambda *args: killed.append(args))
    process = SimpleNamespace(pid=4711, wait=AsyncMock(return_value=0))
    async def spawn(*args, **kwargs):
        assert set(kwargs["env"]) & {"CURSOR_API_KEY", "NODE_OPTIONS", "DEEPGRAM_API_KEY"} == set()
        assert kwargs["start_new_session"] is True
        home = Path(kwargs["cwd"])
        token = home / "token"
        token.write_text("synthetic-local-bridge-token")
        ready = {"schemaVersion": 1, "protocol": "connect", "transport": "tcp", "pid": 4711,
                 "workspaceRef": "/wrong" if bad == "wrong_workspace" else str(tmp_path), "stateRoot": str(home),
                 "authTokenFile": str(token), "url": "http://external.invalid:1234" if bad == "external_url" else "http://127.0.0.1:1234"}
        process.stderr = SimpleNamespace(readline=AsyncMock(return_value=b"x" * 70000 if bad == "oversized" else b"cursor-sdk-bridge ready " + json.dumps(ready).encode() + b"\n"))
        return process
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(ValueError, match="discovery"):
        async with isolation.bridge(str(tmp_path)):
            pytest.fail("Untrusted discovery must never create a client")
    assert [item[0] for item in killed] == [4711, 4711]


@pytest.mark.asyncio
async def test_full_runtime_probe_rejects_pending_sdk_before_bridge(monkeypatch, tmp_path):
    launch = AsyncMock()
    monkeypatch.setattr(isolation, "bridge", launch)
    result = await probe_runtime(AgentConfig(type="cursor", runtime="native", on_incompatible="fail", workspace_dir=str(tmp_path), permission_mode="pending", model="chosen"), env={"CURSOR_API_KEY": "synthetic-key"})
    assert result["status"] == "needs_input"
    launch.assert_not_called()


def test_required_release_wheel_cannot_silently_skip():
    expected = os.environ.get("SLEEPTALK_TEST_CURSOR_SDK_VERSION")
    if expected:
        assert version("cursor-sdk") == expected
        isolation.bundled_bridge()
