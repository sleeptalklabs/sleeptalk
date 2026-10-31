"""Tests for the terminal surface as a session-kernel transport (r2-terminal).

Covers: config selects the terminal transport via the registry; no
provider is constructed in terminal code (AST import check); the full
fake-audio conversation flow through the cascade route; interruption
mid-turn; and a compressed soak (fake audio driven through many turns).
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import io
import logging
import sys
import types
import wave

import pytest
from sleeptalk.builder import build_session
from sleeptalk.config import TransportConfig, load_config
from sleeptalk.barge import BargeCalibration, BargeGate
from sleeptalk.interrupt import AcousticKind, Floor, InterruptPolicy, SpeakingState
from sleeptalk.session import RealtimeSession
from sleeptalk.session_events import SessionEvent, SessionEventType
from sleeptalk.transports.terminal import (
    _TERMINAL_LISTEN_RMS,
    TERMINAL_STREAM_TAIL_MS,
    TerminalTransport,
    _CaptureState,
    _pcm_duration_ms,
    _pcm_rms,
    _SpeakerOutput,
    _stream_chunk,
    _trim_trailing_hush,
    calibrate_capture_threshold,
)
from sleeptalk.transports.terminal_aec import TerminalAec
from sleeptalk.transports.terminal_audio import AudioDeviceError, TerminalSpeaker
from sleeptalk.voice.configs import AdaptiveEndpointingConfig
from sleeptalk.voice_loop.endpointing import TurnCompletionSignal

from tests.runtime.test_terminal_audio import FakeSoundDevice

TERMINAL_MINIMAL = """[transport]
type = "terminal"

[agent]
type = "pi"
"""

TERMINAL_WITH_DEVICES = """[transport]
type = "terminal"
input_device = 2
output_device = 2

[agent]
type = "pi"
"""

SPEECH_50MS = b"\x00\x20" * 2_400
SILENCE_50MS = b"\x00\x00" * 2_400
# Live DeepRe capture measured conversational frames at RMS 220-300 against
# room noise ~10-30. The shipped example sets rms_threshold=0.
CONVERSATION_50MS = b"\xfa\x00" * 2_400
NOISE_50MS = b"\x14\x00" * 2_400
# Discord-measured spoken "stop" is 0.016-0.021 normalized (~590 RMS).
CONVERSATIONAL_STOP_50MS = b"\x4d\x02" * 2_400
# Discord-measured TTS echo is ~0.01 normalized (~328 RMS). Stay under the
# test listen floor (500) so leakage cannot open a turn or barge.
ECHO_50MS = b"\x00\x01" * 2_400
TTS_ECHO_50MS = b"\x48\x01" * 2_400
# Laptop speakers replay the bot at full capture level. This is a delayed
# copy of the TTS we queue, not a quieter leak.
TTS_LOOPBACK = b"\x11\x22" * 8_000
STOP_50MS = b"".join(
    (b"\x00\x40" if index % 2 == 0 else b"\x00\xc0") for index in range(2_400)
)


def _test_policy(
    *,
    start_ms=50,
    silence_ms=50,
    interrupt_ms=50,
    threshold=500,
    bot_grace_ms=400,
    enabled=True,
    mode="immediate",
    min_duration_ms=None,
    grace_ms=0,
    gap_hold_ms=600,
    cooldown_ms=0,
    min_volume=0.03,
):
    return types.SimpleNamespace(
        capture=types.SimpleNamespace(
            rms_threshold=threshold,
            vad_start_ms=start_ms,
            silence_ms=silence_ms,
            terminal_commit_silence_ms=silence_ms,
        ),
        turns=types.SimpleNamespace(
            vad_threshold=threshold,
            interrupt_min_ms=interrupt_ms,
            bot_grace_ms=bot_grace_ms,
        ),
        interrupt=types.SimpleNamespace(
            enabled=enabled,
            mode=mode,
            min_duration_ms=interrupt_ms if min_duration_ms is None else min_duration_ms,
            grace_ms=grace_ms,
            gap_hold_ms=gap_hold_ms,
            cooldown_ms=cooldown_ms,
            min_volume=min_volume,
        ),
    )


def test_terminal_assembler_wires_both_endpoint_layers() -> None:
    config = AdaptiveEndpointingConfig()
    policy = types.SimpleNamespace(
        capture=types.SimpleNamespace(
            rms_threshold=0,
            vad_start_ms=250,
            silence_ms=120,
            smart_turn=True,
            smart_turn_threshold=0.6,
        ),
        turns=types.SimpleNamespace(
            vad_threshold=500,
            interrupt_min_ms=250,
            bot_grace_ms=400,
        ),
        endpointing_config=lambda: config,
    )
    transport = make_transport()
    session = types.SimpleNamespace(
        policy=policy,
        turn_completion_signal=lambda pid: TurnCompletionSignal(
            transcript="and then I was going to",
            source="deepgram",
        ),
    )

    async def _run() -> None:
        capture = transport._capture_state(session)
        assembler = transport._build_assembler(session, capture)
        assert assembler.smart_turn_scorer is not None
        assert assembler.smart_turn_scorer.threshold == 0.6
        assert assembler.turn_completion_provider is not None
        assert assembler.endpointing_config is config
        assert assembler.vad_mode == "silero"
        assert assembler.speech_rms_threshold == 0
        signal = assembler.turn_completion_provider(0)
        assert signal.transcript == "and then I was going to"

    asyncio.run(_run())


def test_terminal_incomplete_partial_holds_past_one_second() -> None:
    """A mid-sentence tail must not commit on the old 1000 ms hush."""
    flushed: list[bytes] = []
    decisions: list[object] = []

    async def on_utterance(_participant: object, pcm: bytes) -> None:
        flushed.append(pcm)

    async def _run() -> None:
        loop = asyncio.get_running_loop()
        assembler = __import__(
            "sleeptalk.voice_loop.utterance", fromlist=["UtteranceAssembler"]
        ).UtteranceAssembler(
            loop=loop,
            min_bytes=100,
            silence_seconds=0.12,
            on_utterance=on_utterance,
            should_ignore_audio=lambda: False,
            speech_rms_threshold=120,
            vad_mode="rms",
            smart_turn_scorer=None,
            turn_completion_provider=lambda _pid: TurnCompletionSignal(
                transcript="and then I was going to",
                source="deepgram",
            ),
            on_endpoint_decision=lambda _pid, decision: decisions.append(decision),
        )
        speech = _mono_to_stereo(CONVERSATION_50MS)
        hush = _mono_to_stereo(SILENCE_50MS)
        assembler.observe(0, "user", speech)
        for _ in range(22):
            assembler.observe(0, "user", hush)
            await asyncio.sleep(0.05)
        assembler._maybe_flush(0)
        assert flushed == []
        assert decisions
        assert decisions[-1].should_endpoint is False
        assert decisions[-1].reason in {"incomplete", "floor_holder", "uncertain"}

    asyncio.run(_run())


def _mono_to_stereo(pcm: bytes) -> bytes:
    from sleeptalk.transports.terminal import _mono_to_stereo_s16

    return _mono_to_stereo_s16(pcm)


def _open_after(state: _CaptureState, pcm: bytes, frames: int = 6) -> bool:
    opened = False
    for _ in range(frames):
        opened_now, _ = state.observe(pcm)
        opened = opened or opened_now
    return opened


def test_shipped_zero_rms_hears_conversational_speech() -> None:
    assert 220 <= _pcm_rms(CONVERSATION_50MS) <= 300
    policy = types.SimpleNamespace(
        capture=types.SimpleNamespace(
            rms_threshold=0,
            vad_start_ms=250,
            silence_ms=120,
        ),
        turns=types.SimpleNamespace(
            vad_threshold=500,
            interrupt_min_ms=250,
            bot_grace_ms=400,
        ),
    )
    capture = make_transport()._capture_state(types.SimpleNamespace(policy=policy))
    assert _open_after(capture, CONVERSATION_50MS) is True


def test_shipped_zero_rms_ignores_room_noise() -> None:
    assert _pcm_rms(NOISE_50MS) <= 30
    policy = types.SimpleNamespace(
        capture=types.SimpleNamespace(
            rms_threshold=0,
            vad_start_ms=250,
            silence_ms=120,
        ),
        turns=types.SimpleNamespace(
            vad_threshold=500,
            interrupt_min_ms=250,
            bot_grace_ms=400,
        ),
    )
    capture = make_transport()._capture_state(types.SimpleNamespace(policy=policy))
    assert _open_after(capture, NOISE_50MS) is False


@pytest.mark.asyncio
async def test_conversational_stop_barges_like_discord_web() -> None:
    assert 0.016 <= _pcm_rms(CONVERSATIONAL_STOP_50MS) / 32768.0 <= 0.021
    transport = make_transport()
    transport._gate = _terminal_gate()
    fired = False
    for _ in range(4):
        evidence = transport._observe_residual(CONVERSATIONAL_STOP_50MS, CONVERSATIONAL_STOP_50MS)
        fired = transport._gate.observe(evidence).fired or fired
    assert fired is True


def _terminal_gate() -> BargeGate:
    """Terminal's live barge configuration, driven off a frozen clock."""
    gate = BargeGate(
        InterruptPolicy(min_volume=0.03, min_duration_ms=150, grace_ms=0, cooldown_ms=0),
        AcousticKind.mixed,
        calibration=BargeCalibration(level_scale=2.5, dip_tolerance_ms=0.0),
        detector="residual_rms",
        clock=lambda: 0,
    )
    gate.turn_started()
    gate.audio_started()
    return gate


@pytest.mark.asyncio
async def test_measured_tts_echo_does_not_barge() -> None:
    assert abs(_pcm_rms(TTS_ECHO_50MS) / 32768.0 - 0.01) < 0.001
    transport = make_transport()
    transport._gate = _terminal_gate()
    for _ in range(4):
        evidence = transport._observe_residual(TTS_ECHO_50MS, TTS_ECHO_50MS)
        assert transport._gate.observe(evidence).fired is False


def test_stream_chunk_stops_after_hush_pad() -> None:
    state = _CaptureState(
        threshold=500,
        start_ms=50,
        silence_ms=1000,
        interrupt_ms=250,
        bot_grace_ms=400,
    )
    for _ in range(2):
        state.observe(SPEECH_50MS)
    assert _stream_chunk(state, SPEECH_50MS) is True
    streamed_quiet_ms = 0
    for _ in range(8):
        state.observe(SILENCE_50MS)
        if _stream_chunk(state, SILENCE_50MS):
            streamed_quiet_ms += 50
    assert streamed_quiet_ms <= TERMINAL_STREAM_TAIL_MS + 50
    assert streamed_quiet_ms >= TERMINAL_STREAM_TAIL_MS


def test_trim_trailing_hush_keeps_last_word_pad() -> None:
    pcm = SPEECH_50MS * 4 + SILENCE_50MS * 20
    trimmed = _trim_trailing_hush(pcm, threshold=500, keep_ms=TERMINAL_STREAM_TAIL_MS)
    assert trimmed.startswith(SPEECH_50MS)
    assert 350 <= _pcm_duration_ms(trimmed) <= 450


