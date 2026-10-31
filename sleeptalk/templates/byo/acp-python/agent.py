#!/usr/bin/env python3
"""A minimal Agent Client Protocol (ACP) agent for Sleeptalk.

If your agent already speaks ACP (Gemini CLI, Goose, Kiro, Qwen Code, Cline,
and others), you don't need this file: point [agent.options].command at it
and set protocol = "acp".

Use this template when you want your own agent to speak ACP so it also works
in editors and other ACP clients. If Sleeptalk is the only client, the
Bridge Lite command template is simpler.

ACP is JSON-RPC 2.0, one message per line on stdin/stdout. Log to stderr.
The only function you need to change is `reply`.

Guide: docs/bring-your-own-agent.md in the Sleeptalk repository.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import sys
import threading
import uuid


async def reply(turn: "Turn", text: str) -> None:
    """Answer one prompt. Replace the demo below with your agent.

    turn.say(text)                         stream reply text
    turn.thought(text)                     stream reasoning (Sleeptalk plays a filler)
    turn.tool_started(id, title, kind)     report tool work so Sleeptalk can narrate it
    turn.tool_finished(id, ok=True)
    await turn.ask_permission(id, title)   True when the caller allows it
    turn.cancelled                         True once the caller interrupts
    """

    # --- Replace from here with your model call. ---
    words = text.strip().lower()
    turn.thought("Thinking.")
    if words == "slow":
        for number in ("One.", "Two.", "Three.", "Four.", "Five."):
            if turn.cancelled:
                return
            turn.say(number + " ")
            await asyncio.sleep(0.3)
    elif words == "hang":
        while not turn.cancelled:
            await asyncio.sleep(0.05)
    elif words == "tool":
        turn.tool_started("t1", "read README.md", "read")
        await asyncio.sleep(0.05)
        turn.tool_finished("t1")
        turn.say("The readme says hello.")
    elif words == "permission":
        allowed = await turn.ask_permission("t2", "delete the build folder")
        turn.say("Deleted it." if allowed else "Okay, I left it alone.")
    else:
        turn.say(f"You said: {text.strip()}.")
    # --- to here. ---


# ---------------------------------------------------------------------------
# Protocol plumbing. You shouldn't need to edit below this line.
# ---------------------------------------------------------------------------

_write_lock = threading.Lock()
_ids = itertools.count(1)
_pending: dict[object, asyncio.Future] = {}


def write(message: dict) -> None:
    with _write_lock:
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", **message}) + "\n")
        sys.stdout.flush()


class Turn:
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.cancelled = False

    def _update(self, **update: object) -> None:
        write({"method": "session/update", "params": {"sessionId": self.session_id, "update": update}})

    def say(self, text: str) -> None:
        if text and not self.cancelled:
            self._update(sessionUpdate="agent_message_chunk", content={"type": "text", "text": text})

    def thought(self, text: str) -> None:
        self._update(sessionUpdate="agent_thought_chunk", content={"type": "text", "text": text})

    def tool_started(self, call_id: str, title: str, kind: str = "other") -> None:
        self._update(sessionUpdate="tool_call", toolCallId=call_id, title=title, kind=kind, status="pending")

    def tool_finished(self, call_id: str, ok: bool = True) -> None:
        self._update(sessionUpdate="tool_call_update", toolCallId=call_id, status="completed" if ok else "failed")

    async def ask_permission(self, call_id: str, title: str) -> bool:
        request_id = f"perm-{next(_ids)}"
        future = asyncio.get_running_loop().create_future()
        _pending[request_id] = future
        write({
            "id": request_id,
            "method": "session/request_permission",
            "params": {
                "sessionId": self.session_id,
                "toolCall": {"toolCallId": call_id, "title": title},
                "options": [
                    {"optionId": "allow_once", "name": "Allow once", "kind": "allow_once"},
                    {"optionId": "reject_once", "name": "Deny", "kind": "reject_once"},
                ],
            },
        })
        try:
            result = await future
        finally:
            _pending.pop(request_id, None)
        outcome = (result or {}).get("outcome") or {}
        return outcome.get("outcome") == "selected" and str(outcome.get("optionId", "")).startswith("allow")


async def main() -> None:
    loop = asyncio.get_running_loop()
    inbox: asyncio.Queue[dict | None] = asyncio.Queue()

    def read_stdin() -> None:
        for line in sys.stdin:
            if line.strip():
                try:
                    loop.call_soon_threadsafe(inbox.put_nowait, json.loads(line))
                except json.JSONDecodeError:
                    print("ignored non-JSON input", file=sys.stderr)
        loop.call_soon_threadsafe(inbox.put_nowait, None)

    threading.Thread(target=read_stdin, daemon=True).start()
    turns: dict[str, Turn] = {}

    async def run_prompt(request_id: object, turn: Turn, text: str) -> None:
        try:
            await reply(turn, text)
        except Exception as exc:
            print(f"prompt failed: {exc!r}", file=sys.stderr)
            write({"id": request_id, "error": {"code": -32000, "message": str(exc)}})
            return
        write({"id": request_id, "result": {"stopReason": "cancelled" if turn.cancelled else "end_turn"}})

    while (message := await inbox.get()) is not None:
        method = message.get("method")
        params = message.get("params") or {}
        request_id = message.get("id")
        if method is None and request_id in _pending:  # a reply to our request
            future = _pending[request_id]
            if not future.done():
                future.set_result(message.get("result"))
        elif method == "initialize":
            write({"id": request_id, "result": {"protocolVersion": 1, "agentCapabilities": {"loadSession": False}}})
        elif method == "session/new":
            session_id = uuid.uuid4().hex
            turns[session_id] = Turn(session_id)
            write({"id": request_id, "result": {"sessionId": session_id}})
        elif method == "session/prompt":
            session_id = str(params.get("sessionId"))
            turn = turns[session_id] = Turn(session_id)
            text = " ".join(part.get("text", "") for part in params.get("prompt", []) if isinstance(part, dict))
            asyncio.create_task(run_prompt(request_id, turn, text))
        elif method == "session/cancel":
            turn = turns.get(str(params.get("sessionId")))
            if turn is not None:
                turn.cancelled = True
            for future in list(_pending.values()):
                if not future.done():
                    future.set_result({"outcome": {"outcome": "cancelled"}})
        elif request_id is not None:
            write({"id": request_id, "error": {"code": -32601, "message": f"method not found: {method}"}})


if __name__ == "__main__":
    asyncio.run(main())
