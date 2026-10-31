"""Independent Agent Bridge v1 checks. No vendor network."""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "examples" / "remote_agent"))

from agent import EchoRemoteAgent
from sleeptalk.agent_bridge import (
    AgentBridgeClient,
    AgentBridgeError,
    AgentBridgeServer,
    SessionLimits,
    parse_frame,
    redact_frame,
)

FIXTURES = ROOT / "examples" / "remote_agent" / "fixtures"
INDEPENDENT = Path(__file__).resolve().parent / "fixtures" / "bridge_independent_host.mjs"
TOKEN = "loopback-token"
DOCS = ROOT / "docs" / "agent-bridge-protocol.md"


def test_golden_fixtures_parse() -> None:
    stream = [
        parse_frame(line)
        for line in (FIXTURES / "stream.jsonl").read_text().splitlines()
        if line.strip()
    ]
    assert stream[0]["type"] == "hello"
    assert stream[-1]["type"] == "turn.completed"
    redacted = redact_frame(stream[0])
    assert redacted["auth"] == "<redacted>"

    mismatch = parse_frame(
        '{"v":2,"type":"hello","auth":{"scheme":"bearer","token":"<redacted>"}}'
    )
    assert mismatch["v"] == 2


def test_docs_cover_local_and_remote_threats() -> None:
    text = DOCS.read_text()
    assert "## Local process versus remote network" in text
    assert "## Deployment and security defaults" in text
    assert "bridge_independent_host.mjs" in text
    assert "loopback" in text
    assert "TLS" in text


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required for the independent fixture")
@pytest.mark.asyncio
async def test_independent_host_completes_session() -> None:
    agent = EchoRemoteAgent()
    server = AgentBridgeServer(
        token=TOKEN,
        agent=agent,
        limits=SessionLimits(heartbeat_ms=80, missed_heartbeats=3),
    )
    host, port = await server.start_tcp("127.0.0.1", 0)
    try:
        proc = await asyncio.create_subprocess_exec(
            "node",
            str(INDEPENDENT),
            "session",
            host,
            str(port),
            env={**os.environ, "BRIDGE_TOKEN": TOKEN},
        )
        code = await asyncio.wait_for(proc.wait(), timeout=8)
        assert code == 0
        assert agent.prompts == ["hello there"]
    finally:
        await server.aclose()


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required for the independent fixture")
@pytest.mark.asyncio
async def test_independent_host_version_mismatch() -> None:
    server = AgentBridgeServer(token=TOKEN, agent=EchoRemoteAgent())
    host, port = await server.start_tcp("127.0.0.1", 0)
    try:
        proc = await asyncio.create_subprocess_exec(
            "node",
            str(INDEPENDENT),
            "mismatch",
            host,
            str(port),
            env={**os.environ, "BRIDGE_TOKEN": TOKEN},
            stderr=asyncio.subprocess.PIPE,
        )
        _stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=8)
        assert proc.returncode == 0
        assert TOKEN.encode() not in (stderr or b"")
    finally:
        await server.aclose()


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required for the independent fixture")
@pytest.mark.asyncio
async def test_disconnect_then_new_session() -> None:
    agent = EchoRemoteAgent()
    server = AgentBridgeServer(token=TOKEN, agent=agent)
    host, port = await server.start_tcp("127.0.0.1", 0)
    try:
        drop = await asyncio.create_subprocess_exec(
            "node",
            str(INDEPENDENT),
            "drop",
            host,
            str(port),
            env={**os.environ, "BRIDGE_TOKEN": TOKEN},
        )
        assert await asyncio.wait_for(drop.wait(), timeout=8) == 0
        client = AgentBridgeClient()
        await client.connect(f"tcp://{host}:{port}", TOKEN)
        try:
            text = await asyncio.wait_for(client.respond("second"), timeout=5)
            assert text == "You said second."
        finally:
            await client.disconnect()
    finally:
        await server.aclose()


def test_python_client_still_rejects_off_loopback() -> None:
    from sleeptalk.agent_bridge import assert_transport_allowed

    with pytest.raises(AgentBridgeError) as exc:
        assert_transport_allowed("tcp", "example.com")
    assert exc.value.code == "protocol"


class _LongAgent:
    """Streams more sentences than the initial credit and the send queue."""

    def __init__(self, count: int = 60) -> None:
        self.count = count

    async def warmup(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def abort(self) -> None:
        return None

    async def respond(self, text: str, timeout: float = 300.0) -> str:
        return text

    def respond_streaming(self, text: str, *, on_event=None):
        async def stream():
            for index in range(self.count):
                yield f"Sentence {index}."

        return stream()


@pytest.mark.asyncio
async def test_long_replies_and_consecutive_turns_share_one_connection() -> None:
    # Regression: the server errored once credit (16) or the unacked queue
    # (32) ran out, and rejected every prompt after the first on a connection.
    server = AgentBridgeServer(token=TOKEN, agent=_LongAgent(60))
    host, port = await server.start_ws("127.0.0.1", 0)
    client = AgentBridgeClient()
    try:
        await client.connect(f"ws://{host}:{port}/v1/agent-bridge", TOKEN)
        for _turn in range(3):
            sentences = await asyncio.wait_for(
                _collect_sentences(client.respond_streaming("go")), timeout=10
            )
            assert len(sentences) == 60
    finally:
        await client.close()
        await server.aclose()


@pytest.mark.asyncio
async def test_agent_reports_backpressure_when_host_never_grants_credit() -> None:
    server = AgentBridgeServer(
        token=TOKEN,
        agent=_LongAgent(40),
        limits=SessionLimits(heartbeat_ms=200, missed_heartbeats=50),
    )
    host, port = await server.start_ws("127.0.0.1", 0)
    client = AgentBridgeClient(auto_credit=False)
    try:
        await client.connect(f"ws://{host}:{port}/v1/agent-bridge", TOKEN)
        with pytest.raises(AgentBridgeError) as caught:
            await asyncio.wait_for(_collect_sentences(client.respond_streaming("go")), timeout=10)
        assert caught.value.code == "backpressure"
    finally:
        await client.close()
        await server.aclose()


async def _collect_sentences(stream) -> list[str]:
    return [sentence async for sentence in stream]
