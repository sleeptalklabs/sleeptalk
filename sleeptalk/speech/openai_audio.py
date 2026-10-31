"""Listening and speaking through an OpenAI-compatible audio API.

OpenRouter, Groq, Together, and Venice all expose the same two routes an
OpenAI key does: ``POST /audio/transcriptions`` (one utterance in, text out)
and ``POST /audio/speech`` (text in, audio out). One key covers both, and
for people whose agent already uses one of these services it is a key they
already have. This module is one adapter with a preset per service.

Listening is batch: the utterance is posted once Sleeptalk's own endpointing
decides the turn has ended, the same way Local speech works, so a turn
lands a little later than with a streaming socket. Speaking asks for PCM
where the service offers it and decodes anything else.
"""
from __future__ import annotations

import asyncio
import json
import re
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp

from sleeptalk.media_audio import AudioDecodeError, decode_audio_bytes
from sleeptalk.metering import UsageMeter
from sleeptalk.providers import ProviderCapabilities, ProviderSpec, provider_http_url
from sleeptalk.speech.common import SpeechError, _has_speakable_content, _http_error_message, _pcm_to_wav
from sleeptalk.speech.lexicon import apply_pronunciation
from sleeptalk.speech.temp_audio import write_tts_temp_file

_SAMPLE_RATE = 48_000
# Whisper-class models are trained on 16 kHz; uploading anything wider is
# three times the bytes for the same words.
_UPLOAD_RATE = 16_000
# Venice's Wizper answered 422 to FLAC and to WAV sent as "audio/wav", and
# transcribed the same WAV sent as "audio/x-wav" (tested 2026-10-04).
_UPLOAD_OVERRIDES: dict[tuple[str, str], str] = {("venice", "fal-ai/wizper"): "audio/x-wav"}
_MAX_AUDIO_BYTES = 32 * 1024 * 1024
_CHUNK_BYTES = 1_920  # 20 ms of mono signed 16-bit PCM at 48 kHz, what playout wants.
_WAV_HEADER = 44


