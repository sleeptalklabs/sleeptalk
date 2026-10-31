"""Text-to-speech on this machine through sherpa-onnx (Piper, Kokoro, or Pocket TTS).

Synthesize bounded phrases in a worker thread, prefetching one phrase while
the current phrase plays. Native inference is still batch-based; cancellation
discards its result and prevents later phrases from starting.
"""

from __future__ import annotations

import asyncio
import re
import threading
import uuid
from collections.abc import AsyncIterator
from contextlib import aclosing
from pathlib import Path
from typing import Any

from sleeptalk.metering import UsageMeter
from sleeptalk.providers import ProviderCapabilities, ProviderSpec
from sleeptalk.speech.common import SpeechError, _has_speakable_content, _pcm_to_wav
from sleeptalk.speech.lexicon import apply_pronunciation
from sleeptalk.speech.local_models import (
    LocalModel,
    LocalModelError,
    kokoro_accent,
    is_ready,
    model_path,
    resample_int16,
    resolve_tts_model,
    voice_index,
)
from sleeptalk.speech.local_stt import not_downloaded
from sleeptalk.speech.local_text import prepare_local_speech
from sleeptalk.speech.temp_audio import write_tts_temp_file
from sleeptalk.speech_speed import wire_speed

_SAMPLE_RATE = 48_000
_CHUNK_BYTES = 1_920  # 20 ms, mono int16.


# A phrase ends at a clause mark once it is this long.
_CLAUSE_CHARS = 45
# Past this with no clause mark, a phrase ends before a word that starts a
# new clause. Each phrase is voiced as a sentence of its own, so a cut
# mid-clause ("...every field against" | "the schema...") is heard as a
# sentence end: measured on Kokoro, 405 ms longer than unsplit.
_JOIN_CHARS = 100
_CLAUSE_STARTS = frozenset({
    "and", "but", "or", "so", "then", "which", "because", "before", "after", "while", "when", "where",
    "unless", "until", "although", "though", "whereas",
})
# The last resort: a cut between any two words.
_HARD_CHARS = 180


def _phrases(text: str) -> list[str]:
    """Prefer clause boundaries, then a word that starts a clause, and cut
    between any two words only past ``_HARD_CHARS``.

    Keep short replies intact and avoid tiny tails. Never split a word, URL,
    or number, or invent punctuation that would change the spoken text.
    """
    words = text.split()
    phrases: list[str] = []
    current: list[str] = []
    for index, word in enumerate(words):
        current.append(word)
        phrase = " ".join(current)
        boundary = re.search(r'[,;:!?。！？—.][\"\u201d\u2019\')\]]*$', word)
        following = words[index + 1].strip("\"'(\u201c\u2018").lower() if index + 1 < len(words) else ""
        if len(words) - index - 1 >= 3 and (
            (len(phrase) >= _CLAUSE_CHARS and boundary)
            or (len(phrase) >= _JOIN_CHARS and following in _CLAUSE_STARTS)
            or len(phrase) >= _HARD_CHARS
        ):
            phrases.append(phrase)
            current = []
    if current:
        phrases.append(" ".join(current))
    return phrases


