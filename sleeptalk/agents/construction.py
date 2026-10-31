"""Resolve and construct one agent through the selected registration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sleeptalk.agents.selection import (
    ProbeStatus,
    Selection,
    family_for_optional,
    request_from_config,
    select_runtime,
)

if TYPE_CHECKING:
    from sleeptalk.registries import Registration


_CORE_METHODS = ("respond_streaming", "respond", "abort", "warmup", "close")


@dataclass(frozen=True)
class AgentConstruction:
    """The registration chosen for a section and its built-in policy result."""

    registration: Registration
    selection: Selection | None


def resolve_agent(
    section: Any,
    env: dict[str, str],
    *,
    gateway_probe: ProbeStatus | None = None,
) -> AgentConstruction:
    """Resolve the factory once without constructing clients or probing binaries."""

    from sleeptalk.defaults import register_defaults
    from sleeptalk.registries import AGENTS

    register_defaults()
    requested = str(getattr(section, "type", "") or "pi")
    if family_for_optional(requested) is None:
        return AgentConstruction(registration=AGENTS.get(requested), selection=None)

    selection = select_runtime(
        request_from_config(section, env, gateway_probe=gateway_probe)
    )
    return AgentConstruction(
        registration=AGENTS.get(selection.registration),
        selection=selection,
    )


def build_agent(
    section: Any,
    env: dict[str, str],
    *,
    gateway_probe: ProbeStatus | None = None,
) -> tuple[Any, Selection | None]:
    """Build the resolved agent and reject adapters that cannot run a turn."""

    from sleeptalk.agent_auth import resolve_agent_auth_env

    env = resolve_agent_auth_env(section, env)
    construction = resolve_agent(section, env, gateway_probe=gateway_probe)
    agent = construction.registration.factory(section, env)
    validate_agent_contract(agent, registration=construction.registration.name)
    return agent, construction.selection


def validate_agent_contract(agent: Any, *, registration: str) -> None:
    """Fail during assembly when a registration does not implement the public contract."""

    missing = [name for name in _CORE_METHODS if not callable(getattr(agent, name, None))]
    if missing:
        methods = ", ".join(missing)
        raise TypeError(
            f"agent registration {registration!r} returned {type(agent).__name__}, "
            f"which is missing callable methods: {methods}"
        )
