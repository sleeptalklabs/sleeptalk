from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from sleeptalk.adapter import (
    ControlAccepted,
    PermissionDecision,
    PermissionRequested,
    ResolvePermission,
    SupportLevel,
    ThinkingStarted,
    adapter_capabilities,
    adapter_control,
    typed_events,
)
from sleeptalk.agents.acp import AcpClient, AcpError
from sleeptalk.agents.profiles import AcpProfile

FAKE_ACP_SERVER = r"""
from __future__ import annotations

import json
import os
import sys


def out(msg: dict) -> None:
    sys.stdout.write(json.dumps(msg, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def respond(req_id, result) -> None:
    out({"jsonrpc": "2.0", "id": req_id, "result": result})


def update(session_update: str, **fields) -> None:
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


def log_wire(line: str) -> None:
    path = os.environ.get("ACP_WIRE_LOG")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(line)
        if not line.endswith("\n"):
            handle.write("\n")


def main() -> None:
    mode = os.environ.get("ACP_MODE", "sentence")
    sentence = os.environ.get("ACP_SENTENCE", "Hello from the agent.")
    session_id = os.environ.get("ACP_SESSION_ID", "s1")
    flood_n = int(os.environ.get("ACP_FLOOD_N", "40"))
    prompt_id = None
    waiting_perm = False

    for raw in sys.stdin:
        log_wire(raw)
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
                    "agentInfo": {"name": "fake-acp"},
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
            if mode == "die":
                sys.exit(1)
            if mode == "hang":
                continue
            if mode == "malformed":
                sys.stdout.write("this is not json\n")
                sys.stdout.flush()
                update(
                    "agent_message_chunk",
                    content={"type": "text", "text": sentence},
                )
                respond(req_id, {"stopReason": "end_turn"})
                prompt_id = None
                continue
            if mode == "flood":
                for index in range(flood_n):
                    update(
                        "agent_message_chunk",
                        content={"type": "text", "text": f"Word{index}. "},
                    )
                respond(req_id, {"stopReason": "end_turn"})
                prompt_id = None
                continue
            if mode == "permission":
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
            update("current_mode_update", mode="default")
            update("cursor/todo", items=[])
            update("agent_thought_chunk", content={"type": "text", "text": "hmm"})
            update(
                "agent_message_chunk",
                content={"type": "text", "text": sentence},
            )
            update(
                "tool_call",
                toolCallId="t1",
                kind="execute",
                title="ls",
                status="pending",
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
"""


@pytest.fixture
def fake_acp_script(tmp_path: Path) -> Path:
    path = tmp_path / "fake_acp_server.py"
    path.write_text(FAKE_ACP_SERVER, encoding="utf-8")
    return path


def _profile(name: str, script: Path) -> AcpProfile:
    return AcpProfile(name=name, binary=sys.executable, extra_args=(str(script),))


def _client(
    script: Path,
    *,
    name: str = "alpha",
    permission_mode: str = "auto",
    event_queue_maxsize: int = 64,
    env: dict[str, str] | None = None,
    session_id: str | None = None,
) -> AcpClient:
    return AcpClient(
        _profile(name, script),
        cwd=str(script.parent),
        session_id=session_id,
        permission_mode=permission_mode,
        event_queue_maxsize=event_queue_maxsize,
        env=env,
    )


def _wire_methods(path: Path) -> list[str]:
    methods: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        message = json.loads(line)
        if "method" in message:
            methods.append(message["method"])
    return methods


def _wire_calls(path: Path) -> list[dict]:
    calls: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        calls.append(json.loads(line))
    return calls


async def _wait_permission(seen: list, timeout: float = 3.0) -> PermissionRequested:
    async def _poll() -> PermissionRequested:
        while True:
            for event in seen:
                if isinstance(event, PermissionRequested):
                    return event
            await asyncio.sleep(0.02)

    return await asyncio.wait_for(_poll(), timeout=timeout)


async def _collect(
    stream: AsyncIterator[str], timeout: float = 5.0
) -> list[str]:
    async def _run() -> list[str]:
        return [sentence async for sentence in stream]

    return await asyncio.wait_for(_run(), timeout=timeout)


@pytest.mark.asyncio
async def test_one_client_two_profiles_stream_a_sentence(fake_acp_script: Path) -> None:
    heard: dict[str, str] = {}
    for name, line in (("alpha", "Hello from alpha."), ("beta", "Hello from beta.")):
        client = _client(
            fake_acp_script, name=name, env={"ACP_SENTENCE": line}
        )
        try:
            heard[name] = await asyncio.wait_for(client.respond("hi"), timeout=5)
            assert client.server_capabilities.get("loadSession") is True
            caps = adapter_capabilities(client)
            assert caps.structured_events.level is SupportLevel.SUPPORTED
            assert caps.tool_lifecycle.level is SupportLevel.SUPPORTED
            assert caps.permissions.level is SupportLevel.SUPPORTED
            assert caps.voice_sessions.level is SupportLevel.UNSUPPORTED
        finally:
            await client.close()
    assert "alpha" in heard["alpha"]
    assert "beta" in heard["beta"]


