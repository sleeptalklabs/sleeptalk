"""What the fresh-voice clock is worth to a Discord caller, in milliseconds.

tests/test_discord_endpoint_parity.py asserts the plumbing: that Discord's
live cascade listener resolves to web's endpointing config. This asserts the
timing that plumbing buys, which is the point of b3856f2 and the thing its
own tests never pinned.

Silero counts the frames inside its 900 ms stop window as speech. Measured
from the end of that window, an unfinished sentence's 1.8 s budget ends
2.7 s after the caller's last word; measured from the last voiced frame, it
ends at 1.8 s. That is the saving, and these tests fail if the clock goes
back.

Measured on synthetic Discord calls the same day (the in-process live
session of scripts/synthetic_call/discord_session.py, Cartesia ink-2
listening, 24 turns per arm - logs/discord-endpoint-timing-2026-10-15):
a live call does not collect that saving yet, because Discord's
speaking-stop event re-stamps the clock. `schedule_flush` calls
`_schedule_endpoint_check(pid)`, whose `voiced` defaults to True, so
`_voiced_at` moves to the moment the caller stopped transmitting and the
pause is measured from there either way. These tests therefore hold the
clock the config chooses, not an end-to-end wait.
"""
from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

pytest.importorskip("discord")

from sleeptalk.input_transcripts import input_endpointing_config
from sleeptalk.voice.configs import AdaptiveEndpointingConfig, TurnCompletionSignal
from sleeptalk.voice_loop.utterance import UtteranceAssembler

#: The frames Silero still calls speech after the caller's last word.
STOP_WINDOW_MS = 900
TRAILING_OFF = "so the thing I wanted to ask you about is whether"


class _Clock:
    """A loop whose time the test sets; timers are recorded, never run."""

    def __init__(self) -> None:
        self.now = 0.0
        self.timers: list[float] = []

    def time(self) -> float:
        return self.now

    def call_later(self, delay, _callback, *_args):
        self.timers.append(delay)
        return SimpleNamespace(cancel=lambda: None)


def _assembler(config: AdaptiveEndpointingConfig) -> tuple[UtteranceAssembler, _Clock]:
    clock = _Clock()

    async def _never(_pid, _pcm):  # pragma: no cover - nothing is flushed here
        raise AssertionError("the test never commits an utterance")

    assembler = UtteranceAssembler(
        loop=clock,
        min_bytes=4_800,
        silence_seconds=0.12,
        on_utterance=_never,
        should_ignore_audio=lambda: False,
        vad_mode="hybrid",
        turn_completion_provider=lambda _pid: TurnCompletionSignal(
            transcript=TRAILING_OFF, source="cartesia"
        ),
        endpointing_config=config,
    )
    # One second of captured speech for this participant, the last voiced
    # frame at t=0, and the stop window's frames counted as speech after it.
    assembler.buffers[1] = bytearray(b"\0" * 192_000)
    assembler.speech_audio_bytes[1] = 192_000
    assembler._voiced_at[1] = 0.0
    assembler.last_audio_at[1] = STOP_WINDOW_MS / 1000
    return assembler, clock


def _discord_config() -> AdaptiveEndpointingConfig:
    session = SimpleNamespace(config=SimpleNamespace(stt=SimpleNamespace(provider="cartesia", model="")))
    return input_endpointing_config(
        session, replace(AdaptiveEndpointingConfig(), local_responsiveness=True)
    )


def _ends_at_ms(config: AdaptiveEndpointingConfig) -> int:
    """When this config calls the turn over, in ms after the last voiced frame."""
    assembler, clock = _assembler(config)
    for step in range(0, 800):
        clock.now = step * 0.005
        if assembler._endpoint_decision(1).should_endpoint:
            return round(clock.now * 1000)
    raise AssertionError("the turn never ended")


def test_an_unfinished_sentence_ends_at_the_budget_from_the_last_word() -> None:
    """The caller trails off: the turn ends one uncertain budget after their
    last word, whatever Silero's stop window is still holding open."""
    config = _discord_config()
    assert config.fresh_voice_timing is True

    decision = _assembler(config)[0]._endpoint_decision(1)
    assert (decision.reason, decision.threshold_ms) == ("uncertain", 1_800)
    assert _ends_at_ms(config) == 1_800


def test_without_the_fresh_voice_clock_it_ends_a_stop_window_later() -> None:
    """b3856f2^: the pause was measured from the end of the stop window, so
    the same caller waited 900 ms longer for the same words."""
    before = replace(_discord_config(), streaming_pause_timing=False)
    assert before.fresh_voice_timing is False

    assert _ends_at_ms(before) == 1_800 + STOP_WINDOW_MS
    assert _ends_at_ms(before) - _ends_at_ms(_discord_config()) == STOP_WINDOW_MS
