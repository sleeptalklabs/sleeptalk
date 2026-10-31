from __future__ import annotations

import os

import pytest


@pytest.fixture
def dummy_cli_bins(tmp_path_factory, monkeypatch):
    """Stub `pi` and `hermes` so spawn checks pass without a host install."""
    bin_dir = tmp_path_factory.mktemp("cli-bins")
    for name in ("pi", "hermes"):
        path = bin_dir / name
        path.write_text("#!/bin/sh\nexit 1\n")
        path.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    return bin_dir


@pytest.fixture(autouse=True)
def _restore_process_env_after_each_test():
    """Keep process env stable across tests that mutate os.environ."""
    snapshot = os.environ.copy()
    yield
    os.environ.clear()
    os.environ.update(snapshot)


@pytest.fixture(autouse=True, scope="session")
def _redirect_voice_logs_away_from_repo(tmp_path_factory):
    """Keep test fixtures out of the real logs/ directory.

    Tests used to build agents from leftover AppSettings defaults, which
    resolved every voice log to the repo's logs/*.jsonl. Keep that isolation
    so a pytest run does not mix fixture replies into operator logs.
    """
    log_dir = tmp_path_factory.mktemp("voice-logs")
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("SLEEPTALK_TRANSCRIPT_DEBUG", "false")
    monkeypatch.setenv(
        "SLEEPTALK_TRANSCRIPT_DEBUG_PATH",
        str(log_dir / "transcript_debug.jsonl"),
    )
    for variable in (
        "VOICE_CHAT_LOG_PATH",
        "VOICE_LATENCY_LOG_PATH",
        "VOICE_SEARCH_LOG_PATH",
        "VOICE_TURN_LOG_PATH",
        "CAPABILITY_GAP_LOG_PATH",
    ):
        monkeypatch.setenv(variable, str(log_dir / f"{variable.lower()}.jsonl"))
    yield
    monkeypatch.undo()
