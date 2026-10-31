"""Hermes gateway adapter: TUI JSON-RPC streaming, tools, errors."""

import pytest

from sleeptalk.agents.hermes_gateway_adapter import (
    HermesGatewayClient,
    HermesGatewayError,
    QueueLink,
    _tool_event,
)


class _FakeTui:
    def __init__(self) -> None:
        self.link = QueueLink(self.handle)

    def handle(self, method: str, params: dict) -> dict:
        if method == "session.create":
            return {"session_id": "s1"}
        if method == "prompt.submit":
            text = str(params.get("text") or "")
            if text == "fail":
                return {"status": "streaming"}
            return {"status": "streaming"}
        return {}


@pytest.mark.asyncio
async def test_streaming_response_sentences():
    events = [
        {"type": "message.delta", "payload": {"text": "Bitcoin is up. "}},
        {"type": "message.delta", "payload": {"text": "It gained 2%."}},
        {"type": "message.complete", "payload": {}},
    ]

    def handle(method: str, params: dict) -> dict:
        if method == "session.create":
            return {"session_id": "s1"}
        if method == "prompt.submit":
            for event in events:
                link._events.put_nowait(event)
            return {"status": "streaming"}
        return {}

    link = QueueLink(handle)
    client = HermesGatewayClient(link=link)
    result = await client.respond("price?")
    assert result == "Bitcoin is up. It gained 2%."


@pytest.mark.asyncio
async def test_tool_events_fire_with_detail():
    seen = []

    def handle(method: str, params: dict) -> dict:
        if method == "session.create":
            return {"session_id": "s1"}
        if method == "prompt.submit":
            link._events.put_nowait(
                {
                    "type": "tool.start",
                    "payload": {
                        "tool_name": "terminal",
                        "args": {"command": "cd /repo && git status"},
                    },
                }
            )
            link._events.put_nowait(
                {"type": "message.delta", "payload": {"text": "On main."}}
            )
            link._events.put_nowait({"type": "message.complete", "payload": {}})
            return {"status": "streaming"}
        return {}

    link = QueueLink(handle)
    client = HermesGatewayClient(link=link)
    async for _ in client.respond_streaming("status?", on_event=seen.append):
        pass
    assert "tool:bash" in seen
    assert any(s.startswith("tooldetail:running") and "/" not in s for s in seen)


@pytest.mark.asyncio
async def test_gateway_error_raises():
    def handle(method: str, params: dict) -> dict:
        if method == "session.create":
            return {"session_id": "s1"}
        if method == "prompt.submit":
            link._events.put_nowait({"type": "error", "payload": {"message": "nope"}})
            return {"status": "streaming"}
        return {}

    link = QueueLink(handle)
    client = HermesGatewayClient(link=link)
    with pytest.raises(HermesGatewayError):
        await client.respond("hi")


def test_tool_event_mapping():
    kind, detail = _tool_event({"tool_name": "web_search", "args": {"query": "btc price"}})
    assert kind == "search" and detail == "searching for btc price"
    kind, detail = _tool_event({"tool_name": "read_file", "args": {"path": "/a/b/main.py"}})
    assert kind == "read" and detail == "reading main.py"
    kind, detail = _tool_event({"tool_name": "memory", "args": {}})
    assert kind == "other" and detail == ""
    kind, detail = _tool_event({"tool_name": "grep", "args": {"pattern": "keepalive"}})
    assert kind == "search" and detail == "searching for keepalive"
    kind, detail = _tool_event({"tool_name": "websearch", "args": {}})
    assert kind == "search" and detail == ""
