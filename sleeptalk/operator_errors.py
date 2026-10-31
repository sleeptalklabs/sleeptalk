"""Error codes for Sleeptalk's local operator commands."""

from __future__ import annotations

from typing import Literal, cast

ErrorCode = Literal[
    "unconfigured",
    "busy",
    "profile_not_found",
    "config",
    "provider",
    "timeout",
    "cancelled",
    "not_found",
    "internal",
]

ERROR_CODES = frozenset(
    {
        "unconfigured",
        "busy",
        "profile_not_found",
        "config",
        "provider",
        "timeout",
        "cancelled",
        "not_found",
        "internal",
    }
)


def validate_error_code(code: str) -> ErrorCode:
    if code not in ERROR_CODES:
        raise ValueError(f"Unknown host error code: {code!r}")
    return cast(ErrorCode, code)


def error_code_for(exc: BaseException, *, fallback: ErrorCode) -> ErrorCode:
    code = getattr(exc, "code", fallback)
    return validate_error_code(str(code))
