"""Independent black-box acceptance for the public session kernel entrypoint."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest
from sleeptalk.builder import build_session
from sleeptalk.config import load_config
from sleeptalk.defaults import register_defaults
from sleeptalk.registries import TRANSPORTS
from sleeptalk.routes.base import (
    InFlightTurn,
    InterruptionGranularity,
    RouteCapabilities,
    RouteInput,
    RouteType,
    TurnContext,
    TurnOutcome,
)
from sleeptalk.session import RealtimeSession
from sleeptalk.session_events import SessionEventType, SessionState, response


class RecordingTransport:
    """Public registry-shaped transport recording its canonical session."""

    def __init__(self, config, env: dict[str, str]) -> None:
        self.config = config
        self.received: list[RealtimeSession] = []

    async def run(self, session: RealtimeSession) -> None:
        self.received.append(session)


def _public_config(tmp_path):
    register_defaults()
    TRANSPORTS.register(
        "session-kernel-probe",
        RecordingTransport,
        config_fields={"log_path"},
    )
    path = tmp_path / "sleeptalk.toml"
    path.write_text(
        """\
[transport]
type = "session-kernel-probe"

[agent]
type = "pi"
""",
        encoding="utf-8",
    )
    return load_config(path, check_env=False, check_binaries=False)


@pytest.mark.asyncio
async def test_public_builder_returns_canonical_session_and_transport_gets_same_object(
    tmp_path,
) -> None:
    session = build_session(_public_config(tmp_path), env={})

    assert isinstance(session, RealtimeSession)
    transport = session.transport
    await session.run()

    assert transport.received == [session]
    assert transport.received[0] is session
    assert session.state is SessionState.CLOSED


class RecordingUsage:
    def __init__(self) -> None:
        self.session_id = "usage-session-1"
        self.finished: list[str] = []

    def finish_session(self, *, status: str = "closed") -> None:
        self.finished.append(status)

    def snapshot(self) -> dict[str, Any]:
        return {"sessionId": self.session_id, "finished": list(self.finished)}


class RecordingRoute:
    capabilities = RouteCapabilities(
        route_type=RouteType.CASCADE,
        streaming_output=True,
        interruption=True,
        interruption_granularity=InterruptionGranularity.SENTENCE,
        resume_after_interruption=True,
        usage_operations=("agent.stream",),
    )

    def __init__(self) -> None:
        self.prepared_with: list[RealtimeSession] = []
        self.turns: list[tuple[RouteInput, TurnContext]] = []
        self.cancelled: list[int] = []
        self.settled: list[tuple[int, str]] = []
        self.closed = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.active: InFlightTurn | None = None

    async def prepare(self, session: RealtimeSession) -> None:
        self.prepared_with.append(session)

    async def run_turn(
        self, route_input: RouteInput, ctx: TurnContext
    ) -> TurnOutcome:
        self.turns.append((route_input, ctx))
        self.active = InFlightTurn(ctx.turn_id, asyncio.current_task())
        ctx.emit(
            response(
                ctx.session_id,
                ctx.turn_id,
                SessionEventType.RESPONSE_STARTED,
            )
        )
        self.started.set()
        await self.release.wait()
        self.active = None
        return TurnOutcome(
            status="cancelled",
            turn_id=ctx.turn_id,
            played=[],
            generated=["not played"],
        )

    def active_turn(self) -> InFlightTurn | None:
        return self.active

    async def cancel_turn(self, turn_id: int) -> None:
        self.cancelled.append(turn_id)
        self.release.set()

    async def settle_usage(self, turn_id: int, *, status: str) -> None:
        self.settled.append((turn_id, status))

    async def close(self) -> None:
        self.closed += 1


@dataclass
class PublicAssembly:
    route: RecordingRoute = field(default_factory=RecordingRoute)
    usage_meter: RecordingUsage = field(default_factory=RecordingUsage)
    transport: RecordingTransport | None = None
    config: Any = None
    policy: Any = None
    agent: Any = None
    stt: Any = None
    streaming_stt: Any = None
    tts: Any = None
    tts_fallbacks: list[Any] = field(default_factory=list)


def test_realtime_session_compatibility_wrap_preserves_object_and_identity() -> None:
    session = RealtimeSession(PublicAssembly())
    session.connect()
    original_events = session.events

    wrapped_again = RealtimeSession(session)

    assert wrapped_again is session
    assert wrapped_again.session_id == "usage-session-1"
    assert wrapped_again.state is SessionState.ACTIVE
    assert wrapped_again.events == original_events


@pytest.mark.asyncio
async def test_public_turn_cancel_and_aclose_own_route_events_and_usage() -> None:
    assembly = PublicAssembly()
    session = RealtimeSession(assembly)

    assert session.usage_meter is assembly.usage_meter

    await session.prepare()
    turn = asyncio.create_task(session.run_turn(RouteInput(text="hello")))
    await assembly.route.started.wait()
    await session.cancel()
    outcome = await turn
    await session.aclose()

    assert assembly.route.prepared_with == [session]
    assert len(assembly.route.turns) == 1

    route_input, context = assembly.route.turns[0]
    assert route_input == RouteInput(text="hello")
    assert context.session_id == session.session_id == assembly.usage_meter.session_id
    assert context.turn_id == outcome.turn_id == 1

    assert assembly.route.cancelled == [1]
    assert assembly.route.settled == [(1, "cancelled")]
    assert assembly.route.closed == 1
    assert assembly.usage_meter.finished == ["closed"]
    assert session.state is SessionState.CLOSED

    assert any(
        event.type is SessionEventType.RESPONSE_STARTED
        and event.session_id == session.session_id
        and event.turn_id == 1
        for event in session.events
    )
    assert all(event.session_id == session.session_id for event in session.events)


def test_sync_close_remains_idempotent_lifecycle_compatibility() -> None:
    assembly = PublicAssembly()
    session = RealtimeSession(assembly)
    session.connect()

    assert session.close() is None
    assert session.close() is None
    assert session.state is SessionState.CLOSED

    # Synchronous close preserves compatibility and owns lifecycle only.
    # Asynchronous route/resource teardown belongs to aclose().
    assert assembly.route.closed == 0
    assert assembly.usage_meter.finished == []
