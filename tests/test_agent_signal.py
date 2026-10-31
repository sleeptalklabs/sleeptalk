"""Typed and callback adapters report the same speech-policy activity."""

import pytest

from sleeptalk.adapter import (
    AssistantDelta,
    LegacyAgentEvent,
    ThinkingStarted,
    ToolEvent,
    ToolPhase,
    agent_signal,
    typed_events,
)


@pytest.mark.parametrize(
    ("typed", "legacy", "expected"),
    [
        (ThinkingStarted(), "thinking", ("thinking", {})),
        (AssistantDelta(), "text", ("text", {})),
        (ToolEvent("bash"), "tool:bash", ("tool", {"tool": "bash"})),
        (ToolEvent(""), "tool", ("tool", {})),
        (
            ToolEvent("", phase=ToolPhase.UPDATED, detail="reading a:b"),
            "tooldetail:reading a:b",
            ("tooldetail", {"detail": "reading a:b"}),
        ),
    ],
)
def test_adapter_boundary_normalizes_both_event_styles(typed, legacy, expected):
    signals = []
    sink = typed_events(lambda event: signals.append(agent_signal(event)))
    sink(typed)
    sink(legacy)
    assert signals == [expected, expected]


def test_tool_completion_does_not_restart_narration():
    for phase in (ToolPhase.COMPLETED, ToolPhase.FAILED):
        assert agent_signal(ToolEvent("bash", call_id="call-1", phase=phase)) is None


def test_legacy_latency_payload_survives_at_adapter_boundary():
    event = LegacyAgentEvent('workbench:{"kind":"latency","stage":"agent_admitted","elapsedMs":12}')
    assert agent_signal(event) == (
        "latency", {"kind": "latency", "stage": "agent_admitted", "elapsedMs": 12}
    )
