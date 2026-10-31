from __future__ import annotations

import asyncio

import pytest
from sleeptalk.agents.pi_rpc import PiRPCClient, PiRPCError


class ControlledPi(PiRPCClient):
    def __init__(self):
        super().__init__()
        self.busy = False
        self.sent: list[dict] = []
        self.prompts: asyncio.Queue[str] = asyncio.Queue()
        self.state_reads: asyncio.Queue[bool] = asyncio.Queue()

    async def _ensure_started(self):
        pass

    async def _send_raw(self, command):
        self.sent.append(command)
        if command["type"] == "get_state":
            await self.state_reads.put(self.busy)
            await self._route_message(
                {
                    "type": "response",
                    "id": command["id"],
                    "success": True,
                    "data": {"isStreaming": self.busy, "sessionId": "session"},
                }
            )
        elif command["type"] == "prompt":
            assert "streamingBehavior" not in command
            assert not self.busy
            self.busy = True
            await self._route_message(
                {
                    "type": "response",
                    "id": command["id"],
                    "success": True,
                }
            )
            await self._route_message({"type": "agent_start"})
            await self.prompts.put(command["message"])

    async def finish(self, text):
        await self._route_message(
            {
                "type": "message_update",
                "assistantMessageEvent": {"type": "text_delta", "delta": text},
            }
        )
        await self._route_message(
            {"type": "agent_end", "messages": [{"role": "assistant", "content": text}]}
        )
        self.busy = False
        await self._route_message({"type": "agent_settled"})


@pytest.mark.asyncio
async def test_concurrent_requests_keep_their_own_output():
    client = ControlledPi()
    first = asyncio.create_task(client.respond("first"))
    assert await client.prompts.get() == "first"
    second = asyncio.create_task(client.respond("second"))
    await asyncio.sleep(0)
    assert client.prompts.empty()
    await client.finish("First result.")
    assert await first == "First result."
    assert await client.prompts.get() == "second"
    await client.finish("Second result.")
    assert await second == "Second result."


@pytest.mark.asyncio
async def test_timed_out_warmup_must_settle_before_next_request():
    client = ControlledPi()
    client.warmup_timeout = 0.01
    warmup = asyncio.create_task(client.warmup())
    assert await client.prompts.get() == "Say OK."
    with pytest.raises(PiRPCError, match="timed out|exceeded maximum time"):
        await warmup
    next_request = asyncio.create_task(client.respond("real request"))
    while not await client.state_reads.get():
        pass
    assert client.prompts.empty()
    await client.finish("Old warmup answer.")
    assert await client.prompts.get() == "real request"
    await client.finish("Relevant answer.")
    assert await next_request == "Relevant answer."


@pytest.mark.asyncio
async def test_cancelling_queued_request_does_not_abort_active_request():
    client = ControlledPi()
    first = asyncio.create_task(client.respond("first"))
    await client.prompts.get()
    queued = asyncio.create_task(client.respond("cancel me"))
    await asyncio.sleep(0)
    queued.cancel()
    with pytest.raises(asyncio.CancelledError):
        await queued
    await client.finish("Still running correctly.")
    assert await first == "Still running correctly."
    assert [cmd["type"] for cmd in client.sent].count("prompt") == 1
    assert not any(cmd["type"] == "abort" for cmd in client.sent)


@pytest.mark.asyncio
async def test_queue_wait_is_bounded_without_sending_or_aborting():
    client = ControlledPi()
    first = asyncio.create_task(client.respond("first"))
    await client.prompts.get()
    with pytest.raises(PiRPCError, match="waiting for the active request"):
        await client.respond("expired", queue_timeout=0.01)
    await client.finish("First result.")
    assert await first == "First result."
    assert [cmd["type"] for cmd in client.sent].count("prompt") == 1


@pytest.mark.asyncio
async def test_unknown_busy_work_is_waited_out_without_adopting_its_answer():
    client = ControlledPi()
    client.busy = True
    request = asyncio.create_task(client.respond("new request"))
    assert await client.state_reads.get() is True
    assert client.prompts.empty()
    await client.finish("Someone else's answer.")
    assert await client.prompts.get() == "new request"
    await client.finish("My answer.")
    assert await request == "My answer."