@pytest.mark.asyncio
async def test_golden_wire_initialize_session_new_prompt(
    fake_acp_script: Path, tmp_path: Path
) -> None:
    wire = tmp_path / "wire.log"
    client = _client(
        fake_acp_script,
        env={"ACP_SENTENCE": "Hello there.", "ACP_WIRE_LOG": str(wire)},
    )
    try:
        text = await asyncio.wait_for(client.respond("Hi"), timeout=5)
    finally:
        await client.close()
    assert "Hello there" in text
    methods = _wire_methods(wire)
    assert methods[:3] == ["initialize", "session/new", "session/prompt"]
    init = next(call for call in _wire_calls(wire) if call.get("method") == "initialize")
    assert init["params"]["protocolVersion"] == 1
    assert "clientCapabilities" in init["params"]
    assert any(item["code"] == "unknown_update" for item in client.diagnostics)
    assert any(item["code"] == "unknown_extension" for item in client.diagnostics)


@pytest.mark.asyncio
async def test_malformed_frame_is_skipped(fake_acp_script: Path) -> None:
    client = _client(
        fake_acp_script,
        env={"ACP_MODE": "malformed", "ACP_SENTENCE": "Still works."},
    )
    try:
        text = await asyncio.wait_for(client.respond("hi"), timeout=5)
    finally:
        await client.close()
    assert "Still works" in text


@pytest.mark.asyncio
async def test_slow_consumer_bounded_queue_stays_responsive(
    fake_acp_script: Path,
) -> None:
    client = _client(
        fake_acp_script,
        event_queue_maxsize=4,
        env={"ACP_MODE": "flood", "ACP_FLOOD_N": "50"},
    )

    async def consume() -> None:
        async for _sentence in client.respond_streaming("hi"):
            await asyncio.sleep(0.05)

    task = asyncio.create_task(consume())
    try:
        await asyncio.sleep(0.2)
        await asyncio.wait_for(client.abort(), timeout=1.0)
        assert client._event_queue.maxsize == 4
        assert client._event_queue.qsize() <= 4
        assert any(item["code"] == "event_queue_full" for item in client.diagnostics)
        try:
            await asyncio.wait_for(task, timeout=2.0)
        except AcpError:
            pass
    finally:
        if not task.done():
            task.cancel()
        await client.close()


@pytest.mark.asyncio
async def test_permission_requested_then_resolve(
    fake_acp_script: Path,
) -> None:
    client = _client(
        fake_acp_script,
        permission_mode="pending",
        env={"ACP_MODE": "permission", "ACP_SENTENCE": "Allowed."},
    )
    seen: list = []
    sink = typed_events(seen.append)
    task = asyncio.create_task(
        _collect(client.respond_streaming("do it", on_event=sink))
    )
    try:
        request = await _wait_permission(seen)
        assert request.prompt
        assert request.request_id == "perm-1"
        result = await adapter_control(
            client,
            ResolvePermission(
                request_id=request.request_id,
                decision=PermissionDecision.ALLOW_ONCE,
            ),
        )
        assert isinstance(result, ControlAccepted)
        sentences = await asyncio.wait_for(task, timeout=5)
        assert any("Allowed" in sentence for sentence in sentences)
        assert any(isinstance(event, PermissionRequested) for event in seen)
        assert not any(
            isinstance(event, ThinkingStarted) for event in seen
        )
    finally:
        if not task.done():
            task.cancel()
        await client.close()


@pytest.mark.asyncio
async def test_an_auto_granted_request_is_not_reported_as_a_question(
    fake_acp_script: Path,
) -> None:
    """Auto mode answers every request itself. Reporting them had a Cursor
    call read each tool call aloud as a yes-or-no question nothing waited on."""
    client = _client(
        fake_acp_script,
        permission_mode="auto",
        env={"ACP_MODE": "permission", "ACP_SENTENCE": "Allowed."},
    )
    seen: list = []
    try:
        sentences = await _collect(client.respond_streaming("do it", on_event=typed_events(seen.append)))
        assert any("Allowed" in sentence for sentence in sentences)
        assert not any(isinstance(event, PermissionRequested) for event in seen)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_abort_sends_session_cancel_and_settles(
    fake_acp_script: Path, tmp_path: Path
) -> None:
    wire = tmp_path / "cancel.log"
    client = _client(
        fake_acp_script,
        env={"ACP_MODE": "hang", "ACP_WIRE_LOG": str(wire)},
    )
    task = asyncio.create_task(client.respond("hi"))
    try:
        await asyncio.sleep(0.3)
        pending = list(client._pending.values())
        assert pending
        await client.abort()
        with pytest.raises(AcpError, match="cancel"):
            await asyncio.wait_for(task, timeout=2)
        assert all(future.done() for future in pending)
        # The fake agent logs what it received from its own process, so the
        # cancel can land in its log a moment after abort() returns.
        for _ in range(100):
            if "session/cancel" in _wire_methods(wire):
                break
            await asyncio.sleep(0.02)
        assert "session/cancel" in _wire_methods(wire)
    finally:
        if not task.done():
            task.cancel()
        await client.close()


