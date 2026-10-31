from sleeptalk.presentation import (
    Line,
    PresentationState,
    Word,
    apply_playback_ms,
    apply_sentence_lit,
    fit_snapshot,
    reduce,
    start_hearing,
    words_from_cartesia_frame,
    words_from_text,
)
from sleeptalk.session_events import (
    SessionEventType,
    Speaker,
    agent_event,
    interruption,
    progress,
    response,
    session_state,
    tool_event,
    transcript,
)
from sleeptalk.session_events import InterruptionPhase, InterruptionSource, SessionState


def test_words_from_cartesia_frame_converts_seconds() -> None:
    words = words_from_cartesia_frame(
        {
            "type": "timestamps",
            "word_timestamps": {
                "words": ["Done,", "the"],
                "start": [0.0, 0.12],
                "end": [0.1, 0.2],
            },
        }
    )
    assert words[0].text == "Done,"
    assert words[0].start_ms == 0
    assert words[0].end_ms == 100
    assert words[1].start_ms == 120


def test_words_from_cartesia_frame_unknown_shape_is_empty() -> None:
    assert words_from_cartesia_frame({"type": "chunk"}) == ()


def test_words_from_text_skips_empty() -> None:
    words = words_from_text("  Done, the board   is running ")
    assert [word.text for word in words] == ["Done,", "the", "board", "is", "running"]
    assert words[0].start_ms == 0
    assert words[1].start_ms is not None and words[1].start_ms > 0
    assert all(
        later.start_ms is not None
        and earlier.start_ms is not None
        and later.start_ms > earlier.start_ms
        for earlier, later in zip(words, words[1:])
    )


def test_reduce_session_active_to_listening() -> None:
    state = reduce(PresentationState(), session_state("s", SessionState.ACTIVE))
    assert state.presence == "listening"


def test_reduce_keeps_tts_speed() -> None:
    state = reduce(PresentationState(tts_speed=1.25), session_state("s", SessionState.ACTIVE))
    assert state.tts_speed == 1.25
    assert state.to_json()["ttsSpeed"] == 1.25


def test_reduce_tool_start_then_complete_by_call_id() -> None:
    start = tool_event(
        "s",
        1,
        SessionEventType.TOOL_STARTED,
        name="edit",
        call_id="c1",
        path="serve.py",
    )
    done = tool_event(
        "s",
        1,
        SessionEventType.TOOL_COMPLETED,
        name="edit",
        call_id="c1",
        path="serve.py",
    )
    state = reduce(reduce(PresentationState(), start), done)
    tools = [line for line in state.lines if line.kind == "tool"]
    assert len(tools) == 1
    assert tools[0].phase == "completed"
    assert tools[0].text == "edit  serve.py"


def test_reduce_two_tool_calls_stay_in_order() -> None:
    first = tool_event(
        "s",
        1,
        SessionEventType.TOOL_STARTED,
        name="read",
        call_id="c1",
        path="a.py",
    )
    second = tool_event(
        "s",
        1,
        SessionEventType.TOOL_STARTED,
        name="edit",
        call_id="c2",
        path="b.py",
    )
    state = reduce(reduce(PresentationState(), first), second)
    tools = [line for line in state.lines if line.kind == "tool"]
    assert [line.text for line in tools] == ["read  a.py", "edit  b.py"]


def test_reduce_assistant_sentence_sets_line_words() -> None:
    event = agent_event("s", 1, "sentence", text="Done, the board is running")
    state = reduce(PresentationState(), event)
    assert len(state.lines) == 1
    line = state.lines[0]
    assert line.kind == "assistant"
    assert line.text == "Done, the board is running"
    assert line.spoken_index == 0
    assert state.presence == "thinking"
    assert [word.text for word in line.words] == [
        "Done,",
        "the",
        "board",
        "is",
        "running",
    ]
    assert line.words[0].start_ms == 0


