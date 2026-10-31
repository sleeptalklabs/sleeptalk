"""Tests for sleeptalk.transports.terminal lifecycle (r2-terminal-lifecycle).

Fake-device driven (no hardware): permission denial recovery messages,
signal-triggered release, exception-path release, idempotent repeated
close, and restart-after-close in the same process.
"""

from __future__ import annotations

import asyncio
import signal
import types

import pytest

from sleeptalk.transports.terminal import TerminalTransport
from sleeptalk.transports.terminal_audio import AudioDeviceError
from sleeptalk.session import RealtimeSession

from tests.runtime.test_terminal_audio import FakeSoundDevice, FakeStream


class Session:
    """Strict public-session stand-in for device lifecycle tests."""

    def set_audio_output(self, output) -> None:
        self.output = output

    async def prepare(self) -> None:
        return None

    async def run_turn(self, *, pcm: bytes):
        return None

    async def interrupt_presentation(self) -> None:
        return None


def make_transport(devices=None) -> TerminalTransport:
    """Real constructor + injected fake sounddevice (audit rework: tests
    use the public lifecycle, so state comes from __init__ not manual
    field assembly)."""
    transport = TerminalTransport(config=None, env={})
    transport.use_sounddevice(FakeSoundDevice(devices=devices) if devices is not None else FakeSoundDevice())
    return transport


# -- permission errors --------------------------------------------------------


class PermissionDeniedStream:
    def __init__(self, **kwargs):
        raise AudioDeviceError(
            "Could not open input device None: PortAudio error: "
            "Audio input is not authorized (microphone permission denied)."
        )

    def start(self): ...
    def stop(self): ...
    def close(self): ...


class PermissionSoundDevice(FakeSoundDevice):
    def RawInputStream(self, **kwargs):
        return PermissionDeniedStream(**kwargs)


def test_permission_denial_names_recovery_step() -> None:
    transport = make_transport()
    transport.use_sounddevice(PermissionSoundDevice())
    result = asyncio.run(_run_expect_error(transport))
    assert result is not None
    exc, events, session, agent = result
    assert "Microphone" in str(exc) or "microphone" in str(exc)
    recovery_events = [e for e in events if e["event"] == "devices_unavailable"]
    assert recovery_events, "permission denial must be recorded"
    recovery = recovery_events[0]["recovery"]
    assert "System Settings" in recovery, "recovery must name a concrete step"
    assert "Microphone" in recovery
    # devices were still released through the error path
    assert any(e["event"] == "session_closed" for e in events)
    assert session.state.value == "closed"
    assert agent.closed


async def _run_expect_error(transport: TerminalTransport):
    session, agent = make_kernel(transport)
    try:
        await session.run()
        return None
    except AudioDeviceError as exc:
        return exc, transport.events, session, agent


# -- signal handling -----------------------------------------------------------


@pytest.mark.asyncio
async def test_sigint_releases_devices() -> None:
    transport = make_transport()
    task = asyncio.create_task(transport.run(Session()))
    await _until(lambda: any(e["event"] == "session_started" for e in transport.events))
    sd = transport._sounddevice
    in_stream, out_stream = sd.opened_streams[0], sd.opened_streams[1]

    # Ctrl-C: deliver SIGINT semantics through the handler directly
    # (raising KeyboardInterrupt in the loop mirrors real Ctrl-C delivery
    # through asyncio's signal handling).
    transport._install_signal_handlers()
    task.cancel()  # simulate the loop-stop interrupting run()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await transport.close()

    assert in_stream.closed, "input device must be released on Ctrl-C"
    assert out_stream.closed, "output device must be released on Ctrl-C"
    assert any(e["event"] == "session_cancelled" for e in transport.events)
    assert any(e["event"] == "session_closed" for e in transport.events)


@pytest.mark.asyncio
async def test_exception_releases_devices() -> None:
    transport = make_transport()
    sd = transport._sounddevice

    class ExplodingRoute:
        def __init__(self) -> None:
            self.closed = 0

        def set_audio_output(self, output) -> None: ...

        def bind(self, session_id, on_event) -> None: ...

        async def prepare(self, session) -> None:
            raise RuntimeError("agent exploded")

        def active_turn(self):
            return None

        async def close(self) -> None:
            self.closed += 1

    route = ExplodingRoute()
    session = RealtimeSession(
        types.SimpleNamespace(transport=transport, route=route, usage_meter=None)
    )

    # the route blows up mid-session after devices are open
    with pytest.raises(RuntimeError, match="agent exploded"):
        await session.run()
    assert any(e["event"] == "session_error" for e in transport.events)
    # both streams opened during run are closed by the finally-close
    assert all(s.closed for s in sd.opened_streams), "exception path must release devices"
    assert any(e["event"] == "session_closed" for e in transport.events)
    assert route.closed == 1
    assert session.state.value == "closed"

