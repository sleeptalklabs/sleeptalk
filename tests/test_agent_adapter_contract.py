from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import MISSING, fields

import pytest

from sleeptalk.adapter import (
    AgentCapabilityManifest,
    AgentControl,
    CapabilitySupport,
    ControlAccepted,
    ControlRejected,
    ControlResult,
    PermissionDecision,
    PermissionRequested,
    ResolvePermission,
    ThinkingStarted,
    Steer,
    SupportLevel,
    ToolEvent,
    VoiceSessionCapability,
    WorkerCapability,
    adapter_capabilities,
    adapter_control,
    event_from_legacy,
    event_to_legacy,
    publish_event,
    typed_events,
)
from sleeptalk.session_events import SessionEventType, adapter_session_event


class LegacyFake:
    def respond_streaming(
        self,
        text: str,
        *,
        on_event=None,
    ) -> AsyncIterator[str]:
        async def _stream() -> AsyncIterator[str]:
            if on_event is not None:
                on_event("thinking")
                on_event("text")
            yield "Hello."

        return _stream()

    async def respond(self, text: str, timeout: float = 300.0) -> str:
        return "Hello."

    async def abort(self) -> None:
        return None

    async def warmup(self) -> None:
        return None

    async def close(self) -> None:
        return None


class RichFake:
    def __init__(self) -> None:
        self.controls: list[AgentControl] = []
        self.agent_capabilities = AgentCapabilityManifest(
            contract_version=1,
            structured_events=CapabilitySupport(SupportLevel.SUPPORTED),
            tool_lifecycle=CapabilitySupport(SupportLevel.SUPPORTED),
            permissions=CapabilitySupport(SupportLevel.SUPPORTED),
            clarification=CapabilitySupport(SupportLevel.UNSUPPORTED),
            session_identity=CapabilitySupport(SupportLevel.UNSUPPORTED),
            status=CapabilitySupport(SupportLevel.UNSUPPORTED),
            usage=CapabilitySupport(SupportLevel.UNSUPPORTED),
            steering=CapabilitySupport(SupportLevel.UNSUPPORTED),
            targeted_cancellation=CapabilitySupport(SupportLevel.UNSUPPORTED),
            workers=WorkerCapability(SupportLevel.UNSUPPORTED),
            voice_sessions=VoiceSessionCapability(
                SupportLevel.UNAVAILABLE,
                reason="no Discord guild configured",
            ),
        )

    def respond_streaming(
        self,
        text: str,
        *,
        on_event=None,
    ) -> AsyncIterator[str]:
        async def _stream() -> AsyncIterator[str]:
            publish_event(on_event, ThinkingStarted())
            publish_event(on_event, ToolEvent(name="bash"))
            publish_event(
                on_event,
                PermissionRequested(
                    request_id="perm-1",
                    prompt="Allow bash?",
                    options=frozenset(
                        {PermissionDecision.DENY, PermissionDecision.ALLOW_ONCE}
                    ),
                ),
            )
            yield "Sure."

        return _stream()

    async def respond(self, text: str, timeout: float = 300.0) -> str:
        return "Sure."

    async def abort(self) -> None:
        return None

    async def warmup(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def control(self, command: AgentControl) -> ControlResult:
        self.controls.append(command)
        if isinstance(command, ResolvePermission):
            return ControlAccepted(request_id=command.request_id)
        return ControlRejected(reason="unsupported command", retryable=False)


def test_legacy_capabilities_are_the_floor() -> None:
    legacy = LegacyFake()
    caps = adapter_capabilities(legacy)
    assert caps == AgentCapabilityManifest.legacy_floor()
    assert caps.permissions.level is SupportLevel.UNSUPPORTED
    assert caps.voice_sessions.level is SupportLevel.UNSUPPORTED


@pytest.mark.asyncio
async def test_legacy_stream_and_idle_abort() -> None:
    legacy = LegacyFake()
    seen: list[str] = []
    sentences = [
        sentence
        async for sentence in legacy.respond_streaming("hi", on_event=seen.append)
    ]
    assert sentences == ["Hello."]
    assert seen == ["thinking", "text"]
    await legacy.abort()


def test_legacy_event_round_trip() -> None:
    for value in ("thinking", "text", "tool:bash"):
        assert event_to_legacy(event_from_legacy(value)) == value


def test_completed_tool_event_is_not_a_spoken_legacy_string() -> None:
    from sleeptalk.adapter import ToolEvent, ToolPhase

    assert (
        event_to_legacy(ToolEvent(name="bash", phase=ToolPhase.COMPLETED)) is None
    )
    assert event_to_legacy(ToolEvent(name="bash", phase=ToolPhase.FAILED)) is None
    assert event_to_legacy(ToolEvent(name="bash", phase=ToolPhase.STARTED)) == "tool:bash"


def test_publish_event_does_not_encode_permission_as_thinking() -> None:
    seen: list[str] = []
    publish_event(
        seen.append,
        PermissionRequested(
            request_id="perm-1",
            prompt="Allow bash?",
            options=frozenset({PermissionDecision.DENY}),
        ),
    )
    assert seen == []
    assert "thinking" not in seen


def test_permission_requested_has_no_approval_fields() -> None:
    event = PermissionRequested(
        request_id="perm-1",
        prompt="Allow bash?",
        options=frozenset({PermissionDecision.DENY}),
    )
    assert not hasattr(event, "allowed")
    assert not hasattr(event, "approved")


def test_resolve_permission_requires_decision() -> None:
    decision = next(item for item in fields(ResolvePermission) if item.name == "decision")
    assert decision.default is MISSING
    assert decision.default_factory is MISSING
    with pytest.raises(TypeError):
        ResolvePermission(request_id="x")


def test_adapter_session_event_permission_request() -> None:
    event = adapter_session_event(
        "session-1",
        1,
        PermissionRequested(
            request_id="perm-1",
            prompt="Allow bash?",
            options=frozenset({PermissionDecision.DENY, PermissionDecision.ALLOW_ONCE}),
        ),
    )
    assert event.type is SessionEventType.AGENT_EVENT
    assert event.payload["schema"] == "adapter.v1"
    assert event.payload["kind"] == "permission.requested"
    assert event.payload["kind"] != "thinking"
    assert "allowed" not in event.payload
    assert "approved" not in event.payload
    assert event.payload["request_id"] == "perm-1"
    assert event.payload["prompt"] == "Allow bash?"
    assert "options" in event.payload


def test_typed_events_parses_tool_string() -> None:
    seen: list[object] = []
    sink = typed_events(seen.append)
    sink("tool:bash")
    assert len(seen) == 1
    assert isinstance(seen[0], ToolEvent)
    assert seen[0].name == "bash"


@pytest.mark.asyncio
async def test_adapter_control_rejects_legacy_steer() -> None:
    result = await adapter_control(LegacyFake(), Steer(instruction="stop"))
    assert isinstance(result, ControlRejected)
    assert result.retryable is False


def test_capability_support_reason_rules() -> None:
    with pytest.raises(ValueError):
        CapabilitySupport(SupportLevel.UNAVAILABLE)
    with pytest.raises(ValueError):
        CapabilitySupport(SupportLevel.SUPPORTED, reason="nope")


def test_rich_fake_voice_sessions_unavailable() -> None:
    rich = RichFake()
    caps = adapter_capabilities(rich)
    assert caps.voice_sessions.level is SupportLevel.UNAVAILABLE
    assert caps.voice_sessions.reason
    assert caps.permissions.level is SupportLevel.SUPPORTED
    assert caps.clarification.level is SupportLevel.UNSUPPORTED


@pytest.mark.asyncio
async def test_rich_fake_exercises_supported_capabilities() -> None:
    rich = RichFake()
    seen: list[object] = []
    sentences = [
        sentence
        async for sentence in rich.respond_streaming(
            "hi", on_event=typed_events(seen.append)
        )
    ]
    assert sentences == ["Sure."]
    kinds = [type(event) for event in seen]
    assert ThinkingStarted in kinds
    assert ToolEvent in kinds
    assert PermissionRequested in kinds
    accepted = await adapter_control(
        rich,
        ResolvePermission(
            request_id="perm-1",
            decision=PermissionDecision.DENY,
        ),
    )
    assert isinstance(accepted, ControlAccepted)
    assert accepted.request_id == "perm-1"


def test_existing_adapters_remain_importable() -> None:
    from sleeptalk.agents.claude_code_adapter import ClaudeCodeClient
    from sleeptalk.agents.hermes_adapter import HermesACPClient
    from sleeptalk.agents.hermes_gateway_adapter import HermesGatewayClient
    from sleeptalk.agents.pi_rpc import PiRPCClient

    assert ClaudeCodeClient is not None
    assert HermesACPClient is not None
    assert HermesGatewayClient is not None
    assert PiRPCClient is not None
    assert adapter_capabilities(object()) == AgentCapabilityManifest.legacy_floor()
