"""JSONL Codex app-server fake. Spawn with `python codex_app_server_fake.py`."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time
from typing import Any

SUPPORTED_USER_AGENT = os.environ.get(
    "CODEX_FAKE_USER_AGENT", "sleeptalk/" + (Path(__file__).parents[2] / "sleeptalk/agents/codex_app_server_schema/VERSION").read_text().strip() + " (test; fixture)"
)
EXPERIMENTAL_METHODS = frozenset(
    {
        "thread/turns/list",
        "thread/items/list",
        "thread/backgroundTerminals/list",
        "process/spawn",
        "experimentalFeature/list",
    }
)


class CodexAppServerFake:
    def __init__(self) -> None:
        self.initialized = False
        self.experimental = False
        self.thread_id = ""
        self.turn_id = ""
        self.hanging = False
        # "hang late": the interrupted turn's completion is held back and
        # arrives after the next turn has started, as real Codex can send it.
        self.late_interrupt = False
        self.held_completion: dict[str, Any] | None = None
        self.approval_id = "srv-approval-1"
        self._ids = 0

    def handle(self, message: dict[str, Any]) -> list[dict[str, Any]]:
        if "result" in message or (
            "error" in message and message.get("method") is None and "id" in message
        ):
            return self._client_result(message)
        method = str(message.get("method") or "")
        req_id = message.get("id")
        params = message.get("params")
        if not isinstance(params, dict):
            params = {}
        if method == "initialize":
            return self._initialize(req_id, params)
        if not self.initialized:
            return [_error(req_id, -32000, "Not initialized")]
        if method == "initialized":
            return []
        if method in EXPERIMENTAL_METHODS and not self.experimental:
            return [
                _error(
                    req_id,
                    -32600,
                    f"{method} requires experimentalApi capability",
                )
            ]
        if method == "thread/start":
            return self._thread_start(req_id)
        if method == "thread/resume":
            return self._thread_resume(req_id, params)
        if method == "turn/start":
            return self._turn_start(req_id, params)
        if method == "turn/steer":
            return self._turn_steer(req_id)
        if method == "turn/interrupt":
            return self._turn_interrupt(req_id)
        if method:
            return [_error(req_id, -32601, f"Method not found: {method}")]
        return []

    def _initialize(self, req_id: Any, params: dict[str, Any]) -> list[dict[str, Any]]:
        if self.initialized:
            return [_error(req_id, -32000, "Already initialized")]
        capabilities = params.get("capabilities")
        if isinstance(capabilities, dict):
            self.experimental = bool(capabilities.get("experimentalApi"))
        self.initialized = True
        return [
            {
                "id": req_id,
                "result": {
                    "userAgent": SUPPORTED_USER_AGENT,
                    "codexHome": "/tmp/codex-home",
                    "platformFamily": "unix",
                    "platformOs": "macos",
                },
            }
        ]

    def _thread_start(self, req_id: Any) -> list[dict[str, Any]]:
        self.thread_id = "thr_fixture"
        thread = {"id": self.thread_id, "name": "fixture", "ephemeral": False}
        return [
            {"id": req_id, "result": {"thread": thread}},
            {"method": "thread/started", "params": {"thread": thread}},
        ]

    def _thread_resume(self, req_id: Any, params: dict[str, Any]) -> list[dict[str, Any]]:
        self.thread_id = str(params.get("threadId") or "thr_resumed")
        thread = {"id": self.thread_id, "name": "resumed", "ephemeral": False}
        return [
            {"id": req_id, "result": {"thread": thread}},
            {"method": "thread/started", "params": {"thread": thread}},
        ]

    def _turn_start(self, req_id: Any, params: dict[str, Any]) -> list[dict[str, Any]]:
        held, self.held_completion = self.held_completion, None
        self._ids += 1
        self.turn_id = f"turn_{self._ids}"
        key = _input_text(params).strip().lower()
        turn = {
            "id": self.turn_id,
            "status": "inProgress",
            "items": [],
            "error": None,
        }
        out: list[dict[str, Any]] = [
            {"id": req_id, "result": {"turn": turn}},
            *([held] if held else []),
            {"method": "turn/started", "params": {"turn": turn}},
        ]
        if key in ("hang", "hang late"):
            self.hanging = True
            self.late_interrupt = key == "hang late"
            return out
        if key == "error":
            out.append(
                {
                    "method": "turn/completed",
                    "params": {
                        "turn": {
                            "id": self.turn_id,
                            "status": "failed",
                            "error": {
                                "code": "BadRequest",
                                "message": "fixture refused the turn",
                            },
                        }
                    },
                }
            )
            return out
        if key == "permission":
            out.extend(self._permission_turn())
            return out
        if key == "tool":
            out.extend(self._tool_turn())
        elif key == "slow":
            out.extend(self._text_turn("First sentence.", thinking=False))
        else:
            out.extend(self._text_turn("First sentence. More after.", thinking=True))
        if key == "slow":
            self._emit(out)
            time.sleep(1.5)
            out = self._text_turn("Second sentence.", thinking=False, started=False)
        out.append(self._completed("completed"))
        return out

    def _turn_steer(self, req_id: Any) -> list[dict[str, Any]]:
        return [{"id": req_id, "result": {"turnId": self.turn_id or "turn_idle"}}]

    def _turn_interrupt(self, req_id: Any) -> list[dict[str, Any]]:
        self.hanging = False
        out = [{"id": req_id, "result": {}}]
        if self.turn_id and self.late_interrupt:
            self.held_completion = self._completed("interrupted")
            self.late_interrupt = False
        elif self.turn_id:
            out.append(self._completed("interrupted"))
        return out

    def _client_result(self, message: dict[str, Any]) -> list[dict[str, Any]]:
        req_id = message.get("id")
        if str(req_id) != self.approval_id:
            return []
        return [
            {
                "method": "serverRequest/resolved",
                "params": {"threadId": self.thread_id, "requestId": self.approval_id},
            },
            {
                "method": "item/completed",
                "params": {
                    "item": {
                        "id": "item_cmd",
                        "type": "commandExecution",
                        "command": "git status",
                        "status": "completed",
                        "exitCode": 0,
                    }
                },
            },
            *self._text_turn("Allowed. Proceeding.", thinking=False, started=False),
            self._completed("completed"),
        ]

    def _permission_turn(self) -> list[dict[str, Any]]:
        return [
            {
                "method": "item/started",
                "params": {
                    "item": {
                        "id": "item_cmd",
                        "type": "commandExecution",
                        "command": "git status",
                        "cwd": ".",
                        "status": "inProgress",
                    }
                },
            },
            {
                "id": self.approval_id,
                "method": "item/commandExecution/requestApproval",
                "params": {
                    "itemId": "item_cmd",
                    "threadId": self.thread_id,
                    "turnId": self.turn_id,
                    "command": "git status",
                    "cwd": ".",
                    "reason": "Allow command execution?",
                    "availableDecisions": [
                        "accept",
                        "acceptForSession",
                        "decline",
                        "cancel",
                    ],
                },
            },
        ]

    def _tool_turn(self) -> list[dict[str, Any]]:
        item = {
            "id": "item_cmd",
            "type": "commandExecution",
            "command": "git status",
            "cwd": ".",
            "status": "inProgress",
        }
        return [
            {"method": "item/started", "params": {"item": item}},
            {
                "method": "item/commandExecution/outputDelta",
                "params": {"itemId": "item_cmd", "delta": "ok"},
            },
            {
                "method": "item/completed",
                "params": {
                    "item": {**item, "status": "completed", "aggregatedOutput": "ok"}
                },
            },
            *self._text_turn("Command finished.", thinking=False, started=False),
        ]

    def _text_turn(
        self, text: str, *, thinking: bool, started: bool = True
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        if thinking:
            out.append(
                {
                    "method": "item/started",
                    "params": {
                        "item": {"id": "item_think", "type": "reasoning", "text": ""}
                    },
                }
            )
            out.append(
                {
                    "method": "item/reasoning/summaryTextDelta",
                    "params": {"itemId": "item_think", "delta": "thinking"},
                }
            )
            out.append(
                {
                    "method": "item/completed",
                    "params": {
                        "item": {
                            "id": "item_think",
                            "type": "reasoning",
                            "text": "thinking",
                        }
                    },
                }
            )
        if started:
            out.append(
                {
                    "method": "item/started",
                    "params": {
                        "item": {"id": "item_msg", "type": "agentMessage", "text": ""}
                    },
                }
            )
        out.append(
            {
                "method": "item/agentMessage/delta",
                "params": {"itemId": "item_msg", "delta": text},
            }
        )
        out.append(
            {
                "method": "item/completed",
                "params": {
                    "item": {"id": "item_msg", "type": "agentMessage", "text": text}
                },
            }
        )
        return out

    def _completed(self, status: str) -> dict[str, Any]:
        return {
            "method": "turn/completed",
            "params": {
                "turn": {
                    "id": self.turn_id,
                    "status": status,
                    "usage": {"inputTokens": 12, "outputTokens": 4},
                }
            },
        }

    def _emit(self, messages: list[dict[str, Any]]) -> None:
        for message in messages:
            sys.stdout.write(json.dumps(message, separators=(",", ":")) + "\n")
        sys.stdout.flush()


def _input_text(params: dict[str, Any]) -> str:
    raw = params.get("input")
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list):
        parts = [
            str(item.get("text") or "")
            for item in raw
            if isinstance(item, dict) and item.get("type") == "text"
        ]
        return "".join(parts)
    return str(params.get("userInput") or "")


def _error(req_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"id": req_id, "error": {"code": code, "message": message}}


def main() -> None:
    fake = CodexAppServerFake()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(message, dict):
            continue
        fake._emit(fake.handle(message))


if __name__ == "__main__":
    main()
