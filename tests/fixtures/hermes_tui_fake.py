"""JSON-RPC TUI Gateway fake for Hermes adapter tests.

Run as a stdio process (`python hermes_tui_fake.py`) or import `FakeTui`.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Callable
from typing import Any

from sleeptalk.agents.hermes_gateway_adapter import QueueLink


class FakeTui:
    def __init__(self) -> None:
        self.link = QueueLink(self.handle)
        self.created = False
        self._pending_text = ""
        self._tasks: set[asyncio.Task[None]] = set()

    def _spawn(self, coroutine) -> None:
        task = asyncio.get_running_loop().create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def settle(self) -> None:
        """Wait until every event this fake scheduled has been pushed.

        Replies to controls sent after a turn ended arrive from background
        tasks; when they run relative to the next turn depends on the Python
        version's scheduler, so tests settle before starting another turn.
        """
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    def handle(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "session.create":
            self.created = True
            return {"session_id": "s1"}
        if method == "prompt.submit":
            self._pending_text = str(params.get("text") or "")
            self._spawn(self._emit_prompt(self._pending_text))
            return {"status": "streaming"}
        if method == "session.interrupt":
            self._spawn(self.link.push({"type": "run.cancelled", "payload": {}}))
            return {"status": "interrupted"}
        if method == "session.steer":
            return {"status": "queued"}
        if method == "subagent.steer":
            return {"status": "queued"}
        if method == "subagent.interrupt":
            return {"status": "interrupted"}
        if method == "approval.respond":
            self._spawn(self._finish_after_ask())
            return {"status": "resolved"}
        if method == "clarify.respond":
            self._spawn(self._finish_after_ask())
            return {"status": "resolved"}
        raise RuntimeError(f"unknown method {method}")

    async def _emit_prompt(self, text: str) -> None:
        key = text.strip().lower()
        if key == "hang":
            return
        if key == "permission":
            await self.link.push(
                {
                    "type": "approval.request",
                    "payload": {
                        "request_id": "perm-1",
                        "command": "ls",
                        "choices": ["once", "deny"],
                    },
                }
            )
            return
        if key == "clarify":
            await self.link.push(
                {
                    "type": "clarify.request",
                    "payload": {
                        "request_id": "clarify-1",
                        "prompt": "Which file?",
                        "choices": ["a", "b"],
                    },
                }
            )
            await self.link.push(
                {"type": "message.delta", "payload": {"text": "Which file?"}}
            )
            await self.link.push({"type": "message.complete", "payload": {}})
            return
        await self._emit_work(key)

    async def _emit_work(self, key: str) -> None:
        await self.link.push({"type": "thinking.delta", "payload": {"text": "..."}})
        if key == "tool":
            await self.link.push(
                {
                    "type": "tool.start",
                    "payload": {
                        "tool_name": "terminal",
                        "args": {"command": "cd /repo && git status"},
                    },
                }
            )
            await self.link.push(
                {
                    "type": "tool.complete",
                    "payload": {"tool_name": "terminal"},
                }
            )
        if key == "delegate":
            await self.link.push(
                {
                    "type": "tool.start",
                    "payload": {
                        "tool_name": "delegate_task",
                        "subagent_id": "sa-0-live",
                        "args": {"goal": "Repo architecture"},
                    },
                }
            )
            await self.link.push(
                {
                    "type": "tool.start",
                    "payload": {
                        "tool_name": "terminal",
                        "args": {"command": "git status"},
                    },
                }
            )
            await self.link.push(
                {
                    "type": "tool.complete",
                    "payload": {
                        "tool_name": "delegate_task",
                        "subagent_id": "sa-0-live",
                    },
                }
            )
        if key == "subagent":
            await self.link.push(
                {
                    "type": "subagent.spawn_requested",
                    "payload": {
                        "subagent_id": "sa-0-1a2b3c4d",
                        "goal": "Explore the repo",
                    },
                }
            )
            await self.link.push(
                {
                    "type": "subagent.start",
                    "payload": {
                        "subagent_id": "sa-0-1a2b3c4d",
                        "goal": "Explore the repo",
                    },
                }
            )
            await self.link.push(
                {
                    "type": "subagent.text",
                    "payload": {
                        "subagent_id": "sa-0-1a2b3c4d",
                        "text": "child tokens must stay silent",
                    },
                }
            )
            await self.link.push(
                {
                    "type": "subagent.thinking",
                    "payload": {"subagent_id": "sa-0-1a2b3c4d", "text": "..."},
                }
            )
            await self.link.push(
                {
                    "type": "subagent.complete",
                    "payload": {
                        "subagent_id": "sa-0-1a2b3c4d",
                        "status": "success",
                        "summary": "Mapped the layout",
                    },
                }
            )
        if key == "noise":
            await self.link.push({"type": "gateway.ready", "payload": {"skin": "fake"}})
            await self.link.push(
                {"type": "delegation.status", "payload": {"active": []}}
            )
        if key == "fail":
            await self.link.push({"type": "error", "payload": {"message": "boom"}})
            return
        await self.link.push(
            {"type": "message.delta", "payload": {"text": "Bitcoin is up. "}}
        )
        if key == "slow":
            return
        await self.link.push(
            {"type": "message.delta", "payload": {"text": "It gained 2%."}}
        )
        await self.link.push({"type": "message.complete", "payload": {"text": "done"}})

    async def _finish_after_ask(self) -> None:
        await self.link.push(
            {"type": "message.delta", "payload": {"text": "Approved."}}
        )
        await self.link.push({"type": "message.complete", "payload": {}})


class _StdioFake:
    def __init__(self, write: Callable[[dict[str, Any]], None]) -> None:
        self._write = write
        self._ids: dict[str, Any] = {}

    def dispatch(self, message: dict[str, Any]) -> None:
        method = str(message.get("method") or "")
        req_id = message.get("id")
        params = message.get("params") or {}
        if method == "session.create":
            self._ok(req_id, {"session_id": "s1"})
            return
        if method == "prompt.submit":
            self._ok(req_id, {"status": "streaming"})
            self._event("thinking.delta", {"text": "..."})
            self._event("message.delta", {"text": "Hello there. "})
            self._event("message.complete", {"text": "Hello there."})
            return
        if method == "session.interrupt":
            self._ok(req_id, {"status": "interrupted"})
            self._event("run.cancelled", {})
            return
        if method == "session.steer":
            self._ok(req_id, {"status": "queued"})
            return
        self._write(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": method},
            }
        )

    def _ok(self, req_id: Any, result: dict[str, Any]) -> None:
        self._write({"jsonrpc": "2.0", "id": req_id, "result": result})

    def _event(self, kind: str, payload: dict[str, Any]) -> None:
        self._write(
            {
                "jsonrpc": "2.0",
                "method": "event",
                "params": {"type": kind, "payload": payload},
            }
        )


def main() -> None:
    fake = _StdioFake(lambda frame: print(json.dumps(frame), flush=True))
    print(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "method": "event",
                "params": {"type": "gateway.ready", "payload": {"skin": "fake"}},
            }
        ),
        flush=True,
    )
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(message, dict):
            fake.dispatch(message)


if __name__ == "__main__":
    main()