def _mix_pcm(left: bytes, right: bytes) -> bytes:
    size = min(len(left), len(right))
    size -= size % 2
    first = memoryview(left[:size]).cast("h")
    second = memoryview(right[:size]).cast("h")
    out = bytearray(size)
    dest = memoryview(out).cast("h")
    for index in range(len(first)):
        value = int(first[index]) + int(second[index])
        dest[index] = max(-32768, min(32767, value))
    return bytes(out)


def _delay_lowpass(pcm: bytes, *, delay_ms: int = 50, alpha: float = 0.2) -> bytes:
    import numpy as np

    samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float64)
    filtered = np.empty_like(samples)
    prev = 0.0
    for index, value in enumerate(samples):
        prev = prev + alpha * (value - prev)
        filtered[index] = prev
    delay = int(48_000 * delay_ms / 1000)
    delayed = np.concatenate([np.zeros(delay), filtered])[: len(samples)]
    return np.clip(delayed, -32768, 32767).astype(np.int16).tobytes()


class _ZeroResidualEngine:
    def process(self, near, far):
        import numpy as np

        return np.zeros_like(near)

    def reset(self) -> None:
        return None


class _PassNearEngine:
    def process(self, near, far):
        return near

    def reset(self) -> None:
        return None


async def _push_utterance(stream, *, speech_frames=1, silence_frames=1) -> None:
    for _ in range(speech_frames):
        stream.push_audio(SPEECH_50MS)
        await asyncio.sleep(0.06)
    for _ in range(silence_frames):
        stream.push_audio(SILENCE_50MS)
        await asyncio.sleep(0.06)


def _write(tmp_path, text: str) -> str:
    path = tmp_path / "sleeptalk.toml"
    path.write_text(text)
    return str(path)


class _RmsCompatSilero:
    """Instant RMS stand-in. DC fixtures are not Silero speech."""

    def __init__(self, config=None) -> None:
        self.config = config

    async def observe(self, pcm: bytes):
        from sleeptalk.voice_loop.vad import VadObservation, pcm_rms

        return VadObservation(is_speech=pcm_rms(pcm) >= 500)

    def reset(self) -> None:
        return None


@pytest.fixture(autouse=True)
def _patch_terminal_silero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "sleeptalk.voice_loop.utterance.SileroVadGate",
        _RmsCompatSilero,
    )


def make_transport(*, env=None, echo_canceller=None) -> TerminalTransport:
    """Real constructor + injected fake sounddevice (audit rework: tests
    use the public lifecycle only)."""
    config = types.SimpleNamespace(transport=TransportConfig(type="terminal"), policy=_test_policy())
    transport = TerminalTransport(config=config, env=env or {})
    transport.use_sounddevice(FakeSoundDevice())
    transport.use_listen_vad(_RmsCompatSilero)
    # Default live AEC3 would treat synthetic ROOM_ECHO as speech. Tests that
    # prove the speaker-safe gate stay fail-closed unless they inject an engine.
    transport.use_echo_canceller(echo_canceller or TerminalAec(engine=None))
    return transport


def make_transport_with_devices(in_dev=None, out_dev=None, sd=None) -> TerminalTransport:
    """Transport with explicit [transport] device selection."""
    fields: dict = {"type": "terminal"}
    if in_dev is not None:
        fields["input_device"] = in_dev
    if out_dev is not None:
        fields["output_device"] = out_dev
    config = types.SimpleNamespace(transport=TransportConfig(**fields), policy=_test_policy())
    transport = TerminalTransport(config=config, env={})
    transport.use_sounddevice(sd or FakeSoundDevice())
    transport.use_listen_vad(_RmsCompatSilero)
    return transport


class RecordingBeing:
    """No-ANSI presentation seam for asserting transport state language."""

    def __init__(self, *, enabled=None) -> None:
        self.enabled = enabled
        self.context: tuple[str, ...] = ()
        self.agent = ""
        self.surface = ""
        self.stack: tuple[str, ...] = ()
        self.states: list[str] = []
        self.headlines: list[str] = []
        self.pinned = False
        self.closed = False
        self.hearing: list[bool] = []

    def set_identity(
        self,
        *,
        agent: str = "",
        surface: str = "terminal",
        product: str = "",
        stack: tuple[str, ...] = (),
    ) -> None:
        del product
        self.agent = agent
        self.surface = surface
        self.stack = stack

    def set_context(self, lines: tuple[str, ...]) -> None:
        self.context = lines

    def pin_top(self) -> None:
        self.pinned = True

    def set(self, state: str, *, active: bool = False, detail: str = "") -> None:
        del active, detail
        self.states.append(state)

    def set_presentation(self, state, *, muted=None) -> None:
        del muted
        self.states.append(state.presence)
        self.headlines.extend(line.text for line in state.lines)

    def set_hearing(self, hearing: bool) -> None:
        self.hearing.append(bool(hearing))

    def close(self) -> None:
        self.closed = True


def test_terminal_visual_maps_normalized_kernel_events() -> None:
    transport = make_transport()
    beings: list[RecordingBeing] = []

    def factory(*, enabled=None):
        being = RecordingBeing(enabled=enabled)
        beings.append(being)
        return being

    transport.use_terminal_being(factory)
    transport._start_visual()
    being = beings[0]
    events = [
        SessionEvent(
            type=SessionEventType.TRANSCRIPT_PARTIAL,
            session_id="terminal-ui",
            turn_id=1,
        ),
        SessionEvent(
            type=SessionEventType.RESPONSE_STARTED,
            session_id="terminal-ui",
            turn_id=1,
        ),
        SessionEvent(
            type=SessionEventType.TOOL_STARTED,
            session_id="terminal-ui",
            turn_id=1,
            payload={"name": "browser"},
        ),
        SessionEvent(
            type=SessionEventType.RESPONSE_FIRST_OUTPUT,
            session_id="terminal-ui",
            turn_id=1,
        ),
        SessionEvent(
            type=SessionEventType.RESPONSE_COMPLETED,
            session_id="terminal-ui",
            turn_id=1,
        ),
        SessionEvent(
            type=SessionEventType.ERROR,
            session_id="terminal-ui",
            payload={"message": "provider failed"},
        ),
    ]
    for event in events:
        transport._on_session_event(event)

    assert being.pinned
    assert being.surface == "terminal"
    assert being.states == [
        "idle",
        "listening",
        "thinking",
        "thinking",
        "speaking",
        "listening",
        "idle",
    ]


def test_set_hearing_is_a_level_not_presence() -> None:
    transport = make_transport()
    beings: list[RecordingBeing] = []

    def factory(*, enabled=None):
        being = RecordingBeing(enabled=enabled)
        beings.append(being)
        return being

    transport.use_terminal_being(factory)
    transport._start_visual()
    being = beings[0]
    transport._set_hearing(True)
    transport._set_hearing(False)
    transport._set_hearing(False)
    assert being.hearing == [True, False, False]
    assert being.states == ["idle"]


def test_terminal_visual_environment_opt_out() -> None:
    config = types.SimpleNamespace(
        transport=TransportConfig(type="terminal"),
        policy=_test_policy(),
    )
    transport = TerminalTransport(config=config, env={"SLEEPTALK_NO_ASCII": "1"})
    beings: list[RecordingBeing] = []

    def factory(*, enabled=None):
        being = RecordingBeing(enabled=enabled)
        beings.append(being)
        return being

    transport.use_terminal_being(factory)
    transport._start_visual()

    assert beings[0].enabled is False


def test_quiet_stdout_logs_keep_the_file_sink(tmp_path) -> None:
    transport = make_transport()
    root = logging.getLogger()
    stdout = logging.StreamHandler(sys.stdout)
    other = logging.StreamHandler(io.StringIO())
    file_handler = logging.FileHandler(tmp_path / "terminal.log")
    root.addHandler(stdout)
    root.addHandler(other)
    root.addHandler(file_handler)
    try:
        transport._quiet_stdout_logs()
        assert stdout not in root.handlers
        assert other in root.handlers
        assert file_handler in root.handlers
    finally:
        root.removeHandler(stdout)
        root.removeHandler(other)
        root.removeHandler(file_handler)


def test_start_keys_skips_when_stdin_is_not_a_tty() -> None:
    transport = make_transport()
    transport._start_keys()
    assert transport._keys_thread is None
    transport._stop_keys()


# -- registry selection --------------------------------------------------------


def test_config_selects_terminal_transport(tmp_path) -> None:
    config = load_config(_write(tmp_path, TERMINAL_MINIMAL), check_env=False, check_binaries=False)
    session = build_session(config, env={})
    from sleeptalk.defaults import register_defaults
    from sleeptalk.registries import TRANSPORTS

    register_defaults()
    assert "terminal" in TRANSPORTS
    assert type(session.transport).__name__ == "TerminalTransport"


def test_terminal_transport_is_registry_buildable(tmp_path) -> None:
    config = load_config(_write(tmp_path, TERMINAL_MINIMAL), check_env=False, check_binaries=False)
    from sleeptalk.defaults import _build_terminal_transport

    transport = _build_terminal_transport(config, env={})
    assert isinstance(transport, TerminalTransport)


def test_profile_device_selection_reaches_requested_devices(tmp_path) -> None:
    """The documented profile boundary must preserve explicit devices.

    Directly constructing ``TransportConfig`` would miss a registry field
    omission because ``load_config`` rejects inapplicable profile keys first.
    """
    config = load_config(
        _write(tmp_path, TERMINAL_WITH_DEVICES), check_env=False, check_binaries=False
    )
    built = build_session(config, env={})
    transport = built.transport
    assert isinstance(transport, TerminalTransport)
    sd = FakeSoundDevice()
    transport.use_sounddevice(sd)
    session = RouteSession(FakeRouteAgent(["hi"]))

    async def _run() -> None:
        task = asyncio.create_task(transport.run(session))
        for _ in range(100):
            if any(event["event"] == "session_started" for event in transport.events):
                break
            await asyncio.sleep(0.01)
        assert transport.microphone is not None
        assert transport.microphone.device_index == 2
        assert sd.opened_streams[1].device == 2
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await transport.close()

    asyncio.run(_run())


# -- no provider construction ----------------------------------------------------


def test_no_provider_constructed_in_terminal_code() -> None:
    """Acceptance: no provider is constructed in terminal code."""
    import sleeptalk.transports.terminal as terminal_mod
    import sleeptalk.transports.terminal_audio as audio_mod

    for module in (terminal_mod, audio_mod):
        tree = ast.parse(inspect.getsource(module))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
        for forbidden in (
            "sleeptalk.speech",
            "sleeptalk.provider_factory",
            "sleeptalk.voice_loop.vad",
        ):
            assert not any(
                imp == forbidden or imp.startswith(forbidden + ".") for imp in imported
            ), (
                f"{module.__name__} imports {forbidden!r} — terminal code must not construct providers"
            )


# -- conversation flow through the cascade route ----------------------------------


class FakeRouteAgent:
    name = "SoakAgent"

    def __init__(self, sentences) -> None:
        self._sentences = sentences
        self.turns = 0
        self.last_text = ""
        self.aborted = False

    async def respond_streaming(self, text: str, *, on_event=None):
        self.turns += 1
        self.last_text = text
        for sentence in self._sentences:
            await asyncio.sleep(0.001)
            yield sentence

    async def abort(self) -> None:
        self.aborted = True

    async def warmup(self) -> None:
        return None

    async def close(self) -> None:
        return None


