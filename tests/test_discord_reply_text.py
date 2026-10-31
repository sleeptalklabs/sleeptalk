"""The spoken call, written into the voice channel's own chat.

One message per finished agent turn, built from the same session events the
web call page's keyed transcript renders. Discord I/O never holds up a turn,
never posts into a channel wider than the call, and never pings anyone.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from sleeptalk.config import SleeptalkConfig
from sleeptalk.discord.access import DiscordAccess
from sleeptalk.discord.session_helpers import parse_voice_echo
from sleeptalk.discord_voice.reply_text import (
    MESSAGE_LIMIT,
    ReplyFeed,
    split_message,
)
from sleeptalk.policy import VoicePolicy, local_responsiveness_enabled
from sleeptalk.session_events import (
    SessionEventType,
    Speaker,
    agent_event,
    response,
    transcript,
)

SESSION = "written-call"


def feed_for(channel, **kwargs) -> ReplyFeed:
    return ReplyFeed(channel, name="Campbell", **kwargs)


def channel_spy(**kwargs):
    return SimpleNamespace(send=AsyncMock(**kwargs))


def posted(channel) -> list[str]:
    return [call.args[0] for call in channel.send.await_args_list]


async def drain(feed: ReplyFeed) -> None:
    async with asyncio.timeout(1):
        while feed._task is not None and not feed._task.done():
            await asyncio.sleep(0)
        if feed._task is not None:
            await asyncio.gather(feed._task, return_exceptions=True)


def say(feed: ReplyFeed, turn: int, *sentences: str, heard: str = "", end=None) -> None:
    """One turn's worth of events, in the order the cascade route emits them."""
    if heard:
        feed.speaking("Alice", heard)
        feed.handle(transcript(
            SESSION, turn, speaker=Speaker.USER, text=heard, final=True,
        ))
    feed.handle(response(SESSION, turn, kind=SessionEventType.RESPONSE_STARTED))
    for sentence in sentences:
        feed.handle(agent_event(SESSION, turn, "sentence", text=sentence, spaced=True))
    if sentences:
        # The route's closing rollup: the whole reply again, after each
        # sentence already went out on its own.
        feed.handle(transcript(
            SESSION, turn, speaker=Speaker.ASSISTANT,
            text=" ".join(sentences), final=True, rollup=True,
        ))
    feed.handle(response(
        SESSION, turn, kind=end or SessionEventType.RESPONSE_COMPLETED,
    ))


# -- what reaches the channel -------------------------------------------


async def test_one_message_per_turn_without_repeating_the_rollup():
    channel = channel_spy()
    feed = feed_for(channel)
    say(feed, 1, "Three files changed.", "The test suite is green.", heard="What changed?")
    await drain(feed)
    # One message, the reply's sentences as the page's rows show them.
    assert posted(channel) == [
        "Three files changed. The test suite is green."
    ]
    say(feed, 2, "Pushed.", heard="Push it.")
    await drain(feed)
    assert len(posted(channel)) == 2


async def test_the_callers_words_are_opt_in():
    quiet, loud = channel_spy(), channel_spy()
    for channel, caller_speech in ((quiet, False), (loud, True)):
        feed = feed_for(channel, caller_speech=caller_speech)
        say(feed, 1, "Three files changed.", heard="What changed?")
        await drain(feed)
    assert posted(quiet) == ["Three files changed."]
    assert posted(loud) == [
        "**Alice:** What changed?\n**Campbell:** Three files changed."
    ]


async def test_the_prompt_the_route_sees_is_never_what_the_channel_shows():
    # Discord hands the route a prompt: room context, or a system note about
    # a lost encryption handshake. The channel shows the words that were said.
    channel = channel_spy()
    feed = feed_for(channel, caller_speech=True)
    feed.speaking("Alice", "What changed?")
    feed.handle(transcript(
        SESSION, 1, speaker=Speaker.USER, final=True,
        text="[system note — not a literal user utterance] ... What changed?",
    ))
    feed.handle(agent_event(SESSION, 1, "sentence", text="Three files.", spaced=True))
    feed.handle(response(SESSION, 1, kind=SessionEventType.RESPONSE_COMPLETED))
    await drain(feed)
    assert posted(channel) == ["**Alice:** What changed?\n**Campbell:** Three files."]


