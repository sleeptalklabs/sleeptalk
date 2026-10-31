from __future__ import annotations

import asyncio
from types import SimpleNamespace

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
from sleeptalk.agents.claude_code_adapter import ClaudeCodeClient
from sleeptalk.agents.claude_code_workers import (
    is_lifecycle_message,
    is_worker_tool_name,
    worker_from_lifecycle,
    worker_from_tool,
)
from tests.fixtures.claude_session import (
    FakeSession,
    ToolResultBlock,
    UserMessage,
    assistant_text,
    assistant_tool,
    result_ok,
)


def _system(**fields: object) -> SimpleNamespace:
    return SimpleNamespace(**fields)


def test_native_lifecycle_frames_become_workers() -> None:
    started = worker_from_lifecycle(
        _system(
            subtype="task_started",
            task_id="task-abc",
            description="Explore",
            task_type="background",
        )
    )
    assert started is not None
    assert started.worker_id == "task-abc"
    assert started.phase is WorkerPhase.STARTED
    assert started.label == "Explore"
    assert started.detached is True
    running = worker_from_lifecycle(
        _system(
            subtype="task_progress",
            task_id="task-abc",
            description="Explore",
        )
    )
    assert running is not None and running.phase is WorkerPhase.RUNNING
    done = worker_from_lifecycle(
        _system(
            subtype="task_notification",
            task_id="task-abc",
            status="completed",
            summary="done",
        )
    )
    assert done is not None
    assert done.phase is WorkerPhase.COMPLETED
    assert done.summary == "done"
    failed = worker_from_lifecycle(
        _system(subtype="task_notification", task_id="task-abc", status="failed")
    )
    assert failed is not None and failed.phase is WorkerPhase.FAILED
    cancelled = worker_from_lifecycle(
        _system(subtype="task_notification", task_id="task-abc", status="stopped")
    )
    assert cancelled is not None and cancelled.phase is WorkerPhase.CANCELLED
    killed = worker_from_lifecycle(
        _system(
            subtype="task_updated",
            task_id="task-abc",
            patch={"status": "killed"},
            status="killed",
        )
    )
    assert killed is not None and killed.phase is WorkerPhase.CANCELLED
    foreground = worker_from_lifecycle(
        _system(
            subtype="task_started",
            task_id="task-fg",
            description="Review",
            task_type="foreground",
        )
    )
    assert foreground is not None and foreground.detached is False


def test_lifecycle_correlates_on_tool_use_id() -> None:
    event = worker_from_lifecycle(
        _system(
            subtype="task_started",
            task_id="task-abc",
            tool_use_id="toolu_01",
            description="Explore",
        )
    )
    assert event is not None
    assert event.worker_id == "toolu_01"


def test_lifecycle_drops_anonymous_ids_and_child_tokens() -> None:
    assert worker_from_lifecycle(_system(subtype="task_started", description="Explore")) is None
    assert is_lifecycle_message(_system(subtype="task_started", description="Explore")) is True
    labeled = worker_from_lifecycle(
        _system(subtype="task_started", task_id="task-abc", description="Explore")
    )
    assert labeled is not None
    assert labeled.label == "Explore"
    assert "task-abc" not in (labeled.label or "")
    assert "task-abc" not in (labeled.summary or "")


def test_live_agent_tool_is_a_worker() -> None:
    event = worker_from_tool(
        "Agent",
        {"description": "Repo architecture", "prompt": "map the repo"},
        "toolu_01",
    )
    assert event is not None
    assert event.worker_id == "toolu_01"
    assert event.label == "Repo architecture"
    assert event.phase is WorkerPhase.STARTED
    assert event.detached is True
    legacy = worker_from_tool(
        "Task",
        {"description": "Repo architecture", "run_in_background": False},
        "toolu_02",
    )
    assert legacy is not None
    assert legacy.detached is False


def test_tool_without_handle_is_not_admitted() -> None:
    assert worker_from_tool("Agent", {"description": "Explore"}, "") is None
    assert is_worker_tool_name("Agent") is True


def test_ordinary_tools_are_not_workers() -> None:
    assert is_worker_tool_name("Read") is False
    assert is_worker_tool_name("Bash") is False
    assert is_worker_tool_name("Shell") is False
    assert is_worker_tool_name("execute") is False
    assert is_worker_tool_name("TaskCreate") is False
    assert worker_from_tool("Read", {"file_path": "main.py"}, "c1") is None
    assert worker_from_tool("Bash", {"command": "ls"}, "c2") is None
    assert worker_from_tool("TaskCreate", {"subject": "todo"}, "c3") is None


def test_manifest_evidence_is_lifecycle_native() -> None:
    caps = adapter_capabilities(ClaudeCodeClient(cwd="/tmp", session=FakeSession()))
    assert caps.workers.level is SupportLevel.SUPPORTED
    assert caps.workers.evidence is WorkerEvidence.LIFECYCLE_NATIVE
    assert event_to_legacy(WorkerEvent(worker_id="w1", phase=WorkerPhase.STARTED)) is None


