"""Speech component contracts. All PCM is mono 48 kHz signed int16 LE.

Streaming STT is optional; REST STT alone is sufficient for committed turns.
Providers own wire encoding and resampling. Closing an iterator must release
that request; close() releases any resources shared across requests.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from sleeptalk.voice.configs import TurnCompletionSignal


class RESTSTT(Protocol):
    async def transcribe_pcm(self, pcm: bytes, *, purpose: str = "") -> str: ...


class StreamingSTT(Protocol):
    async def append_pcm(self, user_id: int, pcm: bytes) -> None: ...

    async def finish_transcription(
        self, user_id: int, *, fallback_pcm: bytes = b"", timeout_seconds: float = 3.0
    ) -> str: ...

    def turn_completion_signal(self, user_id: int) -> TurnCompletionSignal: ...

    async def reset(self) -> None:
        """Discard captured speech and release connections before a fresh call."""
        ...

    async def close(self) -> None: ...


class TTS(Protocol):
    def synthesize(self, text: str) -> AsyncIterator[bytes]:
        """Yield canonical PCM; never WAV headers or compressed audio."""
        ...
