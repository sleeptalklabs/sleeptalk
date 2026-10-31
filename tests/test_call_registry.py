"""Detached call registry and process-lifecycle tests."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
from argparse import Namespace

import pytest

from sleeptalk import call_registry, detached_call


def test_registered_call_is_listed_then_removed(tmp_path) -> None:
    identity = call_registry.ProfileIdentity.from_config(
        config_path=str(tmp_path / "profiles" / "alice.toml"),
        profile="alice",
    )
    call_id = call_registry.new_call_id()

    with call_registry.register_live_call(
        root=tmp_path / "calls",
        identity=identity,
        call_id=call_id,
    ):
        calls = call_registry.list_live_calls(root=tmp_path / "calls")
        assert [(call.call_id, call.profile) for call in calls] == [
            (call_id, "alice")
        ]

    assert call_registry.list_live_calls(root=tmp_path / "calls") == []


def test_profile_lease_is_exclusive_and_recovers_after_close(tmp_path) -> None:
    identity = call_registry.ProfileIdentity.from_config(
        config_path=str(tmp_path / "alice.toml"),
        profile="alice",
    )
    first = call_registry.acquire_profile_lease(
        root=tmp_path / "calls", identity=identity
    )
    try:
        with pytest.raises(call_registry.CallBusyError):
            call_registry.acquire_profile_lease(
                root=tmp_path / "calls", identity=identity
            )
    finally:
        first.close()

    recovered = call_registry.acquire_profile_lease(
        root=tmp_path / "calls", identity=identity
    )
    recovered.close()


def test_explicit_config_selects_one_registry_root_across_working_directories(
    tmp_path, monkeypatch
) -> None:
    profile = tmp_path / "state" / "config" / "profiles" / "alice.toml"
    first_cwd = tmp_path / "first"
    second_cwd = tmp_path / "second"
    first_cwd.mkdir()
    second_cwd.mkdir()
    monkeypatch.delenv("SLEEPTALK_HOME", raising=False)

    monkeypatch.chdir(first_cwd)
    first = call_registry.registry_root_for_config(profile)
    monkeypatch.chdir(second_cwd)
    second = call_registry.registry_root_for_config(profile)

    assert first == second == tmp_path / "state" / "calls"


def test_inherited_profile_lease_outlives_parent_descriptor(tmp_path) -> None:
    identity = call_registry.ProfileIdentity.from_config(
        config_path=str(tmp_path / "alice.toml"),
        profile="alice",
    )
    lease = call_registry.acquire_profile_lease(
        root=tmp_path / "calls", identity=identity
    )
    script = """
import sys
import time
from sleeptalk.call_registry import hold_inherited_profile_lease

with hold_inherited_profile_lease(int(sys.argv[1])):
    print("ready", flush=True)
    time.sleep(60)
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(lease.fd)],
        pass_fds=(lease.fd,),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    lease.close()
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "ready"
        with pytest.raises(call_registry.CallBusyError):
            call_registry.acquire_profile_lease(
                root=tmp_path / "calls", identity=identity
            )
    finally:
        process.terminate()
        process.wait(timeout=2)

    recovered = call_registry.acquire_profile_lease(
        root=tmp_path / "calls", identity=identity
    )
    recovered.close()


def test_busy_detached_launch_never_spawns(tmp_path, monkeypatch) -> None:
    from sleeptalk.call_provider import CallContext

    config_path = tmp_path / "alice.toml"
    identity = call_registry.ProfileIdentity.from_config(
        config_path=str(config_path),
        profile="alice",
    )
    lease = call_registry.acquire_profile_lease(
        root=tmp_path / "calls", identity=identity
    )
    monkeypatch.setattr(
        detached_call, "default_registry_root", lambda: tmp_path / "calls"
    )
    monkeypatch.setattr(
        detached_call.subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("busy launch must not spawn"),
    )
    request = detached_call.DetachedCallRequest(
        rendezvous_url="https://example.test",
        context=CallContext("alice", "conversation", "user"),
        ttl_seconds=300,
        config_path=str(config_path),
        profile="alice",
    )
    try:
        with pytest.raises(detached_call.DetachedCallBusyError):
            detached_call.launch_detached_call(request)
    finally:
        lease.close()


