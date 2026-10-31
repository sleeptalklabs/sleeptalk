"""Protected Hermes proof, including the pinned upstream agent when installed."""
import asyncio
import json
import os
from pathlib import Path
import threading
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from sleeptalk.agents import hermes_bridge as bridge
from sleeptalk.config import AgentConfig


def selected(tmp_path, mode="deny", transport="acp"):
    return AgentConfig(type=transport, runtime="acp" if transport == "acp" else "gateway", on_incompatible="fail",
                       pi_bin=os.environ.get("SLEEPTALK_TEST_HERMES_PYTHON", ""),
                       options={"hermes_root": os.environ.get("SLEEPTALK_TEST_HERMES_ROOT", "")},
                       provider="openai", model="gpt-4o", workspace_dir=str(tmp_path), permission_mode=mode)


@pytest.fixture
def tmp_path():
    # Hermes deliberately refuses file tools under macOS /private/var.
    with tempfile.TemporaryDirectory(prefix="sleeptalk-hermes-test-", dir="/tmp" if Path("/tmp").is_dir() else None) as path:
        yield Path(path).resolve()


@pytest.fixture
def upstream():
    if not os.environ.get("SLEEPTALK_TEST_HERMES_PYTHON") or not os.environ.get("SLEEPTALK_TEST_HERMES_ROOT"):
        pytest.skip("Set SLEEPTALK_TEST_HERMES_PYTHON and SLEEPTALK_TEST_HERMES_ROOT for real pinned Hermes proof")


def test_no_unpinned_or_ambient_startup(tmp_path):
    config = selected(tmp_path)
    with pytest.raises(ValueError, match="pinned Hermes virtual"):
        bridge.prepare(config.model_copy(update={"pi_bin": ""}), {}, transport="acp")
    with pytest.raises(ValueError, match="pending or deny"):
        bridge.prepare(config.model_copy(update={"permission_mode": "auto"}), {}, transport="acp")


@pytest.mark.parametrize("transport", ["acp", "hermes_gateway"])
async def test_real_probe_binds_runtime_before_provider_acceptance(upstream, tmp_path, monkeypatch, transport):
    from sleeptalk.runtime_probe import probe_runtime
    from sleeptalk.agents import provider_auth
    called = []
    async def verify(selection, model):
        called.append((selection.credential, selection.base_url, model))
        return "available"
    monkeypatch.setattr(provider_auth, "verify", verify)
    config = selected(tmp_path, transport=transport)
    result = await probe_runtime(config, env={"OPENAI_API_KEY": "synthetic-key"})
    assert result["status"] == "ready", result
    assert result["adapter"] == transport
    assert called == [("synthetic-key", "https://api.openai.com/v1", "gpt-4o")]
    async def rejected(*a):
        return "unavailable"
    monkeypatch.setattr(provider_auth, "verify", rejected)
    result = await probe_runtime(config, env={"OPENAI_API_KEY": "synthetic-key"})
    assert result["status"] == "needs_input" and result["login"] == "unavailable"


