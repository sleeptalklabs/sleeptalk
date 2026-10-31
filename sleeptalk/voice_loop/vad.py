from __future__ import annotations

import asyncio
import audioop
from dataclasses import dataclass
from sleeptalk.voice.configs import SileroVadConfig  # noqa: F401 — re-export
from sleeptalk.voice_loop.models import bundled_model_path


@dataclass
class SpeechGate:
    threshold: int
    min_speech_ms: int
    sample_rate: int = 48000
    channels: int = 2
    sample_width: int = 2
    # Brief dips below the threshold (between words, stop consonants) do
    # NOT wipe the streak — only sustained quiet does. Without this, real
    # speech almost never builds min_speech_ms of contiguous loud frames:
    # production traces showed maxRms 4000+ with zero counted frames.
    hangover_ms: int = 150

    def __post_init__(self) -> None:
        self.speech_ms = 0.0
        self.last_rms = 0
        self._quiet_ms = 0.0

    def observe(self, pcm: bytes) -> bool:
        self.last_rms = pcm_rms(pcm, sample_width=self.sample_width)
        frame_ms = pcm_duration_ms(
            pcm,
            sample_rate=self.sample_rate,
            channels=self.channels,
            sample_width=self.sample_width,
        )
        if self.last_rms >= self.threshold:
            self.speech_ms += frame_ms
            self._quiet_ms = 0.0
        else:
            self._quiet_ms += frame_ms
            if self._quiet_ms > self.hangover_ms:
                self.speech_ms = 0.0
        return self.speech_ms >= self.min_speech_ms

    def reset(self) -> None:
        self.speech_ms = 0.0
        self.last_rms = 0
        self._quiet_ms = 0.0




@dataclass(frozen=True)
class VadObservation:
    is_speech: bool
    started: bool = False
    stopped: bool = False
    state: str = ""
    confidence: float = 0.0
    volume: float = 0.0


class SileroVadGate:
    def __init__(self, config: SileroVadConfig, analyzer: object | None = None) -> None:
        self.config = config
        self._ratecv_state = None
        self._speaking = False
        self._candidate_speech_ms = 0.0
        self._candidate_silence_ms = 0.0
        self._vad_buffer = b""
        self._model = analyzer or SileroOnnxRuntimeModel(
            model_path=_silero_model_path(),
            sample_rate=config.vad_sample_rate,
        )
        self._frame_samples = 512 if config.vad_sample_rate == 16000 else 256
        self._frame_bytes = self._frame_samples * config.sample_width
        self._frame_ms = self._frame_samples / config.vad_sample_rate * 1000

    @property
    def speaking(self) -> bool:
        return self._speaking

    async def observe(self, pcm: bytes) -> VadObservation:
        vad_pcm = self._prepare_pcm(pcm)
        if not vad_pcm:
            return VadObservation(is_speech=self._speaking)
        self._vad_buffer += vad_pcm

        started = False
        stopped = False
        state_name = "SPEAKING" if self._speaking else "QUIET"
        last_confidence = 0.0
        last_volume = 0.0
        while len(self._vad_buffer) >= self._frame_bytes:
            frame = self._vad_buffer[: self._frame_bytes]
            self._vad_buffer = self._vad_buffer[self._frame_bytes :]
            confidence = await self._voice_confidence(frame)
            volume = _normalized_rms(frame, sample_width=self.config.sample_width)
            last_confidence = confidence
            last_volume = volume
            frame_has_voice = (
                confidence >= self._active_threshold()
                and volume >= self.config.min_volume
            )
            if frame_has_voice:
                self._candidate_speech_ms += self._frame_ms
                self._candidate_silence_ms = 0.0
                if not self._speaking:
                    state_name = "STARTING"
            else:
                self._candidate_speech_ms = 0.0
                if self._speaking:
                    self._candidate_silence_ms += self._frame_ms
                    state_name = "STOPPING"
                else:
                    state_name = "QUIET"

            if not self._speaking and self._candidate_speech_ms >= self.config.start_ms:
                self._speaking = True
                started = True
                state_name = "SPEAKING"

            if self._speaking and self._candidate_silence_ms >= self.config.stop_ms:
                self._speaking = False
                stopped = True
                state_name = "QUIET"
                self._candidate_speech_ms = 0.0
                self._candidate_silence_ms = 0.0
                break

        is_speech = self._speaking or state_name in {"STARTING", "STOPPING"}
        return VadObservation(
            is_speech=is_speech,
            started=started,
            stopped=stopped,
            state=state_name,
            confidence=last_confidence,
            volume=last_volume,
        )

    def reset(self) -> None:
        self._ratecv_state = None
        self._speaking = False
        self._candidate_speech_ms = 0.0
        self._candidate_silence_ms = 0.0
        self._vad_buffer = b""
        reset = getattr(self._model, "reset_states", None)
        if callable(reset):
            reset()

    def _active_threshold(self) -> float:
        """Hysteresis: once speaking, a lower confidence keeps the turn alive
        so borderline frames don't flap SPEAKING/STOPPING."""
        if not self._speaking:
            return self.config.confidence
        if self.config.exit_confidence > 0:
            return self.config.exit_confidence
        return max(self.config.confidence - 0.25, 0.2)

    async def _voice_confidence(self, frame: bytes) -> float:
        confidence = getattr(self._model, "voice_confidence", None)
        if callable(confidence):
            result = confidence(frame)
            if asyncio.iscoroutine(result):
                result = await result
            return float(result)
        loop = asyncio.get_running_loop()
        return float(await loop.run_in_executor(None, self._model, frame))

    def _prepare_pcm(self, pcm: bytes) -> bytes:
        if not pcm:
            return b""
        frame_width = self.config.sample_width * self.config.input_channels
        if len(pcm) < frame_width:
            return b""
        usable_len = len(pcm) - (len(pcm) % frame_width)
        if usable_len <= 0:
            return b""
        pcm = pcm[:usable_len]
        if self.config.input_channels == 2:
            pcm = audioop.tomono(pcm, self.config.sample_width, 0.5, 0.5)
        elif self.config.input_channels != 1:
            raise ValueError(f"Unsupported VAD channel count: {self.config.input_channels}")
        if self.config.input_sample_rate == self.config.vad_sample_rate:
            return pcm
        pcm, self._ratecv_state = audioop.ratecv(
            pcm,
            self.config.sample_width,
            1,
            self.config.input_sample_rate,
            self.config.vad_sample_rate,
            self._ratecv_state,
        )
        return pcm


