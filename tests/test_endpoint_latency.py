from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sleeptalk.voice_loop.utterance import UtteranceAssembler
from tests.test_utterance_gate_hold import Clock, FRAME
from sleeptalk.discord.access import DiscordAccess


def assembler_for(clock, events, delivered, **kwargs):
    async def on_utterance(participant, pcm):
        delivered.append(pcm)
    return UtteranceAssembler(
        loop=clock, min_bytes=1, silence_seconds=0.4,
        should_ignore_audio=kwargs.pop("should_ignore_audio", lambda: False),
        on_utterance=on_utterance,
        on_capture_event=lambda pid, event: events.append(event),
        **kwargs,
    )


@pytest.mark.asyncio
async def test_commit_reports_measured_silence_before_dispatch():
    clock = Clock(asyncio.get_running_loop())
    clock.now = 100
    events, delivered = [], []
    assembler = assembler_for(clock, events, delivered, speech_rms_threshold=100)
    assembler.observe(1, object(), FRAME)
    clock.now = 100.25
    assembler.observe(1, object(), bytes(len(FRAME)))
    clock.now = 100.75
    assembler.flush(1)
    assert events[-1]["kind"] == "utterance_committed"
    assert events[-1]["endpointSilenceMs"] == 750
    assert delivered == []
    await asyncio.sleep(0)
    assert len(delivered) == 1
    assert assembler.last_voiced_at == {}
    assembler.observe(1, object(), FRAME)
    clock.now = 100.95
    assembler.flush(1)
    assert events[-1]["endpointSilenceMs"] == 200
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_gate_reset_does_not_replace_the_last_voiced_frame_clock():
    clock = Clock(asyncio.get_running_loop())
    clock.now = 100
    events, delivered = [], []
    gate = {"shut": False}
    assembler = assembler_for(clock, events, delivered, should_ignore_audio=lambda: gate["shut"])
    assembler.observe(1, object(), FRAME)
    gate["shut"] = True
    assembler._maybe_flush(1)
    clock.now = 101
    gate["shut"] = False
    assembler._maybe_flush(1)
    assert assembler.last_audio_at[1] == 101
    clock.now = 102
    assembler.flush(1)
    assert events[-1]["endpointSilenceMs"] == 2000
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_dropped_participant_cannot_supply_a_later_timing():
    clock = Clock(asyncio.get_running_loop())
    events, delivered = [], []
    assembler = assembler_for(clock, events, delivered)
    assembler.observe(1, object(), FRAME)
    assembler.drop(1)
    assert assembler.last_voiced_at == {}
    assembler.participants[1] = object()
    assembler.buffers[1] = bytearray(FRAME)
    assembler.flush(1)
    assert "endpointSilenceMs" not in events[-1]
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_vad_inference_delay_is_included_in_endpoint_measurement():
    clock = Clock(asyncio.get_running_loop())
    clock.now = 100
    class Gate:
        async def observe(self, pcm):
            clock.now = 100.15
            return SimpleNamespace(is_speech=True, started=True, stopped=False)
    events, delivered = [], []
    assembler = assembler_for(clock, events, delivered, vad_mode="silero", silero_vad_factory=lambda _: Gate())
    assembler.observe(1, object(), FRAME)
    await asyncio.sleep(0)
    clock.now = 100.4
    assembler.flush(1)
    assert events[-1]["endpointSilenceMs"] == 400
    await asyncio.sleep(0)


class Trace:
    created = []
    def __init__(self, **kwargs):
        self.marks = []
        self.created.append(self)
    def mark(self, stage, **fields):
        self.marks.append((stage, fields))
    def finish(self, *args, **kwargs):
        pass


@pytest.mark.asyncio
async def test_web_receipts_are_consumed_even_when_audio_is_rejected(monkeypatch):
    from sleeptalk.web_voice.session import WebCallSession
    monkeypatch.setattr("sleeptalk.web_voice.session.log_latency_event", lambda **_: None)
    monkeypatch.setattr("sleeptalk.web_voice.session.VoiceLatencyTrace", Trace)
    Trace.created = []
    session = WebCallSession.__new__(WebCallSession)
    session._closed = False
    session._pending_endpoint_timings = {}
    session._speech_started_at = None
    session._clear_hearing = lambda: None
    session.surface = "web"
    session.persona = "test"
    session.transport_state = SimpleNamespace(transport="web-webrtc", fields=lambda: {})
    session.on_transcript = None
    session.session = SimpleNamespace(config=None, run_turn=AsyncMock(return_value=SimpleNamespace(turn_id=1, status="completed")))
    session._capture_event("capture_vad_dropout", 0, {"kind": "utterance_committed", "endpointSilenceMs": 999})
    await session._handle_utterance({}, bytes(len(FRAME)))
    assert session._pending_endpoint_timings == {}
    session._capture_event("capture_vad_dropout", 0, {"kind": "utterance_committed", "endpointSilenceMs": 275})
    await session._handle_utterance({}, FRAME)
    await session._handle_utterance({}, FRAME)
    assert Trace.created[0].marks[0][1]["endpointSilenceMs"] == 275
    assert "endpointSilenceMs" not in Trace.created[1].marks[0][1]
    session._closed = True
    session._capture_event("capture_vad_dropout", 0, {"kind": "utterance_committed", "endpointSilenceMs": 88})
    assert session._pending_endpoint_timings == {}


