"""Live captions never commit turns or hold up audio delivery."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from sleeptalk.config import SleeptalkConfig
from sleeptalk.discord_voice.transcripts import LiveTranscripts
from sleeptalk.input_transcripts import input_endpointing_config, input_partial
from sleeptalk.policy import VoicePolicy, local_responsiveness_enabled
from sleeptalk.session_events import Speaker, transcript
from sleeptalk.speech.batch_preview_stt import BatchPreviewSTT
from sleeptalk.providers import ProviderSpec
from sleeptalk.transports.terminal import TerminalTransport
from sleeptalk.voice.configs import TurnCompletionSignal
from sleeptalk.voice_policy_runtime import VoicePolicyRuntime


def session_for(transport="terminal", provider="cartesia"):
    config = SleeptalkConfig.model_validate({
        "transport": {"type": transport}, "stt": {"provider": provider},
    })
    policy = VoicePolicy(local_responsiveness=local_responsiveness_enabled(config))
    return SimpleNamespace(config=config, policy=policy, session_id="caption-test")


@pytest.mark.parametrize("transport", ["web", "terminal", "discord"])
@pytest.mark.parametrize("provider", ["cartesia", "elevenlabs", "deepgram", "local", "venice"])
def test_all_live_cascades_use_fresh_voice_timing(transport, provider):
    session = session_for(transport, provider)
    endpoint = input_endpointing_config(session, session.policy.endpointing_config())
    assert endpoint.local_responsiveness and endpoint.fresh_voice_timing
    assert endpoint.local_streaming_input == (provider == "local")


@pytest.mark.parametrize("provider", ["cartesia", "elevenlabs", "deepgram", "local", "openrouter", "venice"])
def test_terminal_renders_revisions_then_commits_only_final_text(provider):
    session = session_for(provider=provider)
    state = {"text": "Some preview", "source": provider}
    if provider == "local":
        state["source"] = "moonshine_streaming"
    session.turn_completion_signal = lambda _: TurnCompletionSignal(
        transcript=state["text"], source=state["source"],
    ) if provider not in {"openrouter", "venice"} else None
    session.input_transcript_preview = lambda _: state["text"]
    transport = TerminalTransport(session.config, {})
    transport._publish = Mock()
    transport._sync_input_partial(session)
    assert transport._presentation.partial == "Some preview"
    assert not transport._presentation.lines
    count = transport._publish.call_count
    transport._sync_input_partial(session)
    assert transport._publish.call_count == count
    state["text"] = "Some corrected preview"
    transport._sync_input_partial(session)
    assert transport._presentation.partial == state["text"]
    transport._on_session_event(transcript(
        session.session_id, 1, speaker=Speaker.USER, text="Final words.", final=True,
    ))
    assert transport._presentation.partial == ""
    assert transport._presentation.lines[-1].text == "Final words."


def test_wrong_provider_signal_cannot_leak_into_captions():
    session = session_for()
    session.turn_completion_signal = lambda _: TurnCompletionSignal(transcript="old", source="deepgram")
    session.input_transcript_preview = lambda _: "also old"
    assert input_partial(session) == ""


def test_disabled_cascade_policy_does_not_enable_moonshine_endpoint_caps():
    session = session_for(provider="local")
    session.policy.local_responsiveness = False
    endpoint = session.policy.endpointing_config()
    assert input_endpointing_config(session, endpoint) is endpoint
    assert not endpoint.local_streaming_input


@pytest.mark.parametrize("transport", ["terminal", "discord"])
@pytest.mark.parametrize("enabled", [True, False])
async def test_batch_previews_on_other_surfaces_remain_display_only(transport, enabled):
    stt = BatchPreviewSTT(ProviderSpec(provider="openrouter"), api_key="test")
    stt._transcribe_once = AsyncMock(return_value="Preview")
    stt.transcribe_pcm = AsyncMock(return_value="Final transcript")
    session = session_for(transport)
    session.policy.input_previews = enabled
    runtime = VoicePolicyRuntime(agent=None, stt=stt, config=session.config, policy=session.policy)
    pcm = b"\xe8\x03" * 960
    try:
        for _ in range(110):
            await runtime.append_audio(pcm, participant_id=42)
        async with asyncio.timeout(1):
            while stt._preview_tasks:
                await asyncio.sleep(0)
        assert runtime.input_transcript_preview(42) == ("Preview" if enabled else "")
        assert runtime.input_transcript_preview(43) == ""
        assert runtime.turn_completion_signal(42) is None
        result = await runtime.transcribe(pcm * 110, participant_id=42)
        assert result.text == "Final transcript"
        assert runtime.input_transcript_preview(42) == ""
    finally:
        await runtime.close()


async def drain(captions):
    async with asyncio.timeout(1):
        while captions._tasks:
            await asyncio.sleep(0)


async def test_discord_coalesces_revisions_and_edits_the_same_message_for_final():
    message = SimpleNamespace(edit=AsyncMock())
    channel = SimpleNamespace(send=AsyncMock(return_value=message))
    captions = LiveTranscripts(channel, interval=0.01)
    user = SimpleNamespace(id=1, display_name="Alice")
    captions.update(user, "first")
    await drain(captions)
    for n in range(100):
        captions.update(user, f"revision {n}")
    await drain(captions)
    assert channel.send.await_count == 1
    assert message.edit.await_count == 1
    assert message.edit.call_args.kwargs["content"] == "hearing Alice: revision 99"
    caption = captions.seal(user)
    captions.finish(caption, "Final @everyone")
    await drain(captions)
    assert channel.send.await_count == 1
    assert message.edit.call_args.kwargs["content"] == "heard Alice: Final @everyone"
    assert not message.edit.call_args.kwargs["allowed_mentions"].everyone
    assert not captions._active


async def test_discord_slow_send_cannot_mix_speakers_or_overwrite_final_with_partial():
    gate = asyncio.Event()
    messages = []
    async def send(content, **kwargs):
        await gate.wait()
        message = SimpleNamespace(edit=AsyncMock(), initial=content)
        messages.append(message)
        return message
    channel = SimpleNamespace(send=AsyncMock(side_effect=send))
    captions = LiveTranscripts(channel, interval=0)
    alice = SimpleNamespace(id=1, display_name="Alice")
    bob = SimpleNamespace(id=2, display_name="Bob")
    captions.update(alice, "old words")
    await asyncio.sleep(0)
    caption = captions.seal(alice)
    captions.finish(caption, "Correct words")
    captions.update(bob, "Bob's words")
    captions.update(alice, "Next utterance")
    # All updates return while the first Discord send is still blocked.
    assert not messages
    gate.set()
    await drain(captions)
    assert len(messages) == 3
    old = next(m for m in messages if m.initial == "hearing Alice: old words")
    assert old.edit.call_args.kwargs["content"] == "heard Alice: Correct words"
    assert captions._active[1].text == "Next utterance"
    assert captions._active[2].text == "Bob's words"


async def test_discord_captions_bound_content_and_cancel_on_disconnect():
    gate = asyncio.Event()
    channel = SimpleNamespace(send=AsyncMock(side_effect=lambda *a, **k: None))
    async def blocked(*args, **kwargs):
        await gate.wait()
    channel.send.side_effect = blocked
    captions = LiveTranscripts(channel)
    captions.update(SimpleNamespace(id=1, display_name="@everyone"), "x" * 5000)
    await asyncio.sleep(0)
    assert len(channel.send.call_args.args[0]) <= 2000
    captions.close()
    await captions.wait_closed()
    captions.update(SimpleNamespace(id=2, display_name="Bob"), "late words")
    assert not captions._active and not captions._tasks
    assert channel.send.await_count == 1


async def test_discord_delivery_failure_never_retries_each_audio_frame():
    channel = SimpleNamespace(send=AsyncMock(side_effect=TimeoutError))
    captions = LiveTranscripts(channel, interval=0)
    user = SimpleNamespace(id=1, display_name="Alice")
    captions.update(user, "words")
    await drain(captions)
    for _ in range(100):
        captions.update(user, "more words")
    await drain(captions)
    assert channel.send.await_count == 1


@pytest.mark.parametrize("enabled,protected", [(True, False), (False, False), (True, True)])
async def test_discord_capture_wires_optional_partials_and_finalizes_one_message(enabled, protected):
    from sleeptalk.discord.access import DiscordAccess
    from sleeptalk.discord_voice.live_session import PiDiscordLiveSession
    from sleeptalk.routes.base import RouteType

    kernel = session_for("discord")
    kernel.route_type = RouteType.CASCADE
    kernel.subscribe = lambda _: lambda: None
    kernel.append_audio = AsyncMock()
    kernel.turn_completion_signal = lambda _: TurnCompletionSignal(transcript="Hello", source="cartesia")
    bot = SimpleNamespace(
        access=DiscordAccess(required=protected), policy=kernel.policy,
        settings=SimpleNamespace(discord_transcript_debug=enabled),
        realtime_session=kernel, streaming_stt=object(), surface="discord", persona="test",
    )
    voice = SimpleNamespace(is_playing=lambda: False, is_paused=lambda: False)
    message = SimpleNamespace(edit=AsyncMock())
    channel = SimpleNamespace(send=AsyncMock(return_value=message))
    live = PiDiscordLiveSession(bot, voice, channel)
    try:
        assert live.sink._assembler.endpointing_config.fresh_voice_timing
        if protected or not enabled:
            assert live._live_transcripts is None
            return
        live._live_transcripts.interval = 0
        live.turns.observe_pcm = AsyncMock()
        user = SimpleNamespace(id=1, display_name="Alice")
        live.sink.buffers[user.id] = bytearray()
        await live._handle_pcm_frame(user, b"\xe8\x03\xe8\x03" * 960)
        await drain(live._live_transcripts)
        kernel.append_audio.assert_awaited_once()
        assert channel.send.call_args.args[0] == "hearing Alice: Hello"
        async def recognize(_user, _pcm, *, caption):
            live._live_transcripts.finish(caption, "Hello world.")
        live._handle_utterance = recognize
        await live.handle_utterance(user, b"audio")
        await drain(live._live_transcripts)
        assert channel.send.await_count == 1
        assert message.edit.call_args.kwargs["content"] == "heard Alice: Hello world."
        async def no_transcript(_user, _pcm, *, caption):
            pass
        live._handle_utterance = no_transcript
        await live.handle_utterance(user, b"noise")
        await drain(live._live_transcripts)
        assert channel.send.await_count == 1  # no blank messages for ignored noise
        # No more PCM arrives, but the provider publishes a late partial for
        # the still-open next capture. The watch must display it on its own.
        live.sink.users[user.id] = user
        kernel.turn_completion_signal = lambda _: TurnCompletionSignal(
            transcript="Late words", source="cartesia",
        )
        live._caption_watch = asyncio.create_task(live._watch_live_transcripts())
        async with asyncio.timeout(1):
            while channel.send.await_count < 2:
                await asyncio.sleep(0)
        assert channel.send.call_args.args[0] == "hearing Alice: Late words"
    finally:
        if live._caption_watch is not None:
            live._caption_watch.cancel()
            await asyncio.gather(live._caption_watch, return_exceptions=True)
        live.sink.cleanup()
        if live._live_transcripts:
            live._live_transcripts.close()
            await live._live_transcripts.wait_closed()


def test_faster_interruption_preserves_terminal_speaker_grace_and_disabled_mode():
    from sleeptalk.interrupt import AcousticKind, Floor
    from sleeptalk.discord_native.turns import interrupt_from_voice_policy

    session = session_for()
    transport = TerminalTransport(session.config, {})
    for policy in (transport._interrupt_policy(session), interrupt_from_voice_policy(session.policy)):
        for kind, fires in ((AcousticKind.isolated, True), (AcousticKind.mixed, False)):
            floor = Floor(policy, kind)
            floor.observe(0, "turn_start")
            floor.observe(0, "audio_start")
            assert floor.observe(150, "mic_speech").fire is fires
    session.policy.interrupt = replace(session.policy.interrupt, enabled=False)
    policy = interrupt_from_voice_policy(session.policy)
    floor = Floor(policy, AcousticKind.isolated)
    floor.observe(0, "turn_start")
    floor.observe(0, "audio_start")
    assert not floor.observe(1000, "mic_speech").fire
