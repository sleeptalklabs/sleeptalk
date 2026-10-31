"""Decode encoded clips at the provider or file boundary, never at playout."""

from __future__ import annotations

import io
import wave


class AudioDecodeError(ValueError):
    """An encoded clip has no decodable audio samples."""


SAMPLE_RATE = 48_000
SAMPLE_WIDTH = 2


def decode_audio_bytes(audio: bytes) -> bytes:
    """Decode a complete encoded clip to signed int16 little-endian mono 48 kHz."""
    import av

    if not audio:
        raise AudioDecodeError("Encoded clip contains no audio.")
    try:
        with av.open(io.BytesIO(audio)) as container:
            resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
            chunks = []
            for frame in container.decode(audio=0):
                for output in resampler.resample(frame):
                    chunks.append(bytes(output.planes[0])[: output.samples * SAMPLE_WIDTH])
            for output in resampler.resample(None):
                chunks.append(bytes(output.planes[0])[: output.samples * SAMPLE_WIDTH])
    except (av.FFmpegError, ValueError, EOFError) as exc:
        raise AudioDecodeError("Encoded clip contains invalid audio.") from exc
    pcm = b"".join(chunks)
    if not pcm:
        raise AudioDecodeError("Encoded clip contains no audio samples.")
    return pcm


def pcm_to_wav(pcm: bytes) -> bytes:
    """Encode canonical PCM for APIs that explicitly play a complete clip."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(SAMPLE_WIDTH)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(pcm)
    return buffer.getvalue()
