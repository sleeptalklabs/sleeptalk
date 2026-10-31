from sleeptalk.openai_realtime import _tell_instructions
from sleeptalk.routes.hybrid_mouth import Finding, Script
from sleeptalk.routes.hybrid_types import TaskId
from sleeptalk.streaming import SentenceStreamer
from sleeptalk.text import (
    brief_finding,
    extract_sentence,
    mouth_script,
    unwrap_script_transcript,
)


def test_mouth_script_keeps_the_english_and_drops_the_favicon_code() -> None:
    spoken = mouth_script(
        "You have the assets (wordmark, mascot) to make a good one.\n"
        '- **253KB favicon.** `icons: { icon: "/logo.png" }` points at the '
        "1024×1024, 253KB PNG, which is also displayed at 146px in the masthead."
    )
    assert "You have the assets (wordmark, mascot) to make a good one." in spoken
    assert "253KB favicon" in spoken
    assert "logo.png" not in spoken
    assert "{" not in spoken


def test_mouth_script_strips_the_tokens_that_made_realtime_haywire() -> None:
    spoken = mouth_script(
        "ts:3 and appears twice on the page (masthead + CTA) — and it's baked "
        "into `out/index.html`, so if that export deploys, visitors copy gibberish."
    )
    assert "ts:3" not in spoken
    assert "index.html" not in spoken
    assert "appears twice on the page" in spoken
    leftover = mouth_script(
        "`curl -asddsdsds.com` is in `lib/content.ts:3` and appears twice"
    )
    assert "asddsdsds" not in leftover
    assert "content.ts" not in leftover
    assert leftover == "is in and appears twice"


def test_mouth_script_keeps_english_after_mid_token_markdown() -> None:
    spoken = mouth_script("PT`. 2. **The domain itself is a placeholder.**")
    assert "The domain itself is a placeholder." in spoken
    assert "`" not in spoken


def test_finding_from_agent_yield_skips_code_soup() -> None:
    task = TaskId("task-1")
    assert (
        Finding.from_agent_yield(task, '`: { icon: "/logo.png" }`', 2)
        is None
    )
    found = Finding.from_agent_yield(
        task,
        "It's a well-crafted little site. The code is unusually disciplined.",
        2,
    )
    assert found is not None
    assert "well-crafted little site" in found.body


def test_extract_sentence_does_not_split_on_file_extensions() -> None:
    sentence, remainder = extract_sentence(
        "curl is in lib/content.ts:3 and appears twice on the page."
    )
    assert sentence == "curl is in lib/content.ts:3 and appears twice on the page."
    assert remainder == ""
    sentence, remainder = extract_sentence("First one. Then two.")
    assert sentence == "First one."
    assert remainder == "Then two."


FIGMA_TELL = (
    "every value traces back to the Figma mock — but that's also its core "
    "problem: it's a faithful tracing of a mock, not yet a design system. "
    "The tokens in globals.css:36-52 are things like 29.48px, 29.62px, "
    "17.75px, a 1.12px hairline, and --fs-tagline: 29.48px versus --fs-nav: "
    "29.62px."
)


def test_mouth_script_drops_the_figma_css_that_made_realtime_haywire() -> None:
    spoken = mouth_script(FIGMA_TELL)
    assert "Figma mock" in spoken
    assert "core problem" in spoken
    assert "29.48px" not in spoken
    assert "1.12px" not in spoken
    assert "--fs-tagline" not in spoken
    assert "globals.css" not in spoken


def test_unwrap_script_transcript_takes_the_inner_line() -> None:
    assert unwrap_script_transcript('{"script": "I\'ll pull that up."}') == (
        "I'll pull that up."
    )
    assert unwrap_script_transcript("I'll pull that up.") == "I'll pull that up."


def test_tell_instructions_do_not_ask_the_model_to_speak_json() -> None:
    text = _tell_instructions(Script("I'll pull that up.", "ack"))
    assert '{"script": "I\'ll pull that up."}' in text
    assert "JSON string" not in text


