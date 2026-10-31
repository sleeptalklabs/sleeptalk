"""Conformance tests: the reference Discord bot uses the session kernel
(r2-discord-kernel).

Covers: RealtimeSession lifecycle (identity, states, idempotent
connect/close, illegal transitions, events); the reference bot runs an
injected route under the RealtimeSession contract (connect on construct,
session identity stable, route exposed); barge-in/preamble/progress/
join-leave behavior remains owned by the audited runtime (existing
suites); no duplicate active voice runtime (single route/session path).
"""

from __future__ import annotations

from types import SimpleNamespace

import asyncio

import pytest

from sleeptalk.session import RealtimeSession, SessionLifecycleError
from sleeptalk.session_events import SessionEventType, SessionState


class RouteCap:
    class capabilities:  # noqa: N801
        class route_type:  # noqa: N801
            value = "cascade"


class FakeRoute(RouteCap):
    def __init__(self) -> None:
        self.prepared = False
        self.closed = False

    async def prepare(self, session) -> None:
        self.prepared = True

    async def run_turn(self, route_input, ctx):
        return None

    async def cancel_turn(self, turn_id) -> None:
        return None

    async def close(self) -> None:
        self.closed = True


class FakeMeter:
    session_id = "meter-session-1"


class BuiltSession:
    usage_meter = FakeMeter()
    route = None

    def __init__(self, route=None):
        if route is not None:
            BuiltSession.route = route


# -- RealtimeSession lifecycle ---------------------------------------------------


def test_session_identity_from_meter_is_stable() -> None:
    session = RealtimeSession(BuiltSession())
    assert session.session_id == "meter-session-1"
    session.connect()
    session.close()
    assert session.session_id == "meter-session-1"  # stable for the life


def test_session_identity_minted_without_meter() -> None:
    built = type("B", (), {"usage_meter": None, "route": None})()
    session = RealtimeSession(built)
    assert len(session.session_id) >= 8
    other = RealtimeSession(built)
    assert session.session_id != other.session_id  # per-session identity


def test_lifecycle_states_and_events() -> None:
    session = RealtimeSession(BuiltSession())
    assert session.state is SessionState.CREATED
    session.connect()
    assert session.state is SessionState.ACTIVE
    types = [e.type for e in session.events]
    assert types == [
        SessionEventType.SESSION_STATE,  # connecting
        SessionEventType.SESSION_STATE,  # active
    ]
    assert session.events[0].payload["state"] == "connecting"
    assert session.events[1].payload["state"] == "active"
    session.close()
    assert session.state is SessionState.CLOSED
    types = [e.type for e in session.events]
    assert types.count(SessionEventType.SESSION_STATE) == 4  # + closing + closed


def test_connect_idempotent_while_active() -> None:
    session = RealtimeSession(BuiltSession())
    session.connect()
    session.connect()  # no-op, never an error
    assert session.state is SessionState.ACTIVE
    assert len(session.events) == 2


def test_close_idempotent() -> None:
    session = RealtimeSession(BuiltSession())
    session.connect()
    session.close()
    session.close()  # single-writer idempotent shutdown
    assert session.state is SessionState.CLOSED


def test_close_from_created_is_legal() -> None:
    session = RealtimeSession(BuiltSession())
    session.close()
    assert session.state is SessionState.CLOSED


def test_illegal_transitions_raise() -> None:
    session = RealtimeSession(BuiltSession())
    with pytest.raises(SessionLifecycleError):
        session._transition(SessionState.ACTIVE)  # CREATED -> ACTIVE illegal
    closed = RealtimeSession(BuiltSession())
    closed.close()
    with pytest.raises(SessionLifecycleError):
        closed._transition(SessionState.CONNECTING)  # CLOSED is terminal


def test_route_and_route_type_exposed() -> None:
    route = FakeRoute()
    session = RealtimeSession(BuiltSession(route))
    assert session.route is route
    assert session.route_type.value == "cascade"


def test_summary_is_redacted() -> None:
    route = FakeRoute()
    session = RealtimeSession(BuiltSession(route))
    session.connect()
    summary = session.summary()
    assert summary["sessionId"] == "meter-session-1"
    assert summary["state"] == "active"
    assert summary["routeType"] == "cascade"
    assert "token" not in str(summary).lower()
    assert "key" not in str(summary).lower()


# -- reference bot uses the kernel ------------------------------------------------


class _NoopAgent:
    async def respond_streaming(self, text, *, on_event=None):
        yield "sentence"
        return

    async def respond(self, text, timeout=300.0):
        return "sentence"

    async def abort(self):
        return None

    async def warmup(self):
        return None

    async def close(self):
        return None


def _make_bot(route=None):
    """Construct the reference bot with the kernel path exercised."""
    import sys

    if "discord" not in sys.modules:
        pytest.skip("discord.py not installed in this environment")

    from sleeptalk.config import AgentConfig, SleeptalkConfig
    from sleeptalk.discord import DiscordEnv, PiDiscordBot

    config = SleeptalkConfig(agent=AgentConfig(name="Test", slug="test"))
    env = DiscordEnv(token="fake-token")
    return PiDiscordBot, config, env


