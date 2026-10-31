"""Opt-in full turns using an exact upstream OMP SDK and synthetic HTTP provider."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import shutil

import pytest

from sleeptalk.adapter import (
    ControlAccepted, PermissionDecision, PermissionRequested, ResolvePermission,
    SessionIdentified, ToolEvent, UsageReported, typed_events,
)
from sleeptalk.agents import omp_sdk
from sleeptalk.agents.pi_rpc import OmpRPCClient, PiRPCError
from tests.test_omp_sdk import ambient_markers, real_sdk


@pytest.fixture
def release(monkeypatch, tmp_path):
    binary, bun = real_sdk()
    original_which = shutil.which
    monkeypatch.setattr(shutil, "which", lambda name, **kwargs: bun if name == "bun" else original_which(name, **kwargs))
    prepare = omp_sdk.prepare
    def fixture_launch(*args, **kwargs):
        launch = prepare(*args, **kwargs)
        fixture = Path(__file__).parent / "fixtures/omp_sdk_turn.mjs"
        launch.args = [*launch.args[:4], str(fixture), *launch.args[5:], launch.args[4]]
        return launch
    monkeypatch.setattr(omp_sdk, "prepare", fixture_launch)
    marker = ambient_markers(tmp_path)
    def make(mode="deny"):
        return OmpRPCClient(cwd=str(tmp_path), pi_bin=binary, provider="openai", model="gpt-4.1",
                            permission_mode=mode, no_session=False,
                            env={"PATH": "/usr/bin:/bin", "OPENAI_API_KEY": "synthetic-key"})
    yield make, tmp_path
    assert not marker.exists()
    assert not (tmp_path / "fixture-failure").exists()


async def collect(client, prompt, events):
    return [sentence async for sentence in client.respond_streaming(prompt, timeout=10, on_event=typed_events(events.append))]


@pytest.mark.asyncio
async def test_sdk_streams_before_completion_and_keeps_conversation(release):
    make, workspace = release
    client, events = make(), []
    try:
        await client._ensure_started()
        stream = client.respond_streaming("stream-before-finish", timeout=10, on_event=typed_events(events.append))
        assert await asyncio.wait_for(anext(stream), 4) == "First sentence."
        (workspace / "release-stream").touch()
        assert [part async for part in stream] == ["Second sentence."]
        assert any(isinstance(e, UsageReported) and e.input_tokens == 12 and e.output_tokens == 8 for e in events)
        identity = next(e for e in events if isinstance(e, SessionIdentified))
        assert not identity.resumable
        assert await collect(client, "second turn", []) == ["First sentence.", "Second sentence."]
        requests = [json.loads(line) for line in (workspace / "requests.jsonl").read_text().splitlines()]
        assert (requests[-1].get("previous_response_id") == "resp_1"
                or any(m.get("role") == "assistant" for m in requests[-1]["input"]))
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,decision", [("deny", None), ("pending", PermissionDecision.DENY),
                                          ("pending", PermissionDecision.ALLOW_ONCE)])
async def test_sdk_turn_guards_the_actual_write_tool(release, mode, decision):
    make, workspace = release
    client, events = make(mode), []
    task = asyncio.create_task(collect(client, "exercise-tool", events))
    try:
        if decision is not None:
            async with asyncio.timeout(10):
                while not any(isinstance(e, PermissionRequested) for e in events):
                    if task.done():
                        await task
                        pytest.fail("Tool turn completed without permission")
                    await asyncio.sleep(.01)
            assert not (workspace / "tool-proof.txt").exists()
            permission = next(e for e in events if isinstance(e, PermissionRequested))
            assert isinstance(await client.control(ResolvePermission(permission.request_id, decision)), ControlAccepted)
        assert await asyncio.wait_for(task, 15) == ["First sentence.", "Second sentence."]
        assert (workspace / "tool-proof.txt").exists() == (decision is PermissionDecision.ALLOW_ONCE)
        assert any(isinstance(e, ToolEvent) for e in events)
        if mode == "deny":
            assert not any(isinstance(e, PermissionRequested) for e in events)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("prompt", ["hang-until-abort", "exercise-tool"])
async def test_sdk_abort_stream_or_pending_permission_then_next_turn(release, prompt):
    make, workspace = release
    client, events = make("pending"), []
    task = asyncio.create_task(collect(client, prompt, events))
    try:
        async with asyncio.timeout(10):
            while not ((workspace / "model-hanging").exists() or any(isinstance(e, PermissionRequested) for e in events)):
                if task.done():
                    await task
                    pytest.fail("Turn settled before cancellation")
                await asyncio.sleep(.01)
        await client.abort()
        await asyncio.wait_for(task, 5)
        assert not (workspace / "tool-proof.txt").exists()
        assert await collect(client, "after cancellation", []) == ["First sentence.", "Second sentence."]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await client.close()


@pytest.mark.asyncio
async def test_sdk_provider_failure_is_reported_and_next_turn_can_run(release):
    make, _ = release
    client = make()
    try:
        with pytest.raises(PiRPCError):
            await collect(client, "reject-provider-request", [])
        assert await collect(client, "after rejection", []) == ["First sentence.", "Second sentence."]
    finally:
        await client.close()