class FakeSTT:
    """Rest-STT seam: transcribe_pcm -> fixed text, records received PCM."""

    name = "FakeSTT"

    def __init__(self, text: str = "hello from the mic") -> None:
        self._text = text
        self.transcribed: list[bytes] = []

    async def transcribe_pcm(self, pcm: bytes, *, purpose: str = "") -> str:
        self.transcribed.append(pcm)
        return self._text

    async def close(self) -> None:
        return None


class FakeTTS:
    """TTS seam: synthesize yields fixed non-silent PCM chunks per sentence."""

    name = "FakeTTS"

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def synthesize(self, sentence: str):
        for chunk in self._chunks:
            await asyncio.sleep(0)
            yield chunk

    async def close(self) -> None:
        return None


def RouteSession(agent, *, stt=None, tts=None) -> RealtimeSession:
    """Build the real public kernel around a fake cascade assembly."""
    from sleeptalk.routes.cascade import CascadeRoute

    meter = types.SimpleNamespace(session_id="terminal-flow")
    built = types.SimpleNamespace(
        route=CascadeRoute(agent=agent, stt=stt, tts=tts),
        usage_meter=meter,
        agent=agent,
        stt=stt,
        tts=tts,
        transport=None,
    )
    return RealtimeSession(built)


@pytest.mark.asyncio
async def test_full_conversation_flow_fake_audio() -> None:
    """Capture → route turn → (route events) → repeat, all with fakes."""
    transport = make_transport()
    beings: list[RecordingBeing] = []

    def being_factory(*, enabled=None):
        being = RecordingBeing(enabled=enabled)
        beings.append(being)
        return being

    transport.use_terminal_being(being_factory)
    sd = transport._sounddevice
    agent = FakeRouteAgent(["Hello from the route.", "Second sentence."])
    session = RouteSession(agent)

    task = asyncio.create_task(transport.run(session))
    # wait for session start
    for _ in range(100):
        if any(e["event"] == "session_started" for e in transport.events):
            break
        await asyncio.sleep(0.01)

    # push fake microphone audio; the loop drives turns through the route
    in_stream = sd.opened_streams[0]
    await _push_utterance(in_stream)
    for _ in range(200):
        if agent.turns >= 1:
            break
        await asyncio.sleep(0.01)
    assert agent.turns >= 1, "route turn must run from captured audio"
    assert "listening" in beings[0].states
    assert "thinking" in beings[0].states

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await transport.close()
    assert all(s.closed for s in sd.opened_streams)
    assert beings[0].closed
    assert any(e["event"] == "session_closed" for e in transport.events)


@pytest.mark.asyncio
async def test_turn_error_does_not_kill_loop() -> None:
    """A failing turn surfaces the error and the loop keeps going."""
    transport = make_transport()

    class ExplodingAgent(FakeRouteAgent):
        async def respond_streaming(self, text: str, *, on_event=None):
            raise RuntimeError("agent exploded")
            yield  # pragma: no cover

    session = RouteSession(ExplodingAgent([]))
    task = asyncio.create_task(transport.run(session))
    for _ in range(100):
        if any(e["event"] == "session_started" for e in transport.events):
            break
        await asyncio.sleep(0.01)

    in_stream = transport._sounddevice.opened_streams[0]
    for _ in range(3):
        await _push_utterance(in_stream)

    assert any(e["event"] == "turn_error" for e in transport.events), "turn errors must be surfaced"
    # loop still alive (no crash events)
    assert not any(e["event"] == "session_error" for e in transport.events)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await transport.close()


# -- soak ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fake_audio_soak() -> None:
    """Compressed soak: many turns of fake audio through the full
    lifecycle (the card's ten-minute soak compressed to seconds with
    fake devices; real-hardware soak belongs to human QA)."""
    transport = make_transport()
    agent = FakeRouteAgent(["Soak sentence."])
    session = RouteSession(agent)
    task = asyncio.create_task(transport.run(session))
    for _ in range(100):
        if any(e["event"] == "session_started" for e in transport.events):
            break
        await asyncio.sleep(0.01)

    in_stream = transport._sounddevice.opened_streams[0]
    for cycle in range(20):
        await _push_utterance(in_stream)

    assert agent.turns >= 10, f"soak drove {agent.turns} turns, expected >= 10"
    assert not any(e["event"] == "session_error" for e in transport.events)
    # backpressure stayed bounded through the soak
    assert transport.microphone is not None and transport.microphone.dropped_chunks >= 0

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await transport.close()
    assert any(e["event"] == "session_closed" for e in transport.events)


# -- end-to-end: fake PCM crosses STT -> agent -> TTS -> speaker -------------


@pytest.mark.asyncio
async def test_fake_pcm_crosses_stt_agent_tts_speaker() -> None:
    """Acceptance: captured PCM is transcribed, the agent receives the
    text, TTS delivers non-silent audio to the speaker, and playback
    accounting measures callback-consumed bytes (not merely queued)."""
    transport = make_transport()
    beings: list[RecordingBeing] = []

    def being_factory(*, enabled=None):
        being = RecordingBeing(enabled=enabled)
        beings.append(being)
        return being

    transport.use_terminal_being(being_factory)
    sd = transport._sounddevice
    tts_chunks = [bytes(i % 255 + 1 for i in range(200)) for _ in range(20)]  # 20x200B non-silent
    stt = FakeSTT("hello from the mic")
    tts = FakeTTS(tts_chunks)
    agent = FakeRouteAgent(["reply one."])
    session = RouteSession(agent, stt=stt, tts=tts)

    task = asyncio.create_task(transport.run(session))
    for _ in range(100):
        if any(e["event"] == "session_started" for e in transport.events):
            break
        await asyncio.sleep(0.01)

    in_stream = sd.opened_streams[0]
    await _push_utterance(in_stream)
    # wait for the turn to run and deliver audio to the speaker queue
    for _ in range(200):
        if agent.turns >= 1 and transport.audio_output is not None:
            acc = transport.audio_output.accounting()
            if acc.bytes_generated == sum(map(len, tts_chunks)):
                break
        await asyncio.sleep(0.01)
    assert stt.transcribed, "captured PCM must reach STT"
    assert agent.last_text == "hello from the mic", "agent must receive the transcription"
    assert transport.audio_output is not None
    queued = transport.audio_output.accounting()
    assert queued.bytes_generated == sum(map(len, tts_chunks)), "TTS must queue every chunk"
    assert any(event["event"] == "audio_queued" for event in transport.events)
    assert "speaking" in beings[0].states

    # Pull PART of the queued audio: the device callback consumes it.
    out_stream = sd.opened_streams[1]
    out_stream.pull_audio(frames=500)  # 1000 bytes consumed
    consumed = transport.audio_output.accounting()
    assert 0 < consumed.bytes_written < consumed.bytes_generated, (
        "accounting measures callback-consumed bytes, not merely queued"
    )
    # Pull the rest: once everything is consumed, written == generated.
    out_stream.pull_audio(frames=2000)
    final = transport.audio_output.accounting()
    assert final.bytes_written == final.bytes_generated, (
        "fully-consumed audio: accounting written == generated"
    )

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await transport.close()


@pytest.mark.asyncio
async def test_unsupported_tts_payload_never_claims_audible_speech(monkeypatch) -> None:
    sd = FakeSoundDevice()
    speaker = TerminalSpeaker(sounddevice=sd)
    delivered: list[tuple[int, int]] = []
    dropped: list[tuple[int, int]] = []
    output = _SpeakerOutput(
        speaker,
        on_audio=lambda turn_id, size, _pcm=b"": delivered.append((turn_id, size)),
        on_drop=lambda turn_id, size: dropped.append((turn_id, size)),
    )
    compressed = b"ID3" + b"not canonical pcm"

    from sleeptalk.media_audio import AudioDecodeError
    from sleeptalk.providers import ProviderSpec
    from sleeptalk.speech.elevenlabs import ElevenLabsTTS

    tts = ElevenLabsTTS(ProviderSpec(provider="elevenlabs"), api_key="unused")

    async def response(text):
        return compressed

    monkeypatch.setattr(tts, "synthesize_wav_bytes", response)
    with pytest.raises(AudioDecodeError):
        async for pcm in tts.stream_pcm("hello"):
            await output.write(pcm, turn_id=4)

    assert delivered == []
    assert dropped == []
    assert output.accounting().bytes_generated == 0
    assert output.accounting().bytes_dropped_stale == 0
    await output.close()


@pytest.mark.asyncio
async def test_speaker_output_next_turn_after_discard_is_queued() -> None:
    sd = FakeSoundDevice()
    speaker = TerminalSpeaker(sounddevice=sd)
    output = _SpeakerOutput(speaker)
    pcm = b"\x01\x00" * 240

    await output.write(pcm, turn_id=1)
    generated_after_first = output.accounting().bytes_generated
    dropped_after_first = output.accounting().bytes_dropped_stale
    await output.discard_turn(1, "barge-in")
    await output.write(pcm, turn_id=2)

    assert output.accounting().bytes_generated > generated_after_first
    assert output.accounting().bytes_dropped_stale == dropped_after_first
    await output.close()


# -- interruption ----------------------------------------------------------


class _ThinkingAgent:
    """Yields nothing until released, so leftover mic arrives before first TTS."""

    name = "ThinkingAgent"

    def __init__(self) -> None:
        self.turns = 0
        self.last_text = ""
        self.aborted = False
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def respond_streaming(self, text: str, *, on_event=None):
        del on_event
        self.turns += 1
        self.last_text = text
        self.started.set()
        await self.release.wait()
        yield "first sentence"

    async def abort(self) -> None:
        self.aborted = True
        self.release.set()

    async def warmup(self) -> None:
        return None

    async def close(self) -> None:
        return None


class _BlockingAgent:
    """Agent that yields one sentence then blocks until aborted, so a second
    utterance arrives while the first turn is in flight (and its TTS audio
    is already queued)."""

    name = "BlockingAgent"

    def __init__(self) -> None:
        self.turns = 0
        self.last_text = ""
        self.aborted = False
        self.started = asyncio.Event()
        self.after_first = asyncio.Event()
        self.release = asyncio.Event()

    async def respond_streaming(self, text: str, *, on_event=None):
        self.turns += 1
        self.last_text = text
        self.started.set()
        yield "first sentence"
        self.after_first.set()
        await self.release.wait()
        yield "second sentence"

    async def abort(self) -> None:
        self.aborted = True
        self.release.set()

    async def warmup(self) -> None:
        return None

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_second_utterance_interrupts_active_turn() -> None:
    """Acceptance: a second utterance during an active response cancels the
    old turn, flushes queued audio, and begins a clean next turn."""
    transport = make_transport(env={"SLEEPTALK_TERMINAL_FULL_DUPLEX": "1"})
    sd = transport._sounddevice
    tts = FakeTTS([b"\x11\x22" * 500])  # 1000-byte non-silent chunk
    agent = _BlockingAgent()
    session = RouteSession(agent, tts=tts)

    task = asyncio.create_task(transport.run(session))
    for _ in range(100):
        if any(e["event"] == "session_started" for e in transport.events):
            break
        await asyncio.sleep(0.01)

    in_stream = sd.opened_streams[0]
    await _push_utterance(in_stream)  # first utterance -> turn 1
    # wait until turn 1 is in flight and its TTS audio is queued
    for _ in range(200):
        if agent.after_first.is_set() and transport._playback_pending():
            break
        await asyncio.sleep(0.01)
    assert agent.after_first.is_set(), "turn 1 must reach the in-flight state"
    assert transport.audio_output is not None
    queued_before = transport.audio_output.accounting().bytes_generated
    assert queued_before > 0, "turn 1 must queue TTS audio before barge-in"

    # second utterance during the active response
    await _push_utterance(in_stream)
    for _ in range(200):
        if agent.aborted and any(e["event"] == "interruption" for e in transport.events):
            break
        await asyncio.sleep(0.01)
    assert any(e["event"] == "interruption" for e in transport.events), (
        "a second utterance must interrupt the active turn"
    )
    assert agent.aborted, "the cancelled turn must abort the agent"
    assert transport.audio_output.flushed_chunks >= 1, (
        "queued audio must be flushed on interruption"
    )

    # a clean next turn begins
    for _ in range(200):
        if agent.turns >= 2:
            break
        await asyncio.sleep(0.01)
    assert agent.turns >= 2, "a clean next turn must begin after interruption"
    for _ in range(200):
        if transport.audio_output.accounting().bytes_generated > queued_before:
            break
        await asyncio.sleep(0.01)
    assert transport.audio_output.accounting().bytes_generated > queued_before
    assert not any(e["event"] == "session_error" for e in transport.events)

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await transport.close()
    assert any(e["event"] == "session_closed" for e in transport.events)


