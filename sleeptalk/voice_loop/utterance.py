"""Platform-neutral utterance assembly (PR 2, ADR 0001).

Owns everything between "decoded PCM frames tagged with a participant" and
"completed utterance ready for transcription": per-participant buffers, RMS
or Silero VAD gating, endpointing timers, speech-start notification, and the
ignore/collect gates the turn arbiter drives.

This module must never import a platform SDK. Participants are opaque:
keyed by a Hashable participant_id, with the platform's participant object
stored and handed back to callbacks unchanged. The owning transport adapter
(currently the Discord sink shim) feeds frames via observe(); platform
concerns — wire packets, decryption, codec decode, SSRC/user mapping — stay
in the adapter.

Preroll history is deliberately NOT here: it lives with the turn layer
(session._preroll), separate from the per-frame processing path.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Hashable

from sleeptalk.voice_loop.endpointing import (
    AdaptiveEndpointingConfig,
    EndpointDecision,
    TurnCompletionSignal,
    decide_endpoint,
)
from sleeptalk.voice_loop.vad import SileroVadConfig, SileroVadGate, pcm_rms


# Longest single utterance: 60 s of the 48 kHz stereo 16-bit PCM every
# capture path feeds the assembler.
MAX_UTTERANCE_BYTES = 60 * 48_000 * 2 * 2


class UtteranceAssembler:
    """Assembles per-participant utterances from decoded PCM frames.

    Callbacks:
      on_utterance(participant, pcm)      — completed utterance (>= min_bytes)
      should_ignore_audio()               — global gate (turn in flight)
      collect_during_ignore(participant_id) — per-participant escape hatch
      on_speech_started(participant)      — first speech frame of an utterance
      turn_completion_provider(participant_id) — adaptive endpointing signal
      on_endpoint_decision(participant_id, decision)
      on_participant_dropped(participant_id)   — adapter hook to drop codec state
      on_pause(participant_id, pcm)            — speech paused for pause_ms; the
                                                 buffer so far. Report only: it
                                                 never flushes or moves a timer.
    """

    def __init__(
        self,
        *,
        loop: asyncio.AbstractEventLoop,
        min_bytes: int,
        silence_seconds: float,
        on_utterance: Callable[[object, bytes], Awaitable[None]],
        should_ignore_audio: Callable[[], bool],
        collect_during_ignore: Callable[[int], bool] | None = None,
        on_speech_started: Callable[[object], None] | None = None,
        speech_rms_threshold: int = 0,
        vad_mode: str = "rms",
        compare_primary: str = "silero",
        silero_vad_config: SileroVadConfig | None = None,
        silero_vad_factory: Callable[[SileroVadConfig], SileroVadGate] | None = None,
        on_vad_compare_event: Callable[[Hashable, dict], None] | None = None,
        smart_turn_scorer: object | None = None,
        on_smart_turn_event: Callable[[Hashable, dict], None] | None = None,
        turn_completion_provider: Callable[[Hashable], TurnCompletionSignal | str | None]
        | None = None,
        endpointing_config: AdaptiveEndpointingConfig | None = None,
        on_endpoint_decision: Callable[[Hashable, EndpointDecision], None] | None = None,
        on_participant_dropped: Callable[[Hashable], None] | None = None,
        on_capture_event: Callable[[Hashable, dict], None] | None = None,
        max_utterance_bytes: int = MAX_UTTERANCE_BYTES,
        on_pause: Callable[[Hashable, bytes], None] | None = None,
        pause_ms: int = 0,
        preroll_ms: int = 0,
        early_transcripts: Callable[[], bool] | None = None,
    ) -> None:
        self.loop = loop
        self.max_utterance_bytes = max_utterance_bytes
        self.min_bytes = min_bytes
        self.silence_seconds = max(silence_seconds, 0.08)
        self.on_utterance = on_utterance
        self.should_ignore_audio = should_ignore_audio
        # Per-participant escape hatch: participants a barge-in just fired
        # for keep collecting even while a turn holds the global ignore gate,
        # so the sentence that caused the interruption is captured in full.
        self.collect_during_ignore = collect_during_ignore or (lambda _pid: False)
        self.on_speech_started = on_speech_started
        self.speech_rms_threshold = max(speech_rms_threshold, 0)
        self.vad_mode = vad_mode.strip().lower()
        if self.vad_mode not in {"rms", "silero", "hybrid", "compare"}:
            print(
                f"[utterance] vad_mode={self.vad_mode!r} is not supported on the "
                "capture path (supported: 'rms', 'silero', 'hybrid', 'compare'); "
                "using RMS"
            )
            self.vad_mode = "rms"
        self.compare_primary = compare_primary.strip().lower()
        if self.compare_primary not in {"rms", "silero"}:
            self.compare_primary = "silero"
        self.on_vad_compare_event = on_vad_compare_event
        # Compare-mode diagnostics: agreement counters plus per-participant
        # last-verdicts so transitions can be logged.
        self.compare_stats = {
            "agree_frames": 0,
            "disagree_frames": 0,
            "rms_only_frames": 0,
            "silero_only_frames": 0,
        }
        self._compare_last: dict[Hashable, tuple[bool, bool]] = {}
        # Audio-native turn scorer (Smart Turn). Accelerator-only: consulted
        # when the text heuristic lands in an ambiguous bucket; the regex
        # floor-holder veto always wins (see voice_loop/smart_turn.py docstring).
        self.smart_turn_scorer = smart_turn_scorer
        self.on_smart_turn_event = on_smart_turn_event
        self.smart_turn_failures = 0
        self._smart_turn_disabled = False
        self._smart_turn_pending: set[Hashable] = set()
        self._semantic_handles: dict[Hashable, asyncio.TimerHandle] = {}
        self._semantic_tasks: dict[Hashable, asyncio.Task] = {}
        self.silero_vad_config = silero_vad_config or SileroVadConfig()
        self.silero_vad_factory = silero_vad_factory or SileroVadGate
        self.turn_completion_provider = turn_completion_provider
        self.endpointing_config = endpointing_config or AdaptiveEndpointingConfig()
        self.on_endpoint_decision = on_endpoint_decision
        self.on_participant_dropped = on_participant_dropped
        self.on_capture_event = on_capture_event
        # Mid-utterance capture dropout tracker: pid -> streak info for frames
        # that arrived but failed every VAD leg while a buffer was open.
        self._capture_dropouts: dict[Hashable, dict] = {}
        # pid -> when the ignore gate shut on an utterance already under way.
        self._gated_since: dict[Hashable, float] = {}
        self.buffers: dict[Hashable, bytearray] = {}
        # Bytes positively classified as speech. `buffers` deliberately keeps
        # every decoded frame once a turn opens so quiet consonants, tails,
        # and pauses remain available to REST fallback; this counter retains
        # the old min-utterance and mode semantics without mistaking buffered
        # silence for speech.
        self.speech_audio_bytes: dict[Hashable, int] = {}
        self.participants: dict[Hashable, object] = {}
        self.handles: dict[Hashable, asyncio.TimerHandle] = {}
        self.on_pause = on_pause
        self.pause_ms = max(int(pause_ms), 0)
        # Whether a pause brings an early transcript (a batch listener): then
        # endpointing waits for that text and judges the turn by it, instead
        # of ending on silence alone. Asked per decision; routes attach late.
        self.early_transcripts = early_transcripts
        # pid -> (voiced_at, text) of the pause that produced it, and the
        # voiced_at of a pause whose text is still on its way.
        self._early_text: dict[Hashable, tuple[float, str]] = {}
        self._early_waiting: dict[Hashable, float] = {}
        # Utterances that began with the speech of a barge-in: that speech
        # already proved itself to the interruption detector, so it is never
        # dropped as too short (it was, and the next turn lost "Wait, stop.").
        self._barge_started: set[Hashable] = set()
        # The last logged endpoint verdict per participant: re-checks log only a change.
        self._last_verdict: dict[Hashable, tuple] = {}
        # Audio from just before the VAD opened a turn. The VAD needs a
        # moment of voice to decide, so without it a soft first word ("So",
        # "Okay") starts mid-syllable, and a batch transcriber drops it.
        self.preroll_ms = max(int(preroll_ms), 0)
        self._recent: dict[Hashable, list[bytes]] = {}
        self._recent_bytes: dict[Hashable, int] = {}
        # Which utterance a buffer is: a new one gets a new number, so a
        # consumer of the growing buffer can tell it from the last one.
        self.buffer_generation: dict[Hashable, int] = {}
        self._generations = 0
        self.pause_handles: dict[Hashable, asyncio.TimerHandle] = {}
        # Speech bytes at the last reported pause: a re-armed timer with no
        # new speech (the ignore gate re-checking) reports nothing new.
        self._reported_speech: dict[Hashable, int] = {}
        # Log-only: when an early transcript says the turn sounds finished,
        # when it would have ended, and whether the voice came back after.
        self._early_ends: dict[Hashable, list[dict]] = {}
        # When a frame last carried a voice (Silero wind-down frames don't).
        self._voiced_at: dict[Hashable, float] = {}
        self.resume_grace_ms = 750
        self.last_audio_at: dict[Hashable, float] = {}
        self.last_voiced_at: dict[Hashable, float] = {}
        self.silero_vads: dict[Hashable, SileroVadGate] = {}
        self.silero_locks: dict[Hashable, asyncio.Lock] = {}
        self.speech_reported: set[Hashable] = set()
        self.vad_failures = 0

    def observe(self, participant_id: Hashable, participant: object, pcm: bytes) -> None:
        """Entry point for one decoded PCM frame from the adapter."""
        if self.should_ignore_audio() and not self.collect_during_ignore(participant_id):
            # Pre-roll must be the moment before the next turn, never audio
            # from before a stretch the adapter told us to ignore.
            self._take_recent(participant_id)
            return
        if self.vad_mode in {"silero", "hybrid", "compare"}:
            asyncio.create_task(
                self._observe_gated(participant_id, participant, pcm, self.loop.time())
            )
            return
        self._observe_rms(participant_id, participant, pcm)

    def inject_preroll(self, participant_id: Hashable, participant: object, pcm: bytes) -> None:
        """Seed the utterance buffer with audio captured just before a
        barge-in (the moment-of-interruption speech the ignore gate and
        echo guard would otherwise drop), then let normal endpointing
        flush it. No-op for empty input."""
        if not pcm:
            return
        self._take_recent(participant_id)  # the injected audio is this turn's pre-roll
        self._barge_started.add(participant_id)
        self.participants[participant_id] = participant
        if participant_id not in self.buffers:
            self._generations += 1
            self.buffer_generation[participant_id] = self._generations
            self.buffers[participant_id] = bytearray()
        self.buffers[participant_id].extend(pcm)
        self.speech_audio_bytes[participant_id] = (
            self.speech_audio_bytes.get(participant_id, 0) + len(pcm)
        )
        self.last_audio_at[participant_id] = self.loop.time()
        self.last_voiced_at[participant_id] = self.last_audio_at[participant_id]
        self._schedule_endpoint_check(participant_id)

    def schedule_flush(self, participant_id: Hashable) -> None:
        """Platform reports the participant stopped speaking (e.g. Discord
        speaking-stop event): run the endpointing check now."""
        handle = self.handles.pop(participant_id, None)
        if handle:
            handle.cancel()
        self._schedule_endpoint_check(participant_id)

    def flush(self, participant_id: Hashable) -> None:
        handle = self.handles.pop(participant_id, None)
        if handle:
            handle.cancel()
        self._cancel_pause(participant_id, forget=True)
        participant = self.participants.pop(participant_id, None)
        audio = bytes(self.buffers.pop(participant_id, bytearray()))
        speech_bytes = self.speech_audio_bytes.pop(participant_id, len(audio))
        last_voiced_at = self.last_voiced_at.pop(participant_id, None)
        if self.endpointing_config.fresh_voice_timing:
            last_voiced_at = self._voiced_at.get(participant_id, last_voiced_at)
        self.last_audio_at.pop(participant_id, None)
        self.silero_vads.pop(participant_id, None)
        self.silero_locks.pop(participant_id, None)
        self.speech_reported.discard(participant_id)
        self._compare_last.pop(participant_id, None)
        self._smart_turn_pending.discard(participant_id)
        self._capture_dropouts.pop(participant_id, None)
        self._gated_since.pop(participant_id, None)
        barge_started = participant_id in self._barge_started
        self._barge_started.discard(participant_id)
        if self.on_participant_dropped:
            self.on_participant_dropped(participant_id)
        if not participant:
            return
        if speech_bytes < self.min_bytes and not barge_started:
            # The capture VAD gated this utterance's frames out (quiet mic
            # under the RMS/Silero floors), but the streaming STT — fed
            # every decoded frame, ungated — may still have heard real
            # speech. A transcript-backed utterance is not a stray click:
            # deliver it and let the turn pipeline use the STT transcript
            # (2026-08-15: a 50-char utterance silently vanished here; the
            # user repeated themselves and the next turn's transcript
            # contained both, looking like a fragment sent a turn late).
            transcript = self._signal_transcript(participant_id)
            if transcript:
                if self.on_capture_event:
                    self.on_capture_event(
                        participant_id,
                        {
                            "kind": "utterance_rescued",
                            "audioBytes": len(audio),
                            "speechBytes": speech_bytes,
                            "minBytes": self.min_bytes,
                            "transcriptChars": len(transcript),
                        },
                    )
            else:
                if self.on_capture_event and audio:
                    self.on_capture_event(
                        participant_id,
                        {
                            "kind": "utterance_dropped",
                            "audioBytes": len(audio),
                            "speechBytes": speech_bytes,
                            "minBytes": self.min_bytes,
                        },
                    )
                # Too short to be a turn on its own, but often its first
                # words ("Can you" before a dip): the turn that follows gets
                # it as pre-roll instead of starting mid-sentence.
                self._remember(participant_id, audio)
                return
        self._report_early_ends(participant_id)
        self._voiced_at.pop(participant_id, None)
        self._forget_early_text(participant_id)
        if self.on_capture_event:
            fields = {"kind": "utterance_committed", "audioBytes": len(audio)}
            if last_voiced_at is not None:
                fields["endpointSilenceMs"] = max(
                    round((self.loop.time() - last_voiced_at) * 1000), 0
                )
            self.on_capture_event(participant_id, fields)
        asyncio.create_task(self.on_utterance(participant, audio))

    def drop(self, participant_id: Hashable) -> None:
        self._barge_started.discard(participant_id)
        self._last_verdict.pop(participant_id, None)
        self._early_ends.pop(participant_id, None)
        self._voiced_at.pop(participant_id, None)
        self._forget_early_text(participant_id)
        handle = self.handles.pop(participant_id, None)
        if handle:
            handle.cancel()
        self._cancel_pause(participant_id, forget=True)
        self.participants.pop(participant_id, None)
        self.buffers.pop(participant_id, None)
        self.speech_audio_bytes.pop(participant_id, None)
        self.last_audio_at.pop(participant_id, None)
        self.last_voiced_at.pop(participant_id, None)
        self.silero_vads.pop(participant_id, None)
        self.silero_locks.pop(participant_id, None)
        self.speech_reported.discard(participant_id)
        self._compare_last.pop(participant_id, None)
        self._smart_turn_pending.discard(participant_id)
        self._capture_dropouts.pop(participant_id, None)
        self._gated_since.pop(participant_id, None)
        if self.on_participant_dropped:
            self.on_participant_dropped(participant_id)

    def cleanup(self) -> None:
        if self.loop.is_closed():
            return
        self.loop.call_soon_threadsafe(self._cleanup_in_loop)

    def _observe_rms(
        self, participant_id: Hashable, participant: object, pcm: bytes,
        observed_at: float | None = None,
    ) -> None:
        is_speech = (
            not self.speech_rms_threshold
            or pcm_rms(pcm) >= self.speech_rms_threshold
        )
        if not is_speech and participant_id not in self.buffers:
            self._remember(participant_id, pcm)
            return
        if not is_speech:
            # Once speech opens a turn, preserve every decoded frame. VAD is
            # an activity/boundary signal, not a destructive audio filter.
            # This keeps quiet trailing syllables in the REST fallback.
            self.buffers[participant_id].extend(pcm)
            return
        self.participants[participant_id] = participant
        if participant_id not in self.buffers and self.on_speech_started:
            self.on_speech_started(participant)
        if participant_id not in self.buffers:
            self._open_buffer(participant_id)
        self.buffers[participant_id].extend(pcm)
        self.speech_audio_bytes[participant_id] = (
            self.speech_audio_bytes.get(participant_id, 0) + len(pcm)
        )
        self.last_audio_at[participant_id] = self.loop.time()
        self.last_voiced_at[participant_id] = (
            self.last_audio_at[participant_id] if observed_at is None else observed_at
        )
        self._schedule_endpoint_check(participant_id)

    def _rms_leg_speech(self, pcm: bytes) -> bool:
        """The RMS companion leg for hybrid/compare modes. A 0 threshold
        means the operator never configured an energy floor — the leg is
        OFF (everything would pass), and hybrid degrades gracefully to
        pure Silero."""
        return bool(self.speech_rms_threshold) and pcm_rms(pcm) >= self.speech_rms_threshold

    async def _observe_gated(
        self, participant_id: Hashable, participant: object, pcm: bytes,
        observed_at: float | None = None,
    ) -> None:
        """Silero-backed capture for 'silero', 'hybrid', and 'compare' modes.

        - silero:  Silero's verdict alone (existing behavior).
        - hybrid:  union — a frame is speech when Silero OR the RMS floor
                   says so. Silero catches quiet/rambly speech the energy
                   gate scores as silence; the RMS floor covers model
                   misses. Silero's stop only ends the utterance when the
                   RMS leg also went quiet.
        - compare: run both legs, log agreement transitions, act on the
                   compare_primary leg (data-gathering mode for tuning).
        """
        if observed_at is None:
            observed_at = self.loop.time()
        lock = self.silero_locks.setdefault(participant_id, asyncio.Lock())
        async with lock:
            try:
                gate = self.silero_vads.get(participant_id)
                if gate is None:
                    gate = self.silero_vad_factory(self.silero_vad_config)
                    self.silero_vads[participant_id] = gate
                observation = await gate.observe(pcm)
            except (ImportError, RuntimeError, ValueError, OSError, AttributeError) as exc:
                self.vad_failures += 1
                if self.vad_mode != "rms":
                    # Loud, not silent: a configured Silero/hybrid mode that
                    # degrades to RMS changes capture behavior (quiet speech
                    # starts gating on energy) — operators must see it.
                    print(
                        f"[utterance] Silero VAD failed ({type(exc).__name__}: {exc}); "
                        "capture VAD degraded to RMS for this session"
                    )
                self.vad_mode = "rms"
                self._observe_rms(participant_id, participant, pcm, observed_at)
                return

            rms_speech = self._rms_leg_speech(pcm)
            if self.vad_mode == "compare":
                self._record_compare(participant_id, rms_speech, observation)
                if self.compare_primary == "rms":
                    self._observe_rms(participant_id, participant, pcm, observed_at)
                    return
                is_speech = observation.is_speech
                started = observation.started
                stopped = observation.stopped
            elif self.vad_mode == "hybrid":
                is_speech = observation.is_speech or rms_speech
                started = observation.started
                stopped = observation.stopped and not rms_speech
            else:  # silero
                is_speech = observation.is_speech
                started = observation.started
                stopped = observation.stopped

            if stopped:
                if participant_id in self.buffers:
                    # Include the frame on which the VAD declared stop. It is
                    # usually silence, but can contain the low-energy release
                    # of the speaker's final word.
                    self.buffers[participant_id].extend(pcm)
                # Silero's stop is authoritative for the base silence floor
                # (a VAD stop means at least stop_ms of real quiet elapsed),
                # but the score-driven buckets in decide_endpoint can still
                # hold the utterance open (uncertain/incomplete wait longer).
                self._maybe_flush(
                    participant_id,
                    silence_ms=None if self.endpointing_config.fresh_voice_timing else max(
                        int(self.silero_vad_config.stop_ms),
                        int(self.silence_seconds * 1000),
                    ),
                )
                return
            if not is_speech:
                if participant_id not in self.buffers:
                    self._remember(participant_id, pcm)
                if participant_id in self.buffers:
                    # A barge's buffer opens from its injected pre-roll, never
                    # reported as speech: dropping it as a false start lost
                    # "Mm-hmm" and left a soft-barged answer paused for good.
                    if participant_id not in self.speech_reported and participant_id not in self._barge_started:
                        # A false start, as far as the VAD can tell yet. It is
                        # often the first syllables of the turn ("Can you" |
                        # "check the logs") split by a short dip: keep it as
                        # pre-roll so the turn that follows still has it.
                        false_start = bytes(self.buffers.get(participant_id, b"")) + pcm
                        self.drop(participant_id)
                        self._remember(participant_id, false_start)
                    else:
                        # Keep rejected audio for the utterance transcript,
                        # but do not move the speech clock. Sustained quiet
                        # still reaches the endpoint timer normally.
                        self.buffers[participant_id].extend(pcm)
                        # Mid-utterance frame that failed every VAD leg. A
                        # sustained streak here truncates the utterance even
                        # though frames keep arriving — track it.
                        self._track_capture_dropout(participant_id, pcm, observation)
                return

            new_buffer = participant_id not in self.buffers
            self._capture_dropouts.pop(participant_id, None)
            self.participants[participant_id] = participant
            if new_buffer:
                self._open_buffer(participant_id)
            self.buffers[participant_id].extend(pcm)
            self.speech_audio_bytes[participant_id] = (
                self.speech_audio_bytes.get(participant_id, 0) + len(pcm)
            )
            self.last_audio_at[participant_id] = self.loop.time()
            self.last_voiced_at[participant_id] = observed_at
            # Report speech-start when Silero marks it, or when either leg
            # opened a new utterance buffer (hybrid: RMS can start one).
            if (started or new_buffer) and participant_id not in self.speech_reported:
                self.speech_reported.add(participant_id)
                if self.on_speech_started:
                    self.on_speech_started(participant)

            # A 20 ms frame that completes no Silero window reports the old
            # state with no confidence; only a window that heard a voice counts.
            voiced = observation.state in ("STARTING", "SPEAKING") and observation.confidence > 0
            self._schedule_endpoint_check(participant_id, voiced=voiced or rms_speech)

    def _track_capture_dropout(self, participant_id: Hashable, pcm: bytes, observation) -> None:
        """Record a frame that arrived but passed no VAD leg mid-utterance.

        Emits one event per streak once it reaches 300ms: that much dropped
        audio inside an open utterance is what truncates transcripts
        mid-sentence. maxRms distinguishes 'audio present but gated' (RMS in
        the hundreds+) from 'frames arriving silent' (RMS near zero — the
        problem is upstream of the VAD).
        """
        now = self.loop.time()
        streak = self._capture_dropouts.get(participant_id)
        if streak is None:
            streak = {"start": now, "maxRms": 0, "emitted": False}
            self._capture_dropouts[participant_id] = streak
        streak["maxRms"] = max(streak["maxRms"], pcm_rms(pcm))
        dropout_ms = int((now - streak["start"]) * 1000)
        if not streak["emitted"] and dropout_ms >= 300:
            streak["emitted"] = True
            if self.on_capture_event:
                self.on_capture_event(
                    participant_id,
                    {
                        "kind": "vad_dropout",
                        "dropoutMs": dropout_ms,
                        "maxRms": streak["maxRms"],
                        "vadMode": self.vad_mode,
                        "sileroState": observation.state,
                        "sileroConfidence": round(observation.confidence, 4),
                        "sileroVolume": round(observation.volume, 4),
                    },
                )

    def _record_compare(
        self,
        participant_id: Hashable,
        rms_speech: bool,
        observation,
    ) -> None:
        silero_speech = observation.is_speech
        if rms_speech == silero_speech:
            self.compare_stats["agree_frames"] += 1
        else:
            self.compare_stats["disagree_frames"] += 1
            if rms_speech:
                self.compare_stats["rms_only_frames"] += 1
            else:
                self.compare_stats["silero_only_frames"] += 1
        current = (rms_speech, silero_speech)
        last = self._compare_last.get(participant_id)
        self._compare_last[participant_id] = current
        if last != current and self.on_vad_compare_event:
            self.on_vad_compare_event(
                participant_id,
                {
                    "rmsSpeech": rms_speech,
                    "sileroSpeech": silero_speech,
                    "sileroState": observation.state,
                    "vadConfidence": round(observation.confidence, 4),
                    "vadVolume": round(observation.volume, 4),
                    "agree": rms_speech == silero_speech,
                    "comparePrimary": self.compare_primary,
                },
            )


    def _schedule_endpoint_check(self, participant_id: Hashable, *, voiced: bool = True) -> None:
        if len(self.buffers.get(participant_id, b"")) >= self.max_utterance_bytes:
            # Nobody speaks for a minute without a pause. Continuous audio
            # (music, an open mic) must not grow one buffer, and one upload,
            # without end: deliver what is there and start a new utterance.
            if self.on_capture_event:
                self.on_capture_event(
                    participant_id,
                    {"kind": "utterance_length_cap", "audioBytes": len(self.buffers[participant_id])},
                )
            self.flush(participant_id)
            return
        if voiced:
            self._voiced_at[participant_id] = self.loop.time()
        handle = self.handles.pop(participant_id, None)
        if handle:
            handle.cancel()
        self.handles[participant_id] = self.loop.call_later(
            self._next_endpoint_delay_seconds(participant_id),
            self._maybe_flush,
            participant_id,
        )
        if voiced:
            self._voice_after_early_end(participant_id)
            if self.endpointing_config.local_responsiveness:
                self._cancel_semantic_check(participant_id, cancel_task=False)
                self._semantic_handles[participant_id] = self.loop.call_later(
                    self.endpointing_config.semantic_check_ms / 1000,
                    self._start_semantic_check, participant_id
                )
        if self.on_pause is not None and self.pause_ms and voiced:
            # Only a frame with a voice in it restarts the pause. Silero's
            # wind-down frames still count as speech for endpointing, and the
            # turn ends the moment it stops, so timing from them never fired.
            self._cancel_pause(participant_id)
            self.pause_handles[participant_id] = self.loop.call_later(
                self.pause_ms / 1000, self._report_pause, participant_id
            )

    def _open_buffer(self, participant_id: Hashable) -> None:
        self._generations += 1
        self.buffer_generation[participant_id] = self._generations
        self.buffers[participant_id] = bytearray(self._take_recent(participant_id))

    def _remember(self, participant_id: Hashable, pcm: bytes) -> None:
        if not self.preroll_ms:
            return
        frames = self._recent.setdefault(participant_id, [])
        frames.append(pcm)
        total = self._recent_bytes.get(participant_id, 0) + len(pcm)
        limit = self.preroll_ms * 48 * 2 * 2  # 48 kHz stereo s16, as every capture path feeds
        while frames and total - len(frames[0]) >= limit:
            total -= len(frames.pop(0))
        self._recent_bytes[participant_id] = total

    def _take_recent(self, participant_id: Hashable) -> bytes:
        self._recent_bytes.pop(participant_id, None)
        return b"".join(self._recent.pop(participant_id, ()))

    def watch_early_transcript(self, participant_id: Hashable, task: asyncio.Future) -> None:
        """Judge the turn by the pause's early transcript when it lands.

        With ``endpointing_config.early_transcript`` (and a listener that
        brings one, see ``early_transcripts``), the text becomes the turn's
        completion signal: a finished sentence ends at the complete-bucket
        silence instead of waiting the uncertain one, and an unfinished one
        waits for the rest. It also records, as ``early_end_shadow`` when the
        utterance commits, when the text says the turn could have ended.
        """
        voiced_at = self._voiced_at.get(participant_id)
        if voiced_at is None:
            return
        self._early_waiting[participant_id] = voiced_at

        def landed(done: asyncio.Future) -> None:
            if self._early_waiting.get(participant_id) == voiced_at:
                self._early_waiting.pop(participant_id, None)
            if done.cancelled() or done.exception() is not None or participant_id not in self.buffers:
                self._recheck_endpoint(participant_id)
                return
            if self._voiced_at.get(participant_id) != voiced_at:
                return  # they spoke again before the text came back
            self._early_text[participant_id] = (voiced_at, str(done.result() or "").strip())
            self._recheck_endpoint(participant_id)
            now = self.loop.time()
            decision = decide_endpoint(
                signal=TurnCompletionSignal(transcript=str(done.result() or ""), source="pause_ahead"),
                speech_duration_ms=self._speech_duration_ms(participant_id),
                silence_ms=round((now - voiced_at) * 1000),
                base_silence_ms=int(self._base_flush_delay_seconds() * 1000),
                config=self.endpointing_config,
            )
            text_ms = round((now - voiced_at) * 1000)
            self._early_ends.setdefault(participant_id, []).append({
                "voicedAt": voiced_at,
                "endAt": voiced_at + max(decision.threshold_ms, text_ms) / 1000,
                "textReadyMs": text_ms, "thresholdMs": decision.threshold_ms,
                "reason": decision.reason, "score": decision.semantic_score,
            })

        task.add_done_callback(landed)

    def _streaming_text_pending(self, participant_id: Hashable, decision: EndpointDecision) -> bool:
        """Waiting on a budget a streaming transcript could shorten."""
        if decision.reason not in {"acoustic_fallback", "uncertain", "incomplete"}:
            return False
        if not self.turn_completion_provider:
            return False
        signal = self.turn_completion_provider(participant_id)
        return signal is not None and getattr(signal, "source", "streaming") != "pause_ahead"

    def _forget_early_text(self, participant_id: Hashable) -> None:
        self._early_text.pop(participant_id, None)
        self._early_waiting.pop(participant_id, None)

    def _expects_early_text(self, participant_id: Hashable) -> bool:
        """A pause for this stretch of voice will bring text: its timer is armed or its text is on the way."""
        if not self.endpointing_config.early_transcript or self.early_transcripts is None:
            return False
        if not self.on_pause or not self.pause_ms or not self.early_transcripts():
            return False
        voiced_at = self._voiced_at.get(participant_id)
        return participant_id in self.pause_handles or (
            voiced_at is not None and self._early_waiting.get(participant_id) == voiced_at
        )

    def recheck_streaming_endpoint(self, participant_id: Hashable) -> None:
        """Fresh local streaming evidence may settle a pause before VAD hangover."""
        if self.endpointing_config.local_streaming_input and participant_id in self.buffers:
            self._maybe_flush(participant_id)

    def _recheck_endpoint(self, participant_id: Hashable) -> None:
        """Re-judge the turn now that its early text is in (or won't come)."""
        if participant_id not in self.buffers or not self.endpointing_config.early_transcript:
            return
        if self.early_transcripts is None:
            return
        self._maybe_flush(participant_id)

    def _voice_after_early_end(self, participant_id: Hashable) -> None:
        candidates = self._early_ends.get(participant_id)
        if not candidates:
            return
        now = self.loop.time()
        for candidate in candidates:
            if "resumedMs" not in candidate:
                candidate["resumedMs"] = round((now - candidate["endAt"]) * 1000)

    def _report_early_ends(self, participant_id: Hashable) -> None:
        candidates = self._early_ends.pop(participant_id, [])
        if not candidates or not self.on_capture_event:
            return
        now = self.loop.time()
        # A voice that came back after the would-be end means an early end
        # would have cut the person off; within the resume grace the turn is
        # re-asked with both halves, past it the first half stands alone.
        cuts = [c for c in candidates if c.get("resumedMs", -1) >= 0]
        final = next((c for c in reversed(candidates) if "resumedMs" not in c), None)
        event = {
            "kind": "early_end_shadow",
            "candidates": len(candidates),
            "wouldCut": len(cuts),
            "wouldCutPastGrace": sum(1 for c in cuts if c["resumedMs"] > self.resume_grace_ms),
        }
        if final is not None:
            event.update(
                textReadyMs=final["textReadyMs"], thresholdMs=final["thresholdMs"], reason=final["reason"],
                score=final["score"], wouldEndMs=round((final["endAt"] - final["voicedAt"]) * 1000),
                actualEndMs=round((now - final["voicedAt"]) * 1000),
                savedMs=round((now - final["endAt"]) * 1000),
            )
        self.on_capture_event(participant_id, event)

    def _cancel_pause(self, participant_id: Hashable, *, forget: bool = False) -> None:
        handle = self.pause_handles.pop(participant_id, None)
        if handle:
            handle.cancel()
        if forget:
            self._cancel_semantic_check(participant_id)
            self._reported_speech.pop(participant_id, None)

    def _cancel_semantic_check(self, participant_id: Hashable, *, cancel_task: bool = True) -> None:
        handle = self._semantic_handles.pop(participant_id, None)
        if handle is not None:
            handle.cancel()
        task = self._semantic_tasks.pop(participant_id, None) if cancel_task else None
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    def _start_semantic_check(self, participant_id: Hashable, retry: bool = False) -> None:
        self._semantic_handles.pop(participant_id, None)
        if (participant_id not in self.buffers or self.smart_turn_scorer is None
                or self._smart_turn_disabled or self._gate_is_holding(participant_id)
                or self._endpoint_decision(participant_id).filler_detected):
            return
        if participant_id in self._semantic_tasks:
            self._semantic_handles[participant_id] = self.loop.call_later(
                0.1, self._start_semantic_check, participant_id, retry
            )
            return
        self._semantic_tasks[participant_id] = asyncio.create_task(
            self._score_early_pause(
                participant_id, self.buffer_generation.get(participant_id),
                self._voiced_at.get(participant_id), bytes(self.buffers[participant_id]), retry,
            )
        )

    async def _score_early_pause(
        self, participant_id: Hashable, generation: int | None,
        voiced_at: float | None, audio: bytes, retry: bool = False,
    ) -> None:
        # Silence can arrive while inference runs. New speech or a replacement
        # utterance cannot inherit a completion verdict for an older snapshot.
        started = self.loop.time()
        try:
            probability = await self.smart_turn_scorer.score(audio)
            current = (
                participant_id in self.buffers
                and self.buffer_generation.get(participant_id) == generation
                and not self._gate_is_holding(participant_id)
                and self._voiced_at.get(participant_id) == voiced_at
                and voiced_at is not None
                and self.loop.time() >= voiced_at + self.endpointing_config.semantic_check_ms / 1000
                and not self._endpoint_decision(participant_id).filler_detected
            )
            threshold = max(0.8, self.smart_turn_scorer.threshold)
            flushed = current and probability >= threshold
            if self.on_smart_turn_event:
                self.on_smart_turn_event(participant_id, {
                    "probability": round(probability, 4), "threshold": threshold,
                    "flushed": flushed, "stale": not current,
                    "bucketReason": "local_pause_recheck" if retry else "local_early_pause",
                    "attempt": 2 if retry else 1,
                    "inferenceMs": round((self.loop.time() - started) * 1000),
                    "silenceMs": round((self.loop.time() - voiced_at) * 1000) if voiced_at is not None else None,
                })
            if flushed:
                self.flush(participant_id)
            elif current and not retry and self.endpointing_config.fresh_voice_timing:
                # One later look at real captured silence, not a lower confidence
                # bar. New voice replaces this timer; stale results never arm it.
                delay = max(
                    self.endpointing_config.semantic_recheck_ms / 1000
                    - (self.loop.time() - voiced_at), 0.01,
                )
                self._semantic_handles[participant_id] = self.loop.call_later(
                    delay, self._start_semantic_check, participant_id, True
                )
        except (ImportError, RuntimeError, ValueError, OSError, AttributeError) as exc:
            self.smart_turn_failures += 1
            self._smart_turn_disabled = True
            if self.on_smart_turn_event:
                self.on_smart_turn_event(participant_id, {
                    "bucketReason": "local_scorer_unavailable", "error": type(exc).__name__,
                })
        finally:
            if self._semantic_tasks.get(participant_id) is asyncio.current_task():
                self._semantic_tasks.pop(participant_id, None)

    def _report_pause(self, participant_id: Hashable) -> None:
        self.pause_handles.pop(participant_id, None)
        audio = self.buffers.get(participant_id)
        if not audio or self.on_pause is None:
            return
        speech = self.speech_audio_bytes.get(participant_id, 0)
        if self._reported_speech.get(participant_id) == speech:
            return
        self._reported_speech[participant_id] = speech
        try:
            self.on_pause(participant_id, bytes(audio))
        except Exception as exc:  # noqa: BLE001 - a speculation must never break capture
            print(f"[utterance] pause callback failed ({type(exc).__name__}: {exc})")

    def _maybe_flush(self, participant_id: Hashable, silence_ms: int | None = None) -> None:
        if participant_id not in self.buffers:
            return
        if len(self.buffers[participant_id]) >= self.max_utterance_bytes:
            self._schedule_endpoint_check(participant_id)
            return
        if self._gate_is_holding(participant_id):
            self._schedule_endpoint_check(participant_id)
            return
        decision = self._endpoint_decision(participant_id, silence_ms=silence_ms)
        verdict = (decision.reason, decision.threshold_ms, decision.should_endpoint)
        if self.on_endpoint_decision and self._last_verdict.get(participant_id) != verdict:
            self.on_endpoint_decision(participant_id, decision)
        self._last_verdict[participant_id] = verdict
        if decision.should_endpoint:
            self._last_verdict.pop(participant_id, None)
            self.flush(participant_id)
            return
        # Ambiguous bucket: ask the audio-native scorer whether the speaker
        # is actually done. A yes flushes NOW instead of after the full
        # uncertain/incomplete wait; a no costs nothing (the normal timer
        # below still runs). Floor-holder veto: never asked.
        if (
            self.smart_turn_scorer is not None
            and not self.endpointing_config.local_responsiveness
            and not self._smart_turn_disabled
            and not decision.filler_detected
            and decision.reason in {"uncertain", "incomplete", "acoustic_fallback"}
            and participant_id not in self._smart_turn_pending
        ):
            self._smart_turn_pending.add(participant_id)
            asyncio.create_task(self._smart_turn_recheck(participant_id, decision.reason))
        remaining_ms = max(
            decision.threshold_ms - decision.silence_ms,
            5 if self.endpointing_config.fresh_voice_timing else 50,
        )
        if self._streaming_text_pending(participant_id, decision):
            # A streaming listener's text lands on its own schedule: look again
            # soon rather than only when the long budget runs out (Deepgram
            # turns ended 1.8-3.5 s after the voice, text long since in).
            remaining_ms = min(remaining_ms, self.endpointing_config.streaming_recheck_ms)
        handle = self.handles.pop(participant_id, None)
        if handle:
            handle.cancel()
        self.handles[participant_id] = self.loop.call_later(
            remaining_ms / 1000,
            self._maybe_flush,
            participant_id,
        )

    async def _smart_turn_recheck(self, participant_id: Hashable, reason: str) -> None:
        try:
            audio = bytes(self.buffers.get(participant_id) or b"")
            if not audio:
                return
            probability = await self.smart_turn_scorer.score(audio)
            flushed = False
            if (
                probability >= self.smart_turn_scorer.threshold
                and participant_id in self.buffers
            ):
                self.flush(participant_id)
                flushed = True
            if self.on_smart_turn_event:
                self.on_smart_turn_event(
                    participant_id,
                    {
                        "probability": round(probability, 4),
                        "threshold": self.smart_turn_scorer.threshold,
                        "flushed": flushed,
                        "bucketReason": reason,
                    },
                )
        except (ImportError, RuntimeError, ValueError, OSError, AttributeError) as exc:
            self.smart_turn_failures += 1
            if not self._smart_turn_disabled:
                print(
                    f"[utterance] Smart Turn scoring failed ({type(exc).__name__}: {exc}); "
                    "endpointing falls back to the text heuristic for this session"
                )
            self._smart_turn_disabled = True
        finally:
            self._smart_turn_pending.discard(participant_id)

    def _next_endpoint_delay_seconds(self, participant_id: Hashable) -> float:
        if not self.endpointing_config.enabled:
            return self._base_flush_delay_seconds()
        fresh_voice_timing = self.endpointing_config.fresh_voice_timing
        decision = self._endpoint_decision(
            participant_id, silence_ms=None if fresh_voice_timing else 0
        )
        # STOPPING frames preserve audio but must not restart a full silence
        # budget. Keep the deadline anchored to the last fresh voice window.
        threshold_ms = max(decision.threshold_ms - decision.silence_ms, 0)
        if self._streaming_text_pending(participant_id, decision):
            # The last voiced frame arms this timer; with only the long
            # budget here, the streaming re-check in _maybe_flush began 1.8 s
            # after the voice, long after the text was in.
            threshold_ms = min(threshold_ms, self.endpointing_config.streaming_recheck_ms)
        return max(threshold_ms / 1000, 0.005 if fresh_voice_timing else 0.05)

    def _endpoint_decision(
        self,
        participant_id: Hashable,
        *,
        silence_ms: int | None = None,
    ) -> EndpointDecision:
        if silence_ms is None:
            clock = self._voiced_at if self.endpointing_config.fresh_voice_timing else self.last_audio_at
            last_audio_at = clock.get(participant_id, self.loop.time())
            silence_ms = max(int((self.loop.time() - last_audio_at) * 1000), 0)
        return decide_endpoint(
            signal=self._turn_completion_signal(participant_id),
            speech_duration_ms=self._speech_duration_ms(participant_id),
            silence_ms=silence_ms,
            base_silence_ms=int(self._base_flush_delay_seconds() * 1000),
            config=self.endpointing_config,
        )

    def _turn_completion_signal(self, participant_id: Hashable) -> TurnCompletionSignal | str | None:
        signal = self.turn_completion_provider(participant_id) if self.turn_completion_provider else None
        text = signal if isinstance(signal, str) else (getattr(signal, "transcript", "") or "")
        if text.strip() or self.early_transcripts is None or not self.endpointing_config.early_transcript:
            return signal
        early = self._early_text.get(participant_id)
        if early is not None and early[0] == self._voiced_at.get(participant_id) and early[1]:
            return TurnCompletionSignal(transcript=early[1], source="pause_ahead")
        if self._expects_early_text(participant_id):
            # Text is coming: wait the uncertain budget for it rather than
            # ending on 120 ms of silence (which split "I'd like to know
            # about... the history of tea" into two turns).
            return TurnCompletionSignal(transcript="", source="pause_ahead")
        return signal

    def _signal_transcript(self, participant_id: Hashable) -> str:
        signal = self._turn_completion_signal(participant_id)
        if signal is None:
            return ""
        if isinstance(signal, str):
            return signal.strip()
        return (signal.transcript or "").strip()

    def _speech_duration_ms(self, participant_id: Hashable) -> int:
        speech_bytes = self.speech_audio_bytes.get(participant_id, 0)
        if not speech_bytes:
            return 0
        return int(speech_bytes / (48_000 * 2 * 2) * 1000)

    def _gate_is_holding(self, participant_id: Hashable) -> bool:
        """Is the ignore gate manufacturing this silence?

        Frames stop arriving while the gate is shut, so an utterance already
        under way looks like it ended. Endpointing there cut one sentence
        into a turn per gap — measured on terminal, where a status line
        landing mid-sentence shut the gate and the half-caught question was
        flushed as its own turn.

        Held, not held forever: a gate that never reopens must not strand
        the buffer, so the hold stops at the longest pause a real speaker
        would have been given anyway.
        """
        if not self.should_ignore_audio() or self.collect_during_ignore(participant_id):
            if self._gated_since.pop(participant_id, None) is not None:
                # The silence clock restarts where the speaker is audible
                # again; what the gate ate is not evidence they stopped.
                self.last_audio_at[participant_id] = self.loop.time()
                if self.endpointing_config.fresh_voice_timing:
                    self._voiced_at[participant_id] = self.loop.time()
            return False
        now = self.loop.time()
        since = self._gated_since.setdefault(participant_id, now)
        return (now - since) * 1000 < self._gated_hold_limit_ms()

    def _gated_hold_limit_ms(self) -> int:
        config = self.endpointing_config
        return max(
            config.incomplete_silence_ms,
            config.monologue_incomplete_silence_ms,
            int(self._base_flush_delay_seconds() * 1000),
        )

    def _base_flush_delay_seconds(self) -> float:
        # The endpoint floor is the configured silence window in EVERY mode.
        # vad_stop_ms is Silero's speech hangover, not an endpointing floor:
        # folding it in here (max with stop_ms) forced every endpoint
        # decision to wait >=900ms when the Silero leg drove, adding pure
        # dead air to finished utterances. Cut-off protection comes from the
        # score-driven buckets in decide_endpoint, not from this floor.
        return self.silence_seconds

    def _cleanup_in_loop(self) -> None:
        for participant_id in set(self._semantic_handles) | set(self._semantic_tasks):
            self._cancel_semantic_check(participant_id)
        for handle in (*self.handles.values(), *self.pause_handles.values()):
            handle.cancel()
        self.handles.clear()
        self.pause_handles.clear()
        self._reported_speech.clear()
        self._early_ends.clear()
        self._voiced_at.clear()
        self._early_text.clear()
        self._early_waiting.clear()
        self._barge_started.clear()
        self._last_verdict.clear()
        self._recent.clear()
        self._recent_bytes.clear()
        self.buffers.clear()
        self.speech_audio_bytes.clear()
        self.participants.clear()
        self.last_audio_at.clear()
        self.last_voiced_at.clear()
        self.silero_vads.clear()
        self.silero_locks.clear()
        self.speech_reported.clear()
        self._compare_last.clear()
        self._smart_turn_pending.clear()
        self._capture_dropouts.clear()
        self._gated_since.clear()
