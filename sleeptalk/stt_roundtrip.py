"""STT transcription round-trip: prove the configured key + model transcribe.

Posts a bundled ~1s clip of real speech ("sleeptalk check") to the provider's
REST endpoint and reports connectivity + latency. This is NOT a language
validation — the clip is English regardless of stt.language; the language
capability story lives in language_support.py.

The clip (assets/sleeptalk-check.wav) is machine-recorded via macOS `say` as a
placeholder; swap in a human recording when one is available (same filename,
16 kHz mono PCM WAV). A pure tone transcribes empty and false-fails, so the
clip must always contain actual speech.
"""

from __future__ import annotations

import time
from importlib import resources
from urllib.parse import urlencode

import aiohttp

from sleeptalk.providers import ProviderSpec, provider_http_url
from sleeptalk.speech.common import (
    SpeechError,
    _deepgram_query_params_from_spec,
    _http_error_message,
    _read_deepgram_transcript,
)

CLIP_NAME = "sleeptalk-check.wav"


def bundled_clip_bytes() -> bytes:
    return (
        resources.files("sleeptalk.assets")
        .joinpath(CLIP_NAME)
        .read_bytes()
    )


async def stt_roundtrip(
    provider: str, spec: ProviderSpec, api_key: str, *, timeout: float = 15.0
) -> tuple[str, float]:
    """Round-trip the bundled clip through whichever STT provider is configured."""
    provider = provider.strip().lower()
    if provider == "cartesia":
        return await cartesia_roundtrip(spec, api_key, timeout=timeout)
    if provider == "deepgram":
        return await deepgram_roundtrip(spec, api_key, timeout=timeout)
    if provider == "elevenlabs":
        return await elevenlabs_roundtrip(spec, api_key, timeout=timeout)
    if provider == "local":
        return await local_roundtrip(spec, timeout=timeout)
    from sleeptalk.speech.openai_audio import is_preset

    if is_preset(provider):
        return await openai_audio_roundtrip(spec, api_key, timeout=timeout)
    raise SpeechError(f"no STT round-trip check for provider {provider!r}")


async def openai_audio_roundtrip(spec: ProviderSpec, api_key: str, *, timeout: float = 15.0) -> tuple[str, float]:
    """Transcribe the bundled clip on an OpenAI-compatible audio service; returns (transcript, latency_ms)."""
    import asyncio
    import io
    import wave

    from sleeptalk.speech.local_models import resample_int16
    from sleeptalk.speech.openai_audio import OpenAIAudioSTT

    with wave.open(io.BytesIO(bundled_clip_bytes())) as clip:
        pcm = resample_int16(clip.readframes(clip.getnframes()), clip.getframerate(), 48_000)
    stt = OpenAIAudioSTT(spec, api_key=api_key)
    started = time.monotonic()
    try:
        async with asyncio.timeout(timeout):
            transcript = await stt.transcribe_pcm(pcm)
    finally:
        # The adapter keeps its connection open for the next turn; a one-off
        # check has no next turn.
        await stt.close()
    latency_ms = (time.monotonic() - started) * 1000
    if not transcript.strip():
        raise SpeechError(f"{stt.preset.label} accepted the request but transcribed the bundled speech clip as empty; re-run doctor and report if it persists.")
    return transcript.strip(), latency_ms


async def local_roundtrip(spec: ProviderSpec, *, timeout: float = 15.0) -> tuple[str, float]:
    """Transcribe the bundled clip with the local model; returns (transcript, decode_ms)."""
    import asyncio
    import io
    import wave

    from sleeptalk.speech.local_models import resample_int16
    from sleeptalk.speech.local_stt import LocalSTT

    with wave.open(io.BytesIO(bundled_clip_bytes())) as clip:
        pcm = resample_int16(clip.readframes(clip.getnframes()), clip.getframerate(), 48_000)
    if not spec.model or spec.model == "moonshine-small-streaming-en":
        from sleeptalk.speech.local_streaming_stt import LocalStreamingSTT
        stt = LocalStreamingSTT(spec)
    else:
        stt = LocalSTT(spec)
    try:
        async with asyncio.timeout(timeout):
            transcript = await stt.transcribe_pcm(pcm)
        # Decode time only: loading the model is a one-time cost per call.
        latency_ms = float(getattr(stt, "last_decode_ms", 0.0))
    finally:
        await stt.close()
    if not transcript.strip():
        raise SpeechError("The local model transcribed the bundled speech clip as empty; re-run doctor and report if it persists.")
    return transcript.strip(), latency_ms


