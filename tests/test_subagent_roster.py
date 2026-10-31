from __future__ import annotations

from dataclasses import asdict, replace
from typing import get_type_hints

import pytest

from sleeptalk.adapter import (
    AgentCapabilityManifest,
    AssistantDelta,
    CapabilitySupport,
    SupportLevel,
    ToolEvent,
    WorkerCapability,
    WorkerEvent,
    WorkerEvidence,
    WorkerPhase,
)
from sleeptalk.session_events import (
    ATTEMPT_SCOPED_TYPES,
    TURN_SCOPED_TYPES,
    EventError,
    SessionEventType,
    adapter_session_event,
    roster_session_event,
)
from sleeptalk.subagent_roster import SubagentRoster, WorkerRecord


def _manifest(
    *,
    workers: WorkerCapability,
    steering: CapabilitySupport | None = None,
    cancellation: CapabilitySupport | None = None,
) -> AgentCapabilityManifest:
    supported = CapabilitySupport(SupportLevel.SUPPORTED)
    return replace(
        AgentCapabilityManifest.legacy_floor(),
        workers=workers,
        steering=steering or supported,
        targeted_cancellation=cancellation or supported,
    )


def _native() -> AgentCapabilityManifest:
    return _manifest(
        workers=WorkerCapability(
            SupportLevel.SUPPORTED, evidence=WorkerEvidence.LIFECYCLE_NATIVE
        )
    )


def _named_tool() -> AgentCapabilityManifest:
    return _manifest(
        workers=WorkerCapability(
            SupportLevel.SUPPORTED, evidence=WorkerEvidence.NAMED_TOOL
        )
    )


def test_apply_takes_worker_event_only() -> None:
    hints = get_type_hints(SubagentRoster.apply)
    assert hints["event"] is WorkerEvent
    event = adapter_session_event("s1", 1, ToolEvent(name="bash"))
    assert event.type is SessionEventType.TOOL_STARTED
    assert event.payload["name"] == "bash"


def test_adapter_session_event_keeps_assistant_delta_text() -> None:
    event = adapter_session_event("s1", 1, AssistantDelta(text="Done, the board is running"))
    assert event.payload["name"] == "text"
    assert event.payload["text"] == "Done, the board is running"


def test_adapter_session_event_rejects_worker_event() -> None:
    with pytest.raises(EventError, match="WorkerEvent"):
        adapter_session_event(
            "s1", 8, WorkerEvent(worker_id="w1", phase=WorkerPhase.STARTED)
        )


@pytest.mark.parametrize(
    "manifest",
    [
        AgentCapabilityManifest.legacy_floor(),
        _manifest(
            workers=WorkerCapability(
                SupportLevel.SUPPORTED, evidence=WorkerEvidence.NONE
            )
        ),
        _manifest(
            workers=WorkerCapability(
                SupportLevel.UNSUPPORTED, evidence=WorkerEvidence.LIFECYCLE_NATIVE
            )
        ),
        _manifest(
            workers=WorkerCapability(
                SupportLevel.UNAVAILABLE,
                reason="workers offline",
                evidence=WorkerEvidence.LIFECYCLE_NATIVE,
            )
        ),
    ],
    ids=["legacy_floor", "none", "unsupported", "unavailable"],
)
def test_floor_and_refused_levels_admit_nobody(
    manifest: AgentCapabilityManifest,
) -> None:
    roster = SubagentRoster.from_manifest("s1", manifest)
    assert (
        roster.apply(WorkerEvent(worker_id="ghost", phase=WorkerPhase.STARTED)) is None
    )
    snap = roster.snapshot()
    assert snap.workers == ()
    assert snap.capability is manifest.workers
    assert snap.closed is False


def test_duplicate_worker_id_phase_is_none() -> None:
    roster = SubagentRoster.from_manifest("s1", _native())
    first = roster.apply(WorkerEvent(worker_id="w1", phase=WorkerPhase.COMPLETED))
    assert first is not None
    assert roster.apply(WorkerEvent(worker_id="w1", phase=WorkerPhase.COMPLETED)) is None


def test_running_plus_new_summary_appends() -> None:
    roster = SubagentRoster.from_manifest("s1", _native())
    first = roster.apply(
        WorkerEvent(worker_id="w1", phase=WorkerPhase.RUNNING, summary="diffing")
    )
    assert first is not None
    assert (
        roster.apply(
            WorkerEvent(worker_id="w1", phase=WorkerPhase.RUNNING, summary="diffing")
        )
        is None
    )
    progress = roster.apply(
        WorkerEvent(worker_id="w1", phase=WorkerPhase.RUNNING, summary="applying")
    )
    assert progress is not None
    assert progress.record.summary == "applying"
    assert progress.previous_phase is WorkerPhase.RUNNING
    assert roster.snapshot().seq == 2


