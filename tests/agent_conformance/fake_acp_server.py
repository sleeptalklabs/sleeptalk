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
    payload = {"sessionUpdate": session_update, **fields}
    out(
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": os.environ.get("ACP_SESSION_ID", "s1"),
                "update": payload,
            },
        }
    )


def _prompt_text(message: dict) -> str:
    params = message.get("params") or {}
    parts = params.get("prompt") or []
    chunks: list[str] = []
    if isinstance(parts, list):
        for item in parts:
            if isinstance(item, dict):
                chunks.append(str(item.get("text") or ""))
            else:
                chunks.append(str(item))
    return " ".join(chunks).strip().lower()


def main() -> None:
    session_id = os.environ.get("ACP_SESSION_ID", "s1")
    sentence = os.environ.get("ACP_SENTENCE", "Hello from the agent.")
    prompt_id = None
    waiting_perm = False

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
                    "agentCapabilities": {
                        "loadSession": True,
                        "promptCapabilities": {
                            "image": False,
                            "audio": False,
                            "embeddedContext": False,
                        },
                    },
                    "agentInfo": {"name": "conformance-acp"},
                },
            )
            continue
        if method in ("session/new", "session/load"):
            sid = (message.get("params") or {}).get("sessionId") or session_id
            respond(req_id, {"sessionId": sid})
            continue
        if method == "session/cancel":
            if prompt_id is not None:
                respond(prompt_id, {"stopReason": "cancelled"})
                prompt_id = None
            sys.exit(0)
        if method == "session/prompt":
            prompt_id = req_id
            text = _prompt_text(message)
            if text == "hang":
                continue
            if text == "slow":
                update(
                    "agent_thought_chunk",
                    content={"type": "text", "text": "hmm"},
                )
                update(
                    "agent_message_chunk",
                    content={"type": "text", "text": "First sentence."},
                )
                continue
            if text == "permission":
                out(
                    {
                        "jsonrpc": "2.0",
                        "id": "perm-1",
                        "method": "session/request_permission",
                        "params": {
                            "sessionId": session_id,
                            "toolCall": {"toolCallId": "t1", "title": "run ls"},
                            "options": [
                                {
                                    "optionId": "allow_once",
                                    "kind": "allow_once",
                                    "name": "Allow once",
                                },
                                {
                                    "optionId": "deny",
                                    "kind": "reject_once",
                                    "name": "Deny",
                                },
                            ],
                        },
                    }
                )
                waiting_perm = True
                continue
            update(
                "agent_thought_chunk",
                content={"type": "text", "text": "hmm"},
            )
            if text == "tool":
                update(
                    "tool_call",
                    toolCallId="t1",
                    kind="execute",
                    title="ls",
                    status="pending",
                )
                update(
                    "tool_call_update",
                    toolCallId="t1",
                    kind="execute",
                    title="ls",
                    status="completed",
                )
            update(
                "agent_message_chunk",
                content={"type": "text", "text": sentence},
            )
            respond(req_id, {"stopReason": "end_turn"})
            prompt_id = None
            continue
        if waiting_perm and "result" in message and message.get("id") == "perm-1":
            waiting_perm = False
            update(
                "agent_message_chunk",
                content={"type": "text", "text": sentence},
            )
            if prompt_id is not None:
                respond(prompt_id, {"stopReason": "end_turn"})
                prompt_id = None


if __name__ == "__main__":
    main()
