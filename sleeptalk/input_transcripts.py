"""Read revisable input text without granting it authority to commit a turn."""
from __future__ import annotations

from dataclasses import replace
from typing import Any


def input_source(session: Any) -> str:
    stt = getattr(getattr(session, "config", None), "stt", None)
    provider = getattr(stt, "provider", "")
    if provider == "local":
        from sleeptalk.builder import stt_model_for_provider

        if stt_model_for_provider(provider, getattr(stt, "model", "")) == "moonshine-small-streaming-en":
            return "moonshine_streaming"
    return provider


def input_partial(session: Any, participant_id: int = 0) -> str:
    signal_fn = getattr(session, "turn_completion_signal", None)
    signal = signal_fn(participant_id) if callable(signal_fn) else None
    source = getattr(signal, "source", "")
    if source:
        return str(getattr(signal, "transcript", "") or "") if source == input_source(session) else ""
    preview = getattr(session, "input_transcript_preview", None)
    return str(preview(participant_id) or "") if callable(preview) else ""


def input_endpointing_config(session: Any, config: Any) -> Any:
    if config is None:
        return config
    if config.local_responsiveness and input_source(session) == "moonshine_streaming":
        return replace(config, local_streaming_input=True)
    if config.local_responsiveness:
        # Parity with web_voice.session: a live cascade listener gets the
        # fresh-voice clock too. Without it the silence that endpointing
        # measures starts at the end of Silero's stop window rather than at
        # the last voiced frame, and the re-check floor stays at 50 ms.
        return replace(config, streaming_pause_timing=True)
    return config