def test_list_prunes_a_stale_record(tmp_path) -> None:
    profile_dir = tmp_path / "calls" / ("a" * 64)
    profile_dir.mkdir(parents=True)
    record_path = profile_dir / "stale-call.json"
    record_path.write_text(
        json.dumps(
            {
                "version": 1,
                "call_id": "stale-call",
                "profile": "alice",
                "profile_key": "a" * 64,
                "pid": 999_999,
                "process_started": "never",
                "started_at": "2026-09-10T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )

    assert call_registry.list_live_calls(root=tmp_path / "calls") == []
    assert not record_path.exists()


@pytest.mark.asyncio
async def test_detached_wrapper_turns_sigterm_into_cancellation(monkeypatch) -> None:
    started = asyncio.Event()
    cleaned = asyncio.Event()
    handlers: dict[signal.Signals, tuple[object, tuple[object, ...]]] = {}
    loop = asyncio.get_running_loop()

    async def fake_child(_args: Namespace) -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    def capture_handler(sig, callback, *args) -> None:
        handlers[sig] = (callback, args)

    monkeypatch.setattr(detached_call, "_run_registered_child", fake_child)
    monkeypatch.setattr(loop, "add_signal_handler", capture_handler)
    monkeypatch.setattr(loop, "remove_signal_handler", lambda _sig: None)

    task = asyncio.create_task(detached_call._run_child_supervised(Namespace()))
    await started.wait()
    callback, args = handlers[signal.SIGTERM]
    callback(*args)
    await task

    assert cleaned.is_set()


def test_stop_sends_sigterm_and_returns_after_process_exits(tmp_path, monkeypatch) -> None:
    identity = call_registry.ProfileIdentity.from_config(
        config_path=str(tmp_path / "alice.toml"),
        profile="alice",
    )
    call_id = call_registry.new_call_id()
    signals: list[signal.Signals] = []

    with call_registry.register_live_call(
        root=tmp_path / "calls",
        identity=identity,
        call_id=call_id,
    ) as record:
        monkeypatch.setattr(
            call_registry,
            "_process_matches",
            lambda candidate: candidate == record and not signals,
        )

        def fake_kill(pid: int, sig: signal.Signals) -> None:
            assert pid == os.getpid()
            signals.append(sig)

        monkeypatch.setattr(call_registry.os, "kill", fake_kill)
        outcome = call_registry.stop_call(
            call_id,
            root=tmp_path / "calls",
            timeout_seconds=0.1,
        )

    assert signals == [signal.SIGTERM]
    assert outcome.forced is False
    assert outcome.record.call_id == call_id


def test_stop_escalates_to_sigkill_after_timeout(tmp_path, monkeypatch) -> None:
    identity = call_registry.ProfileIdentity.from_config(
        config_path=str(tmp_path / "alice.toml"),
        profile="alice",
    )
    call_id = call_registry.new_call_id()
    signals: list[signal.Signals] = []
    cleared: list[bool] = []
    monkeypatch.setattr("sleeptalk.tailnet.clear_stale_after_kill", lambda: cleared.append(True))

    with call_registry.register_live_call(
        root=tmp_path / "calls",
        identity=identity,
        call_id=call_id,
    ) as record:
        monkeypatch.setattr(
            call_registry,
            "_process_matches",
            lambda candidate: candidate == record and signal.SIGKILL not in signals,
        )
        monkeypatch.setattr(
            call_registry.os,
            "kill",
            lambda pid, sig: signals.append(sig),
        )
        outcome = call_registry.stop_call(
            call_id,
            root=tmp_path / "calls",
            timeout_seconds=0,
        )

    assert signals == [signal.SIGTERM, signal.SIGKILL]
    assert outcome.forced is True
    assert cleared == [True], "a killed call's tailnet entry is cleaned up"


def test_stop_rejects_invalid_or_unknown_call_id(tmp_path) -> None:
    with pytest.raises(call_registry.CallNotFoundError):
        call_registry.stop_call("../../pid", root=tmp_path / "calls")
    with pytest.raises(call_registry.CallNotFoundError):
        call_registry.stop_call("unknown-call", root=tmp_path / "calls")


def test_stop_terminates_a_real_registered_process(tmp_path) -> None:
    call_id = call_registry.new_call_id()
    registry_root = tmp_path / "calls"
    config_path = tmp_path / "alice.toml"
    script = """
import signal
import sys
from pathlib import Path
from sleeptalk.call_registry import ProfileIdentity, register_live_call

def stop(*_args):
    raise SystemExit(0)

signal.signal(signal.SIGTERM, stop)
identity = ProfileIdentity.from_config(config_path=sys.argv[3], profile=sys.argv[2])
with register_live_call(root=Path(sys.argv[1]), identity=identity, call_id=sys.argv[4]):
    print("ready", flush=True)
    signal.pause()
"""
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            script,
            str(registry_root),
            "alice",
            str(config_path),
            call_id,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "ready"
        outcome = call_registry.stop_call(
            call_id,
            root=registry_root,
            timeout_seconds=1.0,
        )
        assert process.wait(timeout=2) == 0
        assert outcome.forced is False
        assert call_registry.list_live_calls(root=registry_root) == []
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=2)


def test_detached_host_log_is_private_to_the_registry(tmp_path, monkeypatch) -> None:
    from types import SimpleNamespace

    from sleeptalk.call_provider import CallContext

    config_path = tmp_path / "alice.toml"
    monkeypatch.setattr(detached_call, "registry_root_for_config", lambda _path: tmp_path / "calls")
    logs = []

    def fake_popen(_command, *, stdout, **_kwargs):
        logs.append(stdout.name)
        return SimpleNamespace(poll=lambda: 1, terminate=lambda: None)

    monkeypatch.setattr(detached_call.subprocess, "Popen", fake_popen)
    request = detached_call.DetachedCallRequest(
        rendezvous_url="https://example.test",
        context=CallContext("alice", "conversation", "user"),
        ttl_seconds=300,
        config_path=str(config_path),
        profile="alice",
    )
    with pytest.raises(detached_call.DetachedCallError) as error:
        detached_call.launch_detached_call(request)
    log_path = tmp_path / "calls" / "live-link-hosts.log"
    assert str(log_path) in str(error.value)
    assert oct(log_path.stat().st_mode & 0o777) == "0o600"
