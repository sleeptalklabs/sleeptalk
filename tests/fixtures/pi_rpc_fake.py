"""JSONL Pi RPC fake. Run as `python pi_rpc_fake.py` or import `handle`."""

from __future__ import annotations

import json
import sys
from typing import Any


def handle(command: dict[str, Any]) -> list[dict[str, Any]]:
    kind = str(command.get("type") or "")
    req_id = command.get("id")
    if kind == "get_state":
        return [
            {
                "id": req_id,
                "type": "response",
                "command": "get_state",
                "success": True,
                "data": {"sessionId": "pi-session-1", "isStreaming": False},
            }
        ]
    if kind == "steer":
        return [{"id": req_id, "type": "response", "command": "steer", "success": True}]
    if kind == "abort":
        return [
            {"id": req_id, "type": "response", "command": "abort", "success": True},
            {"type": "agent_settled"},
        ]
    if kind == "set_thinking_level":
        return [
            {
                "id": req_id,
                "type": "response",
                "command": "set_thinking_level",
                "success": True,
            }
        ]
    if kind != "prompt":
        return [
            {
                "id": req_id,
                "type": "response",
                "command": kind or "unknown",
                "success": False,
                "error": f"unknown {kind}",
            }
        ]
    key = str(command.get("message") or "").strip().lower()
    out: list[dict[str, Any]] = [
        {"id": req_id, "type": "response", "command": "prompt", "success": True},
        {"type": "agent_start"},
    ]
    if key == "hang":
        return out
    if key == "permission":
        out.append(
            {
                "type": "extension_ui_request",
                "id": "ui-1",
                "method": "confirm",
                "title": "Allow?",
            }
        )
    if key == "tool":
        out.append(
            {
                "type": "tool_execution_start",
                "toolCallId": "c1",
                "toolName": "bash",
                "args": {"command": "git status"},
            }
        )
        out.append(
            {
                "type": "tool_execution_end",
                "toolCallId": "c1",
                "toolName": "bash",
                "result": {"content": [{"type": "text", "text": "ok"}]},
                "isError": False,
            }
        )
    if key == "task-child":
        out.append(
            {
                "type": "tool_execution_start",
                "toolCallId": "parent-1",
                "toolName": "task",
                "subagentId": "child-1",
                "args": {"agent": "explore", "task": "Repo architecture"},
            }
        )
        out.append(
            {
                "type": "tool_execution_end",
                "toolCallId": "parent-1",
                "toolName": "task",
                "subagentId": "child-1",
                "args": {"agent": "explore", "task": "Repo architecture"},
                "isError": False,
            }
        )
    out.append(
        {
            "type": "message_update",
            "assistantMessageEvent": {"type": "thinking_delta", "delta": "hmm"},
        }
    )
    first = "First sentence."
    if key == "slow":
        out.append(
            {
                "type": "message_update",
                "assistantMessageEvent": {"type": "text_delta", "delta": first},
            }
        )
        return out
    out.append(
        {
            "type": "message_update",
            "assistantMessageEvent": {"type": "text_delta", "delta": first + " "},
        }
    )
    out.append(
        {
            "type": "message_update",
            "assistantMessageEvent": {"type": "text_delta", "delta": "Second sentence."},
        }
    )
    out.append(
        {
            "type": "agent_end",
            "messages": [
                {
                    "role": "assistant",
                    "usage": {"input": 3, "output": 8, "cost": {"total": 0.01}},
                }
            ],
            "willRetry": False,
        }
    )
    out.append({"type": "agent_settled"})
    return out


def main() -> None:
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            command = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(command, dict):
            continue
        for event in handle(command):
            print(json.dumps(event), flush=True)


if __name__ == "__main__":
    main()
