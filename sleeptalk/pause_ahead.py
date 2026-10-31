"""Transcribe at the start of a pause, before endpointing decides.

A batch STT (OpenRouter, OpenAI, Groq, ...) can only run once the whole
utterance exists, so its round trip normally sits after the endpoint wait.
The endpoint wait is usually the longer of the two: 650-1800 ms of silence
against a few hundred milliseconds of transcription. Starting the upload a
short way into every pause hides the transcription behind that wait.

This never decides when a turn ends. Endpointing is untouched; a pause-ahead
transcript is only *used* when the committed utterance is the same audio it
heard, plus nothing but quiet. Any sound after the snapshot (a resumed
sentence, a soft trailing word) discards it and the turn transcribes the
full utterance the normal way.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from sleeptalk.voice_loop.vad import pcm_rms

# Mono 48 kHz s16: the session's canonical utterance format.
_BYTES_PER_MS = 48_000 * 2 // 1000
_WINDOW_BYTES = 20 * _BYTES_PER_MS
# The trailing stretch of the snapshot that measures this pause's noise floor.
_PAUSE_PROBE_MS = 200
# Below this RMS a window is quiet whatever the floor says.
_ABSOLUTE_QUIET_RMS = 50
_MIN_SPEECH_MS = 300
_MAX_IN_FLIGHT = 2


@dataclass
class _Snapshot:
    pcm: bytes
    started_at: float
    task: asyncio.Task[str]
    quiet_rms: int


@dataclass(frozen=True)
class PauseAheadTake:
    """What the committed turn may use, and why not when it may not."""

    task: asyncio.Task[str] | None
    outcome: str
    head_start_ms: int = 0
    started_at: float = 0.0
    stats: dict[str, Any] = field(default_factory=dict)


def _windows(pcm: bytes) -> list[int]:
    return [
        pcm_rms(pcm[index : index + _WINDOW_BYTES])
        for index in range(0, len(pcm) - _WINDOW_BYTES + 1, _WINDOW_BYTES)
    ]


def _quiet_ceiling(snapshot: bytes) -> int:
    """Loudest RMS the tail may reach and still count as the same pause.

    The snapshot ends in the pause that triggered it, which the VAD already
    judged silent: its loudest window is this room's quiet. A tail may match
    that, with a little headroom, but never approach speech level.
    """
    probe = _windows(snapshot[-_PAUSE_PROBE_MS * _BYTES_PER_MS :])
    speech = sorted(_windows(snapshot)) or [0]
    speech_level = speech[int(len(speech) * 0.9)]
    ceiling = max(_ABSOLUTE_QUIET_RMS, int(max(probe or [0]) * 1.5))
    return min(ceiling, max(_ABSOLUTE_QUIET_RMS, speech_level // 4))


class PauseAheadTranscriber:
    """Speculative batch transcription keyed by participant."""

    def __init__(self, transcribe: Callable[..., Awaitable[str]]) -> None:
        self._transcribe = transcribe
        try:
            self._takes_purpose = "purpose" in inspect.signature(transcribe).parameters
        except (TypeError, ValueError):
            self._takes_purpose = False
        self._latest: dict[int, _Snapshot] = {}
        self._in_flight: dict[int, set[asyncio.Task[str]]] = {}
        self.started = 0

    def start(self, participant_id: int, pcm: bytes) -> asyncio.Task[str] | None:
        """Begin transcribing ``pcm``; None when it is too short or too busy."""
        if len(pcm) < _MIN_SPEECH_MS * _BYTES_PER_MS:
            return None
        latest = self._latest.get(participant_id)
        if latest is not None and latest.pcm == pcm:
            return None
        running = self._in_flight.setdefault(participant_id, set())
        if len(running) >= _MAX_IN_FLIGHT:
            return None
        call = (
            self._transcribe(pcm, purpose="pause_ahead")
            if self._takes_purpose
            else self._transcribe(pcm)
        )
        task = asyncio.ensure_future(call)
        running.add(task)
        task.add_done_callback(lambda done: self._finished(participant_id, done))
        self._latest[participant_id] = _Snapshot(
            pcm, time.perf_counter(), task, _quiet_ceiling(pcm)
        )
        self.started += 1
        return task

    def _finished(self, participant_id: int, task: asyncio.Task[str]) -> None:
        self._in_flight.get(participant_id, set()).discard(task)
        # Stale snapshots are never awaited; retrieve their outcome here.
        if not task.cancelled():
            task.exception()

    def take(self, participant_id: int, pcm: bytes) -> PauseAheadTake:
        """Claim the snapshot for a committed utterance, if it heard all of it."""
        snapshot = self._latest.pop(participant_id, None)
        if snapshot is None:
            return PauseAheadTake(None, "none")
        head_start_ms = int((time.perf_counter() - snapshot.started_at) * 1000)
        common = min(len(pcm), len(snapshot.pcm))
        if common < _MIN_SPEECH_MS * _BYTES_PER_MS or pcm[:common] != snapshot.pcm[:common]:
            return PauseAheadTake(None, "different_audio", head_start_ms)
        # Either side may be longer: the committed turn adds endpoint silence,
        # and a surface may trim trailing hush below the snapshot's length.
        extra = pcm[common:] if len(pcm) > common else snapshot.pcm[common:]
        extra_peak = max(_windows(extra) or [pcm_rms(extra)])
        stats = {"extraMs": len(extra) // _BYTES_PER_MS, "extraPeakRms": extra_peak,
                 "quietCeilingRms": snapshot.quiet_rms}
        if extra_peak > snapshot.quiet_rms:
            return PauseAheadTake(None, "sound_after_pause", head_start_ms, stats=stats)
        return PauseAheadTake(
            snapshot.task, "hit", head_start_ms, snapshot.started_at, stats
        )

    def reset(self) -> None:
        self._latest.clear()

    def close(self) -> None:
        self._latest.clear()
        for running in self._in_flight.values():
            for task in running:
                task.cancel()
        self._in_flight.clear()


def configured_pause_ms(session: Any) -> int:
    """The session's pause-ahead setting, for a surface building its assembler."""
    stt = getattr(getattr(session, "config", None), "stt", None)
    pause = int(getattr(stt, "pause_ahead_ms", 0) or 0)
    if pause and getattr(getattr(session, "policy", None), "local_responsiveness", False):
        # Leave a full 200 ms quiet probe in the snapshot; otherwise its noise
        # estimate includes speech and could accept a quiet resumed word.
        return min(pause, 220)
    return pause


