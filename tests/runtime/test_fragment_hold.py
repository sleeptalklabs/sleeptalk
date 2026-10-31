"""Post-barge-in fragment holding: incomplete utterances right after a
barge-in wait for their continuation instead of being answered as turns."""

import asyncio
import time
from types import SimpleNamespace

import pytest

from sleeptalk.discord_voice import PiDiscordLiveSession
from sleeptalk.policy import VoicePolicy


class _Trace:
    def __init__(self):
        self.marks = []

    def mark(self, stage, **fields):
        self.marks.append(stage)


def _session(**policy_overrides):
    kwargs = {"fragment_hold_ms": 60, "fragment_barge_window_ms": 10_000}
    kwargs.update(policy_overrides)
    policy = VoicePolicy(**kwargs)
    return SimpleNamespace(
        bot=SimpleNamespace(policy=policy),
        _last_barge_at=time.monotonic(),
        _pending_fragments={},
    )


USER = SimpleNamespace(id=7)


@pytest.mark.asyncio
async def test_fragment_held_then_released_on_timeout():
    session = _session()
    trace = _Trace()
    out = await PiDiscordLiveSession._maybe_hold_fragment(
        session, USER, "can you tell me about the", trace
    )
    assert out == "can you tell me about the"
    assert "fragment_held" in trace.marks
    assert "fragment_released" in trace.marks


@pytest.mark.asyncio
async def test_fragment_merges_with_continuation():
    session = _session()
    trace = _Trace()

    async def continue_sentence():
        while USER.id not in session._pending_fragments:
            await asyncio.sleep(0.005)
        fragment = session._pending_fragments[USER.id]
        fragment["continuation"] = "capital of France?"
        fragment["event"].set()

    task = asyncio.create_task(continue_sentence())
    out = await PiDiscordLiveSession._maybe_hold_fragment(
        session, USER, "can you tell me about the", trace
    )
    await task
    assert out == "can you tell me about the capital of France?"
    assert "fragment_merged" in trace.marks


@pytest.mark.asyncio
async def test_complete_utterance_never_held():
    session = _session()
    trace = _Trace()
    out = await PiDiscordLiveSession._maybe_hold_fragment(
        session, USER, "what is the capital of France?", trace
    )
    assert out == "what is the capital of France?"
    assert trace.marks == []


@pytest.mark.asyncio
async def test_incomplete_utterance_held_without_recent_barge():
    # Hesitant mid-monologue pauses fragment just like barge-ins; the hold
    # no longer requires a recent barge-in.
    session = _session()
    session._last_barge_at = time.monotonic() - 60  # long ago
    trace = _Trace()
    out = await PiDiscordLiveSession._maybe_hold_fragment(
        session, USER, "and, also,", trace
    )
    assert out == "and, also,"
    assert "fragment_held" in trace.marks
    assert "fragment_released" in trace.marks


@pytest.mark.asyncio
async def test_hold_disabled_by_policy():
    session = _session(fragment_hold_ms=0)
    trace = _Trace()
    out = await PiDiscordLiveSession._maybe_hold_fragment(
        session, USER, "can you tell me about the", trace
    )
    assert out == "can you tell me about the"
    assert trace.marks == []
