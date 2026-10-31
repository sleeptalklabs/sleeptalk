"""Actual Codex app-server with a deterministic local Responses provider."""

from __future__ import annotations

import asyncio
import json
import os
import threading
import shlex
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
from sleeptalk.agents.codex_app_server import CodexAppServerClient, parse_codex_version


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["text", "deny", "allow", "cancel"])
async def test_actual_codex_stream_usage_and_resume(tmp_path, decision):
    binary = os.environ.get("SLEEPTALK_TEST_CODEX_BIN")
    if not binary:
        pytest.skip("Select an exact staged Codex release")
    expected = os.environ["SLEEPTALK_TEST_CODEX_VERSION"]
    requests = []
    tool_sent = False
    marker = tmp_path / "workspace" / "approved.txt"

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            nonlocal tool_sent
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            msg = {
                "type": "message",
                "id": "msg-fixture",
                "role": "assistant",
                "status": "completed",
                "content": [
                    {
                        "type": "output_text",
                        "text": "Verified Codex response.",
                        "annotations": [],
                    }
                ],
            }
            events = [
                {
                    "type": "response.created",
                    "response": {
                        "id": "resp-fixture",
                        "status": "in_progress",
                        "output": [],
                    },
                },
                {
                    "type": "response.output_item.added",
                    "output_index": 0,
                    "item": {**msg, "status": "in_progress", "content": []},
                },
                {
                    "type": "response.output_text.delta",
                    "item_id": "msg-fixture",
                    "output_index": 0,
                    "content_index": 0,
                    "delta": "Verified Codex response.",
                },
                {"type": "response.output_item.done", "output_index": 0, "item": msg},
                {
                    "type": "response.completed",
                    "response": {
                        "id": "resp-fixture",
                        "status": "completed",
                        "output": [msg],
                        "usage": {
                            "input_tokens": 12,
                            "output_tokens": 4,
                            "total_tokens": 16,
                            "input_tokens_details": {"cached_tokens": 0},
                        },
                    },
                },
            ]
            if decision != "text" and not tool_sent:
                names = {t.get("name") for t in body.get("tools", [])}
                name = "exec_command" if "exec_command" in names else "shell_command"
                command = "printf approved > " + shlex.quote(str(marker))
                arguments = json.dumps(
                    {
                        "cmd" if name == "exec_command" else "command": command,
                        "sandbox_permissions": "require_escalated",
                        "justification": "Write the disposable qualification marker",
                    }
                )
                call = {
                    "type": "function_call",
                    "id": "fc-fixture",
                    "call_id": "call-fixture",
                    "name": name,
                    "arguments": arguments,
                }
                events = [
                    events[0],
                    {
                        "type": "response.output_item.added",
                        "output_index": 0,
                        "item": {**call, "arguments": ""},
                    },
                    {
                        "type": "response.function_call_arguments.delta",
                        "output_index": 0,
                        "item_id": "fc-fixture",
                        "delta": arguments,
                    },
                    {
                        "type": "response.output_item.done",
                        "output_index": 0,
                        "item": call,
                    },
                    {
                        **events[-1],
                        "response": {**events[-1]["response"], "output": [call]},
                    },
                ]
                tool_sent = True
            data = "".join(
                "event: " + e["type"] + "\ndata: " + json.dumps(e) + "\n\n"
                for e in events
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    home, workspace = tmp_path / "home", tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    (home / "config.toml").write_text(f"""model = "fixture-model"
model_provider = "fixture"
approval_policy = "on-request"
sandbox_mode = "read-only"
[model_providers.fixture]
name = "Local fixture"
base_url = "http://127.0.0.1:{server.server_port}/v1"
wire_api = "responses"
requires_openai_auth = false
""")
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(home),
        "CODEX_HOME": str(home),
        "XDG_CONFIG_HOME": str(home),
        "XDG_CACHE_HOME": str(home),
    }
    client = CodexAppServerClient(
        codex_bin=binary,
        cwd=str(workspace),
        model="fixture-model",
        permission_mode="deny" if decision == "deny" else "pending",
        env=env,
    )
    try:
        seen = []
        tasks = []

        def event(e):
            seen.append(e)
            if isinstance(e, PermissionRequested):
                assert not marker.exists()
                if decision == "cancel":
                    tasks.append(asyncio.create_task(client.abort()))
                else:
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

        answer = " ".join(
            [
                p
                async for p in client.respond_streaming(
                    "hello", timeout=20, on_event=typed_events(event)
                )
            ]
        )
        if tasks:
            await asyncio.gather(*tasks)
        if decision != "cancel":
            assert answer == "Verified Codex response."
        if decision in {"allow", "cancel"}:
            assert tasks
        assert marker.exists() is (decision == "allow")
        assert parse_codex_version(client._user_agent) == expected
        assert any(isinstance(e, SessionIdentified) for e in seen)
        if decision != "cancel":
            assert any(
                isinstance(e, UsageReported) and e.output_tokens == 4 for e in seen
            ), seen
        session_id = client.upstream_session_id
        assert await client.respond("again", timeout=20) == "Verified Codex response."
        await client.close()
        client = CodexAppServerClient(
            codex_bin=binary,
            cwd=str(workspace),
            model="fixture-model",
            resume_thread_id=session_id,
            permission_mode="deny",
            env=env,
        )
        assert await client.respond("resume", timeout=20) == "Verified Codex response."
        assert client.upstream_session_id == session_id
        assert len(requests) >= 3
        assert all(r["model"] == "fixture-model" for r in requests)
    finally:
        await client.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