@pytest.mark.asyncio
async def test_bot_with_route_runs_realtime_session() -> None:
    """The reference bot wraps an injected route in RealtimeSession:
    connect on construct, stable identity, route exposed."""
    try:
        from sleeptalk.discord import PiDiscordBot
    except Exception:
        pytest.skip("bot import needs the server env")

    from sleeptalk.config import AgentConfig, SleeptalkConfig
    from sleeptalk.discord import DiscordEnv

    from sleeptalk.config import TransportConfig

    config = SleeptalkConfig(
        agent=AgentConfig(name="kernel-test", slug="kernel-test", type="pi"),
        transport=TransportConfig(authorization_required=False),
    )
    route = FakeRoute()
    bot = PiDiscordBot(
        config,
        DiscordEnv(token="fake-token"),
        realtime_session=RealtimeSession(SimpleNamespace(
            agent=_NoopAgent(), stt=object(), streaming_stt=object(),
            tts=object(), usage_meter=FakeMeter(), route=route,
        )),
    )
    assert bot.realtime_session is not None
    assert bot.realtime_session.state is SessionState.ACTIVE
    assert bot.realtime_session.route is route
    assert bot.realtime_session.session_id == "meter-session-1"
    assert bot.session_route is route


@pytest.mark.asyncio
async def test_bot_requires_realtime_session() -> None:
    from sleeptalk.discord import DiscordEnv, PiDiscordBot
    from sleeptalk.config import SleeptalkConfig

    with pytest.raises(TypeError, match="realtime_session"):
        PiDiscordBot(SleeptalkConfig(), DiscordEnv(token="fake-token"))


@pytest.mark.asyncio
async def test_an_unfinished_locked_bot_is_a_config_error_with_the_fix() -> None:
    from sleeptalk.config import AgentConfig, SleeptalkConfig, TransportConfig
    from sleeptalk.discord import DiscordEnv, PiDiscordBot
    from sleeptalk.discord_check import DiscordCheckError

    config = SleeptalkConfig(agent=AgentConfig(name="t", slug="t", type="pi"),
                             transport=TransportConfig(type="discord", guild_id="10"))
    session = RealtimeSession(SimpleNamespace(agent=_NoopAgent(), stt=object(), streaming_stt=object(),
                                              tts=object(), usage_meter=FakeMeter(), route=FakeRoute()))
    with pytest.raises(DiscordCheckError, match="Finish setup"):
        PiDiscordBot(config, DiscordEnv(token="fake-token"), realtime_session=session)


class FakeTTS:
    def __init__(self, speed: float | None) -> None:
        from sleeptalk.providers import ProviderSpec

        self.spec = ProviderSpec(provider="cartesia", speed=speed)


class BuiltWithTTS:
    usage_meter = FakeMeter()
    route = None

    def __init__(self, tts=None, fallbacks=()):
        self.tts = tts
        self.tts_fallbacks = list(fallbacks)


def test_cycle_tts_speed_replaces_primary_and_fallbacks() -> None:
    primary = FakeTTS(1.0)
    fallback = FakeTTS(1.0)
    session = RealtimeSession(BuiltWithTTS(primary, (fallback,)))
    assert session.cycle_tts_speed() == 1.25
    assert primary.spec.speed == 1.25
    assert fallback.spec.speed == 1.25


def test_cycle_tts_speed_returns_operator_step() -> None:
    primary = FakeTTS(1.25)
    session = RealtimeSession(BuiltWithTTS(primary))
    assert session.cycle_tts_speed() == 1.5
    assert primary.spec.speed == 1.5


def test_cycle_tts_speed_wraps_to_half() -> None:
    primary = FakeTTS(1.5)
    session = RealtimeSession(BuiltWithTTS(primary))
    assert session.cycle_tts_speed() == 0.5


def test_cycle_tts_speed_snaps_a_retired_two_x_profile_onto_the_staircase() -> None:
    primary = FakeTTS(2.0)
    session = RealtimeSession(BuiltWithTTS(primary))
    assert session.current_tts_speed() == 1.5
    assert session.cycle_tts_speed() == 0.5
    assert primary.spec.speed == 0.5


def test_cycle_tts_speed_without_adapters_returns_one() -> None:
    session = RealtimeSession(BuiltSession())
    assert session.cycle_tts_speed() == 1.0


def test_current_tts_speed_reads_operator_step() -> None:
    session = RealtimeSession(BuiltWithTTS(FakeTTS(1.25)))
    assert session.current_tts_speed() == 1.25
    session.cycle_tts_speed()
    assert session.current_tts_speed() == 1.5


class HybridSpeedRoute:
    def __init__(self) -> None:
        self.speed = 1.0

    def current_tts_speed(self) -> float:
        return self.speed

    def set_tts_speed(self, value: float | None) -> float:
        from sleeptalk.speech_speed import snap_speed

        self.speed = snap_speed(value)
        return self.speed

    def cycle_tts_speed(self) -> float:
        from sleeptalk.speech_speed import next_speed

        self.speed = next_speed(self.speed)
        return self.speed


def test_speed_controls_fall_through_to_a_native_route_without_tts_adapters() -> None:
    route = HybridSpeedRoute()
    session = RealtimeSession(BuiltWithTTS())
    session._route = route

    assert session.current_tts_speed() == 1.0
    assert session.set_tts_speed(1.5) == 1.5
    assert session.cycle_tts_speed() == 0.5


def test_speed_controls_keep_using_cascade_adapters_when_present() -> None:
    primary = FakeTTS(1.0)
    route = HybridSpeedRoute()
    session = RealtimeSession(BuiltWithTTS(primary))
    session._route = route

    assert session.set_tts_speed(1.5) == 1.5
    assert primary.spec.speed == 1.5
    assert route.speed == 1.0
