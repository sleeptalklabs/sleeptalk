"""Tests for sleeptalk.session_events (r2-session-events).

Covers serialization round-trips, correlation invariants, ordering rules,
and backward compatibility with unknown future fields.
"""

import json

import pytest

from sleeptalk.session_events import (
    AttemptStatus,
    EventError,
    InterruptionPhase,
    InterruptionSource,
    PaymentMode,
    SessionEvent,
    SessionEventType,
    SessionState,
    Speaker,
    agent_event,
    error,
    interruption,
    progress,
    response,
    session_state,
    tool_event,
    transcript,
    usage_attempt_finished,
    usage_attempt_started,
    usage_summary,
    warning,
)

SID = "sess-abc123"


# -- serialization ------------------------------------------------------


def test_every_event_variant_round_trips_through_json():
    events = [
        session_state(SID, SessionState.ACTIVE),
        transcript(SID, 1, speaker=Speaker.USER, text="hello", final=True),
        transcript(SID, 2, speaker=Speaker.ASSISTANT, text="wor", final=False),
        response(SID, 1, SessionEventType.RESPONSE_STARTED),
        response(SID, 1, SessionEventType.RESPONSE_FIRST_OUTPUT, output_kind="audio"),
        response(SID, 1, SessionEventType.RESPONSE_COMPLETED),
        response(SID, 1, SessionEventType.RESPONSE_CANCELLED),
        agent_event(SID, 1, "thinking", raw={"event": "status"}),
        tool_event(SID, 1, SessionEventType.TOOL_STARTED, name="web_search"),
        tool_event(SID, 1, SessionEventType.TOOL_COMPLETED, name="web_search", call_id="c1"),
        tool_event(SID, 1, SessionEventType.TOOL_FAILED, name="web_search"),
        progress(SID, 1, pool="ack"),
        interruption(
            SID,
            1,
            source=InterruptionSource.USER_SPEECH,
            phase=InterruptionPhase.ANSWERING,
            played=2,
            generated=5,
        ),
        error(SID, "provider stalled", fatal=False),
        warning(SID, "underrun"),
        usage_attempt_started(
            SID,
            1,
            "sess:1:deepgram:stt.stream:1",
            provider="deepgram",
            operation="stt.stream",
            payment_mode=PaymentMode.HOSTED,
            units={"audioMs": 120},
        ),
        usage_attempt_finished(
            SID,
            1,
            "sess:1:deepgram:stt.stream:1",
            provider="deepgram",
            operation="stt.stream",
            payment_mode=PaymentMode.HOSTED,
            status=AttemptStatus.OK,
            units={"audioMs": 440},
        ),
        usage_summary(SID, duration_ms=91_000.0, totals={"audioMs": 440}),
    ]
    assert len({e.type for e in events}) == 18  # every variant exercised
    for event in events:
        restored = SessionEvent.from_json(json.loads(json.dumps(event.to_json())))
        assert restored.type == event.type
        assert restored.session_id == event.session_id
        assert restored.turn_id == event.turn_id
        assert restored.attempt_id == event.attempt_id
        assert restored.payload == event.payload


def test_json_field_names_are_stable_wire_format():
    event = usage_attempt_started(
        SID,
        3,
        "sess:3:cartesia:tts.stream:2",
        provider="cartesia",
        operation="tts.stream",
        payment_mode=PaymentMode.BYOK,
        units={"inputCharacters": 42},
    )
    data = event.to_json()
    assert data["type"] == "usage.attempt.started"
    assert data["sessionId"] == SID
    assert data["turnId"] == 3
    assert data["attemptId"] == "sess:3:cartesia:tts.stream:2"
    assert data["payload"]["provider"] == "cartesia"
    assert data["payload"]["paymentMode"] == "byok"
    # Raw provider units preserved verbatim (ADR 0003 §3).
    assert data["payload"]["units"] == {"inputCharacters": 42}


# -- correlation invariants ---------------------------------------------


def test_turn_scoped_types_require_turn_id():
    for event_type in (
        SessionEventType.RESPONSE_STARTED,
        SessionEventType.TRANSCRIPT_FINAL,
        SessionEventType.INTERRUPTION,
    ):
        with pytest.raises(EventError):
            SessionEvent(type=event_type, session_id=SID)