class PcmResampler:
    """Streaming mono int16 resampler: feed chunks as they arrive, flush at the end."""

    def __init__(self, source_rate: int, target_rate: int = _SAMPLE_RATE) -> None:
        import av

        self.source_rate = source_rate
        self.target_rate = target_rate
        self._resampler = av.AudioResampler(format="s16", layout="mono", rate=target_rate)
        self._pending = b""
        self._pts = 0

    def feed(self, chunk: bytes) -> bytes:
        if self.source_rate == self.target_rate:
            return chunk
        import av

        data = self._pending + chunk
        usable = len(data) - len(data) % 2
        self._pending = data[usable:]
        data = data[:usable]
        if not data:
            return b""
        frame = av.AudioFrame(format="s16", layout="mono", samples=len(data) // 2)
        frame.sample_rate = self.source_rate
        frame.pts = self._pts
        self._pts += len(data) // 2
        frame.planes[0].update(data)
        return b"".join(bytes(out.planes[0])[: out.samples * 2] for out in self._resampler.resample(frame))

    def flush(self) -> bytes:
        if self.source_rate == self.target_rate:
            return b""
        return b"".join(bytes(out.planes[0])[: out.samples * 2] for out in self._resampler.resample(None))


def resample_pcm(pcm: bytes, source_rate: int, target_rate: int) -> bytes:
    resampler = PcmResampler(source_rate, target_rate)
    return resampler.feed(pcm) + resampler.flush()


def _wav_format(header: bytes) -> tuple[int, int, int] | None:
    """(channels, sample_rate, sample_width) from a canonical 44-byte RIFF header, else None."""
    if len(header) < _WAV_HEADER or header[:4] != b"RIFF" or header[8:12] != b"WAVE" or header[12:16] != b"fmt ":
        return None
    channels = int.from_bytes(header[22:24], "little")
    rate = int.from_bytes(header[24:28], "little")
    width = int.from_bytes(header[34:36], "little") // 8
    return (channels, rate, width) if channels in (1, 2) and 8_000 <= rate <= 192_000 and width == 2 else None


def _to_mono(pcm: bytes, channels: int) -> bytes:
    if channels == 1:
        return pcm
    import numpy as np

    frames = np.frombuffer(pcm[: len(pcm) - len(pcm) % 4], dtype=np.int16).reshape(-1, 2)
    return frames.mean(axis=1).astype(np.int16).tobytes()


@dataclass(frozen=True)
class Preset:
    """One service's addresses and defaults. Everything here is public."""

    name: str
    label: str
    base_url: str
    env: str
    console: str
    stt_model: str
    tts_model: str
    tts_voice: str
    # What the speech route returns when asked: "pcm" (raw 16-bit mono at
    # ``pcm_rate``) or a container ("wav", "mp3") the decoder handles.
    tts_format: str = "pcm"
    pcm_rate: int = 24_000
    # Asked again when the listening model returns nothing for audio with a
    # voice in it (Parakeet on OpenRouter does, about one turn in fifteen).
    stt_empty_retry_model: str = ""
    # A second voice on another model, for the profile's fallback list.
    tts_fallback_model: str = ""
    tts_fallback_voice: str = ""
    extra_headers: dict[str, str] = field(default_factory=dict)
    # Fields added to every speech request. Venice renders the whole reply
    # before sending a byte unless asked to stream: first audio for three
    # sentences was 1.5 s on Grok and 32 s on Kokoro, against 0.5 s and 1.2 s.
    tts_body: dict[str, object] = field(default_factory=dict)
    # Voice families that imply a model, so a voice picked from the menu
    # switches the model with it (OpenRouter routes many vendors).
    voice_models: tuple[tuple[str, str], ...] = ()
    # What a user reads on the key page.
    free_line: str = ""
    card_line: str = ""


PRESETS: dict[str, Preset] = {
    "openrouter": Preset(
        name="openrouter",
        label="OpenRouter",
        base_url="https://openrouter.ai/api/v1",
        env="OPENROUTER_API_KEY",
        console="https://openrouter.ai/keys",
        # Measured 2026-09-22 on the bundled clip: Parakeet 331 ms round trip
        # against 470 ms for Whisper turbo; Grok Voice first audio 428 ms with
        # the sentence done in 1 s, against 515 ms and 2.9 s for the free Flux
        # voice, which stays on the menu as the free pick.
        stt_model="nvidia/parakeet-tdt-0.6b-v3",
        stt_empty_retry_model="openai/whisper-large-v3-turbo",
        tts_model="x-ai/grok-voice-tts-1.0",
        tts_voice="eve",
        tts_format="pcm",
        pcm_rate=24_000,
        tts_fallback_model="deepgram/flux-tts:free",
        tts_fallback_voice="flux-hannah-en",
        extra_headers={"HTTP-Referer": "https://github.com/sleeptalklabs/sleeptalk", "X-Title": "Sleeptalk"},
        voice_models=(
            (r"^flux-[a-z]+-[a-z]{2}$", "deepgram/flux-tts:free"),
            (r"^aura-2-[a-z]+-[a-z]{2}$", "deepgram/aura-2"),
            (r":MAI-Voice-2$", "microsoft/mai-voice-2-flash"),
            (r"^(eve|ara|rex|sal|leo)$", "x-ai/grok-voice-tts-1.0"),
            (r"^[a-z]{2}_[a-z]+$", "hexgrad/kokoro-82m"),
            (r"^(tara|leah|jess|dan|mia|zac)$", "canopylabs/orpheus-3b-0.1-ft"),
            (r"^(jessica|eric|sarah|george|adam|alice|bella|bill|brian|callum|charlie|chris|daniel|harry|laura"
             r"|liam|lily|matilda|river|roger|will)$", "elevenlabs/eleven-turbo-v2.5"),
        ),
        free_line="Listening for cents an hour; Grok voices about a dollar an hour of speech, or the Flux voices free.",
        card_line="Uses the balance you already have; no separate sign-up.",
    ),
    "groq": Preset(
        name="groq",
        label="Groq",
        base_url="https://api.groq.com/openai/v1",
        env="GROQ_API_KEY",
        console="https://console.groq.com/keys",
        stt_model="whisper-large-v3-turbo",
        tts_model="canopylabs/orpheus-v1-english",
        tts_voice="troy",
        tts_format="wav",
        free_line="Whisper listening for a few cents an hour.",
        card_line="Uses your Groq account.",
    ),
    "together": Preset(
        name="together",
        label="Together",
        base_url="https://api.together.xyz/v1",
        env="TOGETHER_API_KEY",
        console="https://api.together.xyz/settings/api-keys",
        # Parakeet measured fastest on OpenRouter; Together lists the same model.
        stt_model="nvidia/parakeet-tdt-0.6b-v3",
        # Cartesia's own Sonic through Together takes Cartesia voice IDs: the
        # same golden voice as a direct Cartesia account. Together lists Sonic
        # 3 but not 3.6; Sonic 2 retires on 2026-10-20.
        tts_model="cartesia/sonic-3",
        tts_voice="db6b0ed5-d5d3-463d-ae85-518a07d3c2b4",
        tts_format="wav",
        voice_models=(
            (r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", "cartesia/sonic-3"),
            (r"^(dan|jess|leah|leo|mia|tara|zac|zoe)$", "canopylabs/orpheus-3b-0.1-ft"),
            (r"^[a-z]{2}_[a-z]+$", "hexgrad/Kokoro-82M"),
        ),
        free_line="Parakeet listening for cents an hour; Cartesia voices at a few dollars an hour of speech.",
        card_line="Uses your Together account.",
    ),
    "venice": Preset(
        name="venice",
        label="Venice",
        base_url="https://api.venice.ai/api/v1",
        env="VENICE_API_KEY",
        console="https://venice.ai/settings/api",
        # The same pair as OpenRouter's defaults: Venice lists both.
        stt_model="nvidia/parakeet-tdt-0.6b-v3",
        # Grok Voice: natural, low latency, raw PCM; Kokoro stays as the budget pick.
        tts_model="tts-xai-v1",
        tts_voice="eve",
        tts_format="pcm",
        pcm_rate=24_000,
        tts_body={"streaming": True},
        voice_models=(
            (r"^(altair|ara|atlas|carina|castor|celeste|cosmo|eve|helios|helix|iris|kepler|leo|lumen|luna|lux|naksh|orion|perseus|rex|rigel|sal|sirius|ursa|zagan|zenith)$", "tts-xai-v1"),
            (r"^(dan|jess|leah|leo|mia|tara|zac|zoe)$", "tts-orpheus"),
            (r"^[a-z]{2}_[a-z]+$", "tts-kokoro"),
        ),
        free_line="Parakeet listening and Grok voices from your prepaid credits, about a dollar an hour of speech.",
        card_line="Uses your Venice balance.",
    ),
}


def preset_for(provider: str) -> Preset:
    try:
        return PRESETS[provider.strip().lower()]
    except KeyError:
        raise SpeechError(f"{provider!r} is not an OpenAI-compatible audio service Sleeptalk knows.") from None


def is_preset(provider: str) -> bool:
    return provider.strip().lower() in PRESETS


def model_for_voice(preset: Preset, voice_id: str, configured: str) -> str:
    """The model to speak with: the configured one when the voice belongs to it.

    A model the profile chose on purpose (anything but the preset default)
    wins when the catalog lists the voice under it, or when it takes any
    voice ID. Otherwise, as for older profiles that only name a voice, the
    voice family picks the model (a Flux voice speaks through Flux) and the
    configured model is the fallback.
    """
    configured = (configured or "").strip()
    if configured and configured != preset.tts_model:
        from sleeptalk import speech_catalog

        option = speech_catalog.find(preset.name, speech_catalog.TTS, configured, cached=False)
        if option is None or option.owns_voice(voice_id) is not False:
            return configured
    for pattern, model in preset.voice_models:
        if re.search(pattern, voice_id):
            return model
    return configured or preset.tts_model


def sends_language_hint(provider: str, model: str, options: dict | None = None, language: str = "") -> bool:
    """False for models that detect the language themselves (mixed Chinese and
    English among them), for a language whose hint makes the model worse
    (Whisper told "zh" writes English words in Chinese), or when the profile
    sets ``language_hint = "auto"``."""
    hint = str((options or {}).get("language_hint") or "").strip().lower()
    if hint in {"auto", "off", "none"}:
        return False
    from sleeptalk import speech_catalog

    option = speech_catalog.find(provider, speech_catalog.STT, model, cached=False)
    if option is None:
        return True
    return not (option.detects_language or _primary_language(language) in option.no_hint_for)


def _primary_language(language: str) -> str:
    return (language or "").strip().split("-")[0].lower()


def _headers(preset: Preset, api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}", **preset.extra_headers}


# A request that hasn't answered by then gets a twin; the first answer wins.
# Measured 2026-09-26 on OpenRouter with a warm connection: Parakeet answers
# in 220-400 ms and Grok's first audio comes in 260-350 ms, but about one
# request in six stalls for 0.8-1.7 s. The twin costs only in that case.
_STT_HEDGE_MS = 700
_TTS_HEDGE_MS = 600
# Whisper's stock output for audio it can't make words of.
_PHANTOM_TRANSCRIPTS = {"", "you", "thank you", "thanks", "thank you for watching", "thanks for watching",
                        "bye", "okay", "so", "the end"}


def _hedge_seconds(spec: ProviderSpec, default_ms: int) -> float | None:
    value = spec.options.get("hedge_after_ms", default_ms)
    try:
        ms = int(value)
    except (TypeError, ValueError):
        ms = default_ms
    return ms / 1000 if ms > 0 else None


async def _first_success(start, hedge_s: float | None, discard=None):
    """Run ``start()``; if it hasn't finished after ``hedge_s``, run it again
    and take whichever succeeds first. ``discard`` cleans up a result that
    finished but lost."""
    first = asyncio.ensure_future(start())
    if hedge_s is None:
        return await first
    pending = {first}
    try:
        done, _ = await asyncio.wait(pending, timeout=hedge_s)
        if not done:
            pending.add(asyncio.ensure_future(start()))
        errors: list[BaseException] = []
        while pending:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            winner = next((task for task in done if not task.cancelled() and task.exception() is None), None)
            for task in done:
                if task is not winner and not task.cancelled() and task.exception() is None and discard is not None:
                    await discard(task.result())
                elif task is not winner and not task.cancelled():
                    errors.append(task.exception())
            if winner is not None:
                return winner.result()
        raise errors[0]
    finally:
        for task in pending:
            task.cancel()
        if pending:
            results = await asyncio.gather(*pending, return_exceptions=True)
            if discard is not None:
                for result in results:
                    if not isinstance(result, BaseException):
                        await discard(result)


def _window_rms(pcm: bytes, rate: int) -> list[int]:
    import audioop

    step = rate // 50 * 2  # 20 ms of mono s16
    return [audioop.rms(pcm[i:i + step], 2) for i in range(0, len(pcm) - step + 1, step)]


def trim_quiet_ends(pcm: bytes, rate: int, *, keep_ms: int = 250, probe_ms: int = 200) -> bytes:
    """Drop the silence at each end, keeping ``keep_ms`` of it.

    Quiet means no louder than the quieter end of the recording (an utterance
    starts and ends in room noise), with headroom, and never close to speech
    level. A soft syllable is louder than that and stays.
    """
    windows = _window_rms(pcm, rate)
    probe = max(1, probe_ms // 20)
    if len(windows) < probe * 3:
        return pcm
    floor = min(max(windows[:probe]), max(windows[-probe:]))
    speech = sorted(windows)[int(len(windows) * 0.9)]
    ceiling = min(max(50, int(floor * 1.5)), max(50, speech // 4))
    start = next((i for i, w in enumerate(windows) if w > ceiling), None)
    if start is None:
        return pcm
    end = len(windows) - next(i for i, w in enumerate(reversed(windows)) if w > ceiling)
    keep = keep_ms // 20
    step = rate // 50 * 2
    return pcm[max(0, start - keep) * step: min(len(windows), end + keep) * step]


def _has_voice(pcm: bytes, rate: int = _SAMPLE_RATE) -> bool:
    """Whether a recording holds something louder than its own quiet: a voice."""
    windows = sorted(_window_rms(pcm, rate))
    if len(windows) < 5:
        return False
    return windows[int(len(windows) * 0.9)] >= max(300, 4 * windows[len(windows) // 10])


def _flac(pcm: bytes, rate: int) -> bytes | None:
    """Lossless and about 40% smaller than WAV; None if this build can't encode it."""
    try:
        import io

        import av
        import numpy as np

        buffer = io.BytesIO()
        with av.open(buffer, mode="w", format="flac") as container:
            stream = container.add_stream("flac", rate=rate, layout="mono")
            frame = av.AudioFrame.from_ndarray(np.frombuffer(pcm, dtype=np.int16).reshape(1, -1),
                                               format="s16", layout="mono")
            frame.sample_rate = rate
            for packet in stream.encode(frame):
                container.mux(packet)
            for packet in stream.encode(None):
                container.mux(packet)
        return buffer.getvalue()
    except Exception:  # noqa: BLE001 — WAV always works
        return None


class _KeepAlive:
    """One HTTPS connection per adapter, reused across turns.

    A fresh TLS handshake per utterance and per sentence costs 100 to 300 ms
    each on a distant host; keeping the connection open pays it once, and
    ``warmup`` pays it before the first word rather than on it.
    """

    _session: aiohttp.ClientSession | None = None
    _last_used: float = 0.0

    def client(self) -> aiohttp.ClientSession:
        import time

        self._last_used = time.monotonic()
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(limit=4, keepalive_timeout=75, enable_cleanup_closed=True)
            self._session = aiohttp.ClientSession(connector=connector)
        return self._session

    async def keep_warm(self, idle_seconds: float = 20.0) -> None:
        """Reopen the connection if it sat idle long enough to have closed.

        Called when the person starts talking: the reconnect happens while
        they speak instead of in front of the reply.
        """
        import time

        if time.monotonic() - self._last_used >= idle_seconds:
            warmup = getattr(self, "warmup", None)
            if warmup is not None:
                await warmup()

    async def open_connection(self, url: str, headers: dict[str, str]) -> None:
        """A free request that leaves the connection open."""
        try:
            async with self.client().get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=8)) as response:
                await response.read()
        except Exception:  # noqa: BLE001 — warmup is a courtesy; the first turn will tell
            return

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None


def _cost(payload: object) -> float | None:
    usage = payload.get("usage") if isinstance(payload, dict) else None
    cost = usage.get("cost") if isinstance(usage, dict) else None
    return float(cost) if isinstance(cost, (int, float)) else None


class OpenAIAudioSTT(_KeepAlive):
    """Batch transcription of one utterance per request (``RESTSTT``)."""

    sample_rate = _SAMPLE_RATE
    channels = 1
    sample_width = 2

    def __init__(self, spec: ProviderSpec, *, api_key: str, preset: Preset | None = None) -> None:
        self.spec = spec
        # A preset passed in serves a service that isn't a speech provider on
        # its own (OpenAI, for Realtime's early transcripts).
        self.preset = preset or preset_for(spec.provider)
        self.api_key = api_key
        self.usage_meter: UsageMeter | None = None
        self.last_cost_usd: float | None = None

    def set_usage_meter(self, meter: UsageMeter) -> None:
        self.usage_meter = meter

    @property
    def model(self) -> str:
        return (self.spec.model or "").strip() or self.preset.stt_model

    async def warmup(self) -> None:
        # A real transcription costs money; opening the connection is free.
        if self.api_key.strip():
            await self.open_connection(provider_http_url(self.spec, self.preset.base_url, "/models"),
                                       _headers(self.preset, self.api_key))

    def transcription_form(self, pcm: bytes, model: str = "") -> aiohttp.FormData:
        form = aiohttp.FormData()
        audio = trim_quiet_ends(resample_pcm(pcm, _SAMPLE_RATE, _UPLOAD_RATE), _UPLOAD_RATE)
        model = model or self.model
        upload = _UPLOAD_OVERRIDES.get((self.preset.name, model))
        encoded = None
        if upload is None and self.spec.options.get("upload_format", "flac") == "flac":
            encoded = _flac(audio, _UPLOAD_RATE)
        if encoded is not None:
            form.add_field("file", encoded, filename="utterance.flac", content_type="audio/flac")
        else:
            form.add_field("file", _pcm_to_wav(audio, 1, 2, _UPLOAD_RATE), filename="utterance.wav",
                           content_type=upload or "audio/wav")
        form.add_field("model", model)
        form.add_field("response_format", "json")
        language = _primary_language(self.spec.language)
        if language and sends_language_hint(self.preset.name, model, self.spec.options, language):
            form.add_field("language", language)
        return form

    async def transcribe_pcm(self, pcm: bytes, *, purpose: str = "") -> str:
        """One utterance to text. A stalled request gets a twin after
        ``hedge_after_ms``; an empty answer for audio with a voice in it is
        asked once more of ``stt_empty_retry_model``."""
        if not self.api_key.strip():
            raise SpeechError(f"Missing {self.preset.env}.")
        if not pcm:
            return ""
        text = await _first_success(lambda: self._transcribe_once(pcm, self.model, purpose),
                                    _hedge_seconds(self.spec, _STT_HEDGE_MS))
        retry_model = str(self.spec.options.get("empty_retry_model", self.preset.stt_empty_retry_model) or "")
        if not text and retry_model and retry_model != self.model and _has_voice(pcm):
            again = await self._transcribe_once(pcm, retry_model, "empty_retry")
            if " ".join(re.findall(r"[a-z']+", again.lower())) not in _PHANTOM_TRANSCRIPTS:
                text = again
        return text

    async def _transcribe_once(self, pcm: bytes, model: str, purpose: str) -> str:
        timeout = aiohttp.ClientTimeout(total=float(self.spec.options.get("rest_timeout_seconds") or 12.0))
        attempt_id = f"{uuid.uuid4().hex[:12]}:stt:rest:1"
        if self.usage_meter is not None:
            self.usage_meter.start_attempt(
                provider=self.preset.name, operation="stt.rest", attempt_id=attempt_id,
                metadata={"purpose": purpose, "model": model} if purpose else {"model": model},
                audioMs=len(pcm) / (_SAMPLE_RATE * 2 / 1000), audioBytes=len(pcm),
            )
        try:
            async with self.client().post(
                provider_http_url(self.spec, self.preset.base_url, "/audio/transcriptions"),
                data=self.transcription_form(pcm, model),
                headers=_headers(self.preset, self.api_key),
                timeout=timeout,
            ) as response:
                if response.status != 200:
                    body = await response.text()
                    raise SpeechError(_http_error_message(
                        f"{self.preset.label} transcription", response, body, f"model={model}"))
                payload = await response.json(content_type=None)
        except BaseException as exc:
            if self.usage_meter is not None:
                self.usage_meter.finish_attempt(
                    attempt_id, status="cancelled" if isinstance(exc, asyncio.CancelledError) else "error")
            raise
        self.last_cost_usd = _cost(payload)
        if self.usage_meter is not None:
            if self.last_cost_usd is not None:
                self.usage_meter.add_usage(attempt_id, costUsd=self.last_cost_usd)
            self.usage_meter.finish_attempt(attempt_id, status="ok")
        text = payload.get("text") if isinstance(payload, dict) else None
        return str(text or "").strip()


class OpenAIAudioTTS(_KeepAlive):
    """One sentence per request through ``/audio/speech``."""

    capabilities = ProviderCapabilities(
        supports_streaming=False,
        supports_word_timings=False,
        supports_interruption=False,
    )

    def __init__(self, spec: ProviderSpec, *, api_key: str) -> None:
        self.spec = spec
        self.preset = preset_for(spec.provider)
        self.api_key = api_key
        self.usage_meter: UsageMeter | None = None

    def set_usage_meter(self, meter: UsageMeter) -> None:
        self.usage_meter = meter

    @property
    def voice_id(self) -> str:
        return (self.spec.voice_id or "").strip() or self.preset.tts_voice

    @property
    def model(self) -> str:
        return model_for_voice(self.preset, self.voice_id, (self.spec.model or "").strip())

    @property
    def output_format(self) -> str:
        """The profile's choice, else the model's own (Venice's Orpheus takes
        only WAV and refuses "pcm"), else the service's default."""
        chosen = self.spec.options.get("output_format")
        if chosen:
            return str(chosen)
        from sleeptalk import speech_catalog

        option = speech_catalog.find(self.preset.name, speech_catalog.TTS, self.model, cached=False)
        return (option.audio_format if option else "") or self.preset.tts_format

    @property
    def pcm_rate(self) -> int:
        return int(self.spec.options.get("pcm_rate") or self.preset.pcm_rate)

    async def warmup(self) -> None:
        if self.api_key.strip():
            await self.open_connection(provider_http_url(self.spec, self.preset.base_url, "/models"),
                                       _headers(self.preset, self.api_key))

    def request_body(self, text: str) -> dict:
        body = {
            **self.preset.tts_body,
            "model": self.model,
            "input": apply_pronunciation(text, getattr(self.spec, "language", "") or ""),
            "voice": self.voice_id,
            "response_format": self.output_format,
        }
        if self.spec.speed is not None and self.spec.speed > 0 and abs(self.spec.speed - 1.0) > 1e-6:
            body["speed"] = self.spec.speed
        return body

    async def synthesize_pcm_bytes(self, text: str, *, purpose: str = "") -> bytes:
        """The whole sentence as canonical 48 kHz mono PCM: the stream, joined."""
        return b"".join([chunk async for chunk in self.stream_pcm(text, purpose=purpose)])

    def _decode(self, audio: bytes, content_type: str) -> bytes:
        """Raw PCM gets a WAV header at its rate; anything else is decoded as is."""
        if not audio:
            raise SpeechError(f"{self.preset.label} TTS returned no audio.")
        raw = self.output_format == "pcm" or content_type.startswith("audio/pcm") or content_type.startswith("audio/l16")
        if raw and not audio.startswith(b"RIFF") and not audio.startswith(b"ID3") and not audio[:2] == b"\xff\xfb":
            rate = self.pcm_rate
            match = re.search(r"rate=(\d+)", content_type)
            if match:
                rate = int(match.group(1))
            audio = _pcm_to_wav(audio[: len(audio) - len(audio) % 2], 1, 2, rate)
        try:
            return decode_audio_bytes(audio)
        except AudioDecodeError as exc:
            raise SpeechError(f"{self.preset.label} TTS returned audio Sleeptalk could not decode ({content_type or 'no content type'}).") from exc

    async def synthesize_wav_bytes(self, text: str, *, purpose: str = "") -> bytes:
        return _pcm_to_wav(await self.synthesize_pcm_bytes(text, purpose=purpose), 1, 2, _SAMPLE_RATE)

    async def synthesize_wav(self, text: str) -> Path:
        return write_tts_temp_file(await self.synthesize_wav_bytes(text), prefix=self.preset.name)

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        async for chunk in self.stream_pcm(text):
            yield chunk

    async def stream_pcm(self, text: str, *, purpose: str = "") -> AsyncIterator[bytes]:
        """Yield 48 kHz mono PCM in 20 ms pieces as the service sends audio.

        Raw PCM and WAV bodies play while they download; anything else (MP3)
        is decoded once complete. Closing the iterator early ends the request.
        """
        if not self.api_key.strip():
            raise SpeechError(f"Missing {self.preset.env}.")
        if not _has_speakable_content(text):
            raise SpeechError(f"{self.preset.label} TTS transcript was empty or punctuation-only.")
        body = self.request_body(text)
        attempt_id = f"{uuid.uuid4().hex[:12]}:tts:stream:1"
        if self.usage_meter is not None:
            self.usage_meter.start_attempt(
                provider=self.preset.name, operation="tts.stream", attempt_id=attempt_id,
                metadata={"purpose": purpose, "model": body["model"]} if purpose else {"model": body["model"]},
                inputCharacters=len(body["input"]),
            )
        # No total limit: the body is read as fast as it plays, and a call's
        # player holds only a moment of audio, so a long sentence keeps the
        # request open past any fixed total. A total of 20 s cut every such
        # sentence off mid-word (the "talks, then cuts itself off" Discord
        # report). Connecting and a stalled read still time out.
        limit = float(self.spec.options.get("rest_timeout_seconds") or 20.0)
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=limit, sock_read=limit)
        produced = 0

        async def open_request():
            # The response and its first bytes: the part of a request that stalls.
            response = await self.client().post(
                provider_http_url(self.spec, self.preset.base_url, "/audio/speech"),
                json=body, headers=_headers(self.preset, self.api_key), timeout=timeout,
            )
            try:
                if response.status != 200:
                    detail = await response.text()
                    raise SpeechError(_http_error_message(
                        f"{self.preset.label} TTS", response, detail,
                        f"model={body['model']}, voice={body['voice']}, format={body['response_format']}"))
                return response, await response.content.read(_WAV_HEADER)
            except BaseException:
                response.close()
                raise

        async def discard(opened) -> None:
            opened[0].close()

        # Normal first audio is about 0.4 s. A service that stalls before any
        # (open, but silent) held the turn for the whole read timeout.
        first_limit = float(self.spec.options.get("first_audio_timeout_seconds") or 8.0)
        try:
            try:
                response, head = await asyncio.wait_for(
                    _first_success(open_request, _hedge_seconds(self.spec, _TTS_HEDGE_MS), discard),
                    first_limit,
                )
            except TimeoutError as exc:
                raise SpeechError(f"{self.preset.label} TTS sent no audio within {first_limit:g} s.") from exc
            try:
                content_type = response.headers.get("Content-Type", "")
                async for chunk in self._stream_body(response, content_type, head):
                    produced += len(chunk)
                    yield chunk
            finally:
                response.close()
        except asyncio.CancelledError:
            if self.usage_meter is not None:
                self.usage_meter.finish_attempt(attempt_id, status="cancelled")
            raise
        except Exception:
            if self.usage_meter is not None:
                self.usage_meter.finish_attempt(attempt_id, status="error")
            raise
        if self.usage_meter is not None:
            self.usage_meter.add_usage(attempt_id, outputAudioBytes=produced)
            self.usage_meter.finish_attempt(attempt_id, status="ok")

    async def _stream_body(self, response, content_type: str, head: bytes | None = None) -> AsyncIterator[bytes]:
        """Decide the body's shape from its first bytes, then stream or decode."""
        reader = response.content
        if head is None:
            head = await reader.read(_WAV_HEADER)
        if not head:
            raise SpeechError(f"{self.preset.label} TTS returned no audio.")
        wav = _wav_format(head)
        raw = self.output_format == "pcm" or content_type.startswith(("audio/pcm", "audio/l16"))
        if wav is None and not (raw and not head.startswith((b"RIFF", b"ID3", b"\xff\xfb", b"\xff\xf3", b"OggS", b"fLaC"))):
            # A container we can't stream (MP3 and friends): decode once whole.
            rest = await reader.read()
            audio = head + rest
            if len(audio) > _MAX_AUDIO_BYTES:
                raise SpeechError(f"{self.preset.label} TTS returned more audio than one sentence should need.")
            for piece in _chunks(self._decode(audio, content_type)):
                yield piece
            return
        if wav is not None:
            channels, rate, _ = wav
            first = b""
        else:
            channels = 1
            match = re.search(r"rate=(\d+)", content_type)
            rate = int(match.group(1)) if match else self.pcm_rate
            first = head
        resampler = PcmResampler(rate)
        carry = b""
        total = 0

        def convert(data: bytes) -> bytes:
            nonlocal carry
            data = carry + data
            frame = 2 * channels
            usable = len(data) - len(data) % frame
            carry = data[usable:]
            return resampler.feed(_to_mono(data[:usable], channels))

        buffered = convert(first)
        async for data in reader.iter_chunked(8_192):
            total += len(data)
            if total > _MAX_AUDIO_BYTES:
                raise SpeechError(f"{self.preset.label} TTS returned more audio than one sentence should need.")
            buffered += convert(data)
            while len(buffered) >= _CHUNK_BYTES:
                yield buffered[:_CHUNK_BYTES]
                buffered = buffered[_CHUNK_BYTES:]
        buffered += resampler.flush()
        for piece in _chunks(buffered):
            yield piece
        if total == 0 and not first:
            raise SpeechError(f"{self.preset.label} TTS returned no audio.")


def _chunks(pcm: bytes) -> list[bytes]:
    """Sample-aligned pieces of at most 20 ms."""
    pcm = pcm[: len(pcm) - len(pcm) % 2]
    return [pcm[i:i + _CHUNK_BYTES] for i in range(0, len(pcm), _CHUNK_BYTES)]


async def check_key(preset: Preset, api_key: str, base_url: str = "", *, timeout: float = 8.0) -> None:
    """Prove the key is accepted: every one of these services lists models to a valid key."""
    if not api_key.strip():
        raise SpeechError(f"Missing {preset.env}.")
    url = f"{(base_url or preset.base_url).rstrip('/')}/models"
    async with aiohttp.ClientSession() as session, session.get(
        url, headers=_headers(preset, api_key), timeout=aiohttp.ClientTimeout(total=timeout)
    ) as response:
        if response.status in (401, 403):
            raise SpeechError(f"{preset.label} rejected the API key (HTTP {response.status}).")
        if response.status != 200:
            raise SpeechError(f"{preset.label} connectivity check failed (HTTP {response.status}).")


def payload_text(payload: str) -> str:
    """The ``text`` of a transcription response body, tolerant of odd content types."""
    try:
        value = json.loads(payload)
    except ValueError:
        return ""
    return str(value.get("text") or "").strip() if isinstance(value, dict) else ""
