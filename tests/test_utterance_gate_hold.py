"""Endpointing across the capture ignore gate.

Frames stop arriving while the gate is shut, so an utterance already under
way looks like it ended. Ending it there cut one sentence into a turn per
gap.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from sleeptalk.voice_loop.utterance import UtteranceAssembler

FRAME = b"\x40\x10" * 480  # loud enough to count as speech at any floor


class Clock:
    """The assembler's loop, with the clock on a hand crank.

    Timers are neutered: the tests drive `_maybe_flush` themselves so the
    decision under test is not racing a real callback.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self.now = loop.time()

    def time(self) -> float:
        return self.now

    def call_later(self, delay: float, callback: Any, *args: Any) -> Any:
        handle = self._loop.call_later(3600, lambda: None)
        handle.cancel()
        return handle

    def is_closed(self) -> bool:
        return self._loop.is_closed()


def _assembler(clock: Clock, gate: dict[str, bool], delivered: list[bytes]):
    async def on_utterance(participant: object, pcm: bytes) -> None:
        delivered.append(pcm)

    return UtteranceAssembler(
        loop=clock,
        min_bytes=1,
        silence_seconds=0.4,
        on_utterance=on_utterance,
        should_ignore_audio=lambda: gate["shut"],
    )


@pytest.mark.asyncio
async def test_a_gate_that_shuts_mid_sentence_does_not_end_the_utterance() -> None:
    """Measured on terminal: a status line landing while the user was still
    speaking shut the gate, and the half-caught question was flushed as its
    own turn — "Can you Tell me The latest Well The"."""
    clock = Clock(asyncio.get_running_loop())
    gate = {"shut": False}
    delivered: list[bytes] = []
    assembler = _assembler(clock, gate, delivered)
    speaker = object()

    assembler.observe(0, speaker, FRAME)

    gate["shut"] = True  # the assistant starts a line over the top
    clock.now += 2.0
    assembler._maybe_flush(0)
    assert delivered == [], "the gate's own silence ended the utterance"
    assert 0 in assembler.buffers

    gate["shut"] = False  # the line finishes; the user is still talking
    assembler._maybe_flush(0)
    assembler.observe(0, speaker, FRAME)
    assert delivered == []

    clock.now += 10.0
    assembler._maybe_flush(0)
    await asyncio.sleep(0)
    assert delivered == [FRAME + FRAME], "one sentence, one turn"


@pytest.mark.asyncio
async def test_the_hold_ends_where_a_real_pause_would_have() -> None:
    """A gate that never reopens must not strand the buffer."""
    clock = Clock(asyncio.get_running_loop())
    gate = {"shut": False}
    delivered: list[bytes] = []
    assembler = _assembler(clock, gate, delivered)

    assembler.observe(0, object(), FRAME)
    gate["shut"] = True
    assembler._maybe_flush(0)  # starts the hold

    clock.now += assembler._gated_hold_limit_ms() / 1000 + 1
    assembler._maybe_flush(0)
    await asyncio.sleep(0)
    assert delivered == [FRAME]


@pytest.mark.asyncio
async def test_a_participant_collecting_through_the_gate_is_not_held() -> None:
    """Barge-in already routes the interrupting speaker past the gate."""
    clock = Clock(asyncio.get_running_loop())
    gate = {"shut": True}
    delivered: list[bytes] = []

    async def on_utterance(participant: object, pcm: bytes) -> None:
        delivered.append(pcm)

    assembler = UtteranceAssembler(
        loop=clock,
        min_bytes=1,
        silence_seconds=0.4,
        on_utterance=on_utterance,
        should_ignore_audio=lambda: True,
        collect_during_ignore=lambda _pid: True,
    )
    assembler.observe(0, object(), FRAME)
    clock.now += 10.0
    assembler._maybe_flush(0)
    await asyncio.sleep(0)
    assert delivered == [FRAME]


@pytest.mark.asyncio
async def test_continuous_audio_is_cut_into_bounded_utterances() -> None:
    """Music or an open mic never pauses; one buffer must not grow forever."""
    clock = Clock(asyncio.get_running_loop())
    delivered: list[bytes] = []
    assembler = _assembler(clock, {"shut": False}, delivered)
    assembler.max_utterance_bytes = len(FRAME) * 10
    for _ in range(25):
        assembler.observe(1, object(), FRAME)
    await asyncio.sleep(0)
    assert [len(pcm) for pcm in delivered] == [len(FRAME) * 10] * 2
    assert len(assembler.buffers[1]) == len(FRAME) * 5


@pytest.mark.asyncio
async def test_an_utterance_under_the_cap_is_untouched() -> None:
    from sleeptalk.voice_loop.utterance import MAX_UTTERANCE_BYTES

    clock = Clock(asyncio.get_running_loop())
    delivered: list[bytes] = []
    assembler = _assembler(clock, {"shut": False}, delivered)
    assert assembler.max_utterance_bytes == MAX_UTTERANCE_BYTES == 60 * 192_000
    for _ in range(100):
        assembler.observe(1, object(), FRAME)
    await asyncio.sleep(0)
    assert delivered == []
    assert len(assembler.buffers[1]) == len(FRAME) * 100
