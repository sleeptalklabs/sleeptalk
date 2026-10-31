from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from sleeptalk.agents.hermes_adapter import HermesACPClient, HermesACPError
from sleeptalk.agents.pi_rpc import PiRPCClient, PiRPCError
from sleeptalk.config import AgentConfig
from sleeptalk.defaults import _build_acp_agent, _build_omp_agent, _build_pi_agent
from tests.test_pi_rpc_ownership import ControlledPi


def test_voice_config_leaves_thinking_to_the_harness():
    assert "thinking_level" not in AgentConfig.model_fields
    with pytest.raises(ValidationError):
        AgentConfig(thinking_level="low")


@pytest.mark.asyncio
@pytest.mark.parametrize("factory", [_build_pi_agent, _build_omp_agent])
async def test_voice_launch_preserves_harness_thinking(monkeypatch, factory):
    client = factory(AgentConfig(), {})
    monkeypatch.setattr(client, "_resolve_binary", lambda value: value)
    monkeypatch.setattr(client, "_check_binary", lambda value: None)
    spawn = AsyncMock()
    monkeypatch.setattr(client, "_spawn", spawn)
    rpc = AsyncMock(return_value={})
    monkeypatch.setattr(client, "_rpc", rpc)
    await client._ensure_started()
    assert "--thinking" not in spawn.call_args.args[0]
    assert all(call.args[0]["type"] != "set_thinking_level" for call in rpc.call_args_list)


class SettlingPi(ControlledPi):
    def __init__(self):
        super().__init__()
        self._proc = SimpleNamespace(returncode=None)

    async def _send_raw(self, command):
        if command["type"] == "abort":
            self.sent.append(command)
            self.busy = False
            await self._route_message({"type": "agent_settled"})
        else:
            await super()._send_raw(command)


@pytest.mark.asyncio
async def test_pi_warmup_timeout_aborts_owned_work_before_first_turn():
    client = SettlingPi()
    client.warmup_timeout = 0.01
    warmup = asyncio.create_task(client.warmup())
    assert await client.prompts.get() == "Say OK."
    with pytest.raises(PiRPCError, match="timed out|exceeded maximum time"):
        await warmup
    assert [cmd["type"] for cmd in client.sent].count("abort") == 1
    answer = asyncio.create_task(client.respond("user request"))
    assert await asyncio.wait_for(client.prompts.get(), 0.5) == "user request"
    await client.finish("Ready for you.")
    assert await answer == "Ready for you."


@pytest.mark.asyncio
async def test_pi_admission_and_prompt_markers_are_ordered():
    client = SettlingPi()
    seen = []
    async def consume():
        return [s async for s in client.respond_streaming("private prompt", on_event=seen.append)]
    response = asyncio.create_task(consume())
    await client.prompts.get()
    await client.finish("Answer.")
    await response
    records = [json.loads(e.removeprefix("workbench:")) for e in seen if e.startswith("workbench:")]
    latency = [record for record in records if record["kind"] == "latency"]
    assert [record["stage"] for record in latency] == ["agent_admission_started", "agent_admitted", "agent_prompt_sent"]
    assert "private prompt" not in json.dumps(latency)


def test_pi_identity_reports_effective_settings_without_other_state():
    client = PiRPCClient(model="configured", provider="configured-provider")
    assert client.latency_identity()["agentModel"] is None
    client._record_identity({"model": {"id": "effective", "provider": "actual", "apiKey": "secret"}, "thinkingLevel": "minimal", "messages": ["private"]})
    identity = client.latency_identity()
    assert identity["agentModel"] == "effective"
    assert identity["agentProvider"] == "actual"
    assert identity["agentThinkingLevel"] == "minimal"
    assert identity["agentThinkingLevelStatus"] == "observed"
    assert "secret" not in json.dumps(identity)
    assert "private" not in json.dumps(identity)


class ControlledHermes(HermesACPClient):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._proc = SimpleNamespace(returncode=None)
        self._session_id = "session"
        self.sent = []
        self.prompts = asyncio.Queue()

    async def _ensure_started(self):
        pass

    async def _write_command(self, message):
        self.sent.append(message)
        if message.get("method") == "session/prompt":
            await self.prompts.put(message)
        elif message.get("method") == "session/cancel":
            self._active_prompt.set_result({"stopReason": "cancelled"})

    def _start_idle_drain(self):
        pass


@pytest.mark.asyncio
async def test_hermes_abort_can_run_while_prompt_owns_lock():
    client = ControlledHermes()
    turn = asyncio.create_task(client.respond("user request"))
    await client.prompts.get()
    assert client._lock.locked()
    await asyncio.wait_for(client.abort(), timeout=0.1)
    assert isinstance(await asyncio.wait_for(turn, timeout=0.1), str)
    assert [item["method"] for item in client.sent] == ["session/prompt", "session/cancel"]
    await client.abort()
    assert len(client.sent) == 2


@pytest.mark.asyncio
async def test_hermes_cancelling_queued_request_does_not_cancel_owner():
    client = ControlledHermes()
    owner = asyncio.create_task(client.respond("owner"))
    await client.prompts.get()
    queued = asyncio.create_task(client.respond("queued"))
    await asyncio.sleep(0)
    queued.cancel()
    with pytest.raises(asyncio.CancelledError):
        await queued
    assert len(client.sent) == 1
    with pytest.raises(HermesACPError, match="warmup skipped"):
        await client.warmup()
    await client.abort()
    await owner


@pytest.mark.asyncio
async def test_hermes_cancelled_prompt_settles_before_next_request():
    client = ControlledHermes()
    owner = asyncio.create_task(client.respond("owner"))
    await client.prompts.get()
    owner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await owner
    assert client._active_prompt is None
    assert client._pending == {}
    next_turn = asyncio.create_task(client.respond("next"))
    assert (await asyncio.wait_for(client.prompts.get(), 0.1))["params"]["prompt"][0]["text"] == "next"
    await client.abort()
    await next_turn


@pytest.mark.asyncio
async def test_hermes_delayed_abort_cannot_cancel_a_new_owner():
    client = ControlledHermes()
    client._active_prompt = asyncio.get_running_loop().create_future()
    await client._command_lock.acquire()
    cancellation = asyncio.create_task(client.abort())
    await asyncio.sleep(0)
    client._active_prompt = asyncio.get_running_loop().create_future()
    client._command_lock.release()
    await cancellation
    assert client.sent == []


@pytest.mark.parametrize("level", ["high", "low"])
def test_hermes_observes_harness_thinking_without_setting(level):
    client = _build_acp_agent(AgentConfig(), {})
    client._request = AsyncMock()
    client._record_session_thinking(None)
    assert client.latency_identity()["agentThinkingLevelStatus"] == "unknown"
    client._record_session_thinking([{
        "id": "reasoning", "type": "select", "category": "thought_level",
        "currentValue": level, "options": [{"value": "low"}, {"value": "high"}],
    }])
    client._request.assert_not_called()
    assert client.latency_identity()["agentThinkingLevel"] == level
    assert client.latency_identity()["agentThinkingLevelStatus"] == "observed"
