"""Onboarding probes use synthetic adapters, never the operator's login."""

import asyncio
from contextlib import asynccontextmanager
import json
import sys

import pytest

from sleeptalk.agents import onboarding_cli as onboarding
from sleeptalk.config import AgentConfig


@pytest.fixture
def agent(tmp_path):
    return AgentConfig(type="claude_code", workspace_dir=str(tmp_path), permission_mode="pending")


@pytest.mark.asyncio
@pytest.mark.parametrize("logged_in,code,configuration,login", [
    (True, 0, "available", "unknown"),
    (False, 1, "unavailable", "unavailable"),
])
async def test_claude_login_status_is_not_live_auth(monkeypatch, agent, logged_in, code, configuration, login):
    calls = []
    monkeypatch.setattr(onboarding.importlib.util, "find_spec", lambda _: object())
    monkeypatch.setattr(onboarding, "_claude_binary", lambda: "/fake/bundled/claude")

    async def capture(argv, workspace, env):
        calls.append(argv)
        assert "DEEPGRAM_API_KEY" not in env
        assert env["ANTHROPIC_API_KEY"] == "agent-auth"
        if argv[-1] == "--help":
            return 0, "  status [options] Show authentication status" if "auth" in argv else "  auth Manage authentication"
        return code, json.dumps({"loggedIn": logged_in, "email": "private@example.test"})

    monkeypatch.setattr(onboarding, "_capture", capture)
    result = await onboarding.probe(agent, "claude_code", {"DEEPGRAM_API_KEY": "speech-key", "ANTHROPIC_API_KEY": "agent-auth"})
    assert result["status"] == "needs_input"
    assert result["login_configuration"] == configuration
    assert result["login"] == login
    assert "private@example.test" not in str(result)
    assert calls == [["/fake/bundled/claude", "--help"], ["/fake/bundled/claude", "auth", "--help"], ["/fake/bundled/claude", "auth", "status"]]


@pytest.mark.asyncio
@pytest.mark.parametrize("payload,code", [({}, 0), ({"loggedIn": "true"}, 0), ({"loggedIn": True}, 1), ({"loggedIn": False}, 0)])
async def test_claude_rejects_ambiguous_status(monkeypatch, agent, payload, code):
    monkeypatch.setattr(onboarding.importlib.util, "find_spec", lambda _: object())
    monkeypatch.setattr(onboarding, "_claude_binary", lambda: "fake")

    async def capture(argv, *_):
        if argv[-1] == "--help":
            return 0, "  status [options] Show authentication status" if "auth" in argv else "  auth Manage authentication"
        return code, json.dumps(payload)

    monkeypatch.setattr(onboarding, "_capture", capture)
    result = await onboarding.probe(agent, "claude_code", {})
    assert result["login"] == "unknown"
    assert result["status"] != "ready"


@pytest.mark.asyncio
@pytest.mark.parametrize("registration,mode", [("claude_code", ""), ("claude_code", "skip"), ("claude_code", "auto"), ("cursor_acp", ""), ("cursor_acp", "auto"), ("cursor", "pending")])
async def test_unsupported_permissions_do_not_launch(monkeypatch, agent, registration, mode):
    async def forbidden(*args, **kwargs):
        pytest.fail("Must not launch an adapter with unsupported approvals")

    monkeypatch.setattr(onboarding.asyncio, "create_subprocess_exec", forbidden)
    result = await onboarding.probe(agent.model_copy(update={"permission_mode": mode}), registration, {})
    assert result["permissions"] == "unavailable"
    assert result["status"] == "needs_input"


@pytest.mark.asyncio
async def test_missing_workspace_does_not_launch(agent):
    result = await onboarding.probe(agent.model_copy(update={"workspace_dir": "/does-not-exist-onboarding-test"}), "claude_code", {})
    assert result["workspace"] == "unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize("output,code,configured", [("", 0, "unknown"), ("API key configured", 0, "unknown"), ("Not logged in", 1, "unavailable"), ("Logged in as someone", 1, "unknown")])