@pytest.mark.asyncio
async def test_live_task_tool_call_is_a_worker_not_a_tool() -> None:
    session = FakeSession(
        [
            assistant_tool(
                "Agent",
                {"description": "Repo architecture", "prompt": "map the repo"},
            ),
            UserMessage(content=[ToolResultBlock(tool_use_id="tool-1", content="ok")]),
            assistant_tool("Read", {"file_path": "main.py"}),
            assistant_text("Done."),
            result_ok(),
        ]
    )
    seen: list[object] = []
    client = ClaudeCodeClient(cwd="/tmp", session=session)
    async for _ in client.respond_streaming("hi", on_event=typed_events(seen.append)):
        pass
    workers = [item for item in seen if getattr(item, "kind", None) == "worker"]
    tools = [item for item in seen if getattr(item, "kind", None) == "tool"]
    assert [item.worker_id for item in workers] == ["tool-1", "tool-1"]
    assert workers[0].phase is WorkerPhase.STARTED
    assert workers[0].label == "Repo architecture"
    assert workers[0].detached is True
    assert workers[1].phase is WorkerPhase.COMPLETED
    assert [item.name for item in tools if item.name] == ["read"]


@pytest.mark.asyncio
async def test_native_lifecycle_stream_publishes_workers() -> None:
    session = FakeSession(
        [
            _system(
                subtype="task_started",
                task_id="task-abc",
                tool_use_id="toolu_01",
                description="Explore",
                task_type="background",
            ),
            _system(
                subtype="task_notification",
                task_id="task-abc",
                tool_use_id="toolu_01",
                status="completed",
                summary="mapped",
            ),
            assistant_tool("Bash", {"command": "git status"}),
            assistant_text("Done."),
            result_ok(),
        ]
    )
    seen: list[object] = []
    client = ClaudeCodeClient(cwd="/tmp", session=session)
    async for _ in client.respond_streaming("hi", on_event=typed_events(seen.append)):
        pass
    workers = [item for item in seen if getattr(item, "kind", None) == "worker"]
    tools = [item for item in seen if getattr(item, "kind", None) == "tool"]
    assert [item.worker_id for item in workers] == ["toolu_01", "toolu_01"]
    assert workers[0].phase is WorkerPhase.STARTED
    assert workers[1].phase is WorkerPhase.COMPLETED
    assert workers[1].summary == "mapped"
    assert [item.name for item in tools if item.name] == ["bash"]


@pytest.mark.asyncio
async def test_live_tool_and_lifecycle_share_one_worker() -> None:
    session = FakeSession(
        [
            assistant_tool(
                "Agent",
                {"description": "Repo architecture", "prompt": "map the repo"},
            ),
            _system(
                subtype="task_started",
                task_id="task-abc",
                tool_use_id="tool-1",
                description="Repo architecture",
                task_type="background",
            ),
            _system(
                subtype="task_notification",
                task_id="task-abc",
                tool_use_id="tool-1",
                status="completed",
                summary="mapped",
            ),
            assistant_text("Done."),
            result_ok(),
        ]
    )
    seen: list[object] = []
    client = ClaudeCodeClient(cwd="/tmp", session=session)
    async for _ in client.respond_streaming("hi", on_event=typed_events(seen.append)):
        pass
    workers = [item for item in seen if getattr(item, "kind", None) == "worker"]
    tools = [item for item in seen if getattr(item, "kind", None) == "tool"]
    assert [item.worker_id for item in workers] == ["tool-1", "tool-1", "tool-1"]
    assert [item.phase for item in workers] == [
        WorkerPhase.STARTED,
        WorkerPhase.STARTED,
        WorkerPhase.COMPLETED,
    ]
    assert tools == []


@pytest.mark.asyncio
async def test_late_task_notification_after_parent_result() -> None:
    late: asyncio.Queue[object] = asyncio.Queue()

    class _LateSession(FakeSession):
        async def receive(self):
            async for message in super().receive():
                yield message
            while True:
                message = await late.get()
                if message is None:
                    return
                yield message

    session = _LateSession(
        [
            _system(
                subtype="task_started",
                task_id="task-abc",
                tool_use_id="toolu_01",
                description="Prices",
                task_type="background",
            ),
            assistant_text("Spawned."),
            result_ok(),
        ]
    )
    seen: list[object] = []
    finished = asyncio.Event()

    def _collect(event: object) -> None:
        seen.append(event)
        if getattr(event, "kind", None) == "worker" and event.phase is WorkerPhase.COMPLETED:
            finished.set()

    client = ClaudeCodeClient(cwd="/tmp", session=session)
    try:
        async for _ in client.respond_streaming("hi", on_event=typed_events(_collect)):
            pass
        workers = [item for item in seen if getattr(item, "kind", None) == "worker"]
        assert [item.phase for item in workers] == [WorkerPhase.STARTED]
        await late.put(
            _system(
                subtype="task_notification",
                task_id="task-abc",
                tool_use_id="toolu_01",
                status="completed",
                summary="BTC 77647",
            )
        )
        await asyncio.wait_for(finished.wait(), timeout=2.0)
        workers = [item for item in seen if getattr(item, "kind", None) == "worker"]
        assert workers[-1].phase is WorkerPhase.COMPLETED
        assert workers[-1].summary == "BTC 77647"
    finally:
        await late.put(None)
        await client.close()


@pytest.mark.asyncio
async def test_anonymous_agent_tool_is_swallowed_not_a_tool() -> None:
    session = FakeSession(
        [
            assistant_tool("Agent", {"description": "Explore"}),
            assistant_text("Done."),
            result_ok(),
        ]
    )
    session.messages[0].content[0].id = ""
    seen: list[object] = []
    client = ClaudeCodeClient(cwd="/tmp", session=session)
    async for _ in client.respond_streaming("hi", on_event=typed_events(seen.append)):
        pass
    assert [item.kind for item in seen if getattr(item, "kind", None) in {"worker", "tool"}] == []
