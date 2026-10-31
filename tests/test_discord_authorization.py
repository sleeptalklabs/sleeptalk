import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("discord")

from sleeptalk.config import TransportConfig
from sleeptalk.discord.access import DiscordAccess, audience_allowed, speaker_allowed
from sleeptalk.discord.bot import PiDiscordBot, _guard_channel
from sleeptalk.discord.env import DiscordSurface
from sleeptalk.discord.runtime_lease import acquire_bot_lease, release_bot_lease
from sleeptalk.discord_check import DiscordCheckError
from sleeptalk.discord_native.playback import AuthorizedAudioSource
from sleeptalk.discord_native.sink import NativeDiscordOpusSink
from sleeptalk.discord_voice.live_session import PiDiscordLiveSession


def fixture_bot():
    access = DiscordAccess(required=True, guild_id=10, channels=frozenset({20}),
                           operators=frozenset({1}), speakers=frozenset({2}), listeners=frozenset({3}))
    guild = NS(id=10)
    voice = NS(channel=NS(id=20, guild=guild, members=[NS(id=1), NS(id=9)]), guild=guild)
    return NS(access=access, user=NS(id=9), session_route=None), voice


def test_strict_config_requires_trusted_operator_guild_and_channel():
    assert TransportConfig().presence_auto_join is False
    for values in ({}, {"guild_id": "10"}, {"guild_id": "10", "operator_ids": "1"}):
        policy = DiscordAccess.from_surface(DiscordSurface(authorization_required=True, **values))
        with pytest.raises(ValueError, match="Automatic pairing is unavailable"):
            policy.validate()
    DiscordAccess.from_surface(DiscordSurface(authorization_required=True, guild_id="10", operator_ids="1", allowed_channel_ids="20")).validate()


def test_operator_speaker_listener_and_bots_have_distinct_access():
    bot, voice = fixture_bot()
    assert audience_allowed(bot, voice)
    assert speaker_allowed(bot, voice, NS(id=1))
    assert speaker_allowed(bot, voice, NS(id=2))
    assert not speaker_allowed(bot, voice, NS(id=3))
    assert not speaker_allowed(bot, voice, NS(id=4))
    voice.channel.members += [NS(id=2), NS(id=3)]
    assert audience_allowed(bot, voice)
    voice.channel.members.append(NS(id=4, bot=True))
    assert not audience_allowed(bot, voice)
    assert not speaker_allowed(bot, voice, NS(id=1))


def test_only_operator_can_answer_pending_permission():
    bot, voice = fixture_bot()
    bot.session_route = NS(_pending_permission=object())
    assert speaker_allowed(bot, voice, NS(id=1))
    assert not speaker_allowed(bot, voice, NS(id=2))


@pytest.mark.asyncio
@pytest.mark.parametrize("user_id,guild_id,channel_id,allowed", [(1,10,20,True), (2,10,20,False), (1,11,20,False), (1,10,21,False)])
async def test_slash_authorization_is_ephemeral(user_id, guild_id, channel_id, allowed):
    bot, _ = fixture_bot()
    response = NS(is_done=lambda: False, send_message=AsyncMock())
    interaction = NS(user=NS(id=user_id), guild_id=guild_id, channel_id=channel_id, response=response)
    assert await _guard_channel(bot, interaction) is allowed
    if not allowed:
        assert response.send_message.await_args.kwargs == {"ephemeral": True}


