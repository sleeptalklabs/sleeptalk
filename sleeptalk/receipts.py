"""Versioned, chat-safe host CLI receipts.

Stdout for ``sleeptalk host … --json`` is always a redacted receipt dict.
Secrets, local paths, and process IDs never belong in public receipts.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

from sleeptalk.operator_errors import ErrorCode, validate_error_code

SCHEMA_VERSION = 1

STATUS_READY = "ready"
STATUS_NEEDS_INPUT = "needs_input"
STATUS_CONFLICT = "conflict"
STATUS_FAILED = "failed"
STATUS_STARTED = "started"
STATUS_ERROR = "error"
STATUS_LISTED = "listed"
STATUS_STOPPED = "stopped"
STATUS_OBSERVED = "observed"

OPERATION_ENSURE = "ensure"
OPERATION_START_VOICE = "start_voice"
OPERATION_LIST = "list"
OPERATION_STOP = "stop"
OPERATION_CALL_STATUS = "call_status"

# Keys whose values must never leave the machine in a public receipt.
_SECRET_KEY_FRAGMENTS = (
    "token",
    "secret",
    "password",
    "passwd",
    "api_key",
    "apikey",
    "access_key",
    "private_key",
    "credential",
    "pin",
    "auth",
    "authorization",
    "bearer",
    "ice",
    "turn",
    "install",
)

_SECRET_KEYS = frozenset(
    {
        "token",
        "bot_token",
        "discord_token",
        "api_key",
        "apiKey",
        "access_key",
        "agent_access_key",
        "agentAccessKey",
        "credential",
        "credentials",
        "password",
        "secret",
        "pin",
        "voice_pin",
        "voicePin",
        "authorization",
        "Authorization",
        "auth",
        "ice_servers",
        "iceServers",
        "turn_username",
        "turn_credential",
        "turnUsername",
        "turnCredential",
        "username",  # TURN username often paired with credential
        "pid",
        "process_id",
        "processId",
        "ready_file",
        "readyFile",
        "log_path",
        "logPath",
        "config_path",
        "configPath",
        "path",
        "local_path",
        "localPath",
        "workspace_dir",
        "workspaceDir",
        "cwd",
        "home",
        "SLEEPTALK_HOME",
    }
)

_PATH_VALUE = re.compile(
    r"(?:^|[\s\"'=])(/?(?:Users|home|tmp|var|private|workspaces?)/[^\s\"']+|"
    r"[A-Za-z]:\\[^\s\"']+|"
    r"\.(?:/|\\)[^\s\"']+)",
)
_ENV_KEY_NAME = re.compile(r"\b([A-Z][A-Z0-9_]{2,})\b")


def ensure_ready_receipt(
    *,
    profile: str,
    surface: str,
    route: str,
    agent: str,
    agent_name: str = "",
) -> dict[str, Any]:
    receipt: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "status": STATUS_READY,
        "operation": OPERATION_ENSURE,
        "profile": profile,
        "surface": surface,
        "route": route,
        "agent": agent,
    }
    if agent_name:
        receipt["agent_name"] = agent_name
    return redact_public_receipt(receipt)


def ensure_needs_input_receipt(
    *,
    profile: str,
    surface: str,
    route: str,
    agent: str,
    missing: list[str],
    remediation: str = "",
) -> dict[str, Any]:
    receipt: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "status": STATUS_NEEDS_INPUT,
        "operation": OPERATION_ENSURE,
        "profile": profile,
        "surface": surface,
        "route": route,
        "agent": agent,
        "missing": list(missing),
    }
    if remediation:
        receipt["remediation"] = remediation
    return redact_public_receipt(receipt)


def ensure_conflict_receipt(
    *,
    profile: str,
    surface: str,
    route: str,
    agent: str,
    conflicts: list[str],
) -> dict[str, Any]:
    """Report requested axes that disagree with an existing profile."""
    return redact_public_receipt(
        {
            "schema_version": SCHEMA_VERSION,
            "status": STATUS_CONFLICT,
            "operation": OPERATION_ENSURE,
            "profile": profile,
            "surface": surface,
            "route": route,
            "agent": agent,
            "conflicts": list(conflicts),
            "remediation": (
                "Choose a different profile or update the existing profile explicitly."
            ),
        }
    )


def ensure_failed_receipt(
    *,
    profile: str,
    surface: str,
    route: str,
    agent: str,
    message: str = "Local setup could not be completed.",
    code: ErrorCode = "internal",
) -> dict[str, Any]:
    """Report an operational setup failure without exposing local details."""
    return redact_public_receipt(
        {
            "schema_version": SCHEMA_VERSION,
            "status": STATUS_FAILED,
            "operation": OPERATION_ENSURE,
            "profile": profile,
            "surface": surface,
            "route": route,
            "agent": agent,
            "code": validate_error_code(code),
            "error": message,
        }
    )


def start_voice_receipt(
    *,
    surface: str,
    join_url: str,
    expires_at: str,
    agent_type: str,
    agent_slug: str,
    agent_name: str,
    continuity: str,
    call_id: str,
    profile: str = "",
) -> dict[str, Any]:
    receipt: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "status": STATUS_STARTED,
        "operation": OPERATION_START_VOICE,
        "surface": surface,
        "join_url": join_url,
        "expires_at": expires_at,
        "agent": {
            "type": agent_type,
            "slug": agent_slug,
            "name": agent_name,
        },
        "continuity": continuity,
        "callId": call_id,
    }
    if profile:
        receipt["profile"] = profile
    return redact_public_receipt(receipt)



def call_progress_receipt(
    progress: Mapping[str, Any], *, operation: str = OPERATION_CALL_STATUS,
) -> dict[str, Any]:
    """Project saved observations without exposing the recoverable capability."""
    from datetime import datetime, timezone

    def timestamp(value: float) -> str:
        return datetime.fromtimestamp(value, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    return redact_public_receipt({
        "schema_version": SCHEMA_VERSION,
        "status": STATUS_OBSERVED,
        "operation": operation,
        "callId": progress["callId"],
        "profile": progress["profile"],
        "call_state": progress["state"],
        "expires_at": timestamp(progress["expires_at"]),
        "observations": [
            {"stage": stage, "observed_at": timestamp(at)}
            for stage, at in progress["observations"].items()
        ],
        "audio_acceptance": "unknown",
    })


def start_request_conflict_receipt() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": STATUS_CONFLICT,
        "operation": OPERATION_START_VOICE,
        "code": "config",
        "conflicts": ["request_options"],
        "remediation": "Retry with the original call options, or use a new request ID for a new call.",
    }

def list_calls_receipt(calls: list[dict[str, Any]]) -> dict[str, Any]:
    return redact_public_receipt(
        {
            "schema_version": SCHEMA_VERSION,
            "status": STATUS_LISTED,
            "operation": OPERATION_LIST,
            "calls": calls,
        }
    )


def stop_call_receipt(
    *, call_id: str, profile: str, forced: bool
) -> dict[str, Any]:
    return redact_public_receipt(
        {
            "schema_version": SCHEMA_VERSION,
            "status": STATUS_STOPPED,
            "operation": OPERATION_STOP,
            "callId": call_id,
            "profile": profile,
            "forced": forced,
        }
    )


def error_receipt(
    *, operation: str, code: ErrorCode, message: str
) -> dict[str, Any]:
    return redact_public_receipt(
        {
            "schema_version": SCHEMA_VERSION,
            "status": STATUS_ERROR,
            "operation": operation,
            "code": validate_error_code(code),
            "error": message,
        }
    )


def missing_names_from_problems(problems: list[str]) -> list[str]:
    """Extract env/field names from doctor/config problem lines (never values)."""
    names: list[str] = []
    seen: set[str] = set()
    binary_name = re.compile(r"^'([^']+)' binary\b")
    for problem in problems:
        text = str(problem)
        binary = binary_name.match(text)
        if binary:
            head = binary.group(1)
            if head not in seen:
                seen.add(head)
                names.append(head)
            continue
        # Prefer "VAR missing — …" shape from collect_missing_env.
        if " missing" in text:
            head = text.split(" missing", 1)[0].strip()
            # Drop surrounding quotes from binary problems like "'pi' binary…"
            head = head.strip("'\"")
            if head and head not in seen:
                seen.add(head)
                names.append(head)
                continue
        for match in _ENV_KEY_NAME.findall(text):
            if match in {"OK", "FAIL", "WARN", "SKIP", "PATH"}:
                continue
            if match not in seen:
                seen.add(match)
                names.append(match)
    return names


def redact_public_receipt(value: Any) -> Any:
    """Strip secrets, local paths, and process IDs from any receipt-shaped value."""
    return _redact(value, parent_key="")


def _redact(value: Any, *, parent_key: str) -> Any:
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, item in value.items():
            key_str = str(key)
            if _is_secret_key(key_str):
                continue
            redacted = _redact(item, parent_key=key_str)
            if redacted is _DROP:
                continue
            out[key_str] = redacted
        return out
    if isinstance(value, list):
        return [
            item
            for item in (_redact(v, parent_key=parent_key) for v in value)
            if item is not _DROP
        ]
    if isinstance(value, tuple):
        return [
            item
            for item in (_redact(v, parent_key=parent_key) for v in value)
            if item is not _DROP
        ]
    if isinstance(value, int) and parent_key.lower() in {"pid", "process_id", "processid"}:
        return _DROP
    if isinstance(value, str):
        return _redact_string(value, parent_key=parent_key)
    return value


class _DropType:
    pass


_DROP = _DropType()


def _is_secret_key(key: str) -> bool:
    if key in _SECRET_KEYS:
        return True
    lowered = key.lower().replace("-", "_")
    if lowered in {k.lower() for k in _SECRET_KEYS}:
        return True
    # join_url is intentionally public; other *_url keys with auth fragments drop.
    if lowered == "join_url":
        return False
    return any(fragment in lowered for fragment in _SECRET_KEY_FRAGMENTS)


def _redact_string(value: str, *, parent_key: str) -> Any:
    lowered_key = parent_key.lower()
    if lowered_key in {"pid", "process_id", "processid"}:
        return _DROP
    if lowered_key.endswith("_path") or lowered_key in {"path", "cwd", "home"}:
        return _DROP
    # Keep join URLs; scrub accidental local file paths elsewhere.
    if lowered_key in {"join_url", "url"} and "://" in value:
        return value
    if _PATH_VALUE.search(value) and "://" not in value:
        return "<redacted-path>"
    return value
