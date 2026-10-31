"""The terminal echo gate across a chunked answer.

Hybrid streams the answer from the provider in chunks, so the speaker
queue empties between two words. The gate used to read that as the end of
the utterance, which reopened the microphone onto the assistant's own
voice mid-sentence.
"""

from __future__ import annotations

import asyncio

import pytest
from sleeptalk.config import (
    AgentConfig,
    HybridRouteConfig,
    SleeptalkConfig,
    TransportConfig,
)
from sleeptalk.transports.terminal import TerminalTransport, _SpeakerOutput


class FakeSpeaker:
    """Only the queue depth the echo gate reads."""

    def __init__(self) -> None:
        self.queued_bytes = 0
        self.bytes_played = 0


def _transport() -> TerminalTransport:
    config = SleeptalkConfig(
        agent=AgentConfig(type="claude_code", name="m4rin", slug="sleeptalk"),
        route=HybridRouteConfig(),
        transport=TransportConfig(type="terminal"),
    )
    transport = TerminalTransport(config, {})
    transport._speaker = FakeSpeaker()
    return transport


def _speaking(transport: TerminalTransport, *, at: float) -> None:
    """What _on_audio_queued leaves behind for one chunk of answer audio."""
    transport._speaker.queued_bytes = 4096
    transport._echo_gate_active = True
    transport._awaiting_hush_after_bot = True
    transport._presentation_open = True
    transport._capture_resume_at = at + transport._echo_grace_ms / 1000


def test_a_drained_queue_mid_answer_is_not_the_end_of_speech() -> None:
    transport = _transport()
    _speaking(transport, at=0.0)
    transport._speaker.queued_bytes = 0  # jitter: the next chunk has not landed

    assert transport._speaker_busy(0.1) is True
    assert transport._speaker_busy(0.39) is True


def test_the_microphone_reopens_once_the_answer_really_ends() -> None:
    transport = _transport()
    _speaking(transport, at=0.0)
    transport._speaker.queued_bytes = 0

    assert transport._speaker_busy(0.41) is False


def test_the_hush_that_reopens_the_mic_is_never_asked_mid_answer() -> None:
    """The fragment turns came from slivers caught in these gaps.

    Measured: one sentence arrived as three turns — "Can you Tell me The
    latest Well The", then "What Yes States" — because a quiet frame
    between two of the assistant's words released the hush guard, the
    assembler collected a sliver, and the next chunk shut it again in time
    for endpointing to flush the sliver as its own utterance.
    """
    transport = _transport()
    _speaking(transport, at=0.0)
    transport._speaker.queued_bytes = 0  # jitter, not the end of the answer

    assert transport._hush_check_due(0.1) is False
    # So the guard is still up, and the assembler ignores the frame.
    assert transport._awaiting_hush_after_bot is True
    assert transport._assembler_should_ignore() is True


def test_the_hush_is_asked_for_once_the_answer_really_ends() -> None:
    transport = _transport()
    _speaking(transport, at=0.0)
    transport._speaker.queued_bytes = 0

    assert transport._hush_check_due(0.41) is True


def test_a_full_duplex_terminal_never_waits_for_hush() -> None:
    transport = _transport()
    transport._terminal_full_duplex = True
    _speaking(transport, at=0.0)
    transport._speaker.queued_bytes = 0

    assert transport._hush_check_due(1.0) is False


def test_full_duplex_capture_is_never_gated() -> None:
    transport = _transport()
    transport._terminal_full_duplex = True
    _speaking(transport, at=0.0)

    assert transport._assembler_should_ignore() is False


@pytest.mark.parametrize("queued", [0, 4096])
def test_a_quiet_speaker_past_the_grace_is_idle_whatever_the_gate_says(queued: int) -> None:
    transport = _transport()
    transport._speaker.queued_bytes = queued
    transport._capture_resume_at = 0.0

    assert transport._speaker_busy(1.0) is bool(queued)


