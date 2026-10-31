"""Speech-to-text on this machine through sherpa-onnx (Moonshine).

Batch only: the finished utterance is decoded in a worker thread. On Apple
Silicon Moonshine tiny decodes a one-second clip in about 15 ms, so the wait
after end of speech is the VAD's, not the model's.
"""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from sleeptalk.metering import UsageMeter
from sleeptalk.providers import ProviderSpec
from sleeptalk.speech.common import SpeechError
from sleeptalk.speech.batch_preview_stt import BatchPreviewMixin
from sleeptalk.speech.local_models import (
    LocalModel,
    LocalModelError,
    get_model,
    is_ready,
    model_path,
    resample_int16,
)

_SAMPLE_RATE = 48_000


def not_downloaded(model: LocalModel) -> SpeechError:
    return SpeechError(
        f"Local speech model {model.label} is not downloaded. Run sleeptalk settings, choose Local speech, "
        "and let it fetch the models."
    )


def load_recognizer(model: LocalModel, root: Path | None = None, *, num_threads: int | None = None) -> Any:
    """Build the sherpa-onnx recognizer for a ready model. Blocking; import is lazy."""

    if not is_ready(model, root):
        raise not_downloaded(model)
    try:
        import sherpa_onnx
    except ImportError:
        raise SpeechError("Local speech needs the sleeptalk[local] extra (sherpa-onnx). Run sleeptalk settings to install it.") from None
    directory = model_path(model, root)
    if model.id.startswith("moonshine"):
        return sherpa_onnx.OfflineRecognizer.from_moonshine(
            str(directory / "preprocess.onnx"),
            str(directory / "encode.int8.onnx"),
            str(directory / "uncached_decode.int8.onnx"),
            str(directory / "cached_decode.int8.onnx"),
            str(directory / "tokens.txt"),
            num_threads=num_threads or model.num_threads,
        )
    raise SpeechError(f"no loader for local STT model {model.id}")


class LocalSTT(BatchPreviewMixin):
    sample_rate = _SAMPLE_RATE
    channels = 1
    sample_width = 2
    # Seconds of decoding this machine needs per second of audio. Measured
    # 0.032 (30 s) to 0.050 (82 s) with Moonshine tiny on Apple Silicon; the
    # route's budget keeps a threefold margin so a long turn is not dropped
    # on a slower machine than this one.
    rest_seconds_per_audio_second = 0.15

    def __init__(self, spec: ProviderSpec, *, models_root: Path | None = None) -> None:
        self._init_previews()
        self._decode_lock = threading.Lock()
        self.spec = spec
        self.models_root = models_root
        try:
            self.model = get_model(spec.model or "moonshine-tiny-en", "stt")
        except LocalModelError as exc:
            raise SpeechError(str(exc)) from None
        self.usage_meter: UsageMeter | None = None
        self._recognizer: Any = None
        self._lock = asyncio.Lock()

    def set_usage_meter(self, meter: UsageMeter) -> None:
        self.usage_meter = meter

    async def warmup(self) -> None:
        await self._ready()
        await self.transcribe_pcm(b"\x00" * int(_SAMPLE_RATE * 2 * 0.2), purpose="warmup")

    async def _ready(self) -> Any:
        async with self._lock:
            if self._recognizer is None:
                threads = self.spec.options.get("num_threads")
                self._recognizer = await asyncio.to_thread(
                    load_recognizer, self.model, self.models_root, num_threads=int(threads) if threads else None
                )
            return self._recognizer

    def _decode(self, recognizer: Any, pcm: bytes) -> str:
        import numpy as np

        audio = resample_int16(pcm, _SAMPLE_RATE, self.model.sample_rate)
        samples = np.frombuffer(audio, dtype=np.int16).astype(np.float32) / 32768.0
        stream = recognizer.create_stream()
        stream.accept_waveform(self.model.sample_rate, samples)
        recognizer.decode_stream(stream)
        return str(stream.result.text or "").strip()

    async def transcribe_pcm(self, pcm: bytes, *, purpose: str = "") -> str:
        if not pcm:
            return ""
        recognizer = await self._ready()
        attempt_id = f"{uuid.uuid4().hex[:12]}:stt:local:1"
        if self.usage_meter is not None:
            self.usage_meter.start_attempt(
                provider="local", operation="stt.local", attempt_id=attempt_id,
                metadata={"purpose": purpose, "model": self.model.id} if purpose else {"model": self.model.id},
                audioMs=len(pcm) / (_SAMPLE_RATE * 2 / 1000), audioBytes=len(pcm),
            )
        started = time.monotonic()
        try:
            text = await asyncio.to_thread(self._decode_serial, recognizer, pcm)
        except asyncio.CancelledError:
            if self.usage_meter is not None:
                self.usage_meter.finish_attempt(attempt_id, status="cancelled")
            raise
        except Exception as exc:
            if self.usage_meter is not None:
                self.usage_meter.finish_attempt(attempt_id, status="error")
            raise SpeechError(f"Local STT failed ({type(exc).__name__}): {exc}") from None
        self.last_decode_ms = (time.monotonic() - started) * 1000
        if self.usage_meter is not None:
            self.usage_meter.finish_attempt(attempt_id, status="ok")
        return text

    def _decode_serial(self, recognizer: Any, pcm: bytes) -> str:
        # Cancelling a preview cannot stop its native worker. Serialize actual
        # decoding so final recognition never overlaps that worker.
        with self._decode_lock:
            return self._decode(recognizer, pcm)

    async def close(self) -> None:
        await self._close_previews()
        self._recognizer = None