def load_synthesizer(model: LocalModel, root: Path | None = None, *, num_threads: int | None = None,
                     accent: str = "en-us") -> Any:
    """Build the sherpa-onnx TTS for a ready model. Blocking; import is lazy.

    ``accent`` is Kokoro's eSpeak-NG language ("en-us" or "en" for British);
    a multilingual Kokoro needs a lexicon or a language, or sherpa-onnx exits."""

    if not is_ready(model, root):
        raise not_downloaded(model)
    try:
        import sherpa_onnx
    except ImportError:
        raise SpeechError("Local speech needs the sleeptalk[local] extra (sherpa-onnx). Run sleeptalk settings to install it.") from None
    directory = model_path(model, root)
    threads = num_threads or model.num_threads
    if model.id.startswith("kokoro"):
        config = sherpa_onnx.OfflineTtsModelConfig(
            kokoro=sherpa_onnx.OfflineTtsKokoroModelConfig(
                model=str(directory / "model.onnx"), voices=str(directory / "voices.bin"),
                tokens=str(directory / "tokens.txt"), data_dir=str(directory / "espeak-ng-data"),
                lexicon=str(directory / "lexicon-us-en.txt") + "," + str(directory / "lexicon-zh.txt"),
                lang=accent,
            ),
            num_threads=threads,
        )
    elif model.id == "pocket-tts-en":
        from sleeptalk.speech.pocket_encoder import encoder_path

        if not hasattr(sherpa_onnx, "OfflineTtsPocketModelConfig"):
            raise SpeechError("Pocket TTS needs a newer sherpa-onnx runtime. Reinstall sleeptalk[local].")
        config = sherpa_onnx.OfflineTtsModelConfig(
            pocket=sherpa_onnx.OfflineTtsPocketModelConfig(
                lm_flow=str(directory / "lm_flow.int8.onnx"),
                lm_main=str(directory / "lm_main.int8.onnx"),
                encoder=str(encoder_path(directory)),
                decoder=str(directory / "decoder.int8.onnx"),
                text_conditioner=str(directory / "text_conditioner.onnx"),
                vocab_json=str(directory / "vocab.json"),
                token_scores_json=str(directory / "token_scores.json"),
            ),
            num_threads=threads, provider="cpu",
        )
    elif model.id.startswith("piper"):
        onnx = next(directory.glob("*.onnx"), None)
        if onnx is None:
            raise SpeechError(f"{model.label} is missing its .onnx file; delete {directory} and download again")
        config = sherpa_onnx.OfflineTtsModelConfig(
            vits=sherpa_onnx.OfflineTtsVitsModelConfig(
                model=str(onnx), tokens=str(directory / "tokens.txt"), data_dir=str(directory / "espeak-ng-data"),
            ),
            num_threads=threads,
        )
    else:
        raise SpeechError(f"no loader for local TTS model {model.id}")
    return sherpa_onnx.OfflineTts(sherpa_onnx.OfflineTtsConfig(model=config))