def test_reduce_second_sentence_is_its_own_line() -> None:
    first = agent_event("s", 1, "sentence", text="Done, the board is running")
    second = agent_event("s", 1, "sentence", text="It's running in the background.")
    state = reduce(reduce(PresentationState(), first), second)
    assert [line.text for line in state.lines] == [
        "Done, the board is running",
        "It's running in the background.",
    ]
    assert state.lines[0].spoken_index == 5
    assert state.lines[1].spoken_index == 0


def test_wrapped_twin_collapses_to_one_line() -> None:
    wrapped = agent_event(
        "s",
        1,
        "sentence",
        text="Everyt\nhing's going great,\nthanks for\nasking. What's on your mind?",
    )
    clean = agent_event(
        "s",
        1,
        "sentence",
        text="Everything's going great, thanks for asking. What's on your mind?",
    )
    state = reduce(reduce(PresentationState(), wrapped), clean)
    assert [line.text for line in state.lines] == [
        "Everything's going great, thanks for asking. What's on your mind?"
    ]


def test_token_delta_then_assembled_sentence_is_one_line() -> None:
    delta = agent_event(
        "s",
        1,
        "text",
        text="Makes sense, that's the sleeptalk loop you've been tuning.",
    )
    spoken = agent_event(
        "s",
        1,
        "sentence",
        text="Makes sense, that's the sleeptalk loop you've been tuning.",
    )
    state = reduce(reduce(PresentationState(), delta), spoken)
    assert [line.text for line in state.lines] == [
        "Makes sense, that's the sleeptalk loop you've been tuning."
    ]


def test_leftover_sentence_inside_last_line_does_not_repeat() -> None:
    full = agent_event(
        "s",
        1,
        "sentence",
        text="Makes sense, that's the sleeptalk loop you've been tuning.",
    )
    leftover = agent_event(
        "s",
        1,
        "sentence",
        text="that's the sleeptalk loop you've been tuning.",
    )
    state = reduce(reduce(PresentationState(), full), leftover)
    assert [line.text for line in state.lines] == [
        "Makes sense, that's the sleeptalk loop you've been tuning."
    ]


def test_workbench_text_tokens_are_not_spoken() -> None:
    state = PresentationState()
    for token in ["I", "'m", " running", " as", " the", " pi"]:
        state = reduce(state, agent_event("s", 1, "text", text=token))
    assert state.lines == ()
    assert state.presence == "thinking"
    state = reduce(
        state,
        agent_event("s", 1, "sentence", text="I'm running as the pi coding agent."),
    )
    assert [line.text for line in state.lines] == ["I'm running as the pi coding agent."]


def test_word_chunks_with_leading_spaces_stay_spaced() -> None:
    state = PresentationState()
    for chunk in ["I'm", " running", " as", " the", " pi"]:
        state = reduce(state, agent_event("s", 1, "sentence", text=chunk))
    assert [line.text for line in state.lines] == ["I'm running as the pi"]


def test_spoken_fragments_merge_on_one_line() -> None:
    chunks = [
        "Everyt",
        "hing's going great,",
        "thanks for",
        "asking. What's on your mind?",
    ]
    state = PresentationState()
    for chunk in chunks:
        state = reduce(state, agent_event("s", 1, "sentence", text=chunk))
    assert [line.text for line in state.lines] == [
        "Everything's going great, thanks for asking. What's on your mind?"
    ]
    assert state.lines[0].spoken_index == 0


def test_commit_played_rollup_does_not_reprint_streamed_sentences() -> None:
    first = agent_event(
        "s",
        1,
        "sentence",
        text=(
            "Makes sense — that's the sleeptalk loop you've been tuning "
            "(the 1000 ms commit-on-silence work in the last two commits)."
        ),
    )
    second = agent_event(
        "s",
        1,
        "sentence",
        text=(
            "For what it's worth from my side: I only see committed text, so if a "
            "turn arrives truncated or spliced, that's upstream of me."
        ),
    )
    stray = agent_event("s", 1, "sentence", text=".")
    rollup = transcript(
        "s",
        1,
        speaker=Speaker.ASSISTANT,
        text=(
            "Makes sense — that's the sleeptalk loop you've been tuning "
            "(the 1000 ms commit-on-silence work in the last two commits). "
            "For what it's worth from my side: I only see committed text, so if a "
            "turn arrives truncated or spliced, that's upstream of me."
        ),
        final=True,
    )
    state = PresentationState()
    for event in (first, second, stray, rollup):
        state = reduce(state, event)
    assert [line.text for line in state.lines if line.kind == "assistant"] == [
        first.payload["text"],
        second.payload["text"],
    ]


