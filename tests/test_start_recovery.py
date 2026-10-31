"""Stable start requests survive lost replies without creating another call."""

import asyncio
import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from sleeptalk import call_registry as registry, detached_call, web_call
from sleeptalk.call_provider import CallContext
from sleeptalk.voice_loop.telemetry import VoiceLatencyTrace
from sleeptalk.web_voice.session import BrowserTransportState


@pytest.fixture
def attempt(tmp_path):
    (tmp_path / "voice.toml").write_text('[agent]\ntype = "pi"\n')
    request = detached_call.DetachedCallRequest(
        rendezvous_url="https://example.invalid", context=CallContext("agent", "", "user"),
        ttl_seconds=300, config_path=str(tmp_path / "voice.toml"), request_id="request-0001",
    )
    return request, tmp_path / "calls"


@contextmanager
def live_attempt(request, root):
    identity = request.identity()
    lease = registry.acquire_profile_lease(root=root, identity=identity)
    try:
        registry.reserve_start_request(root=root, identity=identity, request_id=request.request_id,
                                       fingerprint=request.fingerprint(), ttl_seconds=300)
        with registry.register_live_call(root=root, identity=identity, call_id=request.call_id):
            yield
    finally:
        lease.close()


def observe(request, root, stage, **kwargs):
    registry.observe_start_request(root=root, identity=request.identity(), request_id=request.request_id,
                                   stage=stage, **kwargs)


def status(request, root):
    return registry.get_start_request(root=root, identity=request.identity(), request_id=request.request_id)


def test_lost_response_recovers_same_url_call_and_expiry(attempt, monkeypatch):
    request, root = attempt
    with live_attempt(request, root):
        observe(request, root, "link_minted", join_url="https://example.invalid/#private")
        before = status(request, root)
        monkeypatch.setattr(detached_call.subprocess, "Popen", lambda *a, **kw: pytest.fail("duplicate spawn"))
        retry = detached_call.DetachedCallRequest(
            rendezvous_url=request.rendezvous_url, context=request.context, ttl_seconds=300,
            config_path=request.config_path, request_id=request.request_id,
        )
        assert retry.call_id == request.call_id
        assert detached_call.launch_detached_call(retry) == "https://example.invalid/#private"
        assert status(request, root) == before
        public = json.dumps(before) + json.dumps([r.call_id for r in registry.list_live_calls(root=root)])
        assert "private" not in public
        capability = next(root.glob("*/requests/*.capability"))
        assert capability.stat().st_mode & 0o777 == 0o600
        assert capability.parent.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize("stage,expected", [("ended", "ended"), ("failed", "failed"), ("link_consumed", "active")])
def test_replay_never_relaunches_finished_or_consumed_attempt(attempt, monkeypatch, stage, expected):
    request, root = attempt
    with live_attempt(request, root):
        observe(request, root, "link_minted", join_url="https://example.invalid/#private")
        observe(request, root, stage)
        monkeypatch.setattr(detached_call.subprocess, "Popen", lambda *a, **kw: pytest.fail("duplicate spawn"))
        with pytest.raises(detached_call.DetachedCallReplay) as error:
            detached_call.launch_detached_call(request)
        assert error.value.progress["state"] == expected
        assert not list(root.glob("*/requests/*.capability"))


def test_expired_request_does_not_mint_again(attempt, monkeypatch):
    request, root = attempt
    with live_attempt(request, root):
        observe(request, root, "link_minted", join_url="https://example.invalid/#private")
        expiry = status(request, root)["expires_at"]
        monkeypatch.setattr(registry.time, "time", lambda: expiry + 1)
        with pytest.raises(detached_call.DetachedCallReplay) as error:
            detached_call.launch_detached_call(request)
        assert error.value.progress["state"] == "expired"
        assert not list(root.glob("*/requests/*.capability"))


def test_consumed_live_call_outlives_join_expiry(attempt, monkeypatch):
    request, root = attempt
    with live_attempt(request, root):
        observe(request, root, "link_minted", join_url="https://example.invalid/#private")
        observe(request, root, "link_consumed")
        expiry = status(request, root)["expires_at"]
        monkeypatch.setattr(registry.time, "time", lambda: expiry + 1)
        assert status(request, root)["state"] == "active"


def test_crash_before_spawn_is_terminal_and_replay_does_not_spawn(attempt):
    request, root = attempt
    registry.reserve_start_request(root=root, identity=request.identity(), request_id=request.request_id,
                                   fingerprint=request.fingerprint(), ttl_seconds=300)
    with pytest.raises(detached_call.DetachedCallReplay) as error:
        detached_call.launch_detached_call(request)
    assert error.value.progress["state"] == "failed"


def test_starting_request_is_busy_under_existing_lease(attempt):
    request, root = attempt
    lease = registry.acquire_profile_lease(root=root, identity=request.identity())
    try:
        registry.reserve_start_request(root=root, identity=request.identity(), request_id=request.request_id,
                                       fingerprint=request.fingerprint(), ttl_seconds=300)
        with pytest.raises(detached_call.DetachedCallReplay) as error:
            detached_call.launch_detached_call(request)
        assert error.value.progress["state"] == "starting"
    finally:
        lease.close()


def test_dead_call_is_terminal_even_while_a_new_call_owns_profile(attempt):
    request, root = attempt
    with live_attempt(request, root):
        observe(request, root, "link_minted", join_url="https://example.invalid/#private")
    lease = registry.acquire_profile_lease(root=root, identity=request.identity())
    try:
        assert status(request, root)["state"] == "ended"
        assert not list(root.glob("*/requests/*.capability"))
    finally:
        lease.close()