def test_late_completed_after_new_foreground_turn_updates_same_row() -> None:
    roster = SubagentRoster.from_manifest("s1", _native())
    roster.apply(
        WorkerEvent(
            worker_id="w1",
            phase=WorkerPhase.STARTED,
            parent_turn_id="3",
            detached=True,
            label="refactor",
        )
    )
    change = roster.apply(
        WorkerEvent(
            worker_id="w1",
            phase=WorkerPhase.COMPLETED,
            summary="refactor finished",
        )
    )
    assert change is not None
    assert change.record.parent_turn_id == "3"
    assert change.record.phase is WorkerPhase.COMPLETED
    event = roster_session_event(change)
    assert event.turn_id is None
    assert event.type is SessionEventType.ROSTER_CHANGED


def test_detached_running_survives() -> None:
    roster = SubagentRoster.from_manifest("s1", _native())
    roster.apply(
        WorkerEvent(
            worker_id="w1",
            phase=WorkerPhase.RUNNING,
            parent_turn_id="3",
            detached=True,
            label="refactor",
        )
    )
    assert roster.get("w1") is not None
    assert roster.get("w1").phase is WorkerPhase.RUNNING
    assert roster.get("w1").detached is True
    assert roster.get("w1").parent_turn_id == "3"


def test_first_terminal_wins() -> None:
    roster = SubagentRoster.from_manifest("s1", _native())
    roster.apply(
        WorkerEvent(
            worker_id="w1", phase=WorkerPhase.COMPLETED, summary="done"
        )
    )
    assert roster.apply(WorkerEvent(worker_id="w1", phase=WorkerPhase.FAILED)) is None
    assert roster.apply(WorkerEvent(worker_id="w1", phase=WorkerPhase.STARTED)) is None
    record = roster.get("w1")
    assert record is not None
    assert record.phase is WorkerPhase.COMPLETED
    assert record.summary == "done"


def test_backward_phase_is_noop() -> None:
    roster = SubagentRoster.from_manifest("s1", _native())
    roster.apply(WorkerEvent(worker_id="w1", phase=WorkerPhase.STARTED))
    roster.apply(WorkerEvent(worker_id="w1", phase=WorkerPhase.RUNNING))
    assert roster.apply(WorkerEvent(worker_id="w1", phase=WorkerPhase.STARTED)) is None
    assert roster.get("w1").phase is WorkerPhase.RUNNING


def test_prune_keeps_running_drops_completed() -> None:
    roster = SubagentRoster.from_manifest("s1", _native())
    roster.apply(WorkerEvent(worker_id="live", phase=WorkerPhase.RUNNING))
    roster.apply(WorkerEvent(worker_id="done", phase=WorkerPhase.COMPLETED))
    removed = roster.prune_terminal()
    assert removed == ("done",)
    assert roster.get("live") is not None
    assert roster.get("done") is None


def test_close_refuses_apply() -> None:
    roster = SubagentRoster.from_manifest("s1", _native())
    roster.apply(WorkerEvent(worker_id="w1", phase=WorkerPhase.RUNNING))
    roster.close()
    assert roster.apply(WorkerEvent(worker_id="w2", phase=WorkerPhase.STARTED)) is None
    snap = roster.snapshot()
    assert snap.workers == ()
    assert snap.closed is True
    roster.close()
    assert roster.snapshot().closed is True


def test_empty_worker_id_raises() -> None:
    roster = SubagentRoster.from_manifest("s1", _native())
    with pytest.raises(ValueError):
        roster.apply(WorkerEvent(worker_id="", phase=WorkerPhase.STARTED))


def test_two_readers_with_same_since_see_the_same_changes() -> None:
    roster = SubagentRoster.from_manifest("s1", _native())
    roster.apply(WorkerEvent(worker_id="w1", phase=WorkerPhase.STARTED))
    roster.apply(WorkerEvent(worker_id="w1", phase=WorkerPhase.RUNNING))
    first = roster.changes(since=0)
    second = roster.changes(since=0)
    assert first.changes == second.changes
    assert first.until == second.until == 2
    after_first = roster.changes(since=1)
    assert len(after_first.changes) == 1
    assert after_first.changes[0].phase is WorkerPhase.RUNNING


def test_snapshot_and_event_payload_have_no_raw_or_reasoning() -> None:
    roster = SubagentRoster.from_manifest("s1", _native())
    change = roster.apply(
        WorkerEvent(worker_id="w1", phase=WorkerPhase.STARTED, label="refactor")
    )
    assert change is not None
    snap = roster.snapshot()
    assert not hasattr(snap, "raw")
    assert not hasattr(snap, "reasoning")
    for worker in snap.workers:
        assert "raw" not in asdict(worker)
        assert "reasoning" not in asdict(worker)
    payload = roster_session_event(change).payload
    assert "raw" not in payload
    assert "reasoning" not in payload
    assert "raw" not in payload["worker"]
    assert "reasoning" not in payload["worker"]


