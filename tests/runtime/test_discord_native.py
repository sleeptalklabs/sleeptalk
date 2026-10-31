import asyncio
import sys
from types import SimpleNamespace

import pytest

from sleeptalk.discord_native.dave import NativeDaveDecryptor
from sleeptalk.voice_loop.endpointing import TurnCompletionSignal
from sleeptalk.discord_native.sink import NativeDiscordOpusSink
from sleeptalk.voice_loop.vad import SileroVadConfig, SileroVadGate, VadObservation


def test_dave_decryptor_passthrough_when_dave_is_inactive():
    voice = SimpleNamespace(
        _connection=SimpleNamespace(dave_protocol_version=0, dave_session=None)
    )

    assert NativeDaveDecryptor(voice).decrypt_opus(123, b"opus") == b"opus"


def test_dave_decryptor_calls_ready_session(monkeypatch):
    class FakeDaveSession:
        ready = True

        def decrypt(self, user_id, media_type, packet):
            assert user_id == 123
            assert media_type == "audio"
            assert packet == b"encrypted"
            return b"decrypted"

    monkeypatch.setitem(
        sys.modules,
        "davey",
        SimpleNamespace(MediaType=SimpleNamespace(audio="audio")),
    )
    voice = SimpleNamespace(
        _connection=SimpleNamespace(
            dave_protocol_version=1,
            dave_session=FakeDaveSession(),
        )
    )

    assert NativeDaveDecryptor(voice).decrypt_opus(123, b"encrypted") == b"decrypted"


@pytest.mark.asyncio
async def test_native_opus_sink_decrypts_decodes_and_flushes(monkeypatch):
    utterances = []

    class FakeDecoder:
        def decode(self, packet, *, fec):
            assert packet == b"decrypted"
            assert fec is False
            return b"p" * 8

    class FakeDaveDecryptor:
        def __init__(self, voice_client):
            assert voice_client == "voice"

        def decrypt_opus(self, user_id, opus_frame):
            assert user_id == 123
            assert opus_frame == b"encrypted"
            return b"decrypted"

    async def on_utterance(user, pcm):
        utterances.append((user.id, pcm))

    loop = asyncio.get_running_loop()
    sink = NativeDiscordOpusSink(
        loop=loop,
        min_bytes=8,
        silence_seconds=10,
        on_utterance=on_utterance,
        should_ignore_audio=lambda: False,
        dave_decryptor_factory=FakeDaveDecryptor,
    )
    sink._voice_client = "voice"
    monkeypatch.setattr("sleeptalk.discord_native.sink.Decoder", FakeDecoder)

    user = SimpleNamespace(id=123, bot=False)
    sink.write(user, SimpleNamespace(opus=b"encrypted"))
    await asyncio.sleep(0)
    sink._flush_user_id(123)
    await asyncio.sleep(0)

    assert utterances == [(123, b"p" * 8)]
    assert sink.frames_received == 1
    assert sink.frames_decoded == 1
    assert sink.decrypt_failures == 0
    assert sink.decode_failures == 0


@pytest.mark.asyncio
async def test_native_opus_sink_filters_short_and_bot_audio(monkeypatch):
    utterances = []

    class FakeDecoder:
        def decode(self, packet, *, fec):
            return b"p"

    async def on_utterance(user, pcm):
        utterances.append((user.id, pcm))

    loop = asyncio.get_running_loop()
    sink = NativeDiscordOpusSink(
        loop=loop,
        min_bytes=8,
        silence_seconds=10,
        on_utterance=on_utterance,
        should_ignore_audio=lambda: False,
    )
    monkeypatch.setattr("sleeptalk.discord_native.sink.Decoder", FakeDecoder)

    assert sink.wants_opus() is True
    sink.write(SimpleNamespace(id=1, bot=True), SimpleNamespace(opus=b"encrypted"))
    sink.write(SimpleNamespace(id=2, bot=False), SimpleNamespace(opus=b"encrypted"))
    await asyncio.sleep(0)
    sink._flush_user_id(2)
    await asyncio.sleep(0)

    assert utterances == []


