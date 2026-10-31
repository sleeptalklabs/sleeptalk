"""Hume Octave catalog-voice TTS."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import aiohttp

from sleeptalk.providers import ProviderCapabilities, ProviderSpec
from sleeptalk.speech.common import SpeechError, _has_speakable_content, _http_error_message
from sleeptalk.media_audio import decode_audio_bytes
from sleeptalk.speech.lexicon import apply_pronunciation
from sleeptalk.speech_speed import wire_speed
from sleeptalk.speech.temp_audio import write_tts_temp_file


class HumeTTS:
    capabilities = ProviderCapabilities()

    def __init__(self, spec: ProviderSpec, *, api_key: str) -> None:
        self.spec = spec
        self.api_key = api_key

    async def warmup(self) -> None:
        await self.synthesize_wav_bytes("Ready.")

    async def synthesize_wav_bytes(self, text: str) -> bytes:
        if not self.api_key.strip():
            raise SpeechError("Missing HUME_API_KEY.")
        if not self.spec.voice_id.strip():
            raise SpeechError("Missing HUME_VOICE_ID.")
        if not _has_speakable_content(text):
            raise SpeechError("Hume TTS transcript was empty or punctuation-only.")
        payload = {
            "utterances": [{
                "text": apply_pronunciation(text, getattr(self.spec, "language", "") or ""),
                "voice": {"id": self.spec.voice_id, "provider": "HUME_AI"},
                "speed": wire_speed("hume", self.spec.speed, model=self.spec.model),
            }],
            "format": {"type": "wav"},
            "num_generations": 1,
        }
        async with aiohttp.ClientSession() as session, session.post(
            "https://api.hume.ai/v0/tts/file",
            json=payload,
            headers={"X-Hume-Api-Key": self.api_key, "Content-Type": "application/json"},
        ) as response:
            if response.status != 200:
                body = await response.text()
                raise SpeechError(_http_error_message(
                    "Hume TTS", response, body, f"voice={self.spec.voice_id}"
                ))
            return await response.read()

    async def synthesize_wav(self, text: str) -> Path:
        return write_tts_temp_file(await self.synthesize_wav_bytes(text), prefix="hume")

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        yield decode_audio_bytes(await self.synthesize_wav_bytes(text))

    async def stream_pcm(self, text: str) -> AsyncIterator[bytes]:
        yield decode_audio_bytes(await self.synthesize_wav_bytes(text))