class FakeCapture:
    """Only what the gate teardown touches."""

    def __init__(self, *, open: bool = True) -> None:
        self.open = open
        self.resets = 0

    def reset(self) -> None:
        self.resets += 1


def test_the_hush_wait_cannot_outlive_the_grace_it_guards() -> None:
    """Reported on terminal speakers: it hears you and captures nothing.

    The hush guard only lifted on a frame under the noise floor, and the
    frames it dropped skipped the barge detector too, so nothing could
    rescue it. A desk fan — or simply still talking — kept it shut for the
    rest of the session.
    """
    transport = _transport()
    _speaking(transport, at=0.0)
    transport._speaker.queued_bytes = 0

    assert transport._hush_check_due(0.1) is False  # still inside the grace
    transport._end_echo_gate(FakeCapture())

    assert transport._awaiting_hush_after_bot is False
    assert transport._hush_check_due(5.0) is False
    assert transport._assembler_should_ignore() is False
    assert any(event["event"] == "hush_expired" for event in transport.events)


def test_a_gate_that_ends_without_a_pending_hush_says_nothing_about_it() -> None:
    transport = _transport()
    transport._echo_gate_active = True

    transport._end_echo_gate(FakeCapture())

    assert not any(event["event"] == "hush_expired" for event in transport.events)
    assert any(event["event"] == "echo_gate_ended" for event in transport.events)


def test_a_quiet_frame_inside_the_grace_still_takes_the_fast_path() -> None:
    """Expiry is the backstop, not the normal way capture reopens."""
    transport = _transport()
    _speaking(transport, at=0.0)
    transport._speaker.queued_bytes = 0
    capture = FakeCapture()

    assert transport._hush_check_due(0.41) is True
    transport._release_hush(capture)

    assert transport._awaiting_hush_after_bot is False
    assert not any(event["event"] == "hush_expired" for event in transport.events)


# -- who tells the transport a presentation ended -------------------------


def test_a_route_that_says_it_finished_reopens_as_soon_as_the_queue_drains() -> None:
    """Hybrid calls finish_turn at the end of every _tell."""
    transport = _transport()
    _speaking(transport, at=0.0)
    transport._on_audio_finished(1)

    transport._speaker.queued_bytes = 4096  # still playing out
    assert transport._speaker_busy(0.1) is True

    transport._speaker.queued_bytes = 0  # played out
    assert transport._speaker_busy(0.1) is False, "made the user wait out a guess"


def test_a_route_that_says_nothing_keeps_the_grace_window_it_always_had() -> None:
    """Cascade never calls finish_turn, so nothing about it changes."""
    transport = _transport()
    _speaking(transport, at=0.0)
    transport._speaker.queued_bytes = 0

    assert transport._speaker_busy(0.1) is True
    assert transport._speaker_busy(0.39) is True
    assert transport._speaker_busy(0.41) is False


def test_a_finished_presentation_still_bridges_nothing_it_should_not() -> None:
    """The jitter bridge is for a stream still arriving, not a finished one."""
    transport = _transport()
    _speaking(transport, at=0.0)
    transport._speaker.queued_bytes = 0

    assert transport._speaker_busy(0.1) is True   # mid-stream gap
    transport._on_audio_finished(1)
    assert transport._speaker_busy(0.1) is False  # the stream is over


def test_finishing_a_superseded_turn_does_not_reopen_the_current_one() -> None:
    calls: list[int] = []
    speaker = FakeSpeaker()
    output = _SpeakerOutput(speaker, on_finish=calls.append)
    output._epoch = 7

    asyncio.run(output.finish_turn(6))
    assert calls == []
    asyncio.run(output.finish_turn(7))
    assert calls == [7]


def test_finishing_twice_reports_once() -> None:
    transport = _transport()
    _speaking(transport, at=0.0)

    transport._on_audio_finished(1)
    transport._on_audio_finished(1)

    finished = [e for e in transport.events if e["event"] == "presentation_finished"]
    assert len(finished) == 1
