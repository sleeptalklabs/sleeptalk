"""Canned opening lines only when the agent is actually slow.

Measured 2026-10-04 over 30 fast turns: a 0.4 s timer from the agent request
put a canned line on 26 of them and held the answer back by a median 950 ms
(411 ms without), ten times behind an answer that was already written.
"""

import asyncio
import time
from types import SimpleNamespace

from sleeptalk.config import SleeptalkConfig
from sleeptalk.policy import VoicePolicy
from sleeptalk.routes.base import RouteInput, TurnContext
from sleeptalk.routes.cascade import CascadeRoute
from sleeptalk.voice_policy_runtime import VoicePolicyTimings

ACK = "Okay, let me check that."
ACTION = "Still checking."
ANSWER = "The orchard plan is ready."


def policy(**extra):
    return VoicePolicy(instant_ack=True, instant_ack_mode="smart", keepalive_interval_s=0,
                       preamble_pools={"ack_action": (ACK,), "ack": ("Okay.",), "action": (ACTION,)}, **extra)


def timings(ack=0.05, action=5.0):
    return VoicePolicyTimings(ack_delay_seconds=ack, sentence_grace_seconds=0.02, action_delay_seconds=action)


class Output:
    def __init__(self):
        self.writes = []

    async def write(self, pcm, turn_id):
        self.writes.append(pcm.decode())


class TTS:
    def __init__(self, slow_lines=(), delay=0.0):
        self.calls = []
        self.slow_lines = set(slow_lines)
        self.delay = delay

    async def synthesize(self, text):
        self.calls.append(text)
        if text in self.slow_lines:
            await asyncio.sleep(self.delay)
        yield text.encode()


MORE = "It has three rows."


def agent_answering_after(seconds, more_after=None):
    class Agent:
        async def respond_streaming(self, _text):
            await asyncio.sleep(seconds)
            yield ANSWER
            if more_after is not None:
                # Still streaming: the turn's end doesn't tidy the line away.
                await asyncio.sleep(more_after)
                yield MORE
    return Agent()


async def run(route, text, extras=None):
    ctx = TurnContext(session_id="s", turn_id=1, emit=lambda _event: None, extras=dict(extras or {}))
    outcome = await asyncio.wait_for(route.run_turn(RouteInput(text=text), ctx), 3)
    assert outcome.status == "completed"
    return ctx


def test_the_defaults_wait_a_conversational_pause():
    assert VoicePolicyTimings().ack_delay_seconds == 1.2
    assert VoicePolicy().smart_ack_budget_ms == 1200
    assert SleeptalkConfig.model_validate({}).policy.smart_ack_budget_ms == 1200


async def test_a_canned_line_still_being_made_is_dropped_when_the_answer_arrives():
    # The line is decided at 0.05 s; its audio would arrive at 0.2 s, after
    # the answer's text (0.08 s). Nothing of it was heard, so it never plays.
    output, tts = Output(), TTS(slow_lines={ACK}, delay=0.15)
    route = CascadeRoute(agent=agent_answering_after(0.08, more_after=0.3), tts=tts, audio_output=output,
                         policy_timings=timings())
    await route.prepare(SimpleNamespace(policy=policy(), config=None))
    try:
        ctx = await run(route, "What is the orchard plan?")
    finally:
        await route.close()
    assert ACK in tts.calls
    assert output.writes == [ANSWER, MORE]
    assert ctx.extras["_opening_dropped"]


async def test_a_canned_line_that_beats_the_answer_still_plays_first():
    output = Output()
    route = CascadeRoute(agent=agent_answering_after(0.15), tts=TTS(), audio_output=output, policy_timings=timings())
    await route.prepare(SimpleNamespace(policy=policy(), config=None))
    try:
        await run(route, "What is the orchard plan?")
    finally:
        await route.close()
    assert output.writes == [ACK, ANSWER]


async def test_a_bare_yeah_gets_no_acknowledgement():
    output = Output()
    route = CascadeRoute(agent=agent_answering_after(0.15), tts=TTS(), audio_output=output,
                         policy_timings=timings(action=1.0))
    await route.prepare(SimpleNamespace(policy=policy(), config=None))
    try:
        await run(route, "yeah")
    finally:
        await route.close()
    assert output.writes == [ANSWER]


async def test_the_wait_counts_from_the_end_of_the_callers_turn():
    """Transcription time is silence the caller already sat through."""
    output = Output()
    route = CascadeRoute(agent=agent_answering_after(0.15), tts=TTS(), audio_output=output,
                         policy_timings=timings(ack=1.0))
    await route.prepare(SimpleNamespace(policy=policy(), config=None))
    try:
        await run(route, "What is the orchard plan?")
        assert output.writes == [ANSWER]  # 0.15 s of a 1 s allowance: no line
        output.writes.clear()
        await run(route, "What is the orchard plan?", {"_turn_started_at": time.monotonic() - 0.95})
    finally:
        await route.close()
    assert output.writes == [ACK, ANSWER]


async def test_the_next_opening_lines_are_made_ahead_and_play_from_the_cache():
    output, tts = Output(), TTS()
    route = CascadeRoute(agent=agent_answering_after(0.15), tts=tts, audio_output=output,
                         policy_timings=timings(), warm_opening_lines=True)
    await route.prepare(SimpleNamespace(policy=policy(), config=None))
    try:
        lines = route._policy_runtime.next_opening_lines()
        assert {ACK, ACTION, "Okay."} <= set(lines)
        await route._warm_task
        # Looking ahead doesn't use a line up.
        assert route._policy_runtime.next_opening_lines() == lines
        assert sorted(tts.calls) == sorted(lines)
        await run(route, "What is the orchard plan?")
    finally:
        await route.close()
    assert output.writes == [ACK, ANSWER]
    assert tts.calls.count(ACK) == 1  # made once, ahead; played from the cache


async def test_routes_built_for_tests_make_nothing_ahead():
    tts = TTS()
    route = CascadeRoute(agent=agent_answering_after(0), tts=tts, audio_output=Output())
    await route.prepare(SimpleNamespace(policy=policy(), config=None))
    try:
        assert route._warm_task is None and tts.calls == []
    finally:
        await route.close()