def test_native_opus_sink_ignores_empty_speaking_stop():
    loop = asyncio.new_event_loop()
    try:
        sink = NativeDiscordOpusSink(
            loop=loop,
            min_bytes=8,
            silence_seconds=10,
            on_utterance=lambda _user, _pcm: None,
            should_ignore_audio=lambda: False,
        )

        sink._flush_user(None)
    finally:
        loop.close()


@pytest.mark.asyncio
async def test_native_opus_sink_delays_flush_on_discord_speaking_stop(monkeypatch):
    utterances = []

    class FakeDecoder:
        def decode(self, packet, *, fec):
            return packet

    async def on_utterance(user, pcm):
        utterances.append((user.id, pcm))

    loop = asyncio.get_running_loop()
    sink = NativeDiscordOpusSink(
        loop=loop,
        min_bytes=8,
        silence_seconds=0.08,
        on_utterance=on_utterance,
        should_ignore_audio=lambda: False,
    )
    monkeypatch.setattr("sleeptalk.discord_native.sink.Decoder", FakeDecoder)
    user = SimpleNamespace(id=2, bot=False)

    sink.write(user, SimpleNamespace(opus=b"first---"))
    await asyncio.sleep(0)
    sink.on_voice_member_speaking_stop(user)
    await asyncio.sleep(0)

    assert utterances == []

    sink.write(user, SimpleNamespace(opus=b"second--"))
    await asyncio.sleep(0.1)
    await asyncio.sleep(0)

    assert utterances == [(2, b"first---second--")]


@pytest.mark.asyncio
async def test_native_opus_sink_endpoints_quickly_for_complete_transcript():
    utterances = []
    decisions = []

    async def on_utterance(user, pcm):
        utterances.append((user.id, pcm))

    loop = asyncio.get_running_loop()
    sink = NativeDiscordOpusSink(
        loop=loop,
        min_bytes=8,
        silence_seconds=0.08,
        on_utterance=on_utterance,
        should_ignore_audio=lambda: False,
        turn_completion_provider=lambda _user_id: TurnCompletionSignal(
            transcript="How's it going?",
            source="test",
        ),
        on_endpoint_decision=lambda _user_id, decision: decisions.append(decision),
    )
    user = SimpleNamespace(id=2, bot=False)

    sink._process_rms_pcm(user, b"p" * 8)
    sink.last_audio_at[user.id] = loop.time() - 0.7
    sink._maybe_flush_user_id(user.id)
    await asyncio.sleep(0)

    assert utterances == [(2, b"p" * 8)]
    assert decisions[-1].reason == "complete"
    assert decisions[-1].threshold_ms == 650


@pytest.mark.asyncio
async def test_native_opus_sink_keeps_floor_for_filler_partial():
    utterances = []
    decisions = []

    async def on_utterance(user, pcm):
        utterances.append((user.id, pcm))

    loop = asyncio.get_running_loop()
    sink = NativeDiscordOpusSink(
        loop=loop,
        min_bytes=8,
        silence_seconds=0.08,
        on_utterance=on_utterance,
        should_ignore_audio=lambda: False,
        turn_completion_provider=lambda _user_id: TurnCompletionSignal(
            transcript="I think we should, um",
            source="test",
        ),
        on_endpoint_decision=lambda _user_id, decision: decisions.append(decision),
    )
    user = SimpleNamespace(id=2, bot=False)

    sink._process_rms_pcm(user, b"p" * 8)
    sink.last_audio_at[user.id] = loop.time() - 0.6
    sink._maybe_flush_user_id(user.id)
    await asyncio.sleep(0)

    assert utterances == []
    assert user.id in sink.buffers
    assert decisions[-1].reason == "floor_holder"
    assert decisions[-1].filler_detected is True
    sink._cleanup_in_loop()


@pytest.mark.asyncio
async def test_native_opus_sink_reports_pcm_frames_even_when_utterances_are_ignored(monkeypatch):
    frames = []
    utterances = []

    class FakeDecoder:
        def decode(self, packet, *, fec):
            return b"p" * 8

    async def on_utterance(user, pcm):
        utterances.append((user.id, pcm))

    async def on_pcm_frame(user, pcm):
        frames.append((user.id, pcm))

    loop = asyncio.get_running_loop()
    sink = NativeDiscordOpusSink(
        loop=loop,
        min_bytes=8,
        silence_seconds=10,
        on_utterance=on_utterance,
        should_ignore_audio=lambda: True,
        on_pcm_frame=on_pcm_frame,
    )
    sink._voice_client = None
    monkeypatch.setattr("sleeptalk.discord_native.sink.Decoder", FakeDecoder)

    sink.write(SimpleNamespace(id=2, bot=False), SimpleNamespace(opus=b"encrypted"))
    await asyncio.sleep(0)
    sink._flush_user_id(2)
    await asyncio.sleep(0)

    assert frames == [(2, b"p" * 8)]
    assert utterances == []


