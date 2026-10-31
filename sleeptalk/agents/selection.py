"""Deterministic gateway / native / ACP runtime selection.

Callers inject probe results. This module does not talk to the network.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class RuntimeKind(str, Enum):
    GATEWAY = "gateway"
    NATIVE = "native"
    ACP = "acp"


class ProbeStatus(str, Enum):
    REACHABLE = "reachable"
    UNAVAILABLE = "unavailable"
    UNAUTHENTICATED = "unauthenticated"
    INCOMPATIBLE = "incompatible"


class IncompatiblePolicy(str, Enum):
    FALLBACK = "fallback"
    FAIL = "fail"


_PIN_ALIASES = {
    "gateway": RuntimeKind.GATEWAY,
    "native": RuntimeKind.NATIVE,
    "sdk": RuntimeKind.NATIVE,
    "acp": RuntimeKind.ACP,
}

_POLICY_ALIASES = {
    "fallback": IncompatiblePolicy.FALLBACK,
    "fail": IncompatiblePolicy.FAIL,
    "error": IncompatiblePolicy.FAIL,
}

_ACP_PIN_TYPES = frozenset(
    {
        "acp",
        "hermes",
        "hermes-acp",
        "hermes_acp",
        "openclaw-acp",
        "openclaw_acp",
        "cursor_acp",
        "cursor-acp",
    }
)


class SelectionError(ValueError):
    pass


@dataclass(frozen=True)
class Surface:
    kind: RuntimeKind
    registration: str
    preserves_session: bool
    preserves_permissions: bool
    preserved_permission_modes: frozenset[str] = frozenset()


@dataclass(frozen=True)
class RuntimeFamily:
    name: str
    types: frozenset[str]
    gateway: Surface | None = None
    native: Surface | None = None
    acp: Surface | None = None


@dataclass(frozen=True)
class SelectionRequest:
    agent_type: str
    pin: RuntimeKind | None
    policy: IncompatiblePolicy
    gateway_configured: bool
    gateway_probe: ProbeStatus | None
    native_available: bool
    acp_available: bool
    session_id: str = ""
    permission_mode: str = ""


@dataclass(frozen=True)
class Selection:
    family: str
    kind: RuntimeKind
    registration: str
    reason: str
    fallback_from: RuntimeKind | None
    fallback_reason: str | None
    session_continuity: bool
    permission_continuity: bool
    diagnostics: tuple[str, ...]


FAMILIES: tuple[RuntimeFamily, ...] = (
    RuntimeFamily(
        name="pi",
        types=frozenset({"pi"}),
        native=Surface(RuntimeKind.NATIVE, "pi", True, True),
    ),
    RuntimeFamily(
        name="omp",
        types=frozenset({"omp"}),
        native=Surface(RuntimeKind.NATIVE, "omp", True, True),
    ),
    RuntimeFamily(
        name="hermes",
        types=frozenset(
            {"hermes_gateway", "hermes-gateway", "acp", "hermes", "hermes-acp", "hermes_acp"}
        ),
        gateway=Surface(RuntimeKind.GATEWAY, "hermes_gateway", True, True),
        acp=Surface(RuntimeKind.ACP, "acp", False, True),
    ),
    RuntimeFamily(
        name="openclaw",
        types=frozenset({"openclaw", "openclaw-acp", "openclaw_acp"}),
        gateway=Surface(RuntimeKind.GATEWAY, "openclaw", True, True),
        acp=Surface(RuntimeKind.ACP, "openclaw_acp", False, True),
    ),
    RuntimeFamily(
        name="claude_code",
        types=frozenset({"claude_code", "claude-code"}),
        native=Surface(RuntimeKind.NATIVE, "claude_code", True, True),
    ),
    RuntimeFamily(
        name="cursor",
        types=frozenset({"cursor", "cursor_acp", "cursor-acp"}),
        native=Surface(RuntimeKind.NATIVE, "cursor", True, False, frozenset({"deny"})),
        acp=Surface(RuntimeKind.ACP, "cursor_acp", True, True),
    ),
    RuntimeFamily(
        name="codex",
        types=frozenset({"codex", "codex_app_server", "codex-app-server"}),
        native=Surface(RuntimeKind.NATIVE, "codex", True, True),
    ),
)

_FAMILY_BY_TYPE = {alias: family for family in FAMILIES for alias in family.types}


def parse_pin(raw: str) -> RuntimeKind | None:
    key = raw.strip().lower()
    if not key:
        return None
    kind = _PIN_ALIASES.get(key)
    if kind is None:
        valid = ", ".join(sorted(_PIN_ALIASES))
        raise SelectionError(f"unknown runtime pin {raw!r}. Valid options: {valid}.")
    return kind


def parse_policy(raw: str) -> IncompatiblePolicy:
    key = (raw or "fallback").strip().lower() or "fallback"
    policy = _POLICY_ALIASES.get(key)
    if policy is None:
        valid = ", ".join(sorted(_POLICY_ALIASES))
        raise SelectionError(
            f"unknown on_incompatible {raw!r}. Valid options: {valid}."
        )
    return policy


def family_for(agent_type: str) -> RuntimeFamily:
    key = agent_type.strip().lower()
    family = _FAMILY_BY_TYPE.get(key)
    if family is None:
        valid = ", ".join(sorted(_FAMILY_BY_TYPE))
        raise SelectionError(f"unknown agent type {agent_type!r}. Valid options: {valid}.")
    return family


def family_for_optional(agent_type: str) -> RuntimeFamily | None:
    """Return the built-in policy family, or ``None`` for an extension."""

    return _FAMILY_BY_TYPE.get(agent_type.strip().lower())


def request_from_config(
    agent: Any,
    env: dict[str, str],
    *,
    gateway_probe: ProbeStatus | None = None,
    native_available: bool | None = None,
    acp_available: bool = True,
) -> SelectionRequest:
    agent_type = str(getattr(agent, "type", "") or "pi")
    family = family_for(agent_type)
    url = str(getattr(agent, "gateway_url", "") or "").strip()
    gateway_configured = bool(url) or (
        family.gateway is not None and agent_type not in _ACP_PIN_TYPES
    )
    pin = parse_pin(str(getattr(agent, "runtime", "") or ""))
    if native_available is None:
        if family.name == "cursor":
            native_available = pin is RuntimeKind.NATIVE or bool(
                env.get("CURSOR_API_KEY", "").strip()
            )
        else:
            native_available = family.native is not None
    if pin is None and agent_type in _ACP_PIN_TYPES:
        pin = RuntimeKind.ACP
    return SelectionRequest(
        agent_type=agent_type,
        pin=pin,
        policy=parse_policy(str(getattr(agent, "on_incompatible", "") or "fallback")),
        gateway_configured=gateway_configured,
        gateway_probe=gateway_probe,
        native_available=bool(native_available),
        acp_available=acp_available,
        session_id=str(getattr(agent, "gateway_session_id", "") or ""),
        permission_mode=str(getattr(agent, "permission_mode", "") or ""),
    )


def select_runtime(request: SelectionRequest) -> Selection:
    family = family_for(request.agent_type)
    if request.pin is not None:
        return _finish(request, family, *_pick_pinned(request, family))
    return _finish(request, family, *_walk_policy(request, family))


def doctor_lines(selection: Selection) -> list[str]:
    lines = [
        f"selected {selection.registration} ({selection.kind.value}) because {selection.reason}"
    ]
    if selection.fallback_from is not None:
        lines.append(
            f"fell back from {selection.fallback_from.value}: {selection.fallback_reason}"
        )
    lines.extend(selection.diagnostics)
    return lines


def _surface(family: RuntimeFamily, kind: RuntimeKind) -> Surface | None:
    if kind is RuntimeKind.GATEWAY:
        return family.gateway
    if kind is RuntimeKind.NATIVE:
        return family.native
    return family.acp


def _pick_pinned(
    request: SelectionRequest, family: RuntimeFamily
) -> tuple[Surface, str, RuntimeKind | None, str | None]:
    surface = _surface(family, request.pin)
    if surface is None:
        raise SelectionError(
            f"{family.name} has no {request.pin.value} runtime. "
            f"Pin another surface or change agent.type."
        )
    blocked = _blocked(request, surface)
    if blocked is None:
        return surface, f"agent.runtime pins {surface.kind.value}", None, None
    if request.policy is IncompatiblePolicy.FAIL:
        raise SelectionError(blocked)
    fallback, why = _next_available(request, family, exclude=surface.kind)
    return fallback, why, surface.kind, blocked


def _walk_policy(
    request: SelectionRequest, family: RuntimeFamily
) -> tuple[Surface, str, RuntimeKind | None, str | None]:
    if family.gateway is not None and request.gateway_configured:
        blocked = _blocked(request, family.gateway)
        if blocked is None:
            reason = "configured gateway is preferred"
            if request.gateway_probe is None:
                reason = "configured gateway is preferred (probe deferred)"
            return family.gateway, reason, None, None
        if request.policy is IncompatiblePolicy.FAIL:
            raise SelectionError(blocked)
        fallback, why = _next_available(request, family, exclude=RuntimeKind.GATEWAY)
        return fallback, why, RuntimeKind.GATEWAY, blocked
    if family.native is not None and request.native_available:
        return family.native, "named native surface is available", None, None
    if family.native is not None and not request.native_available:
        blocked = f"{family.name} native runtime is unavailable"
        if request.policy is IncompatiblePolicy.FAIL:
            raise SelectionError(blocked)
        fallback, why = _next_available(request, family, exclude=RuntimeKind.NATIVE)
        return fallback, why, RuntimeKind.NATIVE, blocked
    if family.acp is not None and request.acp_available:
        return family.acp, "documented ACP fallback", None, None
    raise SelectionError(f"no reachable runtime for {family.name}")


def _next_available(
    request: SelectionRequest,
    family: RuntimeFamily,
    *,
    exclude: RuntimeKind,
) -> tuple[Surface, str]:
    order = (RuntimeKind.NATIVE, RuntimeKind.ACP)
    for kind in order:
        if kind is exclude:
            continue
        surface = _surface(family, kind)
        if surface is None:
            continue
        if kind is RuntimeKind.NATIVE and not request.native_available:
            continue
        if kind is RuntimeKind.ACP and not request.acp_available:
            continue
        return surface, f"{kind.value} is the next documented runtime"
    if family.acp is not None and request.acp_available and exclude is not RuntimeKind.ACP:
        return family.acp, "documented ACP fallback"
    raise SelectionError(f"no fallback runtime for {family.name}")


def _blocked(request: SelectionRequest, surface: Surface) -> str | None:
    if surface.kind is RuntimeKind.GATEWAY:
        if not request.gateway_configured:
            return f"{surface.registration} gateway is not configured"
        if request.gateway_probe is ProbeStatus.UNAVAILABLE:
            return f"{surface.registration} gateway is unreachable"
        if request.gateway_probe is ProbeStatus.UNAUTHENTICATED:
            return f"{surface.registration} gateway is unauthenticated"
        if request.gateway_probe is ProbeStatus.INCOMPATIBLE:
            return f"{surface.registration} gateway is incompatible"
    if surface.kind is RuntimeKind.NATIVE and not request.native_available:
        return f"{surface.registration} native runtime is unavailable"
    if surface.kind is RuntimeKind.ACP and not request.acp_available:
        return f"{surface.registration} ACP runtime is unavailable"
    return None


def _finish(
    request: SelectionRequest,
    family: RuntimeFamily,
    surface: Surface,
    reason: str,
    fallback_from: RuntimeKind | None,
    fallback_reason: str | None,
) -> Selection:
    diagnostics: list[str] = []
    session_ok = not request.session_id or surface.preserves_session
    permission_ok = (not request.permission_mode or surface.preserves_permissions
                     or request.permission_mode in surface.preserved_permission_modes)
    if request.session_id and not surface.preserves_session:
        message = (
            f"session {request.session_id} cannot continue on {surface.registration}"
        )
        if request.policy is IncompatiblePolicy.FAIL:
            raise SelectionError(message)
        diagnostics.append(message)
    if not permission_ok:
        message = (
            f"permission_mode {request.permission_mode!r} is not preserved on "
            f"{surface.registration}"
        )
        if request.policy is IncompatiblePolicy.FAIL:
            raise SelectionError(message)
        diagnostics.append(message)
    return Selection(
        family=family.name,
        kind=surface.kind,
        registration=surface.registration,
        reason=reason,
        fallback_from=fallback_from,
        fallback_reason=fallback_reason,
        session_continuity=session_ok,
        permission_continuity=permission_ok,
        diagnostics=tuple(diagnostics),
    )
