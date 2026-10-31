"""Exercise shared lifecycle through the public session and Discord entrypoints."""

import asyncio
from types import SimpleNamespace

import pytest

from sleeptalk.discord.bot import DiscordSessionKernel
from sleeptalk.discord_voice.live_session import PiDiscordLiveSession
from sleeptalk.routes.base import TurnOutcome
from sleeptalk.session import RealtimeSession, SessionLifecycleError


class RecordingRoute:
    def __init__(self):
        self.contexts = []
        self.inputs = []
        self.outputs = []
        self.prepares = 0
        self.closes = 0
        self.interrupts = []
        self.cancelled_tasks = []
        self.active = None
        self.block = None
        self.fail = False
        self.started = asyncio.Event()

    async def prepare(self, session):
        self.prepares += 1
        await asyncio.sleep(0)

    def set_audio_output(self, output):
        self.output = output

    async def run_turn(self, route_input, ctx):
        assert self.active is None, "shared session must serialize route turns"
        self.active = SimpleNamespace(turn_id=ctx.turn_id)
        self.contexts.append(ctx)
        self.inputs.append(route_input)
        self.outputs.append(getattr(self, 'output', None))
        self.started.set()
        try:
            if self.block is not None:
                await self.block.wait()
            if self.fail:
                raise RuntimeError('provider failed')
            return TurnOutcome('completed', ctx.turn_id, generated=route_input.text)
        finally:
            self.active = None

    def active_turn(self):
        return self.active

    async def interrupt_presentation(self, turn_id, **kwargs):
        self.interrupts.append(turn_id)

    async def settle_usage(self, turn_id, **kwargs):
        pass

    async def cancel_task(self, task_id):
        self.cancelled_tasks.append(task_id)
        return True

    async def close(self):
        self.closes += 1


class Output:
    def __init__(self):
        self.finished = []
        self.discarded = []

    async def finish_turn(self, turn_id):
        self.finished.append(turn_id)

    async def discard_turn(self, turn_id, reason):
        self.discarded.append((turn_id, reason))


def make_session():
    route = RecordingRoute()
    session = RealtimeSession(SimpleNamespace(route=route))
    return session, route


@pytest.mark.asyncio
@pytest.mark.parametrize('discord', [False, True])
async def test_entrypoints_share_preparation_identity_and_serialization(discord):
    session, route = make_session()
    driver = DiscordSessionKernel(session) if discord else session
    await asyncio.gather(*(driver.run_turn(text=str(n)) for n in range(5)))
    assert route.prepares == 1
    assert [ctx.turn_id for ctx in route.contexts] == [1, 2, 3, 4, 5]
    with pytest.raises(SessionLifecycleError, match='monotonically'):
        await driver.run_turn(text='repeated', turn_id=5)
    await route.contexts[-1].schedule_followup('continuation', {})
    assert route.contexts[-1].turn_id == 6
    await session.aclose()


@pytest.mark.asyncio
async def test_discord_followup_preserves_owner_output_extras_and_finalization():
    session, route = make_session()
    driver = DiscordSessionKernel(session)
    output = Output()
    owner = object()
    driver.set_briefing('Speak briefly.')
    started = []

    def on_started(turn_id):
        assert driver._active_owner is owner
        started.append(turn_id)

    await driver.run_turn(text='First', output=output, owner=owner,
                          extras={'participant_id': 91}, on_started=on_started)
    assert 'Speak briefly.' in route.inputs[0].text
    await route.contexts[0].schedule_followup('Keep going', {'automatic': True})
    assert route.inputs[1].text == 'Keep going'
    assert route.contexts[1].extras['participant_id'] == 91
    assert route.contexts[1].extras['automatic'] is True
    assert route.outputs == [output, output]
    assert output.finished == started == [1, 2]
    assert driver._active_owner is None
    await driver.run_turn(text='Next', output=output, owner=owner)
    assert route.inputs[-1].text == 'Next'
    await driver.close()
    await driver.close()
    assert route.closes == 1


