"""Conformance cases. Import these from a vendor card, the suite tests, or
`sleeptalk adapter test`. They raise ConformanceSkip / ConformanceFailure."""

from __future__ import annotations

import ast
import asyncio
import inspect
from pathlib import Path

from sleeptalk.adapter import (
    AgentAdapter,
    AnswerClarification,
    AssistantDelta,
    ClarificationRequested,
    ControlAccepted,
    ControlRejected,
    LegacyAgentEvent,
    PermissionDecision,
    PermissionRequested,
    ResolvePermission,
    StartVoiceSession,
    Steer,
    SupportLevel,
    ThinkingStarted,
    ToolEvent,
    ToolPhase,
    UsageReported,
    WebVoiceTarget,
    adapter_control,
    typed_events,
)

from sleeptalk.conformance.harness import (
    ConformanceFailure,
    PROMPT_CLARIFY,
    PROMPT_HANG,
    PROMPT_HELLO,
    PROMPT_PERMISSION,
    PROMPT_SLOW,
    PROMPT_TOOL,
    advertised,
    drain,
    fail,
    looks_like_preamble,
    missing,
    require,
    skip,
    skip_unless,
)


def _require_decision(request_id: str) -> None:
    try:
        ResolvePermission(request_id=request_id)  # type: ignore[call-arg]
    except TypeError:
        return
    raise ConformanceFailure("ResolvePermission must require an explicit decision")


async def case_warmup_then_close(adapter: AgentAdapter) -> None:
    await adapter.warmup()
    await adapter.close()


async def case_stream_yields_sentence(adapter: AgentAdapter) -> None:
    _events, sentences = await drain(adapter, PROMPT_HELLO)
    assert sentences, "respond_streaming must yield at least one sentence"


async def case_abort_idle_is_safe(adapter: AgentAdapter) -> None:
    await adapter.abort()


async def case_abort_mid_stream_stops(adapter: AgentAdapter) -> None:
    events: list[object] = []
    sink = typed_events(events.append)
    sentences: list[str] = []

    async def _consume() -> None:
        try:
            async for sentence in adapter.respond_streaming(
                PROMPT_SLOW, on_event=sink
            ):
                sentences.append(sentence)
                await adapter.abort()
        except Exception:
            return

    await asyncio.wait_for(_consume(), timeout=4.0)
    assert sentences, "abort mid-stream needs a first sentence"
    assert len(sentences) == 1, "abort mid-stream must stop further sentences"


async def case_thinking_before_text(adapter: AgentAdapter) -> None:
    events, _sentences = await drain(adapter, PROMPT_HELLO)
    thinking_at = next(
        (
            index
            for index, event in enumerate(events)
            if isinstance(event, ThinkingStarted)
            or (
                isinstance(event, LegacyAgentEvent) and event.value == "thinking"
            )
        ),
        None,
    )
    text_at = next(
        (
            index
            for index, event in enumerate(events)
            if isinstance(event, AssistantDelta)
            or (isinstance(event, LegacyAgentEvent) and event.value == "text")
        ),
        None,
    )
    if thinking_at is None or text_at is None:
        skip("thinking and text did not both occur")
    assert thinking_at < text_at, "thinking must precede text when both occur"


async def case_tool_lifecycle(adapter: AgentAdapter) -> None:
    require(adapter, "tool_lifecycle")
    events, _sentences = await drain(adapter, PROMPT_TOOL)
    tools = [event for event in events if isinstance(event, ToolEvent)]
    if not tools:
        missing("tool_lifecycle")
    started = any(event.phase is ToolPhase.STARTED for event in tools)
    later = any(
        event.phase
        in {ToolPhase.UPDATED, ToolPhase.COMPLETED, ToolPhase.FAILED}
        for event in tools
    )
    if not started or not later:
        missing("tool_lifecycle")


async def _wait_until_inflight(adapter: AgentAdapter, task: asyncio.Task) -> None:
    for _ in range(80):
        if task.done():
            return
        pending = getattr(adapter, "_pending", None)
        if isinstance(pending, dict) and pending:
            return
        if getattr(adapter, "_abort", None) is not None:
            await asyncio.sleep(0.02)
            return
        await asyncio.sleep(0.05)


async def _wait_for(predicate, *, timeout: float = 3.0):
    async def _poll():
        while True:
            found = predicate()
            if found is not None:
                return found
            await asyncio.sleep(0.02)

    return await asyncio.wait_for(_poll(), timeout=timeout)


