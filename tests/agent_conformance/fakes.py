from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator
from pathlib import Path

from sleeptalk.adapter import (
    AgentCapabilityManifest,
    AgentControl,
    AnswerClarification,
    AssistantDelta,
    CapabilitySupport,
    ClarificationRequested,
    ControlAccepted,
    ControlRejected,
    ControlResult,
    SessionIdentified,
    Steer,
    SupportLevel,
    ThinkingStarted,
    ToolEvent,
    ToolPhase,
    UsageReported,
    VoiceSessionCapability,
    WorkerCapability,
    publish_event,
)
from sleeptalk.agents.acp import AcpClient
from sleeptalk.agents.profiles import AcpProfile

from .harness import (
    PROMPT_CLARIFY,
    PROMPT_HANG,
    PROMPT_SLOW,
    PROMPT_TOOL,
)

_SUPPORTED = CapabilitySupport(SupportLevel.SUPPORTED)
_UNSUPPORTED = CapabilitySupport(SupportLevel.UNSUPPORTED)

FAKE_ACP_SERVER = Path(__file__).with_name("fake_acp_server.py")


def _prompt_key(text: str) -> str:
    return text.strip().lower()


class NativeFake:
    def __init__(self) -> None:
        self.upstream_session_id = ""
        self.warmed = False
        self.closed = False
        self._abort = asyncio.Event()
        self.agent_capabilities = AgentCapabilityManifest(
            contract_version=1,
            structured_events=_SUPPORTED,
            tool_lifecycle=_SUPPORTED,
            permissions=_UNSUPPORTED,
            clarification=_UNSUPPORTED,
            session_identity=_SUPPORTED,
            status=_UNSUPPORTED,
            usage=_UNSUPPORTED,
            steering=_UNSUPPORTED,
            targeted_cancellation=_UNSUPPORTED,
            workers=WorkerCapability(SupportLevel.UNSUPPORTED),
            voice_sessions=VoiceSessionCapability(
                SupportLevel.UNAVAILABLE,
                reason="native fake has no voice session transport",
            ),
        )

    def respond_streaming(
        self,
        text: str,
        *,
        on_event=None,
    ) -> AsyncIterator[str]:
        async def _stream() -> AsyncIterator[str]:
            if self.closed:
                raise RuntimeError("native fake is closed")
            self._abort.clear()
            key = _prompt_key(text)
            publish_event(
                on_event,
                SessionIdentified(
                    upstream_session_id=self.upstream_session_id,
                    resumable=False,
                ),
            )
            publish_event(on_event, ThinkingStarted())
            if key == PROMPT_HANG:
                await self._abort.wait()
                return
            if key == PROMPT_TOOL:
                publish_event(
                    on_event,
                    ToolEvent(name="bash", call_id="n1", phase=ToolPhase.STARTED),
                )
                publish_event(
                    on_event,
                    ToolEvent(name="bash", call_id="n1", phase=ToolPhase.COMPLETED),
                )
            publish_event(on_event, AssistantDelta(text="First sentence."))
            yield "First sentence."
            if key == PROMPT_SLOW:
                await self._abort.wait()
                return
            if self._abort.is_set():
                return
            yield "Second sentence."

        return _stream()

    async def respond(self, text: str, timeout: float = 300.0) -> str:
        del timeout
        parts: list[str] = []
        async for sentence in self.respond_streaming(text):
            parts.append(sentence)
        return " ".join(parts)

    async def abort(self) -> None:
        self._abort.set()

    async def warmup(self) -> None:
        self.warmed = True
        self.upstream_session_id = "native-fake-1"

    async def close(self) -> None:
        self.closed = True
        self._abort.set()


