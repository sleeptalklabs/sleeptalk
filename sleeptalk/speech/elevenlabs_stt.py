"""ElevenLabs Scribe REST fallback for realtime calls and explicit batch models."""

from __future__ import annotations

import asyncio
import uuid

import aiohttp

from sleeptalk.providers import ProviderSpec, provider_http_url
from sleeptalk.speech.common import SpeechError, _http_error_message, _pcm_to_wav
from sleeptalk.metering import UsageMeter
from sleeptalk.speech.batch_preview_stt import BatchPreviewMixin

DEFAULT_MODEL = "scribe_v2"
DEFAULT_BASE = "https://api.elevenlabs.io"
_SAMPLE_RATE = 48_000


def batch_model(model: str) -> str:
    return DEFAULT_MODEL if not model or model == "scribe_v2_realtime" else model


def _primary_language(language: str) -> str:
    """"en-US" -> "en"; Scribe takes ISO 639-1 or 639-3, not a region."""

    return (language or "").strip().split("-")[0].lower()


def transcription_form(pcm: bytes, *, model: str, language: str) -> aiohttp.FormData:
    """The multipart body for one utterance; timestamps and diarization off for speed."""

    form = aiohttp.FormData()
    form.add_field("file", _pcm_to_wav(pcm, 1, 2, _SAMPLE_RATE), filename="utterance.wav", content_type="audio/wav")
    form.add_field("model_id", model)
    form.add_field("diarize", "false")
    form.add_field("timestamps_granularity", "none")
    form.add_field("tag_audio_events", "false")
    tag = _primary_language(language)
    if tag:
        form.add_field("language_code", tag)
    return form


class ElevenLabsSTT(BatchPreviewMixin):
    sample_rate = _SAMPLE_RATE
    channels = 1
    sample_width = 2

    def __init__(self, spec: ProviderSpec, *, api_key: str) -> None:
        self._init_previews()
        self.spec = spec
        self.api_key = api_key
        self.usage_meter: UsageMeter | None = None

    def set_usage_meter(self, meter: UsageMeter) -> None:
        self.usage_meter = meter

    async def warmup(self) -> None:
        silence = b"\x00" * int(_SAMPLE_RATE * 2 * 100 / 1000)
        await self.transcribe_pcm(silence, purpose="warmup")

    async def transcribe_pcm(self, pcm: bytes, *, purpose: str = "") -> str:
        if not self.api_key.strip():
            raise SpeechError("Missing ELEVENLABS_API_KEY.")
        if not pcm:
            return ""
        model = batch_model(self.spec.model)
        language = _primary_language(self.spec.language)
        form = transcription_form(pcm, model=model, language=self.spec.language)
        timeout = aiohttp.ClientTimeout(total=float(self.spec.options.get("rest_timeout_seconds") or 8.0))
        attempt_id = f"{uuid.uuid4().hex[:12]}:stt:rest:1"
        if self.usage_meter is not None:
            self.usage_meter.start_attempt(
                provider="elevenlabs",
                operation="stt.rest",
                attempt_id=attempt_id,
                metadata={"purpose": purpose} if purpose else None,
                audioMs=len(pcm) / (_SAMPLE_RATE * 2 / 1000),
                audioBytes=len(pcm),
            )
        try:
            async with aiohttp.ClientSession() as session, session.post(
                provider_http_url(self.spec, DEFAULT_BASE, "/v1/speech-to-text"),
                data=form,
                headers={"xi-api-key": self.api_key},
                timeout=timeout,
            ) as response:
                if response.status != 200:
                    body = await response.text()
                    raise SpeechError(
                        _http_error_message("ElevenLabs STT", response, body, f"model={model}, language={language}")
                    )
                payload = await response.json()
        except (Exception, asyncio.CancelledError) as exc:
            if self.usage_meter is not None:
                self.usage_meter.finish_attempt(
                    attempt_id, status="cancelled" if isinstance(exc, asyncio.CancelledError) else "error")
            raise
        if self.usage_meter is not None:
            self.usage_meter.finish_attempt(attempt_id, status="ok")
        text = payload.get("text") if isinstance(payload, dict) else None
        return str(text or "").strip()

    async def close(self) -> None:
        await self._close_previews()