class LocalTTS:
    capabilities = ProviderCapabilities(
        supports_streaming=True,
        supports_word_timings=False,
        supports_interruption=True,
    )

    def __init__(self, spec: ProviderSpec, *, models_root: Path | None = None) -> None:
        self.spec = spec
        self.models_root = models_root
        try:
            self.model = resolve_tts_model(spec.model, spec.voice_id)
            self.speaker = voice_index(self.model, spec.voice_id)
        except LocalModelError as exc:
            raise SpeechError(str(exc)) from None
        self.accent = kokoro_accent(spec.voice_id)
        self.usage_meter: UsageMeter | None = None
        self._tts: Any = None
        self._lock = asyncio.Lock()
        # Cancelling to_thread does not stop native inference. Keep the lock
        # in the worker so a new turn cannot overlap the abandoned call.
        self._inference_lock = threading.Lock()
        self._requests: dict[threading.Event, asyncio.Task | None] = {}
        self._closed = False

    def set_usage_meter(self, meter: UsageMeter) -> None:
        self.usage_meter = meter

    def set_voice(self, voice_id: str) -> None:
        """Switch voice mid-call; a voice of the other model loads that model next sentence."""
        from dataclasses import replace

        try:
            model = resolve_tts_model("", voice_id)
            speaker = voice_index(model, voice_id)
        except LocalModelError as exc:
            raise SpeechError(str(exc)) from None
        accent = kokoro_accent(voice_id)
        # Another model, or Kokoro between a British and an American voice, loads anew.
        if model.id != self.model.id or (model.id == "kokoro-en" and accent != self.accent):
            self._tts = None
        self.model, self.speaker, self.accent = model, speaker, accent
        self.spec = replace(self.spec, model=model.id, voice_id=voice_id)

    async def warmup(self) -> None:
        await self.synthesize_pcm("Ready.", purpose="warmup")

    async def _ready(self) -> Any:
        async with self._lock:
            if self._closed:
                raise SpeechError("Local TTS is closed.")
            while self._tts is None:
                model, accent = self.model, self.accent
                threads = self.spec.options.get("num_threads")
                tts = await asyncio.to_thread(
                    load_synthesizer, model, self.models_root, num_threads=int(threads) if threads else None,
                    accent=accent,
                )
                if self._closed:
                    raise SpeechError("Local TTS is closed.")
                # set_voice may have switched models, or Kokoro's accent, while loading.
                if self.model == model and self.accent == accent:
                    self._tts = tts
            return self._tts

    def _generate(self, tts: Any, text: str, speaker: int, speed: float, stopped: threading.Event) -> bytes:
        import numpy as np

        with self._inference_lock:
            if stopped.is_set():
                return b""
            audio = tts.generate(text, sid=speaker, speed=speed)
        if stopped.is_set():
            return b""
        samples = np.asarray(audio.samples, dtype=np.float32)
        pcm = np.clip(samples * 32767.0, -32768, 32767).astype(np.int16).tobytes()
        return resample_int16(pcm, int(audio.sample_rate), _SAMPLE_RATE)

    async def synthesize_pcm(self, text: str, *, purpose: str = "") -> bytes:
        async with aclosing(self._stream_pcm(text, purpose=purpose)) as stream:
            return b"".join([pcm async for pcm in stream])

    async def _stream_pcm(self, text: str, *, purpose: str = "") -> AsyncIterator[bytes]:
        """Yield phrase audio immediately; retain at most one phrase ahead."""

        if self._closed:
            raise SpeechError("Local TTS is closed.")
        text = prepare_local_speech(text)
        if not _has_speakable_content(text):
            raise SpeechError("Local TTS transcript was empty or punctuation-only.")
        stopped = threading.Event()
        self._requests[stopped] = None
        attempt_id = f"{uuid.uuid4().hex[:12]}:tts:local:1"
        meter = self.usage_meter
        if meter is not None:
            meter.start_attempt(
                provider="local", operation="tts.local", attempt_id=attempt_id,
                metadata={"purpose": purpose, "model": self.model.id} if purpose else {"model": self.model.id},
                textChars=len(text),
            )
        status = "cancelled"
        try:
            tts = await self._ready()
            speaker = self.speaker
            speed = float(wire_speed("local", self.spec.speed, model=self.model.id) if self.spec.speed else 1.0)
            text = apply_pronunciation(text, self.spec.language or "")
            if self.model.id == "pocket-tts-en":
                from sleeptalk.speech.pocket_tts import stream_pocket
                from sleeptalk.speech.local_voices import is_custom

                async with aclosing(stream_pocket(
                    tts, text, model_path(self.model, self.models_root),
                    self.spec.voice_id if is_custom(self.spec.voice_id) else self.model.voices[speaker],
                    stopped, self._inference_lock,
                    lambda task: self._requests.__setitem__(stopped, task),
                )) as stream:
                    async for chunk in stream:
                        if meter is not None:
                            meter.add_usage(attempt_id, outputAudioBytes=len(chunk))
                        yield chunk
                status = "ok"
                return
            phrases = iter(_phrases(text))

            def start(phrase: str) -> asyncio.Task:
                task = asyncio.create_task(asyncio.to_thread(self._generate, tts, phrase, speaker, speed, stopped))
                self._requests[stopped] = task
                return task

            if stopped.is_set():
                raise asyncio.CancelledError
            pending = start(next(phrases))
            while True:
                pcm = await pending
                if stopped.is_set():
                    raise asyncio.CancelledError
                if not pcm:
                    raise SpeechError("Local TTS returned no audio.")
                following = next(phrases, None)
                # Schedule the next phrase before yielding audio to playback.
                pending = start(following) if following is not None else None
                for offset in range(0, len(pcm), _CHUNK_BYTES):
                    if stopped.is_set():
                        raise asyncio.CancelledError
                    chunk = pcm[offset:offset + _CHUNK_BYTES]
                    if meter is not None:
                        meter.add_usage(attempt_id, outputAudioBytes=len(chunk))
                    yield chunk
                if pending is None:
                    break
            status = "ok"
        except Exception as exc:
            status = "error"
            if isinstance(exc, SpeechError):
                raise
            raise SpeechError(f"Local TTS failed ({type(exc).__name__}): {exc}") from None
        finally:
            stopped.set()
            task = self._requests.pop(stopped, None)
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            if meter is not None:
                meter.finish_attempt(attempt_id, status=status)

    async def synthesize_wav_bytes(self, text: str, *, purpose: str = "") -> bytes:
        return _pcm_to_wav(await self.synthesize_pcm(text, purpose=purpose), 1, 2, _SAMPLE_RATE)

    async def synthesize_wav(self, text: str) -> Path:
        return write_tts_temp_file(await self.synthesize_wav_bytes(text), prefix="local")

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        async with aclosing(self.stream_pcm(text)) as stream:
            async for pcm in stream:
                yield pcm

    async def stream_pcm(self, text: str) -> AsyncIterator[bytes]:
        async with aclosing(self._stream_pcm(text)) as stream:
            async for pcm in stream:
                yield pcm

    async def abort(self) -> None:
        tasks = []
        for stopped, task in tuple(self._requests.items()):
            stopped.set()
            if task is not None:
                task.cancel()
                tasks.append(task)
        await asyncio.gather(*tasks, return_exceptions=True)

    async def close(self) -> None:
        self._closed = True
        await self.abort()
        self._tts = None