async def test_cursor_requires_recognized_status_before_auth(monkeypatch, agent, output, code, configured):
    monkeypatch.setattr(onboarding, "_cursor_binary", lambda _: "fake")

    async def capture(*_):
        return code, output

    monkeypatch.setattr(onboarding, "_capture", capture)
    result = await onboarding.probe(agent, "cursor_acp", {})
    assert result["login_configuration"] == configured
    assert result["status"] != "ready"


class _Input:
    def __init__(self):
        self.requests = []

    def write(self, data):
        self.requests.append(json.loads(data))

    async def drain(self):
        pass


class _Output:
    def __init__(self, messages):
        self.messages = iter(messages)

    async def readline(self):
        return next(self.messages, b"")


@pytest.mark.asyncio
@pytest.mark.parametrize("auth_error", [False, True])
async def test_cursor_acp_auth_without_creating_session(monkeypatch, agent, auth_error):
    monkeypatch.setattr(onboarding, "_cursor_binary", lambda _: "fake")

    async def capture(*_):
        return 0, "\x1b[32mLogged in as\x1b[0m private@example.test"

    monkeypatch.setattr(onboarding, "_capture", capture)
    proc = type("Process", (), {})()
    proc.stdin = _Input()
    proc.stdout = _Output([
        json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": 1, "authMethods": [{"id": "cursor_login"}]}}).encode() + b"\n",
        json.dumps({"jsonrpc": "2.0", "id": 2, **({"error": {"message": "private-token"}} if auth_error else {"result": {}})}).encode() + b"\n",
    ])
    closed = []

    @asynccontextmanager
    async def process(*_):
        try:
            yield proc
        finally:
            closed.append(True)

    monkeypatch.setattr(onboarding, "_process", process)
    result = await onboarding.probe(agent, "cursor_acp", {})
    assert [request["method"] for request in proc.stdin.requests] == ["initialize", "authenticate"]
    assert result["login"] == ("unknown" if auth_error else "available")
    assert result["workspace"] == "unknown"
    assert result["status"] == "needs_input"
    assert "private" not in json.dumps({key: value for key, value in result.items() if key != "next_action"})
    assert "private-token" not in str(result)
    assert closed == [True]


@pytest.mark.asyncio
async def test_rpc_never_services_filesystem_or_permission_request():
    proc = type("Process", (), {})()
    proc.stdin = _Input()
    proc.stdout = _Output([b'{"jsonrpc":"2.0","id":17,"method":"fs/read_text_file","params":{"path":"secret"}}\n'])
    with pytest.raises(onboarding._ProbeError):
        await onboarding._rpc(proc, 1, "initialize", {})
    assert len(proc.stdin.requests) == 1


@pytest.mark.asyncio
async def test_capture_strips_voice_keys_and_bounds_output(tmp_path):
    code, output = await onboarding._capture([
        sys.executable, "-c", "import os; print('DEEPGRAM_API_KEY' in os.environ); print(os.environ['ANTHROPIC_API_KEY'])",
    ], tmp_path, {"DEEPGRAM_API_KEY": "speech-key", "ANTHROPIC_API_KEY": "agent-key"})
    assert code == 0
    assert output.splitlines() == ["False", "agent-key"]
    with pytest.raises(onboarding._ProbeError):
        async with asyncio.timeout(3):
            await onboarding._capture([sys.executable, "-c", "import os; os.write(1, b'x' * 200000);"], tmp_path, {})


@pytest.mark.asyncio
async def test_timeout_reaps_probe_process(monkeypatch, tmp_path):
    pid_file = tmp_path / "pid"
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(.2):
            await onboarding._capture([sys.executable, "-c", "import os, pathlib, time; pathlib.Path('pid').write_text(str(os.getpid())); time.sleep(60)"], tmp_path, {})
    pid = int(pid_file.read_text())
    import os
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@pytest.mark.asyncio
async def test_failure_message_does_not_echo_exception(monkeypatch, agent):
    async def fail(*_):
        raise RuntimeError("SENTINEL credential /private/home")

    monkeypatch.setattr(onboarding, "_claude", fail)
    result = await onboarding.probe(agent, "claude_code", {})
    assert "SENTINEL" not in str(result)
    assert "/private/home" not in str(result)