async def test_a_turn_that_said_nothing_posts_nothing():
    channel = channel_spy()
    feed = feed_for(channel, caller_speech=True)
    say(feed, 1, heard="mm")  # noise that never became an answer
    feed.handle(agent_event(SESSION, 2, "thinking"))
    feed.handle(response(SESSION, 2, kind=SessionEventType.RESPONSE_COMPLETED))
    await drain(feed)
    assert channel.send.await_count == 0


async def test_an_interrupted_answer_is_posted_and_marked_where_it_stopped():
    channel = channel_spy()
    feed = feed_for(channel)
    say(
        feed, 1, "I was halfway through",
        heard="Tell me about it",
        end=SessionEventType.RESPONSE_CANCELLED,
    )
    await drain(feed)
    assert posted(channel) == ["I was halfway through—"]


async def test_a_reply_the_bot_speaks_on_its_own_still_reaches_the_channel():
    # A background job's announcement: no caller, no Discord turn to return
    # text. It is on the event stream, so it is in the channel.
    channel = channel_spy()
    feed = feed_for(channel)
    feed.handle(agent_event(
        SESSION, 7, "sentence", text="Your deploy finished.", spaced=True,
    ))
    feed.handle(response(SESSION, 7, kind=SessionEventType.RESPONSE_COMPLETED))
    await drain(feed)
    assert posted(channel) == ["Your deploy finished."]


async def test_a_reply_can_never_ping_the_channel():
    channel = channel_spy()
    feed = feed_for(channel)
    say(feed, 1, "Done, @everyone.", heard="Say it")
    await drain(feed)
    mentions = channel.send.await_args.kwargs["allowed_mentions"]
    assert not mentions.everyone and not mentions.roles and not mentions.users


# -- Discord's 2000-character ceiling ------------------------------------


def test_long_replies_split_on_the_widest_break_within_the_limit():
    paragraphs = "\n".join(f"Paragraph {n} " + "filler words " * 60 for n in range(8))
    chunks = split_message(paragraphs)
    assert len(chunks) > 1
    assert all(len(chunk) <= MESSAGE_LIMIT for chunk in chunks)
    assert all(chunk.startswith("Paragraph") for chunk in chunks)
    assert " ".join(chunks).split() == paragraphs.split()


def test_a_sentence_break_is_used_when_there_is_no_line_break():
    chunks = split_message("Alpha beta. " * 40, limit=100)
    assert all(len(chunk) <= 100 for chunk in chunks)
    assert all(chunk.endswith(".") for chunk in chunks)


def test_a_word_longer_than_a_message_is_cut_rather_than_dropped():
    chunks = split_message("x" * 4500)
    assert [len(chunk) for chunk in chunks] == [2000, 2000, 500]


async def test_a_reply_over_the_limit_becomes_several_messages():
    channel = channel_spy()
    feed = feed_for(channel)
    say(feed, 1, "Sentence number one. " * 400, heard="Explain everything")
    await drain(feed)
    sent = posted(channel)
    assert len(sent) > 1
    assert all(len(message) <= MESSAGE_LIMIT for message in sent)
    assert sent[0].startswith("Sentence number one.")


async def test_the_echo_marker_turn_stays_one_parseable_message():
    # An open profile's post is read back by a peer bot in the same room,
    # which needs the whole turn in one message it can parse.
    channel = channel_spy()
    feed = feed_for(channel, caller_speech=True, echo_marker=True)
    say(feed, 1, "A long answer. " * 400, heard="Go on")
    await drain(feed)
    sent = posted(channel)
    assert len(sent) == 1
    echo = parse_voice_echo(sent[0])
    assert echo is not None
    assert echo.human_name == "Alice" and echo.human_text == "Go on"
    assert echo.bot_name == "Campbell" and echo.bot_text.startswith("A long answer.")


