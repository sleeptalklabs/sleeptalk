"""Independent black-box acceptance for the package-owned voice policy runtime.

The tests deliberately drive only public ``RealtimeSession`` and
``CascadeRoute`` APIs.  Surface code supplies PCM and an ``AudioOutput``;
policy behavior is observed as normalized events and PCM writes.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import inspect
import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sleeptalk.adapter import AssistantDelta, ToolEvent, ToolPhase
from sleeptalk.policy import TurnPolicyValues, VoicePolicy
from sleeptalk.routes.audio_output import AudioOutputAccounting
from sleeptalk.routes.cascade import CascadeRoute
from sleeptalk.session import RealtimeSession
from sleeptalk.session_events import SessionEvent, SessionEventType
from sleeptalk.voice_loop.telemetry import VoiceLatencyTrace, cascade_turn_identity
from sleeptalk.voice_policy_runtime import VoicePolicyTimings


class StrictStreamingSTT:
    def __init__(self, result: str | BaseException, statistics: dict[str, Any]) -> None:
        self.result = result
        self.statistics = statistics
        self.appended: list[tuple[int, bytes]] = []
        self.finished: list[tuple[int, bytes, float]] = []
        self.signal_participants: list[int] = []
        self.closed = 0

    async def append_pcm(self, participant_id: int, pcm: bytes) -> None:
        self.appended.append((participant_id, pcm))

    async def finish_transcription(
        self,
        participant_id: int,
        *,
        fallback_pcm: bytes,
        timeout_seconds: float,
    ) -> str:
        self.finished.append((participant_id, fallback_pcm, timeout_seconds))
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result

    def last_turn_stats(self, participant_id: int) -> dict[str, Any]:
        return {**self.statistics, "participant": participant_id}

    def turn_completion_signal(self, participant_id: int):
        self.signal_participants.append(participant_id)
        return SimpleNamespace(transcript="LIVE_COMPLETION_SENTINEL", is_final=True)

    async def close(self) -> None:
        self.closed += 1


class AppendFailingStreamingSTT(StrictStreamingSTT):
    async def append_pcm(self, participant_id: int, pcm: bytes) -> None:
        self.appended.append((participant_id, pcm))
        raise RuntimeError("append ingress failed")


class BlockingStreamingSTT(StrictStreamingSTT):
    def __init__(self, result: str) -> None:
        super().__init__(result, {"coverage": 1.0})
        self.finish_started = asyncio.Event()
        self.release = asyncio.Event()

    async def finish_transcription(
        self,
        participant_id: int,
        *,
        fallback_pcm: bytes,
        timeout_seconds: float,
    ) -> str:
        self.finished.append((participant_id, fallback_pcm, timeout_seconds))
        self.finish_started.set()
        await self.release.wait()
        return str(self.result)


class StrictRestSTT:
    def __init__(self, result: str) -> None:
        self.result = result
        self.calls: list[bytes] = []
        self.closed = 0

    async def transcribe_pcm(self, pcm: bytes) -> str:
        self.calls.append(pcm)
        return self.result

    async def close(self) -> None:
        self.closed += 1


class TimeoutRestSTT(StrictRestSTT):
    async def transcribe_pcm(self, pcm: bytes) -> str:
        self.calls.append(pcm)
        await asyncio.sleep(1)
        return self.result


class PacedRestSTT(StrictRestSTT):
    """A listener on this machine: decoding costs time per second of audio."""

    rest_seconds_per_audio_second = 0.3

    async def transcribe_pcm(self, pcm: bytes) -> str:
        self.calls.append(pcm)
        await asyncio.sleep(len(pcm) / (48_000 * 2) * 0.1)
        return self.result


class ScriptedAgent:
    def __init__(
        self,
        *,
        sentence: str = "FINAL_SENTINEL",
        delay: float = 0.0,
        events: tuple[str, ...] = (),
    ) -> None:
        self.sentence = sentence
        self.delay = delay
        self.events = events
        self.prompts: list[str] = []
        self.aborted = 0
        self.closed = 0
        self.warmed = 0

    async def warmup(self) -> None:
        self.warmed += 1

    async def respond_streaming(self, text: str, *, on_event=None):
        self.prompts.append(text)
        if on_event is not None:
            for event in self.events:
                on_event(event)
        if self.delay:
            await asyncio.sleep(self.delay)
        yield self.sentence

    async def abort(self) -> None:
        self.aborted += 1

    async def close(self) -> None:
        self.closed += 1


class DelayedMultiToolAgent(ScriptedAgent):
    """Emit multiple tools after the opening acknowledgement has played."""

    async def respond_streaming(self, text: str, *, on_event=None):
        self.prompts.append(text)
        await asyncio.sleep(0.020)
        if on_event is not None:
            on_event("tooldetail:running first command")
            on_event("tool:bash")
        await asyncio.sleep(0.010)
        if on_event is not None:
            on_event("tooldetail:reading second file")
            on_event("tool:read")
        await asyncio.sleep(0.010)
        yield self.sentence


class StrictTTS:
    def __init__(self) -> None:
        self.texts: list[str] = []
        self.aborted = 0
        self.closed = 0

    async def synthesize(self, text: str):
        self.texts.append(text)
        yield f"PCM<{text}>".encode()

    async def abort(self) -> None:
        self.aborted += 1

    async def close(self) -> None:
        self.closed += 1


class StrictAudioOutput:
    def __init__(self) -> None:
        self.writes: list[tuple[bytes, int]] = []
        self.closed = 0
        self.account = AudioOutputAccounting(turn_id=-1)
        self.discarded: list[tuple[int, str]] = []

    async def write(self, pcm: bytes, turn_id: int) -> None:
        if self.closed:
            raise AssertionError("stale PCM reached a closed output")
        payload = bytes(pcm)
        self.writes.append((payload, turn_id))
        self.account.generated(len(payload))
        self.account.written(len(payload))

    async def discard_turn(self, turn_id: int, reason: str = "interrupted") -> None:
        self.discarded.append((turn_id, reason))

    def accounting(self) -> AudioOutputAccounting:
        return self.account

    async def close(self) -> None:
        self.closed += 1


class SignalingAudioOutput(StrictAudioOutput):
    def __init__(self) -> None:
        super().__init__()
        self.first_write = asyncio.Event()

    async def write(self, pcm: bytes, turn_id: int) -> None:
        await super().write(pcm, turn_id)
        self.first_write.set()


class BlockingTTS(StrictTTS):
    def __init__(self) -> None:
        super().__init__()
        self.first_chunk = asyncio.Event()
        self.release = asyncio.Event()

    async def synthesize(self, text: str):
        self.texts.append(text)
        yield b"FIRST_PCM"
        self.first_chunk.set()
        await self.release.wait()
        yield b"STALE_PCM"


class LongTaskAgent:
    """A tool operation remains alive until released or explicitly cancelled."""

    def __init__(self) -> None:
        self.prompts: list[str] = []
        self.first_started = asyncio.Event()
        self.first_released = asyncio.Event()
        self.aborted = 0
        self.closed = 0
        self.warmed = 0

    async def warmup(self) -> None:
        self.warmed += 1

    async def respond_streaming(self, text: str, *, on_event=None):
        self.prompts.append(text)
        if on_event:
            on_event(ToolEvent(name="bash", phase=ToolPhase.STARTED, detail="Running the full test suite"))
        self.first_started.set()
        await self.first_released.wait()
        yield "Full suite passed orchard sentinel."

    async def abort(self) -> None:
        self.aborted += 1
        self.first_released.set()

    async def close(self) -> None:
        self.closed += 1


@dataclass
class PublicAssembly:
    route: CascadeRoute
    policy: VoicePolicy
    config: Any
    usage_meter: Any = None
    transport: Any = None
    agent: Any = None
    stt: Any = None
    streaming_stt: Any = None
    tts: Any = None
    tts_fallbacks: tuple[Any, ...] = ()


def make_policy() -> VoicePolicy:
    return VoicePolicy(
        narration_style="narrated",
        instant_ack=True,
        instant_ack_mode="blind",
        keepalive_interval_s=0.010,
        preamble_pools={
            "ack": ("Ack orchard sentinel.",),
            "action": ("Action orchard sentinel.",),
            "working": ("Working orchard sentinel.",),
            "reassure": ("Reassure orchard sentinel.",),
        },
        reassure_tool_pools={"bash": ("Bash reassure orchard sentinel.",)},
        narration_phrases={"bash": "Tool orchard sentinel."},
        turns=TurnPolicyValues(
            long_task_check_in_seconds=0.025,
            long_task_auto_continue_seconds=0.015,
        ),
    )


def make_session(
    *,
    agent: Any,
    tts: Any,
    output: StrictAudioOutput,
    streaming_stt: StrictStreamingSTT | None = None,
    rest_stt: StrictRestSTT | None = None,
    policy: VoicePolicy | None = None,
    timings: VoicePolicyTimings | None = None,
    config: Any = None,
) -> RealtimeSession:
    route = CascadeRoute(
        agent=agent,
        tts=tts,
        stt=rest_stt,
        stt_streaming=streaming_stt,
        audio_output=output,
        policy_timings=timings,
    )
    assembly = PublicAssembly(
        route=route,
        policy=policy or make_policy(),
        config=config
        or SimpleNamespace(
            stt=SimpleNamespace(
                streaming_timeout_seconds=0.071,
                rest_timeout_seconds=0.2,
            )
        ),
        agent=agent,
        stt=rest_stt,
        streaming_stt=streaming_stt,
        tts=tts,
    )
    return RealtimeSession(assembly)


def _rich_latency_config() -> SimpleNamespace:
    return SimpleNamespace(
        agent=SimpleNamespace(type="pi", name="deepre", model="opus"),
        route=SimpleNamespace(type="cascade"),
        stt=SimpleNamespace(
            provider="deepgram",
            model="nova-3",
            streaming_timeout_seconds=0.071,
            rest_timeout_seconds=0.2,
        ),
        tts=SimpleNamespace(provider="cartesia", model="sonic", voice_id="voice-1"),
        policy=SimpleNamespace(reply_length="full"),
    )


def _trace_events(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def make_long_task_session(
    *,
    agent: LongTaskAgent,
    tts: StrictTTS,
    output: StrictAudioOutput,
    auto_continue_seconds: float,
) -> RealtimeSession:
    policy = VoicePolicy(
        instant_ack=False,
        keepalive_interval_s=0,
        turns=TurnPolicyValues(
            long_task_check_in_seconds=0.010,
            long_task_auto_continue_seconds=auto_continue_seconds,
        ),
    )
    return make_session(
        agent=agent,
        tts=tts,
        output=output,
        policy=policy,
        timings=VoicePolicyTimings(
            keepalive_interval_seconds=0,
            long_task_check_in_seconds=None,
            long_task_auto_continue_seconds=None,
        ),
    )





def transcript_event(events: list[SessionEvent], turn_id: int) -> SessionEvent:
    return next(
        event
        for event in events
        if event.type is SessionEventType.TRANSCRIPT_FINAL and event.turn_id == turn_id
    )


@pytest.mark.asyncio
async def test_public_pcm_ingress_finalizes_streaming_stt_and_publishes_stats() -> None:
    streaming = StrictStreamingSTT(
        "STREAMING_TRANSCRIPT_SENTINEL",
        {"coverage": 0.991, "finalReason": "utterance_end"},
    )
    rest = StrictRestSTT("REST_MUST_NOT_RUN")
    agent = ScriptedAgent()
    tts = StrictTTS()
    output = StrictAudioOutput()
    session = make_session(
        agent=agent,
        tts=tts,
        output=output,
        streaming_stt=streaming,
        rest_stt=rest,
        policy=VoicePolicy(instant_ack=False, keepalive_interval_s=0),
    )
    live_events: list[SessionEvent] = []
    unsubscribe = session.subscribe(live_events.append)

    chunk_one = b"\x01\x10" * 120
    chunk_two = b"\x02\x20" * 120
    await session.append_audio(chunk_one, participant_id=71)
    await session.append_audio(chunk_two, participant_id=71)
    completion = session.turn_completion_signal(71)
    pcm = b"\x01" * 960
    outcome = await session.run_turn(
        pcm=pcm,
        extras={"participant_id": 71},
        output=output,
    )

    assert outcome.status == "completed"
    assert streaming.appended == [
        (71, chunk_one),
        (71, chunk_two),
    ]
    assert streaming.finished == [(71, pcm, 0.071)]
    assert completion.transcript == "LIVE_COMPLETION_SENTINEL"
    assert streaming.signal_participants == [71]
    assert rest.calls == []
    assert agent.prompts == ["STREAMING_TRANSCRIPT_SENTINEL"]
    event = transcript_event(live_events, 1)
    assert event.payload["sttSource"] == "streaming"
    assert event.payload["sttStatistics"] == {
        "coverage": 0.991,
        "finalReason": "utterance_end",
        "participant": 71,
    }
    assert any(e.type is SessionEventType.RESPONSE_COMPLETED for e in live_events)
    assert output.writes == [(b"PCM<FINAL_SENTINEL>", 1)]

    unsubscribe()
    event_count = len(live_events)
    await session.run_turn(text="UNSUBSCRIBE_SENTINEL", output=output)
    assert len(live_events) == event_count
    await session.aclose()
    assert streaming.closed == rest.closed == agent.closed == tts.closed == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stream_result", "expected_source"),
    [
        ("tiny", "rest_coverage_fallback"),
        (RuntimeError("stream failed"), "rest_stream_error"),
    ],
)
async def test_public_turn_uses_rest_fallback_for_truncated_or_failed_stream(
    stream_result: str | BaseException,
    expected_source: str,
) -> None:
    streaming = StrictStreamingSTT(stream_result, {"coverage": 0.21})
    rest = StrictRestSTT("REST_FALLBACK_SENTINEL")
    agent = ScriptedAgent()
    tts = StrictTTS()
    output = StrictAudioOutput()
    session = make_session(
        agent=agent,
        tts=tts,
        output=output,
        streaming_stt=streaming,
        rest_stt=rest,
        policy=VoicePolicy(instant_ack=False, keepalive_interval_s=0),
    )
    pcm = b"\x02" * 192_000

    await session.append_audio(b"live", participant_id=9)
    await session.run_turn(
        pcm=pcm,
        extras={"participant_id": 9},
        output=output,
    )

    assert rest.calls == [pcm]
    assert agent.prompts == ["REST_FALLBACK_SENTINEL"]
    event = transcript_event(session.events, 1)
    assert event.payload["sttSource"] == expected_source
    assert event.payload["sttStatistics"] == {
        "coverage": 0.21,
        "participant": 9,
    }
    await session.aclose()


@pytest.mark.asyncio
async def test_trailing_endpoint_silence_does_not_reject_complete_stream() -> None:
    streaming = StrictStreamingSTT(
        "short text",
        {"transcript_audio_ms": 820, "sent_audio_ms": 3_200},
    )
    rest = StrictRestSTT("REST_MUST_NOT_RUN")
    agent = ScriptedAgent()
    session = make_session(
        agent=agent,
        tts=StrictTTS(),
        output=StrictAudioOutput(),
        streaming_stt=streaming,
        rest_stt=rest,
        policy=VoicePolicy(instant_ack=False, keepalive_interval_s=0),
    )
    pcm = b"\x04" * 307_200  # 3.2 seconds including endpointing silence.

    await session.append_audio(b"\x04\x00" * 24_000, participant_id=12)
    await session.run_turn(pcm=pcm, extras={"participant_id": 12})

    assert rest.calls == []
    assert agent.prompts == ["short text"]
    assert transcript_event(session.events, 1).payload["sttSource"] == "streaming"
    await session.aclose()


@pytest.mark.asyncio
async def test_rest_timeout_preserves_usable_streaming_transcript() -> None:
    streaming = StrictStreamingSTT("tiny", {"coverage": 0.2})
    rest = TimeoutRestSTT("TOO_LATE")
    agent = ScriptedAgent()
    session = make_session(
        agent=agent,
        tts=StrictTTS(),
        output=StrictAudioOutput(),
        streaming_stt=streaming,
        rest_stt=rest,
        policy=VoicePolicy(instant_ack=False, keepalive_interval_s=0),
    )
    pcm = b"\x05" * 192_000

    await session.append_audio(b"\x05\x00" * 24_000, participant_id=13)
    await session.run_turn(pcm=pcm, extras={"participant_id": 13})

    assert rest.calls == [pcm]
    assert agent.prompts == ["tiny"]
    assert (
        transcript_event(session.events, 1).payload["sttSource"]
        == "streaming_rest_timeout"
    )
    await session.aclose()


@pytest.mark.asyncio
async def test_a_long_turn_is_not_dropped_when_local_decoding_outlasts_the_timeout() -> None:
    """The caller talked for eight seconds and the stream lost the turn. A
    listener on this machine needs longer than stt.rest_timeout_seconds to read
    that much audio, so the fixed timeout threw the turn away and the agent
    heard nothing of it; the budget now follows the recording."""
    streaming = StrictStreamingSTT(RuntimeError("stream lost the turn"), {"coverage": 0.0})
    rest = PacedRestSTT("LONG_TURN_RECOVERED_SENTINEL")
    agent = ScriptedAgent()
    session = make_session(
        agent=agent,
        tts=StrictTTS(),
        output=StrictAudioOutput(),
        streaming_stt=streaming,
        rest_stt=rest,
        policy=VoicePolicy(instant_ack=False, keepalive_interval_s=0),
    )
    pcm = b"\x06" * (8 * 48_000 * 2)  # eight seconds: 0.8 s to read, 0.2 s allowed

    await session.append_audio(b"\x06\x00" * 24_000, participant_id=14)
    await session.run_turn(pcm=pcm, extras={"participant_id": 14})

    assert agent.prompts == ["LONG_TURN_RECOVERED_SENTINEL"]
    assert transcript_event(session.events, 1).payload["sttSource"] == "rest_stream_error"
    await session.aclose()


@pytest.mark.asyncio
async def test_stream_append_failure_keeps_capture_alive_and_uses_rest() -> None:
    streaming = AppendFailingStreamingSTT(
        "STREAM_MUST_NOT_FINALIZE",
        {"coverage": 0.0, "appendHealthy": False},
    )
    rest = StrictRestSTT("REST_AFTER_APPEND_FAILURE_SENTINEL")
    agent = ScriptedAgent()
    tts = StrictTTS()
    output = StrictAudioOutput()
    session = make_session(
        agent=agent,
        tts=tts,
        output=output,
        streaming_stt=streaming,
        rest_stt=rest,
        policy=VoicePolicy(instant_ack=False, keepalive_interval_s=0),
    )
    pcm = b"\x03" * 960

    # Capture continues: the public ingress absorbs streaming-provider failure.
    await session.append_audio(b"failed-live-chunk", participant_id=37)
    outcome = await session.run_turn(
        pcm=pcm,
        extras={"participant_id": 37},
        output=output,
    )

    assert outcome.status == "completed"
    assert streaming.appended == [(37, b"failed-live-chunk")]
    assert streaming.finished == []
    assert rest.calls == [pcm]
    assert agent.prompts == ["REST_AFTER_APPEND_FAILURE_SENTINEL"]
    event = transcript_event(session.events, 1)
    assert event.payload["sttSource"] == "rest_stream_error"
    assert event.payload["sttStatistics"] == {
        "coverage": 0.0,
        "appendHealthy": False,
        "participant": 37,
    }
    assert output.writes == [(b"PCM<FINAL_SENTINEL>", 1)]
    await session.aclose()


@pytest.mark.asyncio
async def test_root_policy_drives_audio_and_normalized_live_events() -> None:
    policy = make_policy()
    timings = VoicePolicyTimings(
        ack_delay_seconds=0.005,
        action_delay_seconds=0.008,
        keepalive_interval_seconds=None,
        long_task_check_in_seconds=0,
        long_task_auto_continue_seconds=0,
    )

    plain_agent = ScriptedAgent(delay=0.08)
    plain_tts = StrictTTS()
    plain_output = StrictAudioOutput()
    plain = make_session(
        agent=plain_agent,
        tts=plain_tts,
        output=plain_output,
        policy=policy,
        timings=timings,
    )
    plain_events: list[SessionEvent] = []
    plain.subscribe(plain_events.append)
    await plain.run_turn(text="plain policy turn", output=plain_output)

    plain_pools = [
        event.payload["pool"] for event in plain_events if event.type is SessionEventType.PROGRESS
    ]
    assert {"ack", "reassure"} <= set(plain_pools)
    assert "action" not in plain_pools
    assert {
        "Ack orchard sentinel.",
        "Reassure orchard sentinel.",
        "FINAL_SENTINEL",
    } <= set(plain_tts.texts)
    assert all(turn_id == 1 for _pcm, turn_id in plain_output.writes)
    await plain.aclose()

    tool_agent = ScriptedAgent(delay=0.08, events=("tool:bash",))
    tool_tts = StrictTTS()
    tool_output = StrictAudioOutput()
    tool_session = make_session(
        agent=tool_agent,
        tts=tool_tts,
        output=tool_output,
        policy=policy,
        timings=timings,
    )
    tool_events: list[SessionEvent] = []
    tool_session.subscribe(tool_events.append)
    await tool_session.run_turn(text="tool policy turn", output=tool_output)

    tool_pools = [
        event.payload["pool"] for event in tool_events if event.type is SessionEventType.PROGRESS
    ]
    assert {"tool_narration", "reassure"} <= set(tool_pools)
    assert "working" not in tool_pools
    assert {
        "Tool orchard sentinel.",
        "Bash reassure orchard sentinel.",
    } <= set(tool_tts.texts)
    assert any(event.type is SessionEventType.AGENT_EVENT for event in tool_events)
    assert any(event.type is SessionEventType.TOOL_STARTED for event in tool_events)
    assert any(event.type is SessionEventType.RESPONSE_COMPLETED for event in tool_events)
    assert all(
        pcm == f"PCM<{text}>".encode()
        for text, (pcm, _turn_id) in zip(tool_tts.texts, tool_output.writes, strict=True)
    )
    await tool_session.aclose()


class ReadingAgent(ScriptedAgent):
    """Reads a long passage out in one go, then runs a slow command."""

    passage = ("Long sentence one.", "Long sentence two.", "Long sentence three.")

    async def respond_streaming(self, text: str, *, on_event=None):
        self.prompts.append(text)
        for sentence in self.passage:
            yield sentence
        if on_event is not None:
            on_event("tool:bash")
        await asyncio.sleep(0.6)
        yield "Done now."


class PacedTTS(StrictTTS):
    """0.15 s of audio per line, so a sentence outlasts the keepalive interval."""

    async def synthesize(self, text: str):
        self.texts.append(text)
        yield b"\x00" * 14_400


class PacedAudioOutput(StrictAudioOutput):
    """Plays in real time, like a call: a write returns as its audio plays."""

    async def write(self, pcm: bytes, turn_id: int) -> None:
        await super().write(pcm, turn_id)
        await asyncio.sleep(len(pcm) / 96_000)


@pytest.mark.asyncio
async def test_progress_lines_wait_for_the_answer_being_read() -> None:
    # A "still going" line or tool narration used to take the voice between
    # sentences the agent had already sent, cutting into a long reading.
    tts = PacedTTS()
    output = PacedAudioOutput()
    session = make_session(
        agent=ReadingAgent(),
        tts=tts,
        output=output,
        policy=make_policy(),
        timings=VoicePolicyTimings(
            ack_delay_seconds=5,
            action_delay_seconds=5,
            keepalive_interval_seconds=0.05,
            long_task_check_in_seconds=0,
            long_task_auto_continue_seconds=0,
        ),
    )
    await session.run_turn(text="read me the chapter", output=output)

    said = tts.texts
    first = said.index("Long sentence one.")
    assert said[first:first + 3] == list(ReadingAgent.passage), said
    assert said[-1] == "Done now."
    # Silence after the reading still gets a progress line.
    assert "Bash reassure orchard sentinel." in said[first + 3:-1], said
    await session.aclose()


@pytest.mark.asyncio
async def test_typed_tool_complete_and_assistant_delta_reach_the_bus() -> None:
    agent = ScriptedAgent(
        events=(
            ToolEvent(
                name="bash",
                phase=ToolPhase.COMPLETED,
                call_id="c1",
                detail="ran ls",
                result_summary="ok",
            ),
            AssistantDelta(text="Done, the board is running."),
        )
    )
    tts = StrictTTS()
    output = StrictAudioOutput()
    session = make_session(
        agent=agent,
        tts=tts,
        output=output,
        policy=VoicePolicy(instant_ack=False, keepalive_interval_s=0),
        timings=VoicePolicyTimings(
            ack_delay_seconds=0,
            action_delay_seconds=0,
            keepalive_interval_seconds=None,
            long_task_check_in_seconds=0,
            long_task_auto_continue_seconds=0,
        ),
    )
    observed: list[SessionEvent] = []
    session.subscribe(observed.append)
    await session.run_turn(text="typed events", output=output)
    completed = [
        event for event in observed if event.type is SessionEventType.TOOL_COMPLETED
    ]
    assert completed, "typed ToolEvent COMPLETED must reach the bus"
    assert completed[0].payload["name"] == "bash"
    assert completed[0].payload["callId"] == "c1"
    assert completed[0].payload["detail"] == "ran ls"
    assert completed[0].payload["result_summary"] == "ok"
    deltas = [
        event
        for event in observed
        if event.type is SessionEventType.AGENT_EVENT
        and event.payload.get("name") == "text"
        and event.payload.get("text") == "Done, the board is running."
    ]
    sentences = [
        event
        for event in observed
        if event.type is SessionEventType.AGENT_EVENT
        and event.payload.get("name") == "sentence"
        and event.payload.get("text") == "Done, the board is running."
    ]
    assert deltas, "AssistantDelta tokens must reach the bus as text"
    assert not sentences, "AssistantDelta must not be spoken as a sentence"
    await session.aclose()


@pytest.mark.asyncio
async def test_tool_progression_speaks_after_ack_and_for_each_tool() -> None:
    tts = StrictTTS()
    output = StrictAudioOutput()
    session = make_session(
        agent=DelayedMultiToolAgent(),
        tts=tts,
        output=output,
        policy=VoicePolicy(
            narration_style="narrated",
            instant_ack=True,
            instant_ack_mode="blind",
            keepalive_interval_s=0,
            preamble_pools={"ack": ("Ack orchard sentinel.",)},
            narration_phrases={
                "bash": "Bash fallback must not replace detail.",
                "read": "Read fallback must not replace detail.",
            },
        ),
        timings=VoicePolicyTimings(
            ack_delay_seconds=0.005,
            action_delay_seconds=1,
            keepalive_interval_seconds=0,
            long_task_check_in_seconds=0,
            long_task_auto_continue_seconds=0,
        ),
    )

    await session.run_turn(text="multi-tool policy turn", output=output)

    assert tts.texts == [
        "Ack orchard sentinel.",
        "Running first command.",
        "Reading second file.",
        "FINAL_SENTINEL",
    ]
    assert [
        event.payload["text"]
        for event in session.events
        if event.type is SessionEventType.PROGRESS
        and event.payload.get("pool") == "tool_narration"
    ] == ["Running first command.", "Reading second file."]
    await session.aclose()


class DuplicateToolAgent(ScriptedAgent):
    """Same tool start twice, then an unmapped name — kernel must not stutter."""

    async def respond_streaming(self, text: str, *, on_event=None):
        self.prompts.append(text)
        if on_event is not None:
            on_event("tool:bash")
            on_event("tool:bash")
            on_event("tool:commandExecution")
        yield self.sentence


@pytest.mark.asyncio
async def test_kernel_speaks_a_generic_tool_line_once() -> None:
    tts = StrictTTS()
    output = StrictAudioOutput()
    session = make_session(
        agent=DuplicateToolAgent(),
        tts=tts,
        output=output,
        policy=VoicePolicy(
            narration_style="narrated",
            instant_ack=False,
            keepalive_interval_s=0,
            narration_phrases={
                "bash": "Running a command.",
                "other": "Working on it.",
            },
        ),
        timings=VoicePolicyTimings(
            ack_delay_seconds=1,
            action_delay_seconds=1,
            keepalive_interval_seconds=0,
            long_task_check_in_seconds=0,
            long_task_auto_continue_seconds=0,
        ),
    )

    await session.run_turn(text="duplicate tool turn", output=output)

    assert tts.texts.count("Running a command.") == 1
    assert "Working on it." not in tts.texts
    await session.aclose()


@pytest.mark.asyncio
async def test_fast_answer_has_no_opening_preamble() -> None:
    tts = StrictTTS()
    output = StrictAudioOutput()
    session = make_session(
        agent=ScriptedAgent(),
        tts=tts,
        output=output,
        policy=VoicePolicy(
            instant_ack=True,
            keepalive_interval_s=0,
            preamble_pools={"ack": ("ACK_MUST_NOT_PLAY",)},
        ),
        timings=VoicePolicyTimings(
            ack_delay_seconds=0.050,
            action_delay_seconds=0.050,
            keepalive_interval_seconds=0,
            long_task_check_in_seconds=0,
            long_task_auto_continue_seconds=0,
        ),
    )

    await session.run_turn(text="fast answer", output=output)

    assert tts.texts == ["FINAL_SENTINEL"]
    await session.aclose()


@pytest.mark.asyncio
async def test_policy_ack_drops_agent_duplicate_preamble() -> None:
    class DuplicatePreambleAgent(ScriptedAgent):
        async def respond_streaming(self, text: str, *, on_event=None):
            self.prompts.append(text)
            await asyncio.sleep(0.030)
            yield "Ack orchard sentinel."
            yield "FINAL_SENTINEL"

    tts = StrictTTS()
    output = StrictAudioOutput()
    session = make_session(
        agent=DuplicatePreambleAgent(),
        tts=tts,
        output=output,
        policy=VoicePolicy(
            instant_ack=True,
            instant_ack_mode="blind",
            keepalive_interval_s=0,
            preamble_pools={"ack": ("Ack orchard sentinel.",)},
        ),
        timings=VoicePolicyTimings(
            ack_delay_seconds=0.005,
            action_delay_seconds=0.010,
            keepalive_interval_seconds=0,
            long_task_check_in_seconds=0,
            long_task_auto_continue_seconds=0,
        ),
    )

    await session.run_turn(text="slow answer", output=output)

    assert tts.texts == ["Ack orchard sentinel.", "FINAL_SENTINEL"]
    assert [
        event.payload["pool"]
        for event in session.events
        if event.type is SessionEventType.PROGRESS
    ] == ["ack", "preamble_dropped"]
    await session.aclose()


@pytest.mark.asyncio
async def test_slow_ack_records_cascade_latency_stages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log_path = tmp_path / "voice_latency.jsonl"
    monkeypatch.setenv("VOICE_LATENCY_LOG_PATH", str(log_path))

    class DuplicatePreambleAgent(ScriptedAgent):
        async def respond_streaming(self, text: str, *, on_event=None):
            self.prompts.append(text)
            await asyncio.sleep(0.030)
            yield "Ack orchard sentinel."
            yield "FINAL_SENTINEL"

    config = _rich_latency_config()
    tts = StrictTTS()
    output = StrictAudioOutput()
    session = make_session(
        agent=DuplicatePreambleAgent(),
        tts=tts,
        output=output,
        config=config,
        policy=VoicePolicy(
            instant_ack=True,
            instant_ack_mode="blind",
            keepalive_interval_s=0,
            preamble_pools={"ack": ("Ack orchard sentinel.",)},
        ),
        timings=VoicePolicyTimings(
            ack_delay_seconds=0.005,
            action_delay_seconds=0.010,
            keepalive_interval_seconds=0,
            long_task_check_in_seconds=0,
            long_task_auto_continue_seconds=0,
        ),
    )
    trace = VoiceLatencyTrace(
        surface="test",
        transport="test",
        persona="test",
        metadata=cascade_turn_identity(config, session_id=session.session_id),
    )

    await session.run_turn(
        text="slow answer",
        output=output,
        extras={"latency_trace": trace},
    )

    events = _trace_events(log_path)
    stages = [event["stage"] for event in events]
    assert "instant_ack_played" in stages or "preamble_played" in stages
    assert "agent_first_speakable_segment" in stages
    summary = next(event for event in events if event["stage"] == "turn_audio_summary")
    assert summary["measurement"] == "output_enqueue"
    assert "ttfrMs" not in summary
    assert any(event["stage"] == "answer_first_output_enqueue" for event in events)
    first = events[0]
    assert first["agent"] == "pi"
    assert first["agentName"] == "deepre"
    assert first["requestedAgentModel"] == "opus"
    assert first["route"] == "cascade"
    assert first["sttProvider"] == "deepgram"
    assert first["ttsProvider"] == "cartesia"
    assert first["replyLength"] == "full"
    assert first["sessionId"] == session.session_id
    await session.aclose()


@pytest.mark.asyncio
async def test_fast_answer_trace_has_speakable_without_preamble(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log_path = tmp_path / "voice_latency.jsonl"
    monkeypatch.setenv("VOICE_LATENCY_LOG_PATH", str(log_path))
    tts = StrictTTS()
    output = StrictAudioOutput()
    session = make_session(
        agent=ScriptedAgent(),
        tts=tts,
        output=output,
        policy=VoicePolicy(
            instant_ack=True,
            keepalive_interval_s=0,
            preamble_pools={"ack": ("ACK_MUST_NOT_PLAY",)},
        ),
        timings=VoicePolicyTimings(
            ack_delay_seconds=0.050,
            action_delay_seconds=0.050,
            keepalive_interval_seconds=0,
            long_task_check_in_seconds=0,
            long_task_auto_continue_seconds=0,
        ),
    )
    trace = VoiceLatencyTrace(surface="test", transport="test", persona="test")

    await session.run_turn(
        text="fast answer",
        output=output,
        extras={"latency_trace": trace},
    )

    stages = [event["stage"] for event in _trace_events(log_path)]
    assert "preamble_played" not in stages
    assert "instant_ack_played" not in stages
    assert "agent_first_speakable_segment" in stages
    await session.aclose()


@pytest.mark.asyncio
async def test_public_smart_ack_action_claims_the_later_action_tier() -> None:
    policy = VoicePolicy(
        instant_ack=True,
        instant_ack_mode="smart",
        keepalive_interval_s=0,
        preamble_pools={
            "ack": ("ACK_SENTINEL",),
            "ack_action": ("COMBINED_SENTINEL",),
            "action": ("ACTION_MUST_NOT_PLAY",),
        },
    )
    tts = StrictTTS()
    output = StrictAudioOutput()
    session = make_session(
        agent=ScriptedAgent(delay=0.040),
        tts=tts,
        output=output,
        policy=policy,
        timings=VoicePolicyTimings(
            ack_delay_seconds=0.005,
            action_delay_seconds=0.008,
            keepalive_interval_seconds=0,
            long_task_check_in_seconds=0,
            long_task_auto_continue_seconds=0,
        ),
    )

    await session.run_turn(text="what is the latest orchard status?", output=output)

    assert tts.texts == ["COMBINED_SENTINEL", "FINAL_SENTINEL"]
    assert [
        event.payload["pool"]
        for event in session.events
        if event.type is SessionEventType.PROGRESS
    ] == ["ack_action"]
    assert output.writes == [
        (b"PCM<COMBINED_SENTINEL>", 1),
        (b"PCM<FINAL_SENTINEL>", 1),
    ]
    await session.aclose()


@pytest.mark.asyncio
async def test_public_smart_ack_budget_falls_back_while_stt_is_pending() -> None:
    streaming = BlockingStreamingSTT("hello there")
    tts = StrictTTS()
    output = SignalingAudioOutput()
    policy = VoicePolicy(
        instant_ack=True,
        instant_ack_mode="smart",
        smart_ack_budget_ms=10,
        keepalive_interval_s=0,
        preamble_pools={
            "ack": ("BUDGET_ACK_SENTINEL",),
            "action": ("ACTION_SENTINEL",),
        },
    )
    session = make_session(
        agent=ScriptedAgent(),
        tts=tts,
        output=output,
        streaming_stt=streaming,
        policy=policy,
        timings=VoicePolicyTimings(
            ack_delay_seconds=0.005,
            action_delay_seconds=0.008,
            keepalive_interval_seconds=0,
            long_task_check_in_seconds=0,
            long_task_auto_continue_seconds=0,
        ),
    )
    await session.append_audio(b"\x01\x00" * 120, participant_id=22)
    turn = asyncio.create_task(
        session.run_turn(
            pcm=b"\x01\x00" * 480,
            extras={"participant_id": 22},
            output=output,
        )
    )

    await asyncio.wait_for(streaming.finish_started.wait(), timeout=0.2)
    await asyncio.wait_for(output.first_write.wait(), timeout=0.2)
    assert not streaming.release.is_set(), "the budget ack must precede STT completion"
    assert output.writes == [(b"PCM<BUDGET_ACK_SENTINEL>", 1)]

    streaming.release.set()
    await asyncio.wait_for(turn, timeout=1)

    assert tts.texts == ["BUDGET_ACK_SENTINEL", "FINAL_SENTINEL"]
    assert [
        event.payload["pool"]
        for event in session.events
        if event.type is SessionEventType.PROGRESS
    ] == ["ack"]
    await session.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("preset", ["balanced", "aggressive", "relaxed"])
async def test_long_task_keeps_original_request_and_finishes_without_confirmation(preset) -> None:
    original = "Run the full suite and keep going until it is done"
    agent, tts, output = LongTaskAgent(), StrictTTS(), StrictAudioOutput()
    session = make_long_task_session(agent=agent, tts=tts, output=output, auto_continue_seconds=0.01)
    session.policy.endpointing = preset
    session.policy.local_responsiveness = True
    observed: list[SessionEvent] = []
    session.subscribe(observed.append)
    turn = asyncio.create_task(session.run_turn(text=original, output=output))
    try:
        await asyncio.wait_for(agent.first_started.wait(), 1)
        # Cross several of the saved legacy checkpoint/auto-continue windows.
        await asyncio.sleep(0.06)
        assert not turn.done()
        assert agent.aborted == 0 and agent.prompts == [original]
        active = session.route.active_turn()
        assert active is not None and active.turn_id == 1
        assert tts.texts == ["Running the full test suite."]  # announce work; no confirmation question
        agent.first_released.set()
        outcome = await asyncio.wait_for(turn, 1)
        assert outcome.status == "completed"
        assert outcome.generated == ["Full suite passed orchard sentinel."]
        assert (b"PCM<Full suite passed orchard sentinel.>", 1) in output.writes
        assert session.route.active_turn() is None
        await asyncio.sleep(0.03)
        assert agent.prompts == [original] and agent.aborted == 0
        assert not any(e.payload.get("pool", "").startswith("long_task_") for e in observed)
        started = [e for e in observed if e.type is SessionEventType.USAGE_ATTEMPT_STARTED]
        finished = [e for e in observed if e.type is SessionEventType.USAGE_ATTEMPT_FINISHED]
        assert {e.turn_id for e in started} == {1}
        assert {e.attempt_id for e in started} <= {e.attempt_id for e in finished}
    finally:
        await session.aclose()
        await asyncio.gather(turn, return_exceptions=True)


@pytest.mark.asyncio
async def test_long_task_progress_does_not_abort_or_restart_work() -> None:
    agent, tts, output = LongTaskAgent(), StrictTTS(), StrictAudioOutput()
    session = make_session(
        agent=agent, tts=tts, output=output,
        policy=VoicePolicy(instant_ack=False, keepalive_interval_s=0.01),
        timings=VoicePolicyTimings(long_task_check_in_seconds=0.01,
                                  long_task_auto_continue_seconds=0.01),
    )
    observed: list[SessionEvent] = []
    progress_seen = asyncio.Event()
    def observe(event):
        observed.append(event)
        if event.type is SessionEventType.PROGRESS and event.payload.get("pool") == "reassure":
            progress_seen.set()
    session.subscribe(observe)
    turn = asyncio.create_task(session.run_turn(text="Run the test suite", output=output))
    try:
        await asyncio.wait_for(progress_seen.wait(), 1)
        await asyncio.sleep(0.04)
        assert not turn.done() and agent.aborted == 0 and len(agent.prompts) == 1
        assert not any("want me to continue" in line for line in tts.texts)
        agent.first_released.set()
        assert (await asyncio.wait_for(turn, 1)).status == "completed"
    finally:
        await session.aclose()
        await asyncio.gather(turn, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["cancel", "close"])
async def test_long_task_still_obeys_explicit_cancellation_and_shutdown(action) -> None:
    agent, tts, output = LongTaskAgent(), StrictTTS(), StrictAudioOutput()
    session = make_long_task_session(agent=agent, tts=tts, output=output, auto_continue_seconds=0.01)
    turn = asyncio.create_task(session.run_turn(text="Run until done", output=output))
    try:
        await asyncio.wait_for(agent.first_started.wait(), 1)
        await asyncio.sleep(0.04)
        assert agent.aborted == 0 and not turn.done()
        if action == "cancel":
            await session.cancel(turn_id=1)
        else:
            await session.aclose()
        outcome = await asyncio.wait_for(turn, 1)
        assert outcome.status == "cancelled"
        await asyncio.sleep(0.04)
        assert agent.aborted == 1 and len(agent.prompts) == 1
        assert output.writes == [(b"PCM<Running the full test suite.>", 1)]
    finally:
        await session.aclose()
        await asyncio.gather(turn, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancel_and_aclose_are_once_only_and_drop_stale_policy_audio() -> None:
    agent = ScriptedAgent(sentence="BLOCKING_ANSWER_SENTINEL")
    tts = BlockingTTS()
    output = StrictAudioOutput()
    streaming = StrictStreamingSTT("unused", {"coverage": 1.0})
    rest = StrictRestSTT("unused")
    session = make_session(
        agent=agent,
        tts=tts,
        output=output,
        streaming_stt=streaming,
        rest_stt=rest,
        policy=make_policy(),
        timings=VoicePolicyTimings(
            ack_delay_seconds=0.03,
            action_delay_seconds=0.03,
            keepalive_interval_seconds=0.03,
            long_task_check_in_seconds=0.03,
            long_task_auto_continue_seconds=0.03,
        ),
    )
    observed: list[SessionEvent] = []
    session.subscribe(observed.append)
    turn = asyncio.create_task(session.run_turn(text="cancel me", output=output))
    await asyncio.wait_for(tts.first_chunk.wait(), timeout=1)

    await session.cancel()
    outcome = await asyncio.wait_for(turn, timeout=1)
    event_count = len(observed)
    write_count = len(output.writes)
    tts.release.set()
    await asyncio.sleep(0.06)
    assert len(observed) == event_count
    assert len(output.writes) == write_count
    await session.aclose()
    await session.aclose()

    assert outcome.status == "cancelled"
    assert outcome.generated == ["BLOCKING_ANSWER_SENTINEL"]
    assert outcome.played == []
    assert output.writes == [(b"FIRST_PCM", 1)]
    assert not any(pcm == b"STALE_PCM" for pcm, _turn_id in output.writes)
    # Text generation has already settled while TTS is blocked.
    assert agent.aborted == 0
    assert tts.aborted == 1
    assert agent.closed == tts.closed == streaming.closed == rest.closed == 1
    assert any(event.type is SessionEventType.INTERRUPTION for event in observed)
    assert any(event.type is SessionEventType.RESPONSE_CANCELLED for event in observed)
    assert output.accounting().bytes_written == len(b"FIRST_PCM")


def test_package_is_surface_free_and_runner_is_only_a_compatibility_alias() -> None:
    import sleeptalk.routes.cascade as cascade_module
    import sleeptalk.session as session_module
    import sleeptalk.voice_policy_runtime as runtime_module

    for module in (session_module, cascade_module, runtime_module):
        tree = ast.parse(inspect.getsource(module))
        imports = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imports.update(
            node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
        )
        assert not any(name == "app" or name.startswith("app.") for name in imports)


class ClipAudioOutput(StrictAudioOutput):
    """Records narration clips by name, as a surface that queues them would."""

    def __init__(self) -> None:
        super().__init__()
        self.clips: list[str] = []

    async def play_file(self, path) -> None:
        self.clips.append(Path(path).name)


class ThinkingAgent(ScriptedAgent):
    """Thinks without a tool call, then answers."""

    def __init__(self, delay: float) -> None:
        super().__init__()
        self.think = delay

    async def respond_streaming(self, text: str, *, on_event=None):
        self.prompts.append(text)
        await asyncio.sleep(self.think)
        yield self.sentence


def _keyboard_session(agent: Any, output: StrictAudioOutput, style: str = "keyboard") -> RealtimeSession:
    return make_session(
        agent=agent,
        tts=StrictTTS(),
        output=output,
        policy=VoicePolicy(instant_ack=False, narration_style=style),
        timings=VoicePolicyTimings(
            keepalive_interval_seconds=0,
            long_task_check_in_seconds=0,
            long_task_auto_continue_seconds=0,
            working_sound_delay_seconds=0.05,
        ),
    )


async def test_keyboard_style_types_while_the_agent_thinks_without_tools() -> None:
    output = ClipAudioOutput()
    session = _keyboard_session(ThinkingAgent(1.0), output)
    await session.run_turn(text="what is two plus two", output=output)
    assert len(output.clips) >= 2, output.clips
    assert all(name.startswith("keyboard-") for name in output.clips)
    assert any(event.type is SessionEventType.PROGRESS and event.payload.get("pool") == "working_sound"
               for event in session.events)
    await session.aclose()


async def test_the_typing_stops_when_the_answer_starts() -> None:
    """Paced by piece length: about one piece per 0.4 s of waiting, none after."""
    output = ClipAudioOutput()
    session = _keyboard_session(ThinkingAgent(0.9), output)
    await session.run_turn(text="what is two plus two", output=output)
    assert 2 <= len(output.clips) <= 3, output.clips
    await session.aclose()


async def test_no_typing_for_a_quick_answer_or_other_styles() -> None:
    output = ClipAudioOutput()
    session = _keyboard_session(ThinkingAgent(0.0), output)
    await session.run_turn(text="hi", output=output)
    assert output.clips == []
    await session.aclose()
    output = ClipAudioOutput()
    session = _keyboard_session(ThinkingAgent(0.5), output, style="narrated")
    await session.run_turn(text="hi", output=output)
    assert output.clips == []
    await session.aclose()


class _WholeUtteranceStreamingSTT(StrictStreamingSTT):
    """A stream whose final text drops words (Cartesia ink-whisper)."""

    final_from_rest = True


class _FailingRestSTT(StrictRestSTT):
    async def transcribe_pcm(self, pcm: bytes) -> str:
        self.calls.append(pcm)
        raise RuntimeError("429 rate limited")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("rest", "heard", "source"),
    [
        # Live: "Espera, para. ¿Cuál es la capital de" from the stream; REST had "Francia".
        (StrictRestSTT("Espera, para, ¿cuál es la capital de Francia?"),
         "Espera, para, ¿cuál es la capital de Francia?", "rest_whole_utterance"),
        # REST failing never costs the words the stream did hear.
        (_FailingRestSTT(""), "Espera, para. ¿Cuál es la capital de", "streaming_rest_failed"),
        (StrictRestSTT(""), "Espera, para. ¿Cuál es la capital de", "streaming_rest_empty"),
    ],
)
async def test_a_stream_that_drops_words_is_heard_whole_by_rest(rest, heard, source) -> None:
    streaming = _WholeUtteranceStreamingSTT("Espera, para. ¿Cuál es la capital de", {"coverage": 1.0})
    agent = ScriptedAgent()
    session = make_session(
        agent=agent, tts=StrictTTS(), output=StrictAudioOutput(),
        streaming_stt=streaming, rest_stt=rest,
        policy=VoicePolicy(instant_ack=False, keepalive_interval_s=0),
    )
    pcm = b"\x05" * 192_000
    await session.append_audio(b"\x05\x00" * 24_000, participant_id=14)
    await session.run_turn(pcm=pcm, extras={"participant_id": 14})

    assert rest.calls == [pcm]
    assert agent.prompts == [heard]
    assert transcript_event(session.events, 1).payload["sttSource"] == source
    await session.aclose()


def test_cartesia_asks_rest_for_the_whole_utterance_only_on_ink_whisper() -> None:
    from sleeptalk.providers import ProviderSpec
    from sleeptalk.speech.cartesia_stt import CartesiaStreamingSTT

    assert CartesiaStreamingSTT(ProviderSpec("cartesia", "ink-whisper", language="es-ES"), api_key="k").final_from_rest
    assert not CartesiaStreamingSTT(ProviderSpec("cartesia", "ink-2", language="en-US"), api_key="k").final_from_rest


@pytest.mark.asyncio
async def test_a_pause_asks_a_streaming_listener_to_finalize() -> None:
    """Deepgram and Cartesia hold the last words at a pause (Deepgram: "What's
    two plus two, just the" for 1.2 s); endpointing then waited 1.8-3.5 s.
    The pause now asks them to finalize, and returns no early-text task."""
    flushed: list[int] = []

    class Flushing(StrictStreamingSTT):
        async def flush_at_pause(self, participant_id: int) -> None:
            flushed.append(participant_id)

    session = make_session(
        agent=ScriptedAgent(), tts=StrictTTS(), output=StrictAudioOutput(),
        streaming_stt=Flushing("unused", {}), rest_stt=StrictRestSTT("unused"),
        policy=VoicePolicy(instant_ack=False, keepalive_interval_s=0),
    )
    assert session.transcribe_ahead(b"\x01\x00" * 480, participant_id=15) is None
    assert flushed == []  # nothing streamed for this caller yet
    await session.append_audio(b"\x01\x00" * 480, participant_id=15)
    assert session.transcribe_ahead(b"\x01\x00" * 480, participant_id=15) is None
    await asyncio.sleep(0)
    assert flushed == [15]
    await session.aclose()


class _LetsGoOnAbort(ScriptedAgent):
    """A stream that, once cancelled, lets go only after abort() (as Claude
    Code's SDK query does), or after 5 s."""

    def __init__(self) -> None:
        super().__init__()
        self.released = asyncio.Event()
        self.started = asyncio.Event()

    async def respond_streaming(self, text: str, *, on_event=None):
        try:
            yield "Searching now."
            self.started.set()
            await asyncio.Event().wait()
        finally:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.released.wait(), 5)

    async def abort(self) -> None:
        self.aborted += 1
        self.released.set()


@pytest.mark.asyncio
async def test_stop_does_not_wait_for_the_agent_to_let_go_before_aborting_it() -> None:
    """Live: "Stop. Cancel that." to Claude Code mid-search went 5 s silent:
    the cancel waited on the stream's cleanup before calling abort()."""
    agent = _LetsGoOnAbort()
    session = make_session(agent=agent, tts=StrictTTS(), output=StrictAudioOutput(),
                           policy=VoicePolicy(instant_ack=False, keepalive_interval_s=0))
    turn = asyncio.create_task(session.run_turn(text="research the TODOs"))
    await asyncio.wait_for(agent.started.wait(), 2)
    started = asyncio.get_running_loop().time()
    await session.interrupt_presentation()
    assert asyncio.get_running_loop().time() - started < 1.0
    assert agent.aborted == 1
    await asyncio.gather(turn, return_exceptions=True)
    await session.aclose()


class _AsksFirst(ScriptedAgent):
    """An agent set to ask before writing (Claude Code / pi with
    permission_mode = "pending"): it waits for permission.resolve."""

    def __init__(self) -> None:
        super().__init__()
        self.decision: Any = None
        self.answered = asyncio.Event()

    async def respond_streaming(self, text: str, *, on_event=None):
        from sleeptalk.adapter import PermissionDecision, PermissionRequested

        on_event(PermissionRequested(
            request_id="perm-1", prompt="Allow Write to approved.txt?",
            options=frozenset({PermissionDecision.DENY, PermissionDecision.ALLOW_ONCE}),
        ))
        await asyncio.wait_for(self.answered.wait(), 5)
        yield "Created approved.txt." if self.decision.value != "deny" else "Okay, I won't create it."

    async def control(self, command):
        from sleeptalk.adapter import ControlAccepted

        self.decision = command.decision
        self.answered.set()
        return ControlAccepted(request_id=command.request_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(("said", "decision", "reply"), [
    ("Yes, go ahead.", "allow_once", "Created approved.txt."),
    ("No, don't do that.", "deny", "Okay, I won't create it."),
])
async def test_an_agent_that_asks_first_is_asked_out_loud_and_answered(said, decision, reply) -> None:
    """Live: Claude Code and pi set to ask hung on their first write; the
    cascade never voiced the request, and "yes" had nowhere to go."""
    agent = _AsksFirst()
    tts = StrictTTS()
    session = make_session(agent=agent, tts=tts, output=StrictAudioOutput(),
                           policy=VoicePolicy(instant_ack=False, keepalive_interval_s=0))
    turn = asyncio.create_task(session.run_turn(text="create approved.txt"))
    for _ in range(100):
        if any("Say yes or no" in text for text in tts.texts):
            break
        await asyncio.sleep(0.01)
    assert "Allow Write to approved.txt? Say yes or no." in tts.texts
    assert await session.answer_pending("What's the weather?") is False  # not an answer
    assert await session.answer_pending(said) is True
    await asyncio.wait_for(turn, 5)
    assert agent.decision.value == decision
    assert reply in tts.texts
    assert await session.answer_pending("yes") is False  # nothing waits now
    await session.aclose()


def test_a_command_line_is_never_read_out_as_the_question() -> None:
    """A Cursor call read "`cd /Users/… && ls data tools && grep -n …`? Say
    yes or no." aloud, every character. The page still shows the request."""
    from sleeptalk.voice_policy_runtime import permission_question

    command = '`cd /Users/developer/projects/example-api && ls data tools && grep -n "@app" server.py`'
    assert permission_question(command) == "The agent wants to run a command. Say yes or no."
    assert permission_question(command, spoken=False).startswith("cd /Users/developer/projects")
    assert permission_question("Allow Write to approved.txt") == "Allow Write to approved.txt? Say yes or no."
    assert permission_question("Run npm install?") == "Run npm install? Say yes or no."


def test_spoken_permission_answers() -> None:
    from sleeptalk.voice_policy_runtime import permission_answer

    assert permission_answer("Yeah, do it.").value == "allow_once"
    assert permission_answer("okay") .value == "allow_once"
    assert permission_answer("Don't.").value == "deny"
    assert permission_answer("No thanks").value == "deny"
    assert permission_answer("Tell me more about it") is None
    assert permission_answer("Nothing yet") is None


@pytest.mark.asyncio
async def test_while_the_agent_waits_on_the_caller_it_asks_again_instead_of_still_working() -> None:
    """Live: "Still writing it out" every few seconds talked over the caller's
    "Yes, go ahead" (lost in the barge grace), and was untrue: the agent waited."""
    agent = _AsksFirst()
    tts = StrictTTS()
    session = make_session(agent=agent, tts=tts, output=StrictAudioOutput(),
                           policy=VoicePolicy(instant_ack=False, keepalive_interval_s=0),
                           timings=VoicePolicyTimings(keepalive_interval_seconds=0.05))
    turn = asyncio.create_task(session.run_turn(text="create approved.txt"))
    await asyncio.sleep(0.5)
    asked = [text for text in tts.texts if "Say yes or no" in text]
    assert len(asked) == 2, tts.texts  # asked, then once more; no "still working"
    assert asked[1].startswith("Still waiting on you.")
    assert all("Say yes or no" in text for text in tts.texts), tts.texts
    assert await session.answer_pending("yes") is True
    await asyncio.wait_for(turn, 5)
    await session.aclose()


class _ReviewsInBackground(ScriptedAgent):
    """Works until released, after starting a tool."""

    def __init__(self) -> None:
        super().__init__()
        self.release = asyncio.Event()
        self.working = asyncio.Event()

    async def respond_streaming(self, text: str, *, on_event=None):
        from sleeptalk.adapter import ToolEvent, ToolPhase

        on_event(ToolEvent(phase=ToolPhase.STARTED, name="read", detail="reading beam.py"))
        self.working.set()
        await asyncio.wait_for(self.release.wait(), 5)
        yield "All three files look fine."


@pytest.mark.asyncio
async def test_what_are_you_doing_is_answered_without_stopping_the_work() -> None:
    """Live: "How are the sub agents doing?" interrupted Claude Code mid-launch
    and its reviewers were "rejected before launching"."""
    agent = _ReviewsInBackground()
    tts = StrictTTS()
    session = make_session(agent=agent, tts=tts, output=StrictAudioOutput(),
                           policy=VoicePolicy(instant_ack=False, keepalive_interval_s=0))
    turn = asyncio.create_task(session.run_turn(text="review the files"))
    await asyncio.wait_for(agent.working.wait(), 2)
    assert await session.answer_status("Tell me about the README") is False
    assert await session.answer_status("What are you doing right now?") is True
    assert any(text.startswith("I'm still reading") for text in tts.texts), tts.texts
    assert agent.aborted == 0
    agent.release.set()
    await asyncio.wait_for(turn, 5)
    assert "All three files look fine." in tts.texts
    assert await session.answer_status("What are you doing?") is False  # nothing is working now
    await session.aclose()


@pytest.mark.parametrize("text", [
    "How are the sub agents doing?", "How are the sub are the sub-agents doing?",
    "What are you doing right now?", "Any update?", "Are you still there?", "What's going on?",
])
def test_status_questions_asked_mid_work(text) -> None:
    from sleeptalk.voice.subagent import is_mid_turn_status_question

    assert is_mid_turn_status_question(text)


@pytest.mark.parametrize("text", ["Stop. Cancel that.", "What is the capital of France?", "Tell me about the README"])
def test_requests_are_not_status_questions(text) -> None:
    from sleeptalk.voice.subagent import is_mid_turn_status_question

    assert not is_mid_turn_status_question(text)
