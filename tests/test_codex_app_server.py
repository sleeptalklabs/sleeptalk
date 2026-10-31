from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from sleeptalk.adapter import (
    Cancel,
    ControlAccepted,
    PermissionDecision,
    ResolvePermission,
    SessionIdentified,
    Steer,
    SupportLevel,
    ToolEvent,
    adapter_capabilities,
    adapter_control,
    typed_events,
)
from sleeptalk.agents.codex_app_server import (
    EXPERIMENTAL_METHODS,
    STABLE_METHODS,
    SUPPORTED_CODEX_VERSION,
    CodexAppServerClient,
    CodexAppServerError,
    CodexSchemaError,
    check_supported_schema,
    parse_codex_version,
    reject_experimental_params,
)
from tests.agent_conformance.cases import (
    case_adapter_does_not_own_voice_policy,
    case_permissions,
    case_steering,
    case_stream_yields_sentence,
    case_tool_lifecycle,
    case_usage,
)
from tests.agent_conformance.harness import drain

FAKE = Path(__file__).with_name("fixtures") / "codex_app_server_fake.py"
SCHEMA_DIR = (
    Path(__file__).resolve().parents[1] / "sleeptalk" / "agents" / "codex_app_server_schema"
)
SCHEMA_BUNDLE = SCHEMA_DIR / "codex_app_server_protocol.v2.schemas.json"
SCHEMA_SHA256 = SCHEMA_DIR / "SHA256"
_CREDENTIAL_MARKERS = (
    "not logged in",
    "login required",
    "unauthorized",
    "unauthenticated",
    "authentication",
    "credentials",
    "auth required",
    "no chatgpt",
)


def _codex_binary() -> str | None:
    return os.environ.get("CODEX_BIN") or shutil.which("codex")


def _is_missing_credentials(exc: BaseException) -> bool:
    if not isinstance(exc, CodexAppServerError) or isinstance(exc, CodexSchemaError):
        return False
    text = str(exc).lower()
    return any(marker in text for marker in _CREDENTIAL_MARKERS)


def _recorded_schema_digest() -> str:
    for line in SCHEMA_SHA256.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return stripped.split()[0]
    raise AssertionError("schema SHA256 pin is empty")


class FixtureCodex(CodexAppServerClient):
    def _launch_args(self) -> list[str]:
        return [sys.executable, str(FAKE)]


@pytest.fixture
async def client() -> FixtureCodex:
    agent = FixtureCodex(cwd=".", permission_mode="pending")
    try:
        yield agent
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_handshake_and_hello_stream(client: FixtureCodex) -> None:
    events, sentences = await drain(client, "hello")
    assert any("First sentence" in sentence for sentence in sentences)
    assert any(isinstance(event, SessionIdentified) for event in events)
    identified = next(
        event for event in events if isinstance(event, SessionIdentified)
    )
    assert identified.upstream_session_id == "thr_fixture"
    assert identified.resumable is True
    await case_usage(client)
    await case_stream_yields_sentence(client)


@pytest.mark.asyncio
async def test_resume_reuses_thread_id() -> None:
    agent = FixtureCodex(cwd=".", resume_thread_id="thr_old")
    try:
        events, _sentences = await drain(agent, "hello")
        identified = next(
            event for event in events if isinstance(event, SessionIdentified)
        )
        assert identified.upstream_session_id == "thr_old"
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_tool_lifecycle_and_steer(client: FixtureCodex) -> None:
    await case_tool_lifecycle(client)
    await case_steering(client)
    cancelled = await adapter_control(client, Cancel(target_id="turn_1"))
    assert isinstance(cancelled, ControlAccepted)


@pytest.mark.asyncio
async def test_permission_maps_to_shared_capability(client: FixtureCodex) -> None:
    await case_permissions(client)


