"""Map Hermes delegation wire events onto WorkerEvent.

Gateway (latest main) emits native ``subagent.*`` frames with ``subagent_id``.
ACP (latest main) only surfaces ``delegate_task`` as a tool_call whose title
starts with ``delegate:`` / ``delegate batch``. Kind ``execute`` is also used
for terminal tools, so it is never a worker signal on its own. Later
``tool_call_update`` frames often carry only ``toolCallId`` and ``status``.
Those complete the same worker when the id is already known.
"""

from __future__ import annotations

import json
from collections.abc import Collection
from pathlib import Path
from typing import Any

from sleeptalk.adapter import WorkerEvent, WorkerPhase

WORKER_TOOLS = frozenset({"delegate_task"})
_CHILD_PREFIXES = ("sa-", "deleg_")
_ACP_DELEGATE_TITLES = ("delegate:", "delegate batch", "delegate task")
_TERMINAL_STATUS = {
    "completed": WorkerPhase.COMPLETED,
    "complete": WorkerPhase.COMPLETED,
    "success": WorkerPhase.COMPLETED,
    "error": WorkerPhase.FAILED,
    "failed": WorkerPhase.FAILED,
    "stalled": WorkerPhase.FAILED,
    "timeout": WorkerPhase.FAILED,
    "unknown": WorkerPhase.FAILED,
    "cancelled": WorkerPhase.CANCELLED,
    "canceled": WorkerPhase.CANCELLED,
    "interrupted": WorkerPhase.CANCELLED,
}


def is_delegate_tool_name(name: str) -> bool:
    return name.strip().lower() in WORKER_TOOLS


def is_acp_delegate(
    update: dict[str, Any],
    known_ids: Collection[str] | None = None,
) -> bool:
    name = str(update.get("name") or update.get("toolName") or "").strip().lower()
    if is_delegate_tool_name(name):
        return True
    title = str(update.get("title") or "").strip().lower()
    if title.startswith(_ACP_DELEGATE_TITLES):
        return True
    worker_id = _acp_worker_id(update)
    return bool(worker_id and known_ids is not None and worker_id in known_ids)


def is_child_target(target_id: str) -> bool:
    text = target_id.strip()
    return text.startswith(_CHILD_PREFIXES)


def worker_from_gateway(kind: str, payload: dict[str, Any]) -> WorkerEvent | None:
    if kind.startswith("subagent."):
        return _from_subagent(kind, payload)
    if kind in {"tool.start", "tool.complete"} and is_delegate_tool_name(
        str(payload.get("tool_name") or payload.get("name") or "")
    ):
        return _from_delegate_tool(kind, payload)
    return None


def worker_from_acp(
    update: dict[str, Any],
    known_ids: Collection[str] | None = None,
) -> WorkerEvent | None:
    if not is_acp_delegate(update, known_ids):
        return None
    worker_id = _acp_worker_id(update)
    if not worker_id:
        return None
    return WorkerEvent(
        worker_id=worker_id,
        phase=_acp_phase(update),
        label=_acp_label(update),
        detached=True,
        summary=_acp_summary(update),
    )


def worker_from_live_manifest(
    path: str | Path, worker_id: str
) -> WorkerEvent | None:
    worker_id = worker_id.strip()
    if not worker_id:
        return None
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    tasks = data.get("tasks")
    if not isinstance(tasks, list):
        tasks = []
    statuses = [
        str(task.get("status") or "").strip().lower()
        for task in tasks
        if isinstance(task, dict)
    ]
    batch_done = "completed" in data or (
        bool(statuses) and all(status in _TERMINAL_STATUS for status in statuses)
    )
    if not batch_done:
        return None
    phase = WorkerPhase.COMPLETED
    for status in statuses:
        mapped = _TERMINAL_STATUS.get(status)
        if mapped is WorkerPhase.FAILED:
            phase = WorkerPhase.FAILED
            break
        if mapped is WorkerPhase.CANCELLED:
            phase = WorkerPhase.CANCELLED
    return WorkerEvent(
        worker_id=worker_id,
        phase=phase,
        detached=True,
        summary=_summary_from_manifest(data),
    )


