"""ElevenLabs TTS provider, extracted from discord_agent (spec + api_key construction)."""

from __future__ import annotations

import asyncio
import uuid
from urllib.parse import quote
from collections.abc import AsyncIterator
from pathlib import Path

import aiohttp

from sleeptalk.metering import UsageMeter
from sleeptalk.providers import ProviderCapabilities, ProviderSpec, provider_http_url
from sleeptalk.speech.common import (
    SpeechError,
    _elevenlabs_wav_bytes,
    _has_speakable_content,
    _http_error_message,
)
from sleeptalk.media_audio import decode_audio_bytes
from sleeptalk.speech.lexicon import apply_pronunciation
from sleeptalk.speech_speed import wire_speed
from sleeptalk.speech.temp_audio import write_tts_temp_file


def _primary_language(language: str) -> str:
    return (language or "").strip().lower().replace("_", "-").split("-", 1)[0]


def accepts_language_code(model: str) -> bool:
    """ElevenLabs enforces ``language_code`` only on the v2.5 Flash/Turbo models."""
    return "v2_5" in (model or "").strip().lower()


def voice_path(voice_id: str) -> str:
    """The synthesis path for one voice: the ID is a single, percent-encoded
    segment, so a crafted value can't reach another endpoint."""
    return "/v1/text-to-speech/" + quote(voice_id, safe="")


class ElevenLabsTTS:
    capabilities = ProviderCapabilities(
        supports_streaming=False,
        supports_word_timings=False,
        supports_interruption=False,
    )

    def __init__(self, spec: ProviderSpec, *, api_key: str) -> None:
        self.spec = spec
        self.api_key = api_key
        self.usage_meter: UsageMeter | None = None

    def set_usage_meter(self, meter: UsageMeter) -> None:
        self.usage_meter = meter

    async def warmup(self) -> None:
        await self.synthesize_wav_bytes("Ready.", purpose="warmup")

    async def synthesize_wav_bytes(self, text: str, *, purpose: str = "") -> bytes:
        if not self.api_key.strip():
            raise SpeechError("Missing ELEVENLABS_API_KEY.")
        if not self.spec.voice_id.strip():
            raise SpeechError("Missing ELEVENLABS_VOICE_ID.")
        if not _has_speakable_content(text):
            raise SpeechError("ElevenLabs TTS transcript was empty or punctuation-only.")

        output_format = str(self.spec.options.get("output_format") or "wav_24000")
        payload = {
            "text": apply_pronunciation(text, getattr(self.spec, "language", "") or ""),
            "model_id": self.spec.model,
        }
        # multilingual_v2 detects the language from the text and rejects a
        # hint; Flash and Turbo v2.5 take one and pronounce better with it.
        language = _primary_language(self.spec.language)
        if language and accepts_language_code(self.spec.model):
            payload["language_code"] = language
        if self.spec.speed is not None and self.spec.speed > 0:
            payload["voice_settings"] = {
                "speed": wire_speed("elevenlabs", self.spec.speed, model=self.spec.model)
            }
        headers = {
            "xi-api-key": self.api_key,
            "Content-Type": "application/json",
        }

        attempt_id = f"{uuid.uuid4().hex[:12]}:tts:rest:1"
        if self.usage_meter is not None:
            self.usage_meter.start_attempt(
                provider="elevenlabs",
                operation="tts.rest",
                attempt_id=attempt_id,
                metadata={"purpose": purpose} if purpose else None,
                inputCharacters=len(payload["text"]),
            )
        try:
            async with aiohttp.ClientSession() as session, session.post(
                provider_http_url(
                    self.spec,
                    "https://api.elevenlabs.io",
                    voice_path(self.spec.voice_id),
                ),
                params={"output_format": output_format},
                json=payload,
                headers=headers,
            ) as response:
                if response.status != 200:
                    body = await response.text()
                    raise SpeechError(
                        _http_error_message(
                            "ElevenLabs TTS",
                            response,
                            body,
                            f"model={payload['model_id']}, voice={self.spec.voice_id}",
                        )
                    )
                audio = await response.read()
        except asyncio.CancelledError:
            if self.usage_meter is not None:
                self.usage_meter.finish_attempt(attempt_id, status="cancelled")
            raise
        except Exception:
            if self.usage_meter is not None:
                self.usage_meter.finish_attempt(attempt_id, status="error")
            raise
        if self.usage_meter is not None:
            self.usage_meter.add_usage(attempt_id, outputAudioBytes=len(audio))
            self.usage_meter.finish_attempt(attempt_id, status="ok")

        return _elevenlabs_wav_bytes(audio, output_format)

    async def synthesize_wav(self, text: str) -> Path:
        return write_tts_temp_file(
            await self.synthesize_wav_bytes(text),
            prefix="elevenlabs",
        )

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        yield decode_audio_bytes(await self.synthesize_wav_bytes(text))

    async def stream_pcm(self, text: str) -> AsyncIterator[bytes]:
        yield decode_audio_bytes(await self.synthesize_wav_bytes(text))
