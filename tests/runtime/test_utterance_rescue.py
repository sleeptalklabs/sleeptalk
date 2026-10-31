"""Flush-time utterance rescue: a sub-min_bytes utterance whose streaming
STT heard real speech must be delivered, not silently dropped (2026-08-15:
quiet-mic frames gated out by the capture VAD, Deepgram transcribed the
utterance fine, flush() discarded it, and the user had to repeat
themselves — the repeated text then surfaced inside the NEXT turn's
transcript)."""

from __future__ import annotations

import asyncio

import pytest

from sleeptalk.voice_loop.utterance import UtteranceAssembler
from sleeptalk.voice.configs import AdaptiveEndpointingConfig, TurnCompletionSignal


def _assembler(
    utterances: list,
    events: list,
    *,
    transcript: str = "",
    min_bytes: int = 1000,
) -> UtteranceAssembler:
    async def on_utterance(participant, pcm):
        utterances.append((participant, pcm))

    def on_capture_event(pid, event):
        events.append(event)

    return UtteranceAssembler(
        loop=asyncio.get_running_loop(),
        min_bytes=min_bytes,
        silence_seconds=0.08,
        on_utterance=on_utterance,
        should_ignore_audio=lambda: False,
        turn_completion_provider=lambda pid: TurnCompletionSignal(
            transcript=transcript, is_final=True, source="deepgram"
        ),
        endpointing_config=AdaptiveEndpointingConfig(enabled=False),
        on_capture_event=on_capture_event,
    )


@pytest.mark.asyncio
async def test_flush_rescues_utterance_the_stt_heard():
    utterances: list = []
    events: list = []
    assembler = _assembler(utterances, events, transcript="to speech. does this work")
    participant = object()
    # Only a trickle of audio passed the capture VAD — below min_bytes.
    assembler.participants[1] = participant
    assembler.buffers[1] = bytearray(b"\x01" * 160)
    assembler.flush(1)
    await asyncio.sleep(0)
    assert len(utterances) == 1
    assert utterances[0][0] is participant
    assert events and events[0]["kind"] == "utterance_rescued"
    assert events[0]["transcriptChars"] == len("to speech. does this work")


@pytest.mark.asyncio
async def test_flush_drops_silent_blip_without_transcript():
    utterances: list = []
    events: list = []
    assembler = _assembler(utterances, events, transcript="")
    assembler.participants[1] = object()
    assembler.buffers[1] = bytearray(b"\x01" * 160)
    assembler.flush(1)
    await asyncio.sleep(0)
    assert not utterances
    assert events and events[0]["kind"] == "utterance_dropped"


@pytest.mark.asyncio
async def test_flush_delivers_normal_utterance_without_rescue_event():
    utterances: list = []
    events: list = []
    assembler = _assembler(utterances, events, transcript="hello there")
    assembler.participants[1] = object()
    assembler.buffers[1] = bytearray(b"\x01" * 2000)
    assembler.flush(1)
    await asyncio.sleep(0)
    assert len(utterances) == 1
    assert [event["kind"] for event in events] == ["utterance_committed"]


@pytest.mark.asyncio
async def test_buffered_silence_does_not_satisfy_minimum_speech_floor():
    utterances: list = []
    events: list = []
    assembler = _assembler(utterances, events, transcript="", min_bytes=1000)
    assembler.participants[1] = object()
    assembler.buffers[1] = bytearray(b"\x00" * 2000)
    assembler.speech_audio_bytes[1] = 160

    assembler.flush(1)
    await asyncio.sleep(0)

    assert not utterances
    assert events[0]["kind"] == "utterance_dropped"
    assert events[0]["audioBytes"] == 2000
    assert events[0]["speechBytes"] == 160


@pytest.mark.asyncio
async def test_a_lagging_streaming_transcript_is_looked_at_again_soon():
    """The last voiced frame armed the endpoint timer with the stale text's
    whole budget ("What's two plus two, just the": 3.5 s), so a streaming
    transcript that was complete 0.4 s later still ended the turn at 1.8-3.5 s."""
    async def on_utterance(participant, pcm):
        return None

    text = {"now": "What's two plus two, just the"}
    assembler = UtteranceAssembler(
        loop=asyncio.get_running_loop(), min_bytes=1000, silence_seconds=0.08,
        on_utterance=on_utterance, should_ignore_audio=lambda: False,
        turn_completion_provider=lambda pid: TurnCompletionSignal(transcript=text["now"], source="deepgram"),
        endpointing_config=AdaptiveEndpointingConfig(enabled=True),
    )
    assembler.buffers[1] = bytearray(b"\x01" * 48_000)
    assembler.speech_audio_bytes[1] = 48_000
    delay = assembler._next_endpoint_delay_seconds(1)
    assert delay <= assembler.endpointing_config.streaming_recheck_ms / 1000
    text["now"] = "What's two plus two? Just the number, please."
    assert assembler._next_endpoint_delay_seconds(1) > assembler.endpointing_config.streaming_recheck_ms / 1000  # complete: its own budget
