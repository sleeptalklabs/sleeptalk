"""PlayHT/PlayAI TTS via the documented HTTP streaming endpoint."""

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


class PlayHTTTS:
    capabilities = ProviderCapabilities()

    def __init__(self, spec: ProviderSpec, *, api_key: str, user_id: str) -> None:
        self.spec = spec
        self.api_key = api_key
        self.user_id = user_id

    async def warmup(self) -> None:
        await self.synthesize_wav_bytes("Ready.")

    async def synthesize_wav_bytes(self, text: str) -> bytes:
        if not self.api_key.strip():
            raise SpeechError("Missing PLAYHT_API_KEY.")
        if not self.user_id.strip():
            raise SpeechError("Missing PLAYHT_USER_ID.")
        if not self.spec.voice_id.strip():
            raise SpeechError("Missing PLAYHT_VOICE_ID.")
        if not _has_speakable_content(text):
            raise SpeechError("PlayHT TTS transcript was empty or punctuation-only.")
        payload = {
            "text": apply_pronunciation(text, getattr(self.spec, "language", "") or ""),
            "voice": self.spec.voice_id,
            "voice_engine": self.spec.model or "Play3.0-mini",
            "output_format": "wav",
            "sample_rate": 24000,
            "speed": wire_speed("playht", self.spec.speed, model=self.spec.model),
        }
        headers = {
            "Authorization": self.api_key,
            "X-USER-ID": self.user_id,
            "Accept": "audio/wav",
            "Content-Type": "application/json",
        }
        async with aiohttp.ClientSession() as session, session.post(
            "https://api.play.ht/api/v2/tts/stream", json=payload, headers=headers
        ) as response:
            if response.status != 200:
                body = await response.text()
                raise SpeechError(_http_error_message(
                    "PlayHT TTS", response, body,
                    f"model={payload['voice_engine']}, voice={self.spec.voice_id}",
                ))
            return await response.read()

    async def synthesize_wav(self, text: str) -> Path:
        return write_tts_temp_file(await self.synthesize_wav_bytes(text), prefix="playht")

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        yield decode_audio_bytes(await self.synthesize_wav_bytes(text))

    async def stream_pcm(self, text: str) -> AsyncIterator[bytes]:
        yield decode_audio_bytes(await self.synthesize_wav_bytes(text))
