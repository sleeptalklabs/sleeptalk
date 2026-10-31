"""OpenClaw agent support: registration, builder wiring, gateway client.

Voice talks to the Gateway WebSocket. `openclaw acp` is fallback only.
These tests pin registration shape, builder mapping, and binary skip
when a gateway URL is set.
"""

import shutil

import pytest

from sleeptalk.config import AgentConfig
from sleeptalk.defaults import register_defaults
from sleeptalk.registries import AGENTS
from sleeptalk.agents.hermes_adapter import HermesACPClient, HermesACPError
from sleeptalk.agents.openclaw_gateway_adapter import (
    DEFAULT_GATEWAY_URL,
    OpenClawGatewayClient,
)


def _registered():
    register_defaults()
    return AGENTS.get("openclaw")


def test_openclaw_registered_with_aliases_and_install():
    registration = _registered()
    assert registration.name == "openclaw"
    assert AGENTS.canonical_name("openclaw-acp") == "openclaw_acp"
    assert AGENTS.canonical_name("openclaw_acp") == "openclaw_acp"
    # Wizard detection hinges on binary_for; install is always manual
    # (install_hint is the whole remediation path — no auto-install).
    assert registration.binary_for(AgentConfig()) == "openclaw"
    assert "openclaw onboard" in registration.install_hint


def test_build_openclaw_acp_alias_uses_openclaw_command():
    register_defaults()
    agent = AGENTS.build(
        "openclaw-acp",
        AgentConfig(type="openclaw-acp", slug="lobster"),
        {},
    )
    assert isinstance(agent, HermesACPClient)
    assert agent.binary == "openclaw"
    assert agent.name == "lobster"


def test_build_openclaw_agent_uses_gateway_client():
    register_defaults()
    agent = AGENTS.build(
        "openclaw",
        AgentConfig(type="openclaw", slug="lobster"),
        {"OPENCLAW_GATEWAY_TOKEN": "secret"},
    )
    assert isinstance(agent, OpenClawGatewayClient)
    assert agent.url == DEFAULT_GATEWAY_URL
    assert agent.token == "secret"
    assert agent.name == "lobster"
    assert agent.permission_mode == "auto"


def test_build_openclaw_agent_respects_permission_mode():
    register_defaults()
    agent = AGENTS.build(
        "openclaw", AgentConfig(type="openclaw", permission_mode="deny"), {}
    )
    assert agent.permission_mode == "deny"


@pytest.mark.asyncio
async def test_missing_openclaw_binary_raises_actionable_error(tmp_path, monkeypatch):
    # The wizard can `npm i -g openclaw` onto the dev machine, so a real
    # openclaw binary may be on PATH — force the missing-binary path or
    # respond() spawns `openclaw acp` and hangs on the gateway handshake.
    monkeypatch.setattr(shutil, "which", lambda _name, path=None: None)
    agent = HermesACPClient(
        cwd=str(tmp_path),
        binary="openclaw",
        binary_fallback="",
        install_hint="openclaw binary not found. Install: npm i -g openclaw",
    )
    with pytest.raises(HermesACPError, match="npm i -g openclaw"):
        await agent.respond("hello")
    await agent.close()
