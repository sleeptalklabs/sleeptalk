"""Progress narration: spoken tool detail, live progress lines, status queries."""

from sleeptalk.streaming import elapsed_suffix as _elapsed_suffix
from sleeptalk.discord.session_helpers import (
    _STATUS_QUERY_RE,
    _is_status_query,
)
from sleeptalk.streaming import (
    compose_progress_line as _compose_progress_line,
    next_reassure_line as _next_reassure_line,
)
from sleeptalk.agents.pi_rpc import tool_detail_from_call


def test_tool_detail_from_call_bash():
    assert tool_detail_from_call("bash", {"command": "git status"}) == "running git status"


def test_tool_detail_from_call_paths_use_basename():
    assert tool_detail_from_call("read", {"path": "/repo/server/app/main.py"}) == "reading main.py"
    assert tool_detail_from_call("edit", {"path": "app/main.py"}) == "editing main.py"
    assert tool_detail_from_call("write", {"file_path": "out.txt"}) == "writing out.txt"


def test_tool_detail_from_call_search_query():
    assert (
        tool_detail_from_call("web_search", {"query": "overnight market news"})
        == "searching the web for overnight market news"
    )


def test_tool_detail_from_call_empty_or_unknown():
    assert tool_detail_from_call("bash", {}) == ""
    assert tool_detail_from_call("think", {"thoughts": "hmm"}) == ""
    assert tool_detail_from_call("bash", None) == ""


def test_tool_detail_truncates_long_commands():
    detail = tool_detail_from_call("bash", {"command": "x" * 200})
    assert detail.startswith("running x")
    assert len(detail) <= 79


def test_compose_progress_line_with_detail():
    assert _compose_progress_line("running git status", 5) == "Still running git status."
    assert (
        _compose_progress_line("searching the web for news", 75)
        == "Still searching the web for news — about 1 minute in."
    )
    assert (
        _compose_progress_line("running make", 130)
        == "Still running make — about 2 minutes in."
    )


def test_compose_progress_line_without_detail():
    assert _compose_progress_line("", 300) == ""


def test_elapsed_suffix():
    assert _elapsed_suffix(59) == ""
    assert _elapsed_suffix(60) == " — about 1 minute in"
    assert _elapsed_suffix(179) == " — about 2 minutes in"


def test_status_query_regex():
    for phrase in (
        "What are you doing?",
        "what's taking so long",
        "are you still working",
        "Are you stuck?",
        "how's it going",
        "any progress",
        "status update",
        "what's the hold up",
        "where are you at",
    ):
        assert _STATUS_QUERY_RE.match(phrase), phrase


def test_status_query_regex_rejects_real_prompts():
    for phrase in (
        "commit the work",
        "what did you change in main.py",
        "are you sure about that",
        "how does the adapter work",
        "stop",
        "yes",
    ):
        assert not _STATUS_QUERY_RE.match(phrase), phrase


from sleeptalk.text import is_hearable, speakable_subject


def test_speakable_subject_collapses_paths_to_basenames():
    assert speakable_subject("ls /Users/alex/Documents/myapp") == "ls myapp"
    assert speakable_subject("cat ./server/app/main.py") == "cat main.py"
    assert speakable_subject("git -C /repo status") == "git status"


def test_speakable_subject_takes_first_real_segment():
    assert speakable_subject("pytest -x -q && git status") == "pytest"
    assert speakable_subject("ls -la /tmp > out.txt") == "ls"
    assert speakable_subject("cd /repo/server && uv run pytest") == "uv run pytest"


def test_speakable_subject_never_reads_code_payloads():
    assert speakable_subject('python3 -c "import urllib; send_tts()"') == "a python snippet"
    assert speakable_subject("python3 -c 'print(1)'") == "a python snippet"
    assert speakable_subject("python3 tools/memory.py save hi") == "memory.py"


def test_speakable_subject_drops_underscores():
    assert speakable_subject("bash scripts/run_tests.sh") == "bash run tests.sh"


def test_speakable_subject_plain_text_unchanged():
    assert speakable_subject("overnight market news") == "overnight market news"


def test_tool_detail_never_speaks_full_paths():
    detail = tool_detail_from_call("bash", {"command": "cd /Users/alex/Documents/myapp && git status"})
    assert "/" not in detail
    assert "alex" not in detail


def test_hearability_gate():
    # Natural lines pass
    assert is_hearable("Still running git status — about a minute in.")
    assert is_hearable("Still reading main.py.")
    assert is_hearable("Hang tight, still going.")
    # Machine syntax never reaches TTS
    assert not is_hearable("Still running cd /Users/alex/Documents/myapp")
    assert not is_hearable("Still running python3 -c import urllib_request")
    assert not is_hearable("Still editing app.pi_discord_bot")  # underscore
    assert not is_hearable("See https://example.com/x")
    assert not is_hearable("Still running make --flag=value")
    assert not is_hearable("")


def test_reassure_lines_never_repeat_within_turn():
    used: set[str] = set()
    lines = []
    for _ in range(30):
        line = _next_reassure_line("bash", "running make", 5.0, used)
        if not line:
            break
        used.add(line)
        lines.append(line)
    assert len(lines) > 8  # dynamic + 4 bash + 10 generic
    assert len(lines) == len(set(lines))  # zero repeats
    assert lines[0] == "Still running make."  # live progress leads


def test_reassure_lines_exhaustion_returns_empty():
    used: set[str] = set()
    while True:
        line = _next_reassure_line([], [], 5.0, used)
        if not line:
            break
        used.add(line)
    assert _next_reassure_line([], [], 5.0, used) == ""


def test_reassure_dynamic_line_varies_by_elapsed():
    used = {"Still running make."}
    line = _next_reassure_line("bash", "running make", 75.0, used)
    assert line == "Still running make — about 1 minute in."


def test_status_query_with_greeting_wrappers():
    assert _is_status_query("hello? are you there? what's going on")
    assert _is_status_query("hey, what are you doing")
    assert _is_status_query("um so what's taking so long")
    assert not _is_status_query("commit the work")
    assert not _is_status_query("yes")
