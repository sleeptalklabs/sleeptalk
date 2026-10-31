"""Read-only onboarding discovery and prerequisite resolution.

Caller metadata is a claim, never probe evidence. This module neither loads
profiles or credentials nor starts an adapter, network connection, or device.
"""

from __future__ import annotations

import os
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from sleeptalk.receipts import SCHEMA_VERSION


class State(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


class Capability(StrEnum):
    EXECUTION = "command_execution"
    HARNESS = "installing_harness_identity"
    WORKSPACE = "workspace_access"
    HOST_BINDING = "runtime_host_binding"
    ADAPTER = "adapter_supported"
    PROTOCOL = "adapter_protocol"
    LOGIN = "adapter_login"
    PERMISSIONS = "adapter_permissions"
    RUNTIME_WORKSPACE = "adapter_workspace"
    HANDOFF = "session_handoff"
    OPERATOR = "operator_binding"
    HUMAN_TERMINAL = "human_terminal"
    LOCAL_INPUT = "local_private_input"
    REMOTE_INPUT = "remote_private_input"
    USER_TO_RUNTIME = "user_to_runtime"
    INVITATION = "private_invitation_delivery"
    HOST_KEY = "host_key_binding"
    DEVICES = "audio_devices"
    AUDIO_CONTROL = "audio_permission_control"
    HTTPS = "secure_audio_page"
    MEDIA = "media_path"
    BOT = "discord_bot"
    PARTICIPANTS = "discord_participants"
    CHANNEL = "discord_channel"
    PROVIDERS = "provider_connectivity"
    SPEECH = "speech_readiness"
    LIFETIME = "call_process_lifetime"
    STORAGE = "durable_storage"
    SERVICE = "service_support"
    PERSISTENCE = "persistence_consent"


Endpoint = Literal[
    "execution_host", "intended_runtime", "workspace", "user_device",
    "installing_harness", "voice_runtime", "tool_hosts", "operator",
    "speech_providers", "discord",
]
Surface = Literal["terminal", "web", "discord"]
Status = Literal["ready", "needs_input", "failed"]
Evidence = Literal[
    "not_probed", "current_process", "directory_access", "registration",
    "not_implemented", "integration_probe",
]


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Context(ContractModel):
    """Non-secret integration claims. Free text is never copied to receipts."""

    schema_version: Literal[1] = 1
    app_label: str = Field(default="", max_length=128)
    harness: str = Field(default="", max_length=128)
    harness_version: str = Field(default="", max_length=128)
    claims: dict[Capability, State] = Field(default_factory=dict)


class Intent(ContractModel):
    workspace: str | None = Field(default=None, min_length=1, max_length=4096)
    agent: str | None = Field(default=None, min_length=1, max_length=128)
    surface: Surface | None = None
    preferred_surface: Surface | None = None
    session: Literal["new", "resume"] = "new"
    persistence: bool = False


class Fact(ContractModel):
    capability: Capability
    state: State = State.UNKNOWN
    evidence: Evidence = "not_probed"
    scope: tuple[Endpoint, ...]
    lifetime: Literal["invocation"] = "invocation"
    claim: State | None = None


# Each prerequisite names the endpoints whose relationship needs verification.
_SCOPES: dict[Capability, tuple[Endpoint, ...]] = {
    Capability.EXECUTION: ("execution_host",),
    Capability.HARNESS: ("installing_harness", "tool_hosts"),
    Capability.WORKSPACE: ("execution_host", "workspace"),
    Capability.HOST_BINDING: ("execution_host", "intended_runtime"),
    Capability.ADAPTER: ("voice_runtime",),
    Capability.PROTOCOL: ("intended_runtime", "voice_runtime"),
    Capability.LOGIN: ("voice_runtime",),
    Capability.PERMISSIONS: ("installing_harness", "voice_runtime", "tool_hosts"),
    Capability.RUNTIME_WORKSPACE: ("voice_runtime", "workspace"),
    Capability.HANDOFF: ("installing_harness", "voice_runtime"),
    Capability.OPERATOR: ("operator", "intended_runtime"),
    Capability.HUMAN_TERMINAL: ("user_device", "execution_host"),
    Capability.LOCAL_INPUT: ("user_device", "intended_runtime"),
    Capability.REMOTE_INPUT: ("user_device", "intended_runtime"),
    Capability.USER_TO_RUNTIME: ("user_device", "intended_runtime"),
    Capability.INVITATION: ("operator", "user_device", "installing_harness"),
    Capability.HOST_KEY: ("user_device", "intended_runtime"),
    Capability.DEVICES: ("user_device", "intended_runtime"),
    Capability.AUDIO_CONTROL: ("operator", "user_device"),
    Capability.HTTPS: ("user_device",),
    Capability.MEDIA: ("user_device", "intended_runtime"),
    Capability.BOT: ("intended_runtime", "discord"),
    Capability.PARTICIPANTS: ("operator", "discord"),
    Capability.CHANNEL: ("discord",),
    Capability.PROVIDERS: ("intended_runtime", "speech_providers"),
    Capability.SPEECH: ("intended_runtime", "speech_providers"),
    Capability.LIFETIME: ("intended_runtime",),
    Capability.STORAGE: ("intended_runtime",),
    Capability.SERVICE: ("intended_runtime",),
    Capability.PERSISTENCE: ("operator", "intended_runtime"),
}

_NEXT: dict[Capability, str] = {
    Capability.EXECUTION: "Provide permitted command execution on the intended host.",
    Capability.HARNESS: "Verify the installing harness through its integration protocol; an app label is insufficient.",
    Capability.WORKSPACE: "Select an accessible workspace directory on the intended host.",
    Capability.HOST_BINDING: "Verify that command execution is on the intended runtime host.",
    Capability.ADAPTER: "Select a registered voice adapter explicitly.",
    Capability.PROTOCOL: "Verify the selected adapter protocol without starting user work.",
    Capability.LOGIN: "Verify the selected runtime login through its private authentication flow.",
    Capability.PERMISSIONS: "Verify that the voice runtime preserves the requested permission behavior.",
    Capability.RUNTIME_WORKSPACE: "Verify that the selected adapter accesses the intended workspace.",
    Capability.HANDOFF: "Verify the requested session identity and exclusive ownership before resuming.",
    Capability.OPERATOR: "Pair through an independently authenticated operator channel; display names and a second code in the same chat are insufficient.",
    Capability.HUMAN_TERMINAL: "Verify that the user directly controls a private terminal on the runtime host.",
    Capability.LOCAL_INPUT: "Use host input start only when the user controls a local browser on the runtime host; discovery does not verify that browser.",
    Capability.REMOTE_INPUT: "Verify a paired operator, the selected host relay connection, and a private browser invitation with host remote.",
    Capability.USER_TO_RUNTIME: "Verify that the user's device can reach the runtime input endpoint.",
    Capability.INVITATION: "Verify private invitation delivery to the authorized operator.",
    Capability.HOST_KEY: "Verify the destination host key through authenticated pairing.",
    Capability.DEVICES: "Verify microphone and speaker access on the selected audio endpoint.",
    Capability.AUDIO_CONTROL: "Verify that the user can grant and revoke audio access.",
    Capability.HTTPS: "Verify a user-reachable secure browser page with microphone support.",
    Capability.MEDIA: "Verify the audio connection between the user device and runtime.",
    Capability.BOT: "Validate the selected Discord bot privately.",
    Capability.PARTICIPANTS: "Verify authorization for every speaker and listener in the selected channel.",
    Capability.CHANNEL: "Verify the selected Discord channel and bot permissions.",
    Capability.PROVIDERS: "Verify bounded connections from the runtime to the selected speech providers.",
    Capability.SPEECH: "Validate speech credentials privately after runtime and input viability checks.",
    Capability.LIFETIME: "Verify that the call can survive the installing agent's turn ending.",
    Capability.STORAGE: "Verify durable private storage on the intended host.",
    Capability.SERVICE: "Verify service support and access under the intended service account.",
    Capability.PERSISTENCE: "Ask whether the user wants continued operation before enabling persistence.",
}

_RUNTIME = (
    Capability.EXECUTION, Capability.WORKSPACE, Capability.HOST_BINDING,
    Capability.ADAPTER, Capability.PROTOCOL, Capability.LOGIN,
    Capability.PERMISSIONS, Capability.RUNTIME_WORKSPACE,
)
_AUDIO: dict[Surface, tuple[Capability, ...]] = {
    "terminal": (Capability.DEVICES, Capability.AUDIO_CONTROL, Capability.HUMAN_TERMINAL, Capability.LIFETIME),
    "web": (Capability.HTTPS, Capability.AUDIO_CONTROL, Capability.MEDIA, Capability.LIFETIME),
    "discord": (Capability.BOT, Capability.OPERATOR, Capability.PARTICIPANTS, Capability.CHANNEL, Capability.LIFETIME),
}
_INPUT = {
    "private_terminal": (Capability.HUMAN_TERMINAL, Capability.HOST_BINDING),
    "local_private_input": (Capability.LOCAL_INPUT, Capability.USER_TO_RUNTIME, Capability.OPERATOR),
    "remote_private_input": (Capability.REMOTE_INPUT, Capability.OPERATOR, Capability.INVITATION, Capability.HOST_KEY),
}


class Requirement(ContractModel):
    capability: Capability
    state: State
    next_action: str


class Operation(ContractModel):
    name: str
    status: Status
    requirements: tuple[Requirement, ...]


class Plan(ContractModel):
    status: Status
    surface: Surface | None
    decisions: tuple[Literal["workspace", "voice_runtime", "surface"], ...]
    operations: tuple[Operation, ...]
    audio_options: tuple[Operation, ...]
    input_options: tuple[Operation, ...]


def resolve_plan(intent: Intent, facts: tuple[Fact, ...]) -> Plan:
    """Resolve only verified prerequisites; never execute the resulting plan."""
    by_name = {fact.capability: fact.state for fact in facts}

    def operation(name: str, prerequisites: tuple[Capability, ...]) -> Operation:
        missing = tuple(
            Requirement(
                capability=key, state=by_name.get(key, State.UNKNOWN),
                next_action=_NEXT[key],
            )
            for key in prerequisites if by_name.get(key) != State.AVAILABLE
        )
        if any(r.state == State.UNAVAILABLE for r in missing):
            status = "failed"
        else:
            status = "needs_input" if missing else "ready"
        return Operation(name=name, status=status, requirements=missing)

    audio = tuple(operation(name, keys) for name, keys in _AUDIO.items())
    inputs = tuple(operation(name, keys) for name, keys in _INPUT.items())
    surface = intent.surface
    if surface is None:
        viable = [option.name for option in audio if option.status == "ready"]
        if intent.preferred_surface in viable:
            surface = intent.preferred_surface
        elif len(viable) == 1 and all(option.status != "needs_input" for option in audio):
            surface = viable[0]
    decisions = []
    if intent.workspace is None:
        decisions.append("workspace")
    if intent.agent is None:
        decisions.append("voice_runtime")
    if surface is None and sum(o.status != "failed" for o in audio) > 1:
        decisions.append("surface")
    runtime = _RUNTIME + ((Capability.HANDOFF,) if intent.session == "resume" else ())
    operations = [operation("connect_runtime", runtime)]
    if by_name.get(Capability.SPEECH) != State.AVAILABLE:
        # Alternatives are reported separately; unavailable entry paths do not
        # block an available private human terminal.
        if any(o.status == "ready" for o in inputs):
            entry_status = "ready"
        elif all(o.status == "failed" for o in inputs):
            entry_status = "failed"
        else:
            entry_status = "needs_input"
        connection = operations[0]
        if connection.status == "failed" or entry_status == "failed":
            entry_status = "failed"
        elif connection.status == "needs_input":
            entry_status = "needs_input"
        operations.append(Operation(
            name="collect_speech", status=entry_status,
            requirements=connection.requirements,
        ))
    if surface:
        operations.append(operation(
            "start_audio",
            runtime + _AUDIO[surface] + (Capability.PROVIDERS, Capability.SPEECH),
        ))
    else:
        operations.append(Operation(
            name="select_audio",
            status="failed" if all(o.status == "failed" for o in audio) else "needs_input",
            requirements=(),
        ))
    if intent.persistence:
        operations.append(operation(
            "enable_persistence",
            runtime + (Capability.SERVICE, Capability.STORAGE, Capability.PERSISTENCE),
        ))
    if any(o.status == "failed" for o in operations):
        status = "failed"
    elif decisions or any(o.status == "needs_input" for o in operations):
        status = "needs_input"
    else:
        status = "ready"
    return Plan(
        status=status, surface=surface, decisions=tuple(decisions),
        operations=tuple(operations), audio_options=audio, input_options=inputs,
    )


class Candidate(ContractModel):
    name: str
    binary: State


class DiscoveryReceipt(ContractModel):
    schema_version: Literal[1] = SCHEMA_VERSION
    discovery_version: Literal[1] = 1
    operation: Literal["discover"] = "discover"
    status: Status
    endpoints: tuple[Endpoint, ...]
    supported_operations: tuple[str, ...] = ("discover", "inventory", "bootstrap", "probe_runtime", "private_input", "remote_pairing", "remote_setup", "remote_status", "credentials", "ensure", "start_voice", "call_status", "list", "stop", "reset_access")
    candidates: tuple[Candidate, ...]
    selected_agent: str | None
    facts: tuple[Fact, ...]
    plan: Plan
    continuity: Literal["unverified"] = "unverified"
    context_received: bool


class DiscoveryErrorReceipt(ContractModel):
    schema_version: Literal[1] = SCHEMA_VERSION
    operation: Literal["discover"] = "discover"
    status: Literal["error"] = "error"
    code: Literal["config"] = "config"
    error: str


def discover(intent: Intent, context: Context | None = None) -> DiscoveryReceipt:
    from sleeptalk.config import AgentConfig
    from sleeptalk.defaults import register_defaults
    from sleeptalk.registries import AGENTS

    register_defaults()
    registrations = AGENTS.registrations()
    selected = next(
        (r for r in registrations if intent.agent in (r.name, *r.aliases)), None,
    )
    candidates = []
    for registration in registrations:
        section = AgentConfig(type=registration.name)
        if not registration.binary_for(section):
            binary = State.UNKNOWN
        else:
            binary = State.AVAILABLE if registration.find_binary(section) else State.UNAVAILABLE
        candidates.append(Candidate(name=registration.name, binary=binary))
    facts = {
        key: Fact(
            capability=key, scope=scope,
            claim=context.claims.get(key) if context else None,
        )
        for key, scope in _SCOPES.items()
    }

    def record(key: Capability, state: State, evidence: Evidence) -> None:
        facts[key] = Fact(
            capability=key, state=state, evidence=evidence,
            scope=_SCOPES[key], claim=facts[key].claim,
        )

    record(Capability.EXECUTION, State.AVAILABLE, "current_process")
    if intent.workspace is not None:
        try:
            workspace = Path(intent.workspace).expanduser().resolve(strict=True)
            accessible = workspace.is_dir() and os.access(workspace, os.R_OK | os.X_OK)
        except (OSError, RuntimeError, ValueError):
            accessible = False
        record(Capability.WORKSPACE, State.AVAILABLE if accessible else State.UNAVAILABLE, "directory_access")
    if intent.agent is not None:
        record(Capability.ADAPTER, State.AVAILABLE if selected else State.UNAVAILABLE, "registration")
    record(Capability.LOCAL_INPUT, State.UNKNOWN, "not_probed")
    record(Capability.REMOTE_INPUT, State.UNKNOWN, "not_probed")
    all_facts = tuple(facts.values())
    plan = resolve_plan(intent, all_facts)
    return DiscoveryReceipt(
        status=plan.status,
        endpoints=tuple(dict.fromkeys(
            endpoint for scope in _SCOPES.values() for endpoint in scope
        )),
        candidates=tuple(candidates), selected_agent=selected.name if selected else None,
        facts=all_facts, plan=plan, context_received=context is not None,
    )


def contract_schema() -> dict:
    """Generated schemas match the installed package's contract models."""
    return {
        "discovery_version": 1,
        "context": Context.model_json_schema(),
        "intent": Intent.model_json_schema(),
        "receipt": TypeAdapter(DiscoveryReceipt | DiscoveryErrorReceipt).json_schema(),
    }
