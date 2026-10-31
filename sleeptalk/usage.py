"""Normalized usage records — the catalog-priceable event schema (r2-usage-schema).

The ``UsageMeter`` (``sleeptalk.metering``) observes provider attempts with
raw, provider-native units (``audioMs``, ``inputCharacters``, …). This module
gives those observations a stable, priceable shape:

- **provider / model / operation** — who did what (operations are the metering
  vocabulary of ADR 0003 §1; the priced catalog of ADR 0004 contains speech
  operations only, ``agent.stream`` is diagnostic-only).
- **quantity / unit** — one normalized number per operation on a declared
  dimension (audio ⇒ seconds; text ⇒ characters).
- **timing** — wall-clock start/finish of the attempt.
- **attribution** — session/turn/attempt identity plus payment mode
  (ADR 0004 §4: every attempt is stamped ``payment_mode ∈ {hosted, byok}``;
  the client never assigns retail prices — pricing belongs to the catalog).
- **raw units** — the provider-native units preserved verbatim, so the
  normalized number is always reconcilable against the provider's own meter
  (billing-reconciliation matrix requirement).

The schema is content-free by construction: prompts, transcripts, audio, and
credentials are forbidden keys (``FORBIDDEN_CONTENT_KEYS``) and their presence
raises :class:`UsageSchemaError` — a usage record must never become a
conversation log.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Mapping

__all__ = [
    "FORBIDDEN_CONTENT_KEYS",
    "Attribution",
    "Operation",
    "OperationDimension",
    "UsageRecord",
    "UsageSchemaError",
    "UsageUnit",
    "agent_stream_attempt",
    "deepgram_stt_attempt",
    "elevenlabs_tts_attempt",
    "native_rt_attempt",
    "cartesia_tts_attempt",
]


class UsageSchemaError(Exception):
    """A usage record is structurally invalid or carries forbidden content."""


class Operation(enum.Enum):
    """Catalog-priceable operations (ADR 0003 §1, ADR 0004 §1).

    Speech operations carry price in the catalog; ``AGENT_STREAM`` is metered
    for diagnostics and identity only — never billed (ADR 0004 §5).
    """

    STT_STREAM = "stt.stream"
    TTS_STREAM = "tts.stream"
    TTS_BUFFERED = "tts.buffered"
    RT_AUDIO = "rt.audio"
    AGENT_STREAM = "agent.stream"


class UsageUnit(enum.Enum):
    """Normalized units. One dimension per operation; raw units stay raw."""

    SECONDS = "seconds"
    CHARACTERS = "characters"


class OperationDimension(enum.Enum):
    AUDIO = "audio"
    TEXT = "text"


#: operation → (normalized unit, dimension). Native routes meter fused
#: audio-to-audio work as RT_AUDIO seconds; cascade meters STT seconds plus
#: TTS characters; agent text is a text dimension for diagnostics.
OPERATION_DIMENSIONS: dict[Operation, tuple[UsageUnit, OperationDimension]] = {
    Operation.STT_STREAM: (UsageUnit.SECONDS, OperationDimension.AUDIO),
    Operation.TTS_STREAM: (UsageUnit.CHARACTERS, OperationDimension.TEXT),
    Operation.TTS_BUFFERED: (UsageUnit.CHARACTERS, OperationDimension.TEXT),
    Operation.RT_AUDIO: (UsageUnit.SECONDS, OperationDimension.AUDIO),
    Operation.AGENT_STREAM: (UsageUnit.CHARACTERS, OperationDimension.TEXT),
}

#: Keys a usage record must never carry (content-free invariant). Checked on
#: raw units, metadata, and every field name at construction.
FORBIDDEN_CONTENT_KEYS = frozenset(
    {
        "prompt",
        "transcript",
        "text",
        "audio",
        "audio_bytes",
        "pcm",
        "content",
        "api_key",
        "apikey",
        "key",
        "token",
        "password",
        "credential",
        "response_body",
    }
)

#: Chargeable in the hosted catalog (ADR 0004: speech operations only).
PRICEABLE_OPERATIONS = frozenset(
    {
        Operation.STT_STREAM,
        Operation.TTS_STREAM,
        Operation.TTS_BUFFERED,
        Operation.RT_AUDIO,
    }
)


@dataclass(frozen=True)
class Attribution:
    """Identity of record for one attempt (ADR 0003 §1, ADR 0004 §4)."""

    session_id: str
    attempt_id: str
    turn_id: int | None = None
    provider: str = ""
    model: str = ""
    #: ``hosted`` attempts debit credit; ``byok`` attempts are
    #: diagnostic-only and can never reach the financial ledger.
    payment_mode: str = "byok"

    def __post_init__(self) -> None:
        if not self.session_id:
            raise UsageSchemaError("attribution requires session_id")
        if not self.attempt_id:
            raise UsageSchemaError("attribution requires attempt_id")
        if self.payment_mode not in {"hosted", "byok"}:
            raise UsageSchemaError(
                f"payment_mode must be hosted|byok, got {self.payment_mode!r}"
            )


def _validate_mapping(mapping: Mapping[str, Any] | None, *, what: str) -> dict[str, Any]:
    if mapping is None:
        return {}
    bad = FORBIDDEN_CONTENT_KEYS.intersection(mapping)
    if bad:
        raise UsageSchemaError(f"{what} carries forbidden content keys: {sorted(bad)}")
    return dict(mapping)


@dataclass(frozen=True)
class UsageRecord:
    """One normalized, priceable usage observation.

    ``quantity`` is the normalized amount in ``unit``; ``raw_units`` preserves
    the provider's native units verbatim (``audioMs``, ``inputCharacters``,
    ``outputAudioBytes`` …) so reconciliation against the provider meter is
    lossless. No retail price is ever assigned here (client-side; the hosted
    catalog owns pricing).
    """

    operation: Operation
    quantity: float
    unit: UsageUnit
    attribution: Attribution
    started_at: str
    finished_at: str
    status: str = "ok"
    raw_units: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        expected_unit, _ = OPERATION_DIMENSIONS[self.operation]
        if self.unit is not expected_unit:
            raise UsageSchemaError(
                f"{self.operation.value} normalizes to {expected_unit.value}, "
                f"got {self.unit.value}"
            )
        if not isinstance(self.quantity, (int, float)) or isinstance(self.quantity, bool):
            raise UsageSchemaError("quantity must be numeric")
        if self.quantity < 0:
            raise UsageSchemaError("quantity must be non-negative")
        # Frozen dataclass: validate via object.__setattr__-safe copies.
        object.__setattr__(self, "raw_units", _validate_mapping(self.raw_units, what="raw_units"))
        object.__setattr__(self, "metadata", _validate_mapping(self.metadata, what="metadata"))
        for forbidden in FORBIDDEN_CONTENT_KEYS:
            if forbidden in self.__dataclass_fields__:
                raise UsageSchemaError(f"UsageRecord field {forbidden!r} is forbidden")

    @property
    def priceable(self) -> bool:
        """Whether the hosted catalog may price this operation."""
        return self.operation in PRICEABLE_OPERATIONS

    def to_json(self) -> dict[str, Any]:
        return {
            "operation": self.operation.value,
            "quantity": self.quantity,
            "unit": self.unit.value,
            "provider": self.attribution.provider,
            "model": self.attribution.model,
            "sessionId": self.attribution.session_id,
            "turnId": self.attribution.turn_id,
            "attemptId": self.attribution.attempt_id,
            "paymentMode": self.attribution.payment_mode,
            "startedAt": self.started_at,
            "finishedAt": self.finished_at,
            "status": self.status,
            "rawUnits": dict(self.raw_units),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> UsageRecord:
        return cls(
            operation=Operation(data["operation"]),
            quantity=float(data["quantity"]),
            unit=UsageUnit(data["unit"]),
            attribution=Attribution(
                session_id=data["sessionId"],
                attempt_id=data["attemptId"],
                turn_id=data.get("turnId"),
                provider=data.get("provider", ""),
                model=data.get("model", ""),
                payment_mode=data.get("paymentMode", "byok"),
            ),
            started_at=data["startedAt"],
            finished_at=data["finishedAt"],
            status=data.get("status", "ok"),
            raw_units=dict(data.get("rawUnits", {})),
            metadata=dict(data.get("metadata", {})),
        )


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _record(
    *,
    operation: Operation,
    quantity: float,
    attribution: Attribution,
    raw_units: Mapping[str, Any],
    status: str,
    started_at: str | None,
    finished_at: str | None,
    metadata: Mapping[str, Any] | None,
) -> UsageRecord:
    return UsageRecord(
        operation=operation,
        quantity=quantity,
        unit=OPERATION_DIMENSIONS[operation][0],
        attribution=attribution,
        started_at=started_at or _now_iso(),
        finished_at=finished_at or _now_iso(),
        status=status,
        raw_units=dict(raw_units),
        metadata=dict(metadata or {}),
    )


# -- provider normalizers (fixtures for the lossless-mapping acceptance) --


def deepgram_stt_attempt(
    *,
    attribution: Attribution,
    audio_ms: float,
    audio_bytes: float = 0.0,
    status: str = "ok",
    started_at: str | None = None,
    finished_at: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> UsageRecord:
    """Deepgram STT: milliseconds (and bytes) → normalized audio seconds.

    Raw ``audioMs``/``audioBytes`` are preserved verbatim for reconciliation
    with Deepgram's duration-metered usage reports.
    """
    attribution = Attribution(
        session_id=attribution.session_id,
        attempt_id=attribution.attempt_id,
        turn_id=attribution.turn_id,
        provider="deepgram",
        model=attribution.model,
        payment_mode=attribution.payment_mode,
    )
    return _record(
        operation=Operation.STT_STREAM,
        quantity=audio_ms / 1000.0,
        attribution=attribution,
        raw_units={"audioMs": audio_ms, "audioBytes": audio_bytes},
        status=status,
        started_at=started_at,
        finished_at=finished_at,
        metadata=metadata,
    )


def _tts_attempt(
    *,
    provider: str,
    attribution: Attribution,
    operation: Operation,
    input_characters: float,
    output_audio_bytes: float = 0.0,
    status: str = "ok",
    started_at: str | None = None,
    finished_at: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> UsageRecord:
    attribution = Attribution(
        session_id=attribution.session_id,
        attempt_id=attribution.attempt_id,
        turn_id=attribution.turn_id,
        provider=provider,
        model=attribution.model,
        payment_mode=attribution.payment_mode,
    )
    return _record(
        operation=operation,
        quantity=input_characters,
        attribution=attribution,
        raw_units={
            "inputCharacters": input_characters,
            "outputAudioBytes": output_audio_bytes,
        },
        status=status,
        started_at=started_at,
        finished_at=finished_at,
        metadata=metadata,
    )


def cartesia_tts_attempt(
    *,
    attribution: Attribution,
    input_characters: float,
    output_audio_bytes: float = 0.0,
    operation: Operation = Operation.TTS_STREAM,
    status: str = "ok",
    started_at: str | None = None,
    finished_at: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> UsageRecord:
    """Cartesia TTS: characters in → normalized character quantity.

    Cartesia meters TTS in credits ≈ input characters; the raw
    ``inputCharacters``/``outputAudioBytes`` are preserved verbatim.
    """
    return _tts_attempt(
        provider="cartesia",
        attribution=attribution,
        operation=operation,
        input_characters=input_characters,
        output_audio_bytes=output_audio_bytes,
        status=status,
        started_at=started_at,
        finished_at=finished_at,
        metadata=metadata,
    )


def elevenlabs_tts_attempt(
    *,
    attribution: Attribution,
    input_characters: float,
    output_audio_bytes: float = 0.0,
    operation: Operation = Operation.TTS_STREAM,
    status: str = "ok",
    started_at: str | None = None,
    finished_at: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> UsageRecord:
    """ElevenLabs TTS: characters in → normalized character quantity.

    ElevenLabs meters TTS per character (``character-cost`` response header);
    the raw ``inputCharacters``/``outputAudioBytes`` are preserved verbatim.
    """
    return _tts_attempt(
        provider="elevenlabs",
        attribution=attribution,
        operation=operation,
        input_characters=input_characters,
        output_audio_bytes=output_audio_bytes,
        status=status,
        started_at=started_at,
        finished_at=finished_at,
        metadata=metadata,
    )


def native_rt_attempt(
    *,
    attribution: Attribution,
    audio_seconds: float,
    status: str = "ok",
    started_at: str | None = None,
    finished_at: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> UsageRecord:
    """Native realtime route: fused audio-to-audio work as ``rt.audio``
    seconds (ADR 0003 §5: native usage is one fused operation)."""
    attribution = Attribution(
        session_id=attribution.session_id,
        attempt_id=attribution.attempt_id,
        turn_id=attribution.turn_id,
        provider=attribution.provider,
        model=attribution.model,
        payment_mode=attribution.payment_mode,
    )
    return _record(
        operation=Operation.RT_AUDIO,
        quantity=audio_seconds,
        attribution=attribution,
        raw_units={"audioSeconds": audio_seconds},
        status=status,
        started_at=started_at,
        finished_at=finished_at,
        metadata=metadata,
    )


def agent_stream_attempt(
    *,
    attribution: Attribution,
    input_characters: float,
    output_characters: float = 0.0,
    status: str = "ok",
    started_at: str | None = None,
    finished_at: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> UsageRecord:
    """Agent work: metered for diagnostics and identity, never billed
    (ADR 0004 §5 — the ledger catalog contains speech operations only)."""
    attribution = Attribution(
        session_id=attribution.session_id,
        attempt_id=attribution.attempt_id,
        turn_id=attribution.turn_id,
        provider=attribution.provider,
        model=attribution.model,
        payment_mode=attribution.payment_mode,
    )
    return _record(
        operation=Operation.AGENT_STREAM,
        quantity=input_characters,
        attribution=attribution,
        raw_units={
            "inputCharacters": input_characters,
            "outputCharacters": output_characters,
        },
        status=status,
        started_at=started_at,
        finished_at=finished_at,
        metadata=metadata,
    )
