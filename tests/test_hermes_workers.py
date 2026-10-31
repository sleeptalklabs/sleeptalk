from __future__ import annotations

import asyncio

import pytest

from sleeptalk.adapter import (
    SupportLevel,
    WorkerEvidence,
    WorkerPhase,
    adapter_capabilities,
    typed_events,
)
from sleeptalk.agents.hermes_adapter import HermesACPClient
from sleeptalk.agents.hermes_workers import (
    is_acp_delegate,
    worker_from_acp,
    worker_from_gateway,
    worker_from_live_manifest,
)


def test_gateway_native_lifecycle() -> None:
    started = worker_from_gateway(
        "subagent.spawn_requested",
        {"subagent_id": "sa-0-1", "goal": "Explore"},
    )
    assert started is not None
    assert started.phase is WorkerPhase.STARTED
    assert started.worker_id == "sa-0-1"
    assert started.detached is True
    running = worker_from_gateway(
        "subagent.start", {"subagent_id": "sa-0-1", "goal": "Explore"}
    )
    assert running is not None and running.phase is WorkerPhase.RUNNING
    done = worker_from_gateway(
        "subagent.complete",
        {"subagent_id": "sa-0-1", "status": "success", "summary": "done"},
    )
    assert done is not None
    assert done.phase is WorkerPhase.COMPLETED
    assert done.summary == "done"
    cancelled = worker_from_gateway(
        "subagent.complete",
        {"subagent_id": "sa-0-1", "status": "interrupted"},
    )
    assert cancelled is not None and cancelled.phase is WorkerPhase.CANCELLED
    failed = worker_from_gateway(
        "subagent.complete",
        {"subagent_id": "sa-0-1", "status": "failed"},
    )
    assert failed is not None and failed.phase is WorkerPhase.FAILED
    timed_out = worker_from_gateway(
        "subagent.complete",
        {"subagent_id": "sa-0-1", "status": "timeout"},
    )
    assert timed_out is not None and timed_out.phase is WorkerPhase.FAILED
    unknown = worker_from_gateway(
        "subagent.complete",
        {"subagent_id": "sa-0-1", "status": "mystery"},
    )
    assert unknown is not None and unknown.phase is WorkerPhase.FAILED
    nested = worker_from_gateway(
        "subagent.start",
        {"subagent_id": "sa-1-2", "goal": "Leaf", "depth": 1},
    )
    assert nested is not None and nested.detached is False


def test_gateway_drops_child_tokens_and_anonymous_ids() -> None:
    assert worker_from_gateway("subagent.text", {"subagent_id": "sa-0-1", "text": "hi"}) is None
    assert worker_from_gateway("subagent.thinking", {"subagent_id": "sa-0-1"}) is None
    assert worker_from_gateway("subagent.tool", {"subagent_id": "sa-0-1"}) is None
    assert worker_from_gateway("subagent.start", {"goal": "Explore"}) is None
    assert (
        worker_from_gateway(
            "tool.start",
            {"tool_name": "delegate_task", "args": {"goal": "Explore"}},
        )
        is None
    )


def test_gateway_tool_shaped_delegate_needs_child_id() -> None:
    event = worker_from_gateway(
        "tool.start",
        {
            "tool_name": "delegate_task",
            "subagent_id": "sa-0-live",
            "args": {"goal": "Repo architecture"},
        },
    )
    assert event is not None
    assert event.worker_id == "sa-0-live"
    assert event.label == "Repo architecture"
    assert event.phase is WorkerPhase.STARTED
    assert worker_from_gateway("tool.start", {"tool_name": "terminal"}) is None


def test_acp_delegate_is_worker_and_read_is_not() -> None:
    update = {
        "name": "delegate_task",
        "title": "delegate: Repo architecture",
        "toolCallId": "call-1",
        "kind": "execute",
        "status": "pending",
    }
    assert is_acp_delegate(update) is True
    event = worker_from_acp(update)
    assert event is not None
    assert event.worker_id == "call-1"
    assert event.label == "Repo architecture"
    assert event.phase is WorkerPhase.STARTED
    assert event.detached is True
    read = {
        "name": "read",
        "title": "read main.py",
        "toolCallId": "call-2",
        "kind": "read",
    }
    assert is_acp_delegate(read) is False
    assert worker_from_acp(read) is None
    terminal = {
        "title": "run git status",
        "toolCallId": "call-3",
        "kind": "execute",
    }
    assert is_acp_delegate(terminal) is False
    assert worker_from_acp(terminal) is None


