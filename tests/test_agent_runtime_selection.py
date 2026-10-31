from __future__ import annotations

from dataclasses import dataclass

import pytest

from sleeptalk.agents.selection import (
    IncompatiblePolicy,
    ProbeStatus,
    RuntimeKind,
    SelectionError,
    SelectionRequest,
    doctor_lines,
    parse_pin,
    parse_policy,
    request_from_config,
    select_runtime,
)
from sleeptalk.config import AgentConfig
from sleeptalk.defaults import register_defaults
from sleeptalk.doctor import OK, run_doctor
from sleeptalk.registries import AGENTS


@dataclass(frozen=True)
class Case:
    id: str
    request: SelectionRequest
    kind: RuntimeKind | None = None
    registration: str = ""
    fallback_from: RuntimeKind | None = None
    session_continuity: bool | None = None
    error: str | None = None


CASES = [
    Case(
        id="hermes_gateway_reachable",
        request=SelectionRequest(
            agent_type="hermes_gateway",
            pin=None,
            policy=IncompatiblePolicy.FALLBACK,
            gateway_configured=True,
            gateway_probe=ProbeStatus.REACHABLE,
            native_available=False,
            acp_available=True,
        ),
        kind=RuntimeKind.GATEWAY,
        registration="hermes_gateway",
    ),
    Case(
        id="hermes_gateway_unavailable_falls_back",
        request=SelectionRequest(
            agent_type="hermes_gateway",
            pin=None,
            policy=IncompatiblePolicy.FALLBACK,
            gateway_configured=True,
            gateway_probe=ProbeStatus.UNAVAILABLE,
            native_available=False,
            acp_available=True,
        ),
        kind=RuntimeKind.ACP,
        registration="acp",
        fallback_from=RuntimeKind.GATEWAY,
    ),
    Case(
        id="hermes_gateway_unauthenticated_falls_back",
        request=SelectionRequest(
            agent_type="hermes_gateway",
            pin=None,
            policy=IncompatiblePolicy.FALLBACK,
            gateway_configured=True,
            gateway_probe=ProbeStatus.UNAUTHENTICATED,
            native_available=False,
            acp_available=True,
        ),
        kind=RuntimeKind.ACP,
        registration="acp",
        fallback_from=RuntimeKind.GATEWAY,
    ),
    Case(
        id="hermes_gateway_incompatible_falls_back",
        request=SelectionRequest(
            agent_type="hermes_gateway",
            pin=None,
            policy=IncompatiblePolicy.FALLBACK,
            gateway_configured=True,
            gateway_probe=ProbeStatus.INCOMPATIBLE,
            native_available=False,
            acp_available=True,
        ),
        kind=RuntimeKind.ACP,
        registration="acp",
        fallback_from=RuntimeKind.GATEWAY,
    ),
    Case(
        id="hermes_gateway_incompatible_fails",
        request=SelectionRequest(
            agent_type="hermes_gateway",
            pin=None,
            policy=IncompatiblePolicy.FAIL,
            gateway_configured=True,
            gateway_probe=ProbeStatus.INCOMPATIBLE,
            native_available=False,
            acp_available=True,
        ),
        error="incompatible",
    ),
    Case(
        id="hermes_acp_pin_keeps_working",
        request=SelectionRequest(
            agent_type="acp",
            pin=RuntimeKind.ACP,
            policy=IncompatiblePolicy.FALLBACK,
            gateway_configured=False,
            gateway_probe=None,
            native_available=False,
            acp_available=True,
        ),
        kind=RuntimeKind.ACP,
        registration="acp",
    ),
    Case(
        id="openclaw_unreachable_falls_back",
        request=SelectionRequest(
            agent_type="openclaw",
            pin=None,
            policy=IncompatiblePolicy.FALLBACK,
            gateway_configured=True,
            gateway_probe=ProbeStatus.UNAVAILABLE,
            native_available=False,
            acp_available=True,
        ),
        kind=RuntimeKind.ACP,
        registration="openclaw_acp",
        fallback_from=RuntimeKind.GATEWAY,
    ),
    Case(
        id="claude_code_stays_native",
        request=SelectionRequest(
            agent_type="claude_code",
            pin=None,
            policy=IncompatiblePolicy.FALLBACK,
            gateway_configured=False,
            gateway_probe=None,
            native_available=True,
            acp_available=True,
        ),
        kind=RuntimeKind.NATIVE,
        registration="claude_code",
    ),
    Case(
        id="codex_stays_native",
        request=SelectionRequest(
            agent_type="codex",
            pin=None,
            policy=IncompatiblePolicy.FALLBACK,
            gateway_configured=False,
            gateway_probe=None,
            native_available=True,
            acp_available=False,
        ),
        kind=RuntimeKind.NATIVE,
        registration="codex",
    ),
    Case(
        id="pi_stays_native",
        request=SelectionRequest(
            agent_type="pi",
            pin=None,
            policy=IncompatiblePolicy.FALLBACK,
            gateway_configured=False,
            gateway_probe=None,
            native_available=True,
            acp_available=False,
        ),
        kind=RuntimeKind.NATIVE,
        registration="pi",
    ),
    Case(
        id="cursor_sdk_when_key_present",
        request=SelectionRequest(
            agent_type="cursor",
            pin=None,
            policy=IncompatiblePolicy.FALLBACK,
            gateway_configured=False,
            gateway_probe=None,
            native_available=True,
            acp_available=True,
        ),
        kind=RuntimeKind.NATIVE,
        registration="cursor",
    ),
    Case(
        id="cursor_acp_when_sdk_unavailable",
        request=SelectionRequest(
            agent_type="cursor",
            pin=None,
            policy=IncompatiblePolicy.FALLBACK,
            gateway_configured=False,
            gateway_probe=None,
            native_available=False,
            acp_available=True,
            session_id="agent-1",
        ),
        kind=RuntimeKind.ACP,
        registration="cursor_acp",
        fallback_from=RuntimeKind.NATIVE,
        session_continuity=True,
    ),
    Case(
        id="cursor_runtime_pin_acp",
        request=SelectionRequest(
            agent_type="cursor",
            pin=RuntimeKind.ACP,
            policy=IncompatiblePolicy.FALLBACK,
            gateway_configured=False,
            gateway_probe=None,
            native_available=True,
            acp_available=True,
        ),
        kind=RuntimeKind.ACP,
        registration="cursor_acp",
    ),
    Case(
        id="session_break_is_reported_not_silent",
        request=SelectionRequest(
            agent_type="hermes_gateway",
            pin=None,
            policy=IncompatiblePolicy.FALLBACK,
            gateway_configured=True,
            gateway_probe=ProbeStatus.UNAVAILABLE,
            native_available=False,
            acp_available=True,
            session_id="live-session",
        ),
        kind=RuntimeKind.ACP,
        registration="acp",
        fallback_from=RuntimeKind.GATEWAY,
        session_continuity=False,
    ),
    Case(
        id="session_break_fails_when_policy_says_fail",
        request=SelectionRequest(
            agent_type="hermes_gateway",
            pin=None,
            policy=IncompatiblePolicy.FAIL,
            gateway_configured=True,
            gateway_probe=ProbeStatus.UNAVAILABLE,
            native_available=False,
            acp_available=True,
            session_id="live-session",
        ),
        error="unreachable",
    ),
]


