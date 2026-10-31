"""Tool-start interjections: a spoken detail ('Checking new uploads.') beats
the canned kind clip, synthesized once and cached."""


from sleeptalk.voice_tool_narration import _detail_sentence


def test_detail_sentence():
    assert _detail_sentence("checking new uploads") == "Checking new uploads."
    assert _detail_sentence("running git status.") == "Running git status."
    assert _detail_sentence("") == ""


def test_choose_tool_narration_skips_same_key_and_second_generic():
    from sleeptalk.voice_tool_narration import choose_tool_narration

    phrases = {"bash": "Running a command.", "other": "Working on it."}
    spoken: set[str] = set()
    first = choose_tool_narration("bash", "", phrases, spoken, False)
    assert first == ("bash", "Running a command.")
    spoken.add("bash:")
    assert choose_tool_narration("bash", "", phrases, spoken, True) is None
    assert choose_tool_narration("commandExecution", "", phrases, spoken, True) is None
    spoken_detail: set[str] = set()
    detail = choose_tool_narration(
        "bash", "running git status", phrases, spoken_detail, False
    )
    assert detail == ("bash", "Running git status.")
    spoken_detail.add("bash:running git status")
    assert (
        choose_tool_narration("bash", "running git status", phrases, spoken_detail, False)
        is None
    )
