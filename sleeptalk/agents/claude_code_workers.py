"""Map Claude Code SDK messages onto WorkerEvent.

Latest Agent SDK main emits native ``system/task_*`` frames with ``task_id``.
Live tool-shaped delegation is ``tool_use`` name ``Agent`` (renamed from
``Task`` in Claude Code v2.1.63). Generic tools stay tools. ``TaskCreate``
is a list tool, not a worker.
"""

from __future__ import annotations

from typing import Any

from sleeptalk.adapter import WorkerEvent, WorkerPhase

WORKER_TOOLS = frozenset({"agent", "task"})
_LIFECYCLE_SUBTYPES = frozenset(
    {"task_started", "task_progress", "task_notification", "task_updated"}
)
_TERMINAL_STATUS = {
    "completed": WorkerPhase.COMPLETED,
    "complete": WorkerPhase.COMPLETED,
    "success": WorkerPhase.COMPLETED,
    "error": WorkerPhase.FAILED,
    "failed": WorkerPhase.FAILED,
    "stopped": WorkerPhase.CANCELLED,
    "cancelled": WorkerPhase.CANCELLED,
    "canceled": WorkerPhase.CANCELLED,
    "killed": WorkerPhase.CANCELLED,
    "interrupted": WorkerPhase.CANCELLED,
}


def is_worker_tool_name(name: str) -> bool:
    return name.strip().lower() in WORKER_TOOLS


def is_lifecycle_message(message: Any) -> bool:
    subtype = str(_field(message, "subtype") or "").strip().lower()
    return subtype in _LIFECYCLE_SUBTYPES


def worker_from_lifecycle(message: Any) -> WorkerEvent | None:
    subtype = str(_field(message, "subtype") or "").strip().lower()
    if subtype not in _LIFECYCLE_SUBTYPES:
        return None
    task_id = str(_field(message, "task_id") or "").strip()
    if not task_id:
        return None
    phase = _lifecycle_phase(subtype, message)
    worker_id = str(_field(message, "tool_use_id") or "").strip() or task_id
    return WorkerEvent(
        worker_id=worker_id,
        phase=phase,
        label=_optional_text(_field(message, "description")),
        detached=_detached_from_task_type(_field(message, "task_type")),
        summary=_lifecycle_summary(subtype, message),
    )


def worker_from_tool(
    name: str,
    tool_input: Any,
    call_id: str,
    *,
    phase: WorkerPhase = WorkerPhase.STARTED,
) -> WorkerEvent | None:
    if not is_worker_tool_name(name):
        return None
    args = tool_input if isinstance(tool_input, dict) else {}
    worker_id = str(
        call_id or args.get("agent_id") or args.get("session_id") or ""
    ).strip()
    if not worker_id:
        return None
    label = _optional_text(
        args.get("description") or args.get("subagent_type") or args.get("prompt")
    )
    return WorkerEvent(
        worker_id=worker_id,
        phase=phase,
        label=label,
        detached=_detached_from_flag(args.get("run_in_background")),
    )


def _lifecycle_phase(subtype: str, message: Any) -> WorkerPhase:
    if subtype == "task_started":
        return WorkerPhase.STARTED
    if subtype == "task_progress":
        return WorkerPhase.RUNNING
    if subtype == "task_notification":
        return _phase_from_status(_field(message, "status"), default=WorkerPhase.FAILED)
    status = _field(message, "status")
    if status in (None, ""):
        patch = _field(message, "patch")
        if isinstance(patch, dict):
            status = patch.get("status")
    if status in (None, ""):
        return WorkerPhase.RUNNING
    return _phase_from_status(status, default=WorkerPhase.RUNNING)


def _lifecycle_summary(subtype: str, message: Any) -> str | None:
    if subtype != "task_notification":
        return None
    return _optional_text(_field(message, "summary"))


def _detached_from_task_type(task_type: Any) -> bool:
    key = str(task_type or "").strip().lower()
    if key in {"foreground", "sync", "synchronous"}:
        return False
    return True


def _detached_from_flag(value: Any) -> bool:
    if value is False:
        return False
    return True


def _phase_from_status(status: Any, *, default: WorkerPhase) -> WorkerPhase:
    key = str(status or "").strip().lower()
    return _TERMINAL_STATUS.get(key, default)


def _field(message: Any, name: str) -> Any:
    value = getattr(message, name, None)
    if value not in (None, ""):
        return value
    data = getattr(message, "data", None)
    if isinstance(data, dict) and data.get(name) not in (None, ""):
        return data.get(name)
    if isinstance(message, dict):
        return message.get(name)
    return None


def _optional_text(value: Any) -> str | None:
    if isinstance(value, dict):
        value = value.get("text")
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None