def test_spoken_near_duplicate_does_not_append() -> None:
    first = agent_event("s", 1, "sentence", text="Everything's ready.")
    twin = transcript(
        "s",
        1,
        speaker=Speaker.ASSISTANT,
        text="Everything 's ready.",
        final=True,
    )
    also = response(
        "s",
        1,
        SessionEventType.RESPONSE_FIRST_OUTPUT,
        output_kind="audio",
        text="Everything's ready.",
    )
    state = reduce(reduce(reduce(PresentationState(), first), twin), also)
    assert [line.text for line in state.lines] == ["Everything's ready."]


def test_apply_playback_ms_lights_words_in_order() -> None:
    words = words_from_text("one two three")
    timed = (
        words[0].__class__(words[0].text, start_ms=0, end_ms=80),
        words[1].__class__(words[1].text, start_ms=80, end_ms=160),
        words[2].__class__(words[2].text, start_ms=160, end_ms=240),
    )
    state = PresentationState(
        lines=(Line(kind="assistant", text="one two three", words=timed),)
    )
    assert apply_playback_ms(state, 79).lines[0].spoken_index == 1
    assert apply_playback_ms(state, 160).lines[0].spoken_index == 3


def test_apply_playback_ms_is_noop_without_timings() -> None:
    state = PresentationState(
        lines=(
            Line(
                kind="assistant",
                text="one two",
                words=(Word("one"), Word("two")),
            ),
        )
    )
    assert apply_playback_ms(state, 500).lines[0].spoken_index == 0


def test_apply_playback_ms_follows_estimated_pace() -> None:
    words = words_from_text("one two three")
    state = PresentationState(
        lines=(Line(kind="assistant", text="one two three", words=words),)
    )
    mid = words[1].start_ms
    assert mid is not None
    assert apply_playback_ms(state, mid - 1).lines[0].spoken_index == 1
    assert apply_playback_ms(state, mid).lines[0].spoken_index == 2


def test_sentence_snap_lights_all_words() -> None:
    state = PresentationState(
        lines=(
            Line(
                kind="assistant",
                text="one two three",
                words=words_from_text("one two three"),
            ),
        )
    )
    assert apply_sentence_lit(state).lines[0].spoken_index == 3


def test_interruption_keeps_spoken_words_lit() -> None:
    lit = apply_sentence_lit(
        PresentationState(
            lines=(
                Line(
                    kind="assistant",
                    text="hi there",
                    words=words_from_text("hi there"),
                ),
            )
        )
    )
    event = interruption(
        "s",
        1,
        source=InterruptionSource.USER_SPEECH,
        phase=InterruptionPhase.ANSWERING,
    )
    state = reduce(lit, event)
    assert state.presence == "listening"
    assert state.lines[0].spoken_index == 2
    assert state.lines[0].text.endswith("—")


def test_progress_line_and_to_json_keys() -> None:
    state = reduce(
        PresentationState(),
        progress("s", 1, pool="action", text="searching the repo"),
    )
    payload = state.to_json()
    assert payload["presence"] == "thinking"
    assert payload["lines"][0]["kind"] == "progress"
    assert payload["lines"][0]["text"] == "searching the repo"
    assert payload["stack"] == []
    assert payload["speaker"] == "agent"
    assert payload["partial"] == ""
    assert payload["hearing"] is False
    stamped = PresentationState(stack=("pi/opus", "deepgram/nova-3"), speaker="max").to_json()
    assert stamped["stack"] == ["pi/opus", "deepgram/nova-3"]
    assert stamped["speaker"] == "max"