@pytest.mark.asyncio
async def test_discord_other_owner_cannot_interrupt_automatic_continuation():
    session, route = make_session()
    driver = DiscordSessionKernel(session)
    owner = object()
    output = Output()
    await driver.run_turn(text='First', owner=owner, output=output)
    followup = route.contexts[0].schedule_followup
    route.block = asyncio.Event()
    route.started.clear()
    running = asyncio.create_task(followup('Continue', {}))
    await route.started.wait()
    await driver.cancel_active_turn(owner=object())
    assert route.interrupts == []
    await driver.cancel_active_turn(owner=owner)
    assert route.interrupts == [2]
    assert output.discarded == [(2, 'barge-in')]
    assert route.cancelled_tasks == []
    route.block.set()
    await running
    assert await followup('stale continuation', {}) is None
    assert len(route.contexts) == 2
    await driver.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['interrupt', 'cancel', 'new_turn', 'close'])
async def test_pending_continuation_is_fenced_by_session_changes(action):
    session, route = make_session()
    await session.run_turn(text='First')
    followup = route.contexts[0].schedule_followup
    if action == 'interrupt':
        await session.interrupt_presentation()
    elif action == 'cancel':
        await session.cancel_task('job')
    elif action == 'new_turn':
        await session.run_turn(text='New request')
    else:
        await session.aclose()
    before = len(route.contexts)
    assert await followup('stale', {}) is None
    assert len(route.contexts) == before
    await session.aclose()


@pytest.mark.asyncio
async def test_close_cancels_active_turn_and_finalizes_output_once():
    session, route = make_session()
    driver = DiscordSessionKernel(session)
    output = Output()
    route.block = asyncio.Event()
    running = asyncio.create_task(driver.run_turn(text='First', output=output))
    await route.started.wait()
    await driver.close()
    with pytest.raises(asyncio.CancelledError):
        await running
    await driver.close()
    assert output.finished == [1]
    assert route.closes == 1
    with pytest.raises(SessionLifecycleError, match='close'):
        await driver.run_turn(text='Too late')


@pytest.mark.asyncio
async def test_failed_route_releases_owner_and_allows_next_turn():
    session, route = make_session()
    driver = DiscordSessionKernel(session)
    output = Output()
    driver.set_briefing('Speak briefly.')
    route.fail = True
    with pytest.raises(RuntimeError, match='provider failed'):
        await driver.run_turn(text='First', output=output, owner=object())
    assert driver._active_owner is None
    route.fail = False
    await driver.run_turn(text='Retry', output=output, owner=object())
    assert 'Speak briefly.' in route.inputs[-1].text
    assert output.finished == [1, 2]
    await driver.close()


@pytest.mark.asyncio
async def test_live_discord_transport_generation_is_not_a_semantic_turn_id():
    session, route = make_session()
    driver = DiscordSessionKernel(session)
    output = Output()
    trace = SimpleNamespace(mark=lambda *args, **kwargs: None)
    live = SimpleNamespace(bot=SimpleNamespace(kernel=driver), route_output=output,
                           _turn_generation=800)
    await PiDiscordLiveSession._stream_and_speak(live, 'First', trace)
    await route.contexts[0].schedule_followup('Continuation', {})
    live._turn_generation = 1
    await PiDiscordLiveSession._stream_and_speak(live, 'Rejoined', trace)
    assert [ctx.turn_id for ctx in route.contexts] == [1, 2, 3]
    assert output.finished == [1, 2, 3]
    await driver.close()


@pytest.mark.asyncio
async def test_close_during_prepare_drains_startup_before_resource_cleanup():
    session, route = make_session()
    preparing = asyncio.Event()
    stopped = asyncio.Event()

    async def prepare(_session):
        preparing.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    original_close = route.close

    async def close():
        assert stopped.is_set()
        await original_close()

    route.prepare = prepare
    route.close = close
    running = asyncio.create_task(session.run_turn(text='First'))
    await preparing.wait()
    await session.aclose()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert route.closes == 1
    assert route.contexts == []


@pytest.mark.asyncio
async def test_queued_followup_rechecks_cancellation_after_lock_wait():
    session, route = make_session()
    await session.run_turn(text='First')
    followup = route.contexts[0].schedule_followup
    await session._turn_lock.acquire()
    pending = asyncio.create_task(followup('Queued', {}))
    await asyncio.sleep(0)
    await session.interrupt_presentation()
    session._turn_lock.release()
    assert await pending is None
    assert len(route.contexts) == 1
    await session.aclose()