@pytest.mark.asyncio
async def test_subprocess_death_settles_pending_once(fake_acp_script: Path) -> None:
    client = _client(fake_acp_script, env={"ACP_MODE": "hang"})
    task = asyncio.create_task(client.respond("hi"))
    try:
        await asyncio.sleep(0.3)
        assert client._proc is not None
        pending = list(client._pending.values())
        assert pending
        client._proc.kill()
        with pytest.raises(AcpError):
            await asyncio.wait_for(task, timeout=2)
        assert all(future.done() for future in pending)
        client._settle_pending(lambda: AcpError("again"))
        assert all(future.done() for future in pending)
    finally:
        if not task.done():
            task.cancel()
        await client.close()


@pytest.mark.asyncio
async def test_abort_stops_speaking_already_buffered_sentences(fake_acp_script: Path) -> None:
    # Flood queues many chunks before the first sentence is consumed; abort
    # must not let any of them reach speech.
    client = _client(fake_acp_script, env={"ACP_MODE": "flood", "ACP_FLOOD_N": "50"})
    spoken: list[str] = []
    try:
        # The fake may finish the prompt before abort lands, in which case no
        # cancel error is left to raise; either way nothing more is spoken.
        try:
            async for sentence in client.respond_streaming("hi"):
                spoken.append(sentence)
                await client.abort()
        except AcpError as exc:
            assert "cancel" in str(exc)
        assert len(spoken) == 1
    finally:
        await client.close()


# Gemini-style ids: nothing matches the id aliases, so only kind can decide.
_PROCEED_OPTIONS = [
    {"optionId": "proceed_always", "kind": "allow_always"},
    {"optionId": "proceed_once", "kind": "allow_once"},
    {"optionId": "cancel", "kind": "reject_once"},
]


def test_permission_options_are_chosen_by_kind_not_position() -> None:
    from sleeptalk.agents.acp import _decisions_from_options, _option_id_for_decision

    assert _option_id_for_decision(PermissionDecision.DENY, _PROCEED_OPTIONS) == "cancel"
    assert _option_id_for_decision(PermissionDecision.ALLOW_ONCE, _PROCEED_OPTIONS) == "proceed_once"
    assert _option_id_for_decision(PermissionDecision.ALLOW_SESSION, _PROCEED_OPTIONS) == "proceed_always"
    assert _decisions_from_options(_PROCEED_OPTIONS) == {
        PermissionDecision.DENY, PermissionDecision.ALLOW_ONCE, PermissionDecision.ALLOW_SESSION,
    }


def test_unrecognized_permission_options_cancel_instead_of_allowing() -> None:
    from sleeptalk.agents.acp import _decisions_from_options, _option_id_for_decision

    options = [{"optionId": "yes", "kind": "something_new"}, {"optionId": "go"}]
    assert _option_id_for_decision(PermissionDecision.DENY, options) is None
    assert _option_id_for_decision(PermissionDecision.ALLOW_ONCE, options) is None
    assert _decisions_from_options(options) == {PermissionDecision.DENY}
    # A single grant never widens into a standing rule.
    allow_always_only = [{"optionId": "a", "kind": "allow_always"}]
    assert _option_id_for_decision(PermissionDecision.ALLOW_ONCE, allow_always_only) is None


def test_agent_file_reads_stay_inside_the_workspace(tmp_path: Path) -> None:
    from sleeptalk.agents.acp import read_workspace_text

    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "notes.txt").write_text("inside")
    (tmp_path / "secret.txt").write_text("outside")
    (workspace / "escape").symlink_to(tmp_path / "secret.txt")
    assert read_workspace_text(str(workspace), str(workspace / "notes.txt")) == "inside"
    assert read_workspace_text(str(workspace), "notes.txt") == "inside"
    for path in (str(tmp_path / "secret.txt"), "../secret.txt", str(workspace / "escape")):
        with pytest.raises(PermissionError):
            read_workspace_text(str(workspace), path)
