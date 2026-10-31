"""Bridge Lite: command and HTTP agents that anyone can write."""

from __future__ import annotations

import asyncio
import json
import sys
import textwrap
from pathlib import Path

import pytest
from pydantic import ValidationError

from sleeptalk.adapter import AssistantDelta, SupportLevel, ToolEvent, typed_events
from sleeptalk.agents.bridge_lite import (
    BridgeLiteError,
    CommandAgent,
    CommandAgentOptions,
    HttpAgent,
    HttpAgentOptions,
    command_environment,
    manifest_from_caps,
)
from sleeptalk.conformance.runner import run_adapter_test

TEMPLATES = Path(__file__).resolve().parents[1] / "sleeptalk" / "templates" / "byo"


def _command(tmp_path: Path, script: str, **options) -> CommandAgent:
    path = tmp_path / "agent.py"
    path.write_text(textwrap.dedent(script), encoding="utf-8")
    return CommandAgent(
        CommandAgentOptions(command=[sys.executable, str(path)], **options),
        env={"PATH": "/usr/bin:/bin"},
        cwd=str(tmp_path),
        name="test-agent",
    )


async def _collect(agent, text: str, events: list | None = None) -> list[str]:
    sink = typed_events(events.append) if events is not None else None
    return [sentence async for sentence in agent.respond_streaming(text, on_event=sink)]


# ---- options and environment ----------------------------------------------


def test_command_requires_a_program() -> None:
    with pytest.raises(ValidationError):
        CommandAgentOptions(command=[])
    with pytest.raises(ValidationError):
        CommandAgentOptions(command=[" "])
    with pytest.raises(ValidationError):
        CommandAgentOptions(command=["x"], unknown=True)


@pytest.mark.parametrize(
    ("url", "ok"),
    [
        ("https://agent.example.com/turn", True),
        ("http://127.0.0.1:8765/turn", True),
        ("http://localhost:8765/turn", True),
        ("http://agent.example.com/turn", False),
        ("https://user:pass@agent.example.com/turn", False),
        ("ftp://agent.example.com", False),
    ],
)
def test_http_url_rules(url: str, ok: bool) -> None:
    if ok:
        HttpAgentOptions(url=url)
    else:
        with pytest.raises(ValidationError):
            HttpAgentOptions(url=url)


def test_http_headers_cannot_hold_secrets() -> None:
    HttpAgentOptions(url="https://a.example/turn", headers={"X-Team": "voice"})
    with pytest.raises(ValidationError, match="token_env"):
        HttpAgentOptions(url="https://a.example/turn", headers={"Authorization": "Bearer nope"})


def test_command_environment_is_an_allowlist() -> None:
    env = {
        "PATH": "/bin",
        "HOME": "/home/me",
        "LC_ALL": "C",
        "OPENAI_API_KEY": "sk-agent",
        "GITHUB_TOKEN": "ghp-unrelated",
        "DEEPGRAM_API_KEY": "voice-secret",
    }
    narrow = command_environment(CommandAgentOptions(command=["x"], env=["OPENAI_API_KEY", "DEEPGRAM_API_KEY"]), env)
    assert narrow == {"PATH": "/bin", "HOME": "/home/me", "LC_ALL": "C", "OPENAI_API_KEY": "sk-agent"}
    wide = command_environment(CommandAgentOptions(command=["x"], inherit_env=True), env)
    assert "GITHUB_TOKEN" in wide
    assert "DEEPGRAM_API_KEY" not in wide


def test_manifest_accepts_both_key_styles_and_transport_limits() -> None:
    manifest = manifest_from_caps(
        {"toolLifecycle": {"level": "supported"}, "usage": "supported", "permissions": True},
        unavailable={"permissions": "no mid-turn channel"},
    )
    assert manifest.tool_lifecycle.level is SupportLevel.SUPPORTED
    assert manifest.usage.level is SupportLevel.SUPPORTED
    assert manifest.permissions.level is SupportLevel.UNAVAILABLE
    assert manifest.permissions.reason == "no mid-turn channel"
    assert manifest.steering.level is SupportLevel.UNSUPPORTED
    assert manifest.voice_sessions.level is SupportLevel.UNAVAILABLE


# ---- command agents -------------------------------------------------------


