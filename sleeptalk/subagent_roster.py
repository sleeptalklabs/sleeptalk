"""Session-scoped store for positively identified sub-agents.

One roster per RealtimeSession. ``apply`` takes ``WorkerEvent`` only.
``ToolEvent`` is a type error. Identity is the worker id, not the current
foreground turn. ``RealtimeSession.prepare`` hangs this object off the
session. ``VoicePolicyRuntime`` emits ``roster_session_event`` for each
non-None change.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sleeptalk.adapter import (
    AgentCapabilityManifest,
    CapabilitySupport,
    SupportLevel,
    WorkerCapability,
    WorkerEvent,
    WorkerEvidence,
    WorkerPhase,
)

_TERMINAL = frozenset(
    {WorkerPhase.COMPLETED, WorkerPhase.FAILED, WorkerPhase.CANCELLED}
)
_PHASE_RANK = {
    WorkerPhase.STARTED: 0,
    WorkerPhase.RUNNING: 1,
    WorkerPhase.COMPLETED: 2,
    WorkerPhase.FAILED: 2,
    WorkerPhase.CANCELLED: 2,
}
_UNSUPPORTED = CapabilitySupport(SupportLevel.UNSUPPORTED)


@dataclass(frozen=True)
class WorkerControls:
    status: CapabilitySupport
    steering: CapabilitySupport
    follow_up: CapabilitySupport
    cancellation: CapabilitySupport


@dataclass(frozen=True)
class WorkerRecord:
    worker_id: str
    label: str
    parent_turn_id: str | None
    detached: bool
    evidence: WorkerEvidence
    phase: WorkerPhase
    summary: str | None
    started_at: str
    updated_at: str
    controls: WorkerControls

    def __post_init__(self) -> None:
        if not self.worker_id:
            raise ValueError("worker_id is required")
        if self.evidence is WorkerEvidence.NONE:
            raise ValueError("WorkerEvidence.NONE cannot appear on a stored record")


@dataclass(frozen=True)
class RosterChange:
    session_id: str
    worker_id: str
    phase: WorkerPhase
    previous_phase: WorkerPhase | None
    record: WorkerRecord


@dataclass(frozen=True)
class RosterSnapshot:
    session_id: str
    seq: int
    capability: WorkerCapability
    workers: tuple[WorkerRecord, ...]
    closed: bool


@dataclass(frozen=True)
class RosterDelta:
    since: int
    until: int
    changes: tuple[RosterChange, ...]


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _project_controls(
    *,
    evidence: WorkerEvidence,
    phase: WorkerPhase,
    steering: CapabilitySupport,
    cancellation: CapabilitySupport,
) -> WorkerControls:
    if evidence is WorkerEvidence.NAMED_TOOL:
        status = _UNSUPPORTED
        steering = _UNSUPPORTED
        cancellation = _UNSUPPORTED
    else:
        status = CapabilitySupport(SupportLevel.SUPPORTED)
    if phase in _TERMINAL:
        cancellation = CapabilitySupport(
            SupportLevel.UNAVAILABLE,
            reason=f"worker is {phase.value}",
        )
    return WorkerControls(
        status=status,
        steering=steering,
        follow_up=_UNSUPPORTED,
        cancellation=cancellation,
    )


class SubagentRoster:
    """Id-keyed worker map plus an internal append-only seq log."""

    def __init__(self, session_id: str, manifest: AgentCapabilityManifest) -> None:
        if not session_id:
            raise ValueError("session_id is required")
        self.session_id = session_id
        self._workers_cap = manifest.workers
        self._steering = manifest.steering
        self._cancellation = manifest.targeted_cancellation
        self._records: dict[str, WorkerRecord] = {}
        self._log: list[RosterChange] = []
        self._seq = 0
        self._closed = False
        self._detached_written: set[str] = set()

    @classmethod
    def from_manifest(
        cls, session_id: str, manifest: AgentCapabilityManifest
    ) -> SubagentRoster:
        return cls(session_id, manifest)

    def apply(self, event: WorkerEvent) -> RosterChange | None:
        if self._closed:
            return None
        if not event.worker_id:
            raise ValueError("worker_id is required")
        existing = self._records.get(event.worker_id)
        if existing is None:
            return self._admit(event)
        return self._transition(existing, event)

    def get(self, worker_id: str) -> WorkerRecord | None:
        return self._records.get(worker_id)

    def snapshot(self) -> RosterSnapshot:
        return RosterSnapshot(
            session_id=self.session_id,
            seq=self._seq,
            capability=self._workers_cap,
            workers=tuple(self._records.values()),
            closed=self._closed,
        )

    def changes(self, since: int = 0) -> RosterDelta:
        until = self._seq
        if since >= until:
            return RosterDelta(since=since, until=until, changes=())
        start = max(since, 0)
        return RosterDelta(since=since, until=until, changes=tuple(self._log[start:until]))

    def prune_terminal(self) -> tuple[str, ...]:
        removed: list[str] = []
        for worker_id, record in list(self._records.items()):
            if record.phase in _TERMINAL:
                del self._records[worker_id]
                self._detached_written.discard(worker_id)
                removed.append(worker_id)
        return tuple(removed)

    def close(self) -> None:
        self._closed = True
        self._records.clear()
        self._detached_written.clear()

    def _admit(self, event: WorkerEvent) -> RosterChange | None:
        if self._workers_cap.level is not SupportLevel.SUPPORTED:
            return None
        if self._workers_cap.evidence is WorkerEvidence.NONE:
            return None
        now = _now_iso()
        detached = False if event.detached is None else event.detached
        if event.detached is not None:
            self._detached_written.add(event.worker_id)
        record = WorkerRecord(
            worker_id=event.worker_id,
            label=event.label or "",
            parent_turn_id=event.parent_turn_id,
            detached=detached,
            evidence=self._workers_cap.evidence,
            phase=event.phase,
            summary=event.summary,
            started_at=now,
            updated_at=now,
            controls=self._controls_for(event.phase),
        )
        return self._commit(record, previous_phase=None)

    def _transition(
        self, existing: WorkerRecord, event: WorkerEvent
    ) -> RosterChange | None:
        if existing.phase in _TERMINAL:
            return None
        incoming_rank = _PHASE_RANK[event.phase]
        current_rank = _PHASE_RANK[existing.phase]
        if incoming_rank < current_rank:
            return None
        same_phase = event.phase is existing.phase
        if same_phase and (event.summary is None or event.summary == existing.summary):
            return None
        if same_phase:
            summary = event.summary
        else:
            summary = existing.summary if event.summary is None else event.summary
        label = existing.label or (event.label or "")
        parent_turn_id = (
            existing.parent_turn_id
            if existing.parent_turn_id is not None
            else event.parent_turn_id
        )
        detached = existing.detached
        if existing.worker_id not in self._detached_written and event.detached is not None:
            detached = event.detached
            self._detached_written.add(existing.worker_id)
        record = WorkerRecord(
            worker_id=existing.worker_id,
            label=label,
            parent_turn_id=parent_turn_id,
            detached=detached,
            evidence=existing.evidence,
            phase=event.phase,
            summary=summary,
            started_at=existing.started_at,
            updated_at=_now_iso(),
            controls=self._controls_for(event.phase),
        )
        return self._commit(record, previous_phase=existing.phase)

    def _controls_for(self, phase: WorkerPhase) -> WorkerControls:
        return _project_controls(
            evidence=self._workers_cap.evidence,
            phase=phase,
            steering=self._steering,
            cancellation=self._cancellation,
        )

    def _commit(
        self, record: WorkerRecord, *, previous_phase: WorkerPhase | None
    ) -> RosterChange:
        self._records[record.worker_id] = record
        change = RosterChange(
            session_id=self.session_id,
            worker_id=record.worker_id,
            phase=record.phase,
            previous_phase=previous_phase,
            record=record,
        )
        self._seq += 1
        self._log.append(change)
        return change
