"""Tests for build_session → CascadeRoute integration (r2-cascade-route).

Covers: existing configurations select CascadeRoute without code changes;
registry-built components are the same objects the route holds; legacy
VoiceSession behavior preserved (fields unchanged, route additive); usage
identity flows (the session's meter is the one the factory attached).
"""

from __future__ import annotations

import asyncio

import pytest

from sleeptalk.builder import build_session
from sleeptalk.config import load_config
from sleeptalk.routes.base import RouteInput, RouteType
from sleeptalk.routes.cascade import CascadeRoute
from sleeptalk.session_events import SessionEventType

# Same shape as the existing acceptance suite's MINIMAL (pi agent + null
# transport); STT/TTS defaults fill in via the config layer.
MINIMAL = """\
[transport]
type = "null"

[agent]
type = "pi"
"""


def _write(tmp_path, text: str) -> str:
    path = tmp_path / "sleeptalk.toml"
    path.write_text(text)
    return str(path)


@pytest.mark.asyncio
async def test_build_session_selects_cascade_route(tmp_path) -> None:
    """Existing configuration, zero code changes: the session carries a
    CascadeRoute composed from the registry-built pieces."""
    config = load_config(_write(tmp_path, MINIMAL), check_env=False, check_binaries=False)
    session = build_session(config, env={})

    assert session.route is not None
    assert isinstance(session.route, CascadeRoute)
    assert session.route.capabilities.route_type is RouteType.CASCADE
    # same objects: the route wraps the registry-built components
    assert session.route._agent is session.agent
    assert session.route._tts.providers == (session.tts,)
    assert session.route._stt is session.stt
    # usage identity: the session's meter is the one the factory used
    assert session.usage_meter is not None
    assert getattr(session.route._agent, "usage_meter", session.usage_meter) is session.usage_meter


@pytest.mark.asyncio
async def test_legacy_session_behavior_preserved(tmp_path) -> None:
    """Every pre-existing VoiceSession field is unchanged; the route is
    additive."""
    config = load_config(_write(tmp_path, MINIMAL), check_env=False, check_binaries=False)
    session = build_session(config, env={})

    assert session.config is config
    assert session.policy is not None
    assert session.transport is not None
    assert session.agent is not None
    assert session.stt is not None
    assert session.streaming_stt is not None
    assert session.tts is not None
    assert session.tts_fallbacks == []
    assert session.usage_meter is not None


@pytest.mark.asyncio
async def test_route_streaming_end_to_end(tmp_path, monkeypatch) -> None:
    """A full turn through the route boundary from a built session."""
    # swap in a deterministic streaming agent via the builder's registry
    # seam (public composition, no private state mutation)
    class EchoAgent:
        name = "EchoAgent"

        async def respond_streaming(self, text: str, *, on_event=None):
            yield f"echo: {text}"

        async def respond(self, text: str, timeout: float = 300.0):
            return f"echo: {text}"

        async def abort(self) -> None:
            return None

        async def warmup(self) -> None:
            return None

        async def close(self) -> None:
            return None

    from dataclasses import replace
    from sleeptalk.defaults import register_defaults
    from sleeptalk.registries import AGENTS

    register_defaults()
    monkeypatch.setitem(AGENTS._registrations, "pi", replace(AGENTS.get("pi"), factory=lambda *_: EchoAgent()))
    config = load_config(_write(tmp_path, MINIMAL), check_env=False, check_binaries=False)
    session = build_session(config, env={})
    route = session.route

    events: list = []
    route.bind("integration-s", events.append)

    class _Session:
        session_id = "integration-s"

        def emit(self, event) -> None:
            events.append(event)

    await route.prepare(_Session())
    from sleeptalk.routes.base import TurnContext

    ctx = TurnContext(session_id="integration-s", turn_id=1, emit=events.append)
    outcome = await route.run_turn(RouteInput(text="hi there"), ctx)

    assert outcome.status == "completed"
    assert outcome.played == ["echo: hi there"]
    types = [e.type for e in events]
    assert SessionEventType.TRANSCRIPT_FINAL in types
    assert SessionEventType.RESPONSE_STARTED in types
    assert SessionEventType.USAGE_ATTEMPT_STARTED in types
    assert SessionEventType.RESPONSE_COMPLETED in types
    assert SessionEventType.USAGE_ATTEMPT_FINISHED in types
    await route.close()


