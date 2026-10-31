"""Reply length: how much of a long answer is spoken out loud."""

from __future__ import annotations

from typing import Any

import pytest

from sleeptalk.policy import REPLY_LENGTHS, VoicePolicy, reply_length_or_default
from sleeptalk.presentation import Line, PresentationState, reduce
from sleeptalk.session_events import SessionEventType
from sleeptalk.summarize import SPOKEN_BUDGET_CHARS, spoken_budget_chars, summarize_tail
from sleeptalk.voice_policy_runtime import VoicePolicyRuntime
from sleeptalk.voice_prompt import BRIEF_SPEECH_CONTRACT

LEDE = "I fixed the auth bug."
DETAILS = tuple(
    f"Detail number {n} about what changed in the handler and why it mattered."
    for n in range(1, 12)
)
CLOSING = "The tests pass, want me to push it?"
LONG_ANSWER = (LEDE, *DETAILS, CLOSING)


class _Ctx:
    session_id = "s"
    turn_id = 1

    def __init__(self) -> None:
        self.events: list[Any] = []
        self.extras: dict[str, Any] = {}

    def emit(self, event: Any) -> None:
        self.events.append(event)


class _Agent:
    """Streams fixed sentences, the way an adapter hands them to the runtime."""

    cwd = ""

    def __init__(self, *sentences: str) -> None:
        self.sentences = sentences
        self.prompts: list[str] = []

    async def respond_streaming(self, prompt: str, on_event: Any = None):
        del on_event
        self.prompts.append(prompt)
        for sentence in self.sentences:
            yield sentence


async def _turn(
    *sentences: str, reply_length: str, agent: _Agent | None = None
) -> tuple[list[str], _Ctx]:
    """Run one turn; return what reached the mouth and the events it emitted."""
    runtime = VoicePolicyRuntime(
        agent=agent or _Agent(*sentences),
        policy=VoicePolicy(reply_length=reply_length),
    )
    ctx = _Ctx()

    async def speak_policy(line: str, _ctx: Any) -> None:
        return None

    spoken = [
        line
        async for line in runtime.respond(
            "how did it go",
            ctx,
            speak_policy=speak_policy,
            suppress_preamble=True,
        )
    ]
    return spoken, ctx


def _transcript(ctx: _Ctx) -> list[str]:
    return [
        event.payload["text"]
        for event in ctx.events
        if event.type is SessionEventType.AGENT_EVENT
        and event.payload.get("name") == "sentence"
        and not event.payload.get("summary")
    ]


def _feed(ctx: _Ctx) -> PresentationState:
    """The call page's feed after the turn: the caller's line, then the reply."""
    state = PresentationState(lines=(Line(kind="user", text="how did it go"),))
    for event in ctx.events:
        state = reduce(state, event)
    return state


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------


def test_default_is_full() -> None:
    assert VoicePolicy().reply_length == "full"


def test_unknown_lengths_fall_back_to_full() -> None:
    """Never drop content on a typo: time is cheaper to lose than information."""
    for value in ("", None, "brief", "SUMMARIZE", 7, object()):
        assert reply_length_or_default(value) == "full"


def test_known_lengths_round_trip_case_insensitively() -> None:
    for length in REPLY_LENGTHS:
        assert reply_length_or_default(length.upper()) == length


def test_only_summarized_carries_a_budget() -> None:
    assert spoken_budget_chars("full") == 0
    assert spoken_budget_chars("summarized") == SPOKEN_BUDGET_CHARS


# ---------------------------------------------------------------------------
# The trim
# ---------------------------------------------------------------------------


def test_the_tail_is_represented_by_its_closing_sentence() -> None:
    assert summarize_tail(["Some detail.", "More detail.", CLOSING]) == CLOSING


def test_an_empty_tail_says_nothing() -> None:
    """Nothing was held back, or nothing held back was sayable."""
    assert summarize_tail([]) == ""
    assert summarize_tail(["   ", "\n"]) == ""


def test_a_paragraph_chunk_is_reduced_to_its_last_sentence() -> None:
    """Adapters yield "sentences"; reading a paragraph would defeat the setting."""
    assert summarize_tail([f"First. Second. {CLOSING}"]) == CLOSING


def test_markdown_chrome_is_stripped_from_the_closing_line() -> None:
    assert summarize_tail([f"- **{CLOSING}**"]) == CLOSING


def test_the_summary_never_invents_words() -> None:
    """Summarized speech only removes words. It cannot make the agent say
    something the agent did not say."""
    source = f"- Heading stuff. {CLOSING}"
    for word in summarize_tail([source]).split():
        assert word in source


def test_a_run_on_closing_sentence_is_cut_at_a_clause() -> None:
    line = "I went through the handler, " + "and then checked every caller " * 20 + "."
    trimmed = summarize_tail([line])
    assert trimmed == "I went through the handler,"