def test_acp_list_content_is_not_a_summary() -> None:
    event = worker_from_acp(
        {
            "name": "delegate_task",
            "title": "delegate: Explore",
            "toolCallId": "c1",
            "content": [{"type": "text", "text": "nope"}],
        }
    )
    assert event is not None
    assert event.summary is None


def test_acp_manifest_is_correlated_child() -> None:
    caps = adapter_capabilities(HermesACPClient())
    assert caps.workers.level is SupportLevel.SUPPORTED
    assert caps.workers.evidence is WorkerEvidence.CORRELATED_CHILD
    assert caps.steering.level is SupportLevel.UNSUPPORTED


def _session_update(update: dict[str, object]) -> dict[str, object]:
    return {"params": {"update": update}}


@pytest.mark.asyncio
async def test_acp_stream_publishes_delegate_as_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = HermesACPClient()
    seen: list[object] = []

    async def _started() -> None:
        client._session_id = "s1"

    async def _send(message: dict[str, object]) -> None:
        if message.get("method") != "session/prompt":
            return
        req_id = message["id"]
        await client._event_queue.put(
            _session_update(
                {
                    "sessionUpdate": "tool_call",
                    "name": "delegate_task",
                    "title": "delegate: Repo architecture",
                    "toolCallId": "call-1",
                    "kind": "execute",
                    "status": "pending",
                }
            )
        )
        await client._event_queue.put(
            _session_update(
                {
                    "sessionUpdate": "tool_call",
                    "name": "read",
                    "title": "read main.py",
                    "toolCallId": "call-2",
                    "kind": "read",
                }
            )
        )
        await client._event_queue.put(
            _session_update(
                {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "Done."},
                }
            )
        )
        future = client._pending[req_id]
        if not future.done():
            future.set_result({"stopReason": "end_turn"})

    monkeypatch.setattr(client, "_ensure_started", _started)
    monkeypatch.setattr(client, "_send", _send)
    async for _ in client.respond_streaming("hi", on_event=typed_events(seen.append)):
        pass
    workers = [item for item in seen if getattr(item, "kind", None) == "worker"]
    tools = [item for item in seen if getattr(item, "kind", None) == "tool"]
    assert [item.worker_id for item in workers] == ["call-1"]
    assert workers[0].label == "Repo architecture"
    assert workers[0].detached is True
    assert [item.name for item in tools if item.name] == ["read"]


def test_acp_nameless_update_matches_known_delegate() -> None:
    start = {
        "name": "delegate_task",
        "title": "delegate: Research the",
        "toolCallId": "call-1",
        "kind": "execute",
        "status": "pending",
    }
    nameless = {
        "sessionUpdate": "tool_call_update",
        "toolCallId": "call-1",
        "kind": "execute",
        "status": "completed",
    }
    assert is_acp_delegate(start) is True
    assert is_acp_delegate(nameless) is False
    assert is_acp_delegate(nameless, known_ids={"call-1"}) is True
    done = worker_from_acp(nameless, known_ids={"call-1"})
    assert done is not None
    assert done.worker_id == "call-1"
    assert done.phase is WorkerPhase.RUNNING
    assert done.detached is True


def test_acp_dispatch_handle_stays_running() -> None:
    event = worker_from_acp(
        {
            "name": "delegate_task",
            "title": "delegate: Prices",
            "toolCallId": "call-9",
            "status": "completed",
            "content": {
                "type": "text",
                "text": '{"status": "dispatched", "mode": "background", "delegation_id": "deleg_ab12"}',
            },
        }
    )
    assert event is not None
    assert event.phase is WorkerPhase.RUNNING
    assert event.worker_id == "call-9"


def test_acp_child_results_complete_the_worker() -> None:
    event = worker_from_acp(
        {
            "name": "delegate_task",
            "title": "delegate: Prices",
            "toolCallId": "call-9",
            "status": "completed",
            "content": {
                "type": "text",
                "text": '{"results": [{"status": "completed", "summary": "BTC 77647"}]}',
            },
        }
    )
    assert event is not None
    assert event.phase is WorkerPhase.COMPLETED
    assert event.summary == "BTC 77647"


