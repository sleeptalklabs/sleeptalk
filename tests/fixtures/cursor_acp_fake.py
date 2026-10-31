from __future__ import annotations

import json
import os
import sys


def out(msg: dict) -> None:
    sys.stdout.write(json.dumps(msg, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def respond(req_id: object, result: object) -> None:
    out({"jsonrpc": "2.0", "id": req_id, "result": result})


def update(session_update: str, **fields: object) -> None:
    out(
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": os.environ.get("ACP_SESSION_ID", "s1"),
                "update": {"sessionUpdate": session_update, **fields},
            },
        }
    )


def main() -> None:
    session_id = os.environ.get("ACP_SESSION_ID", "s1")
    mode = os.environ.get("CURSOR_ACP_MODE", "plain")
    sentence = os.environ.get("ACP_SENTENCE", "Hello from Cursor.")
    prompt_id = None
    waiting = None

    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            continue
        method = message.get("method")
        req_id = message.get("id")

        if method == "initialize":
            respond(
                req_id,
                {
                    "protocolVersion": 1,
                    "agentCapabilities": {"loadSession": True},
                    "authMethods": [{"id": "cursor_login"}],
                    "agentInfo": {"name": "cursor-acp-fake"},
                },
            )
            continue
        if method == "authenticate":
            respond(req_id, {})
            continue
        if method in ("session/new", "session/load"):
            respond(req_id, {"sessionId": session_id})
            continue
        if method == "session/prompt":
            prompt_id = req_id
            if mode == "question":
                out(
                    {
                        "jsonrpc": "2.0",
                        "id": "q1",
                        "method": "cursor/ask_question",
                        "params": {
                            "toolCallId": "call_q",
                            "title": "Need input",
                            "questions": [
                                {
                                    "id": "q1",
                                    "prompt": "Which mode should I use?",
                                    "options": [
                                        {"id": "agent", "label": "Agent"},
                                        {"id": "plan", "label": "Plan"},
                                    ],
                                }
                            ],
                        },
                    }
                )
                waiting = "q1"
                continue
            if mode == "plan":
                out(
                    {
                        "jsonrpc": "2.0",
                        "id": "p1",
                        "method": "cursor/create_plan",
                        "params": {
                            "toolCallId": "call_p",
                            "name": "Refactor tabs",
                            "overview": "Tighten layout.",
                            "plan": "1. Inspect.\n2. Update.",
                            "todos": [],
                        },
                    }
                )
                waiting = "p1"
                continue
            if mode == "unknown":
                out(
                    {
                        "jsonrpc": "2.0",
                        "id": "u1",
                        "method": "cursor/future_thing",
                        "params": {"toolCallId": "call_u"},
                    }
                )
                waiting = "u1"
                continue
            if mode == "notify":
                out(
                    {
                        "jsonrpc": "2.0",
                        "method": "cursor/update_todos",
                        "params": {
                            "toolCallId": "call_t",
                            "todos": [
                                {
                                    "id": "1",
                                    "content": "Set up",
                                    "status": "completed",
                                }
                            ],
                            "merge": True,
                        },
                    }
                )
            update(
                "agent_message_chunk",
                content={"type": "text", "text": sentence},
            )
            respond(req_id, {"stopReason": "end_turn"})
            prompt_id = None
            continue
        if waiting and "result" in message and str(message.get("id")) == waiting:
            waiting = None
            update(
                "agent_message_chunk",
                content={"type": "text", "text": sentence},
            )
            if prompt_id is not None:
                respond(prompt_id, {"stopReason": "end_turn"})
                prompt_id = None


if __name__ == "__main__":
    main()
