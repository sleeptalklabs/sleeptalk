import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest

from sleeptalk.agents.hermes_adapter import HermesACPClient, HermesACPError, tool_detail_from_title

pytestmark = pytest.mark.usefixtures("dummy_cli_bins")


def test_tool_detail_from_title():
    assert tool_detail_from_title("bash", "git status") == "running git status"
    assert tool_detail_from_title("search", "overnight news") == "searching for overnight news"
    assert tool_detail_from_title("read", "main.py") == "reading main.py"
    assert tool_detail_from_title("other", "Thinking") == "Thinking"
    assert tool_detail_from_title("bash", "") == ""


def _acp_response(req_id, result):
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _acp_update(update):
    return {"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": "s1", "update": update}}


class FakeACPProcess:
    """Feeds scripted NDJSON lines; records writes."""

    def __init__(self, messages: list[dict]):
        self._lines = [json.dumps(m).encode() + b"\n" for m in messages]
        self._idx = 0
        self.stdin = MagicMock()
        self.stdin.write = MagicMock()
        self.stdin.drain = lambda: asyncio.sleep(0)
        self.stdout = MagicMock()
        self.stdout.read = MagicMock(side_effect=self._read)
        self._returncode = None

    async def _read(self, n):
        # Yield to the loop so handshake awaits resume between messages;
        # without this the reader consumes responses before their requests
        # are even registered (real ACP can't answer before we ask).
        await asyncio.sleep(0)
        if self._idx >= len(self._lines):
            await asyncio.sleep(3600)  # block like a real idle process
            return b""
        line = self._lines[self._idx]
        self._idx += 1
        return line

    @property
    def returncode(self):
        return self._returncode

    def terminate(self):
        pass

    def kill(self):
        pass

    async def wait(self):
        return 0


def _handshake_messages():
    return [
        _acp_response(1, {"agentInfo": {"name": "hermes-agent"}}),
        _acp_response(2, {"sessionId": "s1"}),
    ]


@pytest.mark.asyncio
async def test_handshake_and_simple_response():
    messages = _handshake_messages() + [
        _acp_update({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "Hello "}}),
        _acp_update({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "there."}}),
        _acp_response(3, {"stopReason": "end_turn"}),
    ]
    proc = FakeACPProcess(messages)
    client = HermesACPClient(cwd="/tmp")

    with patch("asyncio.create_subprocess_exec", return_value=proc):
        result = await client.respond("Hi")

    assert result == "Hello there."
    # initialize, session/new, session/prompt were written
    calls = [json.loads(c[0][0].decode()) for c in proc.stdin.write.call_args_list]
    methods = [c.get("method") for c in calls]
    assert methods == ["initialize", "session/new", "session/prompt"]
    assert calls[2]["params"]["sessionId"] == "s1"


@pytest.mark.asyncio
async def test_streaming_events_fire():
    messages = _handshake_messages() + [
        _acp_update({"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": "hmm"}}),
        _acp_update({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "Done."}}),
        _acp_update({"sessionUpdate": "tool_call", "toolCallId": "t1", "kind": "execute", "title": "ls"}),
        _acp_response(3, {"stopReason": "end_turn"}),
    ]
    proc = FakeACPProcess(messages)
    client = HermesACPClient(cwd="/tmp")

    seen = []
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        async for _ in client.respond_streaming("Hi", on_event=seen.append):
            pass

    semantic = [event for event in seen if not event.startswith("workbench:")]
    visual = [
        json.loads(event.removeprefix("workbench:"))
        for event in seen
        if event.startswith("workbench:")
        and json.loads(event.removeprefix("workbench:")).get("kind") != "latency"
    ]
    assert semantic == ["thinking", "text", "tool", "tooldetail:running ls", "tool:bash"]
    assert [event["kind"] for event in visual] == ["thinking", "text", "tool_start"]
    assert visual[1]["text"] == "Done."
    assert visual[2]["title"] == "ls"


@pytest.mark.asyncio
async def test_permission_request_auto_approved():
    messages = _handshake_messages() + [
        {"jsonrpc": "2.0", "id": 99, "method": "session/request_permission",
         "params": {"options": [{"optionId": "allow_once"}, {"optionId": "allow_always"}]}},
        _acp_update({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "OK."}}),
        _acp_response(3, {"stopReason": "end_turn"}),
    ]
    proc = FakeACPProcess(messages)
    client = HermesACPClient(cwd="/tmp")

    with patch("asyncio.create_subprocess_exec", return_value=proc):
        result = await client.respond("Do it")

    assert result == "OK."
    calls = [json.loads(c[0][0].decode()) for c in proc.stdin.write.call_args_list]
    perm = next(c for c in calls if c.get("id") == 99)
    # Auto mode never grants a standing permission: allow_always is skipped
    # and the single-use option answers the request.
    assert perm["result"]["outcome"]["optionId"] == "allow_once"


@pytest.mark.asyncio
async def test_idle_abort_does_not_cancel_unowned_work():
    messages = _handshake_messages()
    proc = FakeACPProcess(messages)
    client = HermesACPClient(cwd="/tmp")

    with patch("asyncio.create_subprocess_exec", return_value=proc):
        await client._ensure_started()
        await client.abort()

    calls = [json.loads(c[0][0].decode()) for c in proc.stdin.write.call_args_list]
    assert not any(c.get("method") == "session/cancel" for c in calls)


@pytest.mark.asyncio
async def test_internal_error_restarts_session_next_turn():
    """After an ACP internal error the next turn must use a fresh process —
    a wedged hermes session answers every prompt with 'Queued for the next
    turn.' otherwise."""
    procs = [
        FakeACPProcess(_handshake_messages() + [
            {"jsonrpc": "2.0", "id": 3, "error": {"code": -32603, "message": "Internal error"}},
        ]),
        FakeACPProcess(_handshake_messages() + [
            _acp_update({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "Back."}}),
            _acp_response(3, {"stopReason": "end_turn"}),
        ]),
    ]
    client = HermesACPClient(cwd="/tmp")

    with patch("asyncio.create_subprocess_exec", side_effect=procs) as spawn:
        with pytest.raises(HermesACPError):
            await client.respond("Hi")
        result = await client.respond("Again")

    assert result == "Back."
    assert spawn.call_count == 2  # fresh process for the recovery turn
