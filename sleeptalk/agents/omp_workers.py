"""Map oh-my-pi RPC subagent frames onto WorkerEvent.

Latest OMP main forwards ``subagent_lifecycle`` / ``subagent_progress`` after
``set_subagent_subscription`` level ``progress``. Live delegation is the
``task`` tool. ``parentToolCallId`` and ``toolCallId`` are the parent tool
call, not the child id.
"""

from __future__ import annotations

from typing import Any

from sleeptalk.adapter import WorkerEvent, WorkerPhase

WORKER_TOOLS = frozenset({"task"})
_LIFECYCLE_TYPE = "subagent_lifecycle"
_PROGRESS_TYPE = "subagent_progress"
_EVENT_TYPE = "subagent_event"
_CHILD_ID_KEYS = ("id", "subagentId", "subagent_id")
_TERMINAL_STATUS = {
    "completed": WorkerPhase.COMPLETED,
    "complete": WorkerPhase.COMPLETED,
    "success": WorkerPhase.COMPLETED,
    "failed": WorkerPhase.FAILED,
    "error": WorkerPhase.FAILED,
    "aborted": WorkerPhase.CANCELLED,
    "cancelled": WorkerPhase.CANCELLED,
    "canceled": WorkerPhase.CANCELLED,
}


def is_worker_tool_name(name: str) -> bool:
    return name.strip().lower() in WORKER_TOOLS


def is_lifecycle_frame(event: dict[str, Any]) -> bool:
    return str(event.get("type") or "") == _LIFECYCLE_TYPE


def is_progress_frame(event: dict[str, Any]) -> bool:
    return str(event.get("type") or "") == _PROGRESS_TYPE


def is_subagent_frame(event: dict[str, Any]) -> bool:
    return str(event.get("type") or "") in {_LIFECYCLE_TYPE, _PROGRESS_TYPE, _EVENT_TYPE}


def worker_from_lifecycle(payload: dict[str, Any]) -> WorkerEvent | None:
    worker_id = _child_id(payload)
    if not worker_id:
        return None
    status = str(payload.get("status") or "").strip().lower()
    if status == "started":
        phase = WorkerPhase.STARTED
    else:
        phase = _phase_from_status(status, default=WorkerPhase.FAILED)
    label = _label(payload.get("description"), payload.get("task"), payload.get("agent"))
    return WorkerEvent(
        worker_id=worker_id,
        phase=phase,
        label=label,
        detached=_is_detached(payload.get("detached")),
        summary=_terminal_summary(phase, payload),
    )


def worker_from_progress(payload: dict[str, Any]) -> WorkerEvent | None:
    progress = payload.get("progress") if isinstance(payload.get("progress"), dict) else {}
    worker_id = _child_id(progress)
    if not worker_id:
        return None
    status = str(progress.get("status") or "").strip().lower()
    if status == "pending":
        phase = WorkerPhase.STARTED
    elif status == "running":
        phase = WorkerPhase.RUNNING
    else:
        phase = _phase_from_status(status, default=WorkerPhase.RUNNING)
    detached = payload.get("detached")
    if detached is None:
        detached = progress.get("detached")
    return WorkerEvent(
        worker_id=worker_id,
        phase=phase,
        label=_label(
            progress.get("description"),
            progress.get("task") or payload.get("task"),
            progress.get("agent") or payload.get("agent"),
        ),
        detached=_is_detached(detached),
    )


def worker_from_task_tool(event: dict[str, Any]) -> WorkerEvent | None:
    name = str(event.get("toolName") or event.get("tool_name") or "")
    if not is_worker_tool_name(name):
        return None
    args = event.get("args") if isinstance(event.get("args"), dict) else {}
    worker_id = _child_id(event) or _child_id(args)
    if not worker_id:
        return None
    etype = str(event.get("type") or "")
    if etype == "tool_execution_end":
        if event.get("isError") or event.get("error"):
            phase = WorkerPhase.FAILED
        else:
            phase = WorkerPhase.COMPLETED
    else:
        phase = WorkerPhase.STARTED
    return WorkerEvent(
        worker_id=worker_id,
        phase=phase,
        label=_label(args.get("description"), args.get("task") or args.get("context"), args.get("agent")),
        detached=_is_detached(event.get("detached") if "detached" in event else args.get("detached")),
    )


def _child_id(data: dict[str, Any]) -> str:
    for key in _CHILD_ID_KEYS:
        value = str(data.get(key) or "").strip()
        if value:
            return value
    return ""


def _label(*candidates: Any) -> str | None:
    for value in candidates:
        text = _optional_text(value)
        if text:
            return text
    return None


def _terminal_summary(phase: WorkerPhase, payload: dict[str, Any]) -> str | None:
    if phase not in {WorkerPhase.COMPLETED, WorkerPhase.FAILED, WorkerPhase.CANCELLED}:
        return None
    return _label(payload.get("description"), payload.get("task"))


def _is_detached(value: Any) -> bool:
    return value is True


def _phase_from_status(status: Any, *, default: WorkerPhase) -> WorkerPhase:
    key = str(status or "").strip().lower()
    return _TERMINAL_STATUS.get(key, default)


def _optional_text(value: Any) -> str | None:
    if isinstance(value, dict):
        value = value.get("text")
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None