@pytest.mark.parametrize("template", ["command-python/agent.py"])
async def test_python_command_template_passes_fixture_conformance(template: str) -> None:
    def factory() -> CommandAgent:
        return CommandAgent(
            CommandAgentOptions(command=[sys.executable, str(TEMPLATES / template)]),
            env={"PATH": "/usr/bin:/bin"},
            name="template",
        )

    report = await run_adapter_test(factory, adapter="command", mode="fixture")
    assert not report.failed, report.format_text()
    statuses = {check.id: check.status for check in report.checks}
    for check in ("stream.sentence", "abort.mid_stream", "abort.settles", "tool.lifecycle", "permissions", "clarification", "steering", "usage.reported"):
        assert statuses[check] == "pass", report.format_text()


@pytest.mark.skipif(__import__("shutil").which("node") is None, reason="node is required")
async def test_node_command_template_passes_fixture_conformance() -> None:
    def factory() -> CommandAgent:
        return CommandAgent(
            CommandAgentOptions(command=["node", str(TEMPLATES / "command-node" / "agent.mjs")]),
            env={"PATH": __import__("os").environ["PATH"]},
            name="template-node",
        )

    report = await run_adapter_test(factory, adapter="command", mode="fixture")
    assert not report.failed, report.format_text()


MINIMAL = """
    import json, sys
    for line in sys.stdin:
        frame = json.loads(line)
        if frame["type"] == "turn.prompt":
            print("debug output that is not a frame", flush=True)
            print(json.dumps({"type": "assistant.delta", "text": "Hi there. "}), flush=True)
            print(json.dumps({"type": "assistant.delta", "text": "Bye now."}), flush=True)
            print(json.dumps({"type": "turn.completed"}), flush=True)
"""


async def test_minimal_agent_without_hello_or_turn_ids(tmp_path: Path) -> None:
    agent = _command(tmp_path, MINIMAL, hello_timeout_s=0.1)
    events: list[object] = []
    try:
        await agent.warmup()
        assert agent.agent_capabilities.tool_lifecycle.level is SupportLevel.UNSUPPORTED
        assert await _collect(agent, "hello", events) == ["Hi there.", "Bye now."]
        assert any(isinstance(event, AssistantDelta) for event in events)
        assert await agent.respond("again") == "Hi there. Bye now."
    finally:
        await agent.close()


IGNORES_ABORT = """
    import json, sys, time
    for line in sys.stdin:
        frame = json.loads(line)
        if frame["type"] == "turn.prompt":
            turn = frame["turnId"]
            if frame["text"] == "long":
                for n in range(100):
                    print(json.dumps({"type": "assistant.delta", "turnId": turn, "text": f"Number {n}. "}), flush=True)
                    time.sleep(0.05)
            print(json.dumps({"type": "assistant.delta", "turnId": turn, "text": "Done."}), flush=True)
            print(json.dumps({"type": "turn.completed", "turnId": turn}), flush=True)
"""


async def test_abort_stops_speech_and_restarts_an_agent_that_ignores_it(tmp_path: Path) -> None:
    agent = _command(tmp_path, IGNORES_ABORT, hello_timeout_s=0, abort_grace_s=0.3)
    try:
        spoken: list[str] = []
        async for sentence in agent.respond_streaming("long"):
            spoken.append(sentence)
            await agent.abort()
        assert len(spoken) == 1
        first_pid = agent._proc.pid if agent._proc else None
        assert await asyncio.wait_for(_collect(agent, "short"), timeout=5) == ["Done."]
        assert agent._proc is not None and agent._proc.pid != first_pid
    finally:
        await agent.close()


async def test_idle_abort_is_safe(tmp_path: Path) -> None:
    agent = _command(tmp_path, MINIMAL, hello_timeout_s=0)
    await agent.abort()
    await agent.close()
    await agent.abort()


async def test_agent_exit_mid_turn_names_the_env_allowlist(tmp_path: Path) -> None:
    # Stdout closes before the error is written and the process exits: the
    # order that raced on slow CI runners. The message must still name both.
    crash = """
        import os, sys, time
        sys.stdin.readline()
        sys.stdin.readline()
        sys.stdout.flush()
        os.close(1)
        time.sleep(0.3)
        print("KeyError: OPENAI_API_KEY", file=sys.stderr, flush=True)
        sys.exit(3)
    """
    agent = _command(tmp_path, crash, hello_timeout_s=0)
    try:
        with pytest.raises(BridgeLiteError) as caught:
            await _collect(agent, "hello")
        message = str(caught.value)
        assert "exit code 3" in message
        assert "OPENAI_API_KEY" in message
        assert "[agent.options].env" in message
    finally:
        await agent.close()


