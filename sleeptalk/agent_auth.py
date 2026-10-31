"""Explicit, endpoint-scoped model credential selection."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class AgentAuthError(ValueError):
    """A value-free failure resolving a selected model credential."""


class AgentAuthChoice(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    mode: Literal["api_key", "oauth"]
    protocol: Literal["openai-completions", "openai-responses", "openai-codex-responses", "anthropic-messages", "cursor"]
    base_url: str = Field(min_length=1, max_length=2048)

    @field_validator("mode")
    @classmethod
    def api_keys_only(cls, value: str) -> str:
        from sleeptalk.agents.provider_auth import SUBSCRIPTION_REFUSAL
        if value == "oauth":
            raise ValueError(SUBSCRIPTION_REFUSAL)
        return value

    @field_validator("base_url")
    @classmethod
    def endpoint(cls, value: str) -> str:
        from sleeptalk.agents.provider_auth import canonical_endpoint
        return canonical_endpoint(value)


class AgentAuthConfig(AgentAuthChoice):
    credential_ref: str = Field(pattern=r"^credential:[0-9a-f]{32}$")


def validate_model_auth_labels(model: str, provider: str) -> None:
    for value, limit in ((model, 512), (provider, 128)):
        if not isinstance(value, str) or not 1 <= len(value) <= limit or re.search(r"[\x00-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]", value):
            raise ValueError("Select a bounded model and provider without control or bidirectional formatting characters.")


def auth_metadata(auth: AgentAuthChoice) -> dict[str, str]:
    return auth.model_dump(exclude={"credential_ref"})


def auth_scope(auth: AgentAuthChoice) -> str:
    encoded = json.dumps(auth_metadata(auth), sort_keys=True, separators=(",", ":")).encode()
    return "agent-" + hashlib.sha256(encoded).hexdigest()[:56]


def resolve_agent_auth_env(agent, environment: dict[str, str], *, store=None) -> dict[str, str]:
    """Resolve one explicit reference; never fall back to an ambient credential."""
    result = {key: value for key, value in environment.items()
              if key not in {"SLEEPTALK_PROVIDER_AUTH", "SLEEPTALK_PROVIDER_CREDENTIAL"}}
    if getattr(agent, "auth", None) is None:
        return result
    # Only agents that consume a model credential get one; any other agent's
    # shell tools could read it from their environment.
    from sleeptalk.defaults import register_defaults
    from sleeptalk.registries import AGENTS

    register_defaults()
    registration = AGENTS.get_optional(str(getattr(agent, "type", "") or ""))
    if registration is None or "auth" not in (registration.config_fields or ()):
        raise AgentAuthError("This agent doesn't use [agent.auth]; remove it from the profile so its tools never see the key.")
    from sleeptalk.credential_store import CredentialError, CredentialStore

    try:
        selected = store if store is not None else CredentialStore()
        status = selected.status(agent.auth.credential_ref)
        if status.provider != auth_scope(agent.auth):
            raise AgentAuthError("The selected model credential belongs to a different authentication endpoint or protocol.")
        credential = selected.get(agent.auth.credential_ref)
    except CredentialError:
        raise AgentAuthError("The selected model credential is unavailable. Unlock or repair it privately.") from None
    result["SLEEPTALK_PROVIDER_AUTH"] = json.dumps(auth_metadata(agent.auth), sort_keys=True, separators=(",", ":"))
    result["SLEEPTALK_PROVIDER_CREDENTIAL"] = credential
    return result