def test_response_first_output_text() -> None:
    event = response(
        "s",
        1,
        SessionEventType.RESPONSE_FIRST_OUTPUT,
        output_kind="audio",
        text="Hello there",
    )
    state = reduce(PresentationState(), event)
    assert state.presence == "speaking"
    assert state.lines[0].text == "Hello there"
    assert state.lines[0].spoken_index == 0
    assert state.lines[0].words[0].start_ms == 0


def test_hearing_stays_on_until_the_user_turn_ends() -> None:
    paused = reduce(
        start_hearing(PresentationState()),
        transcript("s", 1, speaker=Speaker.USER, text="", final=False),
    )
    assert paused.hearing is True
    assert paused.presence == "listening"
    assert paused.partial == ""
    ended = reduce(
        paused,
        transcript("s", 1, speaker=Speaker.USER, text="do the thing", final=True),
    )
    assert ended.hearing is False
    assert ended.presence == "thinking"
    assert ended.lines[0].text == "do the thing"
    assert ended.partial == ""
    again = start_hearing(ended)
    assert again is not ended
    assert start_hearing(again) is again


def test_user_partial_stays_off_the_committed_feed() -> None:
    event = transcript("s", 1, speaker=Speaker.USER, text="and ping me when", final=False)
    state = reduce(PresentationState(), event)
    assert state.presence == "listening"
    assert state.partial == "and ping me when"
    assert state.lines == ()


def test_user_transcript_final_is_a_white_line() -> None:
    event = transcript("s", 1, speaker=Speaker.USER, text="do the thing", final=True)
    state = reduce(PresentationState(), event)
    assert state.presence == "thinking"
    assert state.lines[0] == Line(kind="user", text="do the thing")


def test_specialist_transcript_is_notes_not_spoken() -> None:
    notes = transcript(
        "s",
        1,
        speaker=Speaker.ASSISTANT,
        text="The agent found three prices and a missing SKU.",
        final=True,
        owner="specialist",
    )
    spoken = transcript(
        "s",
        1,
        speaker=Speaker.ASSISTANT,
        text="That's all I got from the search.",
        final=True,
        owner="realtime",
    )
    state = reduce(reduce(PresentationState(), notes), spoken)
    assert [line.kind for line in state.lines] == ["notes", "assistant"]
    assert state.lines[0].text == "The agent found three prices and a missing SKU."
    assert state.lines[1].text == "That's all I got from the search."


def test_specialist_agent_sentence_stays_notes() -> None:
    event = agent_event(
        "s",
        1,
        "sentence",
        text="Pulled the latest release notes.",
        owner="specialist",
    )
    state = reduce(PresentationState(), event)
    assert [line.kind for line in state.lines] == ["notes"]
    assert state.lines[0].text == "Pulled the latest release notes."


def test_timeline_is_chronological() -> None:
    user = transcript("s", 1, speaker=Speaker.USER, text="edit serve.py", final=True)
    tool = tool_event(
        "s",
        1,
        SessionEventType.TOOL_STARTED,
        name="edit",
        call_id="c1",
        path="serve.py",
    )
    spoken = agent_event("s", 1, "sentence", text="Done.")
    state = reduce(reduce(reduce(PresentationState(), user), tool), spoken)
    assert [line.kind for line in state.lines] == ["user", "tool", "assistant"]
    assert [line.text for line in state.lines] == [
        "edit serve.py",
        "edit  serve.py",
        "Done.",
    ]


def _texts(state: PresentationState) -> list[tuple[str, str]]:
    return [(line.kind, line.text) for line in state.lines]


