from __future__ import annotations

import asyncio
import base64
from dataclasses import replace
from types import SimpleNamespace

import pytest
from sleeptalk.openai_realtime import SPECIALIST_TOOL
from sleeptalk.routes.base import RouteInput
from sleeptalk.routes.hybrid import HybridRoute
from sleeptalk.routes.hybrid_types import DeliveryPhase, HybridRoleContract
from sleeptalk.session_events import SessionEventType

from tests.test_hybrid_route import FakeAgent, FakeOutput, _context
from tests.test_openai_realtime import _manual_provider, _wait_sent


class SnapshotOutput(FakeOutput):
    def accounting(self):
        return replace(self._accounting)


async def _spoken_response(client, response_id: str, text: str) -> None:
    await client._handle_event({
        "type": "response.created", "response": {"id": response_id},
    })
    await client._handle_event({
        "type": "response.output_audio_transcript.done", "response_id": response_id,
        "transcript": text,
    })
    await client._handle_event({
        "type": "response.output_audio.delta", "response_id": response_id,
        "item_id": f"audio-{response_id}", "content_index": 0,
        "delta": base64.b64encode(b"\0\0" * 240).decode(),
    })
    await client._handle_event({
        "type": "response.done", "response": {"id": response_id, "status": "completed"},
    })


@pytest.mark.asyncio
@pytest.mark.parametrize("snapshot_accounting", [False, True])
async def test_tool_only_wire_response_dispatches_once_then_delivers_ack_and_result(snapshot_accounting: bool) -> None:
    provider, client, websocket = await _manual_provider(tools=True)
    calls = []
    agent = FakeAgent()
    agent.wait = True
    route = HybridRoute(
        role_contract=HybridRoleContract("Sleeptalk"), agent=agent,
        realtime=provider, audio_output=(SnapshotOutput if snapshot_accounting else FakeOutput)(calls),
    )
    await route.prepare(SimpleNamespace(session_id="test", emit=lambda event: None))
    turn = asyncio.create_task(route.run_turn(
        RouteInput(text="How are you?"), _context(calls, 1),
    ))
    try:
        async with asyncio.timeout(2):
            await _wait_sent(websocket, "response.create")
            await client._handle_event({
                "type": "response.created", "response": {"id": "tool-response"},
            })
            tool = {
                "type": "response.function_call_arguments.done", "response_id": "tool-response",
                "call_id": "read-call", "name": SPECIALIST_TOOL,
                "arguments": '{"question":"Read README and summarize it."}',
            }
            await client._handle_event(tool)
            await client._handle_event(tool)
            await client._handle_event({
                "type": "response.done",
                "response": {"id": "tool-response", "status": "completed"},
            })
            await _wait_sent(websocket, "response.create", 2)
            assert websocket.sent[-1]["response"]["tool_choice"] == "none"
            await _spoken_response(client, "ack", "I will check that.")
            outcome = await turn
            await agent.started.wait()
            assert outcome.status == "completed"
            assert outcome.played
            assert outcome.pending_task_id == route.tasks[0].task_id.value
            assert len(agent.calls) == 1
            assert "Read README and summarize it." in agent.calls[0]
            assert len([event for event in route.reducer.events if event.kind == "intent.committed"]) == 1
            outputs = [event for event in websocket.sent
                       if event.get("item", {}).get("type") == "function_call_output"]
            assert len(outputs) == 1
            assert outputs[0]["item"]["call_id"] == "read-call"
            agent.release.set()
            await _wait_sent(websocket, "response.create", 3)
            assert websocket.sent[-1]["response"]["tool_choice"] == "none"
            await _spoken_response(client, "result", "Specialist answer.")
            await route.wait_for_detached()
            delivered = [entry[1] for entry in calls if entry[0] == "event"
                         and entry[1].type is SessionEventType.TRANSCRIPT_FINAL
                         and entry[1].payload.get("delivered")]
            assert [event.payload["text"] for event in delivered] == [
                "I will check that.", "Specialist answer.",
            ]
            assert [event.payload["bytesPlayed"] for event in delivered] == [960, 960]
            assert delivered[-1].payload["taskId"] == route.tasks[0].task_id.value
            assert delivered[-1].payload["sourceTurnId"] == 1
            assert route.tasks[0].delivery is DeliveryPhase.DELIVERED
    finally:
        turn.cancel()
        agent.release.set()
        await asyncio.gather(turn, return_exceptions=True)
        await asyncio.wait_for(route.close(), 1)


