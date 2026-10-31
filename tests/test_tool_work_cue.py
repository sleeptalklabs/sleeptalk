"""Tool work has an audible start regardless of optional narration settings."""
import asyncio
from types import SimpleNamespace

import pytest

from sleeptalk.adapter import ToolEvent, ToolPhase, workbench_event
from sleeptalk.policy import VoicePolicy
from sleeptalk.routes.base import RouteInput, TurnContext
from sleeptalk.routes.cascade import CascadeRoute


class Output:
    def __init__(self):
        self.lines = []

    async def write(self, pcm, turn_id):
        self.lines.append(pcm.decode())


class Voice:
    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.lines = []

    async def synthesize(self, text):
        self.lines.append(text)
        self.started.set()
        await self.release.wait()
        yield text.encode()


@pytest.mark.parametrize('style', ['silence', 'keyboard', 'narrated'])
@pytest.mark.parametrize('event', [
    'tool:bash',
    ToolEvent(name='bash', phase=ToolPhase.STARTED),
    workbench_event('tool_start', name='bash', tool_call_id='call-1'),
])
async def test_fast_tool_turn_waits_for_spoken_cue_even_with_acks_suppressed(style, event):
    class Agent:
        async def respond_streaming(self, text, *, on_event):
            on_event(event)
            on_event(event)  # repeated starts must not repeat the cue
            yield 'The result is ready.'

    voice, output = Voice(), Output()
    route = CascadeRoute(agent=Agent(), tts=voice, audio_output=output)
    policy = VoicePolicy(instant_ack=False, narration_style=style, keepalive_interval_s=0,
                         endpointing='aggressive', local_responsiveness=True)
    await route.prepare(SimpleNamespace(policy=policy, config=None))
    ctx = TurnContext(session_id='s', turn_id=1, emit=lambda event: None, extras={'suppress_preamble': True})
    task = asyncio.create_task(route.run_turn(RouteInput(text='Check the files.'), ctx))
    try:
        await asyncio.wait_for(voice.started.wait(), 1)
        assert voice.lines == ['Running a command.']
        assert not task.done()
        voice.release.set()
        outcome = await asyncio.wait_for(task, 1)
        assert outcome.status == 'completed'
        assert output.lines == ['Running a command.', 'The result is ready.']
    finally:
        voice.release.set()
        await route.close()
        await asyncio.gather(task, return_exceptions=True)


async def test_cue_is_spoken_while_tool_work_is_still_running():
    working = asyncio.Event()
    release = asyncio.Event()

    class Agent:
        async def respond_streaming(self, text, *, on_event):
            on_event(ToolEvent(name='bash', phase=ToolPhase.STARTED))
            working.set()
            await release.wait()
            yield 'Done.'

    voice, output = Voice(), Output()
    voice.release.set()
    route = CascadeRoute(agent=Agent(), tts=voice, audio_output=output)
    await route.prepare(SimpleNamespace(policy=VoicePolicy(instant_ack=False, keepalive_interval_s=0), config=None))
    ctx = TurnContext(session_id='s', turn_id=1, emit=lambda event: None, extras={})
    task = asyncio.create_task(route.run_turn(RouteInput(text='Inspect the project.'), ctx))
    try:
        await asyncio.wait_for(voice.started.wait(), 1)
        assert working.is_set() and not task.done()
        assert output.lines == ['Running a command.']
        release.set()
        await asyncio.wait_for(task, 1)
    finally:
        release.set()
        await route.close()
        await asyncio.gather(task, return_exceptions=True)


async def test_speech_boundary_removes_split_emphasis_without_changing_the_transcript():
    class Agent:
        async def respond_streaming(self, text):
            yield '**The result is ready.'
            yield 'It passed.**'
            yield '**'

    voice, output = Voice(), Output()
    voice.release.set()
    route = CascadeRoute(agent=Agent(), tts=voice, audio_output=output)
    await route.prepare(SimpleNamespace(policy=VoicePolicy(instant_ack=False, keepalive_interval_s=0), config=None))
    ctx = TurnContext(session_id='s', turn_id=1, emit=lambda event: None, extras={})
    try:
        outcome = await route.run_turn(RouteInput(text='What happened?'), ctx)
        assert output.lines == ['The result is ready.', 'It passed.']
        assert outcome.generated == ['**The result is ready.', 'It passed.**', '**']
    finally:
        await route.close()


async def test_cancelling_during_slow_tool_cue_does_not_release_late_audio():
    class Agent:
        async def respond_streaming(self, text, *, on_event):
            on_event('tool:bash')
            yield 'Finished.'

    voice, output = Voice(), Output()
    route = CascadeRoute(agent=Agent(), tts=voice, audio_output=output)
    await route.prepare(SimpleNamespace(policy=VoicePolicy(instant_ack=False, keepalive_interval_s=0), config=None))
    ctx = TurnContext(session_id='s', turn_id=1, emit=lambda event: None, extras={})
    task = asyncio.create_task(route.run_turn(RouteInput(text='Check it.'), ctx))
    try:
        await asyncio.wait_for(voice.started.wait(), 1)
        await route.cancel_turn(1)
        voice.release.set()
        outcome = await asyncio.wait_for(task, 1)
        assert outcome.status == 'cancelled'
        assert output.lines == []
    finally:
        voice.release.set()
        await route.close()
        await asyncio.gather(task, return_exceptions=True)
