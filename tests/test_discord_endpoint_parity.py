"""Discord endpointing keeps parity with web on the two things that decide
how long the caller waits before the acknowledgement.

Measured 2026-10-15 on logs/voice_latency.jsonl: Discord committed turns at a
median 1802 ms of silence against web's 900 ms, because 67% of its turns fell
into the 1.8 s "uncertain" bucket while 80% of web's fired in the 650 ms
"complete" one.
"""
from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

pytest.importorskip("discord")

from sleeptalk.input_transcripts import input_endpointing_config
from sleeptalk.voice.configs import AdaptiveEndpointingConfig


def _session(provider: str = "cartesia"):
    return SimpleNamespace(config=SimpleNamespace(stt=SimpleNamespace(provider=provider, model="")))


def test_live_cascade_listener_gets_the_fresh_voice_clock() -> None:
    """web_voice.session sets this inline; Discord goes through the helper."""
    config = replace(AdaptiveEndpointingConfig(), local_responsiveness=True)
    resolved = input_endpointing_config(_session(), config)
    assert resolved.streaming_pause_timing is True
    assert resolved.fresh_voice_timing is True


def test_moonshine_still_takes_the_local_streaming_path() -> None:
    config = replace(AdaptiveEndpointingConfig(), local_responsiveness=True)
    session = SimpleNamespace(
        config=SimpleNamespace(
            stt=SimpleNamespace(provider="local", model="moonshine-small-streaming-en")
        )
    )
    resolved = input_endpointing_config(session, config)
    assert resolved.local_streaming_input is True


def test_patient_endpointing_is_left_alone() -> None:
    config = AdaptiveEndpointingConfig()
    assert config.local_responsiveness is False
    resolved = input_endpointing_config(_session(), config)
    assert resolved.streaming_pause_timing is False
    assert resolved is config


def test_speech_start_reopens_speech_connections_that_idled_shut() -> None:
    """The bot sits in its channel for days; the socket is shut when the
    caller finally talks. Web and terminal both warm on speech start."""
    import sleeptalk.discord_voice.live_session as live

    warmed: list[object] = []
    original = live.warm_on_speech
    live.warm_on_speech = warmed.append
    try:
        session = SimpleNamespace(
            bot=SimpleNamespace(
                realtime_session=SimpleNamespace(note_caller_speech=None),
                policy=SimpleNamespace(resume_grace_ms=0),
            ),
            # A turn already holds the lock: the resume-grace branch, which
            # returns without needing the rest of a live session.
            turn_lock=SimpleNamespace(locked=lambda: True),
            _last_utterance_committed_at={},
        )
        live.PiDiscordLiveSession._speech_started(session, SimpleNamespace(id=1))
    finally:
        live.warm_on_speech = original
    assert warmed == [session.bot.realtime_session]