def test_changed_start_options_are_rejected(attempt):
    request, root = attempt
    with live_attempt(request, root):
        changed = detached_call.DetachedCallRequest(
            rendezvous_url=request.rendezvous_url, context=request.context, ttl_seconds=30,
            config_path=request.config_path, request_id=request.request_id,
        )
        with pytest.raises(registry.CallRegistryError, match="different call options"):
            detached_call.launch_detached_call(changed)


def test_corrupt_request_fails_closed(attempt):
    request, root = attempt
    with live_attempt(request, root):
        record = next(root.glob("*/requests/*.json"))
        record.write_text("not-json")
        with pytest.raises(registry.CallRegistryError, match="could not be read"):
            detached_call.launch_detached_call(request)


async def test_browser_observations_do_not_claim_a_person_heard_audio():
    events = iter([
        {"stage": "ticket_redeemed", "secret": "never-persist"},
        {"stage": "talk_tapped"}, {"stage": "microphone_ready"},
        {"stage": "peer_connected"}, {"stage": "remote_audio_playing"}, None,
    ])
    async def recv():
        return next(events)
    observed = []
    await web_call._record_browser_telemetry(
        SimpleNamespace(recv_telemetry=recv), VoiceLatencyTrace(surface="test", transport="test", persona="test"),
        BrowserTransportState(), observed.append,
    )
    assert observed == ["link_consumed", "waiting_for_permission", "microphone_ready", "browser_connected", "media_playing"]
    assert "response_played" not in observed


async def test_authoritative_redemption_clears_capability_before_mic(attempt):
    from sleeptalk.call_provider import _WebRTCAudioStream

    request, root = attempt
    with live_attempt(request, root):
        observe(request, root, "link_minted", join_url="https://example.invalid/#private")
        stream = _WebRTCAudioStream("wss://example.invalid", (), role="agent", access_key="test")
        stream._websocket = object()
        await stream._handle_signal({"type": "ticket.redeemed", "payload": {}})
        await stream._handle_signal({"type": "ticket.redeemed", "payload": {}})
        stream._close_telemetry()
        await web_call._record_browser_telemetry(
            stream, VoiceLatencyTrace(surface="test", transport="test", persona="test"),
            BrowserTransportState(), lambda stage: observe(request, root, stage),
        )
        progress = status(request, root)
        assert "link_consumed" in progress["observations"]
        assert "microphone_ready" not in progress["observations"]
        assert not list(root.glob("*/requests/*.capability"))
        await stream.peer.close()


@pytest.mark.parametrize("signal_types,unavailable", [(frozenset(), True), (frozenset({"ticket.redeemed"}), False)])
async def test_progress_identifies_old_service_without_redemption_notifications(signal_types, unavailable):
    from sleeptalk.config import SleeptalkConfig

    async def noop():
        pass
    async def mint(*args, **kwargs):
        return SimpleNamespace(token="fixture", join_url="https://example.invalid", signal_types=signal_types)
    def stop_after_ready(url):
        raise RuntimeError("test finished")
    observed = []
    with pytest.raises(RuntimeError, match="test finished"):
        await web_call.run_web_session(
            runtime=SimpleNamespace(prepare=noop, aclose=noop), config=SleeptalkConfig(),
            context=CallContext("agent", "", "user"), ttl_seconds=300,
            on_ready=stop_after_ready, provider=SimpleNamespace(mint=mint), on_progress=observed.append,
        )
    assert ("redemption_notifications_unavailable" in observed) is unavailable



def test_listing_after_abrupt_child_death_removes_private_recovery_url(attempt):
    import subprocess
    import sys

    request, root = attempt
    script = """
import signal
import sys
from pathlib import Path
from sleeptalk.call_registry import (
    ProfileIdentity, acquire_profile_lease, reserve_start_request,
    register_live_call, observe_start_request, request_call_id,
)
root = Path(sys.argv[1])
identity = ProfileIdentity.from_config(config_path=sys.argv[2], profile="voice")
lease = acquire_profile_lease(root=root, identity=identity)
reserve_start_request(root=root, identity=identity, request_id="request-0001", fingerprint="test", ttl_seconds=300)
with register_live_call(root=root, identity=identity, call_id=request_call_id("request-0001", identity)):
    observe_start_request(root=root, identity=identity, request_id="request-0001", stage="link_minted", join_url="https://example.invalid/#private")
    print("ready", flush=True)
    signal.pause()
"""
    child = subprocess.Popen([sys.executable, "-c", script, str(root), request.config_path],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "ready"
        assert list(root.glob("*/requests/*.capability"))
        child.kill()
        child.wait(timeout=2)
        assert registry.list_live_calls(root=root) == []
        assert not list(root.glob("*/requests/*.capability"))
        assert status(request, root)["state"] == "ended"
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=2)



def test_fingerprint_includes_resolved_shared_profile_configuration(tmp_path):
    profile = tmp_path / "profiles" / "voice.toml"
    profile.parent.mkdir()
    profile.write_text('[agent]\nslug = "voice"\n')
    shared = tmp_path / "sleeptalk.toml"
    shared.write_text('[agent]\ntype = "pi"\nmodel = "before"\n')
    def request():
        return detached_call.DetachedCallRequest(
            rendezvous_url="https://example.invalid", context=CallContext("voice", "", "user"),
            ttl_seconds=300, config_path=str(profile), request_id="snapshot-request",
        )
    first = request()
    shared.write_text('[agent]\ntype = "pi"\nmodel = "after"\n')
    second = request()
    assert first.call_id == second.call_id
    assert first.fingerprint() != second.fingerprint()
    shared.write_text('# comment only\n[agent]\nmodel = "after"\ntype = "pi"\n')
    assert request().fingerprint() == second.fingerprint()
    assert first.fingerprint() != request().fingerprint()
