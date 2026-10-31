from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from sleeptalk.adapter import (
    SupportLevel,
    ToolPhase,
    WorkerEvidence,
    WorkerPhase,
    adapter_capabilities,
    typed_events,
)
from sleeptalk.agents.cursor_sdk_adapter import (
    CursorSdkClient,
    CursorSdkError,
    _assistant_text,
)


@dataclass
class FakeMessage:
    type: str
    text: str = ""
    message: Any = None
    name: str = ""
    status: str = ""
    call_id: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    usage: Any = None
    agent_id: str = "agent-test"


class FakeRun:
    def __init__(self, messages: list[Any]) -> None:
        self._messages = messages
        self.status = "running"
        self.cancelled = False

    def messages(self):
        async def _gen():
            for message in self._messages:
                yield message

        return _gen()

    async def wait(self) -> Any:
        if self.cancelled:
            self.status = "cancelled"
        elif self.status == "running":
            self.status = "finished"
        return SimpleNamespace(status=self.status, id="run-1")

    async def cancel(self) -> None:
        self.cancelled = True
        self.status = "cancelled"

    def supports(self, operation: str) -> bool:
        return operation == "cancel"


class FakeAgent:
    def __init__(self, messages: list[Any], agent_id: str = "agent-test") -> None:
        self.agent_id = agent_id
        self.queries: list[str] = []
        self._messages = messages
        self.closed = False
        self.last_run: FakeRun | None = None

    async def send(self, text: str) -> FakeRun:
        self.queries.append(text)
        self.last_run = FakeRun(self._messages)
        return self.last_run

    async def close(self) -> None:
        self.closed = True


def test_assistant_text_reads_sdk_tuple_content() -> None:
    message = SimpleNamespace(
        type="assistant",
        message=SimpleNamespace(
            content=(SimpleNamespace(type="text", text="Hello from Cursor."),)
        ),
    )
    assert _assistant_text(message) == "Hello from Cursor."


def _assistant(text: str) -> FakeMessage:
    return FakeMessage(
        type="assistant",
        message=SimpleNamespace(content=[SimpleNamespace(type="text", text=text)]),
    )


@pytest.mark.asyncio
async def test_streams_assistant_text_from_run_messages() -> None:
    agent = FakeAgent([_assistant("One. "), _assistant("Two.")])
    client = CursorSdkClient(cwd="/tmp", agent=agent)
    result = await client.respond("hi")
    assert result == "One. Two."
    assert agent.queries == ["hi"]


@pytest.mark.asyncio
async def test_maps_cursor_run_events() -> None:
    agent = FakeAgent(
        [
            FakeMessage(type="thinking", text="hmm"),
            FakeMessage(
                type="tool_call",
                name="read",
                status="running",
                call_id="c1",
                args={"path": "main.py"},
            ),
            FakeMessage(
                type="tool_call",
                name="read",
                status="completed",
                call_id="c1",
                args={"path": "main.py"},
            ),
            FakeMessage(
                type="usage",
                usage=SimpleNamespace(input_tokens=3, output_tokens=4),
            ),
            FakeMessage(type="task", status="completed", text="Explore"),
            _assistant("Done."),
        ]
    )
    seen: list[object] = []
    client = CursorSdkClient(cwd="/tmp", agent=agent)
    async for _ in client.respond_streaming("hi", on_event=typed_events(seen.append)):
        pass
    events = seen
    assert any(item.kind == "thinking.started" for item in events)
    tools = [item for item in events if item.kind == "tool"]
    assert tools[0].phase is ToolPhase.STARTED
    assert tools[1].phase is ToolPhase.COMPLETED
    usage = next(item for item in events if item.kind == "usage.reported")
    assert usage.input_tokens == 3
    assert usage.output_tokens == 4
    workers = [item for item in events if item.kind == "worker"]
    assert workers[0].label == "Explore"
    assert workers[0].detached is True
    assert (
        adapter_capabilities(client).workers.evidence is WorkerEvidence.CORRELATED_CHILD
    )


