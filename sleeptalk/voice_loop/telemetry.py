from __future__ import annotations

import json
import os
import stat
import sys
import threading
import time
import uuid
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_LOG_PATH: Path | None = None
MAX_LOG_BYTES = 1024 * 1024
_write_lock = threading.Lock()
# None: the private Sleeptalk logs directory, resolved when first written.
DEFAULT_TRANSCRIPT_DEBUG_LOG_PATH: Path | None = None
_FALLBACK_ALERT_WINDOW = 40
_FALLBACK_ALERT_MIN_SAMPLES = 20
_FALLBACK_ALERT_DOMINANCE = 0.95
_fallback_windows: dict[str, deque[str]] = {}
_fallback_last_alert: dict[str, tuple[str, int]] = {}


@dataclass
class VoiceLatencyTrace:
    surface: str
    transport: str
    persona: str
    trace_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    metadata: dict[str, Any] = field(default_factory=dict)
    _started_at: float = field(default_factory=time.perf_counter)
    _last_at: float = field(default_factory=time.perf_counter)
    _once: set[str] = field(default_factory=set)
    _endpoint_ms: int | None = None

    def mark(self, stage: str, **fields: Any) -> dict[str, Any]:
        now = time.perf_counter()
        elapsed_ms = _duration_ms(self._started_at, now)
        since_previous_ms = _duration_ms(self._last_at, now)
        self._last_at = now
        self._once.add(stage)
        if stage == "utterance_ready":
            self._endpoint_ms = elapsed_ms
        endpoint_fields = (
            {"endpointElapsedMs": elapsed_ms - self._endpoint_ms}
            if self._endpoint_ms is not None else {}
        )
        event = {
            "metric": "voice_latency",
            "loggedAt": _logged_at(),
            "traceId": self.trace_id,
            "surface": self.surface,
            "transport": self.transport,
            "persona": self.persona,
            "stage": stage,
            "elapsedMs": elapsed_ms,
            "sincePreviousMs": since_previous_ms,
            **self.metadata,
            **endpoint_fields,
            **_clean_fields(fields),
        }
        _emit_event(event, prefix="voice_latency", path=_latency_log_path())
        return event

    def mark_once(self, stage: str, **fields: Any) -> dict[str, Any] | None:
        if stage in self._once:
            return None
        return self.mark(stage, **fields)

    def finish(self, stage: str = "turn_finished", **fields: Any) -> dict[str, Any]:
        return self.mark(stage, totalMs=_duration_ms(self._started_at, time.perf_counter()), **fields)


def cascade_turn_identity(config: Any, *, session_id: str = "") -> dict[str, Any]:
    """Settings that distinguish one cascade run in the shared latency log."""
    from sleeptalk.policy import local_responsiveness_enabled

    agent = getattr(config, "agent", None)
    route = getattr(config, "route", None)
    stt = getattr(config, "stt", None)
    tts = getattr(config, "tts", None)
    policy = getattr(config, "policy", None)
    fields = {
        "agent": str(getattr(agent, "type", "") or ""),
        "agentName": str(getattr(agent, "name", "") or ""),
        "requestedAgentModel": str(getattr(agent, "model", "") or ""),
        "latencySchemaVersion": 2,
        "localResponsiveness": local_responsiveness_enabled(config),
        "route": str(getattr(route, "type", "") or "") or "cascade",
        "sttProvider": str(getattr(stt, "provider", "") or ""),
        "sttModel": str(getattr(stt, "model", "") or ""),
        "ttsProvider": str(getattr(tts, "provider", "") or ""),
        "ttsModel": str(getattr(tts, "model", "") or ""),
        "ttsVoiceId": str(getattr(tts, "voice_id", "") or ""),
        "replyLength": str(getattr(policy, "reply_length", "") or ""),
        "sessionId": str(session_id or ""),
    }
    return {key: value for key, value in fields.items() if value}


