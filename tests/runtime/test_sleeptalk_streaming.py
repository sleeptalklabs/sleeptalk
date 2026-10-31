"""Shared streaming layer: SentenceStreamer, EventGate, DeadlineClock.

Cases ported from test_pi_rpc.py / test_hermes_adapter.py /
test_claude_code_adapter.py — they drove the same loops inline.
"""

import asyncio

import pytest

from sleeptalk.streaming import (
    DEFAULT_QUIET_WORK_LINE,
    KEEPALIVE,
    DeadlineClock,
    EventGate,
    SentenceStreamer,
    await_activity,
    compose_progress_line,
    elapsed_suffix,
    next_quiet_progress_line,
    next_reassure_line,
)


# ---- SentenceStreamer -----------------------------------------------------


def test_yields_sentences_incrementally():
    s = SentenceStreamer()
    assert s.feed("First sentence. Sec") == ["First sentence."]
    assert s.feed("ond one!") == ["Second one!"]
    assert s.flush() == ""


def test_streamer_strips_heading_markers():
    s = SentenceStreamer()
    assert s.feed("# Hello there. More words follow.") == [
        "Hello there.",
        "More words follow.",
    ]


def test_first_clause_flushes_early():
    s = SentenceStreamer()
    out = s.feed("Let me pull up the latency logs for this turn, and then we")
    assert out == ["Let me pull up the latency logs for this turn,"]
    out = s.feed(" can see exactly where the time went.")
    assert out == ["and then we can see exactly where the time went."]
    assert s.flush() == ""


def test_short_first_sentence_not_split():
    s = SentenceStreamer()
    assert s.feed("Sure, one sec.") == ["Sure, one sec."]
    assert s.flush() == ""


def test_code_fence_stripped_across_deltas():
    s = SentenceStreamer()
    out = s.feed("Here is the code:\n\n")
    out += s.feed("```python\nprint(1)\n```\n\nDone.")
    # One sentence spanning the stripped fence; whitespace normalizes at
    # the respond() layer (extract_speakable_text).
    assert len(out) == 1
    assert " ".join(out[0].split()) == "Here is the code: Done."
    assert s.flush() == ""


def test_clause_flush_only_once_per_turn():
    # Q1: a short first sentence sets yielded_any; a later long opening-less
    # sentence must NOT clause-flush — it waits for its terminator.
    s = SentenceStreamer()
    assert s.feed("Sure.") == ["Sure."]
    long_sentence = (
        "Now let me walk through the latency logs for this turn, "
        "and then we can see exactly where the time went"
    )
    assert s.feed(long_sentence) == []  # no clause flush mid-turn
    assert s.feed(".") == [long_sentence + "."]


def test_clause_skipped_when_sentence_complete_in_buffer():
    # Q2: sentence extraction wins when a terminator is already present.
    s = SentenceStreamer()
    out = s.feed(
        "Let me pull up the latency logs for this turn, and then we "
        "can see exactly where the time went."
    )
    assert out == [
        "Let me pull up the latency logs for this turn, and then we "
        "can see exactly where the time went."
    ]


def test_flush_returns_trailing_partial():
    s = SentenceStreamer()
    assert s.feed("One. trailing frag") == ["One."]
    assert s.flush() == "trailing frag"


def test_dotted_file_identifiers_are_not_sentence_ends():
    s = SentenceStreamer()
    assert s.feed(
        "curl is in `lib/content.ts:3` and appears twice on the page"
    ) == []
    assert s.feed(".") == [
        "curl is in lib/content.ts:3 and appears twice on the page."
    ]


# ---- EventGate ------------------------------------------------------------


def test_a_number_split_across_deltas_is_not_a_sentence_end():
    """A Discord reply came out as "GLM 5." then "3 from Z-AI": the delta
    ended on the period, so it was cut before the rest of the number."""
    streamer = SentenceStreamer()
    out = streamer.feed("Still the same one. GLM 5.")
    out += streamer.feed("3 from Z-AI, running through OpenRouter. It costs $2.")
    out += streamer.feed("50 an hour. Version 2. ")
    assert out == [
        "Still the same one.",
        "GLM 5.3 from Z-AI, running through OpenRouter.",
        "It costs $2.50 an hour.",
        "Version 2.",
    ]
    # A reply that really ends on a number still reaches the voice.
    streamer.feed("Answer 5.")
    assert streamer.flush() == "Answer 5."