class GatewayFake:
    def __init__(self, session_id: str = "gw-1") -> None:
        self.session_id = session_id
        self.closed = False
        self._abort = asyncio.Event()
        self.agent_capabilities = AgentCapabilityManifest(
            contract_version=1,
            structured_events=_SUPPORTED,
            tool_lifecycle=_SUPPORTED,
            permissions=_UNSUPPORTED,
            clarification=_SUPPORTED,
            session_identity=_UNSUPPORTED,
            status=_UNSUPPORTED,
            usage=_SUPPORTED,
            steering=_SUPPORTED,
            targeted_cancellation=_UNSUPPORTED,
            workers=WorkerCapability(SupportLevel.UNSUPPORTED),
            voice_sessions=VoiceSessionCapability(SupportLevel.UNSUPPORTED),
        )

    def respond_streaming(
        self,
        text: str,
        *,
        on_event=None,
    ) -> AsyncIterator[str]:
        async def _stream() -> AsyncIterator[str]:
            if self.closed:
                raise RuntimeError("gateway fake is closed")
            self._abort.clear()
            key = _prompt_key(text)
            publish_event(on_event, ThinkingStarted())
            if key == PROMPT_HANG:
                await self._abort.wait()
                return
            if key == PROMPT_CLARIFY:
                publish_event(
                    on_event,
                    ClarificationRequested(
                        request_id="clarify-1",
                        prompt="Which file?",
                    ),
                )
            if key == PROMPT_TOOL:
                publish_event(
                    on_event,
                    ToolEvent(
                        name="terminal",
                        call_id="g1",
                        phase=ToolPhase.STARTED,
                    ),
                )
                publish_event(
                    on_event,
                    ToolEvent(
                        name="terminal",
                        call_id="g1",
                        phase=ToolPhase.COMPLETED,
                    ),
                )
            publish_event(
                on_event,
                UsageReported(input_tokens=3, output_tokens=8),
            )
            publish_event(on_event, AssistantDelta(text="First sentence."))
            yield "First sentence."
            if key == PROMPT_SLOW:
                await self._abort.wait()
                return
            if self._abort.is_set():
                return
            yield "Gateway done."

        return _stream()

    async def respond(self, text: str, timeout: float = 300.0) -> str:
        del timeout
        parts: list[str] = []
        async for sentence in self.respond_streaming(text):
            parts.append(sentence)
        return " ".join(parts)

    async def abort(self) -> None:
        self._abort.set()

    async def warmup(self) -> None:
        return None

    async def close(self) -> None:
        self.closed = True
        self._abort.set()

    async def control(self, command: AgentControl) -> ControlResult:
        if isinstance(command, Steer):
            return ControlAccepted()
        if isinstance(command, AnswerClarification):
            return ControlAccepted(request_id=command.request_id)
        return ControlRejected(reason="unsupported command", retryable=False)


class LiarFake:
    def __init__(self) -> None:
        self.agent_capabilities = AgentCapabilityManifest(
            contract_version=1,
            structured_events=_SUPPORTED,
            tool_lifecycle=_UNSUPPORTED,
            permissions=_SUPPORTED,
            clarification=_UNSUPPORTED,
            session_identity=_UNSUPPORTED,
            status=_UNSUPPORTED,
            usage=_UNSUPPORTED,
            steering=_UNSUPPORTED,
            targeted_cancellation=_UNSUPPORTED,
            workers=WorkerCapability(SupportLevel.UNSUPPORTED),
            voice_sessions=VoiceSessionCapability(SupportLevel.UNSUPPORTED),
        )

    def respond_streaming(
        self,
        text: str,
        *,
        on_event=None,
    ) -> AsyncIterator[str]:
        async def _stream() -> AsyncIterator[str]:
            del text, on_event
            yield "Sure."

        return _stream()

    async def respond(self, text: str, timeout: float = 300.0) -> str:
        del timeout
        return "Sure."

    async def abort(self) -> None:
        return None

    async def warmup(self) -> None:
        return None

    async def close(self) -> None:
        return None


def make_acp_client() -> AcpClient:
    return AcpClient(
        AcpProfile(
            name="conformance",
            binary=sys.executable,
            extra_args=(str(FAKE_ACP_SERVER),),
        ),
        cwd=str(FAKE_ACP_SERVER.parent),
        permission_mode="pending",
    )
