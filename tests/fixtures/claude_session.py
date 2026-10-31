from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from tests.agent_conformance.harness import (
    PROMPT_HANG,
    PROMPT_HELLO,
    PROMPT_PERMISSION,
    PROMPT_SLOW,
    PROMPT_TOOL,
)


@dataclass
class TextBlock:
    text: str


@dataclass
class ThinkingBlock:
    thinking: str
    signature: str = ""


@dataclass
class ToolUseBlock:
    name: str
    input: dict[str, Any]
    id: str = "tool-1"


@dataclass
class ToolResultBlock:
    tool_use_id: str
    content: str = ""
    is_error: bool = False


@dataclass
class AssistantMessage:
    content: list[Any]
    model: str = "fake"
    session_id: str | None = None


@dataclass
class UserMessage:
    content: list[Any]


@dataclass
class ResultMessage:
    is_error: bool = False
    result: str = ""
    session_id: str = "sess-1"
    usage: dict[str, int] | None = None
    total_cost_usd: float | None = None


@dataclass
class StreamEvent:
    event: dict[str, Any]
    session_id: str = "sess-1"
    uuid: str = "evt-1"


def assistant_text(*parts: str, session_id: str = "sess-1") -> AssistantMessage:
    return AssistantMessage(
        content=[TextBlock(text="".join(parts))],
        session_id=session_id,
    )


def assistant_tool(name: str, tool_input: dict[str, Any]) -> AssistantMessage:
    return AssistantMessage(content=[ToolUseBlock(name=name, input=tool_input)])


def result_ok(session_id: str = "sess-1") -> ResultMessage:
    return ResultMessage(session_id=session_id, usage={"input_tokens": 1, "output_tokens": 1})


class FakeSession:
    def __init__(
        self,
        messages: list[Any] | None = None,
        *,
        permission_tool: tuple[str, dict[str, Any]] | None = None,
        **options: Any,
    ) -> None:
        self.messages = list(messages or [])
        self.permission_tool = permission_tool
        self.options = options
        self.session_id = str(options.get("resume") or "sess-1")
        self.can_use_tool = options.get("can_use_tool")
        self.permission_mode = options.get("permission_mode")
        self.connect_count = 0
        self.interrupt_count = 0
        self.close_count = 0
        self.queries: list[str] = []
        self.resumes: list[str | None] = [options.get("resume")]
        self._interrupt = asyncio.Event()
        self._permission_result: Any = None

    async def connect(self) -> None:
        self.connect_count += 1
        self._interrupt.clear()

    async def query(self, text: str) -> None:
        self.queries.append(text)

    async def receive(self) -> AsyncIterator[Any]:
        if self.permission_tool and self.can_use_tool is not None:
            name, tool_input = self.permission_tool
            self._permission_result = await self.can_use_tool(
                name,
                tool_input,
                SimpleNamespace(tool_use_id="tool-1"),
            )
        for message in self.messages:
            if self._interrupt.is_set():
                return
            yield message

    async def interrupt(self) -> None:
        self.interrupt_count += 1
        self._interrupt.set()

    async def close(self) -> None:
        self.close_count += 1
        self._interrupt.set()


class RecordingFactory:
    def __init__(self, messages: list[Any] | None = None) -> None:
        self.messages = list(messages or [])
        self.sessions: list[FakeSession] = []
        self.kwargs: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> FakeSession:
        session = FakeSession(self.messages, **kwargs)
        self.sessions.append(session)
        self.kwargs.append(kwargs)
        return session


@dataclass
class ConformanceFakeSession:
    session_id: str = "sess-1"
    can_use_tool: Any = None
    connect_count: int = 0
    _prompt: str = ""
    _interrupt: asyncio.Event = field(default_factory=asyncio.Event)

    async def connect(self) -> None:
        self.connect_count += 1
        self._interrupt = asyncio.Event()

    async def query(self, text: str) -> None:
        self._prompt = text.strip().lower()
        self._interrupt.clear()

    async def receive(self) -> AsyncIterator[Any]:
        key = self._prompt
        if key == PROMPT_HANG:
            await self._interrupt.wait()
            return
        if key == PROMPT_PERMISSION and self.can_use_tool is not None:
            await self.can_use_tool(
                "Bash",
                {"command": "ls"},
                SimpleNamespace(tool_use_id="perm-1"),
            )
            yield AssistantMessage(
                content=[ThinkingBlock(thinking="ok"), TextBlock(text="Allowed.")],
                session_id=self.session_id,
            )
            yield result_ok(self.session_id)
            return
        if key == PROMPT_TOOL:
            yield assistant_tool("Bash", {"command": "git status"})
            yield UserMessage(content=[ToolResultBlock(tool_use_id="tool-1", content="ok")])
            yield assistant_text("Done.")
            yield result_ok(self.session_id)
            return
        if key == PROMPT_SLOW:
            yield assistant_text("First sentence.")
            await self._interrupt.wait()
            return
        else:
            yield AssistantMessage(
                content=[ThinkingBlock(thinking="hmm"), TextBlock(text="Hello there.")],
                session_id=self.session_id,
            )
            yield result_ok(self.session_id)

    async def interrupt(self) -> None:
        self._interrupt.set()

    async def close(self) -> None:
        self._interrupt.set()