def log_latency_event(
    *,
    surface: str,
    transport: str,
    persona: str,
    stage: str,
    trace_id: str | None = None,
    **fields: Any,
) -> dict[str, Any]:
    event = {
        "metric": "voice_latency",
        "loggedAt": _logged_at(),
        "traceId": trace_id or uuid.uuid4().hex[:12],
        "surface": surface,
        "transport": transport,
        "persona": persona,
        "stage": stage,
        **_clean_fields(fields),
    }
    _emit_event(event, prefix="voice_latency", path=_latency_log_path())
    return event


def _duration_ms(start: float, end: float) -> int:
    return round(max(end - start, 0) * 1000)


def _emit_event(
    event: dict[str, Any],
    *,
    prefix: str,
    path: Path,
    allow_stdout: bool = True,
) -> None:
    if prefix == "voice_latency" and not latency_logging_enabled():
        return
    if prefix == "voice_latency":
        event = _timing_fields(event)
    line = json.dumps(event, sort_keys=True)
    if len(line.encode("utf-8")) + 1 > MAX_LOG_BYTES:
        return
    if allow_stdout and os.environ.get("SLEEPTALK_TELEMETRY_STDOUT", "").strip() == "1":
        print(f"{prefix} {line}", flush=True)
    try:
        # Owner-only, and never through a symlink someone left at the path.
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with _write_lock:
            try:
                info = path.lstat()
                if not stat.S_ISREG(info.st_mode):
                    return
                path.chmod(0o600)
                if info.st_size + len(line.encode("utf-8")) + 1 > MAX_LOG_BYTES:
                    path.replace(path.with_name(path.name + ".1"))
            except FileNotFoundError:
                pass
            descriptor = os.open(
                path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600,
            )
            with os.fdopen(descriptor, "a", encoding="utf-8") as file:
                os.fchmod(file.fileno(), 0o600)
                file.write(line + "\n")
    except OSError as exc:
        print(f"{prefix}_log_write_failed {exc}", file=sys.stderr, flush=True)


def _latency_log_path() -> Path:
    configured = os.getenv("VOICE_LATENCY_LOG_PATH", "").strip()
    if configured:
        return Path(configured).expanduser()
    if DEFAULT_LOG_PATH is not None:
        return DEFAULT_LOG_PATH
    from sleeptalk.home import resolve_layout

    return resolve_layout().logs_dir / "voice_latency.jsonl"


def latency_logging_enabled() -> bool:
    """Profile Off overrides inherited trace destinations and stdout probes."""
    flag = os.getenv("SLEEPTALK_LATENCY_LOG_ENABLED", "").strip().lower()
    if flag:
        return flag in {"1", "true", "yes", "on"}
    return bool(os.getenv("VOICE_LATENCY_LOG_PATH", "").strip() or DEFAULT_LOG_PATH is not None)


# Timings can carry counts and identifiers, never arbitrary strings such as
# prompts, exceptions, tool arguments, transcripts, or provider response bodies.
_TIMING_LABELS = frozenset({
    "metric", "loggedAt", "traceId", "surface", "transport", "stage",
    "sessionId", "route", "agent", "agentName", "persona", "requestedAgentModel", "replyLength",
    "sttProvider", "sttModel", "ttsProvider", "ttsModel", "ttsVoiceId",
    "provider", "errorType", "code", "state", "status", "source", "sttSource",
    "architecture", "path", "dominantPath", "kind", "pool", "owner",
    "callProvider", "protocol", "localCandidateType", "remoteCandidateType",
    "measurement",
})
_CONTENT_FIELDS = frozenset({
    "text", "usertext", "assistanttext", "transcript", "prompt", "content",
    "message", "error", "audio", "audiobytes", "arguments", "args", "body",
    "payload", "token", "apikey", "secret", "password",
})


def _timing_fields(fields: dict[str, Any]) -> dict[str, Any]:
    from sleeptalk.logging_setup import redact

    result: dict[str, Any] = {}
    for key, value in fields.items():
        if key.lower().replace("_", "") in _CONTENT_FIELDS:
            continue
        if isinstance(value, (bool, int, float)):
            result[key] = value
        elif isinstance(value, str) and key in _TIMING_LABELS:
            result[key] = redact(value)[:160]
        elif isinstance(value, dict):
            result[key] = _timing_fields(value)
    return result


