"""Loopback tests for Agent Bridge v1. No vendor network."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent import EchoRemoteAgent
from sleeptalk.adapter import CONTRACT_VERSION, SessionIdentified, ThinkingStarted, typed_events
from sleeptalk.agent_bridge import (
    PROTOCOL_VERSION,
    AgentBridgeClient,
    AgentBridgeError,
    AgentBridgeServer,
    KeyStyle,
    SessionLimits,
    assert_transport_allowed,
    parse_frame,
    redact_frame,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
TOKEN = "loopback-token"


def _load_fixture(name: str) -> list[dict]:
    lines = (FIXTURES / name).read_text().splitlines()
    return [parse_frame(line) for line in lines if line.strip()]


async def _tcp_pair(agent: EchoRemoteAgent | None = None):
    bound = agent or EchoRemoteAgent()
    server = AgentBridgeServer(
        token=TOKEN,
        agent=bound,
        limits=SessionLimits(heartbeat_ms=80, missed_heartbeats=3),
    )
    host, port = await server.start_tcp("127.0.0.1", 0)
    client = AgentBridgeClient(key_style=KeyStyle.SNAKE)
    await client.connect(f"tcp://{host}:{port}", TOKEN)
    return server, client, bound


def test_contract_version_is_bridge_v1() -> None:
    assert CONTRACT_VERSION == 1
    assert PROTOCOL_VERSION == CONTRACT_VERSION


def test_fixtures_parse_and_redact_secrets() -> None:
    stream = _load_fixture("stream.jsonl")
    assert stream[0]["type"] == "hello"
    assert stream[-1]["type"] == "turn.completed"
    redacted = redact_frame(stream[0])
    assert redacted["auth"] == "<redacted>"
    dumped = json.dumps(redacted)
    assert TOKEN not in dumped
    assert "<redacted>" in dumped

    auth = _load_fixture("auth-fail.jsonl")
    assert auth[1]["code"] == "auth"
    assert TOKEN not in json.dumps(auth)

    abort = _load_fixture("abort.jsonl")
    assert abort[-2]["type"] == "turn.aborted"
    permission = next(frame for frame in abort if frame["type"] == "permission.requested")
    assert "allowed" not in permission
    assert "approved" not in permission

    pressure = _load_fixture("backpressure.jsonl")
    assert pressure[-1]["code"] == "backpressure"


def test_plaintext_rejected_off_loopback() -> None:
    with pytest.raises(AgentBridgeError) as exc:
        assert_transport_allowed("tcp", "example.com")
    assert exc.value.code == "protocol"
    with pytest.raises(AgentBridgeError):
        assert_transport_allowed("tcp", "0.0.0.0")
    with pytest.raises(AgentBridgeError):
        assert_transport_allowed("ws", "0.0.0.0")


@pytest.mark.asyncio
async def test_start_tcp_rejects_non_loopback() -> None:
    server = AgentBridgeServer(token=TOKEN, agent=EchoRemoteAgent())
    with pytest.raises(AgentBridgeError) as exc:
        await server.start_tcp("example.com", 0)
    assert exc.value.code == "protocol"


@pytest.mark.asyncio
async def test_stream_loopback() -> None:
    server, client, agent = await _tcp_pair()
    try:
        await client.warmup()
        assert agent.warmed
        events: list[object] = []
        sentences = [
            sentence
            async for sentence in client.respond_streaming(
                "hello there",
                on_event=typed_events(events.append),
            )
        ]
        assert sentences == ["You said hello there."]
        assert any(isinstance(event, SessionIdentified) for event in events)
        assert any(isinstance(event, ThinkingStarted) for event in events)
        assert client.session_id
        assert client.resume_token
        assert client.resume_token not in sentences[0]
    finally:
        await client.disconnect()
        await server.aclose()


@pytest.mark.asyncio
async def test_full_respond_loopback() -> None:
    server, client, _agent = await _tcp_pair()
    try:
        text = await client.respond("briefing")
        assert text == "You said briefing."
    finally:
        await client.disconnect()
        await server.aclose()


@pytest.mark.asyncio
async def test_abort_once() -> None:
    agent = EchoRemoteAgent(pause_s=30.0)
    server = AgentBridgeServer(token=TOKEN, agent=agent)
    host, port = await server.start_tcp("127.0.0.1", 0)
    client = AgentBridgeClient()
    await client.connect(f"tcp://{host}:{port}", TOKEN)
    try:
        await client.send_frame(
            {
                "v": 1,
                "type": "turn.prompt",
                "id": "p-hold",
                "turnId": 1,
                "text": "hold please",
                "mode": "stream",
            }
        )
        await asyncio.sleep(0.05)
        abort = {"v": 1, "type": "turn.abort", "id": "a-1", "turnId": 1}
        await client.send_frame(abort)
        await client.send_frame(abort)
        aborted = []
        for _ in range(12):
            frame = await client.next_frame()
            if frame.get("type") == "turn.aborted":
                aborted.append(frame)
            if len(aborted) >= 2:
                break
        assert aborted
        assert all(frame.get("ref") == "a-1" for frame in aborted)
        assert agent.abort_calls == 1
    finally:
        await client.disconnect()
        await server.aclose()


@pytest.mark.asyncio
async def test_auth_failure_does_not_echo_token() -> None:
    agent = EchoRemoteAgent()
    server = AgentBridgeServer(token=TOKEN, agent=agent)
    host, port = await server.start_tcp("127.0.0.1", 0)
    client = AgentBridgeClient()
    with pytest.raises(AgentBridgeError) as exc:
        await client.connect(f"tcp://{host}:{port}", "wrong-token-value")
    assert exc.value.code == "auth"
    assert "wrong-token-value" not in exc.value.message
    assert TOKEN not in exc.value.message
    await server.aclose()


@pytest.mark.asyncio
async def test_version_mismatch() -> None:
    agent = EchoRemoteAgent()
    server = AgentBridgeServer(token=TOKEN, agent=agent)
    host, port = await server.start_tcp("127.0.0.1", 0)
    reader, writer = await asyncio.open_connection(host, port)
    try:
        writer.write(
            b'{"v":2,"type":"hello","auth":{"scheme":"bearer","token":"loopback-token"}}\n'
        )
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout=2.0)
        frame = json.loads(line)
        assert frame["type"] == "hello.err"
        assert frame["code"] == "version_mismatch"
        assert TOKEN not in line.decode()
    finally:
        writer.close()
        await writer.wait_closed()
        await server.aclose()


@pytest.mark.asyncio
async def test_backpressure_slow_consumer() -> None:
    agent = EchoRemoteAgent(flood=8)
    server = AgentBridgeServer(
        token=TOKEN,
        agent=agent,
        limits=SessionLimits(initial_credit=1, send_queue=2, heartbeat_ms=200),
    )
    host, port = await server.start_tcp("127.0.0.1", 0)
    client = AgentBridgeClient(auto_credit=False)
    await client.connect(f"tcp://{host}:{port}", TOKEN)
    try:
        with pytest.raises(AgentBridgeError) as exc:
            collected = []
            async for sentence in client.respond_streaming("flood"):
                collected.append(sentence)
        assert exc.value.code == "backpressure"
        assert exc.value.retryable is True
    finally:
        await client.disconnect()
        await server.aclose()


@pytest.mark.asyncio
async def test_reconnect_rotates_resume_token() -> None:
    agent = EchoRemoteAgent()
    server = AgentBridgeServer(token=TOKEN, agent=agent)
    host, port = await server.start_tcp("127.0.0.1", 0)
    first = AgentBridgeClient()
    await first.connect(f"tcp://{host}:{port}", TOKEN)
    session_id = first.session_id
    old_resume = first.resume_token
    sentences = [s async for s in first.respond_streaming("hello")]
    assert sentences
    await first.disconnect()

    stolen = AgentBridgeClient()
    with pytest.raises(AgentBridgeError) as exc:
        await stolen.connect(
            f"tcp://{host}:{port}",
            TOKEN,
            session_id=session_id,
            resume_token="not-the-token",
        )
    assert exc.value.code == "auth"

    second = AgentBridgeClient()
    await second.connect(
        f"tcp://{host}:{port}",
        TOKEN,
        session_id=session_id,
        resume_token=old_resume,
    )
    try:
        assert second.session_id == session_id
        assert second.resume_token != old_resume
        again = [s async for s in second.respond_streaming("again")]
        assert again == ["You said again."]
        third = AgentBridgeClient()
        with pytest.raises(AgentBridgeError) as reused:
            await third.connect(
                f"tcp://{host}:{port}",
                TOKEN,
                session_id=session_id,
                resume_token=old_resume,
            )
        assert reused.value.code == "auth"
    finally:
        await second.disconnect()
        await server.aclose()


@pytest.mark.asyncio
async def test_permission_silence_is_not_approval() -> None:
    agent = EchoRemoteAgent(request_permission=True)
    server = AgentBridgeServer(token=TOKEN, agent=agent)
    host, port = await server.start_tcp("127.0.0.1", 0)
    client = AgentBridgeClient()
    await client.connect(f"tcp://{host}:{port}", TOKEN)
    try:
        events: list[object] = []
        sentences = [
            sentence
            async for sentence in client.respond_streaming(
                "need a tool",
                on_event=typed_events(events.append),
            )
        ]
        assert sentences
        decisions = [
            getattr(event, "decision", None)
            for event in events
            if getattr(event, "kind", None) == "permission.resolve"
        ]
        assert decisions == []
        seen_types = [getattr(event, "kind", None) for event in events]
        assert "permission.requested" in seen_types
    finally:
        await client.disconnect()
        await server.aclose()


@pytest.mark.asyncio
async def test_heartbeat_and_graceful_disconnect() -> None:
    agent = EchoRemoteAgent()
    server = AgentBridgeServer(
        token=TOKEN,
        agent=agent,
        limits=SessionLimits(heartbeat_ms=40, missed_heartbeats=3),
    )
    host, port = await server.start_tcp("127.0.0.1", 0)
    client = AgentBridgeClient()
    await client.connect(f"tcp://{host}:{port}", TOKEN)
    try:
        await asyncio.sleep(0.12)
        assert client.heartbeats_received >= 1
        await client.disconnect("done")
        assert client.link is None
    finally:
        await server.aclose()


@pytest.mark.asyncio
async def test_websocket_stream_loopback() -> None:
    agent = EchoRemoteAgent()
    server = AgentBridgeServer(token=TOKEN, agent=agent)
    host, port = await server.start_ws("127.0.0.1", 0)
    client = AgentBridgeClient(key_style=KeyStyle.CAMEL)
    await client.connect(f"ws://{host}:{port}/v1/agent-bridge", TOKEN)
    try:
        sentences = [s async for s in client.respond_streaming("hello there")]
        assert sentences == ["You said hello there."]
    finally:
        await client.disconnect()
        await server.aclose()


@pytest.mark.asyncio
async def test_websocket_rejects_token_query() -> None:
    agent = EchoRemoteAgent()
    server = AgentBridgeServer(token=TOKEN, agent=agent)
    host, port = await server.start_ws("127.0.0.1", 0)
    client = AgentBridgeClient()
    with pytest.raises(AgentBridgeError):
        await client.connect(f"ws://{host}:{port}/v1/agent-bridge?token={TOKEN}", TOKEN)
    await server.aclose()
