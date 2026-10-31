"""narrate_tool + ToolVocab: gerund tool narration driven by vocab tables.

Cases ported from the four adapters' tool-event tests (test_pi_rpc,
test_hermes_adapter, test_hermes_gateway_adapter, test_claude_code_adapter,
test_progress_narration). The vocab instances here mirror what each adapter
declares beside its registration.
"""

from sleeptalk.streaming import ToolVocab, narrate_tool

# The four adapters' vocab tables, as they will live beside each adapter.

PI_VOCAB = ToolVocab(
    kinds={
        "bash": "bash", "shell": "bash", "run": "bash",
        "read": "read", "write": "write", "edit": "edit",
        "search": "search", "web_search": "search", "websearch": "search",
    },
    subject_args={
        "bash": ("command",),
        "read": ("path", "file_path"),
        "write": ("path", "file_path"),
        "edit": ("path", "file_path"),
        "search": ("query",),
    },
    verbs={
        "bash": "running", "read": "reading", "write": "writing",
        "edit": "editing", "search": "searching the web for",
    },
)

HERMES_ACP_VOCAB = ToolVocab(
    kinds={
        "execute": "bash", "read": "read", "edit": "edit", "delete": "edit",
        "move": "edit", "search": "search", "fetch": "search", "think": "other",
    },
    fallback_to_subject=True,
)

GATEWAY_VOCAB = ToolVocab(
    kinds={
        "terminal": "bash", "bash": "bash",
        "read_file": "read", "write_file": "write", "edit_file": "edit",
        "web_search": "search", "search": "search", "web_extract": "search",
    },
    subject_args={
        "bash": ("command",),
        "read": ("path", "file_path"),
        "write": ("path", "file_path"),
        "edit": ("path", "file_path"),
        "search": ("query",),
    },
)

from sleeptalk.agents.claude_code_adapter import TOOL_VOCAB as CLAUDE_VOCAB
from sleeptalk.agents.codex_app_server import TOOL_VOCAB as CODEX_VOCAB


# ---- pi (ported from test_progress_narration) ------------------------------


def test_pi_bash():
    assert narrate_tool("bash", {"command": "git status"}, PI_VOCAB) == (
        "bash", "running git status",
    )


def test_pi_paths_use_basename():
    assert narrate_tool("read", {"path": "/repo/server/app/main.py"}, PI_VOCAB)[1] == "reading main.py"
    assert narrate_tool("edit", {"path": "app/main.py"}, PI_VOCAB)[1] == "editing main.py"
    assert narrate_tool("write", {"file_path": "out.txt"}, PI_VOCAB)[1] == "writing out.txt"


def test_pi_search_query():
    assert narrate_tool(
        "web_search", {"query": "overnight market news"}, PI_VOCAB
    ) == ("search", "searching the web for overnight market news")


def test_pi_empty_or_unknown():
    assert narrate_tool("bash", {}, PI_VOCAB) == ("bash", "")
    assert narrate_tool("think", {"thoughts": "hmm"}, PI_VOCAB) == ("other", "")
    assert narrate_tool("bash", None, PI_VOCAB) == ("bash", "")


def test_pi_truncates_long_commands():
    kind, detail = narrate_tool("bash", {"command": "x" * 200}, PI_VOCAB)
    assert kind == "bash"
    assert detail.startswith("running x")
    assert len(detail) <= 79


# ---- hermes ACP (ported from test_hermes_adapter) ---------------------------
# ACP narrates from the tool_call title (a pre-extracted subject string),
# with the raw ACP kind mapped through the vocab.


def test_hermes_acp_titles():
    assert narrate_tool("execute", "git status", HERMES_ACP_VOCAB) == (
        "bash", "running git status",
    )
    assert narrate_tool("search", "overnight news", HERMES_ACP_VOCAB) == (
        "search", "searching for overnight news",
    )
    assert narrate_tool("read", "main.py", HERMES_ACP_VOCAB) == ("read", "reading main.py")
    # Unknown kind: the title is narrated verbatim (fallback_to_subject).
    assert narrate_tool("think", "Thinking", HERMES_ACP_VOCAB) == ("other", "Thinking")
    assert narrate_tool("execute", "", HERMES_ACP_VOCAB) == ("bash", "")


# ---- hermes gateway (ported from test_hermes_gateway_adapter) ---------------


def test_gateway_mapping():
    assert narrate_tool("web_search", {"query": "btc price"}, GATEWAY_VOCAB) == (
        "search", "searching for btc price",
    )
    assert narrate_tool("read_file", {"path": "/a/b/main.py"}, GATEWAY_VOCAB) == (
        "read", "reading main.py",
    )
    assert narrate_tool("memory", {}, GATEWAY_VOCAB) == ("other", "")
    kind, detail = narrate_tool("terminal", {"command": "cd /repo && git status"}, GATEWAY_VOCAB)
    assert kind == "bash"
    assert detail.startswith("running") and "/" not in detail


# ---- claude code (ported from test_claude_code_adapter) ---------------------


def test_claude_mapping():
    assert narrate_tool("Bash", {"command": "ls /Users/x/y"}, CLAUDE_VOCAB) == (
        "bash", "running ls y",
    )
    assert narrate_tool("Read", {"file_path": "/a/b/main.py"}, CLAUDE_VOCAB) == (
        "read", "reading main.py",
    )
    assert narrate_tool("WebSearch", {"query": "btc price"}, CLAUDE_VOCAB) == (
        "search", "searching for btc price",
    )
    assert narrate_tool("Task", {"description": "Research overnight news"}, CLAUDE_VOCAB) == (
        "task", "starting Research overnight news",
    )
    # claude's search also takes a pattern (grep/glob).
    assert narrate_tool("Grep", {"pattern": "TODO"}, CLAUDE_VOCAB) == (
        "search", "searching for TODO",
    )


def test_codex_command_execution_is_bash():
    assert narrate_tool(
        "commandExecution", {"command": "git status"}, CODEX_VOCAB
    ) == ("bash", "running git status")


def test_codex_file_change_uses_basename():
    assert narrate_tool(
        "fileChange", {"path": "/repo/sleeptalk/text.py"}, CODEX_VOCAB
    )[1] == "editing text.py"


def test_codex_mcp_call_speaks_tool_name():
    kind, detail = narrate_tool(
        "mcpToolCall", {"tool": "web_search"}, CODEX_VOCAB
    )
    assert kind == "mcp"
    assert "web search" in detail or "web_search" in detail.replace("_", " ")


# ---- MCP tools (any adapter) -------------------------------------------------


def test_mcp_tool_names_narrate_from_the_tool_name():
    """mcp__<server>__<tool> -> kind 'mcp' + a gerund phrase derived from the
    tool name, so every MCP server gets tailored narration config-free."""
    assert narrate_tool("mcp__research__check_new_uploads", {}, CLAUDE_VOCAB) == (
        "mcp", "checking new uploads",
    )
    assert narrate_tool("mcp__research__analyze_youtube_video", {}, CLAUDE_VOCAB) == (
        "mcp", "analyzing youtube video",
    )
    assert narrate_tool("mcp__research__deep_research", {}, CLAUDE_VOCAB) == (
        "mcp", "researching",
    )
    assert narrate_tool("mcp__x__scrape_url", {}, PI_VOCAB) == ("mcp", "scraping url")