@pytest.mark.asyncio
async def test_interrupt_settles_hang() -> None:
    agent = FixtureCodex(cwd=".")
    events: list[object] = []
    sink = typed_events(events.append)

    async def _consume() -> None:
        async for _sentence in agent.respond_streaming("hang", on_event=sink):
            pass

    try:
        task = asyncio.create_task(_consume())
        await asyncio.sleep(0.2)
        await agent.abort()
        await asyncio.wait_for(task, timeout=3.0)
        assert task.done()
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_a_cancelled_turns_late_completion_does_not_end_the_next() -> None:
    # Codex can send the interrupted turn's turn/completed after the next
    # turn started; taken as the new turn's end, the answer came back empty.
    agent = FixtureCodex(cwd=".")

    async def _consume() -> None:
        async for _sentence in agent.respond_streaming("hang late"):
            pass

    try:
        task = asyncio.create_task(_consume())
        await asyncio.sleep(0.2)
        await agent.abort()
        await asyncio.wait_for(task, timeout=3.0)
        assert "First sentence" in await agent.respond("hello", timeout=5)
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_turn_error_is_structured() -> None:
    agent = FixtureCodex(cwd=".")
    try:
        with pytest.raises(CodexAppServerError, match="fixture refused"):
            await agent.respond("error")
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_schema_mismatch_fails_handshake(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEX_FAKE_USER_AGENT", "codex_cli_rs/0.1.0-other")
    agent = FixtureCodex(cwd=".")
    try:
        with pytest.raises(CodexSchemaError, match="does not match supported schema"):
            await agent.warmup()
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_experimental_method_rejected_without_opt_in(
    client: FixtureCodex,
) -> None:
    with pytest.raises(CodexAppServerError, match="experimentalApi"):
        await client._rpc("thread/turns/list", {})


def test_experimental_fields_rejected_unless_negotiated() -> None:
    with pytest.raises(CodexAppServerError, match="dynamicTools"):
        reject_experimental_params({"dynamicTools": []}, experimental=False)
    reject_experimental_params({"dynamicTools": []}, experimental=True)


def test_schema_pin_and_catalog() -> None:
    assert SUPPORTED_CODEX_VERSION == "0.162.1"
    assert parse_codex_version(
        "sleeptalk/0.162.1 (Mac OS 15.6.1; arm64) dumb (sleeptalk; 0.1.0)"
    ) == SUPPORTED_CODEX_VERSION
    check_supported_schema("sleeptalk/0.153.4 (test; fixture)")
    with pytest.raises(CodexSchemaError):
        check_supported_schema("sleeptalk/9.9.9 (other)")
    assert (SCHEMA_DIR / "VERSION").read_text(encoding="utf-8").strip() == "0.162.1"
    catalog = json.loads(SCHEMA_BUNDLE.read_text(encoding="utf-8"))
    definitions = catalog["definitions"]
    for name in (
        "InitializeParams",
        "ThreadStartParams",
        "ThreadResumeParams",
        "TurnStartParams",
        "TurnSteerParams",
        "TurnInterruptParams",
        "ClientRequest",
    ):
        assert name in definitions
    methods: set[str] = set()
    for variant in definitions["ClientRequest"]["oneOf"]:
        enum = variant.get("properties", {}).get("method", {}).get("enum", [])
        methods.update(enum)
    for method in STABLE_METHODS - {"initialized"}:
        assert method in methods
    assert hashlib.sha256(SCHEMA_BUNDLE.read_bytes()).hexdigest() == _recorded_schema_digest()
    assert "thread/start" in STABLE_METHODS
    assert "turn/steer" in STABLE_METHODS
    assert "turn/interrupt" in STABLE_METHODS
    assert "thread/turns/list" in EXPERIMENTAL_METHODS
    source = Path(
        __import__("sleeptalk.agents.codex_app_server", fromlist=["x"]).__file__
    ).read_text(encoding="utf-8")
    assert "ws://" not in source
    assert "codex exec" not in source
    assert "--listen" not in source


def test_missing_credentials_skip_is_narrow() -> None:
    assert _is_missing_credentials(CodexAppServerError("Not logged in"))
    assert not _is_missing_credentials(
        CodexAppServerError("Codex thread handshake returned no thread id.")
    )
    assert not _is_missing_credentials(CodexAppServerError("Method not found: initialize"))
    assert not _is_missing_credentials(CodexAppServerError("Codex initialize timed out."))
    assert not _is_missing_credentials(CodexSchemaError("schema mismatch"))


def test_manifest_is_honest() -> None:
    manifest = adapter_capabilities(CodexAppServerClient())
    assert manifest.permissions.level is SupportLevel.SUPPORTED
    assert manifest.steering.level is SupportLevel.SUPPORTED
    assert manifest.clarification.level is SupportLevel.UNSUPPORTED


def test_adapter_does_not_own_voice_policy() -> None:
    case_adapter_does_not_own_voice_policy(
        CodexAppServerClient(), sentences=["First sentence."], events=[]
    )


@pytest.mark.asyncio
async def test_local_process_smoke() -> None:
    agent = FixtureCodex(cwd=".")
    try:
        await agent.warmup()
        assert agent.upstream_session_id == "thr_fixture"
        assert parse_codex_version(agent._user_agent) == SUPPORTED_CODEX_VERSION
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_real_codex_binary_handshake_optional() -> None:
    binary = _codex_binary()
    if not binary:
        pytest.skip("codex not on PATH")
    agent = CodexAppServerClient(cwd=os.getcwd(), codex_bin=binary)
    try:
        await agent._spawn(agent._launch_args())
        try:
            await agent._handshake()
        except CodexSchemaError as exc:
            pytest.skip(f"installed Codex schema is not the pinned version: {exc}")
        assert check_supported_schema(agent._user_agent)
        try:
            await agent._ensure_thread()
        except CodexAppServerError as exc:
            if _is_missing_credentials(exc):
                pytest.skip(
                    f"codex app-server thread start needs local credentials: {exc}"
                )
            raise
    finally:
        await agent.close()


def test_committed_schema_matches_installed_codex_optional(tmp_path: Path) -> None:
    binary = _codex_binary()
    if not binary:
        pytest.skip("codex not on PATH")
    version = subprocess.run(
        [binary, "--version"],
        check=False,
        capture_output=True,
        text=True,
    )
    banner = f"{version.stdout} {version.stderr}"
    if SUPPORTED_CODEX_VERSION not in banner:
        pytest.skip(f"installed Codex is not {SUPPORTED_CODEX_VERSION}: {banner.strip()}")
    out = tmp_path / "generated"
    out.mkdir()
    generated = subprocess.run(
        [binary, "app-server", "generate-json-schema", "--out", str(out)],
        check=False,
        capture_output=True,
        text=True,
    )
    if generated.returncode != 0:
        pytest.fail(
            f"generate-json-schema failed: {generated.stderr or generated.stdout}"
        )
    matches = list(out.rglob("codex_app_server_protocol.v2.schemas.json"))
    assert matches, "generate-json-schema did not write the v2 bundle"
    assert matches[0].read_bytes() == SCHEMA_BUNDLE.read_bytes()


@pytest.mark.asyncio
async def test_steer_then_tool_events_are_typed(client: FixtureCodex) -> None:
    events, _sentences = await drain(client, "tool")
    tools = [event for event in events if isinstance(event, ToolEvent)]
    assert tools
    assert any(event.phase.name == "STARTED" for event in tools)
    steered = await adapter_control(client, Steer(instruction="be brief"))
    assert isinstance(steered, ControlAccepted)


@pytest.mark.asyncio
async def test_permission_decision_roundtrip(client: FixtureCodex) -> None:
    events: list[object] = []
    sink = typed_events(events.append)

    async def _consume() -> list[str]:
        return [sentence async for sentence in client.respond_streaming("permission", on_event=sink)]

    task = asyncio.create_task(_consume())
    from sleeptalk.adapter import PermissionRequested

    for _ in range(50):
        request = next(
            (event for event in events if isinstance(event, PermissionRequested)),
            None,
        )
        if request is not None:
            break
        await asyncio.sleep(0.05)
    else:
        task.cancel()
        pytest.fail("permission request never arrived")
    result = await adapter_control(
        client,
        ResolvePermission(
            request_id=request.request_id, decision=PermissionDecision.ALLOW_ONCE
        ),
    )
    assert isinstance(result, ControlAccepted)
    sentences = await asyncio.wait_for(task, timeout=5.0)
    assert any("Allowed" in sentence or "Proceeding" in sentence for sentence in sentences)


@pytest.mark.parametrize("decision,scope", [("decline", "turn"), ("accept", "turn"), ("acceptForSession", "session")])
def test_permission_profile_approval_uses_grants_not_command_decisions(decision, scope):
    from sleeptalk.agents.codex_app_server import _approval_response
    permissions = {"network": {"enabled": True}}
    result = _approval_response({"method": "item/permissions/requestApproval", "params": {"permissions": permissions}}, decision)
    assert result == {"permissions": {} if decision == "decline" else permissions, "scope": scope}
    assert _approval_response({"method": "item/commandExecution/requestApproval"}, decision) == {"decision": decision}
