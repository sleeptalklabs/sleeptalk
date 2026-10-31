from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import pytest

from sleeptalk.adapter import (
    PermissionDecision,
    PermissionRequested,
    ResolvePermission,
    SupportLevel,
    ThinkingStarted,
    ToolEvent,
    ToolPhase,
    WorkerEvidence,
    adapter_capabilities,
    adapter_control,
    typed_events,
)
from sleeptalk.agents.claude_code_adapter import (
    CLI_MAX_BUFFER_SIZE,
    PERMISSION_MODES,
    ClaudeCodeClient,
    ClaudeCodeError,
    _system_prompt,
    _tool_event,
    resolve_permission_mode,
)
from tests.fixtures.claude_session import (
    AssistantMessage,
    FakeSession,
    RecordingFactory,
    ResultMessage,
    StreamEvent,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    assistant_text,
    assistant_tool,
    result_ok,
)


def test_empty_persona_keeps_claude_code_preset() -> None:
    assert _system_prompt("") == {"type": "preset", "preset": "claude_code"}
    assert "append" not in _system_prompt("  ")
    assert _system_prompt("be brief")["append"] == "be brief"


def test_permission_mode_table() -> None:
    assert resolve_permission_mode("skip").sdk_mode == "bypassPermissions"
    assert resolve_permission_mode("skip").install_tool_gate is False
    assert resolve_permission_mode("bypass").sdk_mode == "bypassPermissions"
    assert resolve_permission_mode("bypassPermissions").sdk_mode == "bypassPermissions"
    assert resolve_permission_mode("deny").sdk_mode == "dontAsk"
    assert resolve_permission_mode("dontAsk").sdk_mode == "dontAsk"
    assert resolve_permission_mode("pending").sdk_mode == "default"
    assert resolve_permission_mode("pending").install_tool_gate is True
    assert resolve_permission_mode("default").install_tool_gate is True
    assert resolve_permission_mode("acceptEdits").sdk_mode == "acceptEdits"
    assert resolve_permission_mode("plan").sdk_mode == "plan"
    assert resolve_permission_mode("auto").sdk_mode == "auto"
    assert resolve_permission_mode("auto").install_tool_gate is False
    assert "skip" in PERMISSION_MODES


def test_tool_event_mapping() -> None:
    assert _tool_event("Bash", {"command": "ls /Users/x/y"}) == ("bash", "running ls y")
    assert _tool_event("Read", {"file_path": "/a/b/main.py"}) == ("read", "reading main.py")
    assert _tool_event("WebSearch", {"query": "btc price"}) == (
        "search",
        "searching for btc price",
    )
    assert _tool_event("Task", {"description": "Research news"}) == (
        "task",
        "starting Research news",
    )
    assert _tool_event("mcp__research__check_new_uploads", {}) == (
        "mcp",
        "checking new uploads",
    )


def test_adapter_does_not_import_voice_policy_runtime() -> None:
    source = Path(
        __import__("sleeptalk.agents.claude_code_adapter", fromlist=["x"]).__file__
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert "voice_policy_runtime" not in imported
    assert "voice_policy_runtime" not in source


@pytest.mark.asyncio
async def test_streams_sentences() -> None:
    session = FakeSession(
        [
            assistant_text("One. ", "Two."),
            result_ok(),
        ]
    )
    client = ClaudeCodeClient(cwd="/tmp", session=session)
    result = await client.respond("hi")
    assert result == "One. Two."
    assert session.session_id == "sess-1"
    assert session.queries == ["hi"]


@pytest.mark.asyncio
async def test_streams_sentence_deltas_from_stream_event() -> None:
    session = FakeSession(
        [
            StreamEvent(
                event={
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "One. "},
                }
            ),
            StreamEvent(
                event={
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "Two."},
                }
            ),
            result_ok(),
        ]
    )
    client = ClaudeCodeClient(cwd="/tmp", session=session)
    result = await client.respond("hi")
    assert result == "One. Two."


@pytest.mark.asyncio
async def test_tool_events_and_tool_event_mapping() -> None:
    session = FakeSession(
        [
            assistant_text("Working."),
            assistant_tool("Bash", {"command": "cd /repo && git status"}),
            result_ok(),
        ]
    )
    client = ClaudeCodeClient(cwd="/tmp", session=session)
    seen: list[str] = []
    async for _ in client.respond_streaming("do it", on_event=seen.append):
        pass
    assert "tool" in seen
    assert "tool:bash" in seen
    assert "tooldetail:running git status" in seen
    assert seen.index("tooldetail:running git status") < seen.index("tool:bash")
    kind, detail = _tool_event("Bash", {"command": "cd /repo && git status"})
    assert kind == "bash"
    assert detail == "running git status"
    assert any(
        event.startswith("workbench:") and '"kind":"tool_start"' in event
        for event in seen
    )


