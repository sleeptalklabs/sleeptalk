"""JSONL OMP RPC fake. Run as `python omp_rpc_fake.py` or import `handle`."""

from __future__ import annotations

import json
import sys
from typing import Any


def handle(command: dict[str, Any]) -> list[dict[str, Any]]:
    kind = str(command.get("type") or "")
    req_id = command.get("id")
    if kind == "set_subagent_subscription":
        return [
            {
                "id": req_id,
                "type": "response",
                "command": "set_subagent_subscription",
                "success": True,
                "data": {"level": command.get("level") or "progress"},
            }
        ]
    if kind == "get_state":
        return [
            {
                "id": req_id,
                "type": "response",
                "command": "get_state",
                "success": True,
                "data": {"sessionId": "omp-session-1", "isStreaming": False},
            }
        ]
    if kind == "steer":
        return [{"id": req_id, "type": "response", "command": "steer", "success": True}]
    if kind == "abort":
        return [
            {"id": req_id, "type": "response", "command": "abort", "success": True},
            {"type": "agent_end", "isTerminal": True},
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
    if key == "subagent":
        out.extend(
            [
                {
                    "type": "subagent_lifecycle",
                    "payload": {
                        "id": "child-1",
                        "agent": "explore",
                        "description": "Explore the repo",
                        "status": "started",
                        "detached": True,
                        "index": 0,
                    },
                },
                {
                    "type": "subagent_progress",
                    "payload": {
                        "index": 0,
                        "agent": "explore",
                        "task": "Explore the repo",
                        "progress": {
                            "id": "child-1",
                            "status": "running",
                            "description": "Explore the repo",
                            "recentOutput": ["child tokens"],
                        },
                    },
                },
                {
                    "type": "subagent_lifecycle",
                    "payload": {
                        "id": "child-1",
                        "agent": "explore",
                        "description": "Explore the repo",
                        "status": "completed",
                        "detached": True,
                        "index": 0,
                    },
                },
                {
                    "type": "tool_execution_start",
                    "toolCallId": "bash-1",
                    "toolName": "bash",
                    "args": {"command": "git status"},
                },
                {
                    "type": "tool_execution_end",
                    "toolCallId": "bash-1",
                    "toolName": "bash",
                    "result": {"content": [{"type": "text", "text": "ok"}]},
                    "isError": False,
                },
            ]
        )
    elif key == "task":
        out.extend(
            [
                {
                    "type": "tool_execution_start",
                    "toolCallId": "parent-1",
                    "toolName": "task",
                    "subagentId": "child-live",
                    "args": {"agent": "explore", "task": "Repo architecture"},
                },
                {
                    "type": "tool_execution_end",
                    "toolCallId": "parent-1",
                    "toolName": "task",
                    "subagentId": "child-live",
                    "args": {"agent": "explore", "task": "Repo architecture"},
                    "isError": False,
                },
                {
                    "type": "tool_execution_start",
                    "toolCallId": "read-1",
                    "toolName": "read",
                    "args": {"path": "main.py"},
                },
                {
                    "type": "tool_execution_end",
                    "toolCallId": "read-1",
                    "toolName": "read",
                    "result": {"content": [{"type": "text", "text": "ok"}]},
                    "isError": False,
                },
            ]
        )
    elif key == "anonymous":
        out.extend(
            [
                {
                    "type": "tool_execution_start",
                    "toolCallId": "parent-anon",
                    "toolName": "task",
                    "args": {"agent": "explore", "task": "Explore"},
                },
                {
                    "type": "tool_execution_end",
                    "toolCallId": "parent-anon",
                    "toolName": "task",
                    "args": {"agent": "explore", "task": "Explore"},
                    "isError": False,
                },
            ]
        )
    elif key == "late":
        out.extend(
            [
                {
                    "type": "subagent_lifecycle",
                    "payload": {
                        "id": "child-1",
                        "agent": "explore",
                        "description": "Prices",
                        "status": "started",
                        "detached": True,
                    },
                },
            ]
        )
    elif key == "tokens":
        out.append(
            {
                "type": "subagent_event",
                "payload": {
                    "id": "child-1",
                    "event": {"type": "text_delta", "delta": "child tokens must stay silent"},
                },
            }
        )
    if key == "error":
        out.append(
            {
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "content": [],
                    "stopReason": "error",
                    "errorMessage": (
                        "400 Reasoning is mandatory for this endpoint "
                        "and cannot be disabled.\nraw-http-request=/tmp/x.json"
                    ),
                },
            }
        )
    elif key == "batch":
        out.append(
            {
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "OK. Ready."}],
                },
            }
        )
    else:
        out.append(
            {
                "type": "message_update",
                "assistantMessageEvent": {"type": "text_delta", "delta": "Done."},
            }
        )
    out.append(
        {
            "type": "agent_end",
            "isTerminal": True,
            "messages": [
                {
                    "role": "assistant",
                    "usage": {"input": 2, "output": 4, "cost": {"total": 0.01}},
                }
            ],
        }
    )
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