def test_a_run_on_with_no_clause_boundary_is_cut_on_a_word() -> None:
    line = "I went through every caller " * 30 + "."
    trimmed = summarize_tail([line])
    assert len(trimmed) <= SPOKEN_BUDGET_CHARS
    assert trimmed.endswith(".")
    assert not trimmed.endswith(" .")
    assert line.startswith(trimmed[:-1])


# ---------------------------------------------------------------------------
# The budget guard on the cascade path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_speaks_every_sentence() -> None:
    spoken, _ctx = await _turn(*LONG_ANSWER, reply_length="full")
    assert spoken == list(LONG_ANSWER)


@pytest.mark.asyncio
async def test_summarized_speaks_the_opening_then_one_closing_line() -> None:
    spoken, _ctx = await _turn(*LONG_ANSWER, reply_length="summarized")
    assert spoken[0] == LEDE, "the answer itself is never held back"
    assert spoken[-1] == CLOSING, "the outcome still lands"
    assert len(spoken) < len(LONG_ANSWER), "the middle was held back"
    assert set(spoken) <= set(LONG_ANSWER), "nothing spoken was invented"


@pytest.mark.asyncio
async def test_summarized_keeps_the_opening_verbatim_up_to_the_budget() -> None:
    spoken, _ctx = await _turn(*LONG_ANSWER, reply_length="summarized")
    # Every line before the closing one is the agent's own, in order.
    assert spoken[:-1] == list(LONG_ANSWER[: len(spoken) - 1])
    assert sum(len(line) for line in spoken[:-1]) >= SPOKEN_BUDGET_CHARS


@pytest.mark.asyncio
async def test_a_short_answer_is_untouched_by_summarized() -> None:
    """Under the budget the setting is a no-op — brevity never makes a good
    short answer worse."""
    short = (LEDE, CLOSING)
    assert (await _turn(*short, reply_length="summarized"))[0] == list(short)
    assert (await _turn(*short, reply_length="full"))[0] == list(short)


@pytest.mark.asyncio
async def test_one_enormous_sentence_is_still_spoken() -> None:
    """The budget is checked before a sentence is counted, so the first one
    always goes out: summarized must never silence an answer entirely."""
    huge = "It went fine and here is why. " * 40
    spoken, _ctx = await _turn(huge, reply_length="summarized")
    assert spoken == [huge]


@pytest.mark.asyncio
async def test_the_transcript_keeps_the_whole_answer() -> None:
    """The setting governs the mouth, not the record. A held-back sentence is
    still readable, and the closing line is not double-counted."""
    spoken, ctx = await _turn(*LONG_ANSWER, reply_length="summarized")
    assert _transcript(ctx) == list(LONG_ANSWER)
    assert len(spoken) < len(_transcript(ctx))


# ---------------------------------------------------------------------------
# Phrase cuts on live calls
# ---------------------------------------------------------------------------

# A Claude Code-shaped answer: long sentences, so live calls cut them into
# phrases at word boundaries rather than waiting for the period.
PHRASED_ANSWER = (
    "I looked into the failing call test and found the problem. "
    "The settings page was saving the voice before the provider had finished "
    "loading its catalog. "
    "I moved the save behind the catalog load so that it waits for the "
    "provider, and added a test for it. "
    "Everything in the suite passes now, and the fix is committed on the "
    "branch so you can try it on your next call."
)
PHRASED_CLOSING = (
    "Everything in the suite passes now, and the fix is committed on the "
    "branch so you can try it on your next call."
)


def _phrased(text: str) -> list[str]:
    """What a Snappy live call's streamer hands the runtime for ``text``."""
    from sleeptalk.streaming import (
        LOCAL_PHRASE_STREAMING,
        SNAPPY_PHRASE_STREAMING,
        SentenceStreamer,
    )

    local = LOCAL_PHRASE_STREAMING.set(True)
    snappy = SNAPPY_PHRASE_STREAMING.set(True)
    try:
        streamer = SentenceStreamer()
        chunks = [
            chunk
            for start in range(0, len(text), 7)
            for chunk in streamer.feed(text[start : start + 7])
        ]
        tail = streamer.flush()
        return chunks + ([tail] if tail else [])
    finally:
        SNAPPY_PHRASE_STREAMING.reset(snappy)
        LOCAL_PHRASE_STREAMING.reset(local)


def test_the_closing_sentence_survives_a_phrase_cut() -> None:
    """Regression: the last phrase of a cut sentence was spoken as if it
    were the whole closing line — "the branch so you can try it"."""
    assert summarize_tail(
        [
            "Everything in the suite passes now, and the fix is committed on",
            "the branch so you can try it on your next call.",
        ]
    ) == PHRASED_CLOSING


