"""Opt-in compatibility proof against a real, explicitly selected Pi release.

SLEEPTALK_TEST_PI=/absolute/path/to/pi uv run pytest tests/test_pi_release.py -q
Only the model's HTTP transport is faked; all Pi code is upstream code.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest
from aiohttp import web

from sleeptalk.adapter import (
    ControlAccepted, PermissionDecision, PermissionRequested, ResolvePermission,
    SessionIdentified, ToolEvent, UsageReported, typed_events,
)
from sleeptalk.agents.pi_rpc import PiRPCClient


@pytest.fixture
async def release(tmp_path):
    selected = os.environ.get("SLEEPTALK_TEST_PI")
    if not selected:
        pytest.skip("Set SLEEPTALK_TEST_PI to exercise an actual Pi release")
    binary = Path(selected).absolute()
    assert binary.is_file(), f"Pi release is missing: {binary}"
    node = shutil.which("node")
    assert node, "Node is required for the npm Pi release proof"
    async def provider(request):
        body = await request.json()
        with (tmp_path / "requests.jsonl").open("a") as output:
            output.write(json.dumps(body) + "\n")
        last_user = max(i for i, m in enumerate(body["messages"]) if m["role"] == "user")
        prompt = json.dumps(body["messages"][last_user]["content"])
        tool_done = any(m["role"] == "tool" for m in body["messages"][last_user + 1:])
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        async def send(delta, finish=None):
            chunk = {"id": "fixture", "object": "chat.completion.chunk", "created": 0,
                     "model": "fixture", "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
            if finish:
                chunk["usage"] = {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20}
            await response.write(("data: " + json.dumps(chunk) + "\n\n").encode())
        await send({"role": "assistant"})
        if "hang-until-abort" in prompt:
            await send({"content": "Waiting for cancellation. "})
            while request.transport and not request.transport.is_closing():
                await asyncio.sleep(.01)
            return response
        if "exercise-tool" in prompt and not tool_done:
            await send({"tool_calls": [{"index": 0, "id": "write-proof", "type": "function",
                                       "function": {"name": "write", "arguments": json.dumps({
                                           "path": "tool-proof.txt", "content": "approved"})}}]})
            await send({}, "tool_calls")
        else:
            await send({"content": "First sentence. "})
            await send({"content": "Second sentence."})
            await send({}, "stop")
        await response.write(b"data: [DONE]\n\n")
        return response
    app = web.Application()
    app.router.add_post("/v1/chat/completions", provider)
    runner = web.AppRunner(app, shutdown_timeout=1)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    agent = tmp_path / "home" / ".pi" / "agent"
    agent.mkdir(parents=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (agent / "models.json").write_text(json.dumps({"providers": {"sleeptalk-test": {
        "baseUrl": f"http://127.0.0.1:{port}/v1", "api": "openai-completions",
        "apiKey": "fixture-not-a-secret", "models": [{"id": "fixture", "name": "Fixture",
        "reasoning": False, "input": ["text"], "contextWindow": 32768, "maxTokens": 1024,
        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}}],
    }}}))
    # Keep a stable tool surface across releases; this is ordinary user config.
    (agent / "settings.json").write_text(json.dumps({"defaultTools": ["read", "write", "bash"],
                                                    "retry": {"enabled": False}}))
    for directory in (agent / "extensions", workspace / ".pi" / "extensions"):
        directory.mkdir(parents=True)
        (directory / "untrusted.js").write_text(
            'import { writeFileSync } from "node:fs";\n'
            f'writeFileSync({json.dumps(str(tmp_path / "extension-ran"))}, "bad");\n'
            'export default function () {}\n'
        )
    preload = Path(__file__).with_name("fixtures") / "pi_network_guard.cjs"
    env = {"HOME": str(tmp_path / "home"), "PI_CODING_AGENT_DIR": str(agent),
           "XDG_CONFIG_HOME": str(tmp_path / "home" / ".config"),
           "XDG_CACHE_HOME": str(tmp_path / "home" / ".cache"),
           "PATH": f"{Path(node).parent}:/usr/bin:/bin", "NO_COLOR": "1",
           "NODE_OPTIONS": f"--require={preload}",
           "SLEEPTALK_TEST_NETWORK_MARKER": str(tmp_path / "network-attempt"),
           "SLEEPTALK_TEST_PORT": str(port)}
    version = subprocess.run([str(binary), "--version"], env=env, cwd=workspace,
                             capture_output=True, text=True, check=True, timeout=15).stdout.strip()
    expected = os.environ.get("SLEEPTALK_TEST_PI_VERSION")
    if expected:
        assert version == expected
    def make(mode="deny"):
        return PiRPCClient(cwd=str(workspace), pi_bin=str(binary), provider="sleeptalk-test",
                           model="fixture", permission_mode=mode, env=env)
    try:
        yield make, workspace, tmp_path
    finally:
        await runner.cleanup()
    assert not (tmp_path / "network-attempt").exists()
    assert not (tmp_path / "extension-ran").exists()


async def collect(client, prompt, events):
    return [sentence async for sentence in client.respond_streaming(
        prompt, timeout=10, on_event=typed_events(events.append))]


@pytest.mark.asyncio
async def test_release_stream_usage_and_session_resume(release):
    make, _, root = release
    sessions = root / "sessions"
    events = []
    client = make()
    client.use_saved_session(sessions)
    try:
        assert await collect(client, "hello", events) == ["First sentence.", "Second sentence."]
        assert any(isinstance(e, UsageReported) for e in events)
        assert any(isinstance(e, SessionIdentified) for e in events)
        state = await client._rpc({"type": "get_state"})
        session_id = state["data"]["sessionId"]
    finally:
        await client.close()
    resumed = make()
    resumed.use_saved_session(sessions)
    try:
        await collect(resumed, "second turn", [])
        assert (await resumed._rpc({"type": "get_state"}))["data"]["sessionId"] == session_id
        requests = [json.loads(line) for line in (root / "requests.jsonl").read_text().splitlines()]
        assert any(m["role"] == "assistant" for m in requests[-1]["messages"])
    finally:
        await resumed.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,decision", [("deny", None), ("pending", PermissionDecision.DENY),
                                          ("pending", PermissionDecision.ALLOW_ONCE)])
async def test_release_tool_permissions(release, mode, decision):
    make, workspace, _ = release
    client, events = make(mode), []
    task = asyncio.create_task(collect(client, "exercise-tool", events))
    try:
        if decision is not None:
            async with asyncio.timeout(10):
                while not any(isinstance(e, PermissionRequested) for e in events):
                    if task.done():
                        await task
                        pytest.fail("Tool executed or settled without a permission request")
                    await asyncio.sleep(.01)
            assert not (workspace / "tool-proof.txt").exists()
            permission = next(e for e in events if isinstance(e, PermissionRequested))
            assert isinstance(await client.control(ResolvePermission(permission.request_id, decision)), ControlAccepted)
        await asyncio.wait_for(task, 15)
        assert (workspace / "tool-proof.txt").exists() == (decision is PermissionDecision.ALLOW_ONCE)
        assert any(isinstance(e, ToolEvent) for e in events)
        if mode == "deny":
            assert not any(isinstance(e, PermissionRequested) for e in events)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await client.close()


@pytest.mark.asyncio
async def test_release_abort_then_next_turn(release):
    make, _, root = release
    client, events = make(), []
    task = asyncio.create_task(collect(client, "hang-until-abort", events))
    try:
        async with asyncio.timeout(10):
            while not (root / "requests.jsonl").exists():
                if task.done():
                    await task
                await asyncio.sleep(.01)
        await client.abort()
        await asyncio.wait_for(task, 5)
        assert await collect(client, "after cancellation", []) == ["First sentence.", "Second sentence."]
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await client.close()
