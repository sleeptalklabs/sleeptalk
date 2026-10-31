"""Discord audio shim (PR 2, ADR 0001).

Platform boundary ONLY: voice_recv wire callbacks -> DAVE decryption -> Opus
decode -> per-frame fanout -> the shared UtteranceAssembler (sleeptalk.voice_loop), which
owns VAD, endpointing, buffering, and flush timers keyed by participant id.

Attribute/method names that predate the split (buffers, users, handles,
_flush_user_id, _process_rms_pcm, ...) are kept as thin delegates so the
characterization suite pins behavior across this refactor unmodified.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable

import discord
from discord.ext import voice_recv
from discord.opus import Decoder, OpusError

from sleeptalk.voice_loop.endpointing import (
    AdaptiveEndpointingConfig,
    EndpointDecision,
    TurnCompletionSignal,
)
from sleeptalk.voice_loop.utterance import UtteranceAssembler
from sleeptalk.voice_loop.vad import SileroVadConfig, SileroVadGate


class NativeDiscordOpusSink(voice_recv.AudioSink):
    def __init__(
        self,
        *,
        loop: asyncio.AbstractEventLoop,
        min_bytes: int,
        silence_seconds: float,
        on_utterance: Callable[[discord.User, bytes], Awaitable[None]],
        should_ignore_audio: Callable[[], bool],
        authorized: Callable[[discord.User], bool] | None = None,
        collect_during_ignore: Callable[[int], bool] | None = None,
        on_pcm_frame: Callable[[discord.User, bytes], Awaitable[None]] | None = None,
        on_speech_started: Callable[[discord.User], None] | None = None,
        speech_rms_threshold: int = 0,
        vad_mode: str = "rms",
        compare_primary: str = "silero",
        silero_vad_config: SileroVadConfig | None = None,
        silero_vad_factory: Callable[[SileroVadConfig], SileroVadGate] | None = None,
        on_vad_compare_event: Callable[[int, dict], None] | None = None,
        smart_turn_scorer: object | None = None,
        on_smart_turn_event: Callable[[int, dict], None] | None = None,
        should_report_pcm_frame: Callable[[], bool] | None = None,
        turn_completion_provider: Callable[[int], TurnCompletionSignal | str | None]
        | None = None,
        endpointing_config: AdaptiveEndpointingConfig | None = None,
        on_endpoint_decision: Callable[[int, EndpointDecision], None] | None = None,
        dave_decryptor_factory: Callable[[voice_recv.VoiceRecvClient], NativeDaveDecryptor]
        | None = None,
        on_capture_gap: Callable[[int, dict], None] | None = None,
        on_capture_event: Callable[[int, dict], None] | None = None,
        on_decrypt_failure: Callable[[int, dict], None] | None = None,
        on_pause: Callable[[int, bytes], None] | None = None,
        pause_ms: int = 0,
        early_transcripts: Callable[[], bool] | None = None,
    ) -> None:
        super().__init__()
        self._voice_client = None
        self.loop = loop
        self.authorized = authorized
        self.on_pcm_frame = on_pcm_frame
        self._on_utterance = on_utterance
        self._pcm_frame_tasks: dict[int, set[asyncio.Task[None]]] = {}
        self.should_ignore_audio = should_ignore_audio
        self.should_report_pcm_frame = should_report_pcm_frame or (lambda: True)
        self.dave_decryptor_factory = dave_decryptor_factory or NativeDaveDecryptor
        self.decoders: dict[int, Decoder] = {}
        self.decrypt_failures = 0
        self.decode_failures = 0
        self.frames_received = 0
        self.frames_decoded = 0
        self.on_capture_gap = on_capture_gap
        self.on_decrypt_failure = on_decrypt_failure
        # Per-user decrypt-failure timestamps (loop.time), for the
        # capture-hole guard: a burst inside an utterance's window means
        # the committed transcript is missing audio (join-window DAVE key
        # establishment destroys ~1s of speech).
        self._decrypt_failure_times: dict[int, deque[float]] = {}
        # Mid-utterance frame-arrival watchdog: while a capture buffer is open
        # for a user, every decoded frame re-arms a 400ms timer. If it fires,
        # frames stopped arriving mid-utterance (wire/decrypt side died) —
        # distinct from the assembler's vad_dropout signal, which means frames
        # arrived but were gated out.
        self._gap_watchdogs: dict[int, asyncio.TimerHandle] = {}
        self._gap_watchdog_ms = 400
        self._assembler = UtteranceAssembler(
            loop=loop,
            min_bytes=min_bytes,
            silence_seconds=silence_seconds,
            on_utterance=self._deliver_utterance,
            should_ignore_audio=should_ignore_audio,
            collect_during_ignore=collect_during_ignore,
            on_speech_started=on_speech_started,
            speech_rms_threshold=speech_rms_threshold,
            vad_mode=vad_mode,
            compare_primary=compare_primary,
            silero_vad_config=silero_vad_config,
            silero_vad_factory=silero_vad_factory,
            on_vad_compare_event=on_vad_compare_event,
            smart_turn_scorer=smart_turn_scorer,
            on_smart_turn_event=on_smart_turn_event,
            turn_completion_provider=turn_completion_provider,
            endpointing_config=endpointing_config,
            on_endpoint_decision=on_endpoint_decision,
            on_participant_dropped=lambda pid: self.decoders.pop(pid, None),
            on_capture_event=on_capture_event,
            on_pause=on_pause,
            pause_ms=pause_ms,
            early_transcripts=early_transcripts,
        )

    def decrypt_failures_in_window(self, user_id: int, seconds: float) -> int:
        """How many decrypt failures hit this user in the last `seconds`."""
        times = self._decrypt_failure_times.get(user_id)
        if not times:
            return 0
        cutoff = self.loop.time() - seconds
        return sum(1 for t in times if t >= cutoff)

    def _rearm_gap_watchdog(self, user_id: int) -> None:
        handle = self._gap_watchdogs.get(user_id)
        if handle:
            handle.cancel()
        self._gap_watchdogs[user_id] = self.loop.call_later(
            self._gap_watchdog_ms / 1000, self._check_frame_gap, user_id
        )

    def _check_frame_gap(self, user_id: int) -> None:
        self._gap_watchdogs.pop(user_id, None)
        if user_id not in self._assembler.buffers:
            return
        if self.on_capture_gap:
            self.on_capture_gap(
                user_id,
                {
                    "kind": "frame_starvation",
                    "gapMs": self._gap_watchdog_ms,
                    "decryptFailures": self.decrypt_failures,
                    "decodeFailures": self.decode_failures,
                    "framesReceived": self.frames_received,
                    "framesDecoded": self.frames_decoded,
                },
            )

    # --- voice_recv wire interface (Discord platform edge) ------------------

    def wants_opus(self) -> bool:
        return True

    def write(self, user: discord.User | None, data: voice_recv.VoiceData) -> None:
        if not user or user.bot:
            return
        opus_frame = data.opus
        if not opus_frame:
            return
        self.loop.call_soon_threadsafe(self._process_opus, user, opus_frame)

    @voice_recv.AudioSink.listener()
    def on_voice_member_speaking_stop(self, member: discord.Member) -> None:
        if not member:
            return
        self.loop.call_soon_threadsafe(self._assembler.schedule_flush, member.id)

    @voice_recv.AudioSink.listener()
    def on_voice_member_disconnect(self, member: discord.Member, _ssrc: int | None) -> None:
        if not member:
            return
        self.loop.call_soon_threadsafe(self._assembler.flush, member.id)

    def drop_all(self) -> None:
        """Discard every pending utterance buffer without emitting it
        (session teardown: a reconnect must start from a fresh floor, never
        flush half-captured audio from the dead session into a turn)."""
        if self.loop.is_closed():
            return

        def _drop() -> None:
            for pid in list(self._assembler.participants):
                self._assembler.drop(pid)

        self.loop.call_soon_threadsafe(_drop)

    def cleanup(self) -> None:
        for tasks in self._pcm_frame_tasks.values():
            for task in tasks:
                task.cancel()
        self._pcm_frame_tasks.clear()
        self._assembler.cleanup()
        if self.loop.is_closed():
            return
        self.loop.call_soon_threadsafe(self.decoders.clear)

    # --- Discord media edge: decrypt + decode, then hand PCM to the core ----

    def _process_opus(self, user: discord.User, opus_frame: bytes) -> None:
        if self.authorized is not None and not self.authorized(user):
            self._assembler.drop(user.id)
            return
        self.frames_received += 1
        try:
            decrypted = self._dave_decrypt(user.id, opus_frame)
        except (NativeDaveError, RuntimeError, ValueError):
            self.decrypt_failures += 1
            # Per-failure event (timestamped via the latency log): whether
            # these cluster at utterance tails decides if tail clipping is
            # receiver-side decrypt loss (our bug) or the client simply
            # stopping transmission (starvation events with flat counters).
            times = self._decrypt_failure_times.setdefault(user.id, deque(maxlen=128))
            times.append(self.loop.time())
            if self.on_decrypt_failure:
                self.on_decrypt_failure(
                    user.id,
                    {
                        "kind": "decrypt_failure",
                        "decryptFailures": self.decrypt_failures,
                        "framesReceived": self.frames_received,
                        "framesDecoded": self.frames_decoded,
                        "bufferOpen": user.id in self._assembler.buffers,
                    },
                )
            return

        try:
            decoder = self.decoders.setdefault(user.id, Decoder())
            pcm = decoder.decode(decrypted, fec=False)
        except (OpusError, RuntimeError, ValueError):
            self.decode_failures += 1
            return

        self.frames_decoded += 1
        if user.id in self._assembler.buffers:
            self._rearm_gap_watchdog(user.id)
        if self.on_pcm_frame and self.should_report_pcm_frame():
            task = asyncio.create_task(self.on_pcm_frame(user, pcm))
            pending = self._pcm_frame_tasks.setdefault(user.id, set())
            pending.add(task)
            task.add_done_callback(pending.discard)
        self._assembler.observe(user.id, user, pcm)

    async def _deliver_utterance(self, user: discord.User, pcm: bytes) -> None:
        pending = tuple(self._pcm_frame_tasks.get(user.id, ()))
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if self.authorized is None or self.authorized(user):
            await self._on_utterance(user, pcm)

    def _dave_decrypt(self, user_id: int, opus_frame: bytes) -> bytes:
        voice_client = self.voice_client
        if voice_client is None:
            return opus_frame
        return self.dave_decryptor_factory(voice_client).decrypt_opus(user_id, opus_frame)

    def inject_preroll(self, user: discord.User, pcm: bytes) -> None:
        self._assembler.inject_preroll(user.id, user, pcm)

    def watch_early_transcript(self, user_id: int, task: asyncio.Future) -> None:
        self._assembler.watch_early_transcript(user_id, task)

    def recheck_streaming_endpoint(self, user_id: int) -> None:
        self._assembler.recheck_streaming_endpoint(user_id)

    # --- Compatibility delegates (pinned by the characterization suite) -----
    # The assembler owns this state now; these exist so existing callers and
    # tests keep working unchanged during the PR 2 transition.

    @property
    def buffers(self):
        return self._assembler.buffers

    @property
    def users(self):
        return self._assembler.participants

    @property
    def handles(self):
        return self._assembler.handles

    @property
    def last_audio_at(self):
        return self._assembler.last_audio_at

    @property
    def silero_vads(self):
        return self._assembler.silero_vads

    @property
    def silero_locks(self):
        return self._assembler.silero_locks

    @property
    def speech_reported(self):
        return self._assembler.speech_reported

    @property
    def vad_failures(self):
        return self._assembler.vad_failures

    @vad_failures.setter
    def vad_failures(self, value):
        self._assembler.vad_failures = value

    @property
    def vad_mode(self):
        return self._assembler.vad_mode

    @vad_mode.setter
    def vad_mode(self, value):
        self._assembler.vad_mode = value

    @property
    def min_bytes(self):
        return self._assembler.min_bytes

    @property
    def silence_seconds(self):
        return self._assembler.silence_seconds

    def _flush_user(self, user: discord.User | None) -> None:
        if not user:
            return
        self._assembler.flush(user.id)

    def _flush_user_id(self, user_id: int) -> None:
        self._assembler.flush(user_id)

    def _drop_user_id(self, user_id: int) -> None:
        self._assembler.drop(user_id)

    def _schedule_flush_user(self, user: discord.User | None) -> None:
        if not user:
            return
        self._assembler.schedule_flush(user.id)

    def _maybe_flush_user_id(self, user_id: int, silence_ms: int | None = None) -> None:
        self._assembler._maybe_flush(user_id, silence_ms=silence_ms)

    def _process_rms_pcm(self, user: discord.User, pcm: bytes) -> None:
        self._assembler._observe_rms(user.id, user, pcm)

    def _cleanup_in_loop(self) -> None:
        self._assembler._cleanup_in_loop()
        for handle in self._gap_watchdogs.values():
            handle.cancel()
        self._gap_watchdogs.clear()
        self.decoders.clear()


# Kept import location stable for existing users of the sink module.
from sleeptalk.discord_native.dave import NativeDaveDecryptor, NativeDaveError  # noqa: E402,F401
