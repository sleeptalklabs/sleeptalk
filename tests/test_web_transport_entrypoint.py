from contextlib import asynccontextmanager
import asyncio
from types import SimpleNamespace

import pytest

from sleeptalk.builder import build_session
from sleeptalk.call_provider import CallContext
from sleeptalk.config import AgentConfig, SleeptalkConfig, TransportConfig
from sleeptalk.defaults import register_defaults
from sleeptalk.registries import AGENTS
from sleeptalk.transports.web import WebTransport
from sleeptalk import web_call


async def test_registered_web_transport_runs_the_same_assembled_session(monkeypatch):
    config = SleeptalkConfig(transport=TransportConfig(type="web", options={"local": True}))
    session = build_session(config, env={})
    assert isinstance(session.transport, WebTransport)
    calls = []
    provider = object()

    @asynccontextmanager
    async def local_provider():
        yield provider

    async def run_web_session(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr("sleeptalk.local_rendezvous.LocalCallProvider", local_provider)
    monkeypatch.setattr(web_call, "run_web_session", run_web_session)
    monkeypatch.setattr(web_call, "build_session", lambda *_: pytest.fail("web rebuilt the session"))
    await session.run()
    assert len(calls) == 1
    assert calls[0]["runtime"] is session
    assert calls[0]["provider"] is provider
    assert calls[0]["ttl_seconds"] == 300
    assert session.state.value == "closed"


async def test_shared_web_runner_closes_existing_session_when_mint_fails():
    calls = []

    async def prepare():
        calls.append("prepare")

    async def close():
        calls.append("close")

    async def mint(*args, **kwargs):
        raise RuntimeError("ticket service unavailable")

    with pytest.raises(RuntimeError, match="ticket service unavailable"):
        await web_call.run_web_session(
            runtime=SimpleNamespace(prepare=prepare, aclose=close),
            config=SleeptalkConfig(),
            context=CallContext("agent", "conversation", "user"),
            ttl_seconds=300,
            on_ready=lambda *_: pytest.fail("link announced before mint succeeded"),
            provider=SimpleNamespace(mint=mint),
        )
    assert calls == ["prepare", "close"]


def test_registered_agent_owns_conversation_context(monkeypatch):
    register_defaults()
    monkeypatch.setattr(AGENTS, "_registrations", dict(AGENTS._registrations))

    def continuation(agent, conversation_id):
        agent.gateway_session_id = conversation_id or "saved-custom-session"
        return agent.gateway_session_id

    AGENTS.register("custom-context", lambda *_: None, conversation_context=continuation)
    config = SleeptalkConfig(agent=AgentConfig(type="custom-context"))
    result = web_call._context_for_profile(config, CallContext("agent", "", "user"))
    assert result.conversation_id == "saved-custom-session"
    assert config.agent.gateway_session_id == "saved-custom-session"


def test_browser_context_uses_selected_runtime_instead_of_requested_family():
    config = SleeptalkConfig(agent=AgentConfig(type="hermes_gateway", runtime="acp"))
    result = web_call._context_for_profile(config, CallContext("agent", "", "user"), env={})
    assert result.conversation_id.endswith(":new-web-call")
    assert config.agent.gateway_session_id == ""


async def test_cancelled_web_prepare_closes_the_existing_session():
    started = asyncio.Event()
    closed = []

    async def prepare():
        started.set()
        await asyncio.Future()

    async def close():
        closed.append(True)

    task = asyncio.create_task(web_call.run_web_session(
        runtime=SimpleNamespace(prepare=prepare, aclose=close),
        config=SleeptalkConfig(), context=CallContext("agent", "conversation", "user"),
        ttl_seconds=300, on_ready=lambda *_: None, provider=object(),
    ))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed == [True]


async def test_web_close_failure_still_drains_telemetry(monkeypatch):
    telemetry_started = asyncio.Event()
    telemetry_closed = []

    async def noop():
        pass

    async def telemetry(*args):
        telemetry_started.set()
        try:
            await asyncio.Future()
        finally:
            telemetry_closed.append(True)

    async def recv():
        await telemetry_started.wait()
        raise web_call.CallEnded("done")

    async def mint(*args, **kwargs):
        return SimpleNamespace(token="fixture", join_url="https://fixture.invalid")

    @asynccontextmanager
    async def connect(ticket):
        yield SimpleNamespace(recv=recv)

    class BrowserSession:
        def __init__(self, **kwargs):
            pass

        async def feed_audio_frame(self, frame):
            pass

        async def interrupt_presentation(self, **kwargs):
            pass

        async def close(self):
            raise RuntimeError("close failed")

    monkeypatch.setattr(web_call, "WebCallSession", BrowserSession)
    monkeypatch.setattr(web_call, "_record_browser_telemetry", telemetry)
    with pytest.raises(RuntimeError, match="close failed"):
        await web_call.run_web_session(
            runtime=SimpleNamespace(prepare=noop, aclose=noop), config=SleeptalkConfig(),
            context=CallContext("agent", "conversation", "user"), ttl_seconds=300,
            on_ready=lambda *_: None, provider=SimpleNamespace(mint=mint, connect=connect),
        )
    assert telemetry_closed == [True]
