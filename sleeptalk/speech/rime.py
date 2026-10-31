"""Rime TTS via its streaming HTTP endpoint with WAV output."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import aiohttp

from sleeptalk.providers import ProviderCapabilities, ProviderSpec
from sleeptalk.speech.common import SpeechError, _has_speakable_content, _http_error_message
from sleeptalk.media_audio import decode_audio_bytes
from sleeptalk.speech.lexicon import apply_pronunciation
from sleeptalk.speech.temp_audio import write_tts_temp_file
from sleeptalk.speech_speed import wire_speed


class RimeTTS:
    capabilities = ProviderCapabilities()

    def __init__(self, spec: ProviderSpec, *, api_key: str) -> None:
        self.spec = spec
        self.api_key = api_key

    async def warmup(self) -> None:
        await self.synthesize_wav_bytes("Ready.")

    async def synthesize_wav_bytes(self, text: str) -> bytes:
        if not self.api_key.strip():
            raise SpeechError("Missing RIME_API_KEY.")
        if not self.spec.voice_id.strip():
            raise SpeechError("Missing RIME_VOICE_ID.")
        if not _has_speakable_content(text):
            raise SpeechError("Rime TTS transcript was empty or punctuation-only.")
        payload = {
            "speaker": self.spec.voice_id,
            "text": apply_pronunciation(text, getattr(self.spec, "language", "") or ""),
            "modelId": self.spec.model or "mistv3",
            "samplingRate": 24000,
        }
        if self.spec.language:
            payload["lang"] = self.spec.language.split("-", 1)[0]
        # Mist and Mist v2 read speedAlpha backwards; the profile reciprocates.
        if self.spec.speed is not None and self.spec.speed > 0:
            speed = wire_speed("rime", self.spec.speed, model=payload["modelId"])
            if speed != 1.0:
                payload["speedAlpha"] = speed
        headers = {
            "Accept": "audio/wav",
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        async with aiohttp.ClientSession() as session, session.post(
            "https://users.rime.ai/v1/rime-tts", json=payload, headers=headers
        ) as response:
            if response.status != 200:
                body = await response.text()
                raise SpeechError(_http_error_message(
                    "Rime TTS", response, body,
                    f"model={payload['modelId']}, voice={self.spec.voice_id}",
                ))
            return await response.read()

    async def synthesize_wav(self, text: str) -> Path:
        return write_tts_temp_file(await self.synthesize_wav_bytes(text), prefix="rime")

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        yield decode_audio_bytes(await self.synthesize_wav_bytes(text))

    async def stream_pcm(self, text: str) -> AsyncIterator[bytes]:
        yield decode_audio_bytes(await self.synthesize_wav_bytes(text))