def test_sentence_streamer_holds_dotted_identifiers() -> None:
    streamer = SentenceStreamer()
    assert streamer.feed("Sure.") == ["Sure."]
    assert streamer.feed(
        "all copy centralized in `lib/content.ts`, design tokens derived "
        "from the Figma mock"
    ) == []
    assert streamer.feed(" with the math documented in comments.") == [
        "all copy centralized in lib/content.ts, design tokens derived "
        "from the Figma mock with the math documented in comments."
    ]


def test_a_finding_keeps_what_the_specialist_actually_found() -> None:
    """A finding is data, not a script.

    Reported live: the mouth ran grep and then said it could not determine
    the outcome from the findings it had. It was right — the spoken-word
    filter had deleted every path out of them before the model saw them.
    """
    found = "Found 2 hits: routes/hybrid.py:925 and tests/test_hybrid_route.py:982."

    assert mouth_script(found) == "Found 2 hits: and"  # what it used to send
    assert brief_finding(found) == found


def test_a_finding_unwraps_inline_code_rather_than_deleting_it() -> None:
    """The identifier in the backticks is usually the whole answer."""
    assert (
        brief_finding("The `_capture_held` flag is set in `_run_specialist`.")
        == "The _capture_held flag is set in _run_specialist."
    )


def test_a_finding_still_drops_a_block_with_no_prose_in_it() -> None:
    assert brief_finding("```python\ndef f():\n    return 1\n```") == ""
    assert brief_finding("   ") == ""


def test_a_finding_drops_markdown_chrome_but_keeps_the_sentence() -> None:
    assert (
        brief_finding("## Summary\n- **Done**: reply_length reaches hybrid now.")
        == "Summary Done: reply_length reaches hybrid now."
    )


def test_a_terse_finding_is_not_mistaken_for_soup() -> None:
    """The brevity contract asks the specialist for short answers.

    Judging a line by how many plain words it has drops those: "Specialist
    answer." became nothing, the brief stayed empty, and the mouth had
    literally no answer to give. Letters, not words.
    """
    assert brief_finding("Specialist answer.") == "Specialist answer."
    assert brief_finding("Found two matches.") == "Found two matches."


def test_a_path_among_braces_is_still_somebody_else_s_code() -> None:
    assert brief_finding('`: { icon: "/logo.png" }`') == ""


def test_chinese_sentences_split_on_full_width_punctuation():
    from sleeptalk.text import extract_sentence

    assert extract_sentence("好的。我来看一下这个问题。") == ("好的。", "我来看一下这个问题。")
    assert extract_sentence("真的吗？！”他问。") == ("真的吗？！”", "他问。")
    # Half-width punctuation straight into Chinese, as people type it.
    assert extract_sentence("好的!我来看看") == ("好的!", "我来看看")
    # A dotted file name inside Chinese is not a sentence end.
    assert extract_sentence("我看了main.py文件，问题在第12行。接下来") == (
        "我看了main.py文件，问题在第12行。", "接下来",
    )
    assert extract_sentence("还在想") == ("", "还在想")


def test_long_chinese_opening_flushes_at_a_clause():
    from sleeptalk.text import extract_first_clause

    long = "这个函数的问题在于它没有处理空列表的情况，所以当输入为空的时候会直接报错"
    assert extract_first_clause(long) == (
        "这个函数的问题在于它没有处理空列表的情况，", "所以当输入为空的时候会直接报错",
    )
    assert extract_first_clause("好，我看看") == ("", "好，我看看")


def test_english_splitting_is_unchanged():
    from sleeptalk.text import extract_first_clause, extract_sentence

    assert extract_sentence("Version 1.2 is out. Next") == ("Version 1.2 is out.", "Next")
    assert extract_sentence("lib/content.ts is fine") == ("", "lib/content.ts is fine")
    assert extract_first_clause("short, text") == ("", "short, text")
