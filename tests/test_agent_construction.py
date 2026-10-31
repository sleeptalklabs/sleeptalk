from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from sleeptalk.agents.construction import build_agent, resolve_agent
from sleeptalk.agents.selection import ProbeStatus, RuntimeKind, SelectionError
from sleeptalk.config import AgentConfig
from sleeptalk.defaults import register_defaults
from sleeptalk.registries import AGENTS


class _Agent:
    def respond_streaming(self, text: str, *, on_event=None) -> AsyncIterator[str]:
        async def stream() -> AsyncIterator[str]:
            yield text

        return stream()

    async def respond(self, text: str, timeout: float = 300.0) -> str:
        return text

    async def abort(self) -> None:
        return None

    async def warmup(self) -> None:
        return None

    async def close(self) -> None:
        return None


def test_cursor_construction_uses_the_resolved_factory_once(monkeypatch) -> None:
    import sleeptalk.agents.construction as construction
    from sleeptalk.agents.cursor_sdk_adapter import CursorSdkClient

    calls = 0
    select = construction.select_runtime

    def counted(request):
        nonlocal calls
        calls += 1
        return select(request)

    monkeypatch.setattr(construction, "select_runtime", counted)
    agent, selection = build_agent(
        AgentConfig(type="cursor"), {"CURSOR_API_KEY": "test-key"}
    )
    assert isinstance(agent, CursorSdkClient)
    assert selection is not None and selection.kind is RuntimeKind.NATIVE
    assert calls == 1


def test_cursor_without_key_resolves_to_cursor_acp() -> None:
    from sleeptalk.agents.cursor_acp import CursorAcpClient

    agent, selection = build_agent(AgentConfig(type="cursor"), {})
    assert isinstance(agent, CursorAcpClient)
    assert selection is not None
    assert selection.registration == "cursor_acp"


def test_openclaw_fallback_uses_the_openclaw_command() -> None:
    from sleeptalk.agents.hermes_adapter import HermesACPClient

    agent, selection = build_agent(
        AgentConfig(type="openclaw"),
        {},
        gateway_probe=ProbeStatus.UNAVAILABLE,
    )
    assert isinstance(agent, HermesACPClient)
    assert agent.binary == "openclaw"
    assert selection is not None
    assert selection.registration == "openclaw_acp"


def test_claude_acp_pin_never_constructs_hermes() -> None:
    with pytest.raises(SelectionError, match="has no acp runtime"):
        build_agent(AgentConfig(type="claude_code", runtime="acp"), {})


def test_custom_agent_bypasses_builtin_family_policy() -> None:
    register_defaults()
    AGENTS.register("construction-test-agent", lambda section, env: _Agent())
    construction = resolve_agent(AgentConfig(type="construction-test-agent"), {})
    assert construction.registration.name == "construction-test-agent"
    assert construction.selection is None
    agent, selection = build_agent(AgentConfig(type="construction-test-agent"), {})
    assert isinstance(agent, _Agent)
    assert selection is None


def test_factory_result_must_implement_core_turn_contract() -> None:
    register_defaults()
    AGENTS.register("construction-test-invalid", lambda section, env: object())
    with pytest.raises(TypeError, match="respond_streaming"):
        build_agent(AgentConfig(type="construction-test-invalid"), {})
