from __future__ import annotations

import math

import numpy as np
import pytest

from sleeptalk.transports.terminal_aec import AecStatus, TerminalAec

_SAMPLE_RATE = 48_000
_SAMPLE_WIDTH = 2
_RESIDUAL_FLOOR = 983
SPEECH = b"\x00\x20" * 2_400


def _pcm_rms(pcm: bytes) -> int:
    usable = len(pcm) - (len(pcm) % _SAMPLE_WIDTH)
    if usable <= 0:
        return 0
    samples = memoryview(pcm[:usable]).cast("h")
    return int(math.sqrt(sum(sample * sample for sample in samples) / len(samples)))


def _speechish_pcm(ms: int, *, seed: int = 0) -> bytes:
    n = int(_SAMPLE_RATE * ms / 1000)
    t = np.arange(n) / _SAMPLE_RATE
    noise = np.random.default_rng(seed).normal(0, 1, n)
    samples = (
        0.25 * np.sin(2 * np.pi * 220 * t)
        + 0.20 * np.sin(2 * np.pi * 440 * t)
        + 0.10 * np.sin(2 * np.pi * 880 * t)
        + 0.05 * noise
    )
    return (samples * 20_000).clip(-32768, 32767).astype(np.int16).tobytes()


def _delay_lowpass(pcm: bytes, *, delay_ms: int = 50, alpha: float = 0.2) -> bytes:
    samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float64)
    filtered = np.empty_like(samples)
    prev = 0.0
    for index, value in enumerate(samples):
        prev = prev + alpha * (value - prev)
        filtered[index] = prev
    delay = int(_SAMPLE_RATE * delay_ms / 1000)
    delayed = np.concatenate([np.zeros(delay), filtered])[: len(samples)]
    return np.clip(delayed, -32768, 32767).astype(np.int16).tobytes()


def _tone(freq: float, ms: int, *, amplitude: float = 0.7 * 32767) -> bytes:
    n = int(_SAMPLE_RATE * ms / 1000)
    t = np.arange(n) / _SAMPLE_RATE
    return (amplitude * np.sin(2 * np.pi * freq * t)).clip(-32768, 32767).astype(np.int16).tobytes()


def _mix(left: bytes, right: bytes) -> bytes:
    size = min(len(left), len(right))
    size -= size % _SAMPLE_WIDTH
    first = np.frombuffer(left[:size], dtype=np.int16).astype(np.int32)
    second = np.frombuffer(right[:size], dtype=np.int16).astype(np.int32)
    return np.clip(first + second, -32768, 32767).astype(np.int16).tobytes()


def _process_stream(aec: TerminalAec, far: bytes, near: bytes, *, frame_ms: int = 10) -> bytes:
    frame_bytes = int(_SAMPLE_RATE * _SAMPLE_WIDTH * frame_ms / 1000)
    residual = bytearray()
    for offset in range(0, min(len(far), len(near)), frame_bytes):
        aec.push_farend(far[offset : offset + frame_bytes])
        frame = aec.process(near[offset : offset + frame_bytes])
        residual.extend(frame.residual)
    return bytes(residual)


def test_no_engine_with_farend_is_silent() -> None:
    aec = TerminalAec(engine=None)
    aec.push_farend(SPEECH)
    frame = aec.process(SPEECH)
    assert frame.status is AecStatus.SILENT
    assert _pcm_rms(frame.residual) < 50


def test_no_farend_is_bypass_equal_to_mic() -> None:
    aec = TerminalAec(engine=None)
    frame = aec.process(SPEECH)
    assert frame.status is AecStatus.BYPASS
    assert frame.residual == SPEECH


def test_drained_farend_stays_silent() -> None:
    aec = TerminalAec(engine=None)
    aec.push_farend(SPEECH)
    assert aec.process(SPEECH).status is AecStatus.SILENT
    frame = aec.process(SPEECH)
    assert frame.status is AecStatus.SILENT
    assert _pcm_rms(frame.residual) < 50


def test_reset_clears_farend_to_bypass() -> None:
    aec = TerminalAec(engine=None)
    aec.push_farend(SPEECH)
    assert aec.process(SPEECH).status is AecStatus.SILENT
    drained = aec.process(SPEECH)
    assert drained.status is AecStatus.SILENT
    assert _pcm_rms(drained.residual) < 50
    aec.reset()
    frame = aec.process(SPEECH)
    assert frame.status is AecStatus.BYPASS
    assert frame.residual == SPEECH


def test_aec3_cancels_delayed_colored_echo() -> None:
    pytest.importorskip("pywebrtc_audio")
    far = _speechish_pcm(1200)
    near = _delay_lowpass(far)
    residual = _process_stream(TerminalAec(), far, near)
    start = int(_SAMPLE_RATE * _SAMPLE_WIDTH * 0.8)
    window = residual[start : start + int(_SAMPLE_RATE * _SAMPLE_WIDTH * 0.2)]
    assert _pcm_rms(window) < _RESIDUAL_FLOOR


def test_aec3_keeps_loud_stop_above_floor() -> None:
    pytest.importorskip("pywebrtc_audio")
    far = _speechish_pcm(1200)
    near = _delay_lowpass(far)
    stop_ms = 300
    stop = _tone(1400, stop_ms)
    near = near[: -len(stop)] + _mix(near[-len(stop) :], stop)
    residual = _process_stream(TerminalAec(), far, near)
    window = residual[-len(stop) :]
    assert _pcm_rms(window) > 2000
