"""Post-hoc session health analysis for voice traces."""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sleeptalk.evals._paths import invariant_spec_path


@dataclass
class InvariantViolation:
    invariant_id: str
    trace_id: str
    message: str
    severity: str = "error"


@dataclass
class SessionHealthResult:
    traces: int = 0
    violations: list[InvariantViolation] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not any(v.severity == "error" for v in self.violations)


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def load_invariant_spec(path: Path | None = None) -> dict[str, Any]:
    spec_path = path or invariant_spec_path()
    return json.loads(spec_path.read_text(encoding="utf-8"))


def _stage_aliases(spec: dict[str, Any], group: str) -> tuple[str, ...]:
    stages = spec.get("latency_stages", {}).get(group, [])
    return tuple(stages) if isinstance(stages, list) else (group,)


def _event_types(spec: dict[str, Any], group: str) -> tuple[str, ...]:
    types = spec.get("session_event_types", {}).get(group, [])
    return tuple(types) if isinstance(types, list) else (group,)


def _has_any_stage(events: list[dict[str, Any]], aliases: tuple[str, ...]) -> bool:
    return any(str(event.get("stage") or "") in aliases for event in events)


def _first_elapsed_ms(events: list[dict[str, Any]], aliases: tuple[str, ...]) -> float | None:
    for event in events:
        if str(event.get("stage") or "") not in aliases:
            continue
        elapsed = event.get("elapsedMs")
        if isinstance(elapsed, (int, float)) and elapsed >= 0:
            return float(elapsed)
    return None


def _turn_status(events: list[dict[str, Any]]) -> str | None:
    for event in reversed(events):
        if str(event.get("stage") or "") != "turn_complete":
            continue
        status = event.get("status")
        if isinstance(status, str):
            return status
    return None