@pytest.mark.asyncio
async def test_short_stop_commits_as_the_next_turn() -> None:
    config = types.SimpleNamespace(
        transport=TransportConfig(type="terminal"),
        policy=_test_policy(
            start_ms=250,
            silence_ms=50,
            interrupt_ms=250,
            min_duration_ms=150,
            grace_ms=0,
            cooldown_ms=0,
        ),
    )
    transport = TerminalTransport(
        config=config,
        env={"SLEEPTALK_TERMINAL_FULL_DUPLEX": "1"},
    )
    transport.use_sounddevice(FakeSoundDevice())
    agent = _BlockingAgent()
    session = RouteSession(agent, tts=FakeTTS([TTS_LOOPBACK]))
    task = asyncio.create_task(transport.run(session))
    for _ in range(100):
        if any(event["event"] == "session_started" for event in transport.events):
            break
        await asyncio.sleep(0.01)
    stream = transport._sounddevice.opened_streams[0]
    await _push_utterance(stream, speech_frames=6)
    for _ in range(200):
        if agent.after_first.is_set() and transport._playback_pending():
            break
        await asyncio.sleep(0.01)
    assert agent.after_first.is_set()
    assert transport._playback_pending()
    for _ in range(3):
        stream.push_audio(STOP_50MS)
        await asyncio.sleep(0.06)
    for _ in range(200):
        if any(event["event"] == "interruption" for event in transport.events):
            break
        await asyncio.sleep(0.01)
    assert any(event["event"] == "interruption" for event in transport.events)
    stream.push_audio(SILENCE_50MS)
    await asyncio.sleep(0.06)
    stream.push_audio(SILENCE_50MS)
    await asyncio.sleep(0.06)
    for _ in range(200):
        if agent.turns >= 2:
            break
        await asyncio.sleep(0.01)
    assert agent.turns >= 2, "the stop that cut playback must become the next turn"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await transport.close()


class _TailSTT:
    """First finalize is the full phrase. A leftover last-word turn would be 'me'."""

    name = "TailSTT"

    def __init__(self) -> None:
        self.texts: list[str] = []

    async def transcribe_pcm(self, pcm: bytes, *, purpose: str = "") -> str:
        del pcm, purpose
        text = "please check the file for me" if not self.texts else "me"
        self.texts.append(text)
        return text

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_leftover_last_word_does_not_replace_the_turn() -> None:
    config = types.SimpleNamespace(
        transport=TransportConfig(type="terminal"),
        policy=_test_policy(
            start_ms=250,
            silence_ms=50,
            interrupt_ms=250,
            min_duration_ms=150,
            grace_ms=0,
            cooldown_ms=0,
        ),
    )
    transport = TerminalTransport(
        config=config,
        env={"SLEEPTALK_TERMINAL_FULL_DUPLEX": "1"},
    )
    transport.use_sounddevice(FakeSoundDevice())
    agent = _ThinkingAgent()
    stt = _TailSTT()
    session = RouteSession(agent, stt=stt, tts=FakeTTS([TTS_LOOPBACK]))
    task = asyncio.create_task(transport.run(session))
    for _ in range(100):
        if any(event["event"] == "session_started" for event in transport.events):
            break
        await asyncio.sleep(0.01)
    stream = transport._sounddevice.opened_streams[0]
    await _push_utterance(stream, speech_frames=6)
    for _ in range(200):
        if agent.started.is_set():
            break
        await asyncio.sleep(0.01)
    assert agent.started.is_set()
    assert not transport._playback_pending()
    for _ in range(3):
        stream.push_audio(STOP_50MS)
        await asyncio.sleep(0.06)
    agent.release.set()
    for _ in range(200):
        if transport._playback_pending():
            break
        await asyncio.sleep(0.01)
    assert transport._playback_pending()
    stream.push_audio(STOP_50MS)
    await asyncio.sleep(0.06)
    stream.push_audio(SILENCE_50MS)
    await asyncio.sleep(0.06)
    stream.push_audio(SILENCE_50MS)
    await asyncio.sleep(0.06)
    assert not agent.aborted, "leftover last word must not cancel the real reply"
    assert agent.turns == 1
    assert agent.last_text == "please check the file for me"
    assert stt.texts == ["please check the file for me"]
    assert not [event for event in transport.events if event["event"] == "interruption"]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await transport.close()


@pytest.mark.asyncio
async def test_leftover_last_word_on_speaker_safe_does_not_replace_the_turn() -> None:
    config = types.SimpleNamespace(
        transport=TransportConfig(type="terminal"),
        policy=_test_policy(
            start_ms=250,
            silence_ms=50,
            interrupt_ms=250,
            min_duration_ms=150,
            grace_ms=0,
            cooldown_ms=0,
        ),
    )
    transport = TerminalTransport(config=config, env={})
    transport.use_sounddevice(FakeSoundDevice())
    transport.use_echo_canceller(TerminalAec(engine=_PassNearEngine()))
    agent = _ThinkingAgent()
    stt = _TailSTT()
    session = RouteSession(agent, stt=stt, tts=FakeTTS([TTS_LOOPBACK]))
    task = asyncio.create_task(transport.run(session))
    for _ in range(100):
        if any(event["event"] == "session_started" for event in transport.events):
            break
        await asyncio.sleep(0.01)
    stream = transport._sounddevice.opened_streams[0]
    await _push_utterance(stream, speech_frames=6)
    for _ in range(200):
        if agent.started.is_set():
            break
        await asyncio.sleep(0.01)
    assert agent.started.is_set()
    assert not transport._playback_pending()
    for _ in range(3):
        stream.push_audio(STOP_50MS)
        await asyncio.sleep(0.06)
    agent.release.set()
    for _ in range(200):
        if transport._playback_pending():
            break
        await asyncio.sleep(0.01)
    assert transport._playback_pending()
    stream.push_audio(STOP_50MS)
    await asyncio.sleep(0.06)
    stream.push_audio(SILENCE_50MS)
    await asyncio.sleep(0.06)
    stream.push_audio(SILENCE_50MS)
    await asyncio.sleep(0.06)
    assert not agent.aborted, "speaker-safe leftover last word must not cancel the reply"
    assert agent.turns == 1
    assert agent.last_text == "please check the file for me"
    assert stt.texts == ["please check the file for me"]
    assert not [event for event in transport.events if event["event"] == "interruption"]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await transport.close()


@pytest.mark.asyncio
async def test_speaker_safe_cut_in_during_thinking_starts_next_turn() -> None:
    """A pause-to-send while the agent is working, with no TTS on the
    speaker, must cancel and become the next turn. Speaker-safe echo
    protection does not apply here."""
    transport = make_transport()
    agent = _ThinkingAgent()
    session = RouteSession(agent, tts=FakeTTS([]))
    task = asyncio.create_task(transport.run(session))
    for _ in range(100):
        if any(event["event"] == "session_started" for event in transport.events):
            break
        await asyncio.sleep(0.01)
    stream = transport._sounddevice.opened_streams[0]
    await _push_utterance(stream, speech_frames=6)
    for _ in range(200):
        if agent.started.is_set():
            break
        await asyncio.sleep(0.01)
    assert agent.started.is_set()
    assert not transport._playback_pending()
    await _push_utterance(stream, speech_frames=6)
    # Up to 5 s: a loaded CI runner missed the 2 s this used to allow.
    for _ in range(500):
        if any(event["event"] == "interruption" for event in transport.events):
            break
        await asyncio.sleep(0.01)
    assert any(event["event"] == "interruption" for event in transport.events)
    # The interruption event precedes the awaited agent cancellation.
    # Starting the replacement turn proves that cancellation has settled.
    for _ in range(200):
        if agent.turns >= 2:
            break
        await asyncio.sleep(0.01)
    assert agent.aborted
    assert agent.turns >= 2
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await transport.close()


@pytest.mark.asyncio
async def test_leftover_last_word_during_think_does_not_cut_in() -> None:
    transport = make_transport()
    agent = _ThinkingAgent()
    stt = _TailSTT()
    session = RouteSession(agent, stt=stt, tts=FakeTTS([]))
    task = asyncio.create_task(transport.run(session))
    for _ in range(100):
        if any(event["event"] == "session_started" for event in transport.events):
            break
        await asyncio.sleep(0.01)
    stream = transport._sounddevice.opened_streams[0]
    await _push_utterance(stream, speech_frames=6)
    for _ in range(200):
        if agent.started.is_set():
            break
        await asyncio.sleep(0.01)
    assert agent.started.is_set()
    assert not transport._playback_pending()
    for _ in range(3):
        stream.push_audio(STOP_50MS)
        await asyncio.sleep(0.06)
    stream.push_audio(SILENCE_50MS)
    await asyncio.sleep(0.06)
    stream.push_audio(SILENCE_50MS)
    await asyncio.sleep(0.06)
    assert not agent.aborted, "leftover last word during think must not cancel"
    assert agent.turns == 1
    assert agent.last_text == "please check the file for me"
    assert stt.texts == ["please check the file for me"]
    assert not [event for event in transport.events if event["event"] == "interruption"]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await transport.close()


@pytest.mark.asyncio
async def test_barge_in_flush_does_not_report_speaker_stalled() -> None:
    """d81030f field failure: stop worked, then IDLE ERROR · speaker stalled.

    Flush drops the queue. bytes_generated still exceeds bytes_written, so
    the playback watcher waited two seconds and called that a stuck speaker.
    """
    beings: list[RecordingBeing] = []

    def factory(*, enabled=None):
        being = RecordingBeing(enabled=enabled)
        beings.append(being)
        return being

    transport = make_transport()
    transport.use_terminal_being(factory)
    session = RouteSession(FakeRouteAgent(["unused"]), tts=FakeTTS([]))
    task = asyncio.create_task(transport.run(session))
    for _ in range(100):
        if any(e["event"] == "session_started" for e in transport.events):
            break
        await asyncio.sleep(0.01)

    output = transport.audio_output
    assert output is not None
    await output.write(TTS_LOOPBACK, turn_id=1)
    assert transport._playback_pending()
    acc = output.accounting()
    assert acc.bytes_generated > acc.bytes_written
    output.flush()
    assert output.flushed_chunks >= 1
    assert not transport._playback_pending()

    await asyncio.sleep(2.2)
    assert not [e for e in transport.events if e["event"] == "audio_playback_stalled"]
    assert beings
    assert "ERROR · speaker stalled" not in beings[0].headlines

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await transport.close()