# -- never in the way of the microphone ---------------------------------


async def test_folding_events_returns_while_a_send_is_still_blocked():
    gate = asyncio.Event()

    async def blocked(*args, **kwargs):
        await gate.wait()

    channel = channel_spy(side_effect=blocked)
    feed = feed_for(channel)
    say(feed, 1, "First answer.", heard="One")
    await asyncio.sleep(0)
    # Four more turns are heard, folded and queued while Discord is stuck.
    for turn in range(2, 6):
        say(feed, turn, f"Answer {turn}.", heard=str(turn))
    assert channel.send.await_count == 1  # one in flight, the rest waiting
    assert len(feed._queue) == 4
    gate.set()
    await drain(feed)
    assert posted(channel)[-1] == "Answer 5."


async def test_a_channel_that_cannot_take_messages_stops_being_tried():
    channel = channel_spy(side_effect=discord.HTTPException(
        SimpleNamespace(status=403, reason="Forbidden"), "Missing Permissions"
    ))
    feed = feed_for(channel)
    for turn in range(1, 41):
        say(feed, turn, f"Answer {turn}.", heard=str(turn))
        await drain(feed)
    assert channel.send.await_count == 3
    assert not feed._queue


async def test_a_refused_message_says_why_in_the_log(caplog):
    """A call that is never written must leave Discord's reason in a log
    kept at the default level, once, not once per turn."""
    channel = channel_spy(side_effect=discord.HTTPException(
        SimpleNamespace(status=403, reason="Forbidden"), "Missing Permissions"
    ))
    feed = feed_for(channel)
    with caplog.at_level("INFO", logger="sleeptalk.discord_voice.reply_text"):
        for turn in range(1, 3):
            say(feed, turn, f"Answer {turn}.")
            await drain(feed)
    reasons = [record.getMessage() for record in caplog.records if "not sent" in record.getMessage()]
    assert reasons == ["Call text not sent: 403 HTTPException (0): Missing Permissions"]


async def test_the_first_message_posted_is_logged_once(caplog):
    channel = channel_spy()
    feed = feed_for(channel)
    with caplog.at_level("INFO", logger="sleeptalk.discord_voice.reply_text"):
        for turn in range(1, 4):
            say(feed, turn, f"Answer {turn}.")
            await drain(feed)
    assert [r.getMessage() for r in caplog.records].count("Call text: first message posted") == 1


async def test_a_slow_channel_cannot_grow_without_bound_over_a_long_call():
    gate = asyncio.Event()

    async def blocked(*args, **kwargs):
        await gate.wait()

    channel = channel_spy(side_effect=blocked)
    feed = feed_for(channel)
    for turn in range(1, 300):
        say(feed, turn, f"Answer {turn}.", heard=str(turn))
    assert len(feed._queue) <= 32
    assert len(feed._turns) <= 8
    gate.set()
    feed.close()
    await feed.wait_closed()


async def test_a_turn_that_never_ends_cannot_pile_up():
    feed = feed_for(channel_spy())
    for turn in range(1, 200):
        feed.handle(response(SESSION, turn, kind=SessionEventType.RESPONSE_STARTED))
    assert len(feed._turns) <= 8


async def test_losing_access_mid_call_drops_the_queued_text():
    gate, allowed = asyncio.Event(), {"ok": True}

    async def blocked(*args, **kwargs):
        await gate.wait()

    channel = channel_spy(side_effect=blocked)
    feed = feed_for(channel, authorized=lambda: allowed["ok"])
    say(feed, 1, "Secret answer.", heard="Tell me")
    await asyncio.sleep(0)
    allowed["ok"] = False
    say(feed, 2, "Another answer.", heard="And?")
    gate.set()
    await drain(feed)
    assert channel.send.await_count == 1  # the one already in flight
    assert not feed._queue


