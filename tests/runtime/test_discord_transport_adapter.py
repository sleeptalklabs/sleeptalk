"""Tests for the thin Discord transport adapter.

Covers: Discord transport code never constructs STT/TTS or calls the agent
directly (AST + source search); the guild/channel lifecycle maps to the
session; the transport starts the packaged bot from SleeptalkConfig and
DiscordEnv with no app.* import.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import types
import io
import wave

import pytest

import sleeptalk.transports.discord as discord_transport_mod
from sleeptalk.config import AgentConfig, SleeptalkConfig, STTConfig, TransportConfig, TTSConfig
from sleeptalk.discord import DiscordEnv, PiDiscordBot
from sleeptalk.discord.bot import DiscordSessionKernel
from sleeptalk.discord_voice import DiscordRouteAudioOutput, PiDiscordLiveSession
from sleeptalk.transports.discord import DiscordTransport, transport_summary
from sleeptalk.routes.base import RouteType, TurnOutcome
from sleeptalk.policy import VoicePolicy
from sleeptalk.discord.access import DiscordAccess

OPEN_ACCESS = DiscordAccess(required=False)


def _wav_bytes(pcm: bytes, *, rate: int = 24000) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(pcm)
    return buffer.getvalue()


def _config() -> SleeptalkConfig:
    return SleeptalkConfig(
        transport=TransportConfig(authorization_required=False, 
            token_env="DISCORD_BOT_TOKEN",
            guild_id="1001",
            client_id="client-1",
            allowed_channel_ids="",
            native_voice_enabled=True,
            voice_only=False,
            presence_auto_join=True,
            opus_library="",
        ),
        agent=AgentConfig(
            type="pi",
            name="Campbell",
            slug="campbell",
            provider="pi",
        ),
        stt=STTConfig(rest_timeout_seconds=8.0),
        tts=TTSConfig(provider="cartesia", voice_id="voice-1"),
    )


class FakeConfig:
    """Config-shaped stub for summary tests."""

    class _Section:
        def __init__(self, **kwargs):
            for key, value in kwargs.items():
                setattr(self, key, value)

    def __init__(self):
        self.transport = self._Section(
            token_env="DISCORD_BOT_TOKEN",
            guild_id="1001",
            client_id="client-1",
            allowed_channel_ids="",
            native_voice_enabled=True,
            voice_only=False,
            presence_auto_join=True,
            opus_library="",
        )
        self.agent = self._Section(
            type="pi",
            name="Campbell",
            slug="campbell",
            provider="pi",
            model="",
            gateway_url="",
            gateway_session_id="",
            workspace_dir="",
            pi_bin="",
            persona_instructions="",
            provider_slug="",
            participation_mode="conversationalist",
            addressable_name="",
            proactive_triggers=(),
        )
        self.stt = self._Section(model="nova-3", language="en-US", keyterms="", base_url="")
        self.stt.rest_timeout_seconds = 8.0
        self.tts = self._Section(
            provider="cartesia",
            model="",
            voice_id="voice-1",
            speed=1.0,
            output_format="",
            base_url="",
        )


class IdleRoute:
    def __init__(self):
        self.closes = 0

    def active_turn(self):
        return None

    async def close(self):
        self.closes += 1


def _session(*, route=None, policy=None):
    return types.SimpleNamespace(
        policy=policy,
        agent=object(),
        stt=object(),
        streaming_stt=object(),
        tts=object(),
        tts_fallbacks=[],
        usage_meter=types.SimpleNamespace(finish_session=lambda: None),
        route=route if route is not None else IdleRoute(),
    )


def _production_adapter_source() -> str:
    import sleeptalk.discord.bot as discord_bot_mod

    return "\n".join(
        (
            inspect.getsource(discord_transport_mod),
            inspect.getsource(discord_bot_mod),
        )
    )


def test_complete_production_path_has_no_provider_construction_or_agent_response() -> None:
    source = _production_adapter_source()
    tree = ast.parse(source)
    forbidden_builders = {
        "_build_adapter",
        "build_discord_stt",
        "build_discord_streaming_stt",
        "build_discord_tts",
        "build_streaming_tts",
    }
    called_builders = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in forbidden_builders
    }
    direct_agent_responses = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"respond", "respond_streaming"}
    ]
    assert called_builders == set(), f"Discord path constructs providers/agent: {called_builders}"
    assert direct_agent_responses == [], "Discord path calls the agent directly"


def test_transport_module_never_constructs_providers() -> None:
    source = inspect.getsource(discord_transport_mod)
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    for forbidden in (
        "sleeptalk.speech",
        "sleeptalk.provider_factory",
        "sleeptalk.speech.deepgram",
        "app.custom_agents",
        "app.discord_bot",
        "app.settings",
        "app.pi_discord_bot",
    ):
        assert not any(
            imp == forbidden or imp.startswith(forbidden + ".") for imp in imported
        ), f"transport imports {forbidden!r}"
    for forbidden_call in ("DeepgramSTT(", "CartesiaTTS(", "ElevenLabsTTS(", "build_tts(", "build_stt("):
        assert forbidden_call not in source, f"transport constructs: {forbidden_call}"
    assert ".respond_streaming(" not in source
    assert ".respond(" not in source


def test_bot_receives_realtime_session_not_provider_components() -> None:
    source = inspect.getsource(DiscordTransport.run)
    assert "RealtimeSession(session)" in source
    for forbidden_kwarg in ("agent=session", "stt=session", "tts=session", "route=session"):
        assert forbidden_kwarg not in source


def test_no_server_imports_at_module_scope() -> None:
    tree = ast.parse(inspect.getsource(discord_transport_mod))
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith("app."), (
                f"module-scope server import: {node.module}"
            )
        if isinstance(node, ast.Import):
            assert not any(a.name.startswith("app.") for a in node.names), (
                "module-scope server import"
            )


@pytest.mark.asyncio
async def test_run_maps_transport_lifecycle_to_realtime_session(monkeypatch) -> None:
    bots: list[PiDiscordBot] = []

    async def fake_start(self, token):
        bots.append(self)
        self.started_token = token

    monkeypatch.setattr(discord_transport_mod, "load_opus", lambda _lib: None)
    monkeypatch.setattr(PiDiscordBot, "start", fake_start)

    class Session:
        policy = object()
        agent = object()
        stt = object()
        streaming_stt = object()
        tts = object()
        tts_fallbacks = [object()]
        usage_meter = types.SimpleNamespace(finish_session=lambda: None)
        route = IdleRoute()

    await DiscordTransport(_config(), {"DISCORD_BOT_TOKEN": "tok"}).run(Session())

    bot = bots[-1]
    assert bot.started_token == "tok"
    assert isinstance(bot.env, DiscordEnv)
    assert bot.realtime_session._built.__class__ is Session
    assert bot.realtime_session.route is Session.route
    assert bot.realtime_session.state.value == "closed"
    assert Session.route.closes == 1


@pytest.mark.asyncio
async def test_reference_kernel_drives_join_receive_send_and_leave_through_route() -> None:
    events: list[object] = []

    class Route:
        capabilities = types.SimpleNamespace(route_type=types.SimpleNamespace(value="cascade"))

        def __init__(self):
            self._output = None
            self._active = None

        def bind(self, session_id, emit):
            events.append(("bind", session_id))
            self.emit = emit

        async def prepare(self, session):
            events.append(("join", session.state.value))

        def set_audio_output(self, output):
            self._output = output

        async def run_turn(self, route_input, ctx):
            events.append(("receive", route_input.pcm))
            assert route_input.pcm == b"discord-pcm"
            await self._output.write(_wav_bytes(b"\x01\x00" * 240), ctx.turn_id)
            events.append(("send", ctx.turn_id))
            return TurnOutcome(
                status="completed",
                turn_id=ctx.turn_id,
                played=["route answer"],
                generated=["route answer"],
            )

        def active_turn(self):
            return self._active

        async def cancel_turn(self, turn_id):
            events.append(("cancel", turn_id))

        async def close(self):
            events.append(("leave", None))

    class Playback:
        async def play_source(self, source, **_kwargs):
            events.append(("play", source))
            while source.read():
                await asyncio.sleep(0)
            return types.SimpleNamespace(cancelled=False, completed=True)

        async def stop(self, reason):
            events.append(("playback-stop", reason))

    route = Route()
    built = types.SimpleNamespace(
        policy=object(),
        agent=object(),
        stt=object(),
        streaming_stt=object(),
        tts=object(),
        tts_fallbacks=[],
        usage_meter=None,
        route=route,
    )
    from sleeptalk.session import RealtimeSession

    bot = PiDiscordBot(
        _config(),
        DiscordEnv(token="tok"),
        policy=None,
        realtime_session=RealtimeSession(built),
        stt_rest_timeout_seconds=8.0,
    )
    kernel = bot.kernel
    assert isinstance(kernel, DiscordSessionKernel)
    output = DiscordRouteAudioOutput(Playback())
    await kernel.prepare()
    outcome = await kernel.run_turn(pcm=b"discord-pcm", output=output, turn_id=1)
    await kernel.close()

    assert outcome.generated == ["route answer"]
    assert bot.realtime_session.state.value == "closed"
    assert [event[0] for event in events] == [
        "bind",
        "join",
        "receive",
        "send",
        "play",
        "leave",
    ]


@pytest.mark.asyncio
async def test_hybrid_discord_live_session_uses_canonical_audio_without_standalone_stt() -> None:
    appended = []
    committed = []

    class Realtime:
        route_type = RouteType.HYBRID

        def subscribe(self, callback):
            return lambda: None

        async def append_audio(self, pcm, *, participant_id=0):
            appended.append((pcm, participant_id))

        def turn_completion_signal(self, participant_id=0):
            return None

    class Kernel:
        async def run_turn(self, **kwargs):
            committed.append(kwargs)
            assert "turn_id" not in kwargs
            kwargs["on_started"](41)
            return TurnOutcome(status="completed", turn_id=41)

    class Voice:
        def is_playing(self):
            return False

        def is_paused(self):
            return False

    realtime = Realtime()
    bot = types.SimpleNamespace(
        access=OPEN_ACCESS,
        policy=VoicePolicy(),
        streaming_stt=None,
        realtime_session=realtime,
        kernel=Kernel(),
        surface="hybrid-discord",
        persona="hybrid",
    )
    session = PiDiscordLiveSession(bot, Voice(), None)
    session._new_trace = lambda _user, _pcm: types.SimpleNamespace(
        mark=lambda *_args, **_kwargs: None,
        finish=lambda *_args, **_kwargs: None,
    )
    stereo = b"\x01\x00\x03\x00" * 4
    user = types.SimpleNamespace(id=9)

    await session._append_capture_pcm(user.id, stereo)
    await session._handle_utterance(user, stereo)

    mono = b"\x02\x00" * 4
    assert appended == [(mono, 9)]
    assert committed[0]["pcm"] == mono
    assert committed[0]["owner"] is session
    assert 41 in session._hybrid_traces


@pytest.mark.asyncio
async def test_route_audio_output_keeps_one_pcm_source_for_the_whole_turn() -> None:
    played_sources = []

    class Playback:
        async def play_source(self, source, **_kwargs):
            played_sources.append(source)
            frames = []
            while True:
                frame = source.read()
                if not frame:
                    break
                frames.append(frame)
                await asyncio.sleep(0)
            self.frames = frames
            return types.SimpleNamespace(cancelled=False, completed=True)

        async def stop(self, _reason):
            return True

    playback = Playback()
    output = DiscordRouteAudioOutput(playback)
    first = _wav_bytes(b"\x01\x00" * 240)
    second = _wav_bytes(b"\x02\x00" * 240)

    await output.write(first, 7)
    await output.write(second, 7)
    await output.finish_turn(7)

    assert len(played_sources) == 1
    assert playback.frames
    assert all(len(frame) == 3840 for frame in playback.frames)
    assert output.accounting().turn_id == 7
    assert output.accounting().bytes_written == output.accounting().bytes_generated
    assert output.accounting().chunks == len(playback.frames)


@pytest.mark.asyncio
async def test_route_audio_output_reopens_same_turn_for_detached_specialist_audio() -> None:
    played = []

    class Playback:
        async def play_source(self, source, **_kwargs):
            frames = []
            while True:
                frame = source.read()
                if not frame:
                    break
                frames.append(frame)
                await asyncio.sleep(0)
            played.append(frames)
            return types.SimpleNamespace(cancelled=False, completed=True)

        async def stop(self, _reason):
            return True

    output = DiscordRouteAudioOutput(Playback())

    await output.write(b"\x01\x00" * 240, 4)
    await output.finish_turn(4)
    await output.write(b"\x02\x00" * 240, 4)
    await output.finish_turn(4)

    assert len(played) == 2
    assert all(frames for frames in played)
    assert output.accounting().chunks == 2


@pytest.mark.asyncio
async def test_missing_token_is_clear_error(monkeypatch) -> None:
    monkeypatch.setattr(discord_transport_mod, "load_opus", lambda _lib: None)
    transport = DiscordTransport(_config(), {})
    with pytest.raises(RuntimeError, match="DISCORD_BOT_TOKEN missing"):
        await transport.run(_session())


@pytest.mark.asyncio
async def test_a_rejected_token_is_a_setup_error_that_says_what_to_do(monkeypatch) -> None:
    """A service retried a rejected token every 5 s with only a traceback in its log."""
    import discord
    from sleeptalk.discord_check import DiscordCheckError

    async def rejected(self, token):
        raise discord.LoginFailure("Improper token has been passed.")

    monkeypatch.setattr(discord_transport_mod, "load_opus", lambda _lib: None)
    monkeypatch.setattr(PiDiscordBot, "start", rejected)
    with pytest.raises(DiscordCheckError, match="rejected this bot's token.*Reset Token"):
        await DiscordTransport(_config(), {"DISCORD_BOT_TOKEN": "tok"}).run(_session())


@pytest.mark.asyncio
async def test_transport_runs_without_app(monkeypatch) -> None:
    """The packaged transport constructs PiDiscordBot without importing app.*."""
    import builtins

    real = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name == "app" or name.startswith("app."):
            raise ImportError(f"No module named {name!r} (simulated wheel install)")
        return real(name, *args, **kwargs)

    bots = []

    async def fake_start(self, token):
        bots.append(token)

    monkeypatch.setattr(builtins, "__import__", blocked)
    monkeypatch.setattr(discord_transport_mod, "load_opus", lambda _lib: None)
    monkeypatch.setattr(PiDiscordBot, "start", fake_start)
    await DiscordTransport(_config(), {"DISCORD_BOT_TOKEN": "tok"}).run(_session())
    assert bots == ["tok"]


def test_forbidden_scan_catches_violation() -> None:
    bad = "x = DeepgramSTT(spec, api_key='k')\ny = agent.respond(prompt)"
    for forbidden_call in ("DeepgramSTT(", ".respond("):
        assert forbidden_call in bad, "scan token not detectable in a violating source"
    source = inspect.getsource(discord_transport_mod)
    for forbidden_call in ("DeepgramSTT(", "CartesiaTTS(", ".respond("):
        assert forbidden_call not in source


def test_registry_selects_thin_adapter() -> None:
    from sleeptalk.defaults import register_defaults
    from sleeptalk.registries import TRANSPORTS

    register_defaults()
    registration = TRANSPORTS.get("discord")
    transport = registration.factory(FakeConfig(), {})
    assert isinstance(transport, DiscordTransport)
    assert type(transport).__module__ == "sleeptalk.transports.discord"


def test_legacy_import_path_still_works() -> None:
    from sleeptalk.discord_transport import DiscordTransport as Legacy

    assert Legacy is DiscordTransport


def test_transport_summary_unchanged() -> None:
    summary = transport_summary(FakeConfig())
    assert summary == {
        "guild_id": "1001",
        "voice_only": False,
        "native_voice_enabled": True,
    }


def test_discord_server_integration_required_is_gone() -> None:
    assert not hasattr(discord_transport_mod, "DiscordServerIntegrationRequired")
    assert not hasattr(discord_transport_mod, "resolve_discord_runtime")


@pytest.mark.asyncio
async def test_route_audio_output_plays_narration_clip_inside_the_turn_stream(tmp_path) -> None:
    """Keyboard clacking on Discord rides the turn's own PCM source.

    Going through the playback controller's play_file would stop the answer
    already streaming; through write() the clip queues behind spoken audio
    and is fenced by the same turn cancellation.
    """
    played_sources = []

    class Playback:
        async def play_source(self, source, **_kwargs):
            played_sources.append(source)
            frames = []
            while True:
                frame = source.read()
                if not frame:
                    break
                frames.append(frame)
                await asyncio.sleep(0)
            self.frames = frames
            return types.SimpleNamespace(cancelled=False, completed=True)

        async def play_file(self, *_args, **_kwargs):
            raise AssertionError("clip must not take over the device")

        async def stop(self, _reason):
            return True

    clip = tmp_path / "keyboard.wav"
    clip.write_bytes(_wav_bytes(b"\x03\x00" * 480))
    playback = Playback()
    output = DiscordRouteAudioOutput(playback)

    await output.write(_wav_bytes(b"\x01\x00" * 240), 7)
    assert await output.play_file(clip, turn_id=7) is True
    await output.finish_turn(7)

    assert len(played_sources) == 1
    assert output.accounting().turn_id == 7
    assert output.accounting().chunks == 2


@pytest.mark.asyncio
async def test_route_audio_output_refuses_clip_for_a_cancelled_turn(tmp_path) -> None:
    class Playback:
        async def play_source(self, source, **_kwargs):
            while source.read():
                await asyncio.sleep(0)
            return types.SimpleNamespace(cancelled=False, completed=True)

        async def stop(self, _reason):
            return True

    clip = tmp_path / "keyboard.wav"
    clip.write_bytes(_wav_bytes(b"\x03\x00" * 480))
    output = DiscordRouteAudioOutput(Playback())

    await output.discard_turn(7, "interrupted")

    assert await output.play_file(clip, turn_id=7) is False
    assert await output.play_file(tmp_path / "missing.wav", turn_id=8) is False


@pytest.mark.asyncio
async def test_a_batch_only_listener_can_hold_a_discord_voice_session() -> None:
    """OpenRouter-style STT has no stream: the session transcribes each turn
    whole through the REST path instead of refusing to start."""
    class Voice:
        def is_playing(self):
            return False

        def is_paused(self):
            return False

    def bot(stt):
        return types.SimpleNamespace(
            access=OPEN_ACCESS, policy=VoicePolicy(), streaming_stt=None, stt=stt,
            realtime_session=types.SimpleNamespace(route_type=RouteType.CASCADE, subscribe=lambda _cb: lambda: None,
                                                   turn_completion_signal=lambda participant_id=0: None),
            surface="discord", persona="p",
        )

    session = PiDiscordLiveSession(bot(types.SimpleNamespace(transcribe_pcm=None)), Voice(), None)
    assert session.streaming_stt is None
    with pytest.raises(RuntimeError, match="STT"):
        PiDiscordLiveSession(bot(None), Voice(), None)


@pytest.mark.asyncio
async def test_a_discord_pause_brings_an_early_transcript_to_judge_the_turn() -> None:
    """Discord, synthetic (OpenRouter listening): with no pause hook the sink
    had no text to judge by, so every short utterance ended on 120 ms of quiet
    and "I'd like to know about | the history of tea" was two turns."""
    class Voice:
        def is_playing(self):
            return False

        def is_paused(self):
            return False

    asked: list[tuple[bytes, int]] = []
    loop = asyncio.get_running_loop()

    def transcribe_ahead(pcm, *, participant_id=0):
        asked.append((pcm, participant_id))
        future = loop.create_future()
        future.set_result("I'd like to know about")
        return future

    session_side = types.SimpleNamespace(
        route_type=RouteType.CASCADE, subscribe=lambda _cb: lambda: None,
        turn_completion_signal=lambda participant_id=0: None,
        config=types.SimpleNamespace(stt=types.SimpleNamespace(pause_ahead_ms=300)),
        listens_ahead=lambda: True, transcribe_ahead=transcribe_ahead,
    )
    bot = types.SimpleNamespace(
        access=OPEN_ACCESS, policy=VoicePolicy(), streaming_stt=None,
        stt=types.SimpleNamespace(transcribe_pcm=None), realtime_session=session_side,
        surface="discord", persona="p",
    )
    session = PiDiscordLiveSession(bot, Voice(), None)
    assembler = session.sink._assembler
    assert assembler.pause_ms == 300 and assembler.on_pause is not None
    assert assembler.early_transcripts() is True

    stereo = b"\x01\x00\x01\x00" * 480  # 10 ms of 48 kHz stereo
    session._on_speech_pause(42, stereo)
    assert asked == [(b"\x01\x00" * 480, 42)]  # mono, the speaker's own participant
