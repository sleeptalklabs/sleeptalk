from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

import sleeptalk.agents.claude_code_adapter as claude_adapter
from sleeptalk.agents.claude_code_adapter import ClaudeCodeClient, ClaudeCodeError, _tool_event


class _Text:
    def __init__(self, text: str) -> None:
        self.text = text


class _Tool:
    def __init__(self, name: str, tool_input: dict[str, Any], id: str = "tool-1") -> None:
        self.name = name
        self.input = tool_input
        self.id = id


class _Assistant:
    def __init__(self, content: list[Any], session_id: str = "sess-1") -> None:
        self.content = content
        self.session_id = session_id


class _Result:
    def __init__(self, *, is_error: bool = False, result: str = "", session_id: str = "sess-1") -> None:
        self.is_error = is_error
        self.result = result
        self.session_id = session_id


class _FakeSession:
    def __init__(self, messages: list[Any] | None = None, **options: Any) -> None:
        self.messages = list(messages or [])
        self.session_id = str(options.get("resume") or "sess-1")
        self.can_use_tool = options.get("can_use_tool")
        self.interrupt_count = 0
        self.close_count = 0
        self.queries: list[str] = []

    async def connect(self) -> None:
        return

    async def query(self, text: str) -> None:
        self.queries.append(text)

    async def receive(self) -> AsyncIterator[Any]:
        for message in self.messages:
            yield message

    async def interrupt(self) -> None:
        self.interrupt_count += 1

    async def close(self) -> None:
        self.close_count += 1


class _RecordingFactory:
    def __init__(self, messages: list[Any] | None = None) -> None:
        self.messages = list(messages or [])
        self.sessions: list[_FakeSession] = []
        self.kwargs: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> _FakeSession:
        session = _FakeSession(self.messages, **kwargs)
        self.sessions.append(session)
        self.kwargs.append(kwargs)
        return session


@pytest.mark.asyncio
async def test_streams_sentences_and_captures_session():
    session = _FakeSession([_Assistant([_Text("One. Two.")]), _Result()])
    client = ClaudeCodeClient(cwd="/tmp", session=session)
    result = await client.respond("hi")
    assert result == "One. Two."
    assert session.session_id == "sess-1"


@pytest.mark.asyncio
async def test_tool_use_events_from_assistant_block():
    session = _FakeSession(
        [
            _Assistant([_Text("Working.")]),
            _Assistant([_Tool("Bash", {"command": "cd /repo && git status"})]),
            _Result(),
        ]
    )
    client = ClaudeCodeClient(cwd="/tmp", session=session)
    seen = []
    async for _ in client.respond_streaming("do it", on_event=seen.append):
        pass
    assert "tool" in seen and "tool:bash" in seen
    assert "tooldetail:running git status" in seen


@pytest.mark.asyncio
async def test_result_error_raises():
    session = _FakeSession([_Result(is_error=True, result="boom")])
    client = ClaudeCodeClient(cwd="/tmp", session=session)
    with pytest.raises(ClaudeCodeError):
        await client.respond("hi")


@pytest.mark.asyncio
async def test_abort_interrupts_and_resume_on_next_spawn():
    factory = _RecordingFactory([_Assistant([_Text("Hi.")]), _Result()])
    client = ClaudeCodeClient(cwd="/tmp", session_factory=factory)
    await client.respond("hi")
    factory.messages[:] = [_Assistant([_Text("Back.")]), _Result()]
    await client.abort()
    result = await client.respond("again")
    assert result == "Back."
    assert factory.sessions[0].interrupt_count == 1
    assert factory.kwargs[-1]["resume"] == "sess-1"
    assert len(factory.sessions) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("interrupt_failure", ["wedged", "sdk_timeout"])
async def test_abort_recovers_from_failed_interrupt(monkeypatch, interrupt_failure):
    factory = _RecordingFactory([_Assistant([_Text("Hi.")]), _Result()])
    client = ClaudeCodeClient(cwd="/tmp", session_factory=factory)
    await client.respond("hi")
    first = factory.sessions[0]

    async def failed_interrupt() -> None:
        if interrupt_failure == "wedged":
            await asyncio.Event().wait()
        raise Exception("Control request timeout: interrupt")

    monkeypatch.setattr(first, "interrupt", failed_interrupt)
    monkeypatch.setattr(claude_adapter, "CLAUDE_ABORT_TIMEOUT_S", 0.01)

    await asyncio.wait_for(client.abort(), timeout=0.1)

    assert first.close_count == 1
    factory.messages[:] = [_Assistant([_Text("Back.")]), _Result()]
    assert await client.respond("again") == "Back."
    assert factory.kwargs[-1]["resume"] == "sess-1"
    assert len(factory.sessions) == 2


def test_tool_event_mapping():
    assert _tool_event("Bash", {"command": "ls /Users/x/y"}) == ("bash", "running ls y")
    assert _tool_event("Read", {"file_path": "/a/b/main.py"}) == ("read", "reading main.py")
    assert _tool_event("WebSearch", {"query": "btc price"}) == ("search", "searching for btc price")
    assert _tool_event("Task", {"description": "Research news"}) == ("task", "starting Research news")
    assert _tool_event("mcp__research__check_new_uploads", {}) == ("mcp", "checking new uploads")