async def test_closing_cancels_delivery_and_accepts_no_more_events():
    gate = asyncio.Event()

    async def blocked(*args, **kwargs):
        await gate.wait()

    channel = channel_spy(side_effect=blocked)
    feed = feed_for(channel)
    say(feed, 1, "First answer.", heard="One")
    await asyncio.sleep(0)
    feed.close()
    await feed.wait_closed()
    say(feed, 2, "Later answer.", heard="Two")
    assert not feed._queue and not feed._turns
    assert channel.send.await_count == 1


# -- the session wiring --------------------------------------------------


def live_session(*, protected=True, post_replies=True, post_caller_speech=False,
                 text_channel=None, shown_as=None):
    from sleeptalk.discord_voice.live_session import PiDiscordLiveSession
    from sleeptalk.routes.base import RouteType

    config = SleeptalkConfig.model_validate({"transport": {"type": "discord"}})
    kernel = SimpleNamespace(
        config=config,
        policy=VoicePolicy(local_responsiveness=local_responsiveness_enabled(config)),
        session_id=SESSION,
        route_type=RouteType.CASCADE,
        subscribe=lambda handler: lambda: None,
        append_audio=AsyncMock(),
        turn_completion_signal=lambda _: None,
        input_transcript_preview=lambda _: "",
    )
    # The voice channel's own chat, with an audience the profile authorizes.
    voice_chat = channel_spy()
    voice_chat.id = 10
    voice_chat.guild = SimpleNamespace(id=1)
    if shown_as:
        voice_chat.guild.me = SimpleNamespace(display_name=shown_as)
    voice_chat.members = [SimpleNamespace(id=7)]
    bot = SimpleNamespace(
        access=DiscordAccess(
            required=protected,
            guild_id=1,
            channels=frozenset({10}),
            operators=frozenset({7}),
        ),
        policy=kernel.policy,
        settings=SimpleNamespace(
            discord_transcript_debug=False, discord_voice_text_echo=True,
        ),
        realtime_session=kernel,
        streaming_stt=object(),
        surface="discord",
        persona="test",
        profile=SimpleNamespace(
            name="Campbell",
            slug="campbell",
            discord=SimpleNamespace(
                post_replies=post_replies,
                post_caller_speech=post_caller_speech,
            ),
        ),
    )
    voice = SimpleNamespace(
        is_playing=lambda: False, is_paused=lambda: False, channel=voice_chat,
    )
    live = PiDiscordLiveSession(bot, voice, text_channel)
    return live, voice_chat


async def test_an_authorized_call_writes_into_the_voice_channels_own_chat():
    server_wide = channel_spy()
    live, voice_chat = live_session(protected=True, text_channel=server_wide)
    try:
        # /join typed in a server channel must not publish the call there:
        # only the voice channel's membership is known to be authorized.
        assert live.text_channel is voice_chat
        assert live._reply_feed is not None
        assert live._reply_feed.channel is voice_chat
        assert not live._reply_feed.echo_marker
        assert not live._reply_feed.caller_speech
        say(live._reply_feed, 1, "Three files changed.", heard="What changed?")
        await drain(live._reply_feed)
        assert posted(voice_chat) == ["Three files changed."]
        assert server_wide.send.await_count == 0
    finally:
        live.sink.cleanup()


async def test_an_unauthorized_listener_keeps_the_call_out_of_the_chat():
    live, voice_chat = live_session(protected=True)
    try:
        # Someone the profile never authorized walks into the voice channel.
        voice_chat.members.append(SimpleNamespace(id=404))
        say(live._reply_feed, 1, "Three files changed.", heard="What changed?")
        await drain(live._reply_feed)
        assert voice_chat.send.await_count == 0
    finally:
        live.sink.cleanup()


async def test_an_open_profile_keeps_its_channel_and_its_parseable_echo():
    server_wide = channel_spy()
    live, _ = live_session(protected=False, text_channel=server_wide)
    try:
        assert live.text_channel is server_wide
        assert live._reply_feed.echo_marker
    finally:
        live.sink.cleanup()


@pytest.mark.parametrize("post_replies", [True, False])
async def test_post_replies_is_the_switch(post_replies):
    live, _ = live_session(post_replies=post_replies)
    try:
        assert (live._reply_feed is not None) is post_replies
    finally:
        live.sink.cleanup()


