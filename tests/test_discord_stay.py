"""A Discord bot stays in its voice channel, silent but ready, for days.

Covers the remembered seat (discord/stay.py), rejoining after a drop or a
restart, the listener that restarts itself, and agents that keep their
conversation across restarts.
"""
from __future__ import annotations

import asyncio
import stat
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sleeptalk.discord import stay
from sleeptalk.discord.bot import PiDiscordBot, _end_native_voice
from sleeptalk.discord_voice.live_session import PiDiscordLiveSession


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


# ---- the remembered seat -----------------------------------------------------

def test_a_seat_is_remembered_per_server_privately_and_forgotten(home):
    assert stay.seats(9) == []
    stay.remember(9, stay.Seat(10, 20, 30))
    stay.remember(9, stay.Seat(11, 21))
    stay.remember(9, stay.Seat(10, 22, 30))  # a new /join in the same server moves the seat
    assert stay.seat(9, 10) == stay.Seat(10, 22, 30)
    assert {s.guild_id for s in stay.seats(9)} == {10, 11}
    path = home / ".sleeptalk" / "discord-stay" / "9.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    stay.forget(9, 10)
    assert [s.guild_id for s in stay.seats(9)] == [11]
    stay.forget(9, 11)
    assert not path.exists()
    assert stay.seats(1) == []  # another bot's seats are its own


def test_an_unreadable_seat_file_means_no_seat(home):
    path = home / ".sleeptalk" / "discord-stay" / "9.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json")
    assert stay.seats(9) == []


# ---- rejoining ---------------------------------------------------------------

def _stay_bot(home, *, in_voice: bool = False):
    channel = NS(id=20, name="lounge")
    text = NS(id=30)
    guild = NS(id=10, get_channel=lambda cid: {20: channel, 30: text}.get(cid))
    bot = NS(
        profile=NS(slug="ada", name="Ada"),
        user=NS(id=9),
        voice_clients=[NS(guild=guild)] if in_voice else [],
        native_sessions={},
        _rejoin_tasks={},
        _rejoins={},
        REJOIN_DELAY_SECONDS=0,
        REJOIN_LIMIT=PiDiscordBot.REJOIN_LIMIT,
        REJOIN_WINDOW_SECONDS=PiDiscordBot.REJOIN_WINDOW_SECONDS,
        get_guild=lambda gid: guild if gid == 10 else None,
        is_closed=lambda: False,
        _voice_only_join=AsyncMock(return_value=True),
    )
    for name in ("_bot_id", "_seats", "_remember_seat", "_forget_seat", "_schedule_rejoin", "_rejoin"):
        setattr(bot, name, getattr(PiDiscordBot, name).__get__(bot))
    return bot, guild, channel, text


@pytest.mark.asyncio
async def test_after_a_drop_it_goes_back_to_its_seat_silently(home):
    bot, guild, channel, text = _stay_bot(home)
    stay.remember(9, stay.Seat(10, 20, 30))
    assert await bot._rejoin(10, reason="dropped") is True
    bot._voice_only_join.assert_awaited_once_with(guild, channel, text_channel=text, greet=False)


@pytest.mark.asyncio
async def test_no_seat_no_rejoin_and_already_in_voice_is_left_alone(home):
    bot, *_ = _stay_bot(home)
    assert await bot._rejoin(10, reason="dropped") is False  # /leave forgot it
    bot._voice_only_join.assert_not_awaited()
    bot, *_ = _stay_bot(home, in_voice=True)
    stay.remember(9, stay.Seat(10, 20, 30))
    assert await bot._rejoin(10, reason="dropped") is True
    bot._voice_only_join.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_deleted_channel_is_forgotten(home):
    bot, *_ = _stay_bot(home)
    stay.remember(9, stay.Seat(10, 99))
    assert await bot._rejoin(10, reason="start") is False
    assert stay.seats(9) == []


@pytest.mark.asyncio
async def test_it_stops_rejoining_when_it_keeps_being_dropped(home):
    bot, *_ = _stay_bot(home)
    stay.remember(9, stay.Seat(10, 20, 30))
    for _ in range(PiDiscordBot.REJOIN_LIMIT):
        assert await bot._rejoin(10, reason="dropped") is True
    assert await bot._rejoin(10, reason="dropped") is False
    assert bot._voice_only_join.await_count == PiDiscordBot.REJOIN_LIMIT
    assert stay.seats(9) == []  # /join brings it back


@pytest.mark.asyncio
async def test_on_ready_rejoins_every_remembered_seat(home):
    bot, *_ = _stay_bot(home)
    bot.profile.name = "Ada"
    scheduled = []
    bot._schedule_rejoin = lambda guild_id, *, reason: scheduled.append((guild_id, reason))
    stay.remember(9, stay.Seat(10, 20))
    stay.remember(9, stay.Seat(11, 21))
    with patch("sleeptalk.discord.bot._kick_warmup"):
        await PiDiscordBot.on_ready(bot)
    assert sorted(scheduled) == [(10, "start"), (11, "start")]


