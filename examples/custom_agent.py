"""Custom AgentAdapter that stays on public sleeptalk APIs."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from sleeptalk.adapter import (
    AgentCapabilityManifest,
    CapabilitySupport,
    SessionIdentified,
    SupportLevel,
    VoiceSessionCapability,
    WorkerCapability,
    publish_event,
    typed_events,
)
from sleeptalk.registries import AGENTS
from sleeptalk.streaming import EventGate, SentenceStreamer, ToolVocab, narrate_tool

AGENT_NAME = "custom-echo"

VOCAB = ToolVocab(
    kinds={"shell": "bash"},
    subject_args={"bash": ("command",)},
)


def _supported() -> CapabilitySupport:
    return CapabilitySupport(SupportLevel.SUPPORTED)


def _unsupported() -> CapabilitySupport:
    return CapabilitySupport(SupportLevel.UNSUPPORTED)


class CustomAgent:
    def __init__(self, *, pause_s: float = 0.0) -> None:
        self.name = AGENT_NAME
        self.upstream_session_id = ""
        self.warmed = False
        self.closed = False
        self.pause_s = pause_s
        self._abort = asyncio.Event()
        self.agent_capabilities = AgentCapabilityManifest(
            contract_version=1,
            structured_events=_supported(),
            tool_lifecycle=_supported(),
            permissions=_unsupported(),
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
                raise RuntimeError("custom agent is closed")
            self._abort.clear()
            streamer = SentenceStreamer()
            gate = EventGate(on_event)
            publish_event(
                on_event,
                SessionIdentified(
                    upstream_session_id=self.upstream_session_id,
                    resumable=False,
                ),
            )
            gate.fire("thinking")
            if "tool" in text.lower():
                kind, detail = narrate_tool(
                    "shell",
                    {"command": "echo hello"},
                    VOCAB,
                )
                gate.fire("tool")
                if detail:
                    gate.emit(f"tooldetail:{detail}")
                gate.emit(f"tool:{kind}")
            gate.fire("text")
            chunks = ("First sentence. ", "Second sentence.")
            if text.strip():
                chunks = (f"You said {text.strip()}. ", "That is all.")
            for index, chunk in enumerate(chunks):
                for sentence in streamer.feed(chunk):
                    if self._abort.is_set():
                        return
                    yield sentence
                    if index == 0 and self.pause_s > 0:
                        try:
                            await asyncio.wait_for(
                                self._abort.wait(),
                                timeout=self.pause_s,
                            )
                        except TimeoutError:
                            pass
                        if self._abort.is_set():
                            return
            if self._abort.is_set():
                return
            tail = streamer.flush()
            if tail:
                yield tail

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
        self.upstream_session_id = "custom-echo-1"

    async def close(self) -> None:
        self.closed = True
        self._abort.set()


def register() -> None:
    if AGENT_NAME in AGENTS:
        return
    AGENTS.register(
        AGENT_NAME,
        lambda config, env: CustomAgent(),
        install_hint="custom agent example needs only python3",
    )


async def main() -> None:
    register()
    agent = CustomAgent()
    await agent.warmup()
    events: list[object] = []
    sentences = [
        sentence
        async for sentence in agent.respond_streaming(
            "hello there",
            on_event=typed_events(events.append),
        )
    ]
    await agent.abort()
    await agent.close()
    print("identity:", agent.upstream_session_id)
    print("sentences:", sentences)
    print("events:", [type(event).__name__ for event in events])
    assert agent.warmed
    assert agent.upstream_session_id == "custom-echo-1"
    assert any(isinstance(event, SessionIdentified) for event in events)
    assert sentences
    print("CUSTOM-AGENT ACCEPTANCE: OK")


if __name__ == "__main__":
    asyncio.run(main())