@pytest.mark.asyncio
async def test_warmup_does_not_queue_behind_an_active_request():
    client = ControlledPi()
    first = asyncio.create_task(client.respond("first"))
    await client.prompts.get()
    with pytest.raises(PiRPCError, match="warmup skipped"):
        await client.warmup()
    await client.finish("First result.")
    assert await first == "First result."
    assert [cmd["type"] for cmd in client.sent].count("prompt") == 1


@pytest.mark.asyncio
async def test_busy_admission_timeout_does_not_send_or_abort():
    client = ControlledPi()
    client.busy = True
    with pytest.raises(PiRPCError, match="waiting for previous work to settle"):
        await client.respond("expired", queue_timeout=0.01)
    assert all(cmd["type"] == "get_state" for cmd in client.sent)


@pytest.mark.asyncio
async def test_preflight_failure_does_not_yield_an_unrelated_run():
    class RacedPi(ControlledPi):
        async def _send_raw(self, command):
            if command["type"] != "prompt":
                return await super()._send_raw(command)
            await self._route_message({"type": "agent_start"})
            await self._route_message(
                {
                    "type": "agent_end",
                    "messages": [{"role": "assistant", "content": "Unrelated answer."}],
                }
            )
            await self._route_message(
                {
                    "type": "response",
                    "id": command["id"],
                    "success": False,
                    "error": "Agent is already processing",
                }
            )

    client = RacedPi()
    output = []
    with pytest.raises(PiRPCError, match="already processing"):
        async for sentence in client.respond_streaming("new request"):
            output.append(sentence)
    assert output == []


@pytest.mark.asyncio
async def test_accepted_extension_run_replays_output_emitted_before_prompt_ack():
    class ExtensionPi(ControlledPi):
        async def _send_raw(self, command):
            if command["type"] != "prompt":
                return await super()._send_raw(command)
            await self._route_message({"type": "agent_start"})
            await self._route_message({
                "type": "message_update",
                "assistantMessageEvent": {"type": "text_delta", "delta": "Extension result."},
            })
            await self._route_message({"type": "agent_end", "messages": []})
            await self._route_message({"type": "agent_settled"})
            await self._route_message({
                "type": "response", "id": command["id"], "success": True,
            })

    client = ExtensionPi()
    assert await client.respond("/extension", timeout=0.1) == "Extension result."


@pytest.mark.asyncio
async def test_preflight_output_waits_for_correlated_ack_and_precedes_live_output():
    class DelayedAckPi(ControlledPi):
        async def _send_raw(self, command):
            if command["type"] != "prompt":
                return await super()._send_raw(command)
            self.prompt_id = command["id"]
            await self._route_message({"type": "agent_start"})
            await self._route_message({
                "type": "message_update",
                "assistantMessageEvent": {"type": "text_delta", "delta": "Before ack. "},
            })
            await self.prompts.put(command["message"])

    client = DelayedAckPi()
    output = []

    async def consume():
        async for sentence in client.respond_streaming("extension"):
            output.append(sentence)

    request = asyncio.create_task(consume())
    await client.prompts.get()
    await client._route_message({"type": "response", "id": "another", "success": True})
    await asyncio.sleep(0)
    assert output == []
    await client._route_message({"type": "response", "id": client.prompt_id, "success": True})
    await client.finish("After ack.")
    await request
    assert " ".join(output) == "Before ack. After ack."


@pytest.mark.asyncio
async def test_aborting_while_waiting_for_external_work_does_not_abort_it():
    from types import SimpleNamespace

    client = ControlledPi()
    client._proc = SimpleNamespace(returncode=None)
    client.busy = True
    request = asyncio.create_task(client.respond("queued"))
    await client.state_reads.get()
    await client.abort()
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    assert all(cmd["type"] == "get_state" for cmd in client.sent)
