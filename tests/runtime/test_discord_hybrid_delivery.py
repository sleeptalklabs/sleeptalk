import asyncio
from collections import deque
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sleeptalk.discord.session_helpers import parse_voice_echo
from sleeptalk.discord_voice.live_session import PiDiscordLiveSession
from sleeptalk.session import RealtimeSession
from sleeptalk.session_events import Speaker, agent_event, transcript
from sleeptalk.voice_loop.lifecycle import LifecycleSupervisor


@pytest.fixture
def live(monkeypatch):
    monkeypatch.setattr("sleeptalk.session.append_session_event", lambda event: None)
    session = object.__new__(PiDiscordLiveSession)
    session.bot = SimpleNamespace(
        realtime_session=RealtimeSession(SimpleNamespace()),
        profile=SimpleNamespace(name="Bot"),
    )
    session.text_channel = SimpleNamespace(send=AsyncMock())
    session._hybrid_route = True
    session._hybrid_unsubscribe = None
    session._hybrid_posts = deque()
    session._hybrid_post_task = None
    session._hybrid_authors = {}
    session._hybrid_traces = {}
    session._hybrid_audio_turns = set()
    session._lifecycle = LifecycleSupervisor()
    session.turn_lock = asyncio.Lock()
    session._turn_generation = 0
    session.route_output = SimpleNamespace(on_write=None)
    session._new_trace = Mock(side_effect=lambda *args: Mock())
    return session


def emit_text(live, turn, text, *, speaker=Speaker.ASSISTANT, **fields):
    session = live.bot.realtime_session
    session.emit(transcript(
        session.session_id, turn, speaker=speaker, text=text, final=True, **fields
    ))


async def drain(live):
    if live._hybrid_post_task is not None:
        await live._hybrid_post_task


def test_detached_failure_is_written_to_its_origin_trace(live):
    live._subscribe_hybrid_events()
    source_trace = Mock()
    live._hybrid_traces[1] = source_trace
    live._hybrid_traces[2] = Mock()
    session = live.bot.realtime_session
    session.emit(agent_event(
        session.session_id, 2, "hybrid.task.failed", sourceTurnId=1,
        taskId="failed-task", errorType="AgentError", elapsedMs=1000,
    ))
    source_trace.mark.assert_called_once_with(
        "hybrid.task.failed", sourceTurnId=1, taskId="failed-task",
        errorType="AgentError", routeElapsedMs=1000,
    )
    live._hybrid_traces[2].mark.assert_not_called()
    live._unsubscribe_hybrid_events()


def test_committed_hybrid_owner_is_written_to_the_turn_trace(live):
    live._subscribe_hybrid_events()
    trace = Mock()
    live._hybrid_traces[1] = trace
    session = live.bot.realtime_session
    session.emit(agent_event(
        session.session_id,
        1,
        "hybrid.intent.committed",
        owner="specialist",
        reason="hands",
        taskId="task-1",
    ))

    trace.mark.assert_called_once_with(
        "hybrid.intent.committed",
        sourceTurnId=1,
        owner="specialist",
        reason="hands",
        taskId="task-1",
    )
    live._unsubscribe_hybrid_events()