async def test_a_reply_is_labelled_with_the_bots_name_in_the_server():
    """With the caller's words shown, the reply is labelled with the name
    Discord shows for the bot, not the profile's file name ("local-koko").
    An open profile keeps the profile name its peers parse."""
    live, voice_chat = live_session(post_caller_speech=True, shown_as="kokoro-local")
    try:
        say(live._reply_feed, 1, "Going well, thanks.", heard="How is your day going?")
        await drain(live._reply_feed)
        assert posted(voice_chat) == ["**Alice:** How is your day going?\n**kokoro-local:** Going well, thanks."]
    finally:
        live.sink.cleanup()
    live, _ = live_session(protected=False, shown_as="kokoro-local", text_channel=channel_spy())
    try:
        assert live._reply_feed.name == "Campbell"
    finally:
        live.sink.cleanup()


async def test_post_caller_speech_reaches_the_feed():
    live, _ = live_session(post_caller_speech=True)
    try:
        assert live._reply_feed.caller_speech
    finally:
        live.sink.cleanup()


def test_the_default_discord_profile_posts_replies_and_not_the_caller():
    transport = SleeptalkConfig.model_validate(
        {"transport": {"type": "discord"}}
    ).transport
    assert transport.post_replies is True
    assert transport.post_caller_speech is False


def test_the_config_keys_reach_the_profile_surface():
    from sleeptalk.discord.env import DiscordBotProfile

    config = SleeptalkConfig.model_validate({
        "transport": {"type": "discord", "post_replies": False,
                      "post_caller_speech": True},
    })
    surface = DiscordBotProfile.from_config(config).discord
    assert surface.post_replies is False
    assert surface.post_caller_speech is True


async def test_one_subscriber_feeds_both_the_job_posts_and_the_written_call(monkeypatch):
    monkeypatch.setattr("sleeptalk.session.append_session_event", lambda event: None)
    from sleeptalk.discord_voice.live_session import PiDiscordLiveSession
    from sleeptalk.session import RealtimeSession

    live, voice_chat = live_session()
    try:
        kernel = RealtimeSession(SimpleNamespace())
        live.bot.realtime_session = kernel
        live._job_unsubscribe = None
        live._hybrid_route = False
        PiDiscordLiveSession._subscribe_job_events(live)
        kernel.emit(agent_event(
            kernel.session_id, 1, "job.started", title="deploy", jobId="j1",
        ))
        say(live._reply_feed, 2, "On it.", heard="Deploy")
        await drain(live._reply_feed)
        if live._hybrid_post_task is not None:
            await asyncio.gather(live._hybrid_post_task, return_exceptions=True)
        sent = posted(voice_chat)
        assert "Working in the background: **deploy**" in sent
        assert "On it." in sent
    finally:
        live.sink.cleanup()


# -- masking -------------------------------------------------------------


def test_an_error_posted_in_the_channel_carries_no_key():
    from sleeptalk.discord.helpers import format_error

    line = format_error(RuntimeError(
        "POST https://api.example.com/v1/tts?api_key=sk-ant-abcdefghijklmnopqrstuv "
        "failed (authorization: Bearer abc123def456ghi789jkl)"
    ))
    assert "sk-ant-abcdefghijklmnopqrstuv" not in line
    assert "abc123def456ghi789jkl" not in line
    assert "api.example.com" in line


async def test_an_announcement_never_borrows_the_last_callers_words():
    channel = channel_spy()
    feed = feed_for(channel, caller_speech=True)
    say(feed, 1, "Three files changed.", heard="What changed?")
    # A background job finishes later, with nobody having said anything.
    feed.handle(agent_event(
        SESSION, 2, "sentence", text="Your deploy finished.", spaced=True,
    ))
    feed.handle(response(SESSION, 2, kind=SessionEventType.RESPONSE_COMPLETED))
    await drain(feed)
    assert posted(channel) == [
        "**Alice:** What changed?\n**Campbell:** Three files changed.",
        "Your deploy finished.",
    ]