def start_from_pause(session: Any, mono_pcm: bytes, participant_id: int = 0) -> asyncio.Task[str] | None:
    """Hand a paused utterance to the session; None on routes without it."""
    start = getattr(session, "transcribe_ahead", None)
    if not callable(start):
        return None
    task = start(mono_pcm, participant_id=participant_id)
    return task if isinstance(task, asyncio.Future) else None


_WARMING: set[asyncio.Task] = set()


def warm_on_speech(session: Any) -> None:
    """The person started talking: reopen any speech connection that idled shut,
    while they speak rather than in front of the reply."""
    warm = getattr(session, "keep_warm", None)
    if not callable(warm):
        return
    task = asyncio.ensure_future(warm())
    _WARMING.add(task)
    task.add_done_callback(_warmed)


def _warmed(task: asyncio.Task) -> None:
    _WARMING.discard(task)
    if not task.cancelled():
        task.exception()  # a failed warm-up is the next request's problem, not this one's


class StreamAhead:
    """Send the utterance being recorded to a route that listens as it arrives.

    Only what the assembler keeps is sent (pre-roll included, dropped false
    starts not), in pieces of ``piece_ms``, so the commit finds the same
    audio already on the other side and has only the tail left to send.
    """

    def __init__(self, session: Any, assembler: Any, participant_id: Any, to_mono, *, piece_ms: int = 100) -> None:
        check = getattr(session, "streams_ahead", None)
        self.enabled = bool(callable(check) and check())
        self._session = session
        self._assembler = assembler
        self._participant_id = participant_id
        self._to_mono = to_mono
        self._piece_bytes = 48 * 4 * piece_ms  # 48 kHz stereo s16
        self._generation: Any = None
        self._offset = 0
        self._tasks: set[asyncio.Task] = set()

    def sync(self) -> None:
        if not self.enabled:
            return
        buffer = self._assembler.buffers.get(self._participant_id)
        generation = self._assembler.buffer_generation.get(self._participant_id)
        if buffer is None or generation is None:
            return
        if generation != self._generation:
            self._generation, self._offset = generation, 0
        end = len(buffer) - len(buffer) % 4
        if end - self._offset < self._piece_bytes:
            return
        piece = bytes(buffer[self._offset:end])
        self._offset = end
        # Tasks start in order and the provider serializes them.
        task = asyncio.ensure_future(self._session.stream_ahead(generation, self._to_mono(piece)))
        self._tasks.add(task)
        task.add_done_callback(self._done)

    def _done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled():
            task.exception()  # a failed piece only means the commit sends the whole turn

    def close(self) -> None:
        for task in self._tasks:
            task.cancel()
        self._tasks.clear()
