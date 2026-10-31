"""Resolve where an agent-triggered voice session should be delivered.

Caller integrations may supply their context. Sleeptalk turns that semantic
fact into a Terminal session or a remote browser link. Explicit human Web
starts use the package-owned local browser path instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, Mapping

CALLER_CONTEXT_ENV = "SLEEPTALK_CALLER_CONTEXT"


class CallerKind(StrEnum):
    LOCAL = "local"
    CHAT = "chat"
    UNKNOWN = "unknown"


class VoiceDelivery(StrEnum):
    TERMINAL = "terminal"
    LOCAL_WEB = "local_web"
    REMOTE_WEB = "remote_web"


@dataclass(frozen=True, slots=True)
class CallerContext:
    kind: CallerKind
    source: Literal["argument", "environment", "default"]


@dataclass(frozen=True, slots=True)
class VoiceLaunchPlan:
    delivery: VoiceDelivery
    structured: bool


def _caller_kind(value: str) -> CallerKind:
    normalized = value.strip().lower()
    if normalized in {"local", "terminal", "desktop"}:
        return CallerKind.LOCAL
    if normalized == "unknown":
        return CallerKind.UNKNOWN
    return CallerKind.CHAT


def resolve_caller_context(
    *,
    explicit: str | None,
    env: Mapping[str, str],
) -> CallerContext:
    """Parse caller metadata without maintaining a list of chat products."""
    if explicit and explicit.strip():
        return CallerContext(_caller_kind(explicit), "argument")
    configured = env.get(CALLER_CONTEXT_ENV, "").strip()
    if configured:
        return CallerContext(_caller_kind(configured), "environment")
    return CallerContext(CallerKind.CHAT, "default")


def delivery_for(context: CallerContext) -> VoiceDelivery:
    """Choose delivery from caller context without a network fallback."""
    if context.kind is CallerKind.CHAT:
        return VoiceDelivery.REMOTE_WEB
    return VoiceDelivery.TERMINAL


def launch_plan_for(
    *,
    surface: Literal["web", "terminal"] | None,
    caller: CallerContext,
    rendezvous_override: str | None,
    json_requested: bool,
) -> VoiceLaunchPlan:
    """Resolve delivery and stdout format once at the CLI boundary."""
    if surface == "terminal":
        return VoiceLaunchPlan(VoiceDelivery.TERMINAL, json_requested)
    if surface == "web":
        named_chat = caller.kind is CallerKind.CHAT and caller.source != "default"
        remote = bool(rendezvous_override) or named_chat
        return VoiceLaunchPlan(
            VoiceDelivery.REMOTE_WEB if remote else VoiceDelivery.LOCAL_WEB,
            json_requested or named_chat,
        )
    delivery = delivery_for(caller)
    return VoiceLaunchPlan(
        delivery,
        json_requested or delivery is VoiceDelivery.REMOTE_WEB,
    )


def surface_for(context: CallerContext) -> Literal["terminal", "web"]:
    return "terminal" if delivery_for(context) is VoiceDelivery.TERMINAL else "web"
