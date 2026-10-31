"""The background host behind an agent's call link keeps the same logs as `sleeptalk run`."""
from __future__ import annotations

import argparse
import logging
import shutil
from pathlib import Path

from sleeptalk import detached_call
from sleeptalk.logging_setup import LATENCY_LOG_ENV, profile_logs_dir


def test_call_host_writes_the_turn_timeline_beside_the_profile(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "profiles").mkdir(parents=True)
    shutil.copy(Path(__file__).parents[1] / "sleeptalk" / "sleeptalk.example.toml", home / "sleeptalk.toml")
    profile = home / "profiles" / "voice.toml"
    profile.write_text('[agent]\ntype = "pi"\nname = "Voice"\nslug = "voice"\n\n[transport]\ntype = "web"\n\n[log]\nlatency = true\n')
    monkeypatch.delenv(LATENCY_LOG_ENV, raising=False)
    monkeypatch.chdir(tmp_path)  # the host's own directory must not matter
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        detached_call._setup_child_logging(argparse.Namespace(config=str(profile), echo=False))
        import os

        assert os.environ[LATENCY_LOG_ENV] == str(home / "logs" / "voice_latency.jsonl")
        assert (home / "logs" / "voice.log").exists()
    finally:
        for handler in root.handlers[:]:
            if handler not in before:
                root.removeHandler(handler)
                handler.close()


def test_a_profile_that_wont_load_still_lets_the_call_report_it(tmp_path, monkeypatch):
    monkeypatch.delenv(LATENCY_LOG_ENV, raising=False)
    detached_call._setup_child_logging(argparse.Namespace(config=str(tmp_path / "missing.toml"), echo=False))
    import os

    assert LATENCY_LOG_ENV not in os.environ


def test_logs_dir_matches_run_for_home_and_repo_profiles(tmp_path):
    assert profile_logs_dir(tmp_path / "profiles" / "a.toml") == tmp_path / "logs"
    assert profile_logs_dir(tmp_path / "config" / "profiles" / "a.toml") == tmp_path / "logs"
    assert profile_logs_dir(tmp_path / "a.toml") == tmp_path / "logs"