@pytest.mark.asyncio
async def test_native_opus_sink_can_filter_pcm_frame_reporting(monkeypatch):
    frames = []

    class FakeDecoder:
        def decode(self, packet, *, fec):
            return b"p" * 8

    async def on_utterance(_user, _pcm):
        return None

    async def on_pcm_frame(user, pcm):
        frames.append((user.id, pcm))

    loop = asyncio.get_running_loop()
    sink = NativeDiscordOpusSink(
        loop=loop,
        min_bytes=8,
        silence_seconds=10,
        on_utterance=on_utterance,
        should_ignore_audio=lambda: True,
        on_pcm_frame=on_pcm_frame,
        should_report_pcm_frame=lambda: False,
    )
    sink._voice_client = None
    monkeypatch.setattr("sleeptalk.discord_native.sink.Decoder", FakeDecoder)

    sink.write(SimpleNamespace(id=2, bot=False), SimpleNamespace(opus=b"encrypted"))
    await asyncio.sleep(0)
    sink._flush_user_id(2)
    await asyncio.sleep(0)

    assert frames == []


@pytest.mark.asyncio
async def test_native_opus_sink_uses_rms_threshold_for_utterance_buffering(monkeypatch):
    utterances = []
    speech_started = []
    packets = [b"\x00\x00" * 4, (1000).to_bytes(2, "little", signed=True) * 4]

    class FakeDecoder:
        def decode(self, packet, *, fec):
            return packets.pop(0)

    async def on_utterance(user, pcm):
        utterances.append((user.id, pcm))

    loop = asyncio.get_running_loop()
    sink = NativeDiscordOpusSink(
        loop=loop,
        min_bytes=8,
        silence_seconds=10,
        on_utterance=on_utterance,
        should_ignore_audio=lambda: False,
        on_speech_started=lambda user: speech_started.append(user.id),
        speech_rms_threshold=500,
    )
    monkeypatch.setattr("sleeptalk.discord_native.sink.Decoder", FakeDecoder)

    user = SimpleNamespace(id=2, bot=False)
    sink.write(user, SimpleNamespace(opus=b"quiet"))
    await asyncio.sleep(0)
    sink._flush_user_id(2)
    await asyncio.sleep(0)

    assert utterances == []
    assert speech_started == []

    sink.write(user, SimpleNamespace(opus=b"speech"))
    await asyncio.sleep(0)
    sink._flush_user_id(2)
    await asyncio.sleep(0)

    assert utterances == [(2, (1000).to_bytes(2, "little", signed=True) * 4)]
    assert speech_started == [2]


@pytest.mark.asyncio
async def test_native_opus_sink_uses_silero_vad_for_utterance_boundaries(monkeypatch):
    utterances = []
    speech_started = []
    observations = [
        VadObservation(is_speech=False, state="QUIET"),
        VadObservation(is_speech=True, state="STARTING"),
        VadObservation(is_speech=True, started=True, state="SPEAKING"),
        VadObservation(is_speech=True, state="STOPPING"),
        VadObservation(is_speech=False, stopped=True, state="QUIET"),
    ]
    packets = [b"quiet---", b"start---", b"speech--", b"tail----", b"stop----"]

    class FakeDecoder:
        def decode(self, packet, *, fec):
            return packets.pop(0)

    class FakeSileroGate:
        def __init__(self, _config):
            pass

        async def observe(self, _pcm):
            return observations.pop(0)

    async def on_utterance(user, pcm):
        utterances.append((user.id, pcm))

    loop = asyncio.get_running_loop()
    sink = NativeDiscordOpusSink(
        loop=loop,
        min_bytes=8,
        silence_seconds=10,
        on_utterance=on_utterance,
        should_ignore_audio=lambda: False,
        on_speech_started=lambda user: speech_started.append(user.id),
        vad_mode="silero",
        silero_vad_config=SileroVadConfig(stop_ms=900),
        silero_vad_factory=FakeSileroGate,
    )
    monkeypatch.setattr("sleeptalk.discord_native.sink.Decoder", FakeDecoder)

    user = SimpleNamespace(id=2, bot=False)
    for packet in range(5):
        sink.write(user, SimpleNamespace(opus=f"packet-{packet}".encode()))
        await asyncio.sleep(0)
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    # Once speech opens the turn, even the stop-decision frame is preserved
    # for fallback STT so a low-energy word release cannot be clipped.
    assert utterances == [(2, b"start---speech--tail----stop----")]
    assert speech_started == [2]