def test_a_reply_split_by_a_tool_is_not_repeated_by_its_rollup() -> None:
    events = [
        transcript("s", 1, speaker=Speaker.USER, text="Check the build.", final=True),
        agent_event("s", 1, "sentence", text="Let me look at the build."),
        tool_event("s", 1, kind=SessionEventType.TOOL_STARTED, name="bash", callId="c1", detail="make"),
        agent_event("s", 1, "sentence", text="It passes."),
    ]
    state = PresentationState()
    for event in events:
        state = reduce(state, event)
    shown = _texts(state)
    rollup = transcript("s", 1, speaker=Speaker.ASSISTANT, text="Let me look at the build. It passes.", final=True, rollup=True)
    assert _texts(reduce(state, rollup)) == shown
    # Without the flag, text matching alone repeated the first sentence (the bug).
    unflagged = transcript("s", 1, speaker=Speaker.ASSISTANT, text="Let me look at the build. It passes.", final=True)
    assert [t for k, t in _texts(reduce(state, unflagged)) if k == "assistant"].count("Let me look at the build. It passes.") == 1


def test_a_rollup_still_shows_a_reply_that_streamed_nothing() -> None:
    state = reduce(PresentationState(), transcript("s", 1, speaker=Speaker.USER, text="Status?", final=True))
    state = reduce(state, transcript("s", 1, speaker=Speaker.ASSISTANT, text="Two workers are running.", final=True, rollup=True))
    assert _texts(state)[-1] == ("assistant", "Two workers are running.")


def _job(name: str, **fields):
    return agent_event("s", 1, f"job.{name}", jobId="job-1", title="todo app", **fields)


def test_the_worker_has_its_own_feed_and_the_conversation_only_its_milestones() -> None:
    state = PresentationState(presence="listening")
    for event in [
        _job("started", startedAt=1000),
        _job("thinking", text="The user wants a claw machine…", startedAt=1001),
        _job("thinking", text="…so cannon-es for physics.", startedAt=1001),
        _job("step", text="write_file · writing…", steps=0, startedAt=1002),
        _job("step", text="write_file · claw/index.html", steps=1, startedAt=1003),
        _job("step", text="write_file · claw/index.html", steps=1, startedAt=1003),
        _job("said", text="Page loaded."),
        _job("permission", prompt="Run npm install?"),
        _job("permission_resolved", decision="allow_once"),
        _job("step", text="terminal · npm install", steps=2, startedAt=1004),
        _job("completed", links=["http://localhost:8000/", "/tmp/claw/index.html"]),
    ]:
        state = reduce(state, event)
    # The conversation: started, the ask, done. Never the orb.
    assert [(line.kind, line.phase, line.text) for line in state.lines] == [
        ("worker", "started", "Working on todo app."),
        ("worker", "permission", "Waiting for your OK: Run npm install?"),
        ("worker", "completed", "Done."),
    ]
    assert state.presence == "listening"
    (job,) = state.jobs
    assert (job.job_id, job.phase, job.steps, job.started_at, job.waiting) == ("job-1", "completed", 2, 1000, "")
    assert [(entry.kind, entry.text) for entry in job.entries] == [
        ("thinking", "…so cannon-es for physics."),
        ("step", "write_file · claw/index.html"),
        ("said", "Page loaded."),
        ("ask", "Waiting for your OK: Run npm install?"),
        ("step", "terminal · npm install"),
        ("done", "Done."),
    ]
    payload = state.to_json()
    done = payload["jobs"][0]["entries"][-1]
    assert isinstance(done.pop("startedAt"), int)
    assert done == {"kind": "done", "text": "Done.", "links": ["http://localhost:8000/", "/tmp/claw/index.html"]}
    assert payload["lines"][-1]["links"] == ["http://localhost:8000/", "/tmp/claw/index.html"]


def test_a_job_waiting_on_the_caller_says_so_until_answered() -> None:
    state = reduce(PresentationState(), _job("question", prompt="Which port?"))
    assert state.jobs[0].waiting == "Asks: Which port?"
    assert state.to_json()["jobs"][0]["waiting"] == "Asks: Which port?"
    state = reduce(state, _job("cancelled"))
    assert state.jobs[0].phase == "cancelled" and state.jobs[0].waiting == ""


def _jobn(job_id: str, title: str, name: str, **fields):
    return agent_event("s", 1, f"job.{name}", jobId=job_id, title=title, **fields)


