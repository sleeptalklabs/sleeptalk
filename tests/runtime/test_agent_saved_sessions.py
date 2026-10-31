"""Agents keep their conversation across restarts of a long-lived Discord bot.

pi and OMP continue a session saved in a folder; Hermes resumes its saved
session ID without replaying the old conversation into the next turn.
"""
from __future__ import annotations

import asyncio
import json
import stat
from unittest.mock import MagicMock, patch

import pytest

@pytest.mark.asyncio
async def test_pi_continues_its_saved_conversation(tmp_path, dummy_cli_bins):
    from sleeptalk.agents.pi_rpc import OmpRPCClient, PiRPCClient

    client = PiRPCClient(cwd=str(tmp_path), pi_bin="/bin/sh")
    folder = tmp_path / "sessions"
    folder.mkdir()
    assert client.use_saved_session(folder) is True
    launched = {}

    async def spawn(args, env=None):
        launched["args"] = args

    client._spawn = spawn
    await client._ensure_started()
    args = launched["args"]
    assert args[args.index("--session-dir") + 1] == str(folder)
    assert "--continue" in args and "--no-session" not in args
    # Without a saved session nothing is written, as before.
    plain = PiRPCClient(cwd=str(tmp_path), pi_bin="/bin/sh")
    plain._spawn = spawn
    await plain._ensure_started()
    assert "--no-session" in launched["args"] and "--session-dir" not in launched["args"]
    # OMP's protected launch goes through its SDK, which saves no session.
    assert OmpRPCClient(cwd=str(tmp_path), permission_mode="pending").use_saved_session(folder) is False


@pytest.mark.asyncio
async def test_pi_reset_sets_the_old_conversation_aside(tmp_path):
    from sleeptalk.agents.pi_rpc import PiRPCClient

    folder = tmp_path / "9"
    folder.mkdir()
    (folder / "2026-10-07_a.jsonl").write_text("{}\n")
    client = PiRPCClient(cwd=str(tmp_path))
    client.use_saved_session(folder)
    await client.start_new_conversation()
    assert list(folder.glob("*.jsonl")) == []
    assert (tmp_path / "9-earlier" / "2026-10-07_a.jsonl").exists()


class _GatedACPProcess:
    """An ACP process that answers each request only after it is written,
    like a real one: replayed history arrives before the resume reply."""

    def __init__(self, answers: dict[str, list[dict]]):
        self.answers = answers
        self.lines: asyncio.Queue[bytes] = asyncio.Queue()
        self.stdin = MagicMock()
        self.stdin.write = MagicMock(side_effect=self._write)
        self.stdin.drain = lambda: asyncio.sleep(0)
        self.stdout = MagicMock()
        self.stdout.read = MagicMock(side_effect=self._read)
        self.methods: list[str] = []
        self.returncode = None

    def _write(self, data: bytes):
        message = json.loads(data.decode())
        method = message.get("method")
        if not method or "id" not in message:
            return
        self.methods.append(method)
        for reply in self.answers.get(method, []):
            reply = dict(reply)
            if reply.get("id") == "?":
                reply["id"] = message["id"]
            self.lines.put_nowait(json.dumps(reply).encode() + b"\n")

    async def _read(self, n):
        return await self.lines.get()

    def terminate(self):
        pass

    def kill(self):
        pass

    async def wait(self):
        return 0


def _update(text):
    return {"jsonrpc": "2.0", "method": "session/update",
            "params": {"sessionId": "s-old", "update": {"sessionUpdate": "agent_message_chunk",
                                                       "content": {"type": "text", "text": text}}}}


@pytest.mark.asyncio
async def test_hermes_resumes_its_saved_session_without_replaying_it(tmp_path, dummy_cli_bins):
    from sleeptalk.agents.hermes_adapter import HermesACPClient

    (tmp_path / "hermes-session-id").write_text("s-old\n")
    proc = _GatedACPProcess({
        "initialize": [{"jsonrpc": "2.0", "id": "?", "result": {
            "agentCapabilities": {"loadSession": True, "sessionCapabilities": {"resume": {}}}}}],
        # Hermes replays the old conversation before it answers.
        "session/resume": [_update("An old answer from yesterday."), {"jsonrpc": "2.0", "id": "?", "result": {}}],
        "session/prompt": [_update("Fresh."), {"jsonrpc": "2.0", "id": "?", "result": {"stopReason": "end_turn"}}],
    })
    client = HermesACPClient(cwd="/tmp")
    assert client.use_saved_session(tmp_path) is True
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        answer = await client.respond("Hi again")
    assert answer == "Fresh."
    assert proc.methods == ["initialize", "session/resume", "session/prompt"]
    await client.close()


@pytest.mark.asyncio
async def test_hermes_starts_and_saves_a_session_when_none_or_resume_fails(tmp_path, dummy_cli_bins):
    from sleeptalk.agents.hermes_adapter import HermesACPClient

    (tmp_path / "hermes-session-id").write_text("s-gone\n")
    proc = _GatedACPProcess({
        "initialize": [{"jsonrpc": "2.0", "id": "?", "result": {"agentCapabilities": {"loadSession": True}}}],
        "session/load": [{"jsonrpc": "2.0", "id": "?", "error": {"code": -32002, "message": "not found"}}],
        "session/new": [{"jsonrpc": "2.0", "id": "?", "result": {"sessionId": "s-new"}}],
        "session/prompt": [_update("Hello."), {"jsonrpc": "2.0", "id": "?", "result": {"stopReason": "end_turn"}}],
    })
    client = HermesACPClient(cwd="/tmp")
    client.use_saved_session(tmp_path)
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        assert await client.respond("Hi") == "Hello."
    assert proc.methods == ["initialize", "session/load", "session/new", "session/prompt"]
    assert (tmp_path / "hermes-session-id").read_text().strip() == "s-new"
    assert stat.S_IMODE((tmp_path / "hermes-session-id").stat().st_mode) == 0o600
    await client.close()