@pytest.mark.asyncio
async def test_native_opus_sink_falls_back_to_rms_when_silero_fails(monkeypatch):
    utterances = []

    class FakeDecoder:
        def decode(self, packet, *, fec):
            return (1000).to_bytes(2, "little", signed=True) * 4

    class BrokenSileroGate:
        def __init__(self, _config):
            pass

        async def observe(self, _pcm):
            raise RuntimeError("model unavailable")

    async def on_utterance(user, pcm):
        utterances.append((user.id, pcm))

    loop = asyncio.get_running_loop()
    sink = NativeDiscordOpusSink(
        loop=loop,
        min_bytes=8,
        silence_seconds=10,
        on_utterance=on_utterance,
        should_ignore_audio=lambda: False,
        speech_rms_threshold=500,
        vad_mode="silero",
        silero_vad_factory=BrokenSileroGate,
    )
    monkeypatch.setattr("sleeptalk.discord_native.sink.Decoder", FakeDecoder)

    user = SimpleNamespace(id=2, bot=False)
    sink.write(user, SimpleNamespace(opus=b"speech"))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    sink._flush_user_id(2)
    await asyncio.sleep(0)

    assert sink.vad_mode == "rms"
    assert sink.vad_failures == 1
    assert utterances == [(2, (1000).to_bytes(2, "little", signed=True) * 4)]


@pytest.mark.asyncio
async def test_silero_vad_gate_runs_direct_onnx_model_on_discord_pcm():
    gate = SileroVadGate(SileroVadConfig())

    observation = await gate.observe(b"\x00\x00" * 1920)

    assert observation.is_speech is False


@pytest.mark.asyncio
async def test_native_opus_sink_collects_barge_user_during_ignore(monkeypatch):
    """Barge-in capture: the interrupting user's audio keeps collecting
    while the global ignore gate is held, and inject_preroll seeds the
    utterance with the moment-of-interruption speech."""
    utterances = []

    class FakeDecoder:
        def decode(self, packet, *, fec):
            return b"p" * 960  # speech-sized frame

    async def on_utterance(user, pcm):
        utterances.append((user.id, pcm))

    loop = asyncio.get_running_loop()
    sink = NativeDiscordOpusSink(
        loop=loop,
        min_bytes=8,
        silence_seconds=10,
        on_utterance=on_utterance,
        should_ignore_audio=lambda: True,  # turn in flight
        collect_during_ignore=lambda user_id: user_id == 7,
        speech_rms_threshold=0,
    )
    monkeypatch.setattr("sleeptalk.discord_native.sink.Decoder", FakeDecoder)

    ignored_user = SimpleNamespace(id=3, bot=False)
    barged_user = SimpleNamespace(id=7, bot=False)
    sink.write(ignored_user, SimpleNamespace(opus=b"encrypted"))
    sink.write(barged_user, SimpleNamespace(opus=b"encrypted"))
    await asyncio.sleep(0)

    assert 3 not in sink.buffers  # still ignored
    assert 7 in sink.buffers  # collected despite the gate

    # Pre-roll seeds the buffer ahead of the live frames.
    sink.inject_preroll(barged_user, b"R" * 960)
    sink._flush_user_id(7)
    await asyncio.sleep(0)
    assert utterances == [(7, b"p" * 960 + b"R" * 960)]

    sink.inject_preroll(barged_user, b"")  # no-op, no crash