def _logged_at() -> str:
    return datetime.now(UTC).isoformat()


def _clean_fields(fields: dict[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for key, value in fields.items():
        if value is None:
            continue
        if isinstance(value, (str, int, float, bool)):
            clean[key] = value
            continue
        if isinstance(value, list):
            clean[key] = [_clean_value(item) for item in value]
            continue
        if isinstance(value, dict):
            clean[key] = {str(item_key): _clean_value(item_value) for item_key, item_value in value.items()}
            continue
        clean[key] = str(value)
    return clean


def _clean_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return [_clean_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _clean_value(item) for key, item in value.items()}
    return str(value)


def log_fallback_rate_decision(
    *,
    surface: str,
    transport: str,
    persona: str,
    architecture: str,
    path: str,
    trace_id: str | None = None,
    **fields: Any,
) -> dict[str, Any] | None:
    if not path:
        return None
    key = f"{surface}:{transport}:{persona}:{architecture}"
    window = _fallback_windows.setdefault(key, deque(maxlen=_FALLBACK_ALERT_WINDOW))
    window.append(path)
    if len(window) < _FALLBACK_ALERT_MIN_SAMPLES:
        return None
    counts = Counter(window)
    dominant_path, dominant_count = counts.most_common(1)[0]
    ratio = dominant_count / len(window)
    if ratio < _FALLBACK_ALERT_DOMINANCE:
        return None
    last_path, last_count = _fallback_last_alert.get(key, ("", 0))
    if last_path == dominant_path and last_count == len(window):
        return None
    _fallback_last_alert[key] = (dominant_path, len(window))
    return log_latency_event(
        surface=surface,
        transport=transport,
        persona=persona,
        stage="fallback_rate_warning",
        trace_id=trace_id,
        architecture=architecture,
        dominantPath=dominant_path,
        dominantRatio=round(ratio, 4),
        sampleCount=len(window),
        **fields,
    )


def log_chat_turn(
    *,
    surface: str,
    transport: str,
    persona: str,
    user_text: str,
    assistant_text: str,
    trace_id: str | None = None,
    session_id: str | None = None,
    channel_id: int | str | None = None,
    user_id: int | str | None = None,
    actor_id: str | None = None,
    enabled: bool = True,
    log_path: str | Path | None = None,
    **fields: Any,
) -> dict[str, Any]:
    del enabled, log_path
    event = {
        "metric": "voice_chat",
        "loggedAt": _logged_at(),
        "traceId": trace_id or uuid.uuid4().hex[:12],
        "surface": surface,
        "transport": transport,
        "persona": persona,
        "userText": user_text,
        "assistantText": assistant_text,
        "userChars": len(user_text),
        "assistantChars": len(assistant_text),
        "sessionId": session_id,
        "channelId": channel_id,
        "userId": user_id,
        "actorId": actor_id,
        **_clean_fields(fields),
    }
    event = {key: value for key, value in event.items() if value is not None}
    _emit_transcript_debug(event)
    return event


def _emit_transcript_debug(event: dict[str, Any]) -> None:
    if not _transcript_debug_enabled():
        return
    _emit_event(
        event,
        prefix="transcript_debug",
        path=_transcript_debug_log_path(),
        allow_stdout=False,
    )


def _transcript_debug_log_path() -> Path:
    configured = os.getenv("SLEEPTALK_TRANSCRIPT_DEBUG_PATH", "").strip()
    if configured:
        return Path(configured).expanduser()
    if DEFAULT_TRANSCRIPT_DEBUG_LOG_PATH is not None:
        return DEFAULT_TRANSCRIPT_DEBUG_LOG_PATH
    from sleeptalk.home import resolve_layout

    return resolve_layout().logs_dir / "transcript_debug.jsonl"


def _transcript_debug_enabled() -> bool:
    raw = os.getenv("SLEEPTALK_TRANSCRIPT_DEBUG", "").strip().lower()
    return raw not in {"", "0", "false", "no", "off"}
