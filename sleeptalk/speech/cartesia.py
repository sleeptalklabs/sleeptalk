"""Cartesia speech with streamed 48 kHz mono PCM and in-memory WAV clips."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import aclosing
from pathlib import Path

import aiohttp

from sleeptalk.metering import UsageMeter
from sleeptalk.providers import ProviderCapabilities, ProviderSpec, provider_http_url
from sleeptalk.speech.common import (
    SpeechError,
    _has_speakable_content,
    _http_error_message,
    _pcm_to_wav,
)
from sleeptalk.speech.lexicon import apply_pronunciation
from sleeptalk.speech.temp_audio import write_tts_temp_file
from sleeptalk.speech_speed import wire_speed

_SAMPLE_RATE = 48_000
_CHUNK_BYTES = 1_920  # 20 ms of mono signed 16-bit PCM.


def _primary_language(language: str) -> str:
    return (language or "").strip().lower().replace("_", "-").split("-", 1)[0]


class CartesiaTTS:
    capabilities = ProviderCapabilities(
        supports_streaming=True,
        supports_word_timings=False,
        supports_interruption=True,
    )

    def __init__(self, spec: ProviderSpec, *, api_key: str) -> None:
        self.spec = spec
        self.api_key = api_key
        self.usage_meter: UsageMeter | None = None
        self._http: aiohttp.ClientSession | None = None
        self._closed = False

    def set_usage_meter(self, meter: UsageMeter) -> None:
        self.usage_meter = meter

    def _session(self) -> aiohttp.ClientSession:
        if self._closed:
            raise SpeechError("Cartesia TTS is closed.")
        if self._http is None:
            self._http = aiohttp.ClientSession()
        return self._http

    async def warmup(self) -> None:
        self._session()

    async def close(self) -> None:
        self._closed = True
        if self._http is not None:
            await self._http.close()

    async def synthesize_wav_bytes(self, text: str) -> bytes:
        async with aclosing(self.stream_pcm(text)) as stream:
            pcm = b"".join([chunk async for chunk in stream])
        return _pcm_to_wav(pcm, channels=1, sample_width=2, sample_rate=_SAMPLE_RATE)

    async def synthesize_wav(self, text: str) -> Path:
        return write_tts_temp_file(
            await self.synthesize_wav_bytes(text),
            prefix="cartesia",
        )

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        async with aclosing(self.stream_pcm(text)) as stream:
            async for pcm in stream:
                yield pcm

    async def stream_pcm(self, text: str) -> AsyncIterator[bytes]:
        """Yield sample-aligned 48 kHz mono PCM, at most 20 ms per chunk.

        Each iterator owns its HTTP response. Closing it cancels that synthesis
        while leaving the session's connection pool available to the next turn.
        """
        if not self.api_key.strip():
            raise SpeechError("Missing CARTESIA_API_KEY.")
        if not self.spec.voice_id.strip():
            raise SpeechError("Missing CARTESIA_VOICE_ID.")
        if not _has_speakable_content(text):
            raise SpeechError("Cartesia TTS transcript was empty or punctuation-only.")

        payload = {
            "model_id": self.spec.model,
            "transcript": apply_pronunciation(text, getattr(self.spec, "language", "") or ""),
            "voice": {"id": self.spec.voice_id},
            "output_format": {
                "container": "raw",
                "encoding": "pcm_s16le",
                "sample_rate": _SAMPLE_RATE,
            },
            "generation_config": {
                "speed": wire_speed("cartesia", self.spec.speed, model=self.spec.model)
            },
        }
        # Sonic pronounces by language; without it a French voice reads
        # French text with English phonemes. Primary subtag only ("fr").
        language = _primary_language(self.spec.language)
        if language:
            payload["language"] = language
        headers = {
            "Cartesia-Version": "2026-03-01",
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        session = self._session()
        attempt_id = f"{uuid.uuid4().hex[:12]}:tts:stream:1"
        if self.usage_meter is not None:
            self.usage_meter.start_attempt(
                provider="cartesia",
                operation="tts.stream",
                attempt_id=attempt_id,
                inputCharacters=len(text),
            )
        status = "cancelled"
        try:
            async with session.post(
                provider_http_url(self.spec, "https://api.cartesia.ai", "/tts/bytes"),
                json=payload,
                headers=headers,
            ) as response:
                if response.status != 200:
                    body = await response.text()
                    raise SpeechError(
                        _http_error_message(
                            "Cartesia TTS",
                            response,
                            body,
                            f"model={self.spec.model}, voice={self.spec.voice_id}",
                        )
                    )
                tail = b""
                received = False
                async for chunk in response.content.iter_chunked(_CHUNK_BYTES):
                    if self.usage_meter is not None:
                        self.usage_meter.add_usage(attempt_id, outputAudioBytes=len(chunk))
                    data = tail + chunk
                    aligned = len(data) - len(data) % 2
                    tail = data[aligned:]
                    if aligned:
                        received = True
                        yield data[:aligned]
                if tail:
                    raise SpeechError("Cartesia TTS returned a truncated PCM sample.")
                if not received:
                    raise SpeechError("Cartesia TTS returned no audio.")
            status = "ok"
        except Exception:
            status = "error"
            raise
        finally:
            if self.usage_meter is not None:
                self.usage_meter.finish_attempt(attempt_id, status=status)