@pytest.mark.asyncio
@pytest.mark.parametrize("user_id,sitting_in,allowed,says", [
    (1, 20, True, ""),                    # owner in the locked voice channel, typing in #general
    (1, 21, False, "<#20>"),              # owner in another voice channel
    (1, None, False, "<#20>"),            # owner not in voice at all
    (2, 20, False, "your Discord user ID"),  # someone else, even in the right channel
])
async def test_the_locked_voice_channel_counts_wherever_the_command_is_typed(user_id, sitting_in, allowed, says):
    bot, _ = fixture_bot()
    response = NS(is_done=lambda: False, send_message=AsyncMock())
    voice = NS(channel=NS(id=sitting_in)) if sitting_in else None
    interaction = NS(user=NS(id=user_id, voice=voice), guild_id=10, channel_id=99, response=response)
    assert await _guard_channel(bot, interaction) is allowed
    if not allowed:
        assert says in response.send_message.await_args.args[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure,says", [("hang", "UDP"), ("boom", "Could not join voice")])
async def test_join_always_answers_instead_of_thinking_forever(monkeypatch, failure, says):
    """A voice connect that hangs, or fails in a way nobody expected, still gets
    a reply: Discord otherwise shows "is thinking..." forever."""
    from sleeptalk.discord import bot as bot_module

    bot, _ = fixture_bot()
    bot.profile = NS(name="Bot", discord=NS(native_voice_enabled=True))
    bot.native_sessions = {}
    monkeypatch.setattr(bot_module, "has_native_dave", lambda: True)
    monkeypatch.setattr(bot_module, "VOICE_CONNECT_SECONDS", 0.05)
    monkeypatch.setattr(bot_module, "_guild_voice_client", lambda interaction: None)

    async def connect(*args, **kwargs):
        if failure == "hang":
            await asyncio.sleep(10)
        raise KeyError("unexpected")
    monkeypatch.setattr(bot_module, "_connected_voice_client", connect)
    followup = NS(send=AsyncMock())
    interaction = NS(user=NS(id=1, voice=NS(channel=NS(id=20))), guild_id=10, channel_id=20,
                     response=NS(defer=AsyncMock(), is_done=lambda: True), followup=followup)
    await bot_module._start_native_voice(bot, interaction)
    assert says in followup.send.await_args.args[0]


@pytest.mark.asyncio
async def test_listener_arrival_stops_player_before_disconnect():
    bot, voice = fixture_bot()
    calls = []
    intruder = NS(id=4, guild=voice.guild, bot=False)
    voice.channel.members.append(intruder)
    voice.stop = lambda: calls.append("stop")
    async def disconnect(**kwargs):
        calls.append("disconnect")
    voice.disconnect = disconnect
    bot.profile = NS(discord=NS(guild_id="10", voice_only=False))
    bot.voice_clients = [voice]
    bot._end_native_session = lambda guild_id: calls.append("end")
    # An intruder ends the stay too: the bot doesn't come back to a room it left for safety.
    bot._forget_seat = lambda guild_id: calls.append("forget")
    await PiDiscordBot.on_voice_state_update(bot, intruder, NS(channel=None), NS(channel=voice.channel))
    assert calls == ["stop", "forget", "end", "disconnect"]


@pytest.mark.asyncio
async def test_autojoin_ignores_unauthorized_operator():
    bot, voice = fixture_bot()
    bot.profile = NS(discord=NS(guild_id="10", voice_only=True, presence_auto_join=True))
    bot.voice_clients = []
    bot._voice_only_join = AsyncMock()
    await PiDiscordBot.on_voice_state_update(bot, NS(id=2, guild=voice.guild, bot=False), NS(channel=None), NS(channel=voice.channel))
    bot._voice_only_join.assert_not_awaited()


@pytest.mark.asyncio
async def test_ingestion_rejects_unauthorized_speech_before_stt():
    bot, voice = fixture_bot()
    session = NS(bot=bot, voice=voice)
    await PiDiscordLiveSession.handle_utterance(session, NS(id=4), b"private audio")
    await PiDiscordLiveSession._handle_utterance(session, NS(id=4), b"private audio")
    await PiDiscordLiveSession._handle_pcm_frame(session, NS(id=4), b"private audio")


def test_sink_rejects_before_decode():
    dropped = []
    sink = NS(authorized=lambda user: False, _assembler=NS(drop=dropped.append))
    NativeDiscordOpusSink._process_opus(sink, NS(id=4), b"opus")
    assert dropped == [4]


def test_playback_revocation_is_permanent_even_if_listener_leaves():
    bot, voice = fixture_bot()
    raw = NS(read=lambda: b"workspace reply", is_opus=lambda: False, cleanup=lambda: None)
    source = AuthorizedAudioSource(raw, lambda: audience_allowed(bot, voice))
    assert source.read() == b"workspace reply"
    voice.channel.members.append(NS(id=4))
    assert source.read() == b""
    voice.channel.members.pop()
    assert source.read() == b""


def test_playback_rechecks_after_blocking_source_read():
    bot, voice = fixture_bot()
    def read():
        voice.channel.members.append(NS(id=4))
        return b"workspace reply"
    source = AuthorizedAudioSource(NS(read=read, cleanup=lambda: None), lambda: audience_allowed(bot, voice))
    assert source.read() == b""


def test_bot_runtime_lease_conflicts_across_profiles_and_recovers(tmp_path):
    first = acquire_bot_lease(9, root=tmp_path)
    try:
        with pytest.raises(DiscordCheckError, match="discord_runtime_conflict"):
            acquire_bot_lease(9, root=tmp_path)
        other = acquire_bot_lease(10, root=tmp_path)
        release_bot_lease(other)
    finally:
        release_bot_lease(first)
    restarted = acquire_bot_lease(9, root=tmp_path)
    release_bot_lease(restarted)


@pytest.mark.asyncio
async def test_delayed_permission_request_cannot_be_answered_by_nonoperator():
    from sleeptalk.routes.hybrid import HybridRoute

    route = HybridRoute.__new__(HybridRoute)
    route._pending_permission = object()
    ctx = NS(extras={"permission_authorized": False})
    result = await HybridRoute._resolve_permission(route, None, ctx)
    assert result == "Only an authorized operator can answer this permission request."
    assert route._pending_permission is not None


def test_a_profile_that_says_nothing_is_closed():
    from sleeptalk.config import TransportConfig

    assert DiscordAccess.from_surface(TransportConfig()).required
    assert DiscordAccess.from_surface(object()).required


def test_open_mode_still_limits_the_voice_channels_it_joins():
    access = DiscordAccess(required=False, channels=frozenset({20}))
    assert access.operator(12345)
    assert access.audience(NS(id=20, members=[NS(id=5)]), bot_id=9)
    assert not access.audience(NS(id=21, members=[NS(id=5)]), bot_id=9)


async def _deliver_echo(**message):
    seen = []
    session = NS(text_channel=NS(id=30), ingest_room_echo=lambda *a, **k: seen.append(a))
    bot = NS(
        access=DiscordAccess(required=False), user=NS(id=9), native_sessions={1: session},
        profile=NS(discord=NS(voice_only=False)),
    )
    defaults = dict(guild=NS(id=1), author=NS(id=5, bot=True), webhook_id=None,
                    channel=NS(id=30), content="echo", id=77)
    await PiDiscordBot.on_message(bot, NS(**{**defaults, **message}))
    return seen


async def test_room_echo_only_from_a_bot_in_the_session_chat():
    assert await _deliver_echo()
    assert not await _deliver_echo(webhook_id=123)
    assert not await _deliver_echo(channel=NS(id=31))
