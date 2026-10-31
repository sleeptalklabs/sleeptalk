"""AGENT_NAME: an in-process Python adapter for Sleeptalk.

Choose this over `--kind command` when your agent is Python and you want the
full adapter contract in process: typed events, permissions, steering,
workers, or voice sessions.

Guide: docs/bring-your-own-agent.md in the Sleeptalk repository.
Contract: docs/agent-adapter-contract.md.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from typing import Any

from sleeptalk.adapter import (
    AgentCapabilityManifest,
    AssistantDelta,
    CapabilitySupport,
    SupportLevel,
    ThinkingStarted,
    ToolEvent,
    ToolPhase,
    VoiceSessionCapability,
    WorkerCapability,
    publish_event,
)
from sleeptalk.registries import AGENTS, ComponentOptions
from sleeptalk.streaming import SentenceStreamer

SUPPORTED = CapabilitySupport(SupportLevel.SUPPORTED)
UNSUPPORTED = CapabilitySupport(SupportLevel.UNSUPPORTED)


class Options(ComponentOptions):
    """Private settings from [agent.options]. Unknown keys are rejected."""

    greeting: str = "You said"


class Agent:
    agent_capabilities = AgentCapabilityManifest(
        contract_version=1,
        structured_events=SUPPORTED,
        tool_lifecycle=SUPPORTED,
        permissions=UNSUPPORTED,
        clarification=UNSUPPORTED,
        session_identity=UNSUPPORTED,
        status=UNSUPPORTED,
        usage=UNSUPPORTED,
        steering=UNSUPPORTED,
        targeted_cancellation=UNSUPPORTED,
        workers=WorkerCapability(SupportLevel.UNSUPPORTED),
        voice_sessions=VoiceSessionCapability(SupportLevel.UNSUPPORTED),
    )

    def __init__(self, options: Options, env: dict[str, str]) -> None:
        self.options = options
        self.env = env  # read API keys from here, never from the profile
        self._aborted = asyncio.Event()

    async def _reply(self, text: str, on_event: Callable[..., Any] | None) -> AsyncIterator[str]:
        """Yield reply text chunks. Replace the demo with your model call."""

        # --- Replace from here with your model call. ---
        words = text.strip().lower()
        publish_event(on_event, ThinkingStarted())
        if words == "slow":
            for number in ("One.", "Two.", "Three.", "Four.", "Five."):
                yield number + " "
                await asyncio.sleep(0.3)
        elif words == "hang":
            await self._aborted.wait()
        elif words == "tool":
            publish_event(on_event, ToolEvent(name="read", call_id="t1", detail="reading the readme"))
            publish_event(on_event, ToolEvent(name="read", call_id="t1", phase=ToolPhase.COMPLETED))
            yield "The readme says hello."
        else:
            yield f"{self.options.greeting}: {text.strip()}."
        # --- to here. ---

    def respond_streaming(self, text: str, *, on_event: Callable[..., Any] | None = None) -> AsyncIterator[str]:
        async def stream() -> AsyncIterator[str]:
            self._aborted.clear()
            streamer = SentenceStreamer()
            async for chunk in self._reply(text, on_event):
                if self._aborted.is_set():
                    return
                publish_event(on_event, AssistantDelta(text=chunk))
                for sentence in streamer.feed(chunk):
                    if self._aborted.is_set():
                        return
                    yield sentence
            tail = streamer.flush()
            if tail and not self._aborted.is_set():
                yield tail

        return stream()

    async def respond(self, text: str, timeout: float = 300.0) -> str:
        return " ".join([sentence async for sentence in self.respond_streaming(text)])

    async def abort(self) -> None:
        self._aborted.set()

    async def warmup(self) -> None:
        return None

    async def close(self) -> None:
        self._aborted.set()


def register() -> None:
    AGENTS.register(
        "AGENT_NAME",
        lambda config, env: Agent(Options.model_validate(config.options or {}), env),
        options_schema=Options,
        config_fields={"options", "name", "slug", "persona_instructions", "workspace_dir"},
    )