@pytest.mark.asyncio
async def test_its_own_voice_ending_schedules_a_rejoin():
    guild = NS(id=10)
    calls = []
    bot = NS(
        profile=NS(discord=NS(guild_id="10")),
        user=NS(id=9),
        _end_native_session=lambda gid: calls.append(("end", gid)),
        _schedule_rejoin=lambda gid, *, reason: calls.append(("rejoin", gid, reason)),
    )
    me = NS(id=9, guild=guild, bot=True)
    await PiDiscordBot.on_voice_state_update(bot, me, NS(channel=NS(id=20)), NS(channel=None))
    assert calls == [("end", 10), ("rejoin", 10, "dropped")]


@pytest.mark.asyncio
async def test_leave_forgets_the_seat_before_disconnecting(home, monkeypatch):
    calls = []
    voice = NS(guild=NS(id=10))

    async def disconnect(**kwargs):
        calls.append("disconnect")

    voice.disconnect = disconnect
    bot = NS(profile=NS(name="Ada"), _forget_seat=lambda gid: calls.append(("forget", gid)),
             _end_native_session=lambda gid: calls.append("end"))
    interaction = NS(guild_id=10, response=NS(defer=AsyncMock()), followup=NS(send=AsyncMock()))
    monkeypatch.setattr("sleeptalk.discord.bot._guard_channel", AsyncMock(return_value=True))
    monkeypatch.setattr("sleeptalk.discord.bot._guild_voice_client", lambda _i: voice)
    await _end_native_voice(bot, interaction)
    assert calls == [("forget", 10), "end", "disconnect"]


@pytest.mark.asyncio
async def test_someone_walking_in_wakes_the_ear_and_speech():
    guild = NS(id=10)
    channel = NS(id=20, members=[])
    voice = NS(guild=guild, channel=channel)
    warmed = []
    bot = NS(
        profile=NS(discord=NS(guild_id="10", voice_only=False)),
        user=NS(id=9),
        voice_clients=[voice],
        _warm_for_arrival=lambda gid: warmed.append(gid),
    )
    person = NS(id=1, guild=guild, bot=False)
    with patch("sleeptalk.discord.bot.audience_allowed", return_value=True):
        await PiDiscordBot.on_voice_state_update(bot, person, NS(channel=None), NS(channel=channel))
        # Already there (muting, say) is not an arrival.
        await PiDiscordBot.on_voice_state_update(bot, person, NS(channel=channel), NS(channel=channel))
    assert warmed == [10]


@pytest.mark.asyncio
async def test_arrival_restarts_a_stopped_ear_and_reopens_speech_connections():
    session = NS(_ensure_listening=AsyncMock(return_value=True))
    realtime = NS(keep_warm=AsyncMock())
    bot = NS(native_sessions={10: session}, realtime_session=realtime)
    PiDiscordBot._warm_for_arrival(bot, 10)
    await asyncio.sleep(0)
    session._ensure_listening.assert_awaited_once()
    realtime.keep_warm.assert_awaited_once()


# ---- the listener restarts itself ----------------------------------------------

def _listening_session(*, connected=True, listening=False, shutting_down=False):
    voice = MagicMock()
    voice.is_connected.return_value = connected
    voice.is_listening.return_value = listening
    session = NS(
        voice=voice, sink=object(), text_channel=NS(send=AsyncMock()), bot=NS(profile=NS(slug="ada")),
        _lifecycle=NS(is_shutting_down=shutting_down),
        LISTEN_FAILURES_BEFORE_NOTE=PiDiscordLiveSession.LISTEN_FAILURES_BEFORE_NOTE,
    )
    session._after_listen = PiDiscordLiveSession._after_listen.__get__(session)
    session._listen_stopped = lambda error: None
    return session


@pytest.mark.asyncio
async def test_a_stopped_receiver_listens_again_while_in_voice():
    session = _listening_session()
    assert await PiDiscordLiveSession._ensure_listening(session) is True
    session.voice.listen.assert_called_once_with(session.sink, after=session._after_listen)
    assert session.listen_restarts == 1


@pytest.mark.asyncio
async def test_while_discord_reconnects_it_waits_then_says_so_once():
    session = _listening_session(connected=False)
    for _ in range(PiDiscordLiveSession.LISTEN_FAILURES_BEFORE_NOTE + 2):
        assert await PiDiscordLiveSession._ensure_listening(session) is False
    session.voice.listen.assert_not_called()
    session.text_channel.send.assert_awaited_once()
    assert "/ada join" in session.text_channel.send.await_args.args[0]


@pytest.mark.asyncio
async def test_a_listening_or_closing_session_is_left_alone():
    session = _listening_session(listening=True)
    assert await PiDiscordLiveSession._ensure_listening(session) is True
    session.voice.listen.assert_not_called()
    session = _listening_session(shutting_down=True)
    assert await PiDiscordLiveSession._ensure_listening(session) is False
    session.voice.listen.assert_not_called()


@pytest.mark.asyncio
async def test_the_receiver_stopping_from_its_thread_is_handled_on_the_loop():
    session = _listening_session()
    session.loop = MagicMock()
    PiDiscordLiveSession._after_listen(session, RuntimeError("socket closed"))
    session.loop.call_soon_threadsafe.assert_called_once()
    session = _listening_session(shutting_down=True)
    session.loop = MagicMock()
    PiDiscordLiveSession._after_listen(session, None)
    session.loop.call_soon_threadsafe.assert_not_called()
