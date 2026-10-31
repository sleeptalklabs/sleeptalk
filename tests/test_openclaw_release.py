"""Actual isolated Gateway startup/auth/session checks; no user gateway touched."""

import asyncio
import json
import os
import signal
import socket
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from sleeptalk.agents.openclaw_gateway_adapter import (
    OpenClawGatewayClient,
    AiohttpGatewaySession,
    OpenClawGatewayError,
)


@pytest.mark.asyncio
async def test_actual_gateway_auth_and_session(tmp_path):
    binary = os.environ.get("SLEEPTALK_TEST_OPENCLAW_BIN")
    if not binary:
        pytest.skip("Select a staged OpenClaw release")
    node_path = os.environ.get("SLEEPTALK_TEST_NODE_PATH", os.environ["PATH"])
    requests = []

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            text = "Verified OpenClaw response."
            if body.get("stream"):
                chunks = [
                    {
                        "id": "fixture",
                        "object": "chat.completion.chunk",
                        "created": 1,
                        "model": "fixture-model",
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"role": "assistant", "content": text},
                                "finish_reason": None,
                            }
                        ],
                    },
                    {
                        "id": "fixture",
                        "object": "chat.completion.chunk",
                        "created": 1,
                        "model": "fixture-model",
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                        "usage": {
                            "prompt_tokens": 12,
                            "completion_tokens": 4,
                            "total_tokens": 16,
                        },
                    },
                ]
                data = (
                    "".join("data: " + json.dumps(c) + "\n\n" for c in chunks)
                    + "data: [DONE]\n\n"
                ).encode()
                mime = "text/event-stream"
            else:
                data = json.dumps(
                    {
                        "id": "fixture",
                        "object": "chat.completion",
                        "created": 1,
                        "model": "fixture-model",
                        "choices": [
                            {
                                "index": 0,
                                "message": {"role": "assistant", "content": text},
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {
                            "prompt_tokens": 12,
                            "completion_tokens": 4,
                            "total_tokens": 16,
                        },
                    }
                ).encode()
                mime = "application/json"
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    provider = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    thread = threading.Thread(target=provider.serve_forever, daemon=True)
    thread.start()
    home = tmp_path / "home"
    home.mkdir()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = {
        "PATH": node_path,
        "HOME": str(home),
        "OPENCLAW_STATE_DIR": str(home),
        "OPENCLAW_CONFIG_PATH": str(home / "openclaw.json"),
        "OPENCLAW_DISABLE_BONJOUR": "1",
    }
    expected = os.environ["SLEEPTALK_TEST_OPENCLAW_VERSION"]
    detected = subprocess.run(
        [binary, "--version"],
        env=env,
        cwd=home,
        capture_output=True,
        text=True,
        check=True,
        timeout=15,
    )
    assert expected in detected.stdout.split(), detected.stdout
    (home / "openclaw.json").write_text(
        json.dumps(
            {
                "gateway": {"mode": "local", "controlUi": {"enabled": False}},
                "discovery": {"mdns": {"mode": "off"}},
                "agents": {
                    "defaults": {
                        "workspace": str(home / "workspace"),
                        "model": {"primary": "fixture/fixture-model"},
                    }
                },
                "models": {
                    "providers": {
                        "fixture": {
                            "baseUrl": f"http://127.0.0.1:{provider.server_port}/v1",
                            "apiKey": "synthetic-provider-key",
                            "api": "openai-completions",
                            "models": [
                                {
                                    "id": "fixture-model",
                                    "name": "fixture-model",
                                    "contextWindow": 32000,
                                    "maxTokens": 1024,
                                }
                            ],
                        }
                    }
                },
            }
        )
    )
    client = OpenClawGatewayClient(
        url=f"ws://127.0.0.1:{port}",
        token="synthetic-gateway-token",
        cwd=str(tmp_path),
        name="qualification",
    )
    log_path = tmp_path / "gateway.log"
    with log_path.open("wb") as log:
        process = await asyncio.create_subprocess_exec(
            binary,
            "gateway",
            "run",
            "--allow-unconfigured",
            "--bind",
            "loopback",
            "--port",
            str(port),
            "--auth",
            "token",
            "--token",
            "synthetic-gateway-token",
            "--tailscale",
            "off",
            env=env,
            cwd=home,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
    acp = None
    try:
        for _ in range(100):
            assert process.returncode is None, log_path.read_text()[-3000:]
            session = AiohttpGatewaySession(f"ws://127.0.0.1:{port}")
            try:
                await session.connect()
                client._session = session
                break
            except Exception:
                await session.close()
                await asyncio.sleep(0.2)
        else:
            pytest.fail("Gateway did not start: " + log_path.read_text()[-3000:])
        await client._handshake()
        await client._attach_session()
        assert client.session_key
        assert "sessions.create" in client._methods
        health = await client._session.request("health", {})
        assert health
        response = await client.respond("hello", timeout=30)
        assert requests, (
            "Gateway did not call the fixture provider: " + log_path.read_text()[-3000:]
        )
        assert response == "Verified OpenClaw response.", log_path.read_text()[-3000:]
        assert (
            await client.respond("again", timeout=30) == "Verified OpenClaw response."
        )
        assert requests and all(r["model"] == "fixture-model" for r in requests)
        key = client.session_key
        await client.close()
        client = OpenClawGatewayClient(
            url=f"ws://127.0.0.1:{port}",
            token="synthetic-gateway-token",
            session_key=key,
            cwd=str(tmp_path),
        )
        assert (
            await client.respond("resume", timeout=30) == "Verified OpenClaw response."
        )
        assert client.session_key == key
        bad = OpenClawGatewayClient(url=f"ws://127.0.0.1:{port}", token="invalid-token")
        try:
            with pytest.raises(OpenClawGatewayError):
                await bad.warmup()
        finally:
            await bad.close()
        acp = await asyncio.create_subprocess_exec(
            binary,
            "acp",
            "--url",
            f"ws://127.0.0.1:{port}",
            "--token",
            "synthetic-gateway-token",
            env=env,
            cwd=home,
            stdin=-1,
            stdout=-1,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )

        async def rpc(id, method, params):
            acp.stdin.write(
                (
                    json.dumps(
                        {"jsonrpc": "2.0", "id": id, "method": method, "params": params}
                    )
                    + "\n"
                ).encode()
            )
            await acp.stdin.drain()
            chunks = []
            async with asyncio.timeout(30):
                for _ in range(200):
                    line = await acp.stdout.readline()
                    assert line
                    frame = json.loads(line)
                    if frame.get("method") == "session/update":
                        update = frame["params"]["update"]
                        if update.get("sessionUpdate") == "agent_message_chunk":
                            chunks.append(update["content"].get("text", ""))
                    if frame.get("id") == id:
                        assert "error" not in frame, frame
                        return frame["result"], "".join(chunks)
            pytest.fail("No ACP reply")

        hello, _ = await rpc(
            1, "initialize", {"protocolVersion": 1, "clientCapabilities": {}}
        )
        assert hello["protocolVersion"] == 1
        session, _ = await rpc(
            2, "session/new", {"cwd": str(tmp_path), "mcpServers": []}
        )
        done, text = await rpc(
            3,
            "session/prompt",
            {
                "sessionId": session["sessionId"],
                "prompt": [{"type": "text", "text": "hello"}],
            },
        )
        assert done["stopReason"] == "end_turn"
        assert "Verified OpenClaw response." in text
    finally:
        if acp is not None and acp.returncode is None:
            os.killpg(acp.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(acp.wait(), 3)
            except TimeoutError:
                os.killpg(acp.pid, signal.SIGKILL)
                await acp.wait()
        await client.close()
        provider.shutdown()
        provider.server_close()
        thread.join(timeout=3)
        if process.returncode is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(process.wait(), 5)
            except TimeoutError:
                os.killpg(process.pid, signal.SIGKILL)
                await process.wait()
