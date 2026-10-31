"""Portable WebRTC AEC3 residual for Terminal speaker barge-in."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Any

__all__ = ["AecFrame", "AecStatus", "TerminalAec"]

_SAMPLE_WIDTH = 2
_UNSET: Any = object()


class AecStatus(Enum):
    CLEAN = "clean"
    SILENT = "silent"
    BYPASS = "bypass"


@dataclass(frozen=True)
class AecFrame:
    residual: bytes
    status: AecStatus
    rms: int


def _pcm_rms(pcm: bytes) -> int:
    usable = len(pcm) - (len(pcm) % _SAMPLE_WIDTH)
    if usable <= 0:
        return 0
    samples = memoryview(pcm[:usable]).cast("h")
    return int(math.sqrt(sum(sample * sample for sample in samples) / len(samples)))


def _try_default_engine(sample_rate: int, stream_delay_ms: int) -> Any | None:
    try:
        from pywebrtc_audio import EchoCanceller

        return EchoCanceller(
            sample_rate=sample_rate,
            num_channels=1,
            stream_delay_ms=stream_delay_ms,
        )
    except Exception:
        return None


class TerminalAec:
    def __init__(
        self,
        *,
        engine: Any | None = _UNSET,
        sample_rate: int = 48_000,
        stream_delay_ms: int = 50,
    ) -> None:
        self._sample_rate = sample_rate
        self._hop_samples = max(sample_rate // 100, 1)
        self._hop_bytes = self._hop_samples * _SAMPLE_WIDTH
        self._far = bytearray()
        self._near_tail = bytearray()
        self._had_far = False
        if engine is _UNSET:
            self._engine = _try_default_engine(sample_rate, stream_delay_ms)
        else:
            self._engine = engine

    def push_farend(self, pcm: bytes) -> None:
        if pcm:
            self._far.extend(pcm)
            self._had_far = True

    def process(self, nearend: bytes) -> AecFrame:
        if not self._far:
            self._near_tail.clear()
            usable = len(nearend) - (len(nearend) % _SAMPLE_WIDTH)
            if self._had_far:
                residual = b"\x00" * usable
                return AecFrame(
                    residual=residual, status=AecStatus.SILENT, rms=_pcm_rms(residual)
                )
            return AecFrame(
                residual=nearend,
                status=AecStatus.BYPASS,
                rms=_pcm_rms(nearend),
            )
        if self._engine is None:
            # Live speaker plus no canceller is laptop loopback. Zeros, never the raw mic.
            usable = len(nearend) - (len(nearend) % _SAMPLE_WIDTH)
            residual = b"\x00" * usable
            drop = min(len(self._far), usable)
            if drop:
                del self._far[:drop]
            return AecFrame(residual=residual, status=AecStatus.SILENT, rms=_pcm_rms(residual))
        residual = self._run_engine(nearend)
        return AecFrame(residual=residual, status=AecStatus.CLEAN, rms=_pcm_rms(residual))

    def reset(self) -> None:
        self._far.clear()
        self._near_tail.clear()
        self._had_far = False
        reset = getattr(self._engine, "reset", None)
        if callable(reset):
            reset()

    def _run_engine(self, nearend: bytes) -> bytes:
        import numpy as np

        self._near_tail.extend(nearend)
        out = bytearray()
        hop = self._hop_bytes
        while len(self._near_tail) >= hop:
            near_hop = bytes(self._near_tail[:hop])
            del self._near_tail[:hop]
            if len(self._far) >= hop:
                far_hop = bytes(self._far[:hop])
                del self._far[:hop]
            else:
                far_hop = bytes(self._far) + b"\x00" * (hop - len(self._far))
                self._far.clear()
            residual = self._engine.process(
                np.frombuffer(near_hop, dtype=np.int16),
                np.frombuffer(far_hop, dtype=np.int16),
            )
            if getattr(residual, "dtype", None) != np.int16:
                residual = np.clip(np.rint(residual), -32768, 32767).astype(np.int16)
            out.extend(np.ascontiguousarray(residual).tobytes())
        if not out:
            return b"\x00" * (len(nearend) - (len(nearend) % _SAMPLE_WIDTH))
        return bytes(out)