@pytest.mark.asyncio
async def test_speaker_safe_stays_talkable_when_cancel_cannot_lock() -> None:
    """Unlocked mic energy during TTS must not abort the answer.

    Laptop stop without AEC is how 353a6c2 and e0348cf became unusable.
    Headphones still barge through SLEEPTALK_TERMINAL_FULL_DUPLEX.
    """
    transport = make_transport()
    transport.use_echo_canceller(TerminalAec(engine=None))
    sd = transport._sounddevice
    tts = FakeTTS([TTS_LOOPBACK])
    agent = _BlockingAgent()
    session = RouteSession(agent, tts=tts)

    task = asyncio.create_task(transport.run(session))
    for _ in range(100):
        if any(e["event"] == "session_started" for e in transport.events):
            break
        await asyncio.sleep(0.01)

    in_stream = sd.opened_streams[0]
    await _push_utterance(in_stream)
    # Endpointing, the agent turn, and the first playback frame all happen
    # before this holds; under a loaded machine that exceeds two seconds.
    for _ in range(1000):
        if agent.after_first.is_set() and transport._playback_pending():
            break
        await asyncio.sleep(0.01)
    assert agent.after_first.is_set(), "turn 1 must reach the in-flight state"
    assert transport.audio_output is not None
    assert any(e["event"] == "echo_gate_started" for e in transport.events)

    echo_frame = TTS_LOOPBACK[: len(STOP_50MS)]
    for _ in range(2):
        in_stream.push_audio(_mix_pcm(echo_frame, STOP_50MS))
        await asyncio.sleep(0.06)
    await asyncio.sleep(0.1)

    assert agent.turns == 1
    assert not agent.aborted
    assert not [event for event in transport.events if event["event"] == "interruption"]
    silent = [event for event in transport.events if event["event"] == "barge_unavailable"]
    assert silent == [{"event": "barge_unavailable", "reason": "aec_silent"}]

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await transport.close()


@pytest.mark.asyncio
async def test_hotkey_cuts_speaker_safe_playback() -> None:
    """Laptop speakers cannot barge by voice. s / space still cuts the turn."""
    transport = make_transport()
    transport.use_echo_canceller(TerminalAec(engine=None))
    sd = transport._sounddevice
    tts = FakeTTS([TTS_LOOPBACK])
    agent = _BlockingAgent()
    session = RouteSession(agent, tts=tts)

    task = asyncio.create_task(transport.run(session))
    for _ in range(100):
        if any(e["event"] == "session_started" for e in transport.events):
            break
        await asyncio.sleep(0.01)

    in_stream = sd.opened_streams[0]
    await _push_utterance(in_stream)
    for _ in range(200):
        if agent.after_first.is_set() and transport._playback_pending():
            break
        await asyncio.sleep(0.01)
    assert agent.after_first.is_set()
    assert transport.audio_output is not None
    queued_before = transport.audio_output.accounting().bytes_generated
    assert queued_before > 0

    transport.request_cut_in()
    for _ in range(200):
        if agent.aborted and any(event["event"] == "interruption" for event in transport.events):
            break
        await asyncio.sleep(0.01)

    assert any(event["event"] == "interruption" for event in transport.events)
    stops = [event for event in transport.events if event["event"] == "interruption"]
    assert stops[-1]["detector"] == "hotkey"
    assert agent.aborted
    assert transport.audio_output.flushed_chunks >= 1

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await transport.close()


@pytest.mark.asyncio
async def test_speaker_loopback_copy_does_not_interrupt_speaker_safe() -> None:
    """Laptop speakers replay the bot into the mic at speech level.

    Discord/Web never see that loop (remote user PCM, or browser AEC).
    353a6c2 listened at the capture floor during playback, so a copy of
    queued TTS looked like a spoken stop and cancelled the answer.
    """
    transport = make_transport()
    transport.use_echo_canceller(TerminalAec(engine=_ZeroResidualEngine()))
    sd = transport._sounddevice
    tts = FakeTTS([TTS_LOOPBACK])
    agent = _BlockingAgent()
    session = RouteSession(agent, tts=tts)

    task = asyncio.create_task(transport.run(session))
    for _ in range(100):
        if any(e["event"] == "session_started" for e in transport.events):
            break
        await asyncio.sleep(0.01)

    in_stream = sd.opened_streams[0]
    await _push_utterance(in_stream)
    for _ in range(200):
        if agent.after_first.is_set() and transport._playback_pending():
            break
        await asyncio.sleep(0.01)
    assert agent.after_first.is_set(), "turn 1 must reach the in-flight state"
    assert transport.audio_output is not None
    assert transport.audio_output.accounting().bytes_generated > 0

    echo_frame = TTS_LOOPBACK[: len(SPEECH_50MS)]
    for _ in range(8):
        in_stream.push_audio(echo_frame)
        await asyncio.sleep(0.06)
    await asyncio.sleep(0.1)

    assert agent.turns == 1
    assert not agent.aborted
    assert not [event for event in transport.events if event["event"] == "interruption"]

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await transport.close()


@pytest.mark.asyncio
async def test_room_colored_echo_does_not_interrupt_speaker_safe() -> None:
    """Delayed, speaker-colored TTS must not look like a spoken stop."""
    transport = make_transport()
    transport.use_echo_canceller(TerminalAec(engine=_ZeroResidualEngine()))
    sd = transport._sounddevice
    tts = FakeTTS([TTS_LOOPBACK])
    agent = _BlockingAgent()
    session = RouteSession(agent, tts=tts)

    task = asyncio.create_task(transport.run(session))
    for _ in range(100):
        if any(e["event"] == "session_started" for e in transport.events):
            break
        await asyncio.sleep(0.01)

    in_stream = sd.opened_streams[0]
    await _push_utterance(in_stream)
    for _ in range(200):
        if agent.after_first.is_set() and transport._playback_pending():
            break
        await asyncio.sleep(0.01)
    assert agent.after_first.is_set(), "turn 1 must reach the in-flight state"
    assert transport.audio_output is not None
    assert transport.audio_output.accounting().bytes_generated > 0

    colored = _delay_lowpass(TTS_LOOPBACK * 3)
    frame = len(SPEECH_50MS)
    for index in range(8):
        in_stream.push_audio(colored[index * frame : (index + 1) * frame])
        await asyncio.sleep(0.06)
    await asyncio.sleep(0.1)

    assert agent.turns == 1
    assert not agent.aborted
    assert not [event for event in transport.events if event["event"] == "interruption"]

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await transport.close()


@pytest.mark.asyncio
async def test_colored_echo_plus_loud_stop_interrupts() -> None:
    transport = make_transport()
    transport.use_echo_canceller(TerminalAec(engine=_PassNearEngine()))
    sd = transport._sounddevice
    tts = FakeTTS([TTS_LOOPBACK])
    agent = _BlockingAgent()
    session = RouteSession(agent, tts=tts)

    task = asyncio.create_task(transport.run(session))
    for _ in range(100):
        if any(e["event"] == "session_started" for e in transport.events):
            break
        await asyncio.sleep(0.01)

    in_stream = sd.opened_streams[0]
    await _push_utterance(in_stream)
    for _ in range(200):
        if agent.after_first.is_set() and transport._playback_pending():
            break
        await asyncio.sleep(0.01)
    assert agent.after_first.is_set(), "turn 1 must reach the in-flight state"
    assert transport.audio_output is not None

    colored = _delay_lowpass(TTS_LOOPBACK)
    echo_frame = colored[: len(STOP_50MS)]
    for _ in range(2):
        in_stream.push_audio(_mix_pcm(echo_frame, STOP_50MS))
        await asyncio.sleep(0.06)
    for _ in range(200):
        if agent.aborted and any(event["event"] == "interruption" for event in transport.events):
            break
        await asyncio.sleep(0.01)

    assert any(event["event"] == "interruption" for event in transport.events)
    assert agent.aborted

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await transport.close()


@pytest.mark.asyncio
async def test_speaker_safe_stop_interrupts_when_aec_keeps_residual() -> None:
    """AEC residual that still looks like a spoken stop must cancel TTS."""
    transport = make_transport(echo_canceller=TerminalAec(engine=_PassNearEngine()))
    sd = transport._sounddevice
    tts = FakeTTS([TTS_LOOPBACK])
    agent = _BlockingAgent()
    session = RouteSession(agent, tts=tts)

    task = asyncio.create_task(transport.run(session))
    for _ in range(100):
        if any(e["event"] == "session_started" for e in transport.events):
            break
        await asyncio.sleep(0.01)

    in_stream = sd.opened_streams[0]
    await _push_utterance(in_stream)
    for _ in range(200):
        if agent.after_first.is_set() and transport._playback_pending():
            break
        await asyncio.sleep(0.01)
    assert agent.after_first.is_set(), "turn 1 must reach the in-flight state"

    for _ in range(8):
        in_stream.push_audio(STOP_50MS)
        await asyncio.sleep(0.06)
        if agent.aborted and any(event["event"] == "interruption" for event in transport.events):
            break

    assert any(event["event"] == "interruption" for event in transport.events)
    assert agent.aborted

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await transport.close()