@pytest.mark.asyncio
async def test_claude_without_auth_subcommand_never_receives_prompt(monkeypatch, agent):
    monkeypatch.setattr(onboarding.importlib.util, "find_spec", lambda _: object())
    monkeypatch.setattr(onboarding, "_claude_binary", lambda: "fake")
    calls = []

    async def capture(argv, *_):
        calls.append(argv)
        return 0, "Usage: claude [options] [prompt]"

    monkeypatch.setattr(onboarding, "_capture", capture)
    result = await onboarding.probe(agent, "claude_code", {})
    assert calls == [["fake", "--help"]]
    assert result["login"] == "unknown"


@pytest.mark.parametrize("login", ["available", "unavailable", "unknown"])
async def test_claude_first_party_key_requires_live_auth_and_isolated_initialize(monkeypatch, agent, login):
    monkeypatch.setattr(onboarding.importlib.util, "find_spec", lambda _: object())
    monkeypatch.setattr(onboarding, "_claude_binary", lambda: "fake")
    async def capture(argv, *_):
        if argv[-1] == "--help":
            return 0, "  auth Manage authentication\n  status [options] Authentication status"
        return 0, json.dumps({"loggedIn": True, "apiProvider": "firstParty"})
    calls = []
    async def auth(key):
        assert key == "fixture-key"
        calls.append("auth")
        return login
    async def workspace(config, cwd, env):
        assert str(cwd) == agent.workspace_dir
        calls.append("initialize")
    monkeypatch.setattr(onboarding, "_capture", capture)
    monkeypatch.setattr(onboarding, "_anthropic_api_login", auth)
    monkeypatch.setattr(onboarding, "_claude_workspace", workspace)
    result = await onboarding.probe(agent, "claude_code", {"ANTHROPIC_API_KEY": "fixture-key"})
    assert result["status"] == ("ready" if login == "available" else "needs_input")
    assert calls == (["auth", "initialize"] if login == "available" else ["auth"])


async def test_claude_workspace_initialization_has_no_tools_hooks_mcp_or_prompt(monkeypatch, agent):
    claude_agent_sdk = pytest.importorskip("claude_agent_sdk")
    seen = []
    class Client:
        def __init__(self, *, options):
            assert options.cwd == agent.workspace_dir
            assert options.setting_sources == [] and options.strict_mcp_config
            assert options.tools == [] and options.mcp_servers == {} and options.skills == []
            assert json.loads(options.settings) == {"disableAllHooks": True}
            assert options.extra_args == {"no-session-persistence": None}
            self.options = options
        async def __aenter__(self):
            seen.append("initialize")
            assert (await self.options.can_use_tool("Bash", {}, None)).behavior == "deny"
            return self
        async def get_server_info(self):
            return {"commands": []}
        async def __aexit__(self, *args):
            seen.append("close")
    monkeypatch.setattr(claude_agent_sdk, "ClaudeSDKClient", Client)
    from pathlib import Path
    await onboarding._claude_workspace(agent, Path(agent.workspace_dir), {})
    assert seen == ["initialize", "close"]


@pytest.mark.parametrize("status,body,expected", [(200, b'{"data":[{"type":"model","id":"fixture"}]}', "available"),
    (401, b"private-provider-error", "unavailable"), (403, b"private-provider-error", "unavailable"),
    (302, b"", "unknown"), (429, b"", "unknown"), (200, b'{"data":[]}', "unknown"),
    (200, b"x" * 65537, "unknown")])
async def test_anthropic_nonwork_check_is_bounded_and_never_follows_redirects(monkeypatch, status, body, expected):
    import aiohttp
    class Response:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def iter_chunked(self, size):
            # Fragmented network bodies must be read to EOF, not parsed from
            # whichever bytes happen to be available on the first read.
            for offset in range(0, len(body), 7):
                yield body[offset:offset + 7]
    response = Response()
    response.status = status
    response.content = response
    class Session:
        def __init__(self, *, timeout, trust_env):
            assert timeout.total == 8 and trust_env is False
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def get(self, url, *, params, headers, allow_redirects):
            assert url == "https://api.anthropic.com/v1/models"
            assert params == {"limit": "1"} and allow_redirects is False
            assert headers["x-api-key"] == "fixture-key"
            return response
    monkeypatch.setattr(aiohttp, "ClientSession", Session)
    assert await onboarding._anthropic_api_login("fixture-key") == expected
