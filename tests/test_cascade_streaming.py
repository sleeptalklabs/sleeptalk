import asyncio
from types import SimpleNamespace

import pytest

from sleeptalk.opening_preamble_gate import OpeningPreambleGate
from sleeptalk.policy import VoicePolicy
from sleeptalk.routes.base import RouteInput, TurnContext
from sleeptalk.routes.cascade import CascadeRoute
from sleeptalk.session_events import SessionEventType
from sleeptalk.voice_policy_runtime import VoicePolicyRuntime, VoicePolicyTimings


ACK = "One moment."
ANSWER = "The answer is ready."


def context(events):
    return TurnContext(session_id="streaming", turn_id=1, emit=events.append, extras={})


def smart_ack_policy():
    return VoicePolicy(
        instant_ack=True,
        instant_ack_mode="smart",
        smart_ack_budget_ms=1,
        keepalive_interval_s=0,
        preamble_pools={"ack": (ACK,)},
    )


class ImmediateTTS:
    async def synthesize(self, text):
        yield text.encode()


class RecordingOutput:
    def __init__(self):
        self.writes = []

    async def write(self, pcm, turn_id):
        self.writes.append((pcm, turn_id))


async def test_partially_queued_ack_finishes_before_answer_after_producer_completes():
    ack_started = asyncio.Event()
    release_ack = asyncio.Event()
    ack_cancelled = asyncio.Event()
    agent_finished = asyncio.Event()
    order = []

    class Output(RecordingOutput):
        async def write(self, pcm, turn_id):
            if pcm == ACK.encode():
                order.append("ack_prefix_queued")
                ack_started.set()
                try:
                    await release_ack.wait()
                except asyncio.CancelledError:
                    ack_cancelled.set()
                    raise
                order.append("ack_finished")
            else:
                order.append("answer_queued")
            await super().write(pcm, turn_id)

    class Stt:
        async def transcribe_pcm(self, _pcm):
            await ack_started.wait()
            return "Explain the orchard plan"

    class Agent:
        async def respond_streaming(self, _text):
            order.append("agent_dispatched")
            yield ANSWER
            agent_finished.set()

    output = Output()
    events = []
    route = CascadeRoute(agent=Agent(), tts=ImmediateTTS(), stt=Stt(), audio_output=output)
    await route.prepare(SimpleNamespace(policy=smart_ack_policy(), config=None))
    turn = asyncio.create_task(route.run_turn(RouteInput(pcm=b"\0\0" * 240), context(events)))
    try:
        await asyncio.wait_for(agent_finished.wait(), 1)
        await asyncio.wait({turn}, timeout=0.02)
        assert not turn.done()
        assert not ack_cancelled.is_set()
        assert order == ["ack_prefix_queued", "agent_dispatched"]
        assert not any(event.type is SessionEventType.RESPONSE_FIRST_OUTPUT for event in events)
        release_ack.set()
        outcome = await asyncio.wait_for(turn, 1)
        assert outcome.status == "completed"
        assert order == ["ack_prefix_queued", "agent_dispatched", "ack_finished", "answer_queued"]
        first_outputs = [event for event in events if event.type is SessionEventType.RESPONSE_FIRST_OUTPUT]
        assert len(first_outputs) == 1
        assert first_outputs[0].payload["audioCategory"] == "answer"
        assert first_outputs[0].payload["measurement"] == "output_enqueue"
    finally:
        release_ack.set()
        if not turn.done():
            turn.cancel()
        await asyncio.gather(turn, return_exceptions=True)
        await route.close()


async def test_answer_cancels_ack_that_has_not_reached_output():
    synthesizing_ack = asyncio.Event()
    ack_cancelled = asyncio.Event()

    class TTS(ImmediateTTS):
        async def synthesize(self, text):
            if text == ACK:
                synthesizing_ack.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    ack_cancelled.set()
            yield text.encode()

    class Stt:
        async def transcribe_pcm(self, _pcm):
            await synthesizing_ack.wait()
            return "Explain the orchard plan"

    class Agent:
        async def respond_streaming(self, _text):
            yield ANSWER

    output = RecordingOutput()
    route = CascadeRoute(agent=Agent(), tts=TTS(), stt=Stt(), audio_output=output)
    await route.prepare(SimpleNamespace(policy=smart_ack_policy(), config=None))
    try:
        outcome = await asyncio.wait_for(
            route.run_turn(RouteInput(pcm=b"\0\0" * 240), context([])), 1
        )
        assert outcome.status == "completed"
        assert ack_cancelled.is_set()
        assert output.writes == [(ANSWER.encode(), 1)]
    finally:
        await route.close()