def test_named_tool_status_steer_cancel_unsupported() -> None:
    roster = SubagentRoster.from_manifest("s1", _named_tool())
    roster.apply(WorkerEvent(worker_id="w1", phase=WorkerPhase.RUNNING))
    controls = roster.get("w1").controls
    assert controls.status.level is SupportLevel.UNSUPPORTED
    assert controls.steering.level is SupportLevel.UNSUPPORTED
    assert controls.cancellation.level is SupportLevel.UNSUPPORTED
    assert controls.follow_up.level is SupportLevel.UNSUPPORTED


def test_lifecycle_native_status_supported() -> None:
    roster = SubagentRoster.from_manifest("s1", _native())
    roster.apply(WorkerEvent(worker_id="w1", phase=WorkerPhase.RUNNING))
    controls = roster.get("w1").controls
    assert controls.status.level is SupportLevel.SUPPORTED
    assert controls.steering.level is SupportLevel.SUPPORTED
    assert controls.follow_up.level is SupportLevel.UNSUPPORTED


def test_terminal_cancel_unavailable_with_reason() -> None:
    roster = SubagentRoster.from_manifest("s1", _native())
    roster.apply(WorkerEvent(worker_id="w1", phase=WorkerPhase.COMPLETED))
    cancellation = roster.get("w1").controls.cancellation
    assert cancellation.level is SupportLevel.UNAVAILABLE
    assert cancellation.reason == "worker is completed"
    assert roster.get("w1").controls.steering.level is SupportLevel.SUPPORTED


def test_named_tool_terminal_cancel_is_unavailable() -> None:
    roster = SubagentRoster.from_manifest("s1", _named_tool())
    roster.apply(WorkerEvent(worker_id="w1", phase=WorkerPhase.COMPLETED))
    controls = roster.get("w1").controls
    assert controls.status.level is SupportLevel.UNSUPPORTED
    assert controls.steering.level is SupportLevel.UNSUPPORTED
    assert controls.cancellation.level is SupportLevel.UNAVAILABLE
    assert controls.cancellation.reason == "worker is completed"


def test_roster_changed_is_session_scoped() -> None:
    assert SessionEventType.ROSTER_CHANGED not in TURN_SCOPED_TYPES
    assert SessionEventType.ROSTER_CHANGED not in ATTEMPT_SCOPED_TYPES
    roster = SubagentRoster.from_manifest("s1", _native())
    change = roster.apply(WorkerEvent(worker_id="w1", phase=WorkerPhase.STARTED))
    event = roster_session_event(change)
    assert event.type.value == "roster.changed"
    assert event.turn_id is None
    assert event.payload["workerId"] == "w1"
    assert event.payload["previousPhase"] is None
    assert event.payload["worker"]["parentTurnId"] is None


def test_parent_turn_id_stays_a_string() -> None:
    roster = SubagentRoster.from_manifest("s1", _native())
    roster.apply(
        WorkerEvent(
            worker_id="w1",
            phase=WorkerPhase.STARTED,
            parent_turn_id="3",
        )
    )
    assert roster.get("w1").parent_turn_id == "3"
    assert isinstance(roster.get("w1").parent_turn_id, str)


def test_first_write_identity_and_none_cannot_be_stored() -> None:
    roster = SubagentRoster.from_manifest("s1", _native())
    roster.apply(WorkerEvent(worker_id="w1", phase=WorkerPhase.STARTED))
    roster.apply(
        WorkerEvent(
            worker_id="w1",
            phase=WorkerPhase.RUNNING,
            label="refactor",
            parent_turn_id="7",
            detached=True,
            summary="diffing",
        )
    )
    record = roster.get("w1")
    assert record.label == "refactor"
    assert record.parent_turn_id == "7"
    assert record.detached is True
    roster.apply(
        WorkerEvent(
            worker_id="w1",
            phase=WorkerPhase.RUNNING,
            label="other",
            parent_turn_id="8",
            detached=False,
            summary="applying",
        )
    )
    record = roster.get("w1")
    assert record.label == "refactor"
    assert record.parent_turn_id == "7"
    assert record.detached is True
    assert record.summary == "applying"
    with pytest.raises(ValueError, match="NONE"):
        WorkerRecord(
            worker_id="ghost",
            label="",
            parent_turn_id=None,
            detached=False,
            evidence=WorkerEvidence.NONE,
            phase=WorkerPhase.STARTED,
            summary=None,
            started_at="t",
            updated_at="t",
            controls=roster.get("w1").controls,
        )