def _group_session_events(events: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        trace_id = str(event.get("traceId") or event.get("turn_id") or "")
        if trace_id:
            grouped[trace_id].append(event)
    return grouped


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _seconds_between(
    earlier: dict[str, Any],
    later: dict[str, Any],
    *,
    fallback_ms: float | None = None,
) -> float | None:
    start = _parse_time(earlier.get("loggedAt"))
    end = _parse_time(later.get("loggedAt"))
    if start and end:
        return max((end - start).total_seconds(), 0.0)
    if fallback_ms is not None:
        start_ms = _first_elapsed_ms([earlier], (str(earlier.get("stage") or ""),))
        end_ms = _first_elapsed_ms([later], (str(later.get("stage") or ""),))
        if start_ms is not None and end_ms is not None:
            return max((end_ms - start_ms) / 1000.0, 0.0)
    return None


def _find_first_event(
    events: list[dict[str, Any]],
    *,
    stage_aliases: tuple[str, ...] = (),
    event_types: tuple[str, ...] = (),
) -> dict[str, Any] | None:
    for event in events:
        if stage_aliases and str(event.get("stage") or "") in stage_aliases:
            return event
        event_type = str(event.get("type") or event.get("event") or "")
        if event_types and event_type in event_types:
            return event
    return None


def analyze_session_health(
    latency_events: list[dict[str, Any]],
    session_events: list[dict[str, Any]] | None = None,
    *,
    spec: dict[str, Any] | None = None,
    minimum_turns: int = 1,
) -> SessionHealthResult:
    spec = spec or load_invariant_spec()
    thresholds = spec.get("thresholds", {})
    session_events = session_events or []
    result = SessionHealthResult()

    traces: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in latency_events:
        trace_id = str(event.get("traceId") or "")
        if trace_id:
            traces[trace_id].append(event)

    utterance_aliases = _stage_aliases(spec, "utterance_ready")
    agent_req_aliases = _stage_aliases(spec, "agent_requested")
    agent_token_aliases = _stage_aliases(spec, "agent_first_token")
    tts_audio_aliases = _stage_aliases(spec, "tts_first_audio")
    interruption_aliases = _stage_aliases(spec, "interruption")
    turn_complete_aliases = _stage_aliases(spec, "turn_complete")

    turns = {
        trace_id: events
        for trace_id, events in traces.items()
        if _has_any_stage(events, utterance_aliases)
    }
    result.traces = len(turns)
    if result.traces < minimum_turns:
        result.violations.append(
            InvariantViolation(
                "minimum_turns",
                "",
                f"only {result.traces} completed turns; need at least {minimum_turns}",
            )
        )

    session_by_trace = _group_session_events(session_events)

    agent_token_limit = float(thresholds.get("agent_first_token_after_utterance_s", 30))
    tts_limit = float(thresholds.get("tts_after_agent_token_s", 30))
    tool_progress_limit = float(thresholds.get("progress_after_tool_started_s", 15))
    tool_audio_limit = float(thresholds.get("audio_after_tool_completed_s", 15))
    interrupt_limit = float(thresholds.get("utterance_after_interruption_s", 10))

    ordered_trace_ids = sorted(turns)

    for trace_id, events in turns.items():
        if _has_any_stage(events, utterance_aliases) and _has_any_stage(events, agent_req_aliases):
            if not _has_any_stage(events, agent_token_aliases):
                result.violations.append(
                    InvariantViolation(
                        "agent_stall_after_utterance",
                        trace_id,
                        f"utterance_ready without agent first token within {agent_token_limit:.0f}s",
                    )
                )

        if _has_any_stage(events, agent_token_aliases) and not _has_any_stage(events, tts_audio_aliases):
            status = _turn_status(events)
            if status not in {"cancelled", "canceled"}:
                result.violations.append(
                    InvariantViolation(
                        "tts_silent_after_agent_token",
                        trace_id,
                        f"agent token without TTS/route audio within {tts_limit:.0f}s",
                    )
                )

        if _has_any_stage(events, agent_req_aliases) and not _has_any_stage(events, turn_complete_aliases):
            result.violations.append(
                InvariantViolation(
                    "orphan_turn",
                    trace_id,
                    "agent request without turn_complete",
                )
            )

        trace_session = session_by_trace.get(trace_id, [])
        tool_started = _find_first_event(
            trace_session,
            event_types=_event_types(spec, "tool_started"),
        )
        if tool_started:
            progress = _find_first_event(
                events,
                stage_aliases=_stage_aliases(spec, "tool_narration"),
            ) or _find_first_event(
                events,
                stage_aliases=tts_audio_aliases,
            )
            if progress is None:
                result.violations.append(
                    InvariantViolation(
                        "dead_air_during_tool",
                        trace_id,
                        f"tool.started without progress or audio within {tool_progress_limit:.0f}s",
                    )
                )

        tool_completed = _find_first_event(
            trace_session,
            event_types=_event_types(spec, "tool_completed"),
        )
        if tool_completed:
            combined = sorted(
                [*events, *trace_session],
                key=lambda item: (
                    _parse_time(item.get("loggedAt")) or datetime.min.replace(tzinfo=UTC),
                    str(item.get("stage") or item.get("type") or ""),
                ),
            )
            completed_index = next(
                (
                    index
                    for index, item in enumerate(combined)
                    if str(item.get("type") or item.get("event") or "")
                    in _event_types(spec, "tool_completed")
                ),
                None,
            )
            after_tool = combined[completed_index + 1 :] if completed_index is not None else []
            has_output = any(
                str(item.get("stage") or "") in tts_audio_aliases
                or str(item.get("type") or item.get("event") or "")
                in (
                    *_event_types(spec, "response_first_output"),
                    *_event_types(spec, "response_completed"),
                )
                for item in after_tool
            )
            if not has_output:
                result.violations.append(
                    InvariantViolation(
                        "silent_after_tool_completed",
                        trace_id,
                        f"tool.completed without response audio/output within {tool_audio_limit:.0f}s",
                    )
                )

        if _has_any_stage(events, interruption_aliases):
            turn_index = ordered_trace_ids.index(trace_id)
            if turn_index + 1 < len(ordered_trace_ids):
                next_trace = turns[ordered_trace_ids[turn_index + 1]]
                if not _has_any_stage(next_trace, utterance_aliases):
                    result.violations.append(
                        InvariantViolation(
                            "incomplete_post_interrupt_recovery",
                            trace_id,
                            f"interruption without next utterance_ready within {interrupt_limit:.0f}s",
                        )
                    )
            elif not _has_any_stage(events, tts_audio_aliases):
                result.violations.append(
                    InvariantViolation(
                        "incomplete_post_interrupt_recovery",
                        trace_id,
                        "interruption on final turn without subsequent audio",
                    )
                )

    if latency_events:
        last_trace_id = ordered_trace_ids[-1] if ordered_trace_ids else ""
        if last_trace_id:
            last_events = turns[last_trace_id]
            if _has_any_stage(last_events, agent_req_aliases) and not _has_any_stage(
                last_events, turn_complete_aliases
            ):
                result.violations.append(
                    InvariantViolation(
                        "dirty_session_shutdown",
                        last_trace_id,
                        "session ended with in-flight turn",
                        severity="warning",
                    )
                )

    barge_from = int(thresholds.get("barge_check_from_turn_index", 3))
    if len(ordered_trace_ids) >= barge_from:
        bot_speaking_turns = [
            trace_id
            for trace_id in ordered_trace_ids[barge_from - 1 :]
            if _has_any_stage(turns[trace_id], tts_audio_aliases)
        ]
        if bot_speaking_turns:
            any_barge = any(
                _has_any_stage(turns[trace_id], interruption_aliases)
                for trace_id in ordered_trace_ids
            )
            if not any_barge:
                result.warnings.append(
                    f"no barge/interruption stages observed after turn {barge_from}; "
                    "soak scripts should exercise barge mid-session"
                )

    return result


def render_session_health(result: SessionHealthResult) -> str:
    lines = [
        "session health check",
        f"completed turns: {result.traces}",
        "",
    ]
    errors = [v for v in result.violations if v.severity == "error"]
    warnings = [v for v in result.violations if v.severity == "warning"]
    if errors:
        lines.append("violations")
        for violation in errors:
            suffix = f" ({violation.trace_id})" if violation.trace_id else ""
            lines.append(f"[FAIL] {violation.invariant_id}{suffix}: {violation.message}")
    if warnings:
        lines.append("")
        lines.append("warnings")
        for violation in warnings:
            suffix = f" ({violation.trace_id})" if violation.trace_id else ""
            lines.append(f"[WARN] {violation.invariant_id}{suffix}: {violation.message}")
    for warning in result.warnings:
        lines.append(f"[WARN] {warning}")
    lines.extend(
        [
            "",
            "session health: PASS" if result.passed else "session health: FAIL",
        ]
    )
    return "\n".join(lines)