def test_several_jobs_keep_separate_feeds_a_queue_and_the_selection() -> None:
    state = PresentationState()
    for event in [
        _jobn("job-1", "latency run", "selected"),
        _jobn("job-1", "latency run", "started", startedAt=1000),
        _jobn("job-2", "valuation", "started", startedAt=2000),
        _jobn("job-1", "latency run", "step", text="bash · analyze.py", steps=1),
        _jobn("job-2", "valuation", "step", text="web_search · Six Flags", steps=1),
        _jobn("job-3", "todo app", "queued", position=1),
    ]:
        state = reduce(state, event)
    assert [(job.job_id, job.phase) for job in state.jobs] == [
        ("job-1", "running"), ("job-2", "running"), ("job-3", "queued")]
    assert [entry.text for entry in state.jobs[0].entries] == ["bash · analyze.py"]
    assert [entry.text for entry in state.jobs[1].entries] == ["web_search · Six Flags"]
    assert state.lines[-1].text == "Queued: todo app." and state.lines[-1].phase == "queued"
    payload = state.to_json()
    assert payload["selectedJob"] == "job-1"
    assert payload["jobs"][2]["phase"] == "queued"
    # The caller picked another tab; then the queued job started.
    state = reduce(state, _jobn("job-2", "valuation", "selected"))
    state = reduce(state, _jobn("job-3", "todo app", "started", startedAt=3000))
    assert state.selected_job == "job-2"
    assert (state.jobs[2].phase, state.jobs[2].started_at) == ("running", 3000)


def test_a_follow_up_reopens_its_finished_tab() -> None:
    state = PresentationState()
    for event in [
        _jobn("job-1", "todo app", "started", startedAt=1000),
        _jobn("job-1", "todo app", "completed"),
        _jobn("job-1", "todo app", "started", startedAt=5000),
    ]:
        state = reduce(state, event)
    (job,) = state.jobs
    assert (job.phase, job.started_at) == ("running", 5000)


def test_finished_tabs_go_first_when_there_are_too_many() -> None:
    from sleeptalk.presentation import _WORKER_JOBS

    state = PresentationState()
    state = reduce(state, _jobn("job-live", "latency run", "started"))
    for n in range(_WORKER_JOBS + 2):
        state = reduce(state, _jobn(f"job-{n}", f"done {n}", "started"))
        state = reduce(state, _jobn(f"job-{n}", f"done {n}", "completed"))
    ids = [job.job_id for job in state.jobs]
    assert len(ids) == _WORKER_JOBS
    assert ids[0] == "job-live", "a running job is never dropped for a finished one"
    assert ids[-1] == f"job-{_WORKER_JOBS + 1}"


def test_three_full_live_feeds_still_fit_the_data_channel() -> None:
    """Snapshots over 64 KB were dropped and froze the page once; three live
    feeds of 200 long entries each is the new worst case."""
    from sleeptalk.presentation import fit_snapshot

    state = PresentationState()
    for job in range(3):
        state = reduce(state, _jobn(f"job-{job}", f"job {job}", "started", startedAt=1000))
        for step in range(220):
            state = reduce(state, _jobn(f"job-{job}", f"job {job}", "step",
                                        text=f"bash · {'x' * 150} {step}", steps=step + 1))
    payload = state.to_json()
    fitted = fit_snapshot(payload, 64_000)
    import json

    assert len(json.dumps(fitted, separators=(",", ":")).encode()) <= 64_000
    assert [len(job["entries"]) > 0 for job in fitted["jobs"]] == [True, True, True]


def test_narration_reads_as_paragraphs_and_placeholders_give_way() -> None:
    state = PresentationState()
    for event in [
        _job("said", text="Your to-do app is built and running."),
        _job("said", text="It's a single **self-contained** HTML file —"),
        _job("said", text="wrapped in a clean card layout."),
        _job("step", text="browser_console · writing…", steps=0),
        _job("step", text="browser_console", steps=3),
    ]:
        state = reduce(state, event)
    assert [(entry.kind, entry.text) for entry in state.jobs[0].entries] == [
        ("said", "Your to-do app is built and running. It's a single self-contained HTML file — wrapped in a clean card layout."),
        ("step", "browser_console"),
    ]


