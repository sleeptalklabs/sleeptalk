"""One latency log, every surface in it.

Terminal used to keep its timings in an in-memory event list, so it was
the one surface that could not be compared with web or Discord. These
tests pin the two halves of the fix: terminal emits the shared trace
envelope, and a run points every surface at the same file without anyone
exporting an environment variable by hand.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from sleeptalk.config import (
    AgentConfig,
    CascadeRouteConfig,
    LogConfig,
    PolicyConfig,
    STTConfig,
    SleeptalkConfig,
    TTSConfig,
    TransportConfig,
)
from sleeptalk.logging_setup import LATENCY_LOG_ENV, setup_latency_log
from sleeptalk.session_events import SessionEventType, Speaker, response, transcript
from sleeptalk.transports.terminal import TerminalTransport
from sleeptalk.voice_loop import VoiceLatencyTrace, cascade_turn_identity
from sleeptalk.wizard import WizardAnswers, load_existing_answers, profile_toml


def _transport(*, latency: bool = True) -> TerminalTransport:
    config = SleeptalkConfig(
        agent=AgentConfig(type="claude_code", name="m4rin", slug="sleeptalk"),
        route=CascadeRouteConfig(),
        transport=TransportConfig(type="terminal"),
        log=LogConfig(latency=latency),
    )
    return TerminalTransport(config, {})


def _traces(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


@pytest.fixture
def latency_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "voice_latency.jsonl"
    monkeypatch.setenv(LATENCY_LOG_ENV, str(path))
    return path


# -- the destination ---------------------------------------------------------


def test_a_run_sends_every_surface_to_one_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(LATENCY_LOG_ENV, raising=False)

    path = setup_latency_log(logs_dir=tmp_path / "logs")

    assert path == tmp_path / "logs" / "voice_latency.jsonl"
    assert Path(os.environ[LATENCY_LOG_ENV]) == path


def test_turning_it_off_writes_nowhere(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(LATENCY_LOG_ENV, raising=False)

    assert setup_latency_log(logs_dir=tmp_path, enabled=False) is None
    assert LATENCY_LOG_ENV not in os.environ


def test_a_one_off_run_can_still_redirect_its_own_traces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(LATENCY_LOG_ENV, str(tmp_path / "mine.jsonl"))

    assert setup_latency_log(logs_dir=tmp_path / "logs") == tmp_path / "mine.jsonl"


def test_the_profile_can_name_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(LATENCY_LOG_ENV, raising=False)

    path = setup_latency_log(
        logs_dir=tmp_path / "logs", configured_path=str(tmp_path / "shared.jsonl")
    )

    assert path == tmp_path / "shared.jsonl"


# -- terminal now emits the shared envelope ----------------------------------


def test_a_terminal_turn_is_timed_end_to_end(latency_log: Path) -> None:
    transport = _transport()
    transport._utterance = 1
    transport._start_latency_turn(audioMs=900, voicedMs=700)
    transport._on_session_event(
        transcript("s1", 1, speaker=Speaker.USER, text="hello", final=True)
    )
    transport._on_session_event(
        response("s1", 1, SessionEventType.RESPONSE_STARTED)
    )
    transport._mark_latency("playback_started", once=True, turn_id=1)
    transport._on_session_event(
        response("s1", 1, SessionEventType.RESPONSE_COMPLETED)
    )

    stages = [event["stage"] for event in _traces(latency_log)]
    assert stages == [
        "utterance_ready",
        "stt_completed",
        "agent_started",
        "playback_started",
        "turn_complete",
    ]


def test_terminal_traces_carry_the_fields_the_comparison_groups_by(
    latency_log: Path,
) -> None:
    transport = _transport()
    transport._start_latency_turn(audioMs=900, voicedMs=700)

    first = _traces(latency_log)[0]
    assert first["surface"] == "sleeptalk-terminal"
    assert first["transport"] == "terminal"
    assert first["persona"] == "sleeptalk"
    assert first["agent"] == "claude_code"
    assert first["route"] == "cascade"
    assert first["sttProvider"] == "deepgram"
    assert first["ttsProvider"] == "cartesia"
    assert first["replyLength"] == "full"
    assert first["metric"] == "voice_latency"
    assert first["traceId"]
    assert first["elapsedMs"] >= 0
    assert first["agent"] == "claude_code"


def test_one_turn_is_one_trace(latency_log: Path) -> None:
    transport = _transport()
    transport._start_latency_turn(audioMs=900, voicedMs=700)
    transport._on_session_event(response("s1", 1, SessionEventType.RESPONSE_COMPLETED))
    transport._start_latency_turn(audioMs=800, voicedMs=600)
    transport._on_session_event(response("s1", 2, SessionEventType.RESPONSE_COMPLETED))

    ids = {event["traceId"] for event in _traces(latency_log)}
    assert len(ids) == 2


def test_the_device_reports_first_audio_once_however_many_chunks_arrive(
    latency_log: Path,
) -> None:
    transport = _transport()
    transport._start_latency_turn(audioMs=900, voicedMs=700)
    transport._mark_latency("playback_started", once=True, turn_id=1)
    transport._mark_latency("playback_started", once=True, turn_id=1)

    stages = [event["stage"] for event in _traces(latency_log)]
    assert stages.count("playback_started") == 1


def test_an_interrupted_turn_closes_its_trace(latency_log: Path) -> None:
    transport = _transport()
    transport._start_latency_turn(audioMs=900, voicedMs=700)
    transport._on_session_event(response("s1", 1, SessionEventType.RESPONSE_CANCELLED))
    transport._mark_latency("playback_started")

    stages = [event["stage"] for event in _traces(latency_log)]
    assert stages == ["utterance_ready", "turn_cancelled"]


def test_turning_it_off_leaves_the_turn_untraced(latency_log: Path) -> None:
    transport = _transport(latency=False)
    transport._start_latency_turn(audioMs=900, voicedMs=700)
    transport._on_session_event(response("s1", 1, SessionEventType.RESPONSE_COMPLETED))

    assert _traces(latency_log) == []


def test_a_broken_trace_never_breaks_the_turn(latency_log: Path) -> None:
    transport = _transport()
    transport._start_latency_turn(audioMs=900, voicedMs=700)

    class Exploding:
        def mark(self, *_args, **_kwargs):
            raise RuntimeError("disk gone")

        def finish(self, *_args, **_kwargs):
            raise RuntimeError("disk gone")

    transport._latency = Exploding()
    transport._mark_latency("playback_started")
    transport._finish_latency()

    assert transport._latency is None


# -- the wizard owns the toggle ----------------------------------------------


def test_new_profiles_explicitly_disable_performance_traces() -> None:
    text = profile_toml(WizardAnswers(slug="sleeptalk", surface="terminal"))

    assert "[log]" in text
    assert "latency = false" in text


def test_turning_it_off_in_the_wizard_survives_a_reopen(tmp_path: Path) -> None:
    answers = WizardAnswers(slug="sleeptalk", surface="terminal", latency_log=False)
    (tmp_path / "sleeptalk.toml").write_text(profile_toml(answers), encoding="utf-8")

    reloaded = load_existing_answers("sleeptalk", profiles_dir=tmp_path)

    assert reloaded is not None
    assert reloaded.latency_log is False


def test_a_profile_without_a_trace_preference_does_not_record() -> None:
    assert SleeptalkConfig().log.latency is False


def test_cascade_turn_identity_includes_present_fields() -> None:
    config = SleeptalkConfig(
        agent=AgentConfig(type="pi", name="deepre", model="opus"),
        route=CascadeRouteConfig(),
        stt=STTConfig(provider="deepgram", model="nova-3"),
        tts=TTSConfig(provider="cartesia", model="sonic", voice_id="voice-1"),
        policy=PolicyConfig(reply_length="summarized"),
    )

    identity = cascade_turn_identity(config, session_id="sess-1")

    assert identity["agent"] == "pi"
    assert identity["agentName"] == "deepre"
    assert identity["requestedAgentModel"] == "opus"
    assert "agentModel" not in identity
    assert identity["route"] == "cascade"
    assert identity["sttProvider"] == "deepgram"
    assert identity["sttModel"] == "nova-3"
    assert identity["ttsProvider"] == "cartesia"
    assert identity["ttsModel"] == "sonic"
    assert identity["ttsVoiceId"] == "voice-1"
    assert identity["replyLength"] == "summarized"
    assert identity["sessionId"] == "sess-1"


def test_cascade_turn_identity_omits_empty_model() -> None:
    identity = cascade_turn_identity(
        SimpleNamespace(
            agent=SimpleNamespace(type="pi", name="deepre", model=""),
            route=SimpleNamespace(type="cascade"),
            stt=SimpleNamespace(provider="deepgram", model="nova-3"),
            tts=SimpleNamespace(provider="cartesia", model="", voice_id=""),
            policy=SimpleNamespace(reply_length="full"),
        )
    )

    assert "agentModel" not in identity
    assert "ttsModel" not in identity
    assert "ttsVoiceId" not in identity
    assert identity["agent"] == "pi"
    assert identity["replyLength"] == "full"


def test_mark_once_writes_the_first_time_and_not_the_second(latency_log: Path) -> None:
    trace = VoiceLatencyTrace(surface="t", transport="t", persona="t")

    first = trace.mark_once("agent_request_sent", provider="ScriptedAgent")
    second = trace.mark_once("agent_request_sent", provider="Other")

    assert first is not None
    assert first["stage"] == "agent_request_sent"
    assert second is None
    assert [event["stage"] for event in _traces(latency_log)].count(
        "agent_request_sent"
    ) == 1


# -- the traces have to survive the checker that reads them -------------------


def test_a_healthy_terminal_turn_passes_the_health_invariants(
    latency_log: Path,
) -> None:
    """Audio-first turns used to look like an agent that never spoke.

    The health checker reads a turn with an agent request and no first
    token as a stall, and terminal only ever produced audio.
    """
    from sleeptalk.evals.session_health import analyze_session_health

    transport = _transport()
    transport._start_latency_turn(audioMs=900, voicedMs=700)
    transport._on_session_event(
        transcript("s1", 1, speaker=Speaker.USER, text="hello", final=True)
    )
    transport._on_session_event(response("s1", 1, SessionEventType.RESPONSE_STARTED))
    transport._mark_latency("agent_first_speakable_segment", once=True, textChars=5)
    transport._on_session_event(
        response("s1", 1, SessionEventType.RESPONSE_FIRST_OUTPUT, output_kind="audio")
    )
    transport._mark_latency("answer_first_output_enqueue", once=True, turn_id=1)
    transport._mark_latency("playback_started", once=True, turn_id=1)
    transport._on_session_event(response("s1", 1, SessionEventType.RESPONSE_COMPLETED))

    result = analyze_session_health(_traces(latency_log))

    assert result.traces == 1
    assert [violation.invariant_id for violation in result.violations] == []
