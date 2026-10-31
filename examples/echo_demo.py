"""Clean-machine smoke: boot a sleeptalk session with a null transport and
a toy stdio agent, then hold one text turn through the full agent seam.

Run: python echo_demo.py  (from this directory, package installed)
"""

import asyncio
import json
import sys

from sleeptalk.builder import build_session
from sleeptalk.config import load_config
from sleeptalk.registries import AGENTS
from sleeptalk.streaming import GenericStdioAgent, StdioAgentSpec, ToolVocab, TurnEvent

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

ECHO_SPEC = StdioAgentSpec(
    args=(sys.executable, "-u", "-c", _ECHO_SCRIPT),
    make_prompt=lambda text: {"prompt": text},
    translate=lambda m: (
        TurnEvent(kind="thinking") if m.get("type") == "thinking"
        else TurnEvent(kind="text", text=str(m.get("text", ""))) if m.get("type") == "delta"
        else TurnEvent(kind="tool", tool_name=str(m.get("name", "")), tool_args=m.get("args"))
        if m.get("type") == "tool"
        else TurnEvent(kind="other", terminal=m.get("type") == "done")
    ),
    vocab=ToolVocab(kinds={"shell": "bash"}, subject_args={"bash": ("command",)}),
)

AGENTS.register(
    "echo",
    lambda config, env: GenericStdioAgent(ECHO_SPEC, name="echo"),
    binary=sys.executable,
    install_hint="echo demo needs only python3",
)


async def main() -> None:
    config = load_config("echo_demo.toml", check_env=False)  # demo never calls the providers
    session = build_session(config)
    events: list[str] = []
    reply = await session.agent.respond("hello clean machine", timeout=15.0)
    async def _collect():
        async for sentence in session.agent.respond_streaming("again", on_event=events.append):
            pass
    await _collect()
    await session.agent.close()
    print("reply:", reply)
    print("events:", events)
    await session.transport.run(session)
    assert reply.startswith("You said: hello clean machine."), reply
    assert "tooldetail:running echo hello" in events
    print("CLEAN-MACHINE ACCEPTANCE: OK")


if __name__ == "__main__":
    asyncio.run(main())