def test_a_caller_who_keeps_going_after_a_pause_is_one_growing_line() -> None:
    state = PresentationState()
    for text in ("Make a todo app.", "Make a todo app. Keep it local.", "Make a todo app. Keep it local, don't deploy it."):
        state = reduce(state, transcript("s", 1, speaker=Speaker.USER, text=text, final=True))
    assert [(line.kind, line.text) for line in state.lines] == [
        ("user", "Make a todo app. Keep it local, don't deploy it.")]
    # Once the agent has answered, the caller's next words are a new line.
    state = reduce(state, agent_event("s", 1, "sentence", text="On it."))
    state = reduce(state, transcript("s", 2, speaker=Speaker.USER, text="Make a todo app. Also dark mode.", final=True))
    assert [line.kind for line in state.lines] == ["user", "assistant", "user"]


def test_progress_with_nothing_to_say_adds_no_line() -> None:
    state = reduce(PresentationState(), progress("s", 1, pool="keepalive", text=""))
    assert state.lines == () and state.presence == "thinking"


def test_phrases_the_voice_pipeline_cut_at_a_space_keep_it() -> None:
    state = PresentationState()
    for chunk in ["Goal: pick up queued work", "in the sleeptalk project, then run the", "import check."]:
        state = reduce(state, agent_event("s", 1, "sentence", text=chunk, spaced=True))
    assert [line.text for line in state.lines] == [
        "Goal: pick up queued work in the sleeptalk project, then run the import check."]


def test_a_reply_goes_to_the_page_without_word_timings() -> None:
    plain = Line(kind="assistant", text="Done, the board is running", words=words_from_text("Done, the board is running"))
    assert "words" not in plain.to_json()  # the page splits the text itself
    timed = Line(kind="assistant", text="Done, the board", words=(Word("Done,", 0, 100), Word("the board", 120, 200)))
    assert timed.to_json()["words"] == [{"text": "Done,"}, {"text": "the board"}]


def _long_call() -> PresentationState:
    """Three jobs' worth of steps and a long conversation: about 200 KB."""
    state = PresentationState()
    for job in range(1, 4):
        state = reduce(state, agent_event("s", 1, "job.started", jobId=f"job-{job}", title=f"job {job}"))
        for step in range(1, 151):
            state = reduce(state, agent_event("s", 1, "job.step", jobId=f"job-{job}", title=f"job {job}",
                                              text=f"Bash · running check number {step} of the suite", steps=step))
        if job < 3:
            state = reduce(state, agent_event("s", 1, "job.completed", jobId=f"job-{job}", title=f"job {job}"))
    for turn in range(200):
        state = reduce(state, transcript("s", turn, speaker=Speaker.USER, text=f"Question {turn}?", final=True))
        state = reduce(state, transcript("s", turn, speaker=Speaker.ASSISTANT,
                                         text=f"Answer {turn}. " + "word " * 120, final=True, rollup=True))
    return state


def test_a_long_call_still_fits_the_page_newest_first() -> None:
    import json

    payload = _long_call().to_json()
    assert len(json.dumps(payload).encode()) > 64 * 1024
    fitted = fit_snapshot(payload, 64 * 1024)
    assert len(json.dumps(fitted, separators=(",", ":")).encode()) <= 64 * 1024
    assert fitted["lines"][-1] == payload["lines"][-1]
    finished, running = fitted["jobs"][:2], fitted["jobs"][2]
    assert all(len(job["entries"]) <= 20 and job["entries"][-1] == old["entries"][-1]
               for job, old in zip(finished, payload["jobs"]))
    assert running["entries"][-1] == payload["jobs"][2]["entries"][-1]
    assert fit_snapshot({"lines": []}, 64 * 1024) == {"lines": []}