def test_attempt_scoped_types_require_attempt_id():
    with pytest.raises(EventError):
        SessionEvent(
            type=SessionEventType.USAGE_ATTEMPT_STARTED, session_id=SID, turn_id=1
        )


def test_usage_summary_is_session_scoped_not_turn_scoped():
    event = usage_summary(SID, totals={})
    assert event.turn_id is None
    assert event.to_json().get("turnId") is None


def test_empty_session_id_rejected():
    with pytest.raises(EventError):
        SessionEvent(type=SessionEventType.WARNING, session_id="")


# -- ordering -----------------------------------------------------------


def test_turn_ids_order_within_a_session():
    events = [
        response(SID, 1, SessionEventType.RESPONSE_STARTED),
        response(SID, 2, SessionEventType.RESPONSE_STARTED),
        response(SID, 1, SessionEventType.RESPONSE_CANCELLED),  # late cancel
        response(SID, 3, SessionEventType.RESPONSE_STARTED),
    ]
    # Stale-audio rule (ADR 0003 §4 clause 1): anything older than the
    # newest committed turn must be dropped before producing output.
    newest = max(e.turn_id for e in events)
    stale = [e for e in events if e.turn_id < newest]
    assert newest == 3
    assert len(stale) == 3


# -- backward compatibility ---------------------------------------------


def test_unknown_future_top_level_fields_survive_round_trip():
    legacy = SessionEvent.from_json(
        {
            "type": "session.state",
            "sessionId": SID,
            "timestamp": "2026-08-20T00:00:00+00:00",
            "payload": {"state": "active"},
            "futureField": {"nested": True},
        }
    )
    assert legacy.extra["futureField"] == {"nested": True}
    round_tripped = SessionEvent.from_json(json.loads(json.dumps(legacy.to_json())))
    assert round_tripped.extra["futureField"] == {"nested": True}
    assert round_tripped.payload == {"state": "active"}


def test_extra_block_merges_with_unknown_fields_and_wins():
    event = SessionEvent.from_json(
        {
            "type": "warning",
            "sessionId": SID,
            "payload": {"message": "m"},
            "extra": {"versioned": 1},
            "unversioned": 2,
        }
    )
    assert event.extra == {"unversioned": 2, "versioned": 1}


def test_unknown_payload_fields_pass_through():
    event = SessionEvent.from_json(
        {
            "type": "transcript.final",
            "sessionId": SID,
            "turnId": 7,
            "payload": {"speaker": "user", "text": "hi", "confidence": 0.9},
        }
    )
    assert event.payload["confidence"] == 0.9
    again = SessionEvent.from_json(json.loads(json.dumps(event.to_json())))
    assert again.payload["confidence"] == 0.9


def test_malformed_events_fail_loudly():
    with pytest.raises(EventError):
        SessionEvent.from_json({"sessionId": SID})  # no type
    with pytest.raises(EventError):
        SessionEvent.from_json({"type": "not.a.type", "sessionId": SID})
    with pytest.raises(EventError):
        SessionEvent.from_json({"type": "warning"})  # no sessionId
    with pytest.raises(EventError):
        SessionEvent.from_json({"type": "warning", "sessionId": SID, "turnId": "one"})


def test_interruption_reports_played_and_generated_measures():
    event = interruption(
        SID,
        2,
        source=InterruptionSource.CANCEL,
        phase=InterruptionPhase.WORKING,
        played={"sentences": 1},
        generated={"sentences": 4},
    )
    payload = event.to_json()["payload"]
    assert payload["played"] == {"sentences": 1}
    assert payload["generated"] == {"sentences": 4}
    assert payload["source"] == "cancel"
    assert payload["phase"] == "working"


def test_fatal_error_flag_distinguishes_session_leaving_active():
    recoverable = error(SID, "turn stalled", fatal=False)
    fatal = error(SID, "route dead", fatal=True)
    assert recoverable.to_json()["payload"]["fatal"] is False
    assert fatal.to_json()["payload"]["fatal"] is True
