"""Ordered synthesis failover that cannot repeat partially emitted speech."""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import AsyncIterator, Sequence
from contextlib import aclosing

from sleeptalk.speech.common import SpeechError
from sleeptalk.speech.contracts import TTS

logger = logging.getLogger(__name__)


def tts_cache_identity(provider: TTS) -> tuple:
    """Include options and mutable voice settings in any synthesized-clip key."""
    identity = getattr(provider, "cache_identity", None)
    if identity is not None:
        return identity
    spec = getattr(provider, "spec", None)
    return (
        id(provider),
        *(getattr(spec, name, None) for name in ("provider", "model", "voice_id", "language", "speed")),
        repr(getattr(spec, "options", None)),
    )


class FallbackTTS:
    def __init__(self, primary: TTS, fallbacks: Sequence[TTS] = ()) -> None:
        self.providers = tuple(dict((id(item), item) for item in (primary, *fallbacks)).values())
        self._closed = False
        self._requests: set[asyncio.Task] = set()

    @property
    def spec(self):
        return getattr(self.providers[0], "spec", None)

    @property
    def cache_identity(self) -> tuple:
        return tuple(tts_cache_identity(provider) for provider in self.providers)

    async def keep_warm(self) -> None:
        keep_warm = getattr(self.providers[0], "keep_warm", None)
        if keep_warm is not None:
            await keep_warm()

    async def warmup(self) -> None:
        warmup = getattr(self.providers[0], "warmup", None)
        if warmup is not None:
            await warmup()

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        async with aclosing(self.stream_pcm(text)) as stream:
            async for pcm in stream:
                yield pcm

    async def stream_pcm(self, text: str) -> AsyncIterator[bytes]:
        if self._closed:
            raise SpeechError("TTS is closed.")
        owner = asyncio.current_task()
        if owner is not None:
            self._requests.add(owner)
        try:
            async with aclosing(self._synthesize(text)) as stream:
                async for pcm in stream:
                    yield pcm
        finally:
            if owner is not None:
                self._requests.discard(owner)

    async def _synthesize(self, text: str) -> AsyncIterator[bytes]:
        for index, provider in enumerate(self.providers):
            emitted = False
            synthesize = getattr(provider, "stream_pcm", None) or provider.synthesize
            try:
                async with aclosing(synthesize(text)) as stream:
                    async for pcm in stream:
                        if not pcm:
                            continue
                        emitted = True
                        yield pcm
                if not emitted:
                    raise SpeechError("Speech provider returned no audio.")
                return
            except Exception:
                if emitted or index == len(self.providers) - 1:
                    raise
                logger.warning("TTS provider %s failed before audio; trying fallback", type(provider).__name__)

    async def abort(self) -> None:
        requests = tuple(task for task in self._requests if task is not asyncio.current_task())
        for task in requests:
            task.cancel()
        await asyncio.gather(*requests, return_exceptions=True)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self.abort()

        async def close_provider(provider: TTS) -> None:
            close = getattr(provider, "close", None)
            if close is not None:
                result = close()
                if inspect.isawaitable(result):
                    await result

        results = await asyncio.gather(*(close_provider(provider) for provider in self.providers), return_exceptions=True)
        failures = [result for result in results if isinstance(result, Exception)]
        if failures:
            raise ExceptionGroup("TTS provider cleanup failed", failures)