@pytest.mark.parametrize("case", CASES, ids=[case.id for case in CASES])
def test_select_runtime_table(case: Case) -> None:
    if case.error is not None:
        with pytest.raises(SelectionError, match=case.error):
            select_runtime(case.request)
        return
    selected = select_runtime(case.request)
    assert selected.kind is case.kind
    assert selected.registration == case.registration
    assert selected.fallback_from is case.fallback_from
    if case.session_continuity is not None:
        assert selected.session_continuity is case.session_continuity
    if case.session_continuity is False:
        assert any("cannot continue" in line for line in selected.diagnostics)


def test_parse_pin_and_policy() -> None:
    assert parse_pin("") is None
    assert parse_pin("sdk") is RuntimeKind.NATIVE
    assert parse_policy("") is IncompatiblePolicy.FALLBACK
    assert parse_policy("error") is IncompatiblePolicy.FAIL
    with pytest.raises(SelectionError, match="unknown runtime pin"):
        parse_pin("tui")
    with pytest.raises(SelectionError, match="unknown on_incompatible"):
        parse_policy("maybe")


def test_request_from_config_pins_existing_acp_type() -> None:
    request = request_from_config(AgentConfig(type="acp"), {})
    selected = select_runtime(request)
    assert selected.registration == "acp"
    assert selected.kind is RuntimeKind.ACP
    assert "pins" in selected.reason


def test_request_from_config_cursor_uses_api_key() -> None:
    without = request_from_config(AgentConfig(type="cursor"), {})
    assert without.native_available is False
    with_key = request_from_config(
        AgentConfig(type="cursor"), {"CURSOR_API_KEY": "crsr_key"}
    )
    assert with_key.native_available is True


def test_claude_cannot_select_unimplemented_acp_runtime() -> None:
    request = request_from_config(AgentConfig(type="claude_code", runtime="acp"), {})
    with pytest.raises(SelectionError, match="has no acp runtime"):
        select_runtime(request)


def test_doctor_lines_name_the_selected_runtime() -> None:
    selected = select_runtime(
        SelectionRequest(
            agent_type="pi",
            pin=None,
            policy=IncompatiblePolicy.FALLBACK,
            gateway_configured=False,
            gateway_probe=None,
            native_available=True,
            acp_available=False,
        )
    )
    lines = doctor_lines(selected)
    assert lines[0].startswith("selected pi (native)")


@pytest.mark.asyncio
async def test_doctor_reports_selected_runtime(tmp_path) -> None:
    register_defaults()
    path = tmp_path / "sleeptalk.toml"
    path.write_text(
        "\n".join(
            [
                "[transport]",
                'type = "null"',
                "[agent]",
                'type = "pi"',
            ]
        ),
        encoding="utf-8",
    )
    report = await run_doctor(path, env={}, live=False)
    rung = next(item for item in report.rungs if item.name == "agent runtime")
    assert rung.status == OK
    assert any("selected pi (native)" in line for line in rung.lines)


def test_existing_agent_types_still_register() -> None:
    register_defaults()
    for name in (
        "pi",
        "omp",
        "acp",
        "openclaw",
        "claude_code",
        "hermes_gateway",
        "cursor",
        "codex",
    ):
        assert name in AGENTS


@pytest.mark.parametrize("mode", ["deny", "pending", "auto", "untrusted"])
def test_cursor_native_preserves_only_explicit_deny_policy(mode):
    request = request_from_config(
        AgentConfig(type="cursor", runtime="native", on_incompatible="fail", permission_mode=mode),
        {"CURSOR_API_KEY": "synthetic-key"},
    )
    if mode == "deny":
        selection = select_runtime(request)
        assert selection.registration == "cursor"
        assert selection.permission_continuity is True
        assert not selection.diagnostics
    else:
        with pytest.raises(SelectionError, match="permission_mode"):
            select_runtime(request)