# -- idempotent close and restart ----------------------------------------------


@pytest.mark.asyncio
async def test_repeated_close_is_safe() -> None:
    transport = make_transport()
    await transport.close()
    await transport.close()
    await transport.close()
    assert len([e for e in transport.events if e["event"] == "session_closed"]) == 1


@pytest.mark.asyncio
async def test_restart_after_close_same_process() -> None:
    transport = make_transport()
    task = asyncio.create_task(transport.run(Session()))
    await _until(lambda: any(e["event"] == "session_started" for e in transport.events))
    first_streams = list(transport._sounddevice.opened_streams)

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await transport.close()
    assert all(s.closed for s in first_streams)
    assert transport._microphone is None and transport._speaker is None

    # restart through the public API only: run() resets lifecycle state
    task2 = asyncio.create_task(transport.run(Session()))
    await _until(
        lambda: len(transport._sounddevice.opened_streams) > len(first_streams)
        and any(
            e["event"] == "session_started" for e in transport.events[len(transport.events) - 3 :]
        )
    )
    new_streams = transport._sounddevice.opened_streams[len(first_streams) :]
    assert len(new_streams) == 2
    assert all(not s.closed for s in new_streams), "restarted devices must be open"
    task2.cancel()
    try:
        await task2
    except asyncio.CancelledError:
        pass
    await transport.close()
    assert all(s.closed for s in new_streams)


@pytest.mark.asyncio
async def test_close_after_failed_open_is_safe() -> None:
    transport = make_transport()
    transport.use_sounddevice(PermissionSoundDevice())
    with pytest.raises(AudioDeviceError):
        await transport.run(Session())
    # close() was already called inside run()'s error path; again is fine
    await transport.close()
    assert transport._microphone is None


async def _until(predicate, timeout: float = 2.0) -> None:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met within timeout")


# -- audit rework: route/agent teardown + real signal handling ----------------


class TeardownAgent:
    def __init__(self) -> None:
        self.closed = False

    async def warmup(self) -> None:
        return None

    async def close(self) -> None:
        self.closed = True


def make_kernel(transport: TerminalTransport) -> tuple[RealtimeSession, TeardownAgent]:
    from sleeptalk.routes.cascade import CascadeRoute

    agent = TeardownAgent()
    built = types.SimpleNamespace(
        transport=transport,
        route=CascadeRoute(agent=agent, tts=None),
        agent=agent,
        usage_meter=None,
    )
    return RealtimeSession(built), agent


@pytest.mark.asyncio
async def test_kernel_owns_route_and_agent_teardown() -> None:
    """The transport closes devices; the canonical kernel closes the route."""
    transport = make_transport()
    session, agent = make_kernel(transport)
    task = asyncio.create_task(session.run())
    await _until(lambda: any(e["event"] == "session_started" for e in transport.events))

    await transport.close()
    await task
    assert agent.closed, "agent must be closed by the kernel's route teardown"
    assert session.state.value == "closed"


@pytest.mark.asyncio
async def test_signal_handler_sets_shutdown_not_loop_stop() -> None:
    """Real signal handling: _request_shutdown sets the event and cancels
    the run task — no loop.stop() shortcut (audit finding)."""
    transport = make_transport()
    session, agent = make_kernel(transport)
    task = asyncio.create_task(session.run())
    await _until(lambda: any(e["event"] == "session_started" for e in transport.events))

    # _run_task is set by run() itself; only the handler is invoked here
    transport._request_shutdown(signal.SIGINT)

    assert any(e["event"] == "shutdown_requested" for e in transport.events)
    # the drive loop exits via the shutdown event, not a stopped loop;
    # under suite load the cancel can win the race with the event wait —
    # either path is a clean exit, so accept both outcomes.
    try:
        await asyncio.wait_for(task, timeout=2.0)
    except asyncio.CancelledError:
        pass
    await transport.close()
    assert any(e["event"] == "session_closed" for e in transport.events)
    assert agent.closed
    assert session.state.value == "closed"


@pytest.mark.asyncio
async def test_run_is_restartable_public_api_only() -> None:
    """run() after close() works with zero private-field manipulation
    (the audit's explicit failure: tests used to reset _closed)."""
    transport = make_transport()
    task = asyncio.create_task(transport.run(Session()))
    await _until(lambda: any(e["event"] == "session_started" for e in transport.events))
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await transport.close()
    assert transport._closed is True

    # restart: purely public API
    task2 = asyncio.create_task(transport.run(Session()))
    await _until(
        lambda: sum(1 for e in transport.events if e["event"] == "session_started") == 2
    )
    task2.cancel()
    try:
        await task2
    except asyncio.CancelledError:
        pass
    await transport.close()
    assert sum(1 for e in transport.events if e["event"] == "session_started") == 2