@pytest.mark.asyncio
async def test_typed_tool_events_are_not_duplicated() -> None:
    session = FakeSession(
        [
            assistant_tool("Bash", {"command": "git status"}),
            result_ok(),
        ]
    )
    client = ClaudeCodeClient(cwd="/tmp", session=session)
    events: list[object] = []
    async for _ in client.respond_streaming(
        "do it", on_event=typed_events(events.append)
    ):
        pass
    tool_events = [event for event in events if isinstance(event, ToolEvent)]
    assert tool_events
    assert tool_events[0].phase is ToolPhase.STARTED
    assert tool_events[0].name == "bash"
    assert tool_events[0].call_id == "tool-1"
    assert not any(
        event.phase is ToolPhase.UPDATED and not event.name and event.call_id is None
        for event in tool_events
    )


@pytest.mark.asyncio
async def test_typed_thinking_starts_once() -> None:
    session = FakeSession(
        [
            StreamEvent(
                event={
                    "type": "content_block_delta",
                    "delta": {"type": "thinking_delta", "thinking": "a"},
                }
            ),
            StreamEvent(
                event={
                    "type": "content_block_delta",
                    "delta": {"type": "thinking_delta", "thinking": "b"},
                }
            ),
            assistant_text("Hi."),
            result_ok(),
        ]
    )
    client = ClaudeCodeClient(cwd="/tmp", session=session)
    events: list[object] = []
    async for _ in client.respond_streaming("hi", on_event=typed_events(events.append)):
        pass
    assert sum(isinstance(event, ThinkingStarted) for event in events) == 1


@pytest.mark.asyncio
async def test_auto_permission_mode_is_passed_through() -> None:
    factory = RecordingFactory([assistant_text("OK."), result_ok()])
    client = ClaudeCodeClient(cwd="/tmp", permission_mode="auto", session_factory=factory)
    await client.warmup()
    assert factory.kwargs[-1]["permission_mode"] == "auto"
    assert factory.kwargs[-1]["can_use_tool"] is None
    assert factory.kwargs[-1]["max_buffer_size"] == CLI_MAX_BUFFER_SIZE
    assert CLI_MAX_BUFFER_SIZE > 1_048_576
    caps = adapter_capabilities(client)
    assert caps.permissions.level is SupportLevel.UNAVAILABLE
    assert caps.permissions.reason == "permission_mode=auto"


@pytest.mark.asyncio
async def test_warmup_queries_the_session() -> None:
    session = FakeSession([assistant_text("OK."), result_ok()])
    client = ClaudeCodeClient(cwd="/tmp", session=session)
    await client.warmup()
    assert session.queries == ["Say OK."]


@pytest.mark.asyncio
async def test_object_tool_result_emits_workbench_event() -> None:
    session = FakeSession(
        [
            assistant_tool("Bash", {"command": "git status"}),
            UserMessage(content=[ToolResultBlock(tool_use_id="tool-1", content="ok")]),
            assistant_text("Done."),
            result_ok(),
        ]
    )
    client = ClaudeCodeClient(cwd="/tmp", session=session)
    seen: list[str] = []
    async for _ in client.respond_streaming("do it", on_event=seen.append):
        pass
    assert any(
        event.startswith("workbench:") and '"kind":"tool_result"' in event
        for event in seen
    )
    assert any(
        event.startswith("workbench:") and '"tool_use_id":"tool-1"' in event
        for event in seen
    )


@pytest.mark.asyncio
async def test_empty_persona_passed_as_preset() -> None:
    factory = RecordingFactory([assistant_text("Hi."), result_ok()])
    client = ClaudeCodeClient(cwd="/tmp", session_factory=factory)
    await client.warmup()
    assert factory.kwargs[-1]["system_prompt"] == {
        "type": "preset",
        "preset": "claude_code",
    }


