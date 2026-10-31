from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from sleeptalk.adapter import (
    SupportLevel,
    WorkerEvidence,
    WorkerEvent,
    WorkerPhase,
    adapter_capabilities,
    event_to_legacy,
    typed_events,
)
from sleeptalk.agents.omp_workers import (
    is_worker_tool_name,
    worker_from_lifecycle,
    worker_from_progress,
    worker_from_task_tool,
)
from sleeptalk.agents.pi_rpc import OmpRPCClient, PiRPCClient, PiRPCError

FAKE = Path(__file__).with_name("fixtures") / "omp_rpc_fake.py"


class FixtureOmp(OmpRPCClient):
    async def _ensure_started(self) -> None:
        if self._proc is not None and self._proc.returncode is None:
            return
        if self._closed:
            from sleeptalk.agents.pi_rpc import PiRPCError

            raise PiRPCError("Pi RPC client is closed.")
        await self._spawn([sys.executable, str(FAKE)])
        await self._subscribe_subagents()


def test_native_lifecycle_frames_become_workers() -> None:
    started = worker_from_lifecycle(
        {
            "id": "child-1",
            "agent": "explore",
            "description": "Explore",
            "status": "started",
            "detached": True,
        }
    )
    assert started is not None
    assert started.worker_id == "child-1"
    assert started.phase is WorkerPhase.STARTED
    assert started.label == "Explore"
    assert started.detached is True
    running = worker_from_progress(
        {
            "progress": {
                "id": "child-1",
                "status": "running",
                "description": "Explore",
                "recentOutput": ["secret tokens"],
                "tokens": 99,
            }
        }
    )
    assert running is not None
    assert running.phase is WorkerPhase.RUNNING
    assert running.summary is None
    done = worker_from_lifecycle(
        {
            "id": "child-1",
            "agent": "explore",
            "description": "Explore",
            "status": "completed",
        }
    )
    assert done is not None
    assert done.phase is WorkerPhase.COMPLETED
    assert done.summary == "Explore"
    assert done.detached is False
    failed = worker_from_lifecycle({"id": "child-1", "status": "failed"})
    assert failed is not None and failed.phase is WorkerPhase.FAILED
    cancelled = worker_from_lifecycle({"id": "child-1", "status": "aborted"})
    assert cancelled is not None and cancelled.phase is WorkerPhase.CANCELLED


def test_lifecycle_drops_anonymous_ids_and_child_tokens() -> None:
    assert worker_from_lifecycle({"description": "Explore", "status": "started"}) is None
    assert worker_from_progress({"progress": {"status": "running", "description": "Explore"}}) is None
    labeled = worker_from_lifecycle(
        {"id": "child-1", "description": "Explore", "status": "started"}
    )
    assert labeled is not None
    assert labeled.label == "Explore"
    assert "child-1" not in (labeled.label or "")
    correlated = worker_from_lifecycle(
        {"id": "child-1", "status": "started", "parentToolCallId": "parent-1"}
    )
    assert correlated is not None
    assert correlated.worker_id == "child-1"


def test_live_task_tool_is_a_worker() -> None:
    event = worker_from_task_tool(
        {
            "type": "tool_execution_start",
            "toolName": "task",
            "toolCallId": "parent-1",
            "subagentId": "child-live",
            "args": {"agent": "explore", "task": "Repo architecture"},
        }
    )
    assert event is not None
    assert event.worker_id == "child-live"
    assert event.label == "Repo architecture"
    assert event.phase is WorkerPhase.STARTED
    assert event.detached is False


def test_tool_without_child_id_is_not_admitted() -> None:
    assert (
        worker_from_task_tool(
            {
                "type": "tool_execution_start",
                "toolName": "task",
                "toolCallId": "parent-1",
                "args": {"name": "ScoutBot", "task": "Explore"},
            }
        )
        is None
    )
    assert is_worker_tool_name("task") is True


def test_ordinary_tools_are_not_workers() -> None:
    assert is_worker_tool_name("read") is False
    assert is_worker_tool_name("bash") is False
    assert is_worker_tool_name("shell") is False
    assert is_worker_tool_name("execute") is False
    assert worker_from_task_tool(
        {"type": "tool_execution_start", "toolName": "bash", "id": "x"}
    ) is None
    assert worker_from_task_tool(
        {"type": "tool_execution_start", "toolName": "read", "subagentId": "x"}
    ) is None


def test_manifest_evidence_is_lifecycle_native() -> None:
    caps = adapter_capabilities(OmpRPCClient())
    assert caps.workers.level is SupportLevel.SUPPORTED
    assert caps.workers.evidence is WorkerEvidence.LIFECYCLE_NATIVE
    assert event_to_legacy(WorkerEvent(worker_id="w1", phase=WorkerPhase.STARTED)) is None
    pi = adapter_capabilities(PiRPCClient())
    assert pi.workers.level is SupportLevel.UNSUPPORTED
    assert pi.workers.evidence is WorkerEvidence.NONE


