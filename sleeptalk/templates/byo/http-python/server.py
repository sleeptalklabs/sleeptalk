#!/usr/bin/env python3
"""A Sleeptalk agent behind an HTTP endpoint (Bridge Lite over HTTP).

Sleeptalk POSTs each spoken turn as JSON:

    {"type": "turn.prompt", "turnId": 1, "sessionId": "...", "text": "hello"}

Reply with a streamed body of frames, one JSON object per line
(Content-Type: application/x-ndjson). Sleeptalk also accepts Server-Sent
Events, a single JSON object like {"text": "..."}, or plain streamed text.
Answer GET on the same path with {"type": "hello.ok", "capabilities": {...}}
so Sleeptalk knows which frames you send before the first turn.
When the caller interrupts, Sleeptalk closes the request.

This file uses only the standard library so it runs anywhere. In FastAPI the
same thing is a StreamingResponse of the lines `reply` yields; in Next.js it
is a route handler returning a ReadableStream.

Guide: docs/bring-your-own-agent.md in the Sleeptalk repository.
"""

from __future__ import annotations

import hmac
import json
import os
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8765"))
PATH = "/sleeptalk/turn"
# Shared secret. Optional on loopback; required on any other address, since
# anyone who can reach the port could otherwise drive the agent. Set the same
# value in the variable named by token_env.
TOKEN = os.environ.get("AGENT_TOKEN", "")
LOOPBACK = {"127.0.0.1", "::1", "localhost"}

# Advertise what you send. HTTP turns cannot receive permission answers mid-turn.
CAPABILITIES = {"structured_events": "supported", "tool_lifecycle": "supported", "usage": "supported"}


def reply(session_id: str, text: str) -> Iterator[dict]:
    """Yield frames for one turn. Replace the demo with your agent.

    Keep conversation state per session_id if your agent needs memory.
    """

    # --- Replace from here with your model call. ---
    words = text.strip().lower()
    yield {"type": "thinking.started"}
    if words == "slow":
        for number in ("One.", "Two.", "Three.", "Four.", "Five."):
            yield {"type": "assistant.delta", "text": number + " "}
            time.sleep(0.3)
    elif words == "hang":
        while True:
            time.sleep(0.2)
            yield {"type": "status.changed", "state": "working"}
    elif words == "tool":
        yield {"type": "tool", "name": "read", "phase": "started", "callId": "t1", "detail": "reading the readme"}
        yield {"type": "tool", "name": "read", "phase": "completed", "callId": "t1"}
        yield {"type": "assistant.delta", "text": "The readme says hello."}
    else:
        yield {"type": "assistant.delta", "text": f"You said: {text.strip()}."}
    yield {"type": "usage.reported", "inputTokens": len(text.split()), "outputTokens": 5}
    # --- to here. ---


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_HEAD(self) -> None:  # readiness probe; never starts work
        self.send_response(401 if TOKEN and not self._authorized() else 204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:  # capability discovery; never starts work
        if self.path != PATH:
            self.send_error(404)
            return
        if TOKEN and not self._authorized():
            self.send_error(401)
            return
        data = json.dumps({"v": 1, "type": "hello.ok", "capabilities": CAPABILITIES}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:
        if self.path != PATH:
            self.send_error(404)
            return
        if TOKEN and not self._authorized():
            self.send_error(401)
            return
        length = int(self.headers.get("Content-Length") or 0)
        frame = json.loads(self.rfile.read(length) or b"{}")
        turn_id = frame.get("turnId")
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        try:
            for out in reply(str(frame.get("sessionId") or ""), str(frame.get("text") or "")):
                self._chunk({"v": 1, "turnId": turn_id, **out})
            self._chunk({"v": 1, "turnId": turn_id, "type": "turn.completed"})
            self.wfile.write(b"0\r\n\r\n")
        except (BrokenPipeError, ConnectionResetError):
            pass  # the caller interrupted; stop working on this turn

    def _authorized(self) -> bool:
        presented = self.headers.get("Authorization", "").encode("utf-8")
        return hmac.compare_digest(presented, f"Bearer {TOKEN}".encode("utf-8"))

    def _chunk(self, frame: dict) -> None:
        data = (json.dumps(frame) + "\n").encode("utf-8")
        self.wfile.write(f"{len(data):X}\r\n".encode() + data + b"\r\n")
        self.wfile.flush()

    def log_message(self, format: str, *args: object) -> None:
        pass


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request: object, client_address: object) -> None:
        pass  # a caller interrupting a turn closes the connection; that's normal


if __name__ == "__main__":
    if HOST not in LOOPBACK and not TOKEN:
        raise SystemExit(f"set AGENT_TOKEN to a long random value to listen on {HOST} (and the same value for Sleeptalk)")
    print(f"listening on http://{HOST}:{PORT}{PATH}", flush=True)
    Server((HOST, PORT), Handler).serve_forever()
