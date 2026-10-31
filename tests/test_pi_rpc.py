from __future__ import annotations

import sys
from pathlib import Path

import pytest

from sleeptalk.adapter import (
    ControlAccepted,
    SessionIdentified,
    Steer,
    SupportLevel,
    WorkerEvidence,
    adapter_capabilities,
    adapter_control,
    typed_events,
)
from sleeptalk.agents.pi_rpc import PiRPCClient
from tests.agent_conformance.cases import (
    case_adapter_does_not_own_voice_policy,
    case_steering,
    case_stream_yields_sentence,
    case_tool_lifecycle,
    case_usage,
)

FAKE = Path(__file__).with_name("fixtures") / "pi_rpc_fake.py"


class FixturePi(PiRPCClient):
    async def _ensure_started(self) -> None:
        if self._proc is not None and self._proc.returncode is None:
            return
        if self._closed:
            from sleeptalk.agents.pi_rpc import PiRPCError

            raise PiRPCError("Pi RPC client is closed.")
        await self._spawn([sys.executable, str(FAKE)])


@pytest.mark.asyncio
async def test_session_identity_is_reported_without_warmup():
    client, events = FixturePi(cwd="."), []
    try:
        async for _ in client.respond_streaming("hello", on_event=typed_events(events.append)):
            pass
        identities = [e for e in events if isinstance(e, SessionIdentified)]
        assert identities and identities[0].upstream_session_id == "pi-session-1"
    finally:
        await client.close()


def test_new_upstream_session_is_announced_again():
    client = PiRPCClient()
    events = []
    client._record_identity({"sessionId": "one"})
    client._emit_identity(typed_events(events.append))
    client._record_identity({"sessionId": "one"})
    client._emit_identity(typed_events(events.append))
    client._record_identity({"sessionId": "two"})
    client._emit_identity(typed_events(events.append))
    assert [e.upstream_session_id for e in events] == ["one", "two"]


@pytest.mark.asyncio
async def test_fake_streams_and_steers() -> None:
    client = FixturePi(cwd=".", no_session=True)
    try:
        result = await client.respond("hello")
        assert "First sentence." in result
        steered = await adapter_control(client, Steer(instruction="be brief"))
        assert isinstance(steered, ControlAccepted)
        await case_usage(client)
        await case_tool_lifecycle(client)
        await case_steering(client)
        await case_stream_yields_sentence(client)
    finally:
        await client.close()


def test_manifest_is_honest() -> None:
    manifest = adapter_capabilities(PiRPCClient())
    assert manifest.steering.level is SupportLevel.SUPPORTED
    assert manifest.usage.level is SupportLevel.SUPPORTED
    assert manifest.permissions.level is SupportLevel.UNSUPPORTED
    assert manifest.workers.level is SupportLevel.UNSUPPORTED
    assert manifest.workers.evidence is WorkerEvidence.NONE


@pytest.mark.asyncio
async def test_stock_pi_task_tool_stays_a_tool() -> None:
    seen: list[object] = []
    client = FixturePi(cwd=".", no_session=True)
    try:
        async for _ in client.respond_streaming(
            "task-child", on_event=typed_events(seen.append)
        ):
            pass
    finally:
        await client.close()
    workers = [item for item in seen if getattr(item, "kind", None) == "worker"]
    tools = [item for item in seen if getattr(item, "kind", None) == "tool"]
    assert workers == []
    assert [item.name for item in tools if item.name] == ["task", "task"]


def test_adapter_does_not_own_voice_policy() -> None:
    case_adapter_does_not_own_voice_policy(
        PiRPCClient(), sentences=["First sentence."], events=[]
    )
