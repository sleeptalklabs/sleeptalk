from __future__ import annotations

from sleeptalk.adapter import ToolEvent, ToolPhase, typed_events
from sleeptalk.agents.cursor_voice import (
    ToolSpeakGate,
    build_local_agent_options,
    local_setting_sources,
    wrap_cursor_voice_sink,
)


def test_gate_speaks_started_only_when_kind_changes() -> None:
    gate = ToolSpeakGate()
    assert gate.allow("search", ToolPhase.STARTED) is True
    assert gate.allow("search", ToolPhase.COMPLETED) is False
    assert gate.allow("search", ToolPhase.STARTED) is False
    assert gate.allow("read", ToolPhase.STARTED) is True
    assert gate.allow("search", ToolPhase.STARTED) is True


def test_wrap_drops_start_and_complete_of_same_kind() -> None:
    seen: list[str] = []
    sink = wrap_cursor_voice_sink(seen.append)
    assert sink is not None
    sink("tool:search")
    sink("tool:search")
    assert seen == ["tool:search"]


def test_wrap_collapses_consecutive_same_kind_then_speaks_new() -> None:
    seen: list[str] = []
    sink = wrap_cursor_voice_sink(seen.append)
    assert sink is not None
    sink("tool:search")
    sink("tool:search")
    sink("tool:read")
    assert seen == ["tool:search", "tool:read"]


def test_wrap_speaks_again_after_kind_changes() -> None:
    seen: list[str] = []
    sink = wrap_cursor_voice_sink(seen.append)
    assert sink is not None
    sink("tool:search")
    sink("tool:read")
    sink("tool:search")
    assert seen == ["tool:search", "tool:read", "tool:search"]


def test_wrap_leaves_typed_sink_unchanged() -> None:
    seen: list[object] = []
    typed = typed_events(seen.append)
    assert wrap_cursor_voice_sink(typed) is typed
    typed(ToolEvent(name="search", phase=ToolPhase.STARTED))
    typed(ToolEvent(name="search", phase=ToolPhase.COMPLETED))
    assert [event.phase for event in seen] == [ToolPhase.STARTED, ToolPhase.COMPLETED]


def test_wrap_none_is_none() -> None:
    assert wrap_cursor_voice_sink(None) is None


def test_wrap_tooldetail_does_not_reopen_same_kind() -> None:
    seen: list[str] = []
    sink = wrap_cursor_voice_sink(seen.append)
    assert sink is not None
    sink("tool:search")
    sink("tooldetail:foo")
    sink("tool:search")
    sink("tool:read")
    assert seen == ["tool:search", "tooldetail:foo", "tool:read"]


def test_local_setting_sources_are_project_user_plugins() -> None:
    assert local_setting_sources() == ("project", "user", "plugins")
    assert build_local_agent_options("/tmp", local_setting_sources()) == {
        "cwd": "/tmp",
        "setting_sources": ["project", "user", "plugins"],
    }
