"""Audio-native end-of-turn scoring via the bundled Smart Turn v3 ONNX model.

The text heuristic in endpointing.py is a weak judge of "is this person
actually done" — it can only see what survives flattening into a
transcript. Smart Turn listens to the audio itself (pitch trail, cadence,
elongation) and returns a completion probability, which is what humans
use. This module wraps the Daily/BSD-2 Smart Turn v3 weights shipped in
voice_loop/data behind a tiny async interface the utterance assembler
can call at the endpoint gate.

Design rule (see TURN_DETECTION_LANDSCAPE.md §6): the scorer is an
ACCELERATOR, not the cutoff authority. It only runs when the text
heuristic landed in an ambiguous bucket (uncertain / incomplete /
acoustic-fallback), and the regex floor-holder veto always wins. A wrong
"complete" from the model costs a hiccup (the grace-period abort merges
the continuation); a wrong "incomplete" costs seconds of dead air.

This module must never import a platform SDK.
"""

from __future__ import annotations

import asyncio
import audioop

from sleeptalk.voice_loop.models import bundled_model_path

_MODEL_SAMPLE_RATE = 16000
_WINDOW_SECONDS = 8


class SmartTurnScorer:
    """Score utterance-completion probability from raw PCM.

    Duck-typed for the assembler: ``await scorer.score(pcm) -> float``.
    The ONNX session loads lazily on first use so bot startup isn't
    blocked; a load/inference failure raises the usual suspect tuple and
    the caller disables the scorer loudly (never silently).
    """

    def __init__(self, *, threshold: float = 0.6, analyzer: object | None = None) -> None:
        self.threshold = threshold
        self._analyzer = analyzer
        self._session: object | None = None

    def _load(self) -> object:
        if self._analyzer is not None:
            return self._analyzer
        if self._session is None:
            import onnxruntime

            options = onnxruntime.SessionOptions()
            options.execution_mode = onnxruntime.ExecutionMode.ORT_SEQUENTIAL
            options.inter_op_num_threads = 1
            options.intra_op_num_threads = 1
            options.graph_optimization_level = onnxruntime.GraphOptimizationLevel.ORT_ENABLE_ALL
            self._session = onnxruntime.InferenceSession(
                bundled_model_path("smart_turn_v3"),
                sess_options=options,
            )
        return self._session

    async def score(
        self,
        pcm: bytes,
        *,
        input_sample_rate: int = 48000,
        input_channels: int = 2,
        sample_width: int = 2,
        max_seconds: float = 8.0,
    ) -> float:
        """Completion probability in [0, 1] for the utterance in `pcm`.

        `pcm` is the assembler's utterance buffer (48kHz stereo s16 by
        default); only the last `max_seconds` are scored, matching the
        model's 8-second window.
        """
        if not pcm:
            return 0.0
        bytes_per_second = input_sample_rate * input_channels * sample_width
        tail = pcm[-int(bytes_per_second * max_seconds):]
        frame_width = input_channels * sample_width
        tail = tail[: len(tail) - (len(tail) % frame_width)]
        if not tail:
            return 0.0
        if input_channels == 2:
            tail = audioop.tomono(tail, sample_width, 0.5, 0.5)
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, lambda: self._predict(tail, input_sample_rate=input_sample_rate)
        )

    def _predict(self, pcm_mono: bytes, *, input_sample_rate: int = 48000) -> float:
        import numpy as np
        import soxr

        from sleeptalk.voice_loop.whisper_features import compute_whisper_log_mel_features

        loaded = self._load()
        audio = np.frombuffer(pcm_mono, dtype=np.int16).astype(np.float32) / 32768.0
        if self._analyzer is not None:
            return float(loaded._predict_endpoint(audio).get("probability", 0.0))
        if input_sample_rate != _MODEL_SAMPLE_RATE:
            audio = soxr.resample(
                audio, input_sample_rate, _MODEL_SAMPLE_RATE, quality="HQ"
            )
        max_samples = _WINDOW_SECONDS * _MODEL_SAMPLE_RATE
        if audio.size > max_samples:
            audio = audio[-max_samples:]
        elif audio.size < max_samples:
            audio = np.pad(audio, (max_samples - audio.size, 0), mode="constant")
        log_mel = compute_whisper_log_mel_features(audio, do_normalize=True)
        outputs = loaded.run(None, {"input_features": np.expand_dims(log_mel, axis=0)})
        return float(outputs[0][0].item())
