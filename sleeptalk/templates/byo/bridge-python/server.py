#!/usr/bin/env python3
"""A remote agent that Sleeptalk reaches over Agent Bridge v1.

Run this next to your agent on another machine. Sleeptalk connects in with a
bearer token, sends turns, and receives sentences and events. Use Bridge when
the agent is long-lived and remote and needs permissions, steering, or
reconnects. For a local program use `--kind command`; for a web service that
answers one request per turn use `--kind http`.

This template uses Sleeptalk's reference server (`pip install sleeptalk`).
Other languages implement the frames in docs/agent-bridge-protocol.md.

Security: the reference server listens in plaintext and refuses any address
but loopback. Put a TLS terminator (Caddy, nginx, a cloud load balancer) in
front and give Sleeptalk the wss:// URL.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator

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
from sleeptalk.agent_bridge import AgentBridgeServer
from sleeptalk.streaming import SentenceStreamer

HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8766"))
TOKEN = os.environ.get("AGENT_BRIDGE_TOKEN", "")

SUPPORTED = CapabilitySupport(SupportLevel.SUPPORTED)
UNSUPPORTED = CapabilitySupport(SupportLevel.UNSUPPORTED)


class Agent:
    """Implements Sleeptalk's AgentAdapter. Replace `_reply` with your agent."""

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

    def __init__(self) -> None:
        self._aborted = asyncio.Event()

    async def _reply(self, text: str, on_event) -> AsyncIterator[str]:
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
            yield f"You said: {text.strip()}."
        # --- to here. ---

    def respond_streaming(self, text: str, *, on_event=None) -> AsyncIterator[str]:
        async def stream() -> AsyncIterator[str]:
            self._aborted.clear()
            streamer = SentenceStreamer()
            async for chunk in self._reply(text, on_event):
                if self._aborted.is_set():
                    return
                publish_event(on_event, AssistantDelta(text=chunk))
                for sentence in streamer.feed(chunk):
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


async def main() -> None:
    if not TOKEN:
        raise SystemExit("set AGENT_BRIDGE_TOKEN to a long random value (and the same value for Sleeptalk)")
    server = AgentBridgeServer(token=TOKEN, agent=Agent())
    host, port = await server.start_ws(HOST, PORT)
    print(f"Agent Bridge listening on ws://{host}:{port}/v1/agent-bridge", flush=True)
    await server.wait_closed()


if __name__ == "__main__":
    asyncio.run(main())
