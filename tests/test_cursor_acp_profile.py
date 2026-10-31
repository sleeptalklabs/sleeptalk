from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from sleeptalk.adapter import (
    AnswerClarification,
    ClarificationRequested,
    ControlAccepted,
    PermissionDecision,
    PermissionRequested,
    ResolvePermission,
    SupportLevel,
    adapter_capabilities,
    adapter_control,
    typed_events,
)
from sleeptalk.agents.acp import AcpError
from sleeptalk.agents.cursor_acp import CursorAcpClient
from sleeptalk.agents.profiles.cursor import (
    CURSOR_PROFILE,
    ask_question_answered,
    cancelled_outcome,
    create_plan_accepted,
    first_question_answers,
    nested_outcome,
)

FAKE = Path(__file__).with_name("fixtures") / "cursor_acp_fake.py"


@pytest.mark.asyncio
async def test_missing_cursor_cli_error_names_the_installer(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("sleeptalk.streaming.shutil.which", lambda name, path=None: None)
    monkeypatch.setenv("HOME", str(tmp_path))
    client = CursorAcpClient(cwd=str(tmp_path))
    with pytest.raises(AcpError) as info:
        await client.warmup()
    message = str(info.value)
    assert "curl https://cursor.com/install" in message
    assert "CURSOR_API_KEY" in message
    assert message != "agent binary not found."


@pytest.mark.asyncio
async def test_cursor_cli_found_in_local_bin(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("sleeptalk.streaming.shutil.which", lambda name, path=None: None)
    monkeypatch.setenv("HOME", str(tmp_path))
    fake = tmp_path / ".local" / "bin" / "agent"
    fake.parent.mkdir(parents=True)
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    client = CursorAcpClient(cwd=str(tmp_path))
    assert client._resolve_binary("") == str(fake)


@pytest.mark.asyncio
async def test_logged_out_cursor_cli_says_agent_login(monkeypatch, tmp_path) -> None:
    fake = tmp_path / "agent"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.setattr("sleeptalk.streaming.shutil.which", lambda name, path=None: str(fake))

    async def status(binary: str, cwd: str, *, env=None) -> str:
        assert binary == str(fake)
        return "Not logged in\n"

    monkeypatch.setattr("sleeptalk.agents.cursor_acp._run_status", status)
    client = CursorAcpClient(cwd=str(tmp_path))
    with pytest.raises(AcpError) as info:
        await client.warmup()
    assert "agent login" in str(info.value)
    assert "CURSOR_API_KEY" in str(info.value)


@pytest.mark.asyncio
async def test_logged_in_cursor_cli_passes_preflight(monkeypatch, tmp_path) -> None:
    fake = tmp_path / "agent"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)
    monkeypatch.setattr("sleeptalk.streaming.shutil.which", lambda name, path=None: str(fake))

    async def status(binary: str, cwd: str, *, env=None) -> str:
        return "Logged in as someone@example.com\n"

    monkeypatch.setattr("sleeptalk.agents.cursor_acp._run_status", status)
    client = CursorAcpClient(cwd=str(tmp_path))
    with pytest.raises(AcpError) as info:
        await client.warmup()
    # Preflight let it through; the empty fake then died on initialize.
    assert "agent login" not in str(info.value)
    await client.close()


def test_cursor_profile_launches_agent_acp() -> None:
    assert CURSOR_PROFILE.binary == "agent"
    assert CURSOR_PROFILE.extra_args == ("acp",)
    assert "cursor" in CURSOR_PROFILE.extension_namespaces


def test_nested_outcomes_match_cursor_docs() -> None:
    answered = ask_question_answered(
        [{"questionId": "q1", "selectedOptionIds": ["agent"]}]
    )
    assert answered == {
        "outcome": {
            "outcome": "answered",
            "answers": [{"questionId": "q1", "selectedOptionIds": ["agent"]}],
        }
    }
    assert create_plan_accepted() == {"outcome": {"outcome": "accepted"}}
    assert cancelled_outcome() == {"outcome": {"outcome": "cancelled"}}
    assert nested_outcome("rejected", reason="no")["outcome"]["outcome"] == "rejected"


def test_first_question_answers_pick_the_first_option() -> None:
    answers = first_question_answers(
        {
            "questions": [
                {
                    "id": "q1",
                    "options": [{"id": "agent"}, {"id": "plan"}],
                }
            ]
        }
    )
    assert answers == [{"questionId": "q1", "selectedOptionIds": ["agent"]}]


def _client(
    tmp_path: Path,
    *,
    mode: str = "plain",
    permission_mode: str = "auto",
    handle_extensions: bool = True,
) -> CursorAcpClient:
    return CursorAcpClient(
        cwd=str(tmp_path),
        permission_mode=permission_mode,
        handle_extensions=handle_extensions,
        env={
            "CURSOR_ACP_MODE": mode,
            "ACP_SENTENCE": "Done.",
            "PATH": str(Path(sys.executable).parent) + ":" + __import__("os").environ.get("PATH", ""),
        },
    )


def _patch_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    launched = type(CURSOR_PROFILE)(
        name="cursor",
        binary=sys.executable,
        extra_args=(str(FAKE),),
        extension_namespaces=CURSOR_PROFILE.extension_namespaces,
    )
    plain = type(CURSOR_PROFILE)(
        name="cursor",
        binary=sys.executable,
        extra_args=(str(FAKE),),
        extension_namespaces=frozenset(),
    )
    monkeypatch.setattr("sleeptalk.agents.cursor_acp.CURSOR_PROFILE", launched)
    monkeypatch.setattr("sleeptalk.agents.cursor_acp.CURSOR_PROFILE_NO_EXTENSIONS", plain)


@pytest.mark.asyncio
async def test_base_acp_streams_when_extensions_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_binary(monkeypatch)
    client = _client(tmp_path, handle_extensions=False)
    try:
        text = await asyncio.wait_for(client.respond("hi"), timeout=5)
        caps = adapter_capabilities(client)
        assert caps.clarification.level is SupportLevel.UNAVAILABLE
    finally:
        await client.close()
    assert "Done" in text


@pytest.mark.asyncio
async def test_ask_question_preserves_correlation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_binary(monkeypatch)
    seen: list[object] = []
    client = _client(tmp_path, mode="question", permission_mode="pending")
    try:
        task = asyncio.create_task(
            _collect(client, typed_events(seen.append))
        )
        event = await _wait_event(seen, ClarificationRequested)
        assert event.request_id == "q1"
        assert "mode" in event.prompt.lower()
        assert "Agent" in event.choices
        accepted = await adapter_control(
            client, AnswerClarification(request_id="q1", answer="agent")
        )
        assert isinstance(accepted, ControlAccepted)
        text = await task
    finally:
        await client.close()
    assert "Done" in text


@pytest.mark.asyncio
async def test_create_plan_accepts_with_nested_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_binary(monkeypatch)
    seen: list[object] = []
    client = _client(tmp_path, mode="plan", permission_mode="pending")
    try:
        task = asyncio.create_task(_collect(client, typed_events(seen.append)))
        event = await _wait_event(seen, PermissionRequested)
        assert event.tool_call_id == "call_p"
        await adapter_control(
            client,
            ResolvePermission(
                request_id=event.request_id, decision=PermissionDecision.ALLOW_ONCE
            ),
        )
        text = await task
    finally:
        await client.close()
    assert "Done" in text


@pytest.mark.asyncio
async def test_unknown_cursor_extension_does_not_break_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_binary(monkeypatch)
    client = _client(tmp_path, mode="unknown")
    try:
        text = await asyncio.wait_for(client.respond("hi"), timeout=5)
    finally:
        await client.close()
    assert "Done" in text
    assert any(item["code"] == "unknown_extension" for item in client.diagnostics)


@pytest.mark.asyncio
async def test_respond_streaming_rewrites_spoken_slash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    skill = tmp_path / ".cursor" / "skills" / "unslop" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: unslop\n---\nTighten prose.\n", encoding="utf-8")
    captured: list[str] = []

    async def _fake_stream(self, text: str, timeout: float = 300.0, *, on_event=None):
        captured.append(text)
        if False:
            yield ""

    monkeypatch.setattr(
        "sleeptalk.agents.acp.AcpClient.respond_streaming", _fake_stream
    )
    client = CursorAcpClient(cwd=str(tmp_path))
    async for _ in client.respond_streaming("SLASH unslop please"):
        pass
    assert captured == ["/unslop please"]


async def _collect(client: CursorAcpClient, on_event) -> str:
    parts: list[str] = []
    async for sentence in client.respond_streaming("hi", on_event=on_event):
        parts.append(sentence)
    return " ".join(parts)


async def _wait_event(seen: list[object], cls: type, timeout: float = 3.0):
    async def _poll():
        while True:
            for item in seen:
                if isinstance(item, cls):
                    return item
            await asyncio.sleep(0.02)

    return await asyncio.wait_for(_poll(), timeout=timeout)


@pytest.mark.asyncio
async def test_a_request_to_an_agent_that_already_exited_is_an_acp_error(monkeypatch, tmp_path) -> None:
    client = CursorAcpClient(cwd=str(tmp_path))

    async def lost(payload):
        raise ConnectionResetError("Connection lost")

    monkeypatch.setattr(client, "_send_json", lost)
    with pytest.raises(AcpError, match="ended"):
        await client._request("initialize", {})
    assert client._pending == {}
