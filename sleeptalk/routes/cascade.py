"""CascadeRoute — the proven STT → agent → TTS pipeline behind the Route
protocol (r2-cascade-core, ADR 0003 §5/§6).

The characterized production policy now lives in
``sleeptalk.voice_policy_runtime``. This module is the **route boundary**:
it presents that pipeline through ``Route`` so cascade, native, and hybrid
share one lifecycle contract while legacy server entrypoints use a temporary
compatibility import.

Composition (construction via ``sleeptalk.provider_factory`` /
``build_session``; the route receives the assembled pieces):

- ``agent``   — an ``AgentAdapter`` (``respond_streaming`` + ``abort``)
- ``tts``     — a ``TTSAdapter`` (streaming synthesize)
- ``stt``     — optional streaming + REST STT pair. Live PCM enters through
  ``RealtimeSession.append_audio``; ``run_turn`` finalizes streaming STT and
  uses REST only for the characterized timeout/error/coverage fallbacks.
- ``speak``   — the surface's playback callable (sentence → played audio);
  routes never import a platform SDK.

No surface-specific imports enter this module (verified by tests); the
Discord playback mechanics stay behind the ``speak`` callable.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Any

from sleeptalk.speech.contracts import RESTSTT, StreamingSTT, TTS
from sleeptalk.speech.fallback import tts_cache_identity
from sleeptalk.opening_preamble_gate import OpeningPreambleGate
from sleeptalk.voice_loop.telemetry import cascade_turn_identity, log_latency_event
from sleeptalk.streaming import LOCAL_PHRASE_STREAMING, SNAPPY_PHRASE_STREAMING, is_generic_preamble
from sleeptalk.text import shape_voice_reply
from sleeptalk.routes.audio_output import InFlightTurn
from sleeptalk.routes.base import (
    InterruptionGranularity,
    RouteCapabilities,
    RouteInput,
    RouteStateError,
    RouteType,
    TurnContext,
    TurnOutcome,
)
from sleeptalk.session_events import (
    AttemptStatus,
    InterruptionPhase,
    InterruptionSource,
    PaymentMode,
    SessionEventType,
    Speaker,
    agent_event,
    interruption,
    response,
    session_state,
    transcript,
    usage_attempt_finished,
    usage_attempt_started,
)
from sleeptalk.session_events import SessionState as LifecycleState
from sleeptalk.voice.speech_patterns import LocalIntent, classify_local_intent
from sleeptalk.jobs import is_job_cancel, is_job_cancel_all, is_job_status_question
from sleeptalk.sounds import narration_clip_path, play_clip_on, voice_unavailable_clip_path
from sleeptalk.adapter import PermissionDecision
from sleeptalk.voice_policy_runtime import (
    TranscriptionResult,
    VoicePolicyRuntime,
    VoicePolicyTimings,
    _mark_latency,
    permission_answer,
)

__all__ = ["CascadeRoute"]

logger = logging.getLogger("sleeptalk.routes.cascade")

# Said when the agent fails, or ends a turn with nothing to say, before any of
# its answer was heard: a caller must never be left in silence.
AGENT_FAILED_LINE = "Sorry, the agent hit a problem and couldn't answer. Try asking again."
AGENT_SILENT_LINE = "The agent didn't send back an answer. Try asking again."
# Shown, not said: the voice itself is what failed.
VOICE_FAILED_LINE = "The voice service isn't answering, so this reply is text only."
AGENT_LOGIN_LINE = "The agent couldn't sign in to its model provider. Check its login, then try again."
LISTEN_FAILED_LINE = "Sorry, I couldn't hear that clearly; the speech service didn't answer. Please say it again."


def _failure_line(exc: BaseException) -> str:
    """What to tell the caller about an agent failure: a rejected login is worth naming."""
    text = str(exc).lower()
    if "401" in text or "403" in text or "authentication failed" in text or "unauthorized" in text:
        return AGENT_LOGIN_LINE
    return AGENT_FAILED_LINE
_TELL_CALLER_SECONDS = 10.0


async def _job_reply(jobs: Any, user_text: str) -> str:
    """Answer about background jobs without asking the agent: a yes or no
    one of them (or a cancel confirmation) is waiting on, "stop everything",
    "cancel the job", or "how's it going?"."""
    if not jobs.jobs:
        return ""
    decision = permission_answer(user_text)
    if jobs.confirming is not None:
        if decision is not None:
            return await jobs.answer_cancel(decision is not PermissionDecision.DENY)
        jobs.drop_confirmation()
    if jobs.permission_pending and decision is not None:
        title = jobs.asking.title
        await jobs.answer_permission(decision)
        return "Okay." if decision is not PermissionDecision.DENY else f"Okay, the {title} job won't do that."
    pending = [job for job in jobs.jobs if job.active or job.queued]
    if pending and is_job_cancel_all(user_text):
        count = await jobs.cancel_all()
        return (f"Okay, I stopped the {pending[0].title} job." if count == 1
                else f"Okay, I stopped all {count} jobs.")
    if pending and is_job_cancel(user_text):
        target, how = jobs.resolve(user_text, kind="cancel")
        if target is None:
            return ""
        if how == "selected":
            # Several jobs and none named: say which one before stopping it.
            return jobs.ask_cancel(target)
        await jobs.cancel(target)
        return f"Okay, I stopped the {target.title} job."
    if is_job_status_question(user_text):
        return jobs.status_line(user_text)
    return ""


class CascadeRoute:
    """Cascade assembly strategy behind the Route protocol.

    ``run_turn`` drives the package-owned voice-policy pipeline —
    producer (agent sentences) → synthesizer (TTS) → player (``speak``) —
    with the same event ordering the production runner emits, but
    expressed as normalized session events. Barge-in cancellation
    (``r2-cascade-interruption``) hooks in through ``cancel_turn``.
    """

    def __init__(
        self,
        *,
        agent: Any,
        tts: TTS,
        speak: Callable[[str, int], Awaitable[None]] | None = None,
        stt: RESTSTT | None = None,
        stt_streaming: StreamingSTT | None = None,
        audio_output: Any = None,
        usage_sink: Any = None,
        on_cleanup: Callable[[int, str], Awaitable[None]] | None = None,
        discard_queue: Callable[[int], None] | None = None,
        policy_timings: VoicePolicyTimings | None = None,
        # Make the next canned opening lines at call start and after each turn
        # (the builder turns it on for real calls).
        warm_opening_lines: bool = False,
    ) -> None:
        self._agent = agent
        self._warm_enabled = warm_opening_lines
        self._tts = tts
        self._speak = speak
        self._stt = stt
        # r2-cascade-core audit rework: the real pipeline — PCM input goes
        # through STT, agent text through TTS, synthesized audio through
        # the AudioOutput boundary. Both adapters are exercised.
        self._stt_streaming = stt_streaming
        self._audio_output = audio_output
        self._usage_sink = usage_sink
        # discard (drops any synthesis/playback backlog for the turn).
        self._on_cleanup = on_cleanup
        self._discard_queue = discard_queue
        self._policy_timings = policy_timings
        self._policy_runtime: VoicePolicyRuntime | None = None
        self.capabilities = RouteCapabilities(
            route_type=RouteType.CASCADE,
            streaming_output=True,
            interruption=True,
            interruption_granularity=InterruptionGranularity.SENTENCE,
            resume_after_interruption=True,
            usage_operations=("stt.stream", "agent.stream", "tts.stream"),
        )
        self._prepared = False
        self._closed = False
        # Audit rework: the route OWNS its task scope — run_turn registers
        # itself in _in_flight so cancel/close operate through public
        # state; callers never assign private _turn_task fields.
        self._in_flight: InFlightTurn | None = None
        self._active_turn: int | None = None
        self._cancelled_turns: set[int] = set()
        self._discarded_turns: set[int] = set()
        self._aborted_turns: set[int] = set()
        self._cleaned_turns: set[int] = set()
        self._settled_usage: set[int] = set()
        self._tts_aborted_turns: set[int] = set()
        self._settled_attempts: set[str] = set()
        self._open_attempts: dict[int, set[str]] = {}
        self._tts_attempt_seq = 0
        self._audio_lock = asyncio.Lock()
        # When the audio handed to the output so far ends, by its length.
        self._audio_until = 0.0
        self._clip_cache: OrderedDict[tuple, tuple[bytes, ...]] = OrderedDict()
        self._clip_cache_bytes = 0
        self._warm_task: asyncio.Task[None] | None = None
        self._spoken: list[str] = []
        self._generated: list[str] = []

    # -- usage settlement (audit rework: STT/agent/TTS, exactly-once) --------

    def _attempt_id(self, ctx: TurnContext, operation: str, seq: int) -> str:
        provider = {
            "stt.stream": "stt",
            "agent.stream": type(self._agent).__name__.lower(),
            "tts.stream": "tts",
        }.get(operation, "cascade")
        return f"{ctx.session_id}:{ctx.turn_id}:{provider}:{operation}:{seq}"

    def _start_attempt(self, ctx: TurnContext, operation: str, seq: int) -> str:
        attempt_id = self._attempt_id(ctx, operation, seq)
        provider = {
            "stt.stream": "stt",
            "agent.stream": type(self._agent).__name__,
            "tts.stream": "tts",
        }[operation]
        # Track open attempts per turn: settle_usage settles anything the
        # turn paths leave open (crash/abandon recovery — gate finding).
        self._open_attempts.setdefault(ctx.turn_id, set()).add(attempt_id)
        ctx.emit(
            usage_attempt_started(
                ctx.session_id,
                ctx.turn_id,
                attempt_id,
                provider=provider,
                operation=operation,
                payment_mode=PaymentMode.BYOK,
            )
        )
        return attempt_id

    def _finish_attempt(
        self,
        ctx: TurnContext,
        attempt_id: str,
        operation: str,
        status: str,
        units: dict,
    ) -> None:
        """Exactly-once settlement per attempt id."""
        if attempt_id in self._settled_attempts:
            return
        self._settled_attempts.add(attempt_id)
        for open_ids in self._open_attempts.values():
            open_ids.discard(attempt_id)
        provider = {
            "stt.stream": "stt",
            "agent.stream": type(self._agent).__name__,
            "tts.stream": "tts",
        }[operation]
        ctx.emit(
            usage_attempt_finished(
                ctx.session_id,
                ctx.turn_id,
                attempt_id,
                provider=provider,
                operation=operation,
                payment_mode=PaymentMode.BYOK,
                status=AttemptStatus(value=status),
                units=units,
            )
        )

    # -- pipeline stages (audit rework: real STT/TTS) -----------------------

    async def _transcribe(self, pcm: bytes, ctx: TurnContext) -> TranscriptionResult:
        """PCM -> text through the canonical streaming/fallback policy."""
        return await self.transcribe(pcm, participant_id=int(ctx.extras.get("participant_id", 0)))

    async def transcribe(self, pcm: bytes, *, participant_id: int = 0) -> TranscriptionResult:
        """Finalize canonical PCM using the session's shared STT policy."""
        if self._closed or self._policy_runtime is None:
            raise RouteStateError("transcribe requires a prepared, open route")
        return await self._policy_runtime.transcribe(pcm, participant_id=participant_id)

    async def answer_pending(self, text: str) -> bool:
        """A yes or no to the agent's waiting permission question; whether it answered one."""
        if self._closed or self._policy_runtime is None:
            return False
        return await self._policy_runtime.answer_pending(text)

    async def answer_status(self, text: str) -> bool:
        """Answer a status question asked while the agent works; whether it did."""
        if self._closed or self._policy_runtime is None:
            return False
        return await self._policy_runtime.answer_status(text)

    async def reset_capture(self) -> None:
        if self._policy_runtime is not None:
            await self._policy_runtime.reset_capture()

    async def append_audio(self, pcm: bytes, *, participant_id: int = 0) -> None:
        """Public live-audio ingress used by :class:`RealtimeSession`."""
        if self._closed:
            raise RouteStateError("append_audio after close")
        if self._policy_runtime is None:
            raise RouteStateError("append_audio before prepare")
        await self._policy_runtime.append_audio(pcm, participant_id=participant_id)

    async def keep_warm(self) -> None:
        """Reopen the listening and speaking connections if they sat idle."""
        warms = [getattr(c, "keep_warm", None) for c in (self._stt, self._stt_streaming, self._tts) if c is not None]
        await asyncio.gather(*(warm() for warm in warms if callable(warm)), return_exceptions=True)

    @property
    def pause_ahead_ms(self) -> int:
        return 0 if self._policy_runtime is None else self._policy_runtime.pause_ahead_ms

    def listens_ahead(self) -> bool:
        """Whether a pause brings an early transcript endpointing can judge the turn by."""
        return self._policy_runtime is not None and self._policy_runtime.listens_ahead

    def transcribe_ahead(self, pcm: bytes, *, participant_id: int = 0) -> asyncio.Task[str] | None:
        """Early batch transcription at a pause, which endpointing may judge the turn by."""
        if self._closed or self._policy_runtime is None:
            return None
        return self._policy_runtime.transcribe_ahead(pcm, participant_id=participant_id)

    def input_transcript_preview(self, participant_id: int = 0) -> str:
        if self._policy_runtime is None:
            return ""
        return self._policy_runtime.input_transcript_preview(participant_id)

    def turn_completion_signal(self, participant_id: int = 0) -> Any:
        """Provider evidence for transport-owned endpointing, via the kernel."""
        if self._policy_runtime is None:
            return None
        return self._policy_runtime.turn_completion_signal(participant_id)

    async def _speak_policy(self, line: str, ctx: TurnContext, *, category: str | None = None) -> None:
        """Deliver policy narration through the same enforceable audio seam."""
        line = shape_voice_reply(line)
        if not any(char.isalnum() for char in line):
            return
        if category != "answer":
            # Narration and "still going" lines follow the answer sentences
            # already on their way to the voice. Taking the audio lock as
            # soon as one sentence ended put them in the middle of a long
            # reading ("...the entire engineering. Still running the command.
            # generation that later built the Internet").
            await self._after_queued_answer(ctx)
            if ctx.turn_id in self._cancelled_turns:
                return
        if self._tts is not None and self._audio_output is not None:
            pools = getattr(self._policy_runtime.policy, "preamble_pools", {})
            category = category or ("acknowledgement" if any(line in lines for lines in pools.values()) else "narration")
            await self._speak_audio(line, ctx, category=category)
        if self._speak is not None:
            await self._speak_through_surface(line, ctx)

    async def _after_queued_answer(self, ctx: TurnContext) -> None:
        while (ctx.extras.get("_answer_backlog", 0) > 0
               and ctx.turn_id not in self._cancelled_turns and not self._closed):
            await asyncio.sleep(0.02)

    async def _speak_through_surface(self, line: str, ctx: TurnContext) -> None:
        # The surface says when it is done; until then it is talking.
        self._audio_until = float("inf")
        try:
            await self._speak(line, ctx.turn_id)
        finally:
            self._audio_until = time.monotonic()

    def _voice_quiet_for(self, ctx: TurnContext) -> float:
        """Seconds the caller has heard nothing; 0 while an answer is still to come."""
        if ctx.extras.get("_answer_backlog", 0) > 0:
            return 0.0
        return max(0.0, time.monotonic() - self._audio_until)

    async def _play_clip_policy(self, tool: str, ctx: TurnContext, *, path: Any = None) -> bool:
        """Play the narration clip for this style (or ``path``), if the surface can.

        Returns whether audio actually played, so the caller can stay quiet
        rather than emitting progress for a clip nobody heard.
        """
        del tool
        if ctx.turn_id in self._cancelled_turns:
            return False
        if path is None:
            runtime = self._policy_runtime
            policy = getattr(runtime, "policy", None) if runtime is not None else None
            path = narration_clip_path(getattr(policy, "narration_style", ""))
        if path is None:
            return False
        play_file = getattr(self._audio_output, "play_file", None)
        if not callable(play_file):
            return False
        async with self._audio_lock:
            if ctx.turn_id in self._cancelled_turns:
                return False
            try:
                played = await play_clip_on(play_file, path, ctx.turn_id)
            except (OSError, ValueError, RuntimeError):
                return False
        return played

    async def _tell_caller(self, line: str, ctx: TurnContext) -> bool:
        """Speak a failure line; whether it was said. Never raises, never hangs."""
        try:
            await asyncio.wait_for(self._speak_policy(line, ctx, category="answer"), _TELL_CALLER_SECONDS)
        except (Exception, asyncio.TimeoutError) as exc:  # noqa: BLE001 - best effort; the turn's own error stands
            logger.warning("couldn't tell the caller the turn failed: %s: %s", type(exc).__name__, exc)
            return False
        # On the page too: the call's transcript showed nothing for a turn
        # whose only words were this line.
        ctx.emit(transcript(ctx.session_id, ctx.turn_id, speaker=Speaker.ASSISTANT, text=line, final=True))
        return True

    async def _write_audio(self, pcm: bytes, ctx: TurnContext, category: str) -> None:
        if ctx.turn_id in self._cancelled_turns:
            return
        if category == "acknowledgement":
            ctx.extras["_opening_gate"].audio_committing = True
        started = time.monotonic()
        await self._audio_output.write(pcm, ctx.turn_id)
        # 48 kHz mono 16-bit: 96,000 bytes a second, played from when it
        # was handed over or when the audio before it ends.
        self._audio_until = max(self._audio_until, started) + len(pcm) / 96_000
        if ctx.turn_id in self._cancelled_turns:
            return
        event = _mark_latency(ctx, f"{category}_first_output_enqueue", audioCategory=category)
        if event is not None:
            elapsed = event.get("endpointElapsedMs")
            if elapsed is not None:
                ctx.extras.setdefault("_first_output_ms", elapsed)
                if category == "answer":
                    ctx.extras.setdefault("_first_answer_output_ms", elapsed)
        if category == "acknowledgement":
            gate = ctx.extras.get("_opening_gate")
            if gate is not None:
                gate.audio_started = True
        if category == "answer" and not ctx.extras.get("_audio_output_emitted"):
            ctx.extras["_audio_output_emitted"] = True
            ctx.emit(response(ctx.session_id, ctx.turn_id,
                              kind=SessionEventType.RESPONSE_FIRST_OUTPUT,
                              output_kind="audio", audioCategory=category,
                              measurement="output_enqueue"))

    def _opening_lost_race(self, ctx: TurnContext, category: str) -> bool:
        """True, and the canned line is dropped, when the answer got there first.

        A line decided on just before the answer's first sentence used to be
        synthesized and played in front of it anyway (10 of 26 fast turns on
        2026-10-04). Once any of it has played it finishes: no cut-off words.
        """
        if category != "acknowledgement" or not ctx.extras.get("_answer_ready"):
            return False
        gate = ctx.extras.get("_opening_gate")
        if gate is not None and gate.audio_started:
            return False
        if gate is not None:
            # Nothing was heard: the agent's own opening may still speak.
            gate.claimed_by = ""
            gate.audio_committing = False
        ctx.extras["_opening_dropped"] = True
        _mark_latency(ctx, "acknowledgement_dropped", once=True, audioCategory=category)
        return True

    def _warm_opening_lines(self) -> None:
        """Make the next canned opening lines now, so one that's needed plays at once.

        Live synthesis put a line about 350 ms behind its decision. Six short
        lines at call start, then one more per line used: a few hundred
        characters of speech a call.
        """
        runtime = self._policy_runtime
        if not self._warm_enabled or self._closed or runtime is None or self._tts is None or self._audio_output is None:
            return
        if self._warm_task is not None and not self._warm_task.done():
            return
        synthesize = getattr(self._tts, "stream_pcm", None) or getattr(self._tts, "synthesize", None)
        if synthesize is None:
            return
        identity = tts_cache_identity(self._tts)
        lines = [line for line in runtime.next_opening_lines() if identity + (line,) not in self._clip_cache]
        if not lines:
            return

        async def warm() -> None:
            for line in lines:
                chunks: list[bytes] = []
                size = 0
                try:
                    async with contextlib.aclosing(synthesize(line)) as stream:
                        async for chunk in stream:
                            if chunk:
                                chunks.append(bytes(chunk))
                                size += len(chunk)
                except Exception as exc:  # noqa: BLE001 - a line made live later still plays
                    logger.debug("couldn't make %r ahead: %s: %s", line, type(exc).__name__, exc)
                    return
                if not chunks or size > 192_000 or self._closed:
                    continue
                while self._clip_cache and (len(self._clip_cache) >= 32 or self._clip_cache_bytes + size > 1_536_000):
                    _, removed = self._clip_cache.popitem(last=False)
                    self._clip_cache_bytes -= sum(map(len, removed))
                self._clip_cache[identity + (line,)] = tuple(chunks)
                self._clip_cache_bytes += size

        self._warm_task = asyncio.create_task(warm(), name="cascade-warm-opening-lines")

    async def _speak_audio(self, sentence: str, ctx: TurnContext, *, category: str = "answer") -> None:
        async with self._audio_lock:
            await self._speak_audio_unlocked(sentence, ctx, category=category)

    async def _speak_audio_unlocked(self, sentence: str, ctx: TurnContext, *, category: str = "answer") -> None:
        if ctx.turn_id in self._cancelled_turns:
            return
        # Adapters normally shape their streams, but every voice gets the
        # same final protection, including policy lines and custom adapters.
        sentence = shape_voice_reply(sentence)
        if not sentence or not any(char.isalnum() for char in sentence):
            return
        synthesize = getattr(self._tts, "stream_pcm", None) or getattr(self._tts, "synthesize", None)
        if synthesize is None:
            return
        key = tts_cache_identity(self._tts) + (sentence,)
        if self._opening_lost_race(ctx, category):
            return
        cached = self._clip_cache.get(key) if category != "answer" else None
        if cached is not None:
            self._clip_cache.move_to_end(key)
            _mark_latency(ctx, "policy_clip_cache_hit", once=False, audioCategory=category)
            for pcm in cached:
                await self._write_audio(pcm, ctx, category)
            return
        self._tts_attempt_seq += 1
        tts_attempt = self._start_attempt(ctx, "tts.stream", self._tts_attempt_seq)
        bytes_out = 0
        chunks: list[bytes] = []
        cacheable = category != "answer"
        _mark_latency(ctx, "tts_request_sent", once=False, attemptId=tts_attempt,
                      audioCategory=category, textChars=len(sentence))
        try:
            async with contextlib.aclosing(synthesize(sentence)) as stream:
                async for chunk in stream:
                    if ctx.turn_id in self._cancelled_turns:
                        self._finish_attempt(ctx, tts_attempt, "tts.stream", "cancelled", {})
                        return
                    if chunk:
                        pcm = bytes(chunk)
                        if not bytes_out and self._opening_lost_race(ctx, category):
                            self._finish_attempt(ctx, tts_attempt, "tts.stream", "cancelled",
                                                 {"inputCharacters": len(sentence), "outputAudioBytes": 0})
                            return
                        if not bytes_out:
                            _mark_latency(ctx, "tts_first_audio_chunk", once=False,
                                          attemptId=tts_attempt, audioCategory=category)
                        bytes_out += len(pcm)
                        if cacheable:
                            if bytes_out <= 192_000:
                                chunks.append(pcm)
                            else:
                                chunks.clear()
                                cacheable = False
                        await self._write_audio(pcm, ctx, category)
            self._finish_attempt(ctx, tts_attempt, "tts.stream", "ok",
                                 {"inputCharacters": len(sentence), "outputAudioBytes": bytes_out})
            if cacheable and chunks:
                while self._clip_cache and (len(self._clip_cache) >= 32 or self._clip_cache_bytes + bytes_out > 1_536_000):
                    _, removed = self._clip_cache.popitem(last=False)
                    self._clip_cache_bytes -= sum(map(len, removed))
                self._clip_cache[key] = tuple(chunks)
                self._clip_cache_bytes += bytes_out
        except BaseException as exc:
            status = "cancelled" if isinstance(exc, (asyncio.CancelledError, GeneratorExit)) else "error"
            self._finish_attempt(ctx, tts_attempt, "tts.stream", status,
                                 {"inputCharacters": len(sentence), "outputAudioBytes": bytes_out})
            if status == "error":
                # The turn's handler skips the spoken apology: it needs this voice.
                with contextlib.suppress(AttributeError, TypeError):
                    exc.voice_failed = True
            raise

    # -- public accessors (extended protocol, audit rework) ------------------

    def active_turn(self) -> InFlightTurn | None:
        """The route-owned in-flight turn, if any (public accessor)."""
        if self._in_flight is not None:
            return self._in_flight
        return None

    def output(self):
        """The audio-output boundary this route delivers into, if bound."""
        return self._audio_output

    def set_audio_output(self, output: Any) -> None:
        """Late-bind the audio-output boundary (the terminal surface opens its
        speaker *after* the route is built — builder.py stays untouched). The
        route delivers synthesized TTS PCM here; the output owns playout."""
        self._audio_output = output
        if self._prepared:
            self._warm_opening_lines()

    async def recover(self):
        """Cascade recovery restarts turns (ADR 0003 §2 non-goals: no
        mid-utterance audio splice)."""
        from sleeptalk.routes.base import RecoveryOutcome, RecoveryResult

        return RecoveryResult(
            outcome=RecoveryOutcome.RESTARTED,
            session_id=getattr(self, "_session_id", "") or "cascade-session",
        )

    async def settle_usage(self, turn_id: int, *, status: str) -> None:
        """Settle open usage attempts for a turn (idempotent per turn).

        Tracked-but-open attempts (crash/abandon recovery — gate finding)
        settle first; the per-turn settlement ALWAYS emits through the
        bound sink, so a no-op implementation fails the contract case.
        """
        if turn_id in self._settled_usage:
            return
        self._settled_usage.add(turn_id)
        emit = getattr(self, "_last_emit", None)
        session_id = getattr(self, "_session_id", "") or "cascade-session"
        for attempt_id in sorted(self._open_attempts.get(turn_id, ())):
            if attempt_id in self._settled_attempts or emit is None:
                continue
            self._settled_attempts.add(attempt_id)
            emit(
                usage_attempt_finished(
                    session_id,
                    turn_id,
                    attempt_id,
                    provider="cascade",
                    operation="agent.stream",
                    payment_mode=PaymentMode.BYOK,
                    status=AttemptStatus.CANCELLED,
                    units={},
                )
            )
        self._open_attempts.pop(turn_id, None)
        if emit is not None:
            emit(
                usage_attempt_finished(
                    session_id,
                    turn_id,
                    f":{turn_id}:cascade:settlement:1",
                    provider="cascade",
                    operation="agent.stream",
                    payment_mode=PaymentMode.BYOK,
                    status=AttemptStatus(value=status),
                )
            )

    def _commit_played(self, ctx: TurnContext, spoken: list[str]) -> None:
        if not spoken:
            return
        ctx.emit(
            transcript(
                ctx.session_id,
                ctx.turn_id,
                speaker=Speaker.ASSISTANT,
                text=" ".join(spoken),
                final=True,
                # The whole reply again, after each sentence went out on its
                # own: history keeps it; a live transcript already has it.
                rollup=True,
            )
        )

    async def _complete_local_intent(
        self,
        ctx: TurnContext,
        local: LocalIntent,
        spoken: list[str],
        generated: list[str],
    ) -> TurnOutcome:
        fields = {"source": "local_intent", "intent": local.intent}
        try:
            ctx.emit(
                response(
                    ctx.session_id,
                    ctx.turn_id,
                    kind=SessionEventType.RESPONSE_STARTED,
                    **fields,
                )
            )
            await self._speak_policy(local.reply, ctx, category="answer")
            spoken.append(local.reply)
            generated.append(local.reply)
            if self._speak is not None and not ctx.extras.get("_audio_output_emitted"):
                ctx.extras["_audio_output_emitted"] = True
                ctx.emit(response(ctx.session_id, ctx.turn_id,
                                  kind=SessionEventType.RESPONSE_FIRST_OUTPUT,
                                  output_kind="audio", measurement="speak_completed", **fields))
            self._commit_played(ctx, spoken)
            ctx.emit(
                response(
                    ctx.session_id,
                    ctx.turn_id,
                    kind=SessionEventType.RESPONSE_COMPLETED,
                    **fields,
                )
            )
            await self._cleanup_turn(ctx.turn_id, "completed")
            return TurnOutcome(
                status="completed",
                turn_id=ctx.turn_id,
                played=list(spoken),
                generated=list(generated),
            )
        finally:
            self._active_turn = None
            self._in_flight = None

    # -- Route protocol ---------------------------------------------------

    async def prepare(self, session: Any) -> None:
        if self._closed:
            raise RouteStateError("prepare after close")
        if self._prepared:
            return
        config = getattr(session, "config", None)
        surface = str(getattr(getattr(config, "transport", None), "type", "unknown"))
        identity = cascade_turn_identity(config, session_id=str(getattr(session, "session_id", "")))

        async def prepare_component(name: str, component: Any) -> None:
            warmup = getattr(component, "warmup", None)
            if not callable(warmup):
                return
            started = time.perf_counter()
            status = "ready"
            try:
                await warmup()
            except Exception as exc:
                status = "failed"
                logger.warning("%s warmup failed; first turn will retry: %s: %s",
                               name, type(exc).__name__, exc)
            log_latency_event(
                surface=surface, transport=surface,
                persona=str(getattr(getattr(config, "agent", None), "slug", "")),
                stage=f"{name}_prepare_finished", **identity, status=status,
                durationMs=round((time.perf_counter() - started) * 1000),
            )

        components = {
            id(component): (name, component)
            for name, component in (
                ("agent", self._agent), ("stt", self._stt),
                ("streaming_stt", self._stt_streaming), ("tts", self._tts),
            )
            if component is not None
        }
        await asyncio.gather(*(prepare_component(name, component) for name, component in components.values()))
        self._policy_runtime = VoicePolicyRuntime(
            agent=self._agent,
            stt=self._stt,
            streaming_stt=self._stt_streaming,
            policy=getattr(session, "policy", None),
            config=getattr(session, "config", None),
            timings=self._policy_timings,
        )
        self._prepared = True
        self._warm_opening_lines()
        if hasattr(session, "emit"):
            session.emit(
                session_state(
                    getattr(session, "session_id", ""),
                    LifecycleState.ACTIVE,
                )
            )

    async def run_turn(self, route_input: RouteInput, ctx: TurnContext) -> TurnOutcome:
        ctx.extras["_opening_gate"] = OpeningPreambleGate(
            already_claimed_by="suppressed" if ctx.extras.get("suppress_preamble") else "")
        identity = getattr(self._agent, "latency_identity", None)
        _mark_latency(ctx, "route_started", **(identity() if callable(identity) else {}))
        try:
            return await self._run_turn(route_input, ctx)
        finally:
            # The rotation moved on: make the lines it would say next.
            self._warm_opening_lines()
            ack = ctx.extras.pop("_smart_ack_task", None)
            if ack is not None:
                if not ack.done():
                    ack.cancel()
                await asyncio.gather(ack, return_exceptions=True)
            fields = {"measurement": "output_enqueue", "latencySchemaVersion": 2,
                      "preambleUsed": ctx.extras["_opening_gate"].audio_started}
            if "_first_output_ms" in ctx.extras:
                fields["endpointToFirstOutputMs"] = ctx.extras["_first_output_ms"]
            if "_first_answer_output_ms" in ctx.extras:
                fields["endpointToAnswerOutputMs"] = ctx.extras["_first_answer_output_ms"]
            _mark_latency(ctx, "turn_audio_summary", **fields)
            self._active_turn = None
            self._in_flight = None

    async def _response_sentences(self, user_text: str, ctx: TurnContext, generated: list[str]):
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=2)
        # Sentences received but not yet spoken: policy lines wait for them,
        # and the runtime's "still going" timer counts silence after them.
        ctx.extras["_answer_backlog"] = 0
        ctx.extras["_voice_quiet_for"] = lambda: self._voice_quiet_for(ctx)

        async def produce() -> None:
            token = LOCAL_PHRASE_STREAMING.set(bool(getattr(
                self._policy_runtime.policy, "local_responsiveness", False
            )))
            snappy_token = SNAPPY_PHRASE_STREAMING.set(bool(getattr(
                self._policy_runtime.policy, "snappy_responsiveness", False
            )))
            try:
                async with contextlib.aclosing(self._policy_runtime.respond(
                    user_text, ctx, speak_policy=self._speak_policy,
                    play_clip_policy=self._play_clip_policy,
                    suppress_preamble=bool(ctx.extras.get("suppress_preamble", False)),
                    opening_gate=ctx.extras["_opening_gate"],
                )) as stream:
                    async for sentence in stream:
                        generated.append(sentence)
                        ctx.extras["_answer_backlog"] += 1
                        await queue.put(sentence)
            finally:
                SNAPPY_PHRASE_STREAMING.reset(snappy_token)
                LOCAL_PHRASE_STREAMING.reset(token)

        producer = asyncio.create_task(produce(), name=f"cascade-sentences-{ctx.turn_id}")
        receive: asyncio.Task[str] | None = None
        try:
            while True:
                if not queue.empty():
                    yield queue.get_nowait()
                    # The caller asks for the next one once this one is said.
                    ctx.extras["_answer_backlog"] -= 1
                    continue
                if producer.done():
                    await producer
                    break
                receive = asyncio.create_task(queue.get())
                await asyncio.wait({receive, producer}, return_when=asyncio.FIRST_COMPLETED)
                if receive.done():
                    yield receive.result()
                    ctx.extras["_answer_backlog"] -= 1
                else:
                    receive.cancel()
                    await asyncio.gather(receive, return_exceptions=True)
                receive = None
        finally:
            ctx.extras["_answer_backlog"] = 0
            producer.cancel()
            if receive is not None:
                receive.cancel()
                await asyncio.gather(receive, return_exceptions=True)
            await asyncio.gather(producer, return_exceptions=True)

    async def _run_turn(self, route_input: RouteInput, ctx: TurnContext) -> TurnOutcome:
        if self._closed:
            raise RouteStateError("run_turn after close")
        if not self._prepared:
            raise RouteStateError("run_turn before prepare")
        self._active_turn = ctx.turn_id
        self._in_flight = InFlightTurn(turn_id=ctx.turn_id, task=asyncio.current_task())
        # The caller's silence starts here: canned lines count from it.
        ctx.extras.setdefault("_turn_started_at", time.monotonic())
        spoken: list[str] = []
        generated: list[str] = []
        self._spoken = spoken
        self._generated = generated

        assert self._policy_runtime is not None

        if ctx.extras.get("announcement"):
            # A line the session says on its own (a background job finished):
            # spoken like any reply, so barge-in and captions apply, with no
            # caller transcript and no agent. Shown as a live sentence: the
            # closing rollup alone is hidden when it follows another reply
            # with no caller words in between.
            ctx.emit(agent_event(ctx.session_id, ctx.turn_id, "sentence", text=route_input.text))
            return await self._complete_local_intent(
                ctx, LocalIntent(intent="announcement", reply=route_input.text), spoken, generated,
            )

        # STT (audit rework): PCM input is transcribed before the agent
        # runs — the stored STT adapter is exercised, never bypassed.
        user_text = route_input.text or ""
        stt_result = TranscriptionResult(
            user_text,
            "provided_text" if route_input.text else "none",
            {},
        )
        if (
            not user_text
            and route_input.pcm
            and (self._stt is not None or self._stt_streaming is not None)
        ):
            stt_attempt = self._start_attempt(ctx, "stt.stream", 1)
            transcribe_task = asyncio.create_task(
                self._transcribe(route_input.pcm, ctx),
                name=f"cascade-transcribe-{ctx.turn_id}",
            )
            smart_ack_task: asyncio.Task[str | None] | None = None
            if self._policy_runtime.instant_ack_mode == "smart":

                async def acknowledge_slow_transcription() -> str | None:
                    done, _pending = await asyncio.wait(
                        {transcribe_task},
                        timeout=self._policy_runtime.smart_ack_budget_seconds,
                    )
                    if done:
                        return None
                    return await self._policy_runtime.play_instant_ack(
                        "",
                        ctx,
                        speak_policy=self._speak_policy,
                        blind=True,
                        opening_gate=ctx.extras["_opening_gate"],
                    )

                smart_ack_task = asyncio.create_task(
                    acknowledge_slow_transcription(),
                    name=f"cascade-smart-ack-{ctx.turn_id}",
                )
                ctx.extras["_smart_ack_task"] = smart_ack_task
            try:
                try:
                    stt_result = await transcribe_task
                except asyncio.CancelledError:
                    self._finish_attempt(ctx, stt_attempt, "stt.stream", "cancelled", {})
                    return await self._cancelled_before_response(ctx)
                except Exception as exc:
                    self._finish_attempt(
                        ctx,
                        stt_attempt,
                        "stt.stream",
                        "error",
                        {"errorType": type(exc).__name__},
                    )
                    # A rate limit or outage at the speech service: say so, not silence.
                    if await self._tell_caller(LISTEN_FAILED_LINE, ctx):
                        with contextlib.suppress(AttributeError, TypeError):
                            exc.caller_told = True
                    raise
                user_text = stt_result.text
                self._finish_attempt(
                    ctx,
                    stt_attempt,
                    "stt.stream",
                    "ok",
                    {
                        "audioMs": len(route_input.pcm) / 96 if route_input.pcm else 0,
                        "inputCharacters": len(user_text),
                    },
                )
            finally:
                for task in (transcribe_task,):
                    if task is not None and not task.done():
                        task.cancel()
                        try:
                            await task
                        except asyncio.CancelledError:
                            pass

        _mark_latency(
            ctx,
            "stt_completed",
            sttSource=stt_result.source,
            textChars=len(user_text),
            **{key: value for key, value in stt_result.statistics.items() if key.startswith("pauseAhead")},
        )

        local = classify_local_intent(
            user_text, busy=bool(ctx.extras.get("agent_task_active"))
        )
        jobs = ctx.extras.get("jobs")
        if local is None and jobs is not None and user_text:
            job_reply = await _job_reply(jobs, user_text)
            if job_reply:
                local = LocalIntent(intent="background_job", reply=job_reply)
        transcript_fields: dict[str, Any] = {}
        if local is not None:
            transcript_fields["source"] = "local_intent"
            transcript_fields["intent"] = local.intent
        ctx.emit(
            transcript(
                ctx.session_id,
                ctx.turn_id,
                speaker=Speaker.USER,
                text=user_text or "(speech)",
                final=True,
                sttSource=stt_result.source,
                sttStatistics=stt_result.statistics,
                **transcript_fields,
            )
        )
        if local is not None:
            return await self._complete_local_intent(ctx, local, spoken, generated)
        ctx.emit(
            response(
                ctx.session_id,
                ctx.turn_id,
                kind=SessionEventType.RESPONSE_STARTED,
            )
        )
        agent_provider = type(self._agent).__name__
        agent_attempt_id = f"{ctx.session_id}:{ctx.turn_id}:{agent_provider.lower()}:agent.stream:1"
        ctx.emit(
            usage_attempt_started(
                ctx.session_id,
                ctx.turn_id,
                agent_attempt_id,
                provider=agent_provider,
                operation="agent.stream",
                payment_mode=PaymentMode.BYOK,
            )
        )
        try:
            async with contextlib.aclosing(self._response_sentences(user_text, ctx, generated)) as sentences:
                async for sentence in sentences:
                    if ctx.turn_id in self._cancelled_turns:
                        break
                    sentence = shape_voice_reply(sentence)
                    if not any(char.isalnum() for char in sentence):
                        continue
                    category = "acknowledgement" if is_generic_preamble(sentence, self._policy_runtime.policy) else "answer"
                    if category == "answer":
                        gate = ctx.extras["_opening_gate"]
                        await gate.claim("answer")
                        ack = ctx.extras.get("_smart_ack_task")
                        if ack is not None and not ack.done() and not gate.audio_committing:
                            ack.cancel()
                            await asyncio.gather(ack, return_exceptions=True)
                    if self._tts is not None and self._audio_output is not None:
                        await self._speak_audio(sentence, ctx, category=category)
                    if self._speak is not None:
                        await self._speak_through_surface(sentence, ctx)
                        if category == "answer" and not ctx.extras.get("_audio_output_emitted"):
                            ctx.extras["_audio_output_emitted"] = True
                            ctx.emit(response(ctx.session_id, ctx.turn_id,
                                              kind=SessionEventType.RESPONSE_FIRST_OUTPUT,
                                              output_kind="audio", measurement="speak_completed"))
                    spoken.append(sentence)
            status = "cancelled" if ctx.turn_id in self._cancelled_turns else "completed"
            if status == "completed" and not generated:
                # A turn with no answer at all (an agent that returns nothing,
                # like `openclaw acp` when its provider rejects it).
                _mark_latency(ctx, "agent_empty_answer", once=False)
                await self._tell_caller(AGENT_SILENT_LINE, ctx)
            self._commit_played(ctx, spoken)
            ctx.emit(
                response(
                    ctx.session_id,
                    ctx.turn_id,
                    kind=(
                        SessionEventType.RESPONSE_CANCELLED
                        if status == "cancelled"
                        else SessionEventType.RESPONSE_COMPLETED
                    ),
                )
            )
            ctx.emit(
                usage_attempt_finished(
                    ctx.session_id,
                    ctx.turn_id,
                    agent_attempt_id,
                    provider=agent_provider,
                    operation="agent.stream",
                    payment_mode=PaymentMode.BYOK,
                    status=(AttemptStatus.CANCELLED if status == "cancelled" else AttemptStatus.OK),
                    units={
                        "inputCharacters": len(route_input.text or ""),
                        "outputCharacters": sum(len(s) for s in generated),
                    },
                )
            )
            await self._cleanup_turn(ctx.turn_id, status)
            return TurnOutcome(
                status=status,
                turn_id=ctx.turn_id,
                played=list(spoken),  # sentence-granularity accounting
                generated=list(generated),
            )
        except Exception as exc:
            # Agent failures settle the open attempt (audit rework:
            # exceptions must not leave an unsettled usage attempt).
            self._finish_attempt(
                ctx,
                agent_attempt_id,
                "agent.stream",
                "error",
                {"inputCharacters": len(user_text), "errorType": type(exc).__name__},
            )
            told = False
            if not spoken and ctx.turn_id not in self._cancelled_turns:
                # A voice that just failed would fail the apology too, after
                # its own timeouts (31 s of silence for a stalled one).
                told = not getattr(exc, "voice_failed", False) and await self._tell_caller(_failure_line(exc), ctx)
                if told:
                    # Surfaces with their own apology (Discord) skip it for this one.
                    with contextlib.suppress(AttributeError, TypeError):
                        exc.caller_told = True
                elif (tone := voice_unavailable_clip_path()) is not None:
                    # The voice itself failed: a tone, so the caller isn't
                    # left in silence wondering whether they were heard.
                    await self._play_clip_policy("voice_unavailable", ctx, path=tone)
            self._commit_played(ctx, spoken)
            if not told and ctx.turn_id not in self._cancelled_turns and len(generated) > len(spoken):
                # The voice failed (and with it the spoken apology): the
                # caller heard nothing, so the page shows what wasn't said.
                for text in (" ".join(generated[len(spoken):]), VOICE_FAILED_LINE):
                    ctx.emit(transcript(ctx.session_id, ctx.turn_id, speaker=Speaker.ASSISTANT,
                                        text=text, final=True))
            ctx.emit(
                response(ctx.session_id, ctx.turn_id, kind=SessionEventType.RESPONSE_CANCELLED)
            )
            raise
        except asyncio.CancelledError:
            # The turn task was cancelled (barge-in or close). uncancel()
            # lets the cleanup awaits below run to completion instead of
            # re-delivering cancellation at each suspension point.
            task = asyncio.current_task()
            if task is not None and hasattr(task, "uncancel"):
                task.uncancel()
            ctx.emit(
                usage_attempt_finished(
                    ctx.session_id,
                    ctx.turn_id,
                    agent_attempt_id,
                    provider=agent_provider,
                    operation="agent.stream",
                    payment_mode=PaymentMode.BYOK,
                    status=AttemptStatus.CANCELLED,
                    units={
                        "inputCharacters": len(route_input.text or ""),
                        "outputCharacters": sum(len(s) for s in generated),
                    },
                )
            )
            self._commit_played(ctx, spoken)
            ctx.emit(
                response(
                    ctx.session_id,
                    ctx.turn_id,
                    kind=SessionEventType.RESPONSE_CANCELLED,
                )
            )
            await self._cleanup_turn(ctx.turn_id, "cancelled")
            return TurnOutcome(
                status="cancelled",
                turn_id=ctx.turn_id,
                played=list(spoken),
                generated=list(generated),
            )
        finally:
            self._active_turn = None
            self._in_flight = None

    async def _cancelled_before_response(self, ctx: TurnContext) -> TurnOutcome:
        """Settle cancellation while STT or an instant ack owns the turn."""
        task = asyncio.current_task()
        if task is not None and hasattr(task, "uncancel"):
            task.uncancel()
        ctx.emit(
            response(
                ctx.session_id,
                ctx.turn_id,
                kind=SessionEventType.RESPONSE_CANCELLED,
            )
        )
        await self._cleanup_turn(ctx.turn_id, "cancelled")
        self._active_turn = None
        self._in_flight = None
        return TurnOutcome(
            status="cancelled",
            turn_id=ctx.turn_id,
            played=[],
            generated=[],
        )

    async def cancel_turn(self, turn_id: int) -> None:
        # ADR 0003 §4 clause 5: cancelling an idle route is a no-op.
        if self._closed or self._active_turn != turn_id:
            return
        self._cancelled_turns.add(turn_id)
        # Install the output fence before cancelling producers.  A TTS chunk
        # already crossing an await boundary must not be able to restart the
        # same turn after the transport has been cleared.
        discard_turn = getattr(self._audio_output, "discard_turn", None)
        if callable(discard_turn):
            await discard_turn(turn_id, "barge-in")
        if self._policy_runtime is not None:
            await self._policy_runtime.cancel(turn_id)
        # Queued-audio discard: drop any synthesis/playback backlog for
        # this turn so nothing stale reaches the surface after barge-in.
        if self._discard_queue is not None and turn_id not in self._discarded_turns:
            self._discarded_turns.add(turn_id)
            self._discard_queue(turn_id)
        if self._in_flight is not None:
            self._in_flight.cancel()
        # Propagation order (§4 clause 2): (b) cancel route-internal tasks,
        # then (c) abort upstream work. Playback stop (a) is the surface's
        # to perform on the interruption event; agent abort is ours.
        if self._policy_runtime is None:
            abort = getattr(self._agent, "abort", None)
            if abort is not None and turn_id not in self._aborted_turns:
                self._aborted_turns.add(turn_id)
                await abort()
        # TTS/provider work is aborted too (audit rework): any abort hook
        # on the TTS adapter is invoked exactly once per cancelled turn.
        tts_abort = getattr(self._tts, "abort", None)
        if tts_abort is not None and turn_id not in self._tts_aborted_turns:
            self._tts_aborted_turns.add(turn_id)
            tts_abort_result = tts_abort()
            if hasattr(tts_abort_result, "__await__"):
                await tts_abort_result
        emit = getattr(self, "_last_emit", None)
        if emit is not None:
            emit(
                interruption(
                    getattr(self, "_session_id", ""),
                    turn_id,
                    source=InterruptionSource.USER_SPEECH,
                    phase=InterruptionPhase.ANSWERING,
                    played=list(self._spoken),
                    generated=list(self._generated),
                )
            )

    async def interrupt_presentation(self, turn_id: int | None = None) -> None:
        """Cascade has no detached task, so barge-in cancels its active turn."""
        if turn_id is None:
            turn_id = self._active_turn
        if turn_id is not None:
            await self.cancel_turn(turn_id)

    async def cancel_task(self, task_id: str) -> bool:
        """Cascade turns have no independently addressable specialist task."""
        del task_id
        return False

    async def _cleanup_turn(self, turn_id: int, status: str) -> None:
        """Reservation-neutral turn cleanup: runs exactly once per turn
        (completed, cancelled, or errored) — reservations are the surface's
        concern; the route only guarantees the hook fires and never raises."""
        if turn_id in self._cleaned_turns:
            return
        self._cleaned_turns.add(turn_id)
        if (
            self._discard_queue is not None
            and status != "completed"
            and turn_id not in self._discarded_turns
        ):
            self._discarded_turns.add(turn_id)
            self._discard_queue(turn_id)
        if self._on_cleanup is not None:
            try:
                await self._on_cleanup(turn_id, status)
            except Exception:
                pass  # cleanup must never fail the turn outcome

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._warm_task is not None and not self._warm_task.done():
            self._warm_task.cancel()
            await asyncio.gather(self._warm_task, return_exceptions=True)
        self._clip_cache.clear()
        self._clip_cache_bytes = 0
        if self._in_flight is not None:
            self._in_flight.cancel()

        async def close_component(component: Any) -> None:
            close = getattr(component, "close", None)
            if close is not None:
                result = close()
                if inspect.isawaitable(result):
                    await result

        components = (
            (self._policy_runtime, self._tts, self._agent)
            if self._policy_runtime is not None
            else (self._stt, self._stt_streaming, self._tts, self._agent)
        )
        unique = {id(component): component for component in components if component is not None}
        results = await asyncio.gather(
            *(close_component(component) for component in unique.values()),
            return_exceptions=True,
        )
        failures = [result for result in results if isinstance(result, Exception)]
        if failures:
            raise ExceptionGroup("Cascade component cleanup failed", failures)

    # -- cascade-specific hooks -------------------------------------------

    def bind(self, session_id: str, emit: Callable[[Any], None]) -> None:
        """Late-bind identity + event sink (builder wires this in
        ``r2-cascade-route``); keeps the constructor surface small."""
        self._session_id = session_id
        self._last_emit = emit
