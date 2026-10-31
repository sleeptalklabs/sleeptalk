"""Discord status clips no longer depend on the retired speech queue."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("discord")

from sleeptalk.discord_voice import PiDiscordLiveSession
from sleeptalk.voice_loop.lifecycle import LifecycleSupervisor


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["none", "answer", "new_turn", "hangup"])
async def test_status_clip_is_fenced_after_synthesis(monkeypatch, change):
    session = object.__new__(PiDiscordLiveSession)
    live = {"answer_queued": asyncio.Event()}
    session._live_turn = live
    session._lifecycle = LifecycleSupervisor()
    session.bot = object()
    session._status_line = lambda _: "Still working on it."
    play = AsyncMock()
    session.playback = SimpleNamespace(play_wav_bytes=play)

    async def synthesize(bot, text):
        assert text == "Still working on it."
        if change == "answer":
            live["answer_queued"].set()
        elif change == "new_turn":
            session._live_turn = {"answer_queued": asyncio.Event()}
        elif change == "hangup":
            session._lifecycle.begin_shutdown()
        return b"status clip"

    monkeypatch.setattr("sleeptalk.discord_voice.live_session._dynamic_clip", synthesize)
    await session._answer_status_query()
    if change == "none":
        play.assert_awaited_once_with(b"status clip")
    else:
        play.assert_not_awaited()


def test_discord_skips_its_apology_when_the_voice_itself_failed() -> None:
    """Discord, synthetic (fault proxy, stalled TTS): the route played the
    voice-unavailable tone, then Discord's own spoken apology waited 6 s on
    the same dead voice while holding the turn."""
    from sleeptalk.discord_voice.live_session import _apology_owed

    plain = RuntimeError("agent died")
    told = RuntimeError("agent died")
    told.caller_told = True
    voice = RuntimeError("TTS sent no audio within 8 s")
    voice.voice_failed = True
    assert _apology_owed(plain) is True
    assert _apology_owed(told) is False
    assert _apology_owed(voice) is False