@pytest.mark.parametrize("transport", ["acp", "hermes_gateway"])
@pytest.mark.parametrize("mode,allow", [("deny", False), ("pending", False), ("pending", True), ("pending", "cancel")])
async def test_real_turn_enforces_every_tool_before_dispatch(upstream, tmp_path, monkeypatch, transport, mode, allow):
    from sleeptalk.agents.construction import build_agent
    from sleeptalk.adapter import PermissionRequested, PermissionDecision, ResolvePermission, typed_events
    marker = tmp_path / "approved.txt"
    seen = []
    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path == "/api/show":
                # Recent Hermes probes loopback providers for Ollama metadata.
                # This fixture is an OpenAI endpoint, not an Ollama server.
                self.send_error(404)
                return
            seen.append((self.path, self.headers.get("Authorization"), body))
            assert body.get("model") == "gpt-4o", (self.path, body)
            if not any(m["role"] == "tool" for m in body["messages"]):
                message = {"role": "assistant", "content": None, "tool_calls": [
                    {"id": "call-file", "type": "function", "function": {"name": "write_file", "arguments": json.dumps({"path": str(marker), "content": "approved"})}},
                    {"id": "call-terminal", "type": "function", "function": {"name": "terminal", "arguments": json.dumps({"command": "pwd", "timeout": 5})}},
                ]}
                reason = "tool_calls"
            else:
                message = {"role": "assistant", "content": "Verified response."}
                reason = "stop"
            if body.get("stream"):
                delta = dict(message)
                if "tool_calls" in delta:
                    delta["tool_calls"] = [{"index": i, **call} for i, call in enumerate(delta["tool_calls"])]
                chunks = [{"id": "fixture", "object": "chat.completion.chunk", "created": 1, "model": "gpt-4o",
                           "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                          {"id": "fixture", "object": "chat.completion.chunk", "created": 1, "model": "gpt-4o",
                           "choices": [{"index": 0, "delta": {}, "finish_reason": reason}],
                           "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}]
                data = ("".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n").encode()
                content_type = "text/event-stream"
            else:
                data = json.dumps({"id": "fixture", "object": "chat.completion", "created": 1, "model": "gpt-4o",
                                   "choices": [{"index": 0, "message": message, "finish_reason": reason}],
                                   "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}).encode()
                content_type = "application/json"
            self.send_response(200); self.send_header("Content-Type", content_type); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    original = bridge.prepare
    def fixture_prepare(*a, **kw):
        launch = original(*a, **kw)
        config = json.loads(launch.env["SLEEPTALK_HERMES_CONFIG"])
        # Only this fixture changes the selected HTTPS origin to a loopback
        # provider. Production prepare rejects HTTP authentication endpoints.
        config["base_url"] = f"http://127.0.0.1:{server.server_port}/v1"
        launch.env["SLEEPTALK_HERMES_CONFIG"] = json.dumps(config)
        return launch
    monkeypatch.setattr(bridge, "prepare", fixture_prepare)
    client, _ = build_agent(selected(tmp_path, mode, transport), {"OPENAI_API_KEY": "synthetic-key"})
    decisions = []
    tasks = []
    def event(value):
        if isinstance(value, PermissionRequested):
            decisions.append(value)
            assert not marker.exists()
            if allow == "cancel" and len(decisions) == 2:
                tasks.append(asyncio.create_task(client.abort()))
            else:
                tasks.append(asyncio.create_task(client.control(ResolvePermission(request_id=value.request_id,
                                    decision=PermissionDecision.ALLOW_ONCE if allow else PermissionDecision.DENY))))
    try:
        answer = " ".join([piece async for piece in client.respond_streaming("Run the fixture tools.", timeout=20, on_event=typed_events(event))])
        if allow == "cancel":
            assert not marker.exists()
            assert len(decisions) == 2
            return
        assert answer == "Verified response."
        assert len(seen) == 2
        assert all(auth == "Bearer synthetic-key" for _, auth, _ in seen)
        assert marker.exists() is allow
        if allow:
            assert marker.read_text() == "approved"
        assert len(decisions) == (2 if mode == "pending" else 0), seen[-1][2]["messages"]
        assert all(any(name in d.prompt for name in ("write_file", "terminal")) for d in decisions)
        assert all(d.options == frozenset({PermissionDecision.ALLOW_ONCE, PermissionDecision.DENY}) for d in decisions)
        results = [m for m in seen[-1][2]["messages"] if m["role"] == "tool"]
        assert len(results) == 2
        if allow:
            assert str(tmp_path) in next(m["content"] for m in results if m["tool_call_id"] == "call-terminal")
    finally:
        await client.close()
        await asyncio.gather(*tasks)
        server.shutdown(); server.server_close(); thread.join()


@pytest.mark.parametrize("mode,protocol,endpoint,key,model", [
    ("api_key", "anthropic-messages", "https://api.anthropic.com", "sk-ant-api03-synthetic", "claude-sonnet-4-5"),
    ("api_key", "anthropic-messages", "https://provider.example", "custom-key", "selected-model"),
    ("api_key", "openai-responses", "https://provider.example/v1", "custom-key", "selected-model"),
    ("api_key", "openai-completions", "https://provider.example/v1", "custom-key", "selected-model"),
])
async def test_real_provider_protocol_and_auth_binding(upstream, tmp_path, monkeypatch, mode, protocol, endpoint, key, model):
    from sleeptalk.agents import provider_auth
    async def verify(selection, selected_model):
        assert (selection.mode, selection.protocol, selection.base_url, selection.credential, selected_model) == (mode, protocol, endpoint, key, model)
        return "available"
    monkeypatch.setattr(provider_auth, "verify", verify)
    config = selected(tmp_path).model_copy(update={"model": model})
    env = {"SLEEPTALK_PROVIDER_AUTH": json.dumps({"mode": mode, "protocol": protocol, "base_url": endpoint}),
           "SLEEPTALK_PROVIDER_CREDENTIAL": key}
    result = await bridge.probe(config, env, "acp")
    assert result["status"] == "ready", result


async def test_real_startup_ignores_dotenv_site_and_inherited_hooks(upstream, tmp_path):
    from sleeptalk.agents.onboarding_rpc import _stdio
    root = tmp_path / "source"
    root.mkdir()
    upstream_root = Path(os.environ["SLEEPTALK_TEST_HERMES_ROOT"])
    for source in upstream_root.iterdir():
        if not source.name.startswith("."):
            (root / source.name).symlink_to(source, target_is_directory=source.is_dir())
    upstream_python = Path(os.environ["SLEEPTALK_TEST_HERMES_PYTHON"])
    upstream_site = next((upstream_python.parent.parent / "lib").glob("python*/site-packages"))
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    python = venv / "bin/python"
    python.symlink_to(upstream_python)
    (venv / "pyvenv.cfg").write_text((upstream_python.parent.parent / "pyvenv.cfg").read_text())
    site = venv / "lib" / upstream_site.parent.name / "site-packages"
    site.mkdir(parents=True)
    for dependency in upstream_site.iterdir():
        (site / dependency.name).symlink_to(dependency, target_is_directory=dependency.is_dir())
    sentinel = tmp_path / "ambient-started"
    (site / "sleeptalk_fixture_startup.pth").write_text(f"import pathlib; pathlib.Path({str(sentinel)!r}).write_text('bad')\n")
    (root / ".env").write_text("SLEEPTALK_AMBIENT_SENTINEL=bad\nHERMES_SAFE_MODE=0\n")
    (tmp_path / "sitecustomize.py").write_text(f"from pathlib import Path; Path({str(sentinel)!r}).write_text('bad')")
    config = selected(tmp_path).model_copy(update={"pi_bin": str(python), "options": {"hermes_root": str(root)}})
    launch = bridge.prepare(config, {"OPENAI_API_KEY": "synthetic-key", "PYTHONPATH": str(tmp_path),
                            "HERMES_HOME": str(tmp_path), "PYTHONSTARTUP": str(tmp_path / "sitecustomize.py")}, transport="acp", nonce="fixture")
    try:
        assert "PYTHONPATH" not in launch.env and "PYTHONSTARTUP" not in launch.env
        result = await _stdio(launch.args, launch.home.name, launch.env, {"id": "probe", "method": "sleeptalk/probe"})
        assert result["result"]["credential_bound"] is True
        assert not sentinel.exists()
    finally:
        launch.close()



@pytest.mark.parametrize("transport", ["acp", "gateway"])
async def test_permission_cancel_and_stale_response_fail_closed(transport):
    import io
    from types import SimpleNamespace
    from sleeptalk.agents.hermes_bridge_runtime import Bridge
    agent = SimpleNamespace(session_id="session", interrupt=lambda: None)
    instance = Bridge({"transport": transport, "permission_mode": "pending"}, agent, io.StringIO())
    instance.session_created = True
    pending = asyncio.create_task(instance.permission("tool", "write_file"))
    await asyncio.sleep(0)
    request_id = next(iter(instance.pending))
    if transport == "gateway":
        with pytest.raises(ValueError, match="matching pending"):
            await instance.handle({"id": 1, "method": "approval.respond", "params": {
                "session_id": "session", "request_id": "stale", "choice": "once"}})
    else:
        with pytest.raises(ValueError):
            await instance.handle({"id": "stale", "result": {"outcome": {"outcome": "selected", "optionId": "once"}}})
    assert not pending.done()
    instance.interrupt()
    assert await pending is False
    assert request_id not in instance.pending


async def test_permission_timeout_is_denial(monkeypatch):
    import io
    from types import SimpleNamespace
    from sleeptalk.agents import hermes_bridge_runtime as runtime
    real_wait = asyncio.wait_for
    async def short_wait(future, timeout):
        assert timeout == 120
        return await real_wait(future, 0.01)
    monkeypatch.setattr(runtime.asyncio, "wait_for", short_wait)
    instance = runtime.Bridge({"transport": "acp", "permission_mode": "pending"},
                              SimpleNamespace(session_id="session", interrupt=lambda: None), io.StringIO())
    assert await instance.permission("tool", "write_file") is False
    assert instance.pending == {}


async def test_real_subscription_login_never_reaches_hermes(upstream, tmp_path):
    config = selected(tmp_path).model_copy(update={"provider": "anthropic", "model": "claude-sonnet-4-5"})
    env = {"SLEEPTALK_PROVIDER_AUTH": json.dumps({"mode": "oauth", "protocol": "anthropic-messages", "base_url": "https://api.anthropic.com"}),
           "SLEEPTALK_PROVIDER_CREDENTIAL": "sk-ant-oat01-synthetic"}
    with pytest.raises(ValueError, match="vendor's own app"):
        bridge.prepare(config, env, transport="acp")


async def test_real_session_cannot_adopt_ambient_env_credentials(upstream, tmp_path):
    """Hermes 0.21 adopts .env and environment key edits at each turn; the bridge must not."""
    import subprocess
    config = selected(tmp_path)
    env = {"SLEEPTALK_PROVIDER_AUTH": json.dumps({"mode": "api_key", "protocol": "openai-completions", "base_url": "https://provider.example/v1"}),
           "SLEEPTALK_PROVIDER_CREDENTIAL": "sk-selected-synthetic"}
    launch = bridge.prepare(config, env, transport="acp")
    harness = tmp_path / "env-adoption-proof.py"
    harness.write_text("""
import importlib.util,json,os,socket,subprocess,sys
from pathlib import Path
runtime,root,site=sys.argv[1:]
sys.path[:0]=[root,site]
spec=importlib.util.spec_from_file_location('owned_bridge',runtime)
module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
def forbidden(*a,**k): raise AssertionError('ambient IO was attempted')
socket.socket.connect=forbidden; subprocess.Popen=forbidden
config=json.loads(os.environ.pop('SLEEPTALK_HERMES_CONFIG'))
key=os.environ.pop('SLEEPTALK_HERMES_CREDENTIAL')
agent=module.initialize(config,key)
hostile={'OPENAI_API_KEY':'sk-ambient-hostile','OPENAI_BASE_URL':'https://attacker.example/v1',
         'OPENROUTER_API_KEY':'sk-ambient-hostile','ANTHROPIC_API_KEY':'sk-ambient-hostile'}
os.environ.update(hostile)
Path(os.environ['HERMES_HOME'],'.env').write_text(''.join(f'{k}={v}\\n' for k,v in hostile.items()))
assert agent._try_refresh_env_client_credentials() is False
assert agent.client.api_key == key
assert str(agent.client.base_url).rstrip('/') == config['base_url'].rstrip('/')
agent.close()
print('selected credential kept')
""")
    try:
        result = await asyncio.to_thread(subprocess.run, [launch.args[0], "-I", "-S", str(harness), *launch.args[3:]],
                                         cwd=launch.home.name, env=launch.env, capture_output=True, text=True, timeout=20)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "selected credential kept"
    finally:
        launch.close()


def test_runtime_reports_the_pinned_hermes_version():
    import importlib.util
    from sleeptalk.agents import hermes_bridge_runtime as runtime
    assert runtime.HERMES_VERSION == bridge.HERMES_VERSION
    assert {"_try_refresh_env_client_credentials", "_try_activate_fallback"} <= set(runtime.DISABLED_RECOVERY)


async def test_acp_spawn_failure_releases_owned_home(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from sleeptalk.agents.hermes_adapter import HermesACPClient
    closed = []
    launch = SimpleNamespace(args=["missing"], env={}, home=SimpleNamespace(name=str(tmp_path)), close=lambda: closed.append(True))
    monkeypatch.setattr(bridge, "prepare", lambda *a, **k: launch)
    async def fail(*a, **k):
        raise FileNotFoundError("fixture")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fail)
    client = HermesACPClient(bridge_config=object(), permission_mode="pending")
    with pytest.raises(FileNotFoundError):
        await client.warmup()
    assert closed == [True]
    assert client._bridge_launch is None and client._proc is None


@pytest.mark.parametrize("cancel", [False, True])
async def test_gateway_session_failure_closes_and_resets_owned_runtime(cancel):
    from types import SimpleNamespace
    from sleeptalk.agents.hermes_gateway_adapter import HermesGatewayClient, HermesGatewayError
    closed = []
    class Link:
        calls = 0
        async def request(self, *a):
            self.calls += 1
            raise asyncio.CancelledError() if cancel else ValueError("fixture")
        async def close(self):
            closed.append("link")
    link = Link()
    client = HermesGatewayClient(bridge_config=object(), permission_mode="pending", link=link)
    client._bridge_launch = SimpleNamespace(close=lambda: closed.append("home"))
    with pytest.raises(asyncio.CancelledError if cancel else HermesGatewayError):
        await client.warmup()
    assert closed == ["link", "home"]
    assert client._link is None and client._bridge_launch is None and client.session_id == ""
    with pytest.raises(asyncio.CancelledError if cancel else HermesGatewayError):
        await client.warmup()
    assert link.calls == 2


async def test_protected_gateway_rejects_persistent_permission_without_starting():
    from sleeptalk.adapter import PermissionDecision, ResolvePermission, ControlRejected
    from sleeptalk.agents.hermes_gateway_adapter import HermesGatewayClient
    client = HermesGatewayClient(bridge_config=object(), permission_mode="pending")
    result = await client.control(ResolvePermission(request_id="unknown", decision=PermissionDecision.ALLOW_SESSION))
    assert isinstance(result, ControlRejected)
    assert client._link is None


def test_explicit_auth_is_never_ignored_by_legacy_factory():
    from types import SimpleNamespace
    with pytest.raises(ValueError, match="pending or deny"):
        bridge.protected_config(SimpleNamespace(permission_mode="auto", auth={"mode": "oauth"}, options={}))


@pytest.mark.parametrize("transport", ["acp", "hermes_gateway"])
@pytest.mark.parametrize("mode,protocol,endpoint,model", [
    ("api_key", "openai-completions", "https://api.openai.com/v1", "gpt-4o"),
    ("api_key", "openai-responses", "https://custom.example/v1", "selected-model"),
    ("api_key", "anthropic-messages", "https://custom.example", "selected-model"),
])
async def test_real_configuration_only_never_authenticates_or_reports_ready(upstream, tmp_path, monkeypatch, transport, mode, protocol, endpoint, model):
    from sleeptalk.agents import provider_auth
    async def forbidden(*a, **k):
        pytest.fail("configuration-only inspection attempted provider authentication")
    monkeypatch.setattr(provider_auth, "verify", forbidden)
    config = selected(tmp_path, transport=transport).model_copy(update={"model": model})
    result = await bridge.probe_configuration(config, {"mode": mode, "protocol": protocol, "base_url": endpoint})
    assert result["configuration"] == "available", result
    assert result["status"] == "needs_input" and result["login"] == "unknown"
    assert result["workspace"] == result["permissions"] == result["protocol"] == "available"
    assert "configuration-only" not in json.dumps(result)


@pytest.mark.parametrize("changes", [
    {"type": "openclaw", "runtime": "gateway"},
    {"permission_mode": "auto"},
    {"permission_mode": ""},
    {"gateway_url": "https://remote.example"},
    {"gateway_session_id": "existing"},
    {"type": "hermes", "runtime": ""},
])
async def test_configuration_only_refuses_other_or_ambient_runtimes(tmp_path, monkeypatch, changes):
    from unittest.mock import AsyncMock
    spawn = AsyncMock(side_effect=AssertionError("must not spawn"))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    result = await bridge.probe_configuration(selected(tmp_path).model_copy(update=changes), {
        "mode": "api_key", "protocol": "openai-completions", "base_url": "https://api.openai.com/v1"})
    spawn.assert_not_awaited()
    assert result["configuration"] == "unknown" and result["login"] == "unknown" and result["status"] != "ready"


async def test_real_openai_request_clients_preserve_credential_and_redirect_policy(upstream, tmp_path):
    import subprocess
    config = selected(tmp_path)
    launch = bridge.prepare(config, {"OPENAI_API_KEY": "synthetic-key"}, transport="acp")
    harness = tmp_path / "request-client-proof.py"
    harness.write_text("""
import importlib.util,json,os,socket,subprocess,sys
runtime,root,site=sys.argv[1:]
sys.path[:0]=[root,site]
spec=importlib.util.spec_from_file_location('owned_bridge',runtime)
module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
def forbidden(*a,**k): raise AssertionError('ambient IO was attempted')
socket.socket.connect=forbidden; subprocess.Popen=forbidden
config=json.loads(os.environ.pop('SLEEPTALK_HERMES_CONFIG'))
key=os.environ.pop('SLEEPTALK_HERMES_CREDENTIAL')
agent=module.initialize(config,key)
client=agent._create_request_openai_client(reason='fixture')
assert client.api_key == key
assert str(client.base_url).rstrip('/') == config['base_url']
assert client._client.follow_redirects is False
client.close()
assert agent._replace_primary_openai_client(reason='fixture') is True
assert agent.client._client.follow_redirects is False
agent._client_kwargs['base_url']='https://unexpected.example/v1'
assert agent._replace_primary_openai_client(reason='fixture') is False
assert str(agent.client.base_url).rstrip('/') == config['base_url']
agent.close()
print('sealed')
""")
    try:
        result = await asyncio.to_thread(subprocess.run, [launch.args[0], "-I", "-S", str(harness), *launch.args[3:]],
                                         cwd=launch.home.name, env=launch.env, capture_output=True, text=True, timeout=20)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "sealed"
    finally:
        launch.close()


def test_source_selection_retains_previous_release_and_rejects_mixed_tree(tmp_path, monkeypatch):
    import hashlib
    releases = {'old': {'a': b'old-a', 'b': b'old-b'},
                'new': {'a': b'new-a', 'b': b'new-b'}}
    monkeypatch.setattr(bridge, 'SOURCE_VERSIONS', {
        version: {'hashes': {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}}
        for version, files in releases.items()
    })
    for version, files in releases.items():
        for name, data in files.items(): (tmp_path / name).write_bytes(data)
        assert bridge.source_version(tmp_path) == version
    (tmp_path / 'a').write_bytes(releases['old']['a'])
    with pytest.raises(ValueError, match='verified Hermes source'):
        bridge.source_version(tmp_path)