@pytest.mark.asyncio
async def test_discord_capture_enters_shared_policy_as_mono_and_resets_on_leave():
    session, route = make_session()
    captured = []
    resets = []

    async def append_audio(pcm, *, participant_id=0):
        captured.append((pcm, participant_id))

    async def transcribe(pcm, *, participant_id=0):
        assert (pcm, participant_id) == captured[-1]
        return SimpleNamespace(text='Recognized', source='streaming')

    async def reset_capture():
        resets.append(True)

    route.append_audio = append_audio
    route.transcribe = transcribe
    route.reset_capture = reset_capture
    live = SimpleNamespace(bot=SimpleNamespace(kernel=DiscordSessionKernel(session),
                                               realtime_session=session))
    stereo = b'\x01\x00\x03\x00' * 24
    mono = b'\x02\x00' * 24
    await PiDiscordLiveSession._append_capture_pcm(live, 91, stereo)
    result = await session.transcribe(mono, participant_id=91)
    assert captured == [(mono, 91)]
    assert result.text == 'Recognized'
    await session.reset_capture()
    assert resets == [True]
    assert route.closes == 0
    await session.aclose()


@pytest.mark.asyncio
async def test_synchronous_session_finalizes_output_and_detached_route_retains_it():
    session, route = make_session()
    output = Output()
    session.set_audio_output(output)
    await session.run_turn(text="Synchronous")
    assert output.finished == [1]
    original_run = route.run_turn

    async def run_detached(route_input, ctx):
        await original_run(route_input, ctx)
        return TurnOutcome("completed", ctx.turn_id, pending_task_id="job")

    route.run_turn = run_detached
    await session.run_turn(text="Detached")
    assert output.finished == [1]
    await output.finish_turn(2)
    assert output.finished == [1, 2]
    await session.aclose()


@pytest.mark.asyncio
async def test_output_finalization_failure_releases_discord_owner():
    session, route = make_session()
    driver = DiscordSessionKernel(session)
    output = Output()

    async def failed_finish(turn_id):
        raise RuntimeError("playback failed")

    output.finish_turn = failed_finish
    with pytest.raises(RuntimeError, match="playback failed"):
        await driver.run_turn(text="First", output=output, owner=object())
    assert driver._active_owner is None
    recovered = Output()
    await driver.run_turn(text="Next", output=recovered)
    assert recovered.finished == [2]
    await session.aclose()


class DelayedCancellationRoute(RecordingRoute):
    def __init__(self):
        super().__init__()
        self.block = asyncio.Event()
        self.cancelling = asyncio.Event()
        self.release_cleanup = asyncio.Event()
        self.cleanup_finished = False

    async def interrupt_presentation(self, turn_id, **kwargs):
        self.interrupts.append(turn_id)
        self.block.set()
        self.cancelling.set()
        await self.release_cleanup.wait()
        self.cleanup_finished = True


@pytest.mark.asyncio
async def test_replacement_waits_for_cancellation_cleanup_after_old_turn_finishes():
    route = DelayedCancellationRoute()
    session = RealtimeSession(SimpleNamespace(route=route))
    first = asyncio.create_task(session.run_turn(text="First"))
    await route.started.wait()
    interrupt = asyncio.create_task(session.interrupt_presentation())
    await route.cancelling.wait()
    await first
    replacement = asyncio.create_task(session.run_turn(text="Replacement"))
    try:
        await asyncio.sleep(0)
        assert len(route.contexts) == 1
        assert not replacement.done()
        route.release_cleanup.set()
        await interrupt
        outcome = await replacement
        assert outcome.turn_id == 2
        assert route.cleanup_finished
    finally:
        route.release_cleanup.set()
        await asyncio.gather(interrupt, replacement, return_exceptions=True)
        await session.aclose()


@pytest.mark.asyncio
async def test_close_drains_interruption_before_closing_providers():
    route = DelayedCancellationRoute()
    session = RealtimeSession(SimpleNamespace(route=route))
    first = asyncio.create_task(session.run_turn(text="First"))
    await route.started.wait()
    interrupt = asyncio.create_task(session.interrupt_presentation())
    await route.cancelling.wait()
    await first
    closing = asyncio.create_task(session.aclose())
    try:
        await asyncio.sleep(0)
        assert route.closes == 0
        assert not closing.done()
        await session.interrupt_presentation(1)
        assert route.interrupts == [1]
        route.release_cleanup.set()
        await interrupt
        await closing
        assert route.cleanup_finished
        assert route.closes == 1
    finally:
        route.release_cleanup.set()
        await asyncio.gather(interrupt, closing, return_exceptions=True)
        await session.aclose()