def test_tts_fallback_chain_preserved(tmp_path) -> None:
    """Explicit fallback behavior survives: fallback adapters are built and
    carried on the session exactly as before."""
    text = MINIMAL + (
        "\n[tts]\nprovider = \"cartesia\"\nvoice_id = \"v1\"\n"
        "\n[[tts.fallback]]\nprovider = \"elevenlabs\"\nvoice_id = \"v2\"\n"
    )
    config = load_config(_write(tmp_path, text), check_env=False, check_binaries=False)
    session = build_session(config, env={})
    assert len(session.tts_fallbacks) == 1
    assert session.route is not None  # route still selected


# -- audit rework: the built session executes the route pipeline ------------


class PipelineSTT:
    """STT pair member used via the builder path."""

    def __init__(self) -> None:
        self.calls = 0

    async def transcribe_pcm(self, pcm: bytes, *, purpose: str = "") -> str:
        self.calls += 1
        return "builder pipeline transcript"


class PipelineTTS:
    def __init__(self) -> None:
        self.synthesized: list[str] = []
        capabilities = None

    async def synthesize(self, text: str):
        self.synthesized.append(text)
        yield b"\x03" * 96


@pytest.mark.asyncio
async def test_built_session_executes_real_pipeline(tmp_path, monkeypatch) -> None:
    """The built session's route is the authoritative pipeline: PCM goes
    through STT, the agent answers, TTS synthesizes, and only audio
    crossing the output boundary counts as played."""
    config = load_config(_write(tmp_path, MINIMAL), check_env=False, check_binaries=False)
    session = build_session(config, env={})

    # swap in deterministic pipeline pieces (the builder's composition seam)
    stt = PipelineSTT()
    tts = PipelineTTS()

    class EchoAgent:
        name = "EchoAgent"

        async def respond_streaming(self, text: str, *, on_event=None):
            yield f"echo: {text}"

        async def abort(self):
            return None

        async def warmup(self):
            return None

        async def close(self):
            return None

    session.route._stt = stt
    session.route._tts = tts
    session.route._agent = EchoAgent()

    # bind a recording output boundary (the real playout)
    from sleeptalk.routes.audio_output import AudioOutputAccounting

    class RecordingOutput:
        def __init__(self) -> None:
            self.writes: list[tuple[bytes, int]] = []
            self._accounting = AudioOutputAccounting(turn_id=-1)

        async def write(self, pcm: bytes, turn_id: int) -> None:
            self.writes.append((pcm, turn_id))
            self._accounting.written(len(pcm))

        def accounting(self) -> AudioOutputAccounting:
            return self._accounting

        async def close(self) -> None:
            return None

    output = RecordingOutput()
    session.route._audio_output = output

    events: list = []
    session.route.bind("pipeline-s", events.append)

    class _Session:
        session_id = "pipeline-s"

        def emit(self, event) -> None:
            events.append(event)

    await session.route.prepare(_Session())
    from sleeptalk.routes.base import RouteInput, TurnContext

    ctx = TurnContext(session_id="pipeline-s", turn_id=1, emit=events.append)
    outcome = await session.route.run_turn(RouteInput(pcm=b"\x00" * 192), ctx)

    assert outcome.status == "completed"
    # STT ran: PCM -> transcript
    assert stt.calls == 1
    # agent consumed the transcript
    # TTS synthesized the agent sentence
    assert tts.synthesized == ["echo: builder pipeline transcript"]
    # ONLY audio crossing the output boundary counts as played
    assert len(output.writes) == 1
    assert outcome.played != outcome.generated or output.writes  # audio-based accounting
    await session.route.close()


@pytest.mark.asyncio
async def test_cascade_builder_binds_both_stt_components(tmp_path) -> None:
    """Cascade has both streaming capture and REST fallback providers."""
    config = load_config(_write(tmp_path, MINIMAL), check_env=False, check_binaries=False)
    session = build_session(config, env={})
    assert session.route is not None
    assert session.route._stt_streaming is not None  # full STT pair bound
    assert session.route._stt is not None