async def elevenlabs_roundtrip(
    spec: ProviderSpec, api_key: str, *, timeout: float = 15.0
) -> tuple[str, float]:
    """Transcribe the bundled clip on ElevenLabs Scribe; returns (transcript, latency_ms)."""
    from sleeptalk.speech.elevenlabs_stt import DEFAULT_BASE, batch_model, _primary_language

    if not api_key.strip():
        raise SpeechError("Missing ELEVENLABS_API_KEY.")
    model = batch_model(spec.model)
    form = aiohttp.FormData()
    form.add_field("file", bundled_clip_bytes(), filename=CLIP_NAME, content_type="audio/wav")
    form.add_field("model_id", model)
    form.add_field("diarize", "false")
    form.add_field("timestamps_granularity", "none")
    form.add_field("tag_audio_events", "false")
    language = _primary_language(spec.language)
    if language:
        form.add_field("language_code", language)
    started = time.monotonic()
    async with aiohttp.ClientSession() as session, session.post(
        provider_http_url(spec, DEFAULT_BASE, "/v1/speech-to-text"),
        data=form,
        headers={"xi-api-key": api_key},
        timeout=aiohttp.ClientTimeout(total=timeout),
    ) as response:
        latency_ms = (time.monotonic() - started) * 1000
        if response.status != 200:
            body = await response.text()
            raise SpeechError(
                _http_error_message("ElevenLabs STT", response, body, f"model={model}, language={language}")
            )
        payload = await response.json()
    transcript = str(payload.get("text") or "") if isinstance(payload, dict) else ""
    if not transcript.strip():
        raise SpeechError(
            "ElevenLabs accepted the request but transcribed the bundled speech "
            "clip as empty — the model/key work, but this is unexpected; "
            "re-run doctor and report if it persists."
        )
    return transcript.strip(), latency_ms


async def cartesia_roundtrip(
    spec: ProviderSpec, api_key: str, *, timeout: float = 15.0
) -> tuple[str, float]:
    """Transcribe the bundled clip on Cartesia batch STT; returns (transcript, latency_ms)."""
    from sleeptalk.speech.cartesia_stt import BATCH_MODEL, CARTESIA_VERSION, _primary_language

    if not api_key.strip():
        raise SpeechError("Missing CARTESIA_API_KEY.")
    form = aiohttp.FormData()
    form.add_field("file", bundled_clip_bytes(), filename=CLIP_NAME, content_type="audio/wav")
    form.add_field("model", BATCH_MODEL)
    language = _primary_language(spec.language)
    if language:
        form.add_field("language", language)
    started = time.monotonic()
    async with aiohttp.ClientSession() as session, session.post(
        provider_http_url(spec, "https://api.cartesia.ai", "/stt"),
        data=form,
        headers={"X-API-Key": api_key, "Cartesia-Version": CARTESIA_VERSION},
        timeout=aiohttp.ClientTimeout(total=timeout),
    ) as response:
        latency_ms = (time.monotonic() - started) * 1000
        if response.status != 200:
            body = await response.text()
            raise SpeechError(
                _http_error_message("Cartesia STT", response, body, f"model={BATCH_MODEL}, language={language}")
            )
        payload = await response.json()
    transcript = str(payload.get("text") or "") if isinstance(payload, dict) else ""
    if not transcript.strip():
        raise SpeechError(
            "Cartesia accepted the request but transcribed the bundled speech "
            "clip as empty — the model/key work, but this is unexpected; "
            "re-run doctor and report if it persists."
        )
    return transcript.strip(), latency_ms


async def deepgram_roundtrip(
    spec: ProviderSpec, api_key: str, *, timeout: float = 15.0
) -> tuple[str, float]:
    """Transcribe the bundled clip; returns (transcript, latency_ms)."""
    if not api_key.strip():
        raise SpeechError("Missing DEEPGRAM_API_KEY.")
    params = _deepgram_query_params_from_spec(spec)
    base = provider_http_url(spec, "https://api.deepgram.com", "/v1/listen")
    url = f"{base}?{urlencode(params)}"
    started = time.monotonic()
    async with aiohttp.ClientSession() as session, session.post(
        url,
        data=bundled_clip_bytes(),
        headers={
            "Authorization": f"Token {api_key}",
            "Content-Type": "audio/wav",
        },
        timeout=aiohttp.ClientTimeout(total=timeout),
    ) as response:
        latency_ms = (time.monotonic() - started) * 1000
        if response.status != 200:
            body = await response.text()
            raise SpeechError(
                _http_error_message(
                    "Deepgram STT",
                    response,
                    body,
                    f"model={spec.model}, language={spec.language}",
                )
            )
        payload = await response.json()
    transcript = _read_deepgram_transcript(payload)
    if not transcript.strip():
        raise SpeechError(
            "Deepgram accepted the request but transcribed the bundled speech "
            "clip as empty — the model/key work, but this is unexpected; "
            "re-run doctor and report if it persists."
        )
    return transcript, latency_ms
