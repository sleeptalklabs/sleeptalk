"""Revisable live captions from bounded batch uploads, separate from STT turns.

Only the full utterance (or its validated pause-ahead result) reaches the agent.
Previews have no endpointing authority and never substitute for final recognition.
"""
from __future__ import annotations

import asyncio
import audioop
from dataclasses import dataclass, field

from sleeptalk.speech.openai_audio import OpenAIAudioSTT

_BYTES_PER_MS = 96
_PREROLL_BYTES = 300 * _BYTES_PER_MS
_FIRST_BYTES = 2000 * _BYTES_PER_MS
_STEP_BYTES = 2000 * _BYTES_PER_MS
_MAX_BYTES = 30000 * _BYTES_PER_MS
_MAX_REQUESTS = 6


@dataclass
class _Preview:
    pcm: bytearray = field(default_factory=bytearray)
    text: str = ""
    started: bool = False
    voiced_bytes: int = 0
    last_voice: int = 0
    requested_voice: int = 0
    next_request: int = _FIRST_BYTES
    requests: int = 0
    disabled: bool = False
    task: asyncio.Task | None = None


class BatchPreviewMixin:
    """Bounded display-only previews for batch recognizers; opt in at audio ingress."""

    def _init_previews(self) -> None:
        self._previews: dict[int, _Preview] = {}
        self._preview_tasks: set[asyncio.Task] = set()
        self._preview_closed = False

    def append_preview(self, participant_id: int, pcm: bytes) -> None:
        api_key = getattr(self, "api_key", None)
        if self._preview_closed or not pcm or (api_key is not None and not api_key.strip()):
            return
        state = self._previews.setdefault(participant_id, _Preview())
        if state.disabled or state.requests >= _MAX_REQUESTS or len(state.pcm) >= _MAX_BYTES:
            return
        # A small voice gate avoids paid requests for an idle microphone. Keep
        # pre-roll so the first consonant survives the gate's 120 ms attack.
        voiced = audioop.rms(pcm, 2) >= 300
        if not state.started:
            state.pcm.extend(pcm)
            del state.pcm[:-_PREROLL_BYTES]
            state.voiced_bytes = state.voiced_bytes + len(pcm) if voiced else 0
            if state.voiced_bytes < 120 * _BYTES_PER_MS:
                return
            state.started = True
        else:
            state.pcm.extend(pcm[:_MAX_BYTES - len(state.pcm)])
        if voiced:
            state.last_voice = len(state.pcm)
        if (not voiced or len(state.pcm) < state.next_request
                or state.last_voice <= state.requested_voice
                or state.task is not None):
            return
        snapshot = bytes(state.pcm)
        state.requested_voice = state.last_voice
        state.next_request = len(snapshot) + _STEP_BYTES
        state.requests += 1
        state.task = asyncio.create_task(self._preview(participant_id, state, snapshot))
        self._preview_tasks.add(state.task)
        state.task.add_done_callback(self._preview_tasks.discard)

    async def _preview(self, participant_id: int, state: _Preview, pcm: bytes) -> None:
        try:
            # No hedging or retry: a preview must not multiply paid requests.
            text = await asyncio.wait_for(
                self._transcribe_preview(pcm), timeout=4.0,
            )
            if not self._preview_closed and self._previews.get(participant_id) is state:
                state.text = text
        except asyncio.CancelledError:
            raise
        except Exception:
            # Final batch recognition remains independent of preview health.
            state.disabled = True
        finally:
            state.task = None

    def input_transcript_preview(self, participant_id: int) -> str:
        state = self._previews.get(participant_id)
        return state.text if state is not None else ""

    def clear_preview(self, participant_id: int) -> None:
        state = self._previews.pop(participant_id, None)
        if state is not None and state.task is not None:
            state.task.cancel()

    def reset_previews(self) -> None:
        for participant_id in list(self._previews):
            self.clear_preview(participant_id)

    async def _close_previews(self) -> None:
        self._preview_closed = True
        self.reset_previews()
        if self._preview_tasks:
            await asyncio.gather(*self._preview_tasks, return_exceptions=True)

    async def _transcribe_preview(self, pcm: bytes) -> str:
        return await self.transcribe_pcm(pcm, purpose="live_preview")


class BatchPreviewSTT(BatchPreviewMixin, OpenAIAudioSTT):
    """OpenAI-compatible batch uploads with browser-only preview hooks."""

    def __init__(self, spec, *, api_key):
        super().__init__(spec, api_key=api_key)
        self._init_previews()

    async def _transcribe_preview(self, pcm: bytes) -> str:
        # The full adapter may hedge or retry. Previews get one request only.
        return await self._transcribe_once(pcm, self.model, "live_preview")

    async def close(self) -> None:
        await self._close_previews()
        await super().close()
