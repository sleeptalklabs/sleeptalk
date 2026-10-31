"""Exercise endpoint timers while Silero keeps emitting hangover frames."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sleeptalk.voice.configs import AdaptiveEndpointingConfig, TurnCompletionSignal
from sleeptalk.voice_loop.utterance import UtteranceAssembler
from sleeptalk.voice_loop.vad import VadObservation

PCM = bytes(3840)


class Clock:
    def __init__(self):
        self.now = 100.0
        self.handles = []

    def time(self):
        return self.now

    def call_later(self, delay, callback, *args):
        handle = SimpleNamespace(at=self.now + delay, callback=callback, args=args, cancelled=False)
        handle.cancel = lambda: setattr(handle, 'cancelled', True)
        self.handles.append(handle)
        return handle

    def is_closed(self):
        return False

    async def advance(self, seconds):
        end = self.now + seconds
        while True:
            ready = [h for h in self.handles if not h.cancelled and h.at <= end + 1e-8]
            if not ready:
                break
            handle = min(ready, key=lambda h: h.at)
            self.now = handle.at
            handle.cancel()
            handle.callback(*handle.args)
            # Drain the scorer and the on_utterance callback without wall-clock sleeps.
            for _ in range(4):
                await asyncio.sleep(0)
        self.now = end


class Vad:
    def __init__(self):
        self.observation = VadObservation(True, started=True, state='SPEAKING', confidence=0.95)

    async def observe(self, _):
        return self.observation


def capture(clock, *, local=True, cartesia=False, scorer=None, gate=None):
    vad = Vad()
    delivered, events, scores = [], [], []
    async def receive(_, pcm):
        delivered.append((clock.time(), pcm))
    assembler = UtteranceAssembler(
        loop=clock, min_bytes=1, silence_seconds=0.12, on_utterance=receive,
        should_ignore_audio=lambda: bool(gate and gate['shut']),
        vad_mode='silero', silero_vad_factory=lambda _: vad,
        smart_turn_scorer=scorer,
        endpointing_config=AdaptiveEndpointingConfig(
            local_streaming_input=local and not cartesia, streaming_pause_timing=cartesia,
            local_responsiveness=scorer is not None),
        turn_completion_provider=lambda _: TurnCompletionSignal(
            transcript='Tell me more.' if cartesia else 'Tell me more',
            source='cartesia' if cartesia else 'moonshine_streaming', is_final=not cartesia),
        on_capture_event=lambda _, event: events.append(event),
        on_smart_turn_event=lambda _, event: scores.append(event),
    )
    return assembler, vad, delivered, events, scores


@pytest.mark.parametrize('local', [True, False])
async def test_hangover_frames_cannot_postpone_local_deadline_or_hide_silence(local):
    clock = Clock()
    assembler, vad, delivered, events, _ = capture(clock, local=local)
    try:
        await assembler._observe_gated(0, object(), PCM)
        vad.observation = VadObservation(True, state='STOPPING', confidence=0.01)
        for _ in range(29):  # accepted audio through 580 ms of hangover
            await clock.advance(0.02)
            await assembler._observe_gated(0, object(), PCM)
        await clock.advance(0.04)
        if local:
            assert len(delivered) == 1
            assert delivered[0][0] - 100 == pytest.approx(0.6, abs=0.006)
            assert len(delivered[0][1]) == 30 * len(PCM)  # quiet tails are kept
            commit = next(e for e in events if e['kind'] == 'utterance_committed')
            assert commit['endpointSilenceMs'] == pytest.approx(600, abs=6)
        else:
            assert not delivered  # baseline scheduling is unchanged
    finally:
        assembler._cleanup_in_loop()


@pytest.mark.parametrize('enabled', [False, True])
async def test_cartesia_complete_text_uses_elapsed_quiet_during_hangover(enabled):
    clock = Clock()
    assembler, vad, delivered, events, _ = capture(clock, local=False, cartesia=enabled)
    assembler.turn_completion_provider = lambda _: TurnCompletionSignal(
        transcript='Tell me more.', source='cartesia')
    try:
        await assembler._observe_gated(0, object(), PCM)
        vad.observation = VadObservation(True, state='STOPPING', confidence=0.01)
        for _ in range(32):
            await clock.advance(0.02)
            await assembler._observe_gated(0, object(), PCM)
        await clock.advance(0.02)
        if enabled:
            assert len(delivered) == 1
            assert delivered[0][0] - 100 == pytest.approx(0.65, abs=0.006)
            assert len(delivered[0][1]) == 33 * len(PCM)
            commit = next(e for e in events if e['kind'] == 'utterance_committed')
            assert commit['endpointSilenceMs'] == pytest.approx(650, abs=6)
        else:
            assert not delivered
    finally:
        assembler._cleanup_in_loop()


@pytest.mark.parametrize('text,threshold', [('Tell me more', 1800), ('Tell me about', 3500), ('and um', 3500)])
async def test_cartesia_keeps_uncertain_incomplete_and_floor_holder_budgets(text, threshold):
    clock = Clock()
    assembler, _, delivered, _, _ = capture(clock, cartesia=True)
    assembler.turn_completion_provider = lambda _: TurnCompletionSignal(
        transcript=text, source='cartesia', is_final=True)
    try:
        await assembler._observe_gated(0, object(), PCM)
        await clock.advance(1.3)
        assert not delivered
        assert assembler._endpoint_decision(0).threshold_ms == threshold
    finally:
        assembler._cleanup_in_loop()


@pytest.mark.parametrize('cartesia', [False, True])
async def test_resumed_voice_restarts_the_deadline(cartesia):
    clock = Clock()
    assembler, vad, delivered, _, _ = capture(clock, cartesia=cartesia)
    try:
        await assembler._observe_gated(0, object(), PCM)
        await clock.advance(0.5)
        await assembler._observe_gated(0, object(), PCM)  # voice resumes
        vad.observation = VadObservation(True, state='STOPPING', confidence=0.01)
        await clock.advance(0.2)
        await assembler._observe_gated(0, object(), PCM)
        assert not delivered
        await clock.advance(0.47)
        assert len(delivered) == 1
        assert delivered[0][0] - 100 == pytest.approx(1.15 if cartesia else 1.1, abs=0.006)
    finally:
        assembler._cleanup_in_loop()


@pytest.mark.parametrize('second,commits', [(0.95, True), (0.7, False)])
@pytest.mark.parametrize('cartesia', [False, True])
async def test_smart_turn_gets_one_later_look_without_lowering_confidence(second, commits, cartesia):
    clock = Clock()
    scorer = SimpleNamespace(threshold=0.6, score=AsyncMock(side_effect=[0.7, second]))
    assembler, vad, delivered, _, scores = capture(clock, scorer=scorer, cartesia=cartesia)
    # No acoustic final yet, so the regular endpoint does not mask the retry.
    assembler.turn_completion_provider = lambda _: TurnCompletionSignal(
        transcript='Tell me more', source='cartesia' if cartesia else 'moonshine_streaming')
    try:
        await assembler._observe_gated(0, object(), PCM)
        await clock.advance(0.3)
        assert not delivered and scorer.score.await_count == 1
        assembler.buffers[0].extend(PCM)  # additional real quiet is in the retry snapshot
        await clock.advance(0.25)
        assert scorer.score.await_count == 2
        assert bool(delivered) is commits
        assert [s['attempt'] for s in scores] == [1, 2]
        assert scores[-1]['threshold'] == 0.8
        assert scores[-1]['silenceMs'] == 500
        assert len(scorer.score.await_args_list[1].args[0]) > len(scorer.score.await_args_list[0].args[0])
        await clock.advance(0.05)
        assert scorer.score.await_count == 2
    finally:
        assembler._cleanup_in_loop()


@pytest.mark.parametrize('cartesia', [False, True])
async def test_retry_is_cancelled_by_resumed_voice_and_floor_holders_veto_it(cartesia):
    clock = Clock()
    scorer = SimpleNamespace(threshold=0.6, score=AsyncMock(return_value=0.7))
    assembler, vad, delivered, _, scores = capture(clock, scorer=scorer, cartesia=cartesia)
    try:
        await assembler._observe_gated(0, object(), PCM)
        await clock.advance(0.3)
        await assembler._observe_gated(0, object(), PCM)
        await clock.advance(0.21)  # old retry would have fired at 500 ms
        assert scorer.score.await_count == 1 and not delivered
        assembler.turn_completion_provider = lambda _: TurnCompletionSignal(transcript='and um')
        await clock.advance(0.3)
        assert scorer.score.await_count == 1 and not delivered
    finally:
        assembler._cleanup_in_loop()


@pytest.mark.parametrize('cartesia', [False, True])
async def test_reopening_capture_gate_does_not_use_manufactured_silence(cartesia):
    clock = Clock()
    gate = {'shut': False}
    assembler, vad, delivered, _, _ = capture(clock, gate=gate, cartesia=cartesia)
    try:
        await assembler._observe_gated(0, object(), PCM)
        gate['shut'] = True
        await clock.advance(0.8)
        gate['shut'] = False
        assembler._maybe_flush(0)
        await asyncio.sleep(0)
        assert not delivered
        await clock.advance(0.67)
        assert len(delivered) == 1
    finally:
        assembler._cleanup_in_loop()


@pytest.mark.parametrize('cartesia', [False, True])
async def test_late_smart_turn_cannot_flush_after_an_ignore_gate_reopens(cartesia):
    clock = Clock()
    gate = {'shut': False}
    entered, release = asyncio.Event(), asyncio.Event()
    async def score(_):
        entered.set()
        await release.wait()
        return 0.99
    scorer = SimpleNamespace(threshold=0.6, score=score)
    assembler, _, delivered, _, scores = capture(clock, scorer=scorer, gate=gate, cartesia=cartesia)
    try:
        await assembler._observe_gated(0, object(), PCM)
        await clock.advance(0.3)
        assert entered.is_set()
        gate['shut'] = True
        assert assembler._gate_is_holding(0)
        gate['shut'] = False
        release.set()
        for _ in range(4):
            await asyncio.sleep(0)
        assert not delivered
        assert scores[-1]['stale']
    finally:
        release.set()
        assembler._cleanup_in_loop()
