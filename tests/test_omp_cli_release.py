"""Opt-in compatibility proof against a real, explicitly selected OMP CLI release.

SLEEPTALK_TEST_OMP_CLI=/absolute/path/to/omp uv run pytest tests/test_omp_cli_release.py -q
Only the model's HTTP transport is faked; all OMP code is upstream code.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import subprocess
import shutil

import pytest
from aiohttp import web

from sleeptalk.adapter import (
    SessionIdentified, ToolEvent, UsageReported, typed_events,
    WorkerEvent, WorkerPhase,
)
from sleeptalk.agents.pi_rpc import OmpRPCClient


@pytest.fixture
async def release(tmp_path):
    selected = os.environ.get("SLEEPTALK_TEST_OMP_CLI")
    if not selected:
        pytest.skip("Set SLEEPTALK_TEST_OMP_CLI to exercise an actual OMP CLI release")
    binary = Path(selected).absolute()
    assert binary.is_file(), f"OMP CLI release is missing: {binary}"
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
        if "exercise-worker" in prompt and not tool_done:
            await send({"tool_calls": [{"index": 0, "id": "task-proof", "type": "function",
                                       "function": {"name": "task", "arguments": json.dumps({
                                           "agent": "task", "task": "child-fixture", "name": "Fixture worker",
                                           "solutionSpace": "Reply without tools", "isolated": False})}}]})
            await send({}, "tool_calls")
        elif "child-fixture" in prompt:
            await send({"content": "Private child answer."})
            await send({}, "stop")
        elif "exercise-tool" in prompt and not tool_done:
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
    agent = tmp_path / "home" / ".omp" / "agent"
    agent.mkdir(parents=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (agent / "models.yml").write_text(json.dumps({"providers": {"sleeptalk-test": {
        "baseUrl": f"http://127.0.0.1:{port}/v1", "api": "openai-completions",
        "apiKey": "fixture-not-a-secret", "models": [{"id": "fixture", "name": "Fixture",
        "reasoning": False, "input": ["text"], "contextWindow": 32768, "maxTokens": 1024,
        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}}],
    }}}))
    (agent / "config.yml").write_text(json.dumps({"startup": {"checkUpdate": False},
        "retry": {"enabled": False}, "memory": {"backend": "off"},
        "memories": {"enabled": False}, "secrets": {"enabled": False},
        "task": {"batch": False, "isolation": {"enabled": False}, "prewalk": False},
        "async": {"enabled": False}, "modelRoles": {"task": "sleeptalk-test/fixture"}}))
    env = {"HOME": str(tmp_path / "home"), "PI_CODING_AGENT_DIR": str(agent),
           "XDG_CONFIG_HOME": str(tmp_path / "home" / ".config"),
           "XDG_CACHE_HOME": str(tmp_path / "home" / ".cache"),
           "PATH": "/usr/bin:/bin", "NO_COLOR": "1"}
    version = subprocess.run([str(binary), "--version"], env=env, cwd=workspace,
                             capture_output=True, text=True, check=True, timeout=15).stdout.strip()
    expected = os.environ.get("SLEEPTALK_TEST_OMP_CLI_VERSION")
    if expected:
        assert version.removeprefix("omp/") == expected
    def make():
        return OmpRPCClient(cwd=str(workspace), pi_bin=str(binary), provider="sleeptalk-test",
                           model="fixture", permission_mode="auto", env=env,
                           extra_args=["--no-extensions", "--no-skills", "--no-lsp", "--tools", "read,write,bash,task"])
    try:
        yield make, workspace, tmp_path
    finally:
        await runner.cleanup()
        shutil.rmtree(tmp_path / "home")


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
async def test_release_auto_tool(release):
    make, workspace, _ = release
    client, events = make(), []
    try:
        assert await collect(client, "exercise-tool", events) == ["First sentence.", "Second sentence."]
        assert (workspace / "tool-proof.txt").read_text() == "approved"
        assert any(isinstance(e, ToolEvent) for e in events)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_release_abort_then_next_turn(release):
    make, _, root = release
    client, events = make(), []
    task = asyncio.create_task(collect(client, "hang-until-abort", events))
    try:
        async with asyncio.timeout(30):
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


@pytest.mark.asyncio
async def test_release_worker_lifecycle_keeps_child_text_out_of_speech(release):
    if os.environ.get("SLEEPTALK_TEST_OMP_CLI_VERSION") != "18.10.2":
        pytest.skip("Real task schema proof is specific to 18.10.2; older worker mapping uses contract fixtures")
    make, _, root = release
    client, events = make(), []
    try:
        assert await collect(client, "exercise-worker", events) == ["First sentence.", "Second sentence."]
        workers = [e for e in events if isinstance(e, WorkerEvent)]
        assert any(e.phase is WorkerPhase.STARTED for e in workers), events
        assert any(e.phase is WorkerPhase.COMPLETED for e in workers), workers
        requests = [json.loads(line) for line in (root / "requests.jsonl").read_text().splitlines()]
        assert any("child-fixture" in json.dumps(r["messages"]) for r in requests)
    finally:
        await client.close()