def _from_subagent(kind: str, payload: dict[str, Any]) -> WorkerEvent | None:
    if kind in {"subagent.text", "subagent.thinking", "subagent.tool", "subagent.progress"}:
        return None
    worker_id = str(
        payload.get("subagent_id") or payload.get("child_session_id") or ""
    ).strip()
    if not worker_id:
        return None
    if kind == "subagent.spawn_requested":
        phase = WorkerPhase.STARTED
    elif kind == "subagent.start":
        phase = WorkerPhase.RUNNING
    elif kind == "subagent.complete":
        phase = _phase_from_status(payload.get("status"), default=WorkerPhase.FAILED)
    else:
        return None
    return WorkerEvent(
        worker_id=worker_id,
        phase=phase,
        label=_optional_text(payload.get("goal") or payload.get("text")),
        detached=_is_top_level(payload),
        summary=_optional_text(payload.get("summary")) if kind == "subagent.complete" else None,
    )


def _from_delegate_tool(kind: str, payload: dict[str, Any]) -> WorkerEvent | None:
    args = payload.get("args") if isinstance(payload.get("args"), dict) else {}
    worker_id = str(
        payload.get("subagent_id") or payload.get("child_session_id") or ""
    ).strip()
    if not worker_id:
        return None
    label = _optional_text(
        payload.get("goal") or args.get("goal") or payload.get("text")
    )
    if not label:
        tasks = args.get("tasks")
        if isinstance(tasks, list) and tasks:
            label = f"{len(tasks)} checks"
    phase = (
        WorkerPhase.COMPLETED
        if kind == "tool.complete"
        else WorkerPhase.STARTED
    )
    return WorkerEvent(
        worker_id=worker_id,
        phase=phase,
        label=label,
        detached=_is_top_level(payload),
    )


def _acp_worker_id(update: dict[str, Any]) -> str:
    return str(update.get("toolCallId") or update.get("tool_call_id") or "").strip()


def _acp_label(update: dict[str, Any]) -> str | None:
    title = str(update.get("title") or "").strip()
    if title.lower().startswith("delegate:"):
        return title.split(":", 1)[1].strip() or title
    return title or None


def _acp_phase(update: dict[str, Any]) -> WorkerPhase:
    status = str(update.get("status") or "").strip().lower()
    if status in {"pending", ""}:
        return WorkerPhase.STARTED
    if status in {"in_progress", "running"}:
        return WorkerPhase.RUNNING
    if status in {"completed", "complete", "success"}:
        payload = _payload_dict(update)
        if _has_child_results(payload):
            return WorkerPhase.COMPLETED
        return WorkerPhase.RUNNING
    return _phase_from_status(status, default=WorkerPhase.STARTED)


def _acp_summary(update: dict[str, Any]) -> str | None:
    payload = _payload_dict(update)
    if payload is not None:
        summary = _summary_from_results(payload)
        if summary:
            return summary
        if _is_dispatch_handle(payload):
            return None
    return _optional_text(update.get("content"))


def _payload_dict(update: dict[str, Any]) -> dict[str, Any] | None:
    for key in ("content", "rawOutput", "raw_output"):
        text = _flatten_text(update.get(key))
        if not text:
            continue
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return None


def _is_dispatch_handle(payload: dict[str, Any] | None) -> bool:
    if not payload:
        return False
    status = str(payload.get("status") or "").strip().lower()
    if status == "dispatched":
        return True
    if str(payload.get("mode") or "").strip().lower() == "background":
        return True
    return bool(payload.get("live_transcripts") or payload.get("delegation_id"))


def _has_child_results(payload: dict[str, Any] | None) -> bool:
    if not payload:
        return False
    results = payload.get("results")
    return isinstance(results, list) and bool(results)


def _summary_from_results(payload: dict[str, Any]) -> str | None:
    results = payload.get("results")
    if not isinstance(results, list):
        return None
    parts: list[str] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        text = str(item.get("summary") or "").strip()
        if text:
            parts.append(text)
    return " ".join(parts) or None


def _summary_from_manifest(data: dict[str, Any]) -> str | None:
    tasks = data.get("tasks")
    if not isinstance(tasks, list):
        return None
    parts: list[str] = []
    for task in tasks:
        if not isinstance(task, dict):
            continue
        text = str(task.get("summary") or "").strip()
        if text:
            parts.append(text)
    return " ".join(parts) or None


def _flatten_text(value: Any) -> str | None:
    if isinstance(value, list):
        parts = [_flatten_text(item) for item in value]
        text = "".join(part for part in parts if part)
        return text or None
    if isinstance(value, dict):
        if value.get("type") == "text" or "text" in value:
            inner = value.get("text")
            return inner.strip() if isinstance(inner, str) and inner.strip() else None
        return None
    if isinstance(value, str):
        text = value.strip()
        return text or None
    return None


def _is_top_level(payload: dict[str, Any]) -> bool:
    depth = payload.get("depth")
    if depth is None:
        return True
    try:
        return int(depth) == 0
    except (TypeError, ValueError):
        return True


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