@pytest.mark.asyncio
async def test_late_stop_transcript_rejects_speculative_wire_tool_call() -> None:
    provider, client, websocket = await _manual_provider(tools=True)
    calls = []
    agent = FakeAgent()
    agent.wait = True
    route = HybridRoute(
        role_contract=HybridRoleContract("Sleeptalk"), agent=agent,
        realtime=provider, audio_output=FakeOutput(calls), speculative_audio=True,
    )
    await route.prepare(SimpleNamespace(session_id="test", emit=lambda event: None))
    turn = asyncio.create_task(route.run_turn(
        RouteInput(pcm=b"\0\0" * 480), _context(calls, 1),
    ))
    try:
        async with asyncio.timeout(2):
            await _wait_sent(websocket, "input_audio_buffer.commit")
            await client._handle_event({"type": "input_audio_buffer.committed", "item_id": "stop-input"})
            await _wait_sent(websocket, "response.create")
            await client._handle_event({"type": "response.created", "response": {"id": "speculative"}})
            await client._handle_event({
                "type": "response.function_call_arguments.done", "response_id": "speculative",
                "call_id": "speculative-call", "name": SPECIALIST_TOOL, "item_id": "speculative-tool",
                "arguments": '{"question":"Read README."}',
            })
            await client._handle_event({
                "type": "response.done", "response": {"id": "speculative", "status": "completed"},
            })
            assert not agent.calls
            assert not any(entry[0] == "audio.write" for entry in calls)
            await client._handle_event({
                "type": "conversation.item.input_audio_transcription.completed",
                "item_id": "stop-input", "transcript": "Stop",
            })
            outcome = await turn
            assert not agent.calls
            assert outcome.heard == "Stop"
            assert not any(event.get("item", {}).get("type") == "function_call_output"
                           for event in websocket.sent)
            assert {"type": "conversation.item.delete", "item_id": "stop-input"} in websocket.sent
            assert {"type": "conversation.item.delete", "item_id": "speculative-tool"} in websocket.sent
    finally:
        turn.cancel()
        agent.release.set()
        await asyncio.gather(turn, return_exceptions=True)
        await asyncio.wait_for(route.close(), 1)


@pytest.mark.asyncio
async def test_silent_wire_acknowledgement_cannot_count_as_a_successful_spoken_turn() -> None:
    provider, client, websocket = await _manual_provider(tools=True)
    calls = []
    agent = FakeAgent()
    agent.wait = True
    route = HybridRoute(
        role_contract=HybridRoleContract("Sleeptalk"), agent=agent,
        realtime=provider, audio_output=FakeOutput(calls),
    )
    await route.prepare(SimpleNamespace(session_id="test", emit=lambda event: None))
    turn = asyncio.create_task(route.run_turn(
        RouteInput(text="How are you?"), _context(calls, 1),
    ))
    try:
        async with asyncio.timeout(2):
            await _wait_sent(websocket, "response.create")
            await client._handle_event({"type": "response.created", "response": {"id": "request"}})
            await client._handle_event({
                "type": "response.function_call_arguments.done", "response_id": "request",
                "call_id": "work", "name": SPECIALIST_TOOL,
                "arguments": '{"question":"Read README."}',
            })
            await client._handle_event({
                "type": "response.done", "response": {"id": "request", "status": "completed"},
            })
            await _wait_sent(websocket, "response.create", 2)
            await client._handle_event({"type": "response.created", "response": {"id": "silent"}})
            await client._handle_event({
                "type": "response.output_audio_transcript.done", "response_id": "silent",
                "transcript": "I will check.",
            })
            await client._handle_event({
                "type": "response.done", "response": {"id": "silent", "status": "completed"},
            })
            outcome = await turn
            assert outcome.status == "failed"
            assert not outcome.played
            assert not any(entry[0] == "audio.write" for entry in calls)
            assert not any(entry[0] == "event" and entry[1].type is SessionEventType.TRANSCRIPT_FINAL
                           and entry[1].payload.get("delivered") for entry in calls)
    finally:
        turn.cancel()
        agent.release.set()
        await asyncio.gather(turn, return_exceptions=True)
        await asyncio.wait_for(route.close(), 1)
