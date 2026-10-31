"""Opt-in debug probes write transcripts, so their files are owner-only."""

from __future__ import annotations

import json

from sleeptalk import session_event_log
from sleeptalk.voice_loop import telemetry


def test_transcript_debug_defaults_to_the_private_logs_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("SLEEPTALK_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SLEEPTALK_TRANSCRIPT_DEBUG", "1")
    monkeypatch.delenv("SLEEPTALK_TRANSCRIPT_DEBUG_PATH", raising=False)
    telemetry.log_chat_turn(surface="web", transport="t", persona="p",
                            user_text="private words", assistant_text="reply")
    path = tmp_path / "home" / "logs" / "transcript_debug.jsonl"
    assert json.loads(path.read_text())["userText"] == "private words"
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert oct(path.parent.stat().st_mode & 0o777) == "0o700"


def test_probe_log_is_owner_only_and_refuses_a_symlink(tmp_path, monkeypatch):
    path = tmp_path / "events.jsonl"
    monkeypatch.setenv(session_event_log.ENV, str(path))
    session_event_log.append_probe_record({"type": "probe", "text": "private words"})
    assert oct(path.stat().st_mode & 0o777) == "0o600"

    elsewhere = tmp_path / "elsewhere.jsonl"
    link = tmp_path / "link.jsonl"
    link.symlink_to(elsewhere)
    monkeypatch.setenv(session_event_log.ENV, str(link))
    session_event_log.append_probe_record({"type": "probe", "text": "private words"})
    assert not elsewhere.exists()
