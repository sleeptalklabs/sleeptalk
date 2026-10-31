"""Tests for session health invariant checker."""

from __future__ import annotations

from sleeptalk.evals.session_health import analyze_session_health, render_session_health


def latency(trace_id: str, stage: str, **fields):
    return {"traceId": trace_id, "surface": "test", "stage": stage, **fields}


def session(trace_id: str, event_type: str, **fields):
    return {"traceId": trace_id, "type": event_type, **fields}


def complete_turn(trace_id: str):
    return [
        latency(trace_id, "utterance_ready"),
        latency(trace_id, "agent_request_sent"),
        latency(trace_id, "agent_first_text_delta"),
        latency(trace_id, "tts_request_sent"),
        latency(trace_id, "tts_first_audio_chunk"),
        latency(trace_id, "turn_complete", status="completed"),
    ]


def test_healthy_multi_turn_session_passes():
    events = complete_turn("t1") + complete_turn("t2")
    events.append(latency("t2", "barge_in_detected"))

    result = analyze_session_health(events, minimum_turns=2)

    assert result.passed
    assert "session health: PASS" in render_session_health(result)


def test_agent_stall_is_flagged():
    events = [
        latency("t1", "utterance_ready"),
        latency("t1", "agent_request_sent"),
        latency("t1", "turn_complete", status="completed"),
    ]

    result = analyze_session_health(events, minimum_turns=1)

    assert not result.passed
    assert any(v.invariant_id == "agent_stall_after_utterance" for v in result.violations)


def test_silent_after_agent_token_is_flagged():
    events = [
        latency("t1", "utterance_ready"),
        latency("t1", "agent_request_sent"),
        latency("t1", "agent_first_text_delta"),
        latency("t1", "turn_complete", status="completed"),
    ]

    result = analyze_session_health(events, minimum_turns=1)

    assert not result.passed
    assert any(v.invariant_id == "tts_silent_after_agent_token" for v in result.violations)


def test_silent_after_tool_completed_uses_session_events():
    events = [
        latency("t1", "utterance_ready"),
        latency("t1", "agent_request_sent"),
        latency("t1", "agent_first_text_delta"),
        latency("t1", "turn_complete", status="completed"),
    ]
    session_events = [
        session("t1", "tool.started"),
        session("t1", "tool.completed"),
    ]

    result = analyze_session_health(events, session_events, minimum_turns=1)

    assert not result.passed
    assert any(v.invariant_id == "silent_after_tool_completed" for v in result.violations)


def test_post_interrupt_recovery_requires_next_turn():
    events = complete_turn("t1") + [
        latency("t2", "utterance_ready"),
        latency("t2", "speech_interrupted"),
    ]

    result = analyze_session_health(events, minimum_turns=2)

    assert not result.passed
    assert any(
        v.invariant_id == "incomplete_post_interrupt_recovery" for v in result.violations
    )


def test_cancelled_turn_without_tts_is_allowed():
    events = [
        latency("t1", "utterance_ready"),
        latency("t1", "agent_request_sent"),
        latency("t1", "agent_first_text_delta"),
        latency("t1", "turn_complete", status="cancelled"),
    ]

    result = analyze_session_health(events, minimum_turns=1)

    assert result.passed