async def test_error_frame_fails_the_turn(tmp_path: Path) -> None:
    script = """
        import json, sys
        for line in sys.stdin:
            frame = json.loads(line)
            if frame["type"] == "turn.prompt":
                print(json.dumps({"type": "error", "code": "agent", "message": "model quota exceeded"}), flush=True)
    """
    agent = _command(tmp_path, script, hello_timeout_s=0)
    try:
        with pytest.raises(BridgeLiteError, match="quota"):
            await _collect(agent, "hello")
    finally:
        await agent.close()


async def test_unadvertised_permission_request_is_ignored(tmp_path: Path) -> None:
    script = """
        import json, sys
        for line in sys.stdin:
            frame = json.loads(line)
            if frame["type"] == "turn.prompt":
                print(json.dumps({"type": "permission.requested", "requestId": "p1", "prompt": "rm?", "options": ["deny"]}), flush=True)
                print(json.dumps({"type": "tool", "name": "bash", "phase": "started"}), flush=True)
                print(json.dumps({"type": "turn.completed", "text": "Finished."}), flush=True)
    """
    agent = _command(tmp_path, script, hello_timeout_s=0)
    events: list[object] = []
    try:
        assert await _collect(agent, "go", events) == ["Finished."]
        kinds = {type(event).__name__ for event in events}
        assert "PermissionRequested" not in kinds
        assert any(isinstance(event, ToolEvent) for event in events)
    finally:
        await agent.close()


async def test_missing_command_is_actionable(tmp_path: Path) -> None:
    agent = CommandAgent(CommandAgentOptions(command=["definitely-not-an-agent-xyz"]), env={"PATH": "/usr/bin"}, cwd=str(tmp_path))
    with pytest.raises(BridgeLiteError, match="command not found"):
        await agent.warmup()
    await agent.close()


# ---- HTTP agents ----------------------------------------------------------


