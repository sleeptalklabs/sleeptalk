"""Reference remote agent used by Agent Bridge loopback tests."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from sleeptalk.adapter import (
    AgentCapabilityManifest,
    CapabilitySupport,
    PermissionDecision,
    PermissionRequested,
    SessionIdentified,
    SupportLevel,
    ThinkingStarted,
    ToolEvent,
    ToolPhase,
    VoiceSessionCapability,
    WorkerCapability,
    publish_event,
)


def _supported() -> CapabilitySupport:
    return CapabilitySupport(SupportLevel.SUPPORTED)


def _unsupported() -> CapabilitySupport:
    return CapabilitySupport(SupportLevel.UNSUPPORTED)


class EchoRemoteAgent:
    def __init__(
        self,
        *,
        pause_s: float = 0.0,
        flood: int = 0,
        request_permission: bool = False,
    ) -> None:
        self.upstream_session_id = "remote-echo-1"
        self.warmed = False
        self.closed = False
        self.pause_s = pause_s
        self.flood = flood
        self.request_permission = request_permission
        self.abort_calls = 0
        self.prompts: list[str] = []
        self._abort = asyncio.Event()
        self.agent_capabilities = AgentCapabilityManifest(
            contract_version=1,
            structured_events=_supported(),
            tool_lifecycle=_supported(),
            permissions=_supported() if request_permission else _unsupported(),
            clarification=_unsupported(),
            session_identity=_supported(),
            status=_unsupported(),
            usage=_unsupported(),
            steering=_unsupported(),
            targeted_cancellation=_unsupported(),
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
                raise RuntimeError("remote agent is closed")
            self._abort.clear()
            self.prompts.append(text)
            publish_event(
                on_event,
                SessionIdentified(
                    upstream_session_id=self.upstream_session_id,
                    resumable=True,
                ),
            )
            publish_event(on_event, ThinkingStarted())
            if "tool" in text.lower():
                publish_event(
                    on_event,
                    ToolEvent(name="bash", phase=ToolPhase.STARTED, detail="running echo"),
                )
            if self.request_permission:
                publish_event(
                    on_event,
                    PermissionRequested(
                        request_id="perm-1",
                        prompt="Allow the demo tool?",
                        options=frozenset(
                            {PermissionDecision.DENY, PermissionDecision.ALLOW_ONCE}
                        ),
                    ),
                )
                await asyncio.sleep(0.15)
                if self._abort.is_set():
                    return
            count = self.flood if self.flood else 1
            for index in range(count):
                if self.pause_s:
                    try:
                        await asyncio.wait_for(self._abort.wait(), timeout=self.pause_s)
                        return
                    except TimeoutError:
                        pass
                if self._abort.is_set():
                    return
                if count == 1:
                    yield f"You said {text}."
                else:
                    yield f"Line {index + 1} of {text}."

        return _stream()

    async def respond(self, text: str, timeout: float = 300.0) -> str:
        del timeout
        return f"You said {text}."

    async def abort(self) -> None:
        self.abort_calls += 1
        self._abort.set()

    async def warmup(self) -> None:
        self.warmed = True

    async def close(self) -> None:
        self.closed = True