@pytest.mark.asyncio
async def test_live_task_tool_call_is_a_worker_not_a_tool() -> None:
    """Live Cursor emits Task as tool_call name=task, not message.type=task."""
    agent = FakeAgent(
        [
            FakeMessage(
                type="tool_call",
                name="task",
                status="running",
                call_id="task-1",
                args={"description": "Repo architecture", "prompt": "map the repo"},
            ),
            FakeMessage(
                type="tool_call",
                name="task",
                status="completed",
                call_id="task-1",
                args={"description": "Repo architecture"},
            ),
            FakeMessage(
                type="tool_call",
                name="read",
                status="running",
                call_id="c1",
                args={"path": "main.py"},
            ),
            _assistant("Done."),
        ]
    )
    seen: list[object] = []
    client = CursorSdkClient(cwd="/tmp", agent=agent)
    async for _ in client.respond_streaming("hi", on_event=typed_events(seen.append)):
        pass
    workers = [item for item in seen if item.kind == "worker"]
    tools = [item for item in seen if item.kind == "tool"]
    assert [item.worker_id for item in workers] == ["task-1", "task-1"]
    assert workers[0].phase is WorkerPhase.RUNNING
    assert workers[0].label == "Repo architecture"
    assert workers[0].detached is True
    assert workers[1].phase is WorkerPhase.COMPLETED
    assert [item.name for item in tools] == ["read"]


@pytest.mark.asyncio
async def test_legacy_sink_speaks_same_read_once() -> None:
    agent = FakeAgent(
        [
            FakeMessage(
                type="tool_call",
                name="read",
                status="running",
                call_id="c1",
                args={"path": "main.py"},
            ),
            FakeMessage(
                type="tool_call",
                name="read",
                status="completed",
                call_id="c1",
                args={"path": "main.py"},
            ),
            _assistant("Done."),
        ]
    )
    seen: list[str] = []
    client = CursorSdkClient(cwd="/tmp", agent=agent)
    async for _ in client.respond_streaming("hi", on_event=seen.append):
        pass
    tools = [item for item in seen if item.startswith("tool:")]
    assert tools == ["tool:read"]


@pytest.mark.asyncio
async def test_spoken_slash_rewrites_to_cursor_skill(tmp_path: Path) -> None:
    skill_dir = tmp_path / ".cursor" / "skills" / "unslop"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: unslop\ndescription: cut slop\n---\nbody\n",
        encoding="utf-8",
    )
    agent = FakeAgent([_assistant("Ok.")])
    client = CursorSdkClient(cwd=str(tmp_path), agent=agent)
    await client.respond("SLASH unslop please")
    assert agent.queries == ["/unslop please"]


@pytest.mark.asyncio
async def test_run_error_status_is_not_a_startup_error() -> None:
    agent = FakeAgent([_assistant("nope.")])

    async def _send(text: str) -> FakeRun:
        run = FakeRun([])
        run.status = "error"
        return run

    agent.send = _send  # type: ignore[method-assign]
    client = CursorSdkClient(cwd="/tmp", agent=agent)
    with pytest.raises(CursorSdkError, match="failed"):
        await client.respond("hi")


@pytest.mark.asyncio
async def test_abort_cancels_active_run() -> None:
    started = asyncio_event()
    release = asyncio_event()

    class SlowRun(FakeRun):
        def messages(self):
            async def _gen():
                started.set()
                await release.wait()
                yield _assistant("late.")

            return _gen()

    agent = FakeAgent([])

    async def _send(text: str) -> SlowRun:
        run = SlowRun([])
        agent.last_run = run
        return run

    agent.send = _send  # type: ignore[method-assign]
    client = CursorSdkClient(cwd="/tmp", agent=agent)
    task = asyncio_task(client.respond("hi"))
    await started.wait()
    await client.abort()
    release.set()
    with pytest.raises(CursorSdkError, match="cancelled"):
        await task
    assert agent.last_run is not None
    assert agent.last_run.cancelled is True


@pytest.mark.asyncio
async def test_warmup_identifies_resumable_agent() -> None:
    agent = FakeAgent([], agent_id="agent-abc")
    client = CursorSdkClient(cwd="/tmp", agent=agent, agent_id="agent-abc")
    await client.warmup()
    caps = adapter_capabilities(client)
    assert caps.session_identity.level is SupportLevel.SUPPORTED
    assert caps.permissions.level is SupportLevel.UNAVAILABLE
    assert "no interactive tool-approval" in (caps.permissions.reason or "")


def asyncio_event():
    return asyncio.Event()


def asyncio_task(coro):
    return asyncio.create_task(coro)