def test_event_gate_fires_once_per_kind():
    seen: list[str] = []
    gate = EventGate(seen.append)
    gate.fire("text")
    gate.fire("text")
    gate.fire("thinking")
    gate.fire("text")
    assert seen == ["text", "thinking"]


def test_event_gate_emit_always_passes():
    seen: list[str] = []
    gate = EventGate(seen.append)
    gate.emit("tool:bash")
    gate.emit("tool:bash")
    gate.emit("tooldetail:running ls")
    assert seen == ["tool:bash", "tool:bash", "tooldetail:running ls"]


def test_event_gate_no_callback_is_quiet():
    gate = EventGate(None)
    gate.fire("text")  # must not raise
    gate.emit("tool:bash")


# ---- DeadlineClock + await_activity ---------------------------------------


def _clock(timeout, *, keepalive=0.0, fired=None):
    return DeadlineClock(
        timeout,
        idle_error=lambda: TimeoutError("idle"),
        hard_error=lambda: TimeoutError("hard"),
        keepalive_interval_s=keepalive,
        on_keepalive=(lambda: fired.append("keepalive")) if fired is not None else None,
    )


async def _never():
    await asyncio.sleep(3600)


@pytest.mark.asyncio
async def test_idle_timeout_raises_idle_error():
    clock = _clock(0.1)
    with pytest.raises(TimeoutError, match="idle"):
        await await_activity(clock, {"event": asyncio.ensure_future(_never())})


@pytest.mark.asyncio
async def test_activity_resets_idle_clock():
    # Hard deadline is turn-start + timeout, so idle-reset is observable
    # only inside that window: after activity, the idle deadline sits a
    # full timeout ahead of now while the hard deadline stays put.
    clock = _clock(1.0)
    hard = clock._hard_deadline
    await asyncio.sleep(0.05)
    clock.note_activity()
    assert clock._idle_deadline > hard
    assert clock._hard_deadline == hard  # never moves


@pytest.mark.asyncio
async def test_hard_deadline_never_resets():
    clock = _clock(0.25)
    # Keep the idle clock alive past the hard deadline.
    while True:
        await asyncio.sleep(0.05)
        try:
            clock.note_activity()
            clock.remaining()
        except TimeoutError as exc:
            assert str(exc) == "hard"
            break


@pytest.mark.asyncio
async def test_wait_returns_completed_waiter_name():
    clock = _clock(5.0)

    async def quick():
        await asyncio.sleep(0.01)
        return "payload"

    task = asyncio.ensure_future(quick())
    name = await await_activity(clock, {"event": task, "abort": asyncio.ensure_future(_never())})
    assert name == "event"
    assert task.result() == "payload"


@pytest.mark.asyncio
async def test_keepalive_fires_during_silence():
    fired: list[str] = []
    clock = _clock(5.0, keepalive=0.05, fired=fired)
    event = asyncio.ensure_future(_never())
    try:
        name = await await_activity(clock, {"event": event})
        assert name == KEEPALIVE
        assert fired == ["keepalive"]
        # Rearmed: a second silent stretch fires again.
        name = await await_activity(clock, {"event": event})
        assert name == KEEPALIVE
        assert fired == ["keepalive", "keepalive"]
    finally:
        event.cancel()


@pytest.mark.asyncio
async def test_keepalive_resets_on_audible_progress_only():
    fired: list[str] = []
    clock = _clock(5.0, keepalive=0.1, fired=fired)
    # Transport activity (thinking deltas) does NOT defer keepalive...
    await asyncio.sleep(0.07)
    clock.note_activity()
    await asyncio.sleep(0.07)
    clock.note_activity()
    assert clock.keepalive_due()
    # ...but audible progress (yielded sentence / tool narration) does.
    clock.note_audible()
    assert not clock.keepalive_due()


@pytest.mark.asyncio
async def test_keepalive_disabled_at_zero():
    fired: list[str] = []
    clock = _clock(0.15, keepalive=0.0, fired=fired)
    event = asyncio.ensure_future(_never())
    try:
        with pytest.raises(TimeoutError, match="idle"):
            await await_activity(clock, {"event": event})
        assert fired == []
    finally:
        event.cancel()


# ---- reassure line picking (moved from pi_discord_bot) --------------------