class SileroOnnxRuntimeModel:
    def __init__(self, *, model_path: str, sample_rate: int) -> None:
        if sample_rate not in {8000, 16000}:
            raise ValueError(f"Unsupported Silero VAD sample rate: {sample_rate}")
        import numpy as np
        import onnxruntime

        options = onnxruntime.SessionOptions()
        options.inter_op_num_threads = 1
        options.intra_op_num_threads = 1
        providers = (
            ["CPUExecutionProvider"]
            if "CPUExecutionProvider" in onnxruntime.get_available_providers()
            else None
        )
        self._session = onnxruntime.InferenceSession(
            model_path,
            providers=providers,
            sess_options=options,
        )
        self._np = np
        self._sample_rate = sample_rate
        self._frame_samples = 512 if sample_rate == 16000 else 256
        self._context_samples = 64 if sample_rate == 16000 else 32
        self.reset_states()

    def reset_states(self) -> None:
        self._state = self._np.zeros((2, 1, 128), dtype="float32")
        self._context = self._np.zeros((1, self._context_samples), dtype="float32")

    def voice_confidence(self, frame: bytes) -> float:
        audio = self._np.frombuffer(frame, dtype=self._np.int16)
        if audio.shape[-1] != self._frame_samples:
            raise ValueError(
                f"Silero VAD frame needs {self._frame_samples} samples; got {audio.shape[-1]}"
            )
        audio = audio.astype(self._np.float32) / 32768.0
        audio = self._np.expand_dims(audio, 0)
        model_input = self._np.concatenate((self._context, audio), axis=1)
        output, state = self._session.run(
            None,
            {
                "input": model_input,
                "state": self._state,
                "sr": self._np.array(self._sample_rate, dtype="int64"),
            },
        )
        self._state = state
        self._context = model_input[:, -self._context_samples :]
        return float(output[0][0])


def _silero_model_path() -> str:
    return bundled_model_path("silero_vad")


def _normalized_rms(pcm: bytes, *, sample_width: int) -> float:
    if not pcm:
        return 0.0
    max_amplitude = float(1 << (sample_width * 8 - 1))
    if max_amplitude <= 0:
        return 0.0
    return min(pcm_rms(pcm, sample_width=sample_width) / max_amplitude, 1.0)


def pcm_rms(pcm: bytes, *, sample_width: int = 2) -> int:
    if not pcm:
        return 0
    return audioop.rms(pcm, sample_width)


def pcm_duration_ms(
    pcm: bytes,
    *,
    sample_rate: int = 48000,
    channels: int = 2,
    sample_width: int = 2,
) -> float:
    bytes_per_second = sample_rate * channels * sample_width
    if bytes_per_second <= 0:
        return 0.0
    return len(pcm) / bytes_per_second * 1000