async def case_permissions(adapter: AgentAdapter) -> None:
    require(adapter, "permissions")
    events: list[object] = []
    sink = typed_events(events.append)
    sentences: list[str] = []

    async def _consume() -> None:
        async for sentence in adapter.respond_streaming(
            PROMPT_PERMISSION, on_event=sink
        ):
            sentences.append(sentence)

    task = asyncio.create_task(_consume())
    try:
        request = await _wait_for(
            lambda: next(
                (
                    event
                    for event in events
                    if isinstance(event, PermissionRequested)
                ),
                None,
            )
        )
    except TimeoutError:
        if not task.done():
            task.cancel()
        missing("permissions")
        return
    assert not hasattr(request, "allowed")
    assert not hasattr(request, "approved")
    _require_decision(request.request_id)
    result = await adapter_control(
        adapter,
        ResolvePermission(
            request_id=request.request_id,
            decision=PermissionDecision.ALLOW_ONCE,
        ),
    )
    if not isinstance(result, ControlAccepted):
        missing("permissions")
    await asyncio.wait_for(task, timeout=5.0)


def case_permission_types() -> None:
    event = PermissionRequested(
        request_id="perm-1",
        prompt="Allow?",
        options=frozenset({PermissionDecision.DENY}),
    )
    assert not hasattr(event, "allowed")
    assert not hasattr(event, "approved")
    _require_decision("perm-1")


async def case_clarification(adapter: AgentAdapter) -> None:
    skip_unless(adapter, "clarification")
    events, _sentences = await drain(adapter, PROMPT_CLARIFY)
    requests = [
        event for event in events if isinstance(event, ClarificationRequested)
    ]
    if not requests:
        missing("clarification")
    request = requests[0]
    assert not hasattr(request, "answer")
    answer = AnswerClarification(request_id=request.request_id, answer="that one")
    result = await adapter_control(adapter, answer)
    if not isinstance(result, ControlAccepted):
        missing("clarification")


async def case_steering(adapter: AgentAdapter) -> None:
    skip_unless(adapter, "steering")
    result = await adapter_control(adapter, Steer(instruction="be brief"))
    if not isinstance(result, ControlAccepted):
        missing("steering")


async def case_usage(adapter: AgentAdapter) -> None:
    skip_unless(adapter, "usage")
    events, _sentences = await drain(adapter, PROMPT_HELLO)
    if not any(isinstance(event, UsageReported) for event in events):
        missing("usage")


async def case_voice_sessions_unavailable(adapter: AgentAdapter) -> None:
    capability = advertised(adapter).voice_sessions
    if capability.level is SupportLevel.SUPPORTED:
        skip("voice_sessions is supported; unavailable path does not apply")
    if capability.level is SupportLevel.UNAVAILABLE:
        assert capability.reason, "unavailable voice_sessions must include a reason"
    result = await adapter_control(
        adapter,
        StartVoiceSession(request_id="vs-1", target=WebVoiceTarget()),
    )
    assert not isinstance(result, ControlAccepted)
    assert isinstance(result, ControlRejected)


async def case_timeout_cancel_settles(adapter: AgentAdapter) -> None:
    events: list[object] = []
    sink = typed_events(events.append)

    async def _consume() -> list[str]:
        out: list[str] = []
        async for sentence in adapter.respond_streaming(
            PROMPT_HANG, on_event=sink
        ):
            out.append(sentence)
        return out

    task = asyncio.create_task(_consume())
    try:
        await _wait_until_inflight(adapter, task)
        await adapter.abort()
        try:
            await asyncio.wait_for(task, timeout=2.0)
        except Exception:
            pass
        assert task.done(), "timeout/cancel must settle the in-flight turn"
    finally:
        if not task.done():
            task.cancel()


def case_adapter_does_not_own_voice_policy(
    adapter: AgentAdapter, *, sentences: list[str], events: list[object]
) -> None:
    module = inspect.getmodule(type(adapter))
    if module is None or not getattr(module, "__file__", None):
        fail("adapter class has no module file to inspect")
    source = Path(module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
            imported.update(f"{node.module}.{alias.name}" for alias in node.names)
    assert not any(
        name == "voice_policy_runtime" or name.endswith(".voice_policy_runtime")
        for name in imported
    )
    assert "voice_policy_runtime" not in source
    for sentence in sentences:
        assert not looks_like_preamble(sentence), "events are facts, not preamble text"
    for event in events:
        if isinstance(event, AssistantDelta) and event.text:
            assert not looks_like_preamble(event.text)
        if isinstance(event, LegacyAgentEvent):
            assert not looks_like_preamble(event.value)
