"""Print what the primary voice would say for a detached worker.

No Discord, no API keys. Run from the sleeptalk package:

    uv run python examples/subagent_voice_demo.py
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from sleeptalk.adapter import (
    AgentCapabilityManifest,
    CapabilitySupport,
    SupportLevel,
    VoiceSessionCapability,
    WorkerCapability,
    WorkerEvent,
    WorkerEvidence,
    WorkerPhase,
    publish_event,
)
from sleeptalk.routes.cascade import CascadeRoute
from sleeptalk.session import RealtimeSession
from sleeptalk.session_events import SessionEventType


def _manifest() -> AgentCapabilityManifest:
    supported = CapabilitySupport(SupportLevel.SUPPORTED)
    unsupported = CapabilitySupport(SupportLevel.UNSUPPORTED)
    return AgentCapabilityManifest(
        contract_version=1,
        structured_events=supported,
        tool_lifecycle=supported,
        permissions=unsupported,
        clarification=unsupported,
        session_identity=unsupported,
        status=unsupported,
        usage=unsupported,
        steering=unsupported,
        targeted_cancellation=unsupported,
        workers=WorkerCapability(
            SupportLevel.SUPPORTED, evidence=WorkerEvidence.LIFECYCLE_NATIVE
        ),
        voice_sessions=VoiceSessionCapability(SupportLevel.UNSUPPORTED),
    )


class DemoAgent:
    def __init__(self) -> None:
        self.agent_capabilities = _manifest()

    async def respond_streaming(self, text: str, *, on_event=None):
        if "running" in text.lower():
            yield "The roster already answered that."
            return
        publish_event(
            on_event,
            WorkerEvent(
                worker_id="researcher-1",
                phase=WorkerPhase.STARTED,
                label="researcher",
                detached=True,
                summary="compare frameworks",
            ),
        )
        yield "I'll keep chatting while that runs."
        publish_event(
            on_event,
            WorkerEvent(
                worker_id="researcher-1",
                phase=WorkerPhase.COMPLETED,
                label="researcher",
                summary="comparison ready",
            ),
        )

    async def abort(self) -> None:
        return None

    async def warmup(self) -> None:
        return None

    async def close(self) -> None:
        return None


async def _run() -> None:
    spoken: list[str] = []

    async def speak(line: str, turn_id: int) -> None:
        del turn_id
        spoken.append(line)
        print(f"SPEAK: {line}")

    agent = DemoAgent()
    route = CascadeRoute(agent=agent, tts=None, speak=speak)
    session = RealtimeSession(
        SimpleNamespace(route=route, agent=agent, policy=None, config=None)
    )
    session.subscribe(
        lambda event: print(
            f"EVENT: {event.type.value}"
            + (
                f" worker={event.payload.get('workerId')} phase={event.payload.get('phase')}"
                if event.type is SessionEventType.ROSTER_CHANGED
                else ""
            )
        )
    )
    print("--- turn 1: start detached research ---")
    await session.run_turn(text="compare those frameworks")
    print("--- turn 2: status ---")
    await session.run_turn(text="what's still running?")
    print("--- spoken ---")
    for line in spoken:
        print(f"  {line}")
    await session.aclose()


if __name__ == "__main__":
    asyncio.run(_run())