@pytest.mark.asyncio
async def test_discord_concurrent_utterances_keep_their_own_timing(monkeypatch):
    from sleeptalk.discord_voice.live_session import PiDiscordLiveSession
    monkeypatch.setattr("sleeptalk.discord_voice.live_session.log_latency_event", lambda **_: None)
    monkeypatch.setattr("sleeptalk.discord_voice.live_session.VoiceLatencyTrace", Trace)
    Trace.created = []
    session = PiDiscordLiveSession.__new__(PiDiscordLiveSession)
    session._pending_endpoint_timings = {}
    session._task_endpoint_timings = {}
    session._speech_latency_traces = {}
    session._speak_tasks = []
    session._lifecycle = SimpleNamespace(is_shutting_down=False)
    session.bot = SimpleNamespace(access=DiscordAccess(required=False), surface="discord", persona="test", policy=SimpleNamespace(capture=SimpleNamespace(silence_ms=120)))
    session.voice = SimpleNamespace(channel=None)
    session._trace_metadata = lambda user: {}
    user = SimpleNamespace(id=1)
    first_started, release_first = asyncio.Event(), asyncio.Event()
    observed = {}
    async def handle(user, pcm):
        if pcm == b"first":
            first_started.set()
            await release_first.wait()
        if pcm == b"drop":
            return
        trace = session._new_trace(user, pcm)
        observed[pcm] = dict(trace.marks[-1][1])
    session._handle_utterance = handle
    for silence in (650, 300):
        session._on_capture_event(1, {"kind": "utterance_committed", "endpointSilenceMs": silence})
    first = asyncio.create_task(session.handle_utterance(user, b"first"))
    await first_started.wait()
    await session.handle_utterance(user, b"second")
    release_first.set()
    await first
    assert observed[b"first"]["endpointSilenceMs"] == 650
    assert observed[b"second"]["endpointSilenceMs"] == 300
    assert observed[b"first"]["configuredSilenceMs"] == 120
    assert "silenceMs" not in observed[b"first"]
    session._on_capture_event(1, {"kind": "utterance_committed", "endpointSilenceMs": 800})
    await session.handle_utterance(user, b"drop")
    await session.handle_utterance(user, b"fresh")
    assert "endpointSilenceMs" not in observed[b"fresh"]
    assert session._pending_endpoint_timings == session._task_endpoint_timings == {}


@pytest.mark.asyncio
async def test_terminal_receipt_is_consumed_before_drops_and_stamped_on_next_turn(monkeypatch):
    from sleeptalk.config import SleeptalkConfig, TransportConfig
    from sleeptalk.transports.terminal import TerminalTransport
    terminal = TerminalTransport(SleeptalkConfig(transport=TransportConfig(type="terminal")), {})
    session = SimpleNamespace(
        policy=SimpleNamespace(capture=SimpleNamespace(smart_turn=False, silence_ms=120)),
        run_turn=AsyncMock(return_value=SimpleNamespace(status="completed")),
    )
    assembler = terminal._build_assembler(session, SimpleNamespace())
    assembler.on_capture_event(0, {"kind": "utterance_committed", "endpointSilenceMs": 700})
    await terminal._on_assembled_utterance(object(), FRAME)
    assert not terminal._pending_endpoint_timings
    terminal._session = session
    monkeypatch.setattr(terminal, "_playback_pending", lambda: False)
    marks = []
    monkeypatch.setattr(terminal, "_start_latency_turn", lambda **fields: marks.append(fields))
    assembler.on_capture_event(0, {"kind": "utterance_committed", "endpointSilenceMs": 340})
    await terminal._on_assembled_utterance(object(), FRAME)
    await terminal._turn_task
    assert marks[-1]["endpointSilenceMs"] == 340
    await terminal._on_assembled_utterance(object(), FRAME)
    await terminal._turn_task
    assert "endpointSilenceMs" not in marks[-1]
    assembler.on_capture_event(0, {"kind": "utterance_committed", "endpointSilenceMs": 111})
    await terminal.close()
    assert not terminal._pending_endpoint_timings
    assembler.on_capture_event(0, {"kind": "utterance_committed", "endpointSilenceMs": 999})
    assert not terminal._pending_endpoint_timings


def test_sentences_in_other_scripts_are_judged_by_their_end_mark() -> None:
    """Chinese had no Latin words to weigh, scored 0.0, and every turn waited
    the 3.5 s incomplete budget; its full-width marks now end it like "?"."""
    from sleeptalk.voice_loop.endpointing import estimate_completion_score as score

    assert score("二加二等于几？只说数字就好。") >= 0.9
    assert score("नमस्ते, आप कैसे हैं।") >= 0.9
    assert 0.55 <= score("我想了解一下") < 0.72  # no end mark: uncertain, not complete
    assert score("I'd like to know about") < 0.55  # trails off on a preposition