@pytest.mark.asyncio
async def test_native_lifecycle_stream_publishes_workers() -> None:
    seen: list[object] = []
    client = FixtureOmp(cwd=".", no_session=True)
    try:
        async for _ in client.respond_streaming(
            "subagent", on_event=typed_events(seen.append)
        ):
            pass
    finally:
        await client.close()
    workers = [item for item in seen if getattr(item, "kind", None) == "worker"]
    tools = [item for item in seen if getattr(item, "kind", None) == "tool"]
    assert [item.worker_id for item in workers] == ["child-1", "child-1", "child-1"]
    assert workers[0].phase is WorkerPhase.STARTED
    assert workers[0].label == "Explore the repo"
    assert workers[0].detached is True
    assert workers[1].phase is WorkerPhase.RUNNING
    assert workers[2].phase is WorkerPhase.COMPLETED
    assert [item.name for item in tools if item.name] == ["bash", "bash"]
    texts = [
        item
        for item in seen
        if getattr(item, "kind", None) == "assistant.delta"
        and "child tokens" in getattr(item, "text", "")
    ]
    assert texts == []


@pytest.mark.asyncio
async def test_live_task_tool_call_is_a_worker_not_a_tool() -> None:
    seen: list[object] = []
    client = FixtureOmp(cwd=".", no_session=True)
    try:
        async for _ in client.respond_streaming("task", on_event=typed_events(seen.append)):
            pass
    finally:
        await client.close()
    workers = [item for item in seen if getattr(item, "kind", None) == "worker"]
    tools = [item for item in seen if getattr(item, "kind", None) == "tool"]
    assert [item.worker_id for item in workers] == ["child-live", "child-live"]
    assert workers[0].phase is WorkerPhase.STARTED
    assert workers[0].label == "Repo architecture"
    assert workers[1].phase is WorkerPhase.COMPLETED
    assert [item.name for item in tools if item.name] == ["read", "read"]


@pytest.mark.asyncio
async def test_anonymous_task_tool_is_swallowed_not_a_tool() -> None:
    seen: list[object] = []
    client = FixtureOmp(cwd=".", no_session=True)
    try:
        async for _ in client.respond_streaming(
            "anonymous", on_event=typed_events(seen.append)
        ):
            pass
    finally:
        await client.close()
    kinds = [item.kind for item in seen if getattr(item, "kind", None) in {"worker", "tool"}]
    assert kinds == []


@pytest.mark.asyncio
async def test_late_lifecycle_after_parent_turn_completes() -> None:
    seen: list[object] = []
    finished = asyncio.Event()

    def _collect(event: object) -> None:
        seen.append(event)
        if getattr(event, "kind", None) == "worker" and event.phase is WorkerPhase.COMPLETED:
            finished.set()

    client = FixtureOmp(cwd=".", no_session=True)
    try:
        async for _ in client.respond_streaming("late", on_event=typed_events(_collect)):
            pass
        workers = [item for item in seen if getattr(item, "kind", None) == "worker"]
        assert [item.phase for item in workers] == [WorkerPhase.STARTED]
        await client._event_queue.put(
            {
                "type": "subagent_lifecycle",
                "payload": {
                    "id": "child-1",
                    "agent": "explore",
                    "description": "BTC 77647",
                    "status": "completed",
                    "detached": True,
                },
            }
        )
        await asyncio.wait_for(finished.wait(), timeout=2.0)
        workers = [item for item in seen if getattr(item, "kind", None) == "worker"]
        assert workers[-1].phase is WorkerPhase.COMPLETED
        assert workers[-1].summary == "BTC 77647"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_subagent_event_tokens_are_not_spoken() -> None:
    seen: list[object] = []
    client = FixtureOmp(cwd=".", no_session=True)
    try:
        async for _ in client.respond_streaming(
            "tokens", on_event=typed_events(seen.append)
        ):
            pass
    finally:
        await client.close()
    texts = [getattr(item, "text", "") for item in seen if getattr(item, "kind", None) == "assistant.delta"]
    assert all("child tokens" not in text for text in texts)
    assert [item.kind for item in seen if getattr(item, "kind", None) == "worker"] == []


def test_omp_warmup_waits_for_reasoning_models() -> None:
    assert OmpRPCClient.warmup_timeout > PiRPCClient.warmup_timeout
    assert OmpRPCClient.warmup_timeout >= 60.0


@pytest.mark.asyncio
async def test_assistant_error_is_raised() -> None:
    client = FixtureOmp(cwd=".", no_session=True)
    try:
        with pytest.raises(PiRPCError, match="Reasoning is mandatory"):
            await client.respond("error")
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_message_end_text_without_deltas_is_spoken() -> None:
    client = FixtureOmp(cwd=".", no_session=True)
    try:
        text = await client.respond("batch")
    finally:
        await client.close()
    assert "OK. Ready." in text
