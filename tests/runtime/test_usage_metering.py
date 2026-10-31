import json

from sleeptalk.metering import UsageMeter


def test_usage_meter_accumulates_attempts_without_content(tmp_path):
    log_path = tmp_path / "provider_usage.jsonl"
    meter = UsageMeter(
        log_path=log_path,
        session_id="session123",
        surface="deepre-discord",
    )

    meter.start_attempt(
        provider="deepgram",
        operation="stt.websocket",
        attempt_id="turn1:stt:1",
        audioMs=20,
    )
    meter.add_usage("turn1:stt:1", audioMs=40, audioBytes=7_680)
    meter.finish_attempt("turn1:stt:1", status="ok")
    meter.start_attempt(
        provider="cartesia",
        operation="tts.websocket",
        attempt_id="turn1:tts:1",
        inputCharacters=42,
    )
    meter.finish_attempt("turn1:tts:1", status="retry")
    meter.finish_session()

    events = [json.loads(line) for line in log_path.read_text().splitlines()]
    summary = events[-1]
    assert summary["stage"] == "session_summary"
    assert summary["totals"] == {
        "audioBytes": 7_680,
        "audioMs": 60,
        "inputCharacters": 42,
    }
    assert summary["attempts"] == {
        "cartesia:tts.websocket": 1,
        "deepgram:stt.websocket": 1,
    }
    serialized = log_path.read_text()
    assert "prompt" not in serialized
    assert "transcript" not in serialized
    assert "api_key" not in serialized


def test_usage_meter_ignores_duplicate_attempt_start(tmp_path):
    meter = UsageMeter(log_path=tmp_path / "usage.jsonl", session_id="session123")

    meter.start_attempt(
        provider="cartesia",
        operation="tts.websocket",
        attempt_id="same-attempt",
        inputCharacters=20,
    )
    meter.start_attempt(
        provider="cartesia",
        operation="tts.websocket",
        attempt_id="same-attempt",
        inputCharacters=20,
    )

    assert meter.snapshot()["totals"]["inputCharacters"] == 20
    assert meter.snapshot()["attempts"]["cartesia:tts.websocket"] == 1


def test_usage_meter_carries_attempt_metadata_to_finished_event(tmp_path):
    log_path = tmp_path / "usage.jsonl"
    meter = UsageMeter(log_path=log_path, session_id="session123")

    meter.start_attempt(
        provider="deepgram",
        operation="stt.rest",
        attempt_id="warmup-attempt",
        metadata={"purpose": "warmup"},
        audioMs=100,
    )
    meter.finish_attempt("warmup-attempt", status="ok")

    event = json.loads(log_path.read_text().splitlines()[-1])
    assert event["stage"] == "attempt_finished"
    assert event["purpose"] == "warmup"