@pytest.mark.asyncio
async def test_transport_failure_reconnects_next_turn() -> None:
    class FlakySession(FakeSession):
        async def receive(self):
            if self.connect_count == 1:
                raise RuntimeError("transport down")
            async for message in FakeSession.receive(self):
                yield message

    session = FlakySession([assistant_text("Back."), result_ok()])
    client = ClaudeCodeClient(cwd="/tmp", session=session)
    with pytest.raises(RuntimeError, match="transport down"):
        await client.respond("a")
    assert session.close_count == 1
    result = await client.respond("b")
    assert result == "Back."
    assert session.connect_count == 2


@pytest.mark.asyncio
async def test_result_error_raises() -> None:
    session = FakeSession(
        [ResultMessage(is_error=True, result="boom", session_id="sess-1")]
    )
    client = ClaudeCodeClient(cwd="/tmp", session=session)
    with pytest.raises(ClaudeCodeError, match="boom"):
        await client.respond("hi")


@pytest.mark.asyncio
async def test_interrupt_reaches_session() -> None:
    session = FakeSession([assistant_text("Hi."), result_ok()])
    client = ClaudeCodeClient(cwd="/tmp", session=session)
    await client.warmup()
    await client.abort()
    assert session.interrupt_count == 1


@pytest.mark.asyncio
async def test_next_turn_reconnects_with_same_session_id() -> None:
    factory = RecordingFactory([assistant_text("Hi."), result_ok()])
    client = ClaudeCodeClient(cwd="/tmp", session_factory=factory)
    await client.respond("hi")
    first = factory.sessions[0]
    assert first.session_id == "sess-1"
    factory.messages[:] = [assistant_text("Back."), result_ok()]
    await client.abort()
    assert first.interrupt_count == 1
    result = await client.respond("again")
    assert result == "Back."
    assert factory.kwargs[-1]["resume"] == "sess-1"
    assert factory.sessions[-1].session_id == "sess-1"


@pytest.mark.asyncio
async def test_permission_pending_emits_and_allow_once_continues() -> None:
    session = FakeSession(
        [assistant_text("Allowed."), result_ok()],
        permission_tool=("Bash", {"command": "ls"}),
    )
    client = ClaudeCodeClient(cwd="/tmp", permission_mode="pending", session=session)
    events: list[object] = []
    sentences: list[str] = []

    async def _consume() -> None:
        async for sentence in client.respond_streaming(
            "permission", on_event=typed_events(events.append)
        ):
            sentences.append(sentence)

    task = asyncio.create_task(_consume())
    request = await _wait_for_permission(events)
    assert isinstance(request, PermissionRequested)
    assert not hasattr(request, "allowed")
    assert not hasattr(request, "approved")
    assert PermissionDecision.ALLOW_SESSION not in request.options
    assert PermissionDecision.ALLOW_ONCE in request.options
    accepted = await adapter_control(
        client,
        ResolvePermission(
            request_id=request.request_id,
            decision=PermissionDecision.ALLOW_ONCE,
        ),
    )
    await task
    assert accepted.request_id == request.request_id
    assert sentences
    assert session._permission_result is not None
    assert session._permission_result.allow is True


@pytest.mark.asyncio
async def test_permission_pending_never_auto_approves() -> None:
    session = FakeSession(
        [assistant_text("nope."), result_ok()],
        permission_tool=("Bash", {"command": "ls"}),
    )
    client = ClaudeCodeClient(cwd="/tmp", permission_mode="pending", session=session)
    events: list[object] = []

    async def _consume() -> None:
        async for _ in client.respond_streaming("permission", on_event=typed_events(events.append)):
            pass

    task = asyncio.create_task(_consume())
    await _wait_for_permission(events)
    assert session._permission_result is None
    task.cancel()
    with pytest.raises(BaseException):
        await task


@pytest.mark.asyncio
async def test_keepalive_does_not_end_receive() -> None:
    class DelayedSession(FakeSession):
        async def receive(self):
            await asyncio.sleep(0.05)
            async for message in FakeSession.receive(self):
                yield message

    session = DelayedSession([assistant_text("Still here."), result_ok()])
    client = ClaudeCodeClient(cwd="/tmp", session=session, keepalive_interval_s=0.01)
    result = await client.respond("hi")
    assert result == "Still here."


@pytest.mark.asyncio
async def test_second_respond_waits_until_receive_finishes() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    class GatedSession(FakeSession):
        async def receive(self):
            entered.set()
            await release.wait()
            async for message in FakeSession.receive(self):
                yield message

    session = GatedSession([assistant_text("One."), result_ok()])
    client = ClaudeCodeClient(cwd="/tmp", session=session)
    first = asyncio.create_task(client.respond("a"))
    await entered.wait()
    second = asyncio.create_task(client.respond("b"))
    await asyncio.sleep(0.02)
    assert session.queries == ["a"]
    release.set()
    assert await first == "One."
    assert await second == "One."
    assert session.queries == ["a", "b"]