@pytest.mark.asyncio
async def test_a_phrased_answer_closes_on_a_whole_sentence() -> None:
    chunks = _phrased(PHRASED_ANSWER)
    assert len(chunks) > 4, "the fixture must actually be phrase-cut"
    spoken, _ctx = await _turn(*chunks, reply_length="summarized")
    assert spoken[-1] == PHRASED_CLOSING


@pytest.mark.asyncio
async def test_the_mouth_never_stops_mid_sentence() -> None:
    """The budget can run out on a phrase cut; the opening still finishes
    the sentence it is in before the tail takes over."""
    chunks = _phrased(PHRASED_ANSWER)
    spoken, _ctx = await _turn(*chunks, reply_length="summarized")
    opening = " ".join(spoken[:-1])
    assert opening.endswith((".", "!", "?")), opening
    assert PHRASED_ANSWER.startswith(opening)


# ---------------------------------------------------------------------------
# The page shows what was said
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_feed_shows_what_was_said_and_folds_the_full_answer() -> None:
    """The page used to show every written sentence while the voice skipped
    most of them. Now the feed is what the caller heard, the skip marked
    with an ellipsis, and the whole answer is kept for "Full answer"."""
    spoken, ctx = await _turn(*LONG_ANSWER, reply_length="summarized")
    replies = [line for line in _feed(ctx).lines if line.kind == "assistant"]
    shown = " ".join(line.text for line in replies)
    assert shown == " ".join([*spoken[:-1], f"… {CLOSING}"])
    assert [line.full for line in replies][:-1] == [""] * (len(replies) - 1)
    assert replies[-1].full == " ".join(LONG_ANSWER)
    assert replies[-1].to_json()["full"] == " ".join(LONG_ANSWER)


@pytest.mark.asyncio
async def test_a_reply_said_in_full_has_no_fold() -> None:
    _spoken, ctx = await _turn(*LONG_ANSWER, reply_length="full")
    replies = [line for line in _feed(ctx).lines if line.kind == "assistant"]
    assert " ".join(line.text for line in replies) == " ".join(LONG_ANSWER)
    assert not any(line.full for line in replies)
    assert all("full" not in line.to_json() for line in replies)


# ---------------------------------------------------------------------------
# Asking for brevity, not only trimming it
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_summarized_asks_the_agent_for_a_short_answer() -> None:
    """Trimming can only remove sentences. Asking is what stops the agent
    writing the sprawl in the first place — and it shortens the transcript
    and the token bill too, which no post-hoc trim can do."""
    agent = _Agent(LEDE, CLOSING)
    await _turn(reply_length="summarized", agent=agent)
    assert agent.prompts[0].startswith(BRIEF_SPEECH_CONTRACT)


@pytest.mark.asyncio
async def test_full_leaves_the_prompt_alone() -> None:
    agent = _Agent(LEDE, CLOSING)
    await _turn(reply_length="full", agent=agent)
    assert BRIEF_SPEECH_CONTRACT not in agent.prompts[0]


@pytest.mark.asyncio
async def test_the_budget_is_about_three_spoken_sentences() -> None:
    """The backstop matches what the contract asks for, so an agent that
    obeys is never trimmed and an agent that ignores it is."""
    spoken, _ctx = await _turn(*LONG_ANSWER, reply_length="summarized")
    assert len(spoken) <= 5, spoken


async def test_concise_speech_keeps_all_details_without_asking_the_agent_to_write_less():
    from sleeptalk.voice_prompt import CONCISE_SPEECH_CONTRACT

    agent = _Agent(*LONG_ANSWER)
    spoken, ctx = await _turn(*LONG_ANSWER, reply_length='concise', agent=agent)
    assert agent.prompts[0].startswith(CONCISE_SPEECH_CONTRACT)
    assert BRIEF_SPEECH_CONTRACT not in agent.prompts[0]
    assert len(spoken) < len(LONG_ANSWER)
    assert _transcript(ctx) == list(LONG_ANSWER)
    feed = _feed(ctx)
    assert all(detail in ' '.join(line.full or line.text for line in feed.lines) for detail in DETAILS)


async def test_concise_keeps_original_code_and_links_in_the_written_result():
    from sleeptalk.adapter import AssistantDelta

    original = '# Result\n\nI fixed it.\n\n```python\nprint("important code")\n```\n\n[Report](https://example.com/report?q=1)'

    class WithOriginal(_Agent):
        async def respond_streaming(self, prompt, on_event=None):
            self.prompts.append(prompt)
            if on_event:
                on_event(AssistantDelta(text=original))
            yield 'I fixed it.'

    spoken, ctx = await _turn(reply_length='concise', agent=WithOriginal())
    assert spoken == ['I fixed it.']
    assert any(line.full == original for line in _feed(ctx).lines)
