from __future__ import annotations

from collections.abc import AsyncIterator, Callable

import pytest

from sleeptalk.adapter import AgentAdapter

from .cases import (
    case_abort_idle_is_safe,
    case_abort_mid_stream_stops,
    case_adapter_does_not_own_voice_policy,
    case_clarification,
    case_permission_types,
    case_permissions,
    case_steering,
    case_stream_yields_sentence,
    case_thinking_before_text,
    case_timeout_cancel_settles,
    case_tool_lifecycle,
    case_usage,
    case_voice_sessions_unavailable,
    case_warmup_then_close,
)
from .fakes import GatewayFake, LiarFake, NativeFake, make_acp_client
from .harness import PROMPT_HELLO, advertised, drain, require, skip_unless

Factory = Callable[[], AgentAdapter]


def _native() -> NativeFake:
    return NativeFake()


def _gateway() -> GatewayFake:
    return GatewayFake()


def _hermes_tui():
    from sleeptalk.agents.hermes_gateway_adapter import HermesGatewayClient
    from tests.fixtures.hermes_tui_fake import FakeTui

    fake = FakeTui()
    return HermesGatewayClient(link=fake.link, permission_mode="pending", session_id="s1")


def _pi_rpc():
    import sys
    from pathlib import Path

    from sleeptalk.agents.pi_rpc import PiRPCClient

    fake = Path(__file__).resolve().parents[1] / "fixtures" / "pi_rpc_fake.py"

    class FixturePi(PiRPCClient):
        async def _ensure_started(self) -> None:
            if self._proc is not None and self._proc.returncode is None:
                return
            await self._spawn([sys.executable, str(fake)])

    return FixturePi(cwd=".", no_session=True)


def _claude_sdk():
    from sleeptalk.agents.claude_code_adapter import ClaudeCodeClient
    from tests.fixtures.claude_session import ConformanceFakeSession

    return ClaudeCodeClient(
        cwd=".",
        permission_mode="pending",
        session=ConformanceFakeSession(),
    )


def _codex_app_server():
    import sys
    from pathlib import Path

    from sleeptalk.agents.codex_app_server import CodexAppServerClient

    fake = Path(__file__).resolve().parents[1] / "fixtures" / "codex_app_server_fake.py"

    class FixtureCodex(CodexAppServerClient):
        def _launch_args(self) -> list[str]:
            return [sys.executable, str(fake)]

    return FixtureCodex(cwd=".", permission_mode="pending")


def _openclaw_gateway():
    from sleeptalk.agents.openclaw_gateway_adapter import OpenClawGatewayClient
    from tests.fixtures.openclaw_gateway import FakeGatewaySession

    return OpenClawGatewayClient(
        session=FakeGatewaySession(),
        token="test-token",
        permission_mode="pending",
        session_key="s1",
    )


ADAPTERS: list[tuple[str, Factory]] = [
    ("native", _native),
    ("gateway", _gateway),
    ("acp", make_acp_client),
    ("hermes-tui", _hermes_tui),
    ("pi-rpc", _pi_rpc),
    ("claude-sdk", _claude_sdk),
    ("openclaw-gateway", _openclaw_gateway),
    ("codex-app-server", _codex_app_server),
]


@pytest.fixture(params=[name for name, _factory in ADAPTERS], ids=lambda name: name)
async def adapter(request: pytest.FixtureRequest) -> AsyncIterator[AgentAdapter]:
    factory = dict(ADAPTERS)[request.param]
    agent = factory()
    try:
        yield agent
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_warmup_then_close(adapter: AgentAdapter) -> None:
    await case_warmup_then_close(adapter)


@pytest.mark.asyncio
async def test_stream_yields_sentence(adapter: AgentAdapter) -> None:
    await case_stream_yields_sentence(adapter)


@pytest.mark.asyncio
async def test_abort_idle_is_safe(adapter: AgentAdapter) -> None:
    await case_abort_idle_is_safe(adapter)


@pytest.mark.asyncio
async def test_abort_mid_stream_stops_further_sentences(adapter: AgentAdapter) -> None:
    await case_abort_mid_stream_stops(adapter)


@pytest.mark.asyncio
async def test_thinking_before_text_when_both_occur(adapter: AgentAdapter) -> None:
    await case_thinking_before_text(adapter)


@pytest.mark.asyncio
async def test_tool_lifecycle(adapter: AgentAdapter) -> None:
    await case_tool_lifecycle(adapter)


@pytest.mark.asyncio
async def test_permissions(adapter: AgentAdapter) -> None:
    await case_permissions(adapter)


def test_permission_requested_has_no_approval_fields() -> None:
    case_permission_types()


@pytest.mark.asyncio
async def test_clarification(adapter: AgentAdapter) -> None:
    await case_clarification(adapter)


@pytest.mark.asyncio
async def test_steering(adapter: AgentAdapter) -> None:
    await case_steering(adapter)


@pytest.mark.asyncio
async def test_usage(adapter: AgentAdapter) -> None:
    await case_usage(adapter)


@pytest.mark.asyncio
async def test_voice_sessions_unavailable(adapter: AgentAdapter) -> None:
    await case_voice_sessions_unavailable(adapter)


@pytest.mark.asyncio
async def test_timeout_cancel_settles(adapter: AgentAdapter) -> None:
    await case_timeout_cancel_settles(adapter)


@pytest.mark.asyncio
async def test_adapters_do_not_own_voice_policy(adapter: AgentAdapter) -> None:
    events, sentences = await drain(adapter, PROMPT_HELLO)
    case_adapter_does_not_own_voice_policy(
        adapter, sentences=sentences, events=events
    )


def test_skip_unless_skips_unsupported() -> None:
    agent = NativeFake()
    with pytest.raises(pytest.skip.Exception, match="permissions is unsupported"):
        skip_unless(agent, "permissions")


def test_require_skips_unsupported_not_supported_miss() -> None:
    agent = NativeFake()
    with pytest.raises(pytest.skip.Exception, match="steering is unsupported"):
        require(agent, "steering")


@pytest.mark.asyncio
async def test_false_supported_permissions_fails() -> None:
    liar = LiarFake()
    assert advertised(liar).permissions.level.value == "supported"
    with pytest.raises(pytest.fail.Exception, match="permissions advertised SUPPORTED"):
        await case_permissions(liar)
