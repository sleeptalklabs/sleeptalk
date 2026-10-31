"""Shared helpers for speech providers: wav shaping, error text, content gate."""

from __future__ import annotations

import io
import re
import wave
import struct


class SpeechError(RuntimeError):
    pass


# Tags Deepgram refuses in their regional form (HTTP 400, measured 2026-09-24)
# but accepts as the bare language.
_DEEPGRAM_LANGUAGE_TAGS = {"hi-IN": "hi"}


def _deepgram_query_params_from_spec(spec: ProviderSpec) -> list[tuple[str, str]]:
    params = [
        ("model", spec.model),
        ("smart_format", "true"),
        ("language", _DEEPGRAM_LANGUAGE_TAGS.get(spec.language, spec.language)),
    ]
    keyterms = spec.options.get("keyterms")
    if not isinstance(keyterms, list):
        keyterms = []
    if "nova-3" in spec.model.lower():
        params.extend(("keyterm", keyterm) for keyterm in keyterms)
    else:
        params.extend(("keywords", f"{keyterm}:2") for keyterm in keyterms)
    return params


def _pcm_to_wav(pcm: bytes, channels: int, sample_width: int, sample_rate: int) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(sample_width)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)
    return buffer.getvalue()


def _stereo_pcm16_to_mono(pcm: bytes) -> bytes:
    if not pcm or len(pcm) % 4:
        return pcm
    output = bytearray(len(pcm) // 2)
    out_index = 0
    for index in range(0, len(pcm), 4):
        left = int.from_bytes(pcm[index : index + 2], "little", signed=True)
        right = int.from_bytes(pcm[index + 2 : index + 4], "little", signed=True)
        mixed = int((left + right) / 2)
        output[out_index : out_index + 2] = mixed.to_bytes(2, "little", signed=True)
        out_index += 2
    return bytes(output)


def _read_deepgram_transcript(payload: dict) -> str:
    # Streaming (websocket v1/listen) responses nest under "channel";
    # prerecorded REST responses nest under "results.channels[0]".
    for path in (
        ("channel", "alternatives"),
        ("results", "channels"),
    ):
        try:
            if path[0] == "channel":
                transcript = payload["channel"]["alternatives"][0]["transcript"]
            else:
                transcript = payload["results"]["channels"][0]["alternatives"][0]["transcript"]
        except (KeyError, IndexError, TypeError):
            continue
        return transcript.strip() if isinstance(transcript, str) else ""
    return ""


def _normalize_wav_bytes(audio: bytes) -> bytes:
    if not audio.startswith(b"RIFF") or b"WAVE" not in audio[:16]:
        return audio
    try:
        with wave.open(io.BytesIO(audio), "rb") as source:
            pcm = source.readframes(source.getnframes())
            channels = source.getnchannels()
            sample_width = source.getsampwidth()
            sample_rate = source.getframerate()
    except (EOFError, wave.Error):
        return audio
    if not pcm:
        return audio
    return _pcm_to_wav(pcm, channels, sample_width, sample_rate)


def _elevenlabs_wav_bytes(audio: bytes, output_format: str) -> bytes:
    if audio.startswith(b"RIFF") and b"WAVE" in audio[:16]:
        return _normalize_wav_bytes(audio)
    if output_format.startswith("pcm_"):
        try:
            sample_rate = int(output_format.split("_", 1)[1])
        except (IndexError, ValueError):
            sample_rate = 24000
        return _pcm_to_wav(audio, channels=1, sample_width=2, sample_rate=sample_rate)
    return audio


def _has_speakable_content(text: str) -> bool:
    return any(character.isalnum() for character in text)


def _http_error_message(
    provider: str,
    response: aiohttp.ClientResponse,
    body: str,
    context: str,
) -> str:
    detail = body.strip() or response.reason or "empty response body"
    return f"{provider} failed ({response.status}; {context}): {detail}"