def test_elapsed_suffix_ported():
    assert elapsed_suffix(59) == ""
    assert elapsed_suffix(60) == " — about 1 minute in"
    assert elapsed_suffix(179) == " — about 2 minutes in"


def test_compose_progress_line_ported():
    assert compose_progress_line("running git status", 5) == "Still running git status."
    assert (
        compose_progress_line("searching the web for news", 75)
        == "Still searching the web for news — about 1 minute in."
    )
    assert compose_progress_line("", 300) == ""


def test_next_quiet_progress_line_speaks_once():
    used: set[str] = set()
    first = next_quiet_progress_line("running git status", 5, used)
    assert first == "Still running git status."
    used.add(first)
    assert next_quiet_progress_line("running git status", 15, used) == ""
    assert next_quiet_progress_line("", 20, used) == ""
    empty_used: set[str] = set()
    assert next_quiet_progress_line("", 5, empty_used) == DEFAULT_QUIET_WORK_LINE


def test_next_reassure_line_prefers_dynamic_then_tool_then_generic():
    used: set[str] = set()
    line = next_reassure_line("bash", "running make", 5, used)
    assert line == "Still running make."
    used.add(line)
    line = next_reassure_line("bash", "", 5, used)
    assert "running" in line or "command" in line  # tool-specific pool
    used.add(line)


def test_next_reassure_line_never_repeats_and_empties():
    used: set[str] = set()
    seen: set[str] = set()
    for _ in range(50):
        line = next_reassure_line("", "", 5, used)
        if not line:
            break
        assert line not in seen
        seen.add(line)
        used.add(line)
    # Exhausted the generic pool without repeating.
    assert len(seen) >= 10
    assert next_reassure_line("", "", 5, used) == ""


class _FakeWS:
    """Async iterator of fake TEXT messages for _read_messages."""

    def __init__(self, payloads: list[str]):
        import aiohttp as _aio

        self._messages = [
            type("M", (), {"type": _aio.WSMsgType.TEXT, "data": data})()
            for data in payloads
        ]

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._messages:
            raise StopAsyncIteration
        return self._messages.pop(0)


def test_deepgram_streaming_url_uses_configured_proxy_origin():
    from sleeptalk.providers import ProviderSpec
    from sleeptalk.speech.deepgram import _streaming_url

    spec = ProviderSpec(
        provider="deepgram",
        model="nova-3",
        options={"base_url": "https://proxy.example/providers/deepgram"},
    )

    url = _streaming_url(spec)

    assert url.startswith(
        "wss://proxy.example/providers/deepgram/v1/listen?"
    )
    assert "model=nova-3" in url


@pytest.mark.asyncio
async def test_read_messages_parses_finals():
    """Regression: _read_messages must parse JSON (json was unimported)."""
    from sleeptalk.providers import ProviderSpec
    from sleeptalk.speech.deepgram import _DeepgramStreamingTurn

    turn = _DeepgramStreamingTurn(
        ProviderSpec(provider="deepgram", model="nova-3"), api_key="x"
    )
    turn._ws = _FakeWS([
        '{"channel": {"alternatives": [{"transcript": "hello world"}]}, "is_final": true, "speech_final": true}',
        "not json",
    ])
    await turn._read_messages()
    assert turn.transcript == "hello world"


@pytest.mark.asyncio
async def test_deepgram_first_partial_latency_event_fires_once_per_attempt():
    from sleeptalk.providers import ProviderSpec
    from sleeptalk.speech.deepgram import _DeepgramStreamingTurn

    turn = _DeepgramStreamingTurn(
        ProviderSpec(provider="deepgram", model="nova-3"), api_key="x"
    )
    events: list[tuple[str, dict]] = []
    turn.begin_latency_attempt(
        attempt_id="trace123:stt:ws:1",
        on_event=lambda stage, fields: events.append((stage, fields)),
    )
    turn._ws = _FakeWS(
        [
            '{"channel": {"alternatives": [{"transcript": "hello"}]}, "is_final": false}',
            '{"channel": {"alternatives": [{"transcript": "hello world"}]}, "is_final": true}',
        ]
    )

    await turn._read_messages()

    assert [stage for stage, _ in events] == ["stt_first_partial"]
    assert events[0][1]["attemptId"] == "trace123:stt:ws:1"
    assert events[0][1]["provider"] == "deepgram"
    assert events[0][1]["transcriptChars"] == 5