def test_live_manifest_completes_when_batch_finishes(tmp_path) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        """
        {
          "delegation_id": "deleg_ab12",
          "task_count": 1,
          "completed": "2026-08-29 01:00:00",
          "tasks": [
            {"index": 0, "goal": "Prices", "status": "completed", "summary": "BTC 77647"}
          ]
        }
        """,
        encoding="utf-8",
    )
    event = worker_from_live_manifest(manifest, worker_id="call-1")
    assert event is not None
    assert event.worker_id == "call-1"
    assert event.phase is WorkerPhase.COMPLETED
    assert event.summary == "BTC 77647"
    running = tmp_path / "running.json"
    running.write_text(
        '{"delegation_id": "deleg_cd", "tasks": [{"index": 0, "status": "running"}]}',
        encoding="utf-8",
    )
    assert worker_from_live_manifest(running, worker_id="call-1") is None


@pytest.mark.asyncio
async def test_acp_stream_correlates_nameless_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = HermesACPClient()
    seen: list[object] = []

    async def _started() -> None:
        client._session_id = "s1"

    async def _send(message: dict[str, object]) -> None:
        if message.get("method") != "session/prompt":
            return
        req_id = message["id"]
        await client._event_queue.put(
            _session_update(
                {
                    "sessionUpdate": "tool_call",
                    "name": "delegate_task",
                    "title": "delegate: Research the",
                    "toolCallId": "call-1",
                    "kind": "execute",
                    "status": "pending",
                }
            )
        )
        await client._event_queue.put(
            _session_update(
                {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": "call-1",
                    "kind": "execute",
                    "status": "completed",
                }
            )
        )
        await client._event_queue.put(
            _session_update(
                {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "Spawned."},
                }
            )
        )
        future = client._pending[req_id]
        if not future.done():
            future.set_result({"stopReason": "end_turn"})

    monkeypatch.setattr(client, "_ensure_started", _started)
    monkeypatch.setattr(client, "_send", _send)
    async for _ in client.respond_streaming("hi", on_event=typed_events(seen.append)):
        pass
    workers = [item for item in seen if getattr(item, "kind", None) == "worker"]
    assert [item.phase for item in workers] == [
        WorkerPhase.STARTED,
        WorkerPhase.RUNNING,
    ]


@pytest.mark.asyncio
async def test_acp_late_child_result_after_prompt_returns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = HermesACPClient()
    seen: list[object] = []
    finished = asyncio.Event()

    def _collect(event: object) -> None:
        seen.append(event)
        if getattr(event, "kind", None) == "worker" and event.phase is WorkerPhase.COMPLETED:
            finished.set()

    async def _started() -> None:
        client._session_id = "s1"

    async def _send(message: dict[str, object]) -> None:
        if message.get("method") != "session/prompt":
            return
        req_id = message["id"]
        await client._event_queue.put(
            _session_update(
                {
                    "sessionUpdate": "tool_call",
                    "name": "delegate_task",
                    "title": "delegate: Research the",
                    "toolCallId": "call-1",
                    "kind": "execute",
                    "status": "pending",
                }
            )
        )
        await client._event_queue.put(
            _session_update(
                {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "Spawned."},
                }
            )
        )
        future = client._pending[req_id]
        if not future.done():
            future.set_result({"stopReason": "end_turn"})

    monkeypatch.setattr(client, "_ensure_started", _started)
    monkeypatch.setattr(client, "_send", _send)
    async for _ in client.respond_streaming("hi", on_event=typed_events(_collect)):
        pass
    await client._event_queue.put(
        _session_update(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "call-1",
                "kind": "execute",
                "status": "completed",
                "content": {
                    "type": "text",
                    "text": '{"results": [{"status": "completed", "summary": "BTC 77647"}]}',
                },
            }
        )
    )
    await asyncio.wait_for(finished.wait(), timeout=2.0)
    workers = [item for item in seen if getattr(item, "kind", None) == "worker"]
    assert workers[-1].phase is WorkerPhase.COMPLETED
    assert workers[-1].summary == "BTC 77647"
