"""Tests for sleeptalk.usage + metering normalized records (r2-usage-schema).

Covers: lossless Deepgram duration mapping; both TTS provider unit mappings;
native audio/text dimensions; content-free enforcement; meter integration
with the normalized ``usage`` field on attempt events; BYOK/hosted payment
mode attribution; priceability rules (agent.stream never priceable).
"""

import json

import pytest

from sleeptalk.metering import UsageMeter
from sleeptalk.usage import (
    FORBIDDEN_CONTENT_KEYS,
    Attribution,
    Operation,
    OperationDimension,
    UsageRecord,
    UsageSchemaError,
    UsageUnit,
    agent_stream_attempt,
    cartesia_tts_attempt,
    deepgram_stt_attempt,
    elevenlabs_tts_attempt,
    native_rt_attempt,
)


def attr(**overrides) -> Attribution:
    base = {
        "session_id": "s-1",
        "attempt_id": "s-1:1:deepgram:stt.stream:1",
        "turn_id": 1,
        "payment_mode": "byok",
    }
    base.update(overrides)
    return Attribution(**base)


# -- Deepgram duration mapping -------------------------------------------


def test_deepgram_duration_maps_losslessly() -> None:
    record = deepgram_stt_attempt(
        attribution=attr(),
        audio_ms=1_234,
        audio_bytes=118_656,
    )
    assert record.operation is Operation.STT_STREAM
    assert record.quantity == pytest.approx(1.234)
    assert record.unit is UsageUnit.SECONDS
    assert record.attribution.provider == "deepgram"
    # Raw units preserved verbatim — reconciliation is lossless.
    assert record.raw_units == {"audioMs": 1_234, "audioBytes": 118_656}
    assert record.priceable


def test_deepgram_round_trip_preserves_raw() -> None:
    record = deepgram_stt_attempt(attribution=attr(), audio_ms=500)
    restored = UsageRecord.from_json(json.loads(json.dumps(record.to_json())))
    assert restored == record


# -- TTS providers --------------------------------------------------------


def test_cartesia_characters_map() -> None:
    record = cartesia_tts_attempt(
        attribution=attr(attempt_id="s-1:2:cartesia:tts.stream:1"),
        input_characters=420,
        output_audio_bytes=96_000,
    )
    assert record.operation is Operation.TTS_STREAM
    assert record.quantity == 420
    assert record.unit is UsageUnit.CHARACTERS
    assert record.attribution.provider == "cartesia"
    assert record.raw_units == {
        "inputCharacters": 420,
        "outputAudioBytes": 96_000,
    }
    assert record.priceable


def test_elevenlabs_characters_map() -> None:
    record = elevenlabs_tts_attempt(
        attribution=attr(attempt_id="s-1:3:elevenlabs:tts.buffered:1"),
        input_characters=87,
        output_audio_bytes=21_000,
        operation=Operation.TTS_BUFFERED,
    )
    assert record.quantity == 87
    assert record.unit is UsageUnit.CHARACTERS
    assert record.attribution.provider == "elevenlabs"
    assert record.raw_units["inputCharacters"] == 87
    assert record.priceable


# -- native dimensions ----------------------------------------------------


def test_native_audio_dimension() -> None:
    record = native_rt_attempt(
        attribution=attr(
            attempt_id="s-1:4:openai:rt.audio:1", provider="openai"
        ),
        audio_seconds=12.5,
    )
    assert record.operation is Operation.RT_AUDIO
    assert record.quantity == 12.5
    assert record.unit is UsageUnit.SECONDS
    assert record.raw_units == {"audioSeconds": 12.5}
    assert record.priceable


def test_native_text_dimension_is_agent_diagnostic() -> None:
    record = agent_stream_attempt(
        attribution=attr(
            attempt_id="s-1:5:claude-code:agent.stream:1", provider="claude-code"
        ),
        input_characters=100,
        output_characters=240,
    )
    # Text dimension representable, but agent work is never priceable.
    assert record.operation is Operation.AGENT_STREAM
    assert record.unit is UsageUnit.CHARACTERS
    assert record.quantity == 100
    assert record.raw_units["outputCharacters"] == 240
    assert not record.priceable


def test_operation_dimensions_are_declared() -> None:
    from sleeptalk.usage import OPERATION_DIMENSIONS

    for operation, (unit, dimension) in OPERATION_DIMENSIONS.items():
        if dimension is OperationDimension.AUDIO:
            assert unit is UsageUnit.SECONDS
        else:
            assert unit is UsageUnit.CHARACTERS
    # cascade speech ops + fused native op + agent diagnostics
    assert set(OPERATION_DIMENSIONS) == {
        Operation.STT_STREAM,
        Operation.TTS_STREAM,
        Operation.TTS_BUFFERED,
        Operation.RT_AUDIO,
        Operation.AGENT_STREAM,
    }


# -- content-free enforcement --------------------------------------------

def test_schema_forbids_conversation_content_in_raw_units() -> None:
    with pytest.raises(UsageSchemaError, match="forbidden content keys"):
        UsageRecord(
            operation=Operation.STT_STREAM,
            quantity=0.1,
            unit=UsageUnit.SECONDS,
            attribution=attr(),
            started_at="2026-08-21T00:00:00+00:00",
            finished_at="2026-08-21T00:00:01+00:00",
            raw_units={"transcript": "hello there"},  # content, not a unit
        )