async def _serve(handler):
    from aiohttp import web

    app = web.Application()
    app.router.add_route("*", "/turn", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    return runner, f"http://127.0.0.1:{port}/turn"


def _http_agent(url: str, **options) -> HttpAgent:
    return HttpAgent(HttpAgentOptions(url=url, **options), env={"AGENT_TOKEN": "secret-token"}, name="http-test")


@pytest.mark.parametrize("style", ["ndjson", "sse", "json", "text"])
async def test_http_reply_formats(style: str) -> None:
    from aiohttp import web

    seen: dict[str, object] = {}

    async def handler(request: web.Request) -> web.StreamResponse:
        if request.method == "GET":
            return web.json_response({"type": "hello.ok", "capabilities": {"toolLifecycle": "supported", "permissions": "supported"}})
        seen["body"] = await request.json()
        seen["auth"] = request.headers.get("Authorization")
        if style == "json":
            return web.json_response({"text": "Hello there. How can I help?"})
        content_type = {"ndjson": "application/x-ndjson", "sse": "text/event-stream", "text": "text/plain"}[style]
        response = web.StreamResponse(headers={"Content-Type": content_type})
        await response.prepare(request)
        chunks = ["Hello there. ", "How can I help?"]
        for chunk in chunks:
            if style == "ndjson":
                await response.write((json.dumps({"type": "assistant.delta", "text": chunk}) + "\n").encode())
            elif style == "sse":
                await response.write(f"data: {json.dumps({'type': 'assistant.delta', 'text': chunk})}\n\n".encode())
            else:
                await response.write(chunk.encode())
        if style == "sse":
            await response.write(b"data: [DONE]\n\n")
        await response.write_eof()
        return response

    runner, url = await _serve(handler)
    agent = _http_agent(url, token_env="AGENT_TOKEN")
    try:
        await agent.warmup()
        assert agent.agent_capabilities.tool_lifecycle.level is SupportLevel.SUPPORTED
        assert agent.agent_capabilities.permissions.level is SupportLevel.UNAVAILABLE
        assert await _collect(agent, "hi") == ["Hello there.", "How can I help?"]
        body = seen["body"]
        assert isinstance(body, dict) and body["type"] == "turn.prompt" and body["text"] == "hi"
        assert body["sessionId"] == agent.session_id
        assert seen["auth"] == "Bearer secret-token"
    finally:
        await agent.close()
        await runner.cleanup()


async def test_http_rejected_credential_is_actionable() -> None:
    from aiohttp import web

    async def handler(request: web.Request) -> web.Response:
        return web.Response(status=401)

    runner, url = await _serve(handler)
    agent = _http_agent(url)
    try:
        with pytest.raises(BridgeLiteError, match="token_env"):
            await _collect(agent, "hi")
    finally:
        await agent.close()
        await runner.cleanup()


async def test_http_abort_closes_the_request() -> None:
    from aiohttp import web

    closed = asyncio.Event()

    async def handler(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(headers={"Content-Type": "application/x-ndjson"})
        await response.prepare(request)
        try:
            for n in range(200):
                await response.write((json.dumps({"type": "assistant.delta", "text": f"Number {n}. "}) + "\n").encode())
                await asyncio.sleep(0.02)
        except (ConnectionResetError, asyncio.CancelledError):
            closed.set()
            raise
        return response

    runner, url = await _serve(handler)
    agent = _http_agent(url)
    try:
        spoken = []
        async for sentence in agent.respond_streaming("count"):
            spoken.append(sentence)
            await agent.abort()
        assert len(spoken) == 1
        await asyncio.wait_for(closed.wait(), timeout=3)
    finally:
        await agent.close()
        await runner.cleanup()


async def test_http_python_template_passes_fixture_conformance(unused_tcp_port: int) -> None:
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        str(TEMPLATES / "http-python" / "server.py"),
        env={"PATH": "/usr/bin:/bin", "PORT": str(unused_tcp_port)},
        stdout=asyncio.subprocess.PIPE,
    )
    try:
        assert proc.stdout is not None
        await asyncio.wait_for(proc.stdout.readline(), timeout=10)
        url = f"http://127.0.0.1:{unused_tcp_port}/sleeptalk/turn"
        report = await run_adapter_test(lambda: _http_agent(url), adapter="http", mode="fixture")
        assert not report.failed, report.format_text()
        assert {c.id: c.status for c in report.checks}["tool.lifecycle"] == "pass"
    finally:
        proc.terminate()
        await proc.wait()


# ---- setup mistakes -------------------------------------------------------


def test_setup_hint_catches_acp_agents_run_as_plain_programs() -> None:
    from sleeptalk.agents.bridge_lite import command_setup_hint

    hint = command_setup_hint(["opencode"], "lite")
    assert 'protocol = "acp"' in hint and '["opencode", "acp"]' in hint
    assert "without `acp`" in command_setup_hint(["/usr/local/bin/opencode"], "acp")
    assert command_setup_hint(["opencode", "acp"], "acp") == ""
    assert "cursor_acp" in command_setup_hint(["agent"], "lite")
    assert command_setup_hint(["node", "agent.mjs"], "lite") == ""
    assert command_setup_hint([sys.executable, "agent.py"], "lite") == ""


async def test_program_that_never_speaks_the_protocol_fails_fast(tmp_path: Path) -> None:
    silent = """
        import sys, time
        for line in sys.stdin:
            time.sleep(0.01)   # reads prompts, answers nothing (like an interactive app)
    """
    agent = _command(tmp_path, silent, hello_timeout_s=0, first_frame_timeout_s=0.5, idle_timeout_s=120)
    try:
        started = asyncio.get_running_loop().time()
        with pytest.raises(BridgeLiteError, match="has not sent a single Bridge Lite frame") as caught:
            await _collect(agent, "hello")
        assert asyncio.get_running_loop().time() - started < 5
        assert 'protocol = "acp"' in str(caught.value)
    finally:
        await agent.close()


async def test_first_frame_timeout_only_applies_before_any_frame(tmp_path: Path) -> None:
    script = """
        import json, sys, time
        print(json.dumps({"type": "hello.ok", "capabilities": {}}), flush=True)
        for line in sys.stdin:
            frame = json.loads(line)
            if frame["type"] == "turn.prompt":
                time.sleep(1.0)
                print(json.dumps({"type": "turn.completed", "text": "Slow but fine."}), flush=True)
    """
    agent = _command(tmp_path, script, hello_timeout_s=2, first_frame_timeout_s=0.3)
    try:
        await agent.warmup()
        assert await _collect(agent, "go") == ["Slow but fine."]
    finally:
        await agent.close()


async def test_adapter_test_reports_the_setup_hint(tmp_path: Path) -> None:
    import os
    import stat

    fake = tmp_path / "opencode"
    fake.write_text(f"#!{sys.executable}\nimport sys\nfor _ in sys.stdin: pass\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)

    def factory() -> CommandAgent:
        return CommandAgent(CommandAgentOptions(command=[str(fake)], hello_timeout_s=0), env=dict(os.environ), cwd=str(tmp_path))

    report = await run_adapter_test(factory, adapter="command", mode="offline")
    hints = [check for check in report.checks if check.id == "setup.hint"]
    assert hints and hints[0].status == "warn" and "ACP agent" in hints[0].detail
