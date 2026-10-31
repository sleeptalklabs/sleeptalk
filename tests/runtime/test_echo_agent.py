"""Echo agent: a new stdio agent from a command + vocab + registration ONLY.

This is the executable proof of the streaming-layer end state: no new
streaming or lifecycle code — the whole adapter is a StdioAgentSpec fed to
GenericStdioAgent, registered like any other agent kind.
"""

import json
import sys

import pytest

from sleeptalk.registries import AGENTS
from sleeptalk.streaming import (
    GenericStdioAgent,
    StdioAgentSpec,
    ToolVocab,
    TurnEvent,
)

# The "agent": a python one-liner speaking a trivial NDJSON protocol.
# Reads one {"prompt": ...} line; emits thinking, text deltas, a tool call,
# and a terminal event — then keeps the line open like a real long-lived
# agent process.
_ECHO_SCRIPT = r"""
import json, sys
def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    prompt = json.loads(line).get("prompt", "")
    emit({"type": "thinking", "text": "hmm"})
    emit({"type": "delta", "text": "You said: " + prompt + ". "})
    emit({"type": "delta", "text": "That is all."})
    emit({"type": "tool", "name": "shell", "args": {"command": "echo hello"}})
    emit({"type": "done"})
"""

# The kind-vocab table: raw tool name -> narration kind + subject arg.
ECHO_VOCAB = ToolVocab(
    kinds={"shell": "bash"},
    subject_args={"bash": ("command",)},
)


def _translate(message):
    etype = message.get("type")
    if etype == "thinking":
        return TurnEvent(kind="thinking")
    if etype == "delta":
        return TurnEvent(kind="text", text=str(message.get("text", "")))
    if etype == "tool":
        return TurnEvent(
            kind="tool",
            tool_name=str(message.get("name", "")),
            tool_args=message.get("args"),
        )
    if etype == "done":
        return TurnEvent(kind="other", terminal=True)
    return TurnEvent(kind="other")


ECHO_SPEC = StdioAgentSpec(
    args=(sys.executable, "-u", "-c", _ECHO_SCRIPT),
    make_prompt=lambda text: {"prompt": text},
    translate=_translate,
    vocab=ECHO_VOCAB,
)


def _build_echo_agent(config, env):
    return GenericStdioAgent(ECHO_SPEC, cwd="/tmp", name="echo")


# The registration — the third and last ingredient. Registered for this
# module only so the fixture agent never leaks into other suites.
@pytest.fixture(autouse=True, scope="module")
def _register_echo_agent():
    AGENTS.register(
        "echo",
        _build_echo_agent,
        binary=sys.executable,
        install_hint="echo fixture needs only python3",
        replace=True,
    )
    yield
    AGENTS._registrations.pop("echo", None)


@pytest.mark.asyncio
async def test_echo_agent_streams_via_shared_layer():
    agent = AGENTS.build("echo", None, {})
    assert isinstance(agent, GenericStdioAgent)
    try:
        seen: list[str] = []
        sentences: list[str] = []
        async for sentence in agent.respond_streaming("hi there", on_event=seen.append):
            sentences.append(sentence)
        assert sentences == ["You said: hi there.", "That is all."]
        assert seen == [
            "thinking",
            "text",
            "tool",
            "tooldetail:running echo hello",
            "tool:bash",
        ]
        # Full-turn respond() normalizes through the same speakable path.
        assert await agent.respond("again") == "You said: again. That is all."
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_echo_agent_times_out_when_silent():
    silent_spec = StdioAgentSpec(
        args=(sys.executable, "-u", "-c", "import time\ntime.sleep(3600)"),
        make_prompt=lambda text: {"prompt": text},
        translate=_translate,
        vocab=ECHO_VOCAB,
    )
    agent = GenericStdioAgent(silent_spec, cwd="/tmp", name="echo-silent")
    try:
        with pytest.raises(RuntimeError, match="timed out"):
            await agent.respond("hi", timeout=0.2)
    finally:
        await agent.close()