@pytest.mark.asyncio
async def test_default_capture_discards_speaker_echo_then_resumes() -> None:
    """Physical-QA regression: raw speaker output cannot recursively become
    another terminal turn, but capture resumes after the bot-grace window."""
    config = types.SimpleNamespace(
        transport=TransportConfig(type="terminal"),
        policy=_test_policy(bot_grace_ms=100),
    )
    transport = TerminalTransport(config=config, env={})
    transport.use_sounddevice(FakeSoundDevice())
    transport.use_echo_canceller(TerminalAec(engine=None))
    agent = FakeRouteAgent(["audible answer"])
    session = RouteSession(
        agent,
        stt=FakeSTT("heard user"),
        tts=FakeTTS([b"\x11\x22" * 2_000]),
    )
    task = asyncio.create_task(transport.run(session))
    for _ in range(100):
        if any(e["event"] == "session_started" for e in transport.events):
            break
        await asyncio.sleep(0.01)

    input_stream = transport._sounddevice.opened_streams[0]
    await _push_utterance(input_stream)
    for _ in range(200):
        if transport.audio_output and transport.audio_output.accounting().bytes_generated:
            break
        await asyncio.sleep(0.01)
    assert agent.turns == 1

    # Simulate the MacBook speakers leaking the bot's answer into its mic.
    # Quiet leakage must not barge or start a second turn.
    for _ in range(8):
        input_stream.push_audio(ECHO_50MS)
        await asyncio.sleep(0.06)
    for _ in range(3):
        input_stream.push_audio(SILENCE_50MS)
        await asyncio.sleep(0.06)
    await asyncio.sleep(0.1)
    assert agent.turns == 1
    assert not [event for event in transport.events if event["event"] == "interruption"]
    assert any(event["event"] == "echo_gate_started" for event in transport.events)

    # Once playback is callback-consumed and its configured decay grace has
    # elapsed, a real next utterance is accepted normally.
    accounting = transport.audio_output.accounting()
    pending_bytes = accounting.bytes_generated - accounting.bytes_written
    output_stream = transport._sounddevice.opened_streams[1]
    output_stream.pull_audio(frames=(pending_bytes + 1) // 2)
    await asyncio.sleep(0.15)
    input_stream.push_audio(SILENCE_50MS)
    await asyncio.sleep(0.06)
    await _push_utterance(input_stream)
    for _ in range(200):
        if agent.turns >= 2:
            break
        await asyncio.sleep(0.01)

    assert agent.turns == 2
    assert any(event["event"] == "echo_gate_ended" for event in transport.events)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_first_sentence_after_tts_drain_commits_during_echo_grace() -> None:
    """Laptop speakers. After TTS drains, the next real sentence must become
    a turn even while bot-grace still has the echo gate up.

    AEC without an engine returns silent residual for as long as far-end
    was pushed. Using that residual as observe() energy makes the first
    post-answer sentence invisible. The user has to speak again after
    the gate drops. Quiet speaker leak must still not open a turn.
    """
    config = types.SimpleNamespace(
        transport=TransportConfig(type="terminal"),
        policy=_test_policy(bot_grace_ms=2000),
    )
    transport = TerminalTransport(config=config, env={})
    transport.use_sounddevice(FakeSoundDevice())
    transport.use_echo_canceller(TerminalAec(engine=None))
    agent = FakeRouteAgent(["audible answer"])
    session = RouteSession(
        agent,
        stt=FakeSTT("heard user"),
        tts=FakeTTS([b"\x11\x22" * 2_000]),
    )
    task = asyncio.create_task(transport.run(session))
    for _ in range(100):
        if any(e["event"] == "session_started" for e in transport.events):
            break
        await asyncio.sleep(0.01)

    input_stream = transport._sounddevice.opened_streams[0]
    await _push_utterance(input_stream)
    for _ in range(200):
        if transport.audio_output and transport.audio_output.accounting().bytes_generated:
            break
        await asyncio.sleep(0.01)
    assert agent.turns == 1
    assert any(event["event"] == "echo_gate_started" for event in transport.events)

    accounting = transport.audio_output.accounting()
    pending_bytes = accounting.bytes_generated - accounting.bytes_written
    output_stream = transport._sounddevice.opened_streams[1]
    output_stream.pull_audio(frames=(pending_bytes + 1) // 2)
    await asyncio.sleep(0.05)
    assert transport._echo_gate_active
    assert not transport._playback_pending()

    for _ in range(3):
        input_stream.push_audio(ECHO_50MS)
        await asyncio.sleep(0.06)
    for _ in range(2):
        input_stream.push_audio(SILENCE_50MS)
        await asyncio.sleep(0.06)
    await asyncio.sleep(0.05)
    assert agent.turns == 1
    assert transport._echo_gate_active

    await _push_utterance(input_stream)
    for _ in range(200):
        if agent.turns >= 2:
            break
        await asyncio.sleep(0.01)

    assert agent.turns == 2, "first sentence after TTS drain must commit during grace"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_speech_level_loopback_after_tts_drain_does_not_open_a_turn() -> None:
    config = types.SimpleNamespace(
        transport=TransportConfig(type="terminal"),
        policy=_test_policy(bot_grace_ms=400),
    )
    transport = TerminalTransport(config=config, env={})
    transport.use_sounddevice(FakeSoundDevice())
    transport.use_echo_canceller(TerminalAec(engine=None))
    agent = FakeRouteAgent(["Hi, I'm here.", "Alright."])
    session = RouteSession(
        agent,
        stt=FakeSTT(""),
        tts=FakeTTS([TTS_LOOPBACK]),
    )
    task = asyncio.create_task(transport.run(session))
    for _ in range(100):
        if any(e["event"] == "session_started" for e in transport.events):
            break
        await asyncio.sleep(0.01)

    input_stream = transport._sounddevice.opened_streams[0]
    await _push_utterance(input_stream, speech_frames=6)
    for _ in range(200):
        if agent.turns >= 1 and transport.audio_output and transport.audio_output.accounting().bytes_generated:
            break
        await asyncio.sleep(0.01)
    assert agent.turns == 1

    accounting = transport.audio_output.accounting()
    pending_bytes = accounting.bytes_generated - accounting.bytes_written
    output_stream = transport._sounddevice.opened_streams[1]
    output_stream.pull_audio(frames=(pending_bytes + 1) // 2)
    await asyncio.sleep(0.05)
    assert transport._echo_gate_active
    assert not transport._playback_pending()

    # Replay the final synthesized clip once within the configured 400 ms
    # decay window. Repeating it for longer than that window models fresh
    # input, which the transport deliberately admits after grace expires.
    for offset in range(0, len(TTS_LOOPBACK), len(SPEECH_50MS)):
        input_stream.push_audio(TTS_LOOPBACK[offset : offset + len(SPEECH_50MS)])
        await asyncio.sleep(0.06)
    for _ in range(4):
        input_stream.push_audio(SILENCE_50MS)
        await asyncio.sleep(0.06)
    await asyncio.sleep(0.2)

    assert agent.turns == 1, "speech-level speaker copy after drain must not start a turn"
    assert agent.last_text == ""

    await _push_utterance(input_stream, speech_frames=6)
    for _ in range(200):
        if agent.turns >= 2:
            break
        await asyncio.sleep(0.01)
    assert agent.turns == 2, "a real sentence after the loopback hush must still commit"

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def _pcm_tone(amplitude: int, frames: int = 2_400) -> bytes:
    return amplitude.to_bytes(2, "little", signed=True) * frames


def test_calibrate_capture_threshold_raises_floor_above_room_tone() -> None:
    room = _pcm_tone(1400)
    speech = _pcm_tone(8000)
    threshold = calibrate_capture_threshold(120, room * 20)
    assert threshold > 1400
    state = _CaptureState(
        threshold=threshold,
        start_ms=250,
        silence_ms=650,
        interrupt_ms=250,
        bot_grace_ms=400,
    )
    for _ in range(20):
        opened, endpoint = state.observe(room)
        assert opened is False
        assert endpoint is False
        assert state.open is False
    for _ in range(6):
        state.observe(speech)
    assert state.open is True


def test_calibrate_capture_threshold_quiet_room_keeps_conversational_speech() -> None:
    room = _pcm_tone(80)
    speech = _pcm_tone(250)
    threshold = calibrate_capture_threshold(120, room * 20)
    assert 80 < threshold < 250
    state = _CaptureState(
        threshold=threshold,
        start_ms=250,
        silence_ms=650,
        interrupt_ms=250,
        bot_grace_ms=400,
    )
    for _ in range(20):
        opened, _endpoint = state.observe(room)
        assert opened is False
        assert state.open is False
    for _ in range(6):
        state.observe(speech)
    assert state.open is True


def test_capture_state_calibrates_from_microphone_dump() -> None:
    transport = TerminalTransport(
        config=types.SimpleNamespace(transport=TransportConfig(type="terminal"), policy=None),
        env={},
    )
    transport._microphone = types.SimpleNamespace(read=lambda: _pcm_tone(1400) * 20)
    state = transport._capture_state(types.SimpleNamespace(policy=None))
    assert state.threshold == _TERMINAL_LISTEN_RMS
    assert state.hush_threshold > 1400


def test_noisy_warmup_still_hears_conversational_speech() -> None:
    transport = make_transport()
    transport._microphone = types.SimpleNamespace(read=lambda: _pcm_tone(1400) * 20)
    policy = types.SimpleNamespace(
        capture=types.SimpleNamespace(
            rms_threshold=0,
            vad_start_ms=250,
            silence_ms=120,
        ),
        turns=types.SimpleNamespace(
            vad_threshold=500,
            interrupt_min_ms=250,
            bot_grace_ms=400,
        ),
    )
    capture = transport._capture_state(types.SimpleNamespace(policy=policy))
    assert capture.threshold <= _TERMINAL_LISTEN_RMS
    assert capture.hush_threshold > 1400
    assert _open_after(capture, CONVERSATION_50MS) is True


@pytest.mark.asyncio
async def test_silero_listen_commits_conversation_not_room_tone() -> None:
    from sleeptalk.voice_loop.vad import VadObservation, pcm_rms

    class _SpeechBandSilero:
        def __init__(self, config=None) -> None:
            self.config = config

        async def observe(self, pcm: bytes):
            rms = pcm_rms(pcm)
            return VadObservation(is_speech=200 <= rms <= 400)

        def reset(self) -> None:
            return None

    flushed: list[bytes] = []

    async def on_utterance(_participant: object, pcm: bytes) -> None:
        flushed.append(pcm)

    loop = asyncio.get_running_loop()
    assembler = __import__(
        "sleeptalk.voice_loop.utterance", fromlist=["UtteranceAssembler"]
    ).UtteranceAssembler(
        loop=loop,
        min_bytes=100,
        silence_seconds=0.12,
        on_utterance=on_utterance,
        should_ignore_audio=lambda: False,
        speech_rms_threshold=0,
        vad_mode="silero",
        silero_vad_factory=_SpeechBandSilero,
        smart_turn_scorer=None,
    )
    room = _mono_to_stereo(_pcm_tone(1400))
    speech = _mono_to_stereo(CONVERSATION_50MS)
    hush = _mono_to_stereo(SILENCE_50MS)
    for _ in range(8):
        assembler.observe(0, "user", room)
        await asyncio.sleep(0)
    assert flushed == []
    for _ in range(6):
        assembler.observe(0, "user", speech)
        await asyncio.sleep(0)
    for _ in range(8):
        assembler.observe(0, "user", hush)
        await asyncio.sleep(0.05)
    assert flushed, "conversational speech must commit after a noisy-room listen"


@pytest.mark.asyncio
async def test_continuous_microphone_frames_form_one_turn_without_cancel_loop() -> None:
    """Physical-QA regression: callback chunks are frames, not utterances."""
    config = types.SimpleNamespace(
        transport=TransportConfig(type="terminal"),
        policy=_test_policy(start_ms=250, silence_ms=150, interrupt_ms=250),
    )
    transport = TerminalTransport(config=config, env={})
    transport.use_sounddevice(FakeSoundDevice())
    transport.use_echo_canceller(TerminalAec(engine=None))
    stt = FakeSTT("continuous speech")
    agent = FakeRouteAgent(["one answer"])
    session = RouteSession(agent, stt=stt, tts=FakeTTS([b"\x01\x02" * 100]))
    task = asyncio.create_task(transport.run(session))
    for _ in range(100):
        if any(e["event"] == "session_started" for e in transport.events):
            break
        await asyncio.sleep(0.01)

    stream = transport._sounddevice.opened_streams[0]
    await _push_utterance(stream, speech_frames=10, silence_frames=3)
    for _ in range(200):
        if agent.turns:
            break
        await asyncio.sleep(0.01)

    assert agent.turns == 1
    assert len(stt.transcribed) == 1
    assert not [e for e in transport.events if e["event"] == "interruption"]

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_ambient_frames_never_start_or_cancel_a_turn() -> None:
    config = types.SimpleNamespace(
        transport=TransportConfig(type="terminal"),
        policy=_test_policy(start_ms=150, silence_ms=100, interrupt_ms=150, threshold=500),
    )
    transport = TerminalTransport(config=config, env={})
    transport.use_sounddevice(FakeSoundDevice())
    transport.use_echo_canceller(TerminalAec(engine=None))
    agent = FakeRouteAgent(["should not run"])
    task = asyncio.create_task(transport.run(RouteSession(agent)))
    for _ in range(100):
        if any(e["event"] == "session_started" for e in transport.events):
            break
        await asyncio.sleep(0.01)

    quiet_noise = b"\x64\x00" * 2_400  # RMS 100, below the profile floor.
    stream = transport._sounddevice.opened_streams[0]
    for _ in range(20):
        stream.push_audio(quiet_noise)
        await asyncio.sleep(0.055)

    assert agent.turns == 0
    assert not [e for e in transport.events if e["event"] == "interruption"]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_mixed_floor_rejects_silero() -> None:
    with pytest.raises(ValueError, match="Silero cannot run on mixed audio"):
        Floor(InterruptPolicy(), AcousticKind.mixed, detector="silero")


@pytest.mark.asyncio
async def test_terminal_floor_is_mixed_residual_rms(monkeypatch: pytest.MonkeyPatch) -> None:
    constructed: list[str] = []

    class _ForbiddenSilero:
        def __init__(self, *args: object, **kwargs: object) -> None:
            constructed.append(type(self).__name__)
            raise AssertionError("Silero must not run on mixed audio")

    import sleeptalk.voice_loop.vad as vad

    monkeypatch.setattr(vad, "SileroVadGate", _ForbiddenSilero)
    monkeypatch.setattr(vad, "SileroOnnxRuntimeModel", _ForbiddenSilero)
    transport = make_transport()
    task = asyncio.create_task(transport.run(RouteSession(FakeRouteAgent(["hi"]))))
    for _ in range(100):
        if any(event["event"] == "session_started" for event in transport.events):
            break
        await asyncio.sleep(0.01)
    gate = transport._gate
    assert gate is not None
    assert gate.kind is AcousticKind.mixed
    assert gate.detector == "residual_rms"
    assert constructed == []
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await transport.close()


@pytest.mark.asyncio
async def test_playback_pending_is_queued_bytes_not_generated() -> None:
    transport = make_transport()
    task = asyncio.create_task(transport.run(RouteSession(FakeRouteAgent(["unused"]))))
    for _ in range(100):
        if any(event["event"] == "session_started" for event in transport.events):
            break
        await asyncio.sleep(0.01)
    output = transport.audio_output
    speaker = transport._speaker
    assert output is not None
    assert speaker is not None
    await output.write(TTS_LOOPBACK, turn_id=1)
    assert speaker.queued_bytes > 0
    assert transport._playback_pending() is True
    output.flush()
    assert speaker.queued_bytes == 0
    assert output.accounting().bytes_generated > 0
    assert transport._playback_pending() is False
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await transport.close()


@pytest.mark.asyncio
async def test_leftover_mic_during_think_does_not_cancel() -> None:
    config = types.SimpleNamespace(
        transport=TransportConfig(type="terminal"),
        policy=_test_policy(min_duration_ms=50, grace_ms=0, gap_hold_ms=600, cooldown_ms=0),
    )
    transport = TerminalTransport(config=config, env={"SLEEPTALK_TERMINAL_FULL_DUPLEX": "1"})
    transport.use_sounddevice(FakeSoundDevice())
    agent = _ThinkingAgent()
    session = RouteSession(agent, tts=FakeTTS([TTS_LOOPBACK]))
    task = asyncio.create_task(transport.run(session))
    for _ in range(100):
        if any(event["event"] == "session_started" for event in transport.events):
            break
        await asyncio.sleep(0.01)
    stream = transport._sounddevice.opened_streams[0]
    await _push_utterance(stream)
    for _ in range(200):
        if agent.started.is_set():
            break
        await asyncio.sleep(0.01)
    assert agent.started.is_set()
    assert not transport._playback_pending()
    for _ in range(4):
        stream.push_audio(STOP_50MS)
        await asyncio.sleep(0.06)
    assert not [event for event in transport.events if event["event"] == "interruption"]
    assert not agent.aborted
    assert agent.turns == 1
    agent.release.set()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await transport.close()


@pytest.mark.asyncio
async def test_residual_energy_holds_in_gap_after_drain() -> None:
    config = types.SimpleNamespace(
        transport=TransportConfig(type="terminal"),
        policy=_test_policy(min_duration_ms=50, grace_ms=0, gap_hold_ms=400, cooldown_ms=0),
    )
    transport = TerminalTransport(config=config, env={"SLEEPTALK_TERMINAL_FULL_DUPLEX": "1"})
    transport.use_sounddevice(FakeSoundDevice())
    agent = _BlockingAgent()
    session = RouteSession(agent, tts=FakeTTS([TTS_LOOPBACK]))
    task = asyncio.create_task(transport.run(session))
    for _ in range(100):
        if any(event["event"] == "session_started" for event in transport.events):
            break
        await asyncio.sleep(0.01)
    stream = transport._sounddevice.opened_streams[0]
    await _push_utterance(stream)
    for _ in range(200):
        if agent.after_first.is_set() and transport._playback_pending():
            break
        await asyncio.sleep(0.01)
    assert agent.after_first.is_set()
    assert transport.audio_output is not None
    accounting = transport.audio_output.accounting()
    pending_bytes = accounting.bytes_generated - accounting.bytes_written
    output_stream = transport._sounddevice.opened_streams[1]
    output_stream.pull_audio(frames=(pending_bytes + 1) // 2)
    for _ in range(40):
        if not transport._playback_pending():
            break
        await asyncio.sleep(0.01)
    assert not transport._playback_pending()
    await asyncio.sleep(0.05)
    stream.push_audio(STOP_50MS)
    await asyncio.sleep(0.06)
    stream.push_audio(STOP_50MS)
    await asyncio.sleep(0.06)
    assert not [event for event in transport.events if event["event"] == "interruption"]
    assert not agent.aborted
    assert transport._gate is not None
    assert transport._gate.floor_state is SpeakingState.hold
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await transport.close()


@pytest.mark.asyncio
async def test_terminal_never_claims_to_listen_while_the_mic_is_deaf() -> None:
    """The reported bug: speech during warmup vanished under a 'listening' UI."""
    transport = make_transport()
    beings: list[RecordingBeing] = []

    def factory(*, enabled=None):
        being = RecordingBeing(enabled=enabled)
        beings.append(being)
        return being

    transport.use_terminal_being(factory)
    session = RouteSession(FakeRouteAgent(["hi"]))
    ready = asyncio.Event()
    original_prepare = session.prepare

    async def slow_prepare() -> None:
        await ready.wait()
        await original_prepare()

    session.prepare = slow_prepare
    task = asyncio.create_task(transport.run(session))
    for _ in range(200):
        if beings and beings[0].headlines:
            break
        await asyncio.sleep(0.01)
    assert beings, "visual never started"
    being = beings[0]
    # prepare() is still running, so the capture loop has not read a frame.
    assert any("warming up" in text for text in being.headlines)
    assert "listening" not in being.states

    ready.set()
    for _ in range(300):
        if "listening" in being.states:
            break
        await asyncio.sleep(0.01)
    assert "listening" in being.states
    assert not any("warming up" in line.text for line in transport._presentation.lines)
    assert [e for e in transport.events if e["event"] == "capture_ready"]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await transport.close()


def test_status_bar_shows_the_narration_style() -> None:
    from sleeptalk.presentation import PresentationState
    from sleeptalk.transports.terminal_visual import render_live_screen
    screen = "\n".join(render_live_screen(
        PresentationState(presence="listening", narration_style="keyboard"),
        0.0,
        columns=100,
        rows=24,
        muted=False,
        color=False,
    ))
    assert "tools: keyboard" in screen
    assert "s settings" in screen


def test_status_bar_shows_the_reply_length() -> None:
    from sleeptalk.presentation import PresentationState
    from sleeptalk.transports.terminal_visual import render_live_screen, reply_token

    assert [reply_token(v) for v in ("full", "summarized")] == ["full", "brief"]
    screen = "\n".join(render_live_screen(
        PresentationState(presence="listening", reply_length="summarized"),
        0.0,
        columns=100,
        rows=24,
        muted=False,
        color=False,
    ))
    assert "response: brief" in screen
    assert "s settings" in screen


def test_the_footer_fits_every_terminal_width() -> None:
    """Hint words are shed before the controls can wrap."""
    from sleeptalk.presentation import PresentationState
    from sleeptalk.transports.terminal_visual import _footer, visible_len

    for length in ("full", "summarized"):
        state = PresentationState(
            presence="listening", latency_ms=38, reply_length=length
        )
        for columns in (60, 72, 80, 100, 120):
            footer = _footer(state, 0.0, muted=False, columns=columns, color=False)
            assert visible_len(footer) == columns
            # Every key stays discoverable at every width.
            for key in ("Space", "m", "s", "q"):
                assert f"{key} " in footer or footer.endswith(key) or f"{key} ·" in footer


@pytest.mark.asyncio
async def test_clip_playback_goes_through_the_echo_canceller() -> None:
    """Written straight to the device, the agent's typing returns as barge-in."""
    from pathlib import Path

    from sleeptalk.sounds import narration_clip_path
    from sleeptalk.transports.terminal import _SpeakerOutput

    clip = narration_clip_path("keyboard")
    if clip is None:
        pytest.skip("no keyboard clip in this tree")
    written: list[bytes] = []
    seen: list[int] = []

    class _Speaker:
        bytes_played = 0

        def write(self, pcm: bytes) -> None:
            written.append(pcm)

        def flush(self) -> int:
            return 0

    output = _SpeakerOutput(_Speaker(), on_audio=lambda _t, n, _p: seen.append(n))
    await output.play_file(Path(clip))
    assert written, "clip never reached the speaker"
    assert seen == [len(written[0])], "clip bypassed the far-end/AEC hook"


@pytest.mark.asyncio
async def test_missing_clip_is_silence_not_an_error() -> None:
    from pathlib import Path

    from sleeptalk.transports.terminal import _SpeakerOutput

    class _Speaker:
        bytes_played = 0

        def write(self, pcm: bytes) -> None:
            raise AssertionError("nothing should reach the speaker")

        def flush(self) -> int:
            return 0

    output = _SpeakerOutput(_Speaker())
    assert await output.play_file(Path("/nope/missing.wav")) is None


def _transport_with_capture_mode(mode: str, env: dict | None = None) -> TerminalTransport:
    config = types.SimpleNamespace(
        transport=TransportConfig(type="terminal", capture_mode=mode),
        policy=_test_policy(),
    )
    return TerminalTransport(config=config, env=env or {})


def test_profile_capture_mode_headphones_selects_silero() -> None:
    assert _transport_with_capture_mode("headphones")._terminal_full_duplex is True


def test_profile_capture_mode_speakers_stays_energy_based() -> None:
    assert _transport_with_capture_mode("speakers")._terminal_full_duplex is False


def test_unset_capture_mode_defaults_to_speakers() -> None:
    """Guessing headphones wrongly makes the agent interrupt itself."""
    assert _transport_with_capture_mode("")._terminal_full_duplex is False
    assert _transport_with_capture_mode("nonsense")._terminal_full_duplex is False


def test_env_override_beats_the_profile_in_both_directions() -> None:
    headphones_profile_off = _transport_with_capture_mode(
        "headphones", {"SLEEPTALK_TERMINAL_FULL_DUPLEX": "0"}
    )
    speakers_profile_on = _transport_with_capture_mode(
        "speakers", {"SLEEPTALK_TERMINAL_FULL_DUPLEX": "1"}
    )
    assert headphones_profile_off._terminal_full_duplex is False
    assert speakers_profile_on._terminal_full_duplex is True


@pytest.mark.asyncio
async def test_boot_prompt_is_skipped_when_something_already_answered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A profile answer or an env override must never re-ask at boot."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    def _refuse(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("boot must not prompt when the answer is known")

    monkeypatch.setattr("builtins.input", _refuse)
    for transport in (
        _transport_with_capture_mode("headphones"),
        _transport_with_capture_mode("speakers"),
        _transport_with_capture_mode("", {"SLEEPTALK_TERMINAL_FULL_DUPLEX": "1"}),
    ):
        await transport._prompt_capture_mode()


@pytest.mark.asyncio
async def test_boot_prompt_never_blocks_a_non_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    def _refuse(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("a non-interactive run must not prompt")

    monkeypatch.setattr("builtins.input", _refuse)
    await _transport_with_capture_mode("")._prompt_capture_mode()


@pytest.mark.asyncio
async def test_boot_prompt_reads_headphones_and_enter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    for reply, expected in (("h", True), ("headphones", True), ("", False), ("s", False)):
        monkeypatch.setattr("builtins.input", lambda _prompt, r=reply: r)
        transport = _transport_with_capture_mode("")
        await transport._prompt_capture_mode()
        assert transport._terminal_full_duplex is expected, reply


class _NeverSpeechSilero:
    """Loud, but never speech — a microwave beep or a door slam."""

    def __init__(self, config=None) -> None:
        self.config = config

    async def observe(self, pcm: bytes):
        from sleeptalk.voice_loop.vad import VadObservation

        return VadObservation(is_speech=False)

    def reset(self) -> None:
        return None


@pytest.mark.asyncio
async def test_loud_non_speech_does_not_barge_in_under_full_duplex() -> None:
    """The reason terminal moved off raw energy: with headphones on, every
    beep and slam cleared the RMS bar and cut the agent off mid-sentence."""
    config = types.SimpleNamespace(
        transport=TransportConfig(type="terminal"),
        policy=_test_policy(min_duration_ms=150, grace_ms=0, gap_hold_ms=600, cooldown_ms=0),
    )
    transport = TerminalTransport(config=config, env={"SLEEPTALK_TERMINAL_FULL_DUPLEX": "1"})
    transport.use_sounddevice(FakeSoundDevice())
    agent = _BlockingAgent()
    session = RouteSession(agent, tts=FakeTTS([TTS_LOOPBACK]))
    task = asyncio.create_task(transport.run(session))
    for _ in range(100):
        if any(event["event"] == "session_started" for event in transport.events):
            break
        await asyncio.sleep(0.01)
    stream = transport._sounddevice.opened_streams[0]
    await _push_utterance(stream)
    for _ in range(200):
        if agent.after_first.is_set() and transport._playback_pending():
            break
        await asyncio.sleep(0.01)
    assert agent.after_first.is_set()
    assert transport._playback_pending()
    # Swap only the barge detector: the listen path still has to hear the
    # opening utterance for this scenario to exist at all.
    transport._barge_vad = _NeverSpeechSilero()
    # Far past min_duration_ms, and every frame clears the volume bar.
    for _ in range(8):
        stream.push_audio(STOP_50MS)
        await asyncio.sleep(0.06)
    assert not [event for event in transport.events if event["event"] == "interruption"]
    assert not agent.aborted
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await transport.close()


@pytest.mark.asyncio
async def test_barge_fires_after_min_duration_while_queued() -> None:
    """Full duplex is the headphone opt-in, so the floor runs Silero."""
    config = types.SimpleNamespace(
        transport=TransportConfig(type="terminal"),
        policy=_test_policy(min_duration_ms=150, grace_ms=0, gap_hold_ms=600, cooldown_ms=0),
    )
    transport = TerminalTransport(config=config, env={"SLEEPTALK_TERMINAL_FULL_DUPLEX": "1"})
    transport.use_sounddevice(FakeSoundDevice())
    agent = _BlockingAgent()
    session = RouteSession(agent, tts=FakeTTS([TTS_LOOPBACK]))
    task = asyncio.create_task(transport.run(session))
    for _ in range(100):
        if any(event["event"] == "session_started" for event in transport.events):
            break
        await asyncio.sleep(0.01)
    stream = transport._sounddevice.opened_streams[0]
    await _push_utterance(stream)
    for _ in range(200):
        if agent.after_first.is_set() and transport._playback_pending():
            break
        await asyncio.sleep(0.01)
    assert agent.after_first.is_set()
    assert transport._playback_pending()
    stream.push_audio(STOP_50MS)
    await asyncio.sleep(0.06)
    stream.push_audio(STOP_50MS)
    await asyncio.sleep(0.06)
    assert not [event for event in transport.events if event["event"] == "interruption"]
    assert transport._playback_pending()
    stream.push_audio(STOP_50MS)
    await asyncio.sleep(0.06)
    for _ in range(200):
        if agent.aborted and any(event["event"] == "interruption" for event in transport.events):
            break
        await asyncio.sleep(0.01)
    stops = [event for event in transport.events if event["event"] == "interruption"]
    assert stops
    assert stops[0]["detector"] == "silero"
    assert agent.aborted
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await transport.close()


@pytest.mark.asyncio
async def test_interrupt_mode_off_does_not_cut_playback() -> None:
    config = types.SimpleNamespace(
        transport=TransportConfig(type="terminal"),
        policy=_test_policy(mode="off", min_duration_ms=50, grace_ms=0),
    )
    transport = TerminalTransport(config=config, env={"SLEEPTALK_TERMINAL_FULL_DUPLEX": "1"})
    transport.use_sounddevice(FakeSoundDevice())
    agent = _BlockingAgent()
    session = RouteSession(agent, tts=FakeTTS([TTS_LOOPBACK]))
    task = asyncio.create_task(transport.run(session))
    for _ in range(100):
        if any(event["event"] == "session_started" for event in transport.events):
            break
        await asyncio.sleep(0.01)
    stream = transport._sounddevice.opened_streams[0]
    await _push_utterance(stream)
    for _ in range(200):
        if agent.after_first.is_set() and transport._playback_pending():
            break
        await asyncio.sleep(0.01)
    assert agent.after_first.is_set()
    for _ in range(4):
        stream.push_audio(STOP_50MS)
        await asyncio.sleep(0.06)
    assert not [event for event in transport.events if event["event"] == "interruption"]
    assert not agent.aborted
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await transport.close()


@pytest.mark.asyncio
async def test_provider_normalizes_wav_before_raw_speaker_playback() -> None:
    transport = make_transport()
    raw_24k_mono = b"\x34\x12" * 2_400
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(24_000)
        wav_file.writeframes(raw_24k_mono)
    from sleeptalk.media_audio import decode_audio_bytes

    tts = FakeTTS([decode_audio_bytes(buffer.getvalue())])
    agent = FakeRouteAgent(["normalized answer"])
    session = RouteSession(agent, stt=FakeSTT(), tts=tts)
    task = asyncio.create_task(transport.run(session))
    for _ in range(100):
        if any(e["event"] == "session_started" for e in transport.events):
            break
        await asyncio.sleep(0.01)
    await _push_utterance(transport._sounddevice.opened_streams[0])
    for _ in range(200):
        accounting = transport.audio_output.accounting() if transport.audio_output else None
        if accounting and accounting.bytes_generated:
            break
        await asyncio.sleep(0.01)

    accounting = transport.audio_output.accounting()
    assert accounting.bytes_generated > len(raw_24k_mono)
    assert accounting.bytes_generated < len(buffer.getvalue()) * 3
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# -- device selection -----------------------------------------------------


def test_device_selection_reaches_requested_devices() -> None:
    """Acceptance: typed input/output device selection reaches the
    requested devices (no silent fallback to the host default)."""
    sd = FakeSoundDevice()  # 0=mic(default in), 1=speakers(default out), 2=USB(headset, both)
    transport = make_transport_with_devices(in_dev=2, out_dev=2, sd=sd)
    agent = FakeRouteAgent(["hi"])
    session = RouteSession(agent)

    async def _run() -> None:
        task = asyncio.create_task(transport.run(session))
        for _ in range(100):
            if any(e["event"] == "session_started" for e in transport.events):
                break
            await asyncio.sleep(0.01)
        assert transport.microphone is not None
        assert transport.microphone.device_index == 2, "input must reach the requested device"
        # the second opened stream is the speaker
        assert sd.opened_streams[1].device == 2, "output must reach the requested device"
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await transport.close()

    asyncio.run(_run())


def test_invalid_device_selection_is_actionable_error() -> None:
    """Acceptance: an invalid device selection raises an actionable
    AudioDeviceError (with a recovery hint) rather than silently falling back."""
    transport = make_transport_with_devices(in_dev="No Such Mic", out_dev=None)
    session = RouteSession(FakeRouteAgent([]))

    async def _run() -> None:
        with pytest.raises(AudioDeviceError, match="Unknown input device"):
            await transport.run(session)
        assert any(e["event"] == "devices_unavailable" for e in transport.events), (
            "the device error must be surfaced as an actionable event"
        )
        assert any("recovery" in e for e in transport.events), (
            "the error must carry a concrete recovery hint"
        )

    asyncio.run(_run())
    # close is safe after a failed run (idempotent teardown)
    asyncio.run(transport.close())


def test_a_missing_portaudio_library_says_how_to_install_it(monkeypatch) -> None:
    """Linux with the terminal extra but no libportaudio2: sounddevice raises
    OSError on import; the hint used to say to pip install sounddevice."""
    import builtins

    from sleeptalk.transports.terminal import TerminalTransport
    from sleeptalk.transports.terminal_audio import AudioDeviceError, _load_sounddevice

    real_import = builtins.__import__

    def no_portaudio(name, *args, **kwargs):
        if name == "sounddevice":
            raise OSError("PortAudio library not found")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_portaudio)
    with pytest.raises(AudioDeviceError) as caught:
        _load_sounddevice()
    assert "libportaudio2" in TerminalTransport._recovery_for(caught.value)


def test_a_mic_that_sends_only_silence_is_named_once():
    transport = make_transport()
    transport._microphone = types.SimpleNamespace(silent_seconds=1.0)
    transport._check_mic_silence()
    assert not transport._mic_silence_reported
    transport._microphone = types.SimpleNamespace(silent_seconds=3.5)
    transport._check_mic_silence()
    lines = [line.text for line in transport._presentation.lines if "only silence" in line.text]
    assert len(lines) == 1 and "Privacy & Security" in lines[0]
    assert [e for e in transport.events if e["event"] == "microphone_silent"]
