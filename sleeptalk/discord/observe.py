from __future__ import annotations

from sleeptalk.voice_loop.endpointing import EndpointDecision
from sleeptalk.voice_loop.telemetry import log_fallback_rate_decision, log_latency_event


def _log_native_endpoint_decision(
    *,
    surface: str,
    persona: str,
    user_id: int,
    decision: EndpointDecision,
    trace_id: str | None = None,
) -> None:
    log_latency_event(
        surface=surface,
        transport="discord-native",
        persona=persona,
        stage="endpoint_decision",
        trace_id=trace_id,
        userId=user_id,
        silenceMs=decision.silence_ms,
        thresholdMs=decision.threshold_ms,
        mode=decision.mode,
        semanticScore=decision.semantic_score,
        fillerDetected=decision.filler_detected,
        endpoint=decision.should_endpoint,
        reason=decision.reason,
        source=decision.source,
        transcriptChars=len(decision.transcript),
    )
    log_fallback_rate_decision(
        surface=surface,
        transport="discord-native",
        persona=persona,
        architecture="semantic_endpointing",
        path="acoustic_fallback" if decision.reason == "acoustic_fallback" else "semantic",
        trace_id=trace_id,
    )