@pytest.mark.asyncio
async def test_deepgram_stream_meter_accumulates_audio_per_attempt(tmp_path):
    from sleeptalk.metering import UsageMeter
    from sleeptalk.providers import ProviderSpec
    from sleeptalk.speech.deepgram import _DeepgramStreamingTurn

    class _SendWS:
        closed = False

        async def send_bytes(self, _payload):
            return None

        async def send_json(self, payload):
            assert payload == {"type": "Finalize"}

    meter = UsageMeter(log_path=tmp_path / "usage.jsonl", session_id="session123")
    turn = _DeepgramStreamingTurn(
        ProviderSpec(provider="deepgram", model="nova-3"),
        api_key="x",
        usage_meter=meter,
    )
    turn._ws = _SendWS()

    async def _no_connect():
        return None

    turn._connect = _no_connect
    turn.begin_latency_attempt(
        attempt_id="trace123:stt:ws:1",
        on_event=lambda *_args: None,
    )

    await turn.send_pcm(b"\x00" * 1_920)
    await turn.finish(fallback_pcm=b"", timeout_seconds=0.05)

    snapshot = meter.snapshot()
    assert snapshot["totals"]["audioMs"] == 20
    assert snapshot["totals"]["audioBytes"] == 1_920
    assert snapshot["attempts"]["deepgram:stt.websocket"] == 1
    assert snapshot["openAttemptIds"] == []


@pytest.mark.asyncio
async def test_segment_final_is_not_mistaken_for_end_of_turn_and_keeps_interim_tail():
    from sleeptalk.providers import ProviderSpec
    from sleeptalk.speech.deepgram import _DeepgramStreamingTurn

    turn = _DeepgramStreamingTurn(
        ProviderSpec(provider="deepgram", model="nova-3"), api_key="x"
    )
    turn._ws = _FakeWS(
        [
            '{"start": 0.0, "duration": 2.0, "channel": {"alternatives": [{"transcript": "Can you make a game"}]}, "is_final": true, "speech_final": false}',
            '{"start": 2.0, "duration": 1.0, "channel": {"alternatives": [{"transcript": "so I could play it"}]}, "is_final": false, "speech_final": false}',
        ]
    )

    await turn._read_messages()

    signal = turn.turn_completion_signal()
    assert signal.transcript == "Can you make a game so I could play it"
    assert signal.is_final is False


@pytest.mark.asyncio
async def test_empty_from_finalize_response_releases_fresh_finalize_wait():
    from sleeptalk.providers import ProviderSpec
    from sleeptalk.speech.deepgram import _DeepgramStreamingTurn

    turn = _DeepgramStreamingTurn(
        ProviderSpec(provider="deepgram", model="nova-3"), api_key="x"
    )
    turn._ws = _FakeWS(['{"from_finalize": true}'])

    await turn._read_messages()

    assert turn._finalize_event.is_set()


@pytest.mark.asyncio
async def test_finish_ignores_stale_final_event_and_waits_for_fresh_tail():
    from sleeptalk.providers import ProviderSpec
    from sleeptalk.speech.deepgram import _DeepgramStreamingTurn

    turn = _DeepgramStreamingTurn(
        ProviderSpec(provider="deepgram", model="nova-3"), api_key="x"
    )

    class _FinishWS:
        closed = False

        async def send_json(self, payload):
            assert payload == {"type": "Finalize"}

            async def deliver_tail():
                await asyncio.sleep(0.02)
                turn._final_parts.append("so I could play it")
                turn._final_event.set()
                turn._finalize_event.set()

            asyncio.create_task(deliver_tail())

    turn._ws = _FinishWS()
    turn._final_parts = ["Can you make a game"]
    turn._latest_interim = "so I could play it"
    turn._final_event.set()  # stale segment-final from before Finalize
    turn._sent_source_bytes = 48_000 * 2 * 2 * 3
    turn._latest_result_end_seconds = 2.0

    transcript = await turn.finish(fallback_pcm=b"", timeout_seconds=1.0)

    assert transcript == "Can you make a game so I could play it"
    assert turn.last_finish_stats.finalize_exit == "from_finalize"
    assert turn.last_finish_stats.finalize_wait_ms >= 15