async def test_upstream_cancellation_does_not_leave_consumer_waiting_for_a_sentence():
    class Agent:
        async def respond_streaming(self, _text):
            raise asyncio.CancelledError("upstream request cancelled")
            yield "unreachable"

    route = CascadeRoute(agent=Agent(), tts=None)
    await route.prepare(SimpleNamespace(policy=VoicePolicy(instant_ack=False), config=None))
    try:
        outcome = await asyncio.wait_for(
            route.run_turn(RouteInput(text="Explain the orchard plan"), context([])), 1
        )
        assert outcome.status == "cancelled"
        assert outcome.generated == []
    finally:
        await route.close()


async def test_slow_tts_allows_bounded_read_ahead_and_cancel_settles_producer():
    fourth_sentence = asyncio.Event()
    synthesis_started = asyncio.Event()
    synthesis_closed = asyncio.Event()
    generated = []

    class Agent:
        async def respond_streaming(self, _text):
            for number in range(20):
                generated.append(number)
                if len(generated) == 4:
                    fourth_sentence.set()
                yield f"Answer sentence {number}."

    class TTS:
        async def synthesize(self, _text):
            synthesis_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                synthesis_closed.set()
            yield b"unreachable"

    route = CascadeRoute(agent=Agent(), tts=TTS(), audio_output=RecordingOutput())
    await route.prepare(SimpleNamespace(policy=VoicePolicy(instant_ack=False), config=None))
    turn = asyncio.create_task(route.run_turn(RouteInput(text="Explain the orchard plan"), context([])))
    try:
        await asyncio.wait_for(synthesis_started.wait(), 1)
        await asyncio.wait_for(fourth_sentence.wait(), 1)
        await asyncio.sleep(0)
        # One sentence synthesizing, two queued, one blocked on queue.put.
        assert generated == [0, 1, 2, 3]
        await asyncio.wait_for(route.cancel_turn(1), 1)
        outcome = await asyncio.wait_for(turn, 1)
        assert outcome.status == "cancelled"
        assert synthesis_closed.is_set()
        assert generated == [0, 1, 2, 3]
        assert not any(
            task.get_name() == "cascade-sentences-1" and not task.done()
            for task in asyncio.all_tasks()
        )
    finally:
        if not turn.done():
            turn.cancel()
        await asyncio.gather(turn, return_exceptions=True)
        await route.close()


@pytest.mark.parametrize("ending", ["ack_failure", "consumer_closed"])
async def test_started_policy_ack_settles_owned_tasks_on_failure_or_consumer_close(ending):
    ack_started = asyncio.Event()
    release_ack = asyncio.Event()
    ack_settled = asyncio.Event()
    agent_finished = asyncio.Event()
    gate = OpeningPreambleGate()

    class Agent:
        async def respond_streaming(self, _text):
            await ack_started.wait()
            yield ANSWER
            agent_finished.set()

    async def speak(_text, _ctx):
        gate.audio_committing = gate.audio_started = True
        ack_started.set()
        try:
            await release_ack.wait()
            raise RuntimeError("ACK provider disconnected after first PCM")
        finally:
            ack_settled.set()

    policy = VoicePolicy(
        instant_ack=True,
        instant_ack_mode="always",
        keepalive_interval_s=0,
        preamble_pools={"ack": (ACK,)},
    )
    runtime = VoicePolicyRuntime(
        agent=Agent(), policy=policy, timings=VoicePolicyTimings(ack_delay_seconds=0)
    )
    stream = runtime.respond("Explain the orchard plan", context([]), speak_policy=speak, opening_gate=gate)
    finishing = None
    try:
        assert await asyncio.wait_for(anext(stream), 1) == ANSWER
        if ending == "ack_failure":
            finishing = asyncio.create_task(anext(stream))
            await asyncio.wait_for(agent_finished.wait(), 1)
            await asyncio.sleep(0)
            release_ack.set()
            with pytest.raises(RuntimeError, match="ACK provider disconnected"):
                await asyncio.wait_for(finishing, 1)
        else:
            await asyncio.wait_for(stream.aclose(), 1)
        assert ack_settled.is_set()
        assert not runtime._tasks.get(1)
    finally:
        release_ack.set()
        if finishing is not None:
            if not finishing.done():
                finishing.cancel()
            await asyncio.gather(finishing, return_exceptions=True)
        await stream.aclose()
        await runtime.close()
