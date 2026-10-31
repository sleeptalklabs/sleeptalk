from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest

from sleeptalk.interrupt import AcousticKind, Floor, InterruptPolicy, SpeakingState
from sleeptalk.routes.cascade import CascadeRoute
from sleeptalk.session import RealtimeSession

_PLAYBACK = (
    Path(__file__).resolve().parents[1] / "sleeptalk" / "web_voice" / "playback.py"
)


class _IdleRoute:
    def active_turn(self):
        return None

    async def cancel_turn(self, turn_id: int) -> None:
        raise AssertionError("idle cancel must not reach the route")


def _playout_empty_block() -> str:
    source = _PLAYBACK.read_text()
    start = source.find("async def _run_playout")
    assert start >= 0
    body = source[start:]
    empty_at = body.find("if self._audio_queue.empty():")
    assert empty_at >= 0
    return body[empty_at:].split("except", 1)[0]


def test_web_floor_holds_leftover_mic_after_drain():
    floor = Floor(InterruptPolicy(), AcousticKind.isolated, detector="silero")
    floor.observe(0, "turn_start")
    floor.observe(0, "audio_start")
    floor.observe(1500, "drain")
    leftover = floor.observe(1800, "mic_speech")
    assert leftover.fire is False
    assert leftover.state is SpeakingState.hold
    assert "self._mark_drain()" in _playout_empty_block()
    assert "self.active = False" not in _playout_empty_block()


def test_web_floor_fires_800ms_into_the_first_clip():
    floor = Floor(InterruptPolicy(), AcousticKind.isolated, detector="silero")
    floor.observe(0, "turn_start")
    floor.observe(0, "audio_start")
    stop = floor.observe(800, "mic_speech")
    assert stop.fire is True
    assert stop.reason == "interrupt while audio plays"


@pytest.mark.asyncio
async def test_idle_session_cancel_is_a_noop():
    session = RealtimeSession(SimpleNamespace(route=_IdleRoute()))
    await session.cancel()


@pytest.mark.asyncio
async def test_session_keeps_presentation_interrupt_separate_from_task_cancel():
    class HybridRoute:
        class capabilities:
            class route_type:
                value = "hybrid"

        def __init__(self):
            self.interrupted = []
            self.cancelled = []

        def active_turn(self):
            return SimpleNamespace(turn_id=7)

        async def interrupt_presentation(self, turn_id=None):
            self.interrupted.append(turn_id)

        async def cancel_task(self, task_id):
            self.cancelled.append(task_id)
            return True

    route = HybridRoute()
    session = RealtimeSession(SimpleNamespace(route=route))

    await session.interrupt_presentation()
    assert route.interrupted == [7]
    assert route.cancelled == []

    assert await session.cancel_task("task-1") is True
    assert route.cancelled == ["task-1"]


def test_cancel_turn_discards_output_before_policy_cancel():
    source = inspect.getsource(CascadeRoute.cancel_turn)
    discard_at = source.find("discard_turn")
    policy_at = source.find("self._policy_runtime.cancel")
    assert 0 <= discard_at < policy_at


@pytest.mark.asyncio
async def test_a_failed_prepare_is_not_retried_once_per_audio_frame() -> None:
    """append_audio prepares lazily, and Discord delivers ~50 frames a second.

    A route whose warmup failed was re-preparing on every frame, which turned
    one slow agent warmup into "Agent is already processing" and then a wall
    of timeouts that bricked the session.
    """
    import asyncio
    from types import SimpleNamespace

    from sleeptalk.session import RealtimeSession

    attempts = 0

    class FailingRoute:
        capabilities = None

        async def prepare(self, session):
            nonlocal attempts
            attempts += 1
            raise RuntimeError("warmup failed")

        def bind(self, session_id, emit):
            return None

        async def append_audio(self, pcm, *, participant_id=0):
            return None

    session = RealtimeSession(SimpleNamespace(route=FailingRoute()))

    for _ in range(50):
        with pytest.raises(RuntimeError):
            await session.append_audio(b"\0\0" * 960)

    assert attempts == 1, f"warmup ran {attempts} times for 50 frames"
