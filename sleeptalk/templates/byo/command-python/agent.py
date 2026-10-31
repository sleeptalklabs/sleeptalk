#!/usr/bin/env python3
"""A Sleeptalk agent that speaks Bridge Lite over stdio.

Sleeptalk starts this program and writes one JSON object per line to stdin.
Write one JSON object per line to stdout. Print logs to stderr, never stdout.

The only function you need to change is `reply`. Everything else is the
protocol, and it already passes `sleeptalk adapter test --fixture`.

Guide: docs/bring-your-own-agent.md in the Sleeptalk repository.
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading

# Advertise only what you actually send. Remove a line if you don't use it.
CAPABILITIES = {
    "structured_events": "supported",
    "tool_lifecycle": "supported",
    "permissions": "supported",
    "clarification": "supported",
    "steering": "supported",
    "status": "supported",
    "usage": "supported",
}


async def reply(turn: "Turn", text: str) -> None:
    """Answer one spoken turn. Replace the demo below with your agent.

    turn.say(text)            stream reply text (any size; Sleeptalk splits sentences)
    turn.tool(name, detail)   report tool work so Sleeptalk can narrate it
    await turn.ask_permission(prompt) -> "deny" | "allow_once" | "allow_session"
    turn.aborted              True once the caller interrupts; stop quickly
    """

    # --- Replace from here with your model call, e.g. streaming an LLM: ---
    #   for chunk in client.chat.completions.create(..., stream=True):
    #       if turn.aborted: return
    #       turn.say(chunk.choices[0].delta.content or "")
    words = text.strip().lower()
    turn.send({"type": "thinking.started"})
    if words == "slow":
        for number in ("One.", "Two.", "Three.", "Four.", "Five."):
            if turn.aborted:
                return
            turn.say(number + " ")
            await asyncio.sleep(0.3)
    elif words == "hang":
        while not turn.aborted:
            await asyncio.sleep(0.05)
        return
    elif words == "tool":
        turn.tool("read", "reading the readme", phase="started", call_id="t1")
        await asyncio.sleep(0.05)
        turn.tool("read", "reading the readme", phase="completed", call_id="t1")
        turn.say("The readme says hello.")
    elif words == "permission":
        decision = await turn.ask_permission("Allow deleting the build folder?")
        turn.say("Deleted it." if decision != "deny" else "Okay, I left it alone.")
    elif words == "clarify":
        turn.send({"type": "clarification.requested", "requestId": f"c-{turn.id}", "prompt": "Which file?", "choices": ["a", "b"]})
        turn.say("Tell me which file when you're ready.")
    else:
        turn.say(f"You said: {text.strip()}.")
    turn.send({"type": "usage.reported", "inputTokens": len(text.split()), "outputTokens": 5})
    # --- to here. ---


# ---------------------------------------------------------------------------
# Protocol plumbing. You shouldn't need to edit below this line.
# ---------------------------------------------------------------------------

_write_lock = threading.Lock()


def send(frame: dict) -> None:
    line = json.dumps({"v": 1, **frame}, ensure_ascii=False)
    with _write_lock:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


class Turn:
    def __init__(self, turn_id: int) -> None:
        self.id = turn_id
        self.aborted = False
        self._permissions: dict[str, asyncio.Future[str]] = {}

    def send(self, frame: dict) -> None:
        send({**frame, "turnId": self.id})

    def say(self, text: str) -> None:
        if text and not self.aborted:
            self.send({"type": "assistant.delta", "text": text})

    def tool(self, name: str, detail: str = "", *, phase: str = "started", call_id: str | None = None) -> None:
        self.send({"type": "tool", "name": name, "phase": phase, "detail": detail, "callId": call_id})

    async def ask_permission(self, prompt: str, options=("deny", "allow_once")) -> str:
        request_id = f"perm-{self.id}-{len(self._permissions) + 1}"
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._permissions[request_id] = future
        self.send({"type": "permission.requested", "requestId": request_id, "prompt": prompt, "options": list(options)})
        self.send({"type": "status.changed", "state": "blocked"})
        try:
            return await future
        finally:
            self._permissions.pop(request_id, None)
            self.send({"type": "status.changed", "state": "working"})

    def resolve(self, request_id: str, decision: str) -> bool:
        future = self._permissions.get(request_id)
        if future is None or future.done():
            return False
        future.set_result(decision)
        return True


async def main() -> None:
    loop = asyncio.get_running_loop()
    inbox: asyncio.Queue[dict | None] = asyncio.Queue()

    def read_stdin() -> None:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                loop.call_soon_threadsafe(inbox.put_nowait, json.loads(line))
            except json.JSONDecodeError:
                print(f"ignored non-JSON input: {line[:80]}", file=sys.stderr)
        loop.call_soon_threadsafe(inbox.put_nowait, None)

    threading.Thread(target=read_stdin, daemon=True).start()
    current: Turn | None = None
    task: asyncio.Task | None = None

    async def run(turn: Turn, text: str) -> None:
        try:
            await reply(turn, text)
        except Exception as exc:  # report, don't crash the process
            print(f"turn {turn.id} failed: {exc!r}", file=sys.stderr)
            turn.send({"type": "error", "code": "agent", "message": str(exc), "retryable": True})
            return
        turn.send({"type": "turn.aborted"} if turn.aborted else {"type": "turn.completed"})

    while (frame := await inbox.get()) is not None:
        kind = frame.get("type")
        turn_id = frame.get("turnId", frame.get("turn_id"))
        if kind == "hello":
            send({"type": "hello.ok", "capabilities": CAPABILITIES})
        elif kind == "turn.prompt":
            current = Turn(int(turn_id or 0))
            task = asyncio.create_task(run(current, str(frame.get("text") or "")))
        elif kind == "turn.abort":
            if current is not None and current.id == turn_id and task is not None and not task.done():
                current.aborted = True
                for future in current._permissions.values():
                    if not future.done():
                        future.set_result("deny")
            else:
                send({"type": "turn.aborted", "turnId": turn_id})
        elif kind == "permission.resolve":
            ok = current is not None and current.resolve(str(frame.get("requestId")), str(frame.get("decision")))
            send({"type": "control.accepted" if ok else "control.rejected", "ref": frame.get("id"), "requestId": frame.get("requestId"), "reason": None if ok else "unknown request"})
        elif kind in {"clarification.answer", "steer", "cancel"}:
            send({"type": "control.accepted", "ref": frame.get("id"), "requestId": frame.get("requestId")})
        elif kind == "session.close":
            break
    if task is not None and not task.done():
        task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
