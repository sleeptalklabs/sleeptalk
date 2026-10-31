"""Core PCM frame helpers shared by package-owned voice transports."""

from __future__ import annotations

import audioop
import wave
from io import BytesIO
from pathlib import Path

CORE_RATE = 48000
CORE_CHANNELS = 2
CORE_WIDTH = 2
CORE_FRAME_BYTES = 3840  # 20 ms @ 48 kHz stereo s16le


class PlayResult:
    def __init__(self, *, cancelled: bool) -> None:
        self.cancelled = cancelled


def wav_to_core_frames(path: Path) -> list[bytes]:
    return _read_wav_core_frames(str(path))


def wav_bytes_to_core_frames(audio: bytes) -> list[bytes]:
    return _read_wav_core_frames(BytesIO(audio))


def pcm_duration_ms(
    pcm: bytes,
    *,
    sample_rate: int = CORE_RATE,
    channels: int = CORE_CHANNELS,
    sample_width: int = CORE_WIDTH,
) -> int:
    bytes_per_second = sample_rate * channels * sample_width
    if bytes_per_second <= 0:
        return 0
    return round(len(pcm) / bytes_per_second * 1000)


def _read_wav_core_frames(source) -> list[bytes]:
    with wave.open(source, "rb") as wav:
        rate = wav.getframerate()
        channels = wav.getnchannels()
        width = wav.getsampwidth()
        raw = wav.readframes(wav.getnframes())
    if width != CORE_WIDTH:
        raw = audioop.lin2lin(raw, width, CORE_WIDTH)
    if channels != CORE_CHANNELS:
        raw = (
            audioop.tostereo(raw, CORE_WIDTH, 1.0, 1.0)
            if channels == 1
            else audioop.tomono(raw, CORE_WIDTH, 0.5, 0.5)
        )
        if channels != 1:
            raw = audioop.tostereo(raw, CORE_WIDTH, 1.0, 1.0)
    if rate != CORE_RATE:
        raw, _ = audioop.ratecv(raw, CORE_WIDTH, CORE_CHANNELS, rate, CORE_RATE, None)
    return [raw[i : i + CORE_FRAME_BYTES] for i in range(0, len(raw), CORE_FRAME_BYTES)]