def test_schema_forbids_content_in_metadata() -> None:
    with pytest.raises(UsageSchemaError, match="forbidden content keys"):
        cartesia_tts_attempt(
            attribution=attr(),
            input_characters=10,
            metadata={"prompt": "what did the user say"},
        )


def test_schema_forbids_credentials() -> None:
    with pytest.raises(UsageSchemaError, match="forbidden content keys"):
        elevenlabs_tts_attempt(
            attribution=attr(),
            input_characters=10,
            metadata={"api_key": "sk-secret"},
        )


def test_forbidden_key_catalog() -> None:
    assert "prompt" in FORBIDDEN_CONTENT_KEYS
    assert "transcript" in FORBIDDEN_CONTENT_KEYS
    assert "audio" in FORBIDDEN_CONTENT_KEYS
    assert "api_key" in FORBIDDEN_CONTENT_KEYS


# -- attribution ----------------------------------------------------------


def test_payment_mode_must_be_hosted_or_byok() -> None:
    with pytest.raises(UsageSchemaError, match="payment_mode"):
        attr(payment_mode="gift")


def test_attribution_requires_identity() -> None:
    with pytest.raises(UsageSchemaError):
        Attribution(session_id="", attempt_id="a")
    with pytest.raises(UsageSchemaError):
        Attribution(session_id="s", attempt_id="")


def test_hosted_mode_attribution_accepted() -> None:
    record = deepgram_stt_attempt(
        attribution=attr(payment_mode="hosted"), audio_ms=1_000
    )
    assert record.attribution.payment_mode == "hosted"
    assert record.to_json()["paymentMode"] == "hosted"


# -- unit/dimension consistency -------------------------------------------


def test_wrong_unit_for_operation_rejected() -> None:
    with pytest.raises(UsageSchemaError, match="normalizes to"):
        UsageRecord(
            operation=Operation.STT_STREAM,
            quantity=1.0,
            unit=UsageUnit.CHARACTERS,  # STT is audio ⇒ seconds
            attribution=attr(),
            started_at="2026-08-21T00:00:00+00:00",
            finished_at="2026-08-21T00:00:01+00:00",
        )


def test_negative_quantity_rejected() -> None:
    with pytest.raises(UsageSchemaError, match="non-negative"):
        deepgram_stt_attempt(attribution=attr(), audio_ms=-5)


# -- meter integration ----------------------------------------------------


def test_meter_emits_normalized_usage_on_finished(tmp_path) -> None:
    log_path = tmp_path / "usage.jsonl"
    meter = UsageMeter(log_path=log_path, session_id="s-1", surface="test")
    record = deepgram_stt_attempt(attribution=attr(), audio_ms=2_000)

    meter.start_attempt(
        provider="deepgram",
        operation="stt.stream",
        attempt_id=record.attribution.attempt_id,
        record=record,
        audioMs=2_000,
    )
    meter.finish_attempt(record.attribution.attempt_id, status="ok")

    events = [json.loads(line) for line in log_path.read_text().splitlines()]
    finished = [e for e in events if e["stage"] == "attempt_finished"][0]
    assert finished["usage"]["operation"] == "stt.stream"
    assert finished["usage"]["quantity"] == 2.0
    assert finished["usage"]["unit"] == "seconds"
    assert finished["usage"]["rawUnits"] == {"audioMs": 2_000, "audioBytes": 0}
    # legacy fields unchanged for existing readers
    assert finished["units"] == {"audioMs": 2_000}
    assert finished["provider"] == "deepgram"


def test_meter_without_record_keeps_legacy_shape(tmp_path) -> None:
    log_path = tmp_path / "usage.jsonl"
    meter = UsageMeter(log_path=log_path, session_id="s-1")
    meter.start_attempt(
        provider="cartesia",
        operation="tts.rest",
        attempt_id="a1",
        inputCharacters=42,
    )
    meter.finish_attempt("a1", status="ok")
    events = [json.loads(line) for line in log_path.read_text().splitlines()]
    finished = [e for e in events if e["stage"] == "attempt_finished"][0]
    assert finished["usage"] is None
    assert finished["units"] == {"inputCharacters": 42}


def test_meter_cancelled_attempt_keeps_normalized_record(tmp_path) -> None:
    log_path = tmp_path / "usage.jsonl"
    meter = UsageMeter(log_path=log_path, session_id="s-1")
    record = cartesia_tts_attempt(attribution=attr(), input_characters=100)
    meter.start_attempt(
        provider="cartesia",
        operation="tts.stream",
        attempt_id=record.attribution.attempt_id,
        record=record,
        inputCharacters=100,
    )
    meter.finish_attempt(record.attribution.attempt_id, status="cancelled")
    events = [json.loads(line) for line in log_path.read_text().splitlines()]
    finished = [e for e in events if e["stage"] == "attempt_finished"][0]
    assert finished["status"] == "cancelled"
    assert finished["usage"]["quantity"] == 100
