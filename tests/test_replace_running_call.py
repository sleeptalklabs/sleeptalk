"""A closed call that's still shutting down holds its profile ("already has a
live call"); the settings page offers to end it and start a new one."""

import subprocess
import sys

import pytest

from sleeptalk import call_registry, host, settings_page

HOLDER = """
import signal
import sys
from pathlib import Path
from sleeptalk.call_registry import ProfileIdentity, acquire_profile_lease, register_live_call

def stop(*_args):
    raise SystemExit(0)

signal.signal(signal.SIGTERM, stop)
identity = ProfileIdentity.from_config(config_path=sys.argv[2], profile="alice")
lease = acquire_profile_lease(root=Path(sys.argv[1]), identity=identity)
with register_live_call(root=Path(sys.argv[1]), identity=identity, call_id=sys.argv[3]):
    print("ready", flush=True)
    signal.pause()
"""


@pytest.fixture
def profile(tmp_path, monkeypatch):
    monkeypatch.delenv("SLEEPTALK_HOME", raising=False)
    path = tmp_path / "config" / "profiles" / "alice.toml"
    path.parent.mkdir(parents=True)
    path.write_text('[transport]\ntype = "web"\n')
    return path


def hold(profile):
    """A call host that owns the profile, as one shutting down still does."""
    root = call_registry.registry_root_for_config(profile)
    process = subprocess.Popen([sys.executable, "-c", HOLDER, str(root), str(profile), call_registry.new_call_id()],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert process.stdout.readline().strip() == "ready", process.stderr.read()
    return process, root


def test_ending_the_profiles_running_call_frees_the_profile(profile):
    process, root = hold(profile)
    identity = call_registry.ProfileIdentity.from_config(config_path=str(profile), profile="alice")
    try:
        with pytest.raises(call_registry.CallBusyError, match="already has a live call"):
            call_registry.acquire_profile_lease(root=root, identity=identity)
        assert settings_page.stop_profile_calls(profile) == 1
        assert process.wait(timeout=3) == 0
        call_registry.acquire_profile_lease(root=root, identity=identity).close()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=2)


def test_another_profiles_call_is_left_running(profile, tmp_path):
    other = profile.with_name("bob.toml")
    other.write_text(profile.read_text())
    process, _root = hold(profile)
    try:
        assert settings_page.stop_profile_calls(other) == 0
        assert process.poll() is None
    finally:
        process.kill()
        process.wait(timeout=2)


def test_a_busy_profile_is_refused_with_its_own_code(profile, monkeypatch):
    monkeypatch.setattr(host, "start_voice_web", lambda **_kw: {
        "status": "error", "code": "busy", "error": "Profile 'alice' already has a live call."})
    with pytest.raises(ValueError, match="^call_running$"):
        settings_page.start_call(profile, here=True)


def test_replace_ends_the_running_call_before_starting(profile, monkeypatch):
    order = []
    monkeypatch.setattr(settings_page, "stop_profile_calls", lambda path: order.append(("stop", path)) or 1)
    monkeypatch.setattr(host, "start_voice_web", lambda **kw: order.append(("start", kw["config"])) or {
        "status": "started", "join_url": "http://127.0.0.1:1/#/call/x"})
    assert settings_page.start_call(profile, here=True, replace=True)["join_url"]
    assert order == [("stop", profile), ("start", str(profile))]
    order.clear()
    settings_page.start_call(profile, here=True)
    assert order == [("start", str(profile))]


def test_the_call_host_logs_how_long_it_held_the_profile_after_the_call(monkeypatch):
    import argparse
    import time

    from sleeptalk import detached_call, web_call
    from sleeptalk.voice_loop import telemetry

    seen = []
    monkeypatch.setattr(telemetry, "log_latency_event", lambda **event: seen.append(event))
    monkeypatch.setattr(web_call, "call_ended_at", None)
    detached_call._log_shutdown(argparse.Namespace(profile="alice"))
    assert seen == []  # no call ran
    monkeypatch.setattr(web_call, "call_ended_at", time.monotonic() - 2.5)
    detached_call._log_shutdown(argparse.Namespace(profile="alice"))
    assert seen[0]["stage"] == "call_host_exit" and 2400 <= seen[0]["shutdownMs"] <= 4000