@pytest.mark.asyncio
async def test_pending_clears_setting_sources() -> None:
    factory = RecordingFactory([assistant_text("Hi."), result_ok()])
    client = ClaudeCodeClient(cwd="/tmp", permission_mode="pending", session_factory=factory)
    await client.warmup()
    assert factory.kwargs[-1]["setting_sources"] == []
    assert factory.kwargs[-1]["can_use_tool"] is not None


@pytest.mark.asyncio
async def test_startup_config_change_reconnects_with_resume(tmp_path) -> None:
    mcp = tmp_path / ".mcp.json"
    mcp.write_text("{}")
    factory = RecordingFactory([assistant_text("Hi."), result_ok()])
    client = ClaudeCodeClient(cwd=str(tmp_path), session_factory=factory)
    await client.respond("hi")
    assert len(factory.sessions) == 1
    mcp.write_text('{"mcpServers": {}}')
    factory.messages[:] = [assistant_text("Back."), result_ok()]
    result = await client.respond("again")
    assert result == "Back."
    assert len(factory.sessions) == 2
    assert factory.kwargs[-1]["resume"] == "sess-1"
    assert factory.sessions[0].close_count == 1


@pytest.mark.asyncio
async def test_skip_leaves_setting_sources_unset() -> None:
    factory = RecordingFactory([assistant_text("Hi."), result_ok()])
    client = ClaudeCodeClient(cwd="/tmp", permission_mode="skip", session_factory=factory)
    await client.warmup()
    assert factory.kwargs[-1]["setting_sources"] is None


@pytest.mark.asyncio
async def test_permission_mode_skip_does_not_install_can_use_tool() -> None:
    session = FakeSession([assistant_text("Hi."), result_ok()])
    client = ClaudeCodeClient(cwd="/tmp", permission_mode="skip", session=session)
    await client.warmup()
    assert session.can_use_tool is None
    caps = adapter_capabilities(client)
    assert caps.permissions.level is SupportLevel.UNAVAILABLE
    assert caps.permissions.reason == "permission_mode=skip"


@pytest.mark.asyncio
async def test_bypass_permissions_are_unavailable() -> None:
    session = FakeSession([assistant_text("Hi."), result_ok()])
    client = ClaudeCodeClient(cwd="/tmp", permission_mode="bypass", session=session)
    caps = adapter_capabilities(client)
    assert caps.permissions.level is SupportLevel.UNAVAILABLE
    assert caps.permissions.reason == "permission_mode=bypass"


@pytest.mark.asyncio
async def test_skip_capabilities_and_targeted_cancel() -> None:
    session = FakeSession([assistant_text("Hi."), result_ok()])
    client = ClaudeCodeClient(cwd="/tmp", permission_mode="pending", session=session)
    caps = adapter_capabilities(client)
    assert caps.structured_events.level is SupportLevel.SUPPORTED
    assert caps.tool_lifecycle.level is SupportLevel.SUPPORTED
    assert caps.permissions.level is SupportLevel.SUPPORTED
    assert caps.session_identity.level is SupportLevel.SUPPORTED
    assert caps.usage.level is SupportLevel.SUPPORTED
    assert caps.targeted_cancellation.level is SupportLevel.SUPPORTED
    assert caps.steering.level is SupportLevel.UNSUPPORTED
    assert caps.workers.level is SupportLevel.SUPPORTED
    assert caps.workers.evidence is WorkerEvidence.LIFECYCLE_NATIVE
    assert caps.voice_sessions.level is SupportLevel.UNSUPPORTED
    await client.warmup()
    from sleeptalk.adapter import Cancel, ControlAccepted

    result = await adapter_control(client, Cancel(target_id="turn-1"))
    assert isinstance(result, ControlAccepted)
    assert session.interrupt_count == 1


async def _wait_for_permission(events: list[object], *, timeout: float = 2.0):
    async def _poll():
        while True:
            found = next(
                (event for event in events if isinstance(event, PermissionRequested)),
                None,
            )
            if found is not None:
                return found
            await asyncio.sleep(0.02)

    return await asyncio.wait_for(_poll(), timeout=timeout)


def test_existing_import_still_works() -> None:
    assert ClaudeCodeClient is not None
    assert AssistantMessage is not None
    assert ToolUseBlock is not None
