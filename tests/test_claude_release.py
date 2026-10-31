"""Actual Python SDK and bundled Claude CLI against a local Messages fixture."""

from __future__ import annotations

import json
import asyncio
import os
from importlib.metadata import version
import threading
import subprocess
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from sleeptalk.adapter import (
    SessionIdentified,
    UsageReported,
    PermissionRequested,
    PermissionDecision,
    ResolvePermission,
    typed_events,
)
from sleeptalk.agents.claude_code_adapter import ClaudeCodeClient


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["text", "deny", "allow"])
async def test_actual_claude_sdk_stream_and_repeated_turn(
    tmp_path, monkeypatch, decision
):
    expected = os.environ.get("SLEEPTALK_TEST_CLAUDE_SDK_VERSION")
    if not expected:
        pytest.skip("Select an exact Claude SDK wheel for release qualification")
    assert version("claude-agent-sdk") == expected
    selected_cli = os.environ.get("SLEEPTALK_TEST_CLAUDE_BIN")
    expected_cli = os.environ.get("SLEEPTALK_TEST_CLAUDE_CLI_VERSION")
    if selected_cli:
        from claude_agent_sdk._internal.transport.subprocess_cli import (
            SubprocessCLITransport,
        )

        # Exercise the SDK's CLI fallback without replacing its wheel.
        monkeypatch.setattr(
            SubprocessCLITransport, "_find_cli", lambda self: selected_cli
        )
    seen_requests = []
    marker = tmp_path / "workspace" / "approved.txt"

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen_requests.append((self.path, body))
            if "count_tokens" in self.path:
                data = json.dumps({"input_tokens": 12}).encode()
                content_type = "application/json"
            else:
                events = [
                    {
                        "type": "message_start",
                        "message": {
                            "id": "msg_fixture",
                            "type": "message",
                            "role": "assistant",
                            "model": body.get("model", "fixture"),
                            "content": [],
                            "stop_reason": None,
                            "stop_sequence": None,
                            "usage": {"input_tokens": 12, "output_tokens": 0},
                        },
                    },
                    {
                        "type": "content_block_start",
                        "index": 0,
                        "content_block": {"type": "text", "text": ""},
                    },
                    {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {
                            "type": "text_delta",
                            "text": "Verified Claude response.",
                        },
                    },
                    {"type": "content_block_stop", "index": 0},
                    {
                        "type": "message_delta",
                        "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                        "usage": {"output_tokens": 4},
                    },
                    {"type": "message_stop"},
                ]
                completed = any(
                    isinstance(m.get("content"), list)
                    and any(
                        b.get("type") == "tool_result"
                        for b in m["content"]
                        if isinstance(b, dict)
                    )
                    for m in body.get("messages", [])
                )
                if (
                    decision != "text"
                    and not completed
                    and any(t.get("name") == "Write" for t in body.get("tools", []))
                ):
                    events[1]["content_block"] = {
                        "type": "tool_use",
                        "id": "call-fixture",
                        "name": "Write",
                        "input": {},
                    }
                    events[2]["delta"] = {
                        "type": "input_json_delta",
                        "partial_json": json.dumps(
                            {"file_path": str(marker), "content": "approved"}
                        ),
                    }
                    events[4]["delta"]["stop_reason"] = "tool_use"
                data = "".join(
                    "event: " + e["type"] + "\ndata: " + json.dumps(e) + "\n\n"
                    for e in events
                ).encode()
                content_type = "text/event-stream"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    home, workspace = tmp_path / "home", tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(home),
        "USERPROFILE": str(home),
        "CLAUDE_CONFIG_DIR": str(home),
        "ANTHROPIC_API_KEY": "synthetic-key",
        "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{server.server_port}",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_TELEMETRY": "1",
        "DISABLE_ERROR_REPORTING": "1",
    }
    if expected_cli:
        import claude_agent_sdk

        executable = selected_cli or str(
            Path(claude_agent_sdk.__file__).parent / "_bundled" / "claude"
        )
        detected = subprocess.run(
            [executable, "--version"],
            env=env,
            cwd=home,
            capture_output=True,
            text=True,
            check=True,
            timeout=15,
        )
        assert detected.stdout.split()[0] == expected_cli
    # SDK merges the parent environment. Do not inherit login/provider overrides.
    for key in tuple(os.environ):
        monkeypatch.delenv(key)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    client = ClaudeCodeClient(
        cwd=str(workspace),
        model="claude-sonnet-4-5",
        permission_mode="pending",
        env=env,
    )
    try:
        events = []
        tasks = []

        def event(e):
            events.append(e)
            if isinstance(e, PermissionRequested):
                assert not marker.exists()
                tasks.append(
                    asyncio.create_task(
                        client.control(
                            ResolvePermission(
                                request_id=e.request_id,
                                decision=PermissionDecision.ALLOW_ONCE
                                if decision == "allow"
                                else PermissionDecision.DENY,
                            )
                        )
                    )
                )

        result = " ".join(
            [
                s
                async for s in client.respond_streaming(
                    "hello", timeout=30, on_event=typed_events(event)
                )
            ]
        )
        assert result == "Verified Claude response."
        if tasks:
            await asyncio.gather(*tasks)
        if decision != "text":
            assert tasks
        assert marker.exists() is (decision == "allow")
        assert any(isinstance(e, SessionIdentified) for e in events)
        assert any(
            isinstance(e, UsageReported) and e.output_tokens >= 4 for e in events
        )
        assert await client.respond("again", timeout=30) == "Verified Claude response."
        assert any("messages" in path for path, _ in seen_requests)
    finally:
        await client.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