@pytest.mark.asyncio
async def test_detached_answer_keeps_original_author_and_trace_during_new_turn(live):
    second_started = asyncio.Event()
    release_second = asyncio.Event()

    semantic_id = 0

    async def run_turn(*, on_started, **kwargs):
        nonlocal semantic_id
        semantic_id += 1
        turn_id = semantic_id
        on_started(turn_id)
        emit_text(live, turn_id, f"Question {turn_id}", speaker=Speaker.USER)
        emit_text(live, turn_id, "Working on it.", delivered=True, speechKind="ack")
        if turn_id == 2:
            second_started.set()
            await release_second.wait()
        return SimpleNamespace(status="completed", heard="duplicate", played=["duplicate"])

    live.bot.kernel = SimpleNamespace(run_turn=run_turn)
    await live._handle_hybrid_utterance(SimpleNamespace(id=1, display_name="Alice"), b"\0" * 8)
    source_trace = live._hybrid_traces[1]
    second_turn = asyncio.create_task(live._handle_hybrid_utterance(
        SimpleNamespace(id=2, display_name="Bob"), b"\0" * 8
    ))
    await second_started.wait()
    delivery_trace = live._hybrid_traces[2]
    emit_text(live, 2, "The fix is ready.", delivered=True, sourceTurnId=1, taskId="task-1")
    kernel = live.bot.realtime_session
    kernel.emit(agent_event(
        kernel.session_id, 2, "hybrid.delivery", sourceTurnId=1,
        deliveryTurnId=2, taskId="task-1", status="delivered", elapsedMs=40,
    ))
    kernel.emit(agent_event(
        kernel.session_id, 1, "hybrid.task.completed", sourceTurnId=1,
        taskId="task-1", status="succeeded",
    ))
    await drain(live)
    release_second.set()
    await second_turn

    messages = [call.args[0] for call in live.text_channel.send.await_args_list]
    assert len(messages) == 5
    assert "**Alice:** " in messages[-1]
    assert "**Bot:** The fix is ready." in messages[-1]
    assert "duplicate" not in "\n".join(messages)
    source_trace.mark.assert_any_call(
        "hybrid.delivery", sourceTurnId=1, deliveryTurnId=2,
        taskId="task-1", status="delivered", routeElapsedMs=40,
    )
    source_trace.mark.assert_any_call(
        "hybrid.task.completed", sourceTurnId=1,
        taskId="task-1", status="succeeded",
    )
    assert not any(call.args[0] == "hybrid.delivery" for call in delivery_trace.mark.call_args_list)
    assert messages[0] == "**Alice:** Question 1"
    assert messages[2] == "**Bob:** Question 2"
    assert all(parse_voice_echo(messages[index]) is not None for index in (1, 3, 4))


@pytest.mark.asyncio
async def test_only_delivered_transcripts_post_and_subscription_is_idempotent(live):
    live._subscribe_hybrid_events()
    live._subscribe_hybrid_events()
    emit_text(live, 1, "Unplayed generated findings")
    emit_text(live, 1, "Silent response", delivered=False)
    emit_text(live, 1, "I'll check.", delivered=True, speechKind="ack")
    emit_text(live, 1, "The longer answer", delivered=True, interrupted=True)
    await drain(live)

    messages = [call.args[0] for call in live.text_channel.send.await_args_list]
    assert len(messages) == 2
    assert messages[0].endswith("**Bot:** I'll check.")
    assert messages[1].endswith("**Bot:** [Interrupted; partially played] The longer answer")


@pytest.mark.asyncio
async def test_channel_posts_remain_ordered_when_first_send_blocks(live):
    live._subscribe_hybrid_events()
    started = asyncio.Event()
    release = asyncio.Event()
    posted = []

    async def send(content):
        if not posted:
            started.set()
            await release.wait()
        posted.append(content)

    live.text_channel.send = send
    emit_text(live, 1, "First", delivered=True)
    await started.wait()
    emit_text(live, 2, "Second", delivered=True)
    await asyncio.sleep(0)
    assert posted == []
    release.set()
    await drain(live)
    assert [message.split("**Bot:** ")[1] for message in posted] == ["First", "Second"]


@pytest.mark.asyncio
async def test_shutdown_unsubscribes_and_cancels_pending_channel_posts(live):
    live._subscribe_hybrid_events()
    started = asyncio.Event()

    async def send(content):
        started.set()
        await asyncio.Event().wait()

    live.text_channel.send = AsyncMock(side_effect=send)
    emit_text(live, 1, "In flight", delivered=True)
    await started.wait()
    emit_text(live, 1, "Queued", delivered=True)
    live._lifecycle.begin_shutdown()
    live._unsubscribe_hybrid_events()
    live._unsubscribe_hybrid_events()
    live._subscribe_hybrid_events()
    with pytest.raises(asyncio.CancelledError):
        await live._hybrid_post_task
    emit_text(live, 2, "After shutdown", delivered=True)
    assert not live._hybrid_posts
    assert not live.bot.realtime_session._subscribers
    assert live.text_channel.send.await_count == 1


@pytest.mark.asyncio
async def test_cascade_does_not_subscribe(live):
    live._hybrid_route = False
    live._subscribe_hybrid_events()
    emit_text(live, 1, "Cascade owns posting", delivered=True)
    assert live._hybrid_unsubscribe is None
    live.text_channel.send.assert_not_awaited()
