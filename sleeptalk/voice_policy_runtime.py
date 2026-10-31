"""Shared Cascade speech policy, transcription, and progress narration.

CascadeRoute owns this runtime. Transports supply capture and playback through
the shared session and route contracts. No platform SDK is imported here.
"""

from __future__ import annotations

import asyncio
import logging
import contextlib
import inspect
import itertools
import sys
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import asdict, dataclass
from typing import Any

from sleeptalk.opening_preamble_gate import OpeningPreambleGate
from sleeptalk.pause_ahead import PauseAheadTranscriber
from sleeptalk.speech.contracts import RESTSTT, StreamingSTT
from sleeptalk.adapter import (
    AssistantDelta,
    ControlAccepted,
    PermissionDecision,
    PermissionRequested,
    ResolvePermission,
    ToolEvent,
    ToolPhase,
    WorkerEvent,
    adapter_control,
    agent_signal,
    typed_events,
)
from sleeptalk.session_event_log import append_probe_record
from sleeptalk.session_events import (
    SessionEventType,
    adapter_session_event,
    agent_event,
    progress,
    tool_event,
)
from sleeptalk.voice.subagent import (
    FloorState,
    SpeechKind,
    WorkerVoiceState,
    admit_worker,
    decide_worker_speech,
    is_mid_turn_status_question,
    is_status_question,
    note_start,
    remember_ack,
    status_line,
    take_immediate,
    take_queued,
)
from sleeptalk.streaming import (
    elapsed_suffix,
    is_generic_preamble,
    select_ack_pool,
)
from sleeptalk.streaming import (
    next_reassure_line as _shared_next_reassure_line,
)
from sleeptalk.summarize import ends_sentence, spoken_budget_chars, summarize_tail
from sleeptalk.sounds import working_slices
from sleeptalk.text import is_hearable
from sleeptalk.voice_loop.telemetry import VoiceLatencyTrace
from sleeptalk.voice_prompt import prepare_voice_prompt
from sleeptalk.jobs import (
    HANDOFF_INSTRUCTION,
    STARTED_LINE,
    CancelJob,
    JobMarkerFilter,
    JobNote,
    StartJob,
    cancel_meant,
    with_raw_bodies,
)
from sleeptalk.policy import narration_style_or_default, reply_length_or_default
from sleeptalk.voice_tool_narration import (
    choose_tool_narration,
    play_tool_keyboard,
    speak_tool_progression,
    tool_status_line,
)

_OpeningPreambleGate = OpeningPreambleGate

_OPENING_MARKER_KEY = "_latency_opening_marker_ms"
_ANSWER_TEXT_KEY = "_latency_answer_text_ms"

_logger = logging.getLogger("sleeptalk.voice_policy_runtime")


def _latency_trace(ctx: Any) -> VoiceLatencyTrace | None:
    extras = getattr(ctx, "extras", None) or {}
    if not isinstance(extras, dict):
        return None
    trace = extras.get("latency_trace")
    return trace if hasattr(trace, "mark") else None


def _mark_latency(
    ctx: Any, stage: str, *, once: bool = True, **fields: Any
) -> dict[str, Any] | None:
    trace = _latency_trace(ctx)
    if trace is None:
        return None
    try:
        if once:
            marker = getattr(trace, "mark_once", None)
            if callable(marker):
                return marker(stage, **fields)
            extras = getattr(ctx, "extras", None)
            if isinstance(extras, dict):
                seen = extras.setdefault("_latency_once", set())
                if stage in seen:
                    return None
                seen.add(stage)
        return trace.mark(stage, **fields)
    except Exception:
        return None


def _note_opening(ctx: Any, event: dict[str, Any] | None) -> None:
    extras = getattr(ctx, "extras", None)
    if event is None or not isinstance(extras, dict) or extras.get(_OPENING_MARKER_KEY) is not None:
        return
    elapsed = event.get("elapsedMs")
    if isinstance(elapsed, (int, float)):
        extras[_OPENING_MARKER_KEY] = int(elapsed)


def _note_answer(ctx: Any, event: dict[str, Any] | None) -> None:
    extras = getattr(ctx, "extras", None)
    if event is None or not isinstance(extras, dict):
        return
    elapsed = event.get("elapsedMs")
    if not isinstance(elapsed, (int, float)):
        return
    value = int(elapsed)
    extras.setdefault(_ANSWER_TEXT_KEY, value)
    extras.setdefault(_OPENING_MARKER_KEY, value)


def _mark_turn_text_summary(ctx: Any) -> None:
    extras = getattr(ctx, "extras", None)
    if not isinstance(extras, dict):
        return
    opening = extras.get(_OPENING_MARKER_KEY)
    answer = extras.get(_ANSWER_TEXT_KEY)
    if opening is None:
        opening = answer
    fields: dict[str, Any] = {}
    if isinstance(opening, int):
        fields["openingMarkerMs"] = opening
    if isinstance(answer, int):
        fields["answerTextMs"] = answer
    if not fields:
        return
    fields["preambleUsed"] = bool(
        isinstance(opening, int) and (not isinstance(answer, int) or opening < answer)
    )
    _mark_latency(ctx, "turn_text_summary", once=True, **fields)


# Turn pacing constants (owned here since PR mt-runner-extraction; the
# Discord session module imports them for back-compat).
# How long a turn runs silent before a canned opening line, counted from
# when the caller's turn is judged over (transcription included). 1.2 s (was 0.4 s
# from the agent request): measured 2026-10-04 over 30 fast turns, the 0.4 s
# timer fired on 26 of them and held the real answer back by a median 950 ms
# against 411 ms without, ten times behind an answer that was already
# written. A pause under about a second sounds like conversation.
_ACK_DELAY_SECONDS = 1.2
# The agent is mid-sentence at that point: this much longer for the sentence.
_SENTENCE_GRACE_SECONDS = 0.4
# The pools an opening line can come from; the route makes their next lines
# ahead of time, so a line that is needed plays at once.
OPENING_POOLS = ("ack", "ack_action", "action:task", "ack:empathy", "action", "working")
# Tier-2 action phrase. 2.0s (was 2.5): with a smart ack claiming the floor
# at ~1s post-commit, 2.5s left >1.5s of dead air between ack and action —
# over the research's friction threshold (VOICE_PREAMBLES_PLAN.md).
_ACTION_DELAY_SECONDS = 2.0
# Keyboard style: how long a turn waits in silence before the typing sound
# starts. Quick answers arrive first and never hear it.
_WORKING_SOUND_DELAY_SECONDS = 0.8
_LOOP_DELAY_SECONDS = 2.0
_LOOP_GAP_SECONDS = 1.4
_MIDTURN_GAP_SECONDS = 3.0

@dataclass(frozen=True)
class VoicePolicyTimings:
    """Public timing seam for the shared voice policy.

    Production uses the characterized values below.  Tests may inject short
    delays through :class:`CascadeRoute`; transports never own these timers.
    ``None`` delegates to the resolved root/profile policy.
    """

    ack_delay_seconds: float = _ACK_DELAY_SECONDS
    sentence_grace_seconds: float = _SENTENCE_GRACE_SECONDS
    action_delay_seconds: float = _ACTION_DELAY_SECONDS
    keepalive_interval_seconds: float | None = None
    # Legacy timing seams, ignored: long tasks keep their original request.
    long_task_check_in_seconds: float | None = None
    long_task_auto_continue_seconds: float | None = None
    working_sound_delay_seconds: float = _WORKING_SOUND_DELAY_SECONDS


@dataclass(frozen=True)
class TranscriptionResult:
    text: str
    source: str
    statistics: dict[str, Any]


_PERMISSION_YES = re.compile(
    r"^(?:yes|yeah|yep|yup|sure|ok(?:ay)?|go ahead|allow(?: it)?|approve(?: it)?|do it|please do)\b", re.IGNORECASE)
_PERMISSION_NO = re.compile(
    r"^(?:no|nope|don'?t|do not|deny|decline|refuse|never mind)\b", re.IGNORECASE)


def permission_answer(text: str) -> PermissionDecision | None:
    """A spoken yes or no to a permission question, or None for anything else."""
    words = " ".join(str(text or "").split()).strip(" ,.!?")
    if _PERMISSION_NO.match(words):
        return PermissionDecision.DENY
    if _PERMISSION_YES.match(words):
        return PermissionDecision.ALLOW_ONCE
    return None


def permission_question(prompt: str, *, spoken: bool = True) -> str:
    """The question when the agent asks to use a tool. Spoken, a command
    line is never read out ("`cd … && grep -n …`? Say yes or no." was every
    character of it); the page shows the full request (``spoken=False``)."""
    asked = " ".join(str(prompt or "").split()).strip(" ?.`") or "Allow the agent to continue"
    if spoken and (not is_hearable(asked) or len(asked) > 120):
        return "The agent wants to run a command. Say yes or no."
    return f"{asked}? Say yes or no."


class VoicePolicyRuntime:
    """Canonical provider-neutral voice behavior for cascade sessions.

    This class is intentionally package-owned: surface code only streams PCM,
    receives normalized events, and implements ``AudioOutput``.  Streaming STT
    finalization, REST fallback, response pacing, progress narration, and
    uninterrupted long-running work all live here.
    """

    def __init__(
        self,
        *,
        agent: Any,
        stt: RESTSTT | None = None,
        streaming_stt: StreamingSTT | None = None,
        policy: Any = None,
        config: Any = None,
        timings: VoicePolicyTimings | None = None,
    ) -> None:
        self.agent = agent
        self.stt = stt
        self.streaming_stt = streaming_stt
        self.policy = policy
        self.config = config
        self.timings = timings or VoicePolicyTimings()
        self._streamed_participants: set[int] = set()
        self._pause_flushes: set[asyncio.Future] = set()
        # An agent's question waiting on the caller: (turn context, request).
        self._pending_permission: tuple[Any, PermissionRequested] | None = None
        # The turn whose agent is working, for status questions asked meanwhile.
        self._working_now: tuple[Any, ...] | None = None
        # Turns that handed work to a background job (see cancel).
        self._job_turns: dict[int, Any] = {}
        self._stream_errors: dict[int, Exception] = {}
        self._tasks: dict[int, set[asyncio.Task[Any]]] = {}
        self._cancelled: set[int] = set()
        self._closed = False
        self._pool_rotation: dict[str, int] = {}
        self._providers_closed: set[int] = set()
        self._agent_request_epochs: dict[int, int] = {}
        self._agent_aborted_epochs: set[tuple[int, int]] = set()
        self._active_agent_requests: dict[int, int] = {}
        self._pause_ahead: PauseAheadTranscriber | None = None
        transcribe_pcm = getattr(stt, "transcribe_pcm", None)
        if streaming_stt is None and callable(transcribe_pcm) and self.pause_ahead_ms > 0:
            self._pause_ahead = PauseAheadTranscriber(transcribe_pcm)

    @property
    def pause_ahead_ms(self) -> int:
        """Pause length that starts an early batch transcription; 0 is off."""
        return int(getattr(getattr(self.config, "stt", None), "pause_ahead_ms", 0) or 0)

    @property
    def listens_ahead(self) -> bool:
        """A pause brings an early transcript (a batch listener with pause-ahead on)."""
        return self._pause_ahead is not None and not self._closed

    def transcribe_ahead(self, pcm: bytes, *, participant_id: int = 0) -> asyncio.Task[str] | None:
        """Start transcribing the utterance so far; it is used only if nothing follows.

        A streaming listener is asked to finalize instead: its completion
        signal then carries the whole text, which endpointing re-reads.
        """
        if self._closed or not pcm:
            return None
        flush = getattr(self.streaming_stt, "flush_at_pause", None)
        if callable(flush) and participant_id in self._streamed_participants:
            task = asyncio.ensure_future(flush(participant_id))
            self._pause_flushes.add(task)
            task.add_done_callback(self._pause_flush_done)
            return None
        if self._pause_ahead is None:
            return None
        return self._pause_ahead.start(participant_id, pcm)

    def _pause_flush_done(self, task: asyncio.Future) -> None:
        self._pause_flushes.discard(task)
        if not task.cancelled():
            task.exception()  # retrieved: a failed flush only means the slower wait

    async def reset_capture(self) -> None:
        """Forget prior-call speech while retaining the configured providers."""
        self._streamed_participants.clear()
        self._stream_errors.clear()
        reset_previews = getattr(self.stt, "reset_previews", None)
        if callable(reset_previews):
            reset_previews()
        if self._pause_ahead is not None:
            self._pause_ahead.reset()
        reset = getattr(self.streaming_stt, "reset", None)
        if reset is not None:
            await reset()

    async def append_audio(self, pcm: bytes, *, participant_id: int = 0) -> None:
        """Feed live PCM to streaming STT through the public session ingress."""
        if self._closed or not pcm:
            return
        if self.streaming_stt is None:
            preview = getattr(self.stt, "append_preview", None)
            if (callable(preview) and getattr(self.policy, "input_previews", True)
                    and getattr(getattr(self.config, "transport", None), "type", "")
                    in {"web", "terminal", "discord"}):
                preview(participant_id, pcm)
            return
        append = getattr(self.streaming_stt, "append_pcm", None)
        if not callable(append):
            return
        self._streamed_participants.add(participant_id)
        try:
            await append(participant_id, pcm)
        except Exception as exc:  # noqa: BLE001 - REST remains the health fallback
            self._stream_errors[participant_id] = exc

    def input_transcript_preview(self, participant_id: int = 0) -> str:
        """Display-only batch previews are never turn-completion evidence."""
        preview = getattr(self.stt, "input_transcript_preview", None)
        return preview(participant_id) if callable(preview) and not self._closed else ""

    def turn_completion_signal(self, participant_id: int = 0) -> Any:
        """Expose provider completion evidence through the kernel boundary."""
        signal = getattr(self.streaming_stt, "turn_completion_signal", None)
        if not callable(signal):
            return None
        try:
            return signal(participant_id)
        except Exception:  # noqa: BLE001 - endpointing degrades to acoustic policy
            return None

    async def transcribe(self, pcm: bytes, *, participant_id: int = 0) -> TranscriptionResult:
        """Finalize streamed STT, falling back to REST only on health failure."""
        clear_preview = getattr(self.stt, "clear_preview", None)
        if callable(clear_preview):
            clear_preview(participant_id)
        stream_error: Exception | None = self._stream_errors.pop(participant_id, None)
        streamed = ""
        can_finish = (
            participant_id in self._streamed_participants
            and stream_error is None
            and callable(getattr(self.streaming_stt, "finish_transcription", None))
        )
        if can_finish:
            try:
                streamed = await self.streaming_stt.finish_transcription(
                    participant_id,
                    fallback_pcm=pcm,
                    timeout_seconds=self._stt_timeout("streaming_timeout_seconds", 1.4),
                )
            except Exception as exc:  # noqa: BLE001 - any stream failure falls back
                stream_error = exc
                streamed = ""
            finally:
                self._streamed_participants.discard(participant_id)
        else:
            self._streamed_participants.discard(participant_id)

        if stream_error is not None:
            discard = getattr(self.streaming_stt, "discard_transcription", None)
            if callable(discard):
                await discard(participant_id)
        statistics = self._streaming_statistics(participant_id)
        # A stream whose final text drops words (Cartesia's ink-whisper loses
        # Spanish tails and Chinese clauses) asks for the whole utterance by
        # REST; the streamed text stays the fallback.
        whole = bool(streamed) and bool(getattr(self.streaming_stt, "final_from_rest", False))
        if streamed and not whole and not self._looks_truncated(pcm, streamed, statistics):
            return TranscriptionResult(streamed.strip(), "streaming", statistics)
        if not can_finish and stream_error is None and self._pause_ahead is not None:
            early = await self._take_pause_ahead(participant_id, pcm)
            if isinstance(early, TranscriptionResult):
                return early
            statistics = {**statistics, **early}

        transcribe_pcm = getattr(self.stt, "transcribe_pcm", None)
        if not callable(transcribe_pcm):
            if stream_error is not None:
                raise stream_error
            return TranscriptionResult(
                streamed.strip(), "streaming" if can_finish else "none", statistics
            )
        try:
            rest = await asyncio.wait_for(
                transcribe_pcm(pcm), timeout=self._batch_budget_seconds(pcm)
            )
        except TimeoutError:
            # A coverage fallback is an attempt to improve a usable stream,
            # not permission to turn that stream into silence for the user.
            if streamed.strip():
                return TranscriptionResult(
                    streamed.strip(), "streaming_rest_timeout", statistics
                )
            raise
        except Exception:
            if whole and streamed.strip():
                return TranscriptionResult(streamed.strip(), "streaming_rest_failed", statistics)
            raise
        if whole and not (rest or "").strip():
            return TranscriptionResult(streamed.strip(), "streaming_rest_empty", statistics)
        if whole:
            source = "rest_whole_utterance"
        elif stream_error is not None:
            source = "rest_stream_error"
        elif streamed:
            source = "rest_coverage_fallback"
        elif can_finish:
            source = "rest_stream_empty"
        else:
            source = "rest"
        return TranscriptionResult((rest or "").strip(), source, statistics)

    async def _take_pause_ahead(
        self, participant_id: int, pcm: bytes
    ) -> TranscriptionResult | dict[str, Any]:
        """The early transcript when it heard this exact utterance, else why not."""
        take = self._pause_ahead.take(participant_id, pcm)
        fields: dict[str, Any] = {"pauseAhead": take.outcome, **{
            f"pauseAhead{key[0].upper()}{key[1:]}": value for key, value in take.stats.items()
        }}
        if take.task is None:
            if take.head_start_ms:
                fields["pauseAheadHeadStartMs"] = take.head_start_ms
            return fields
        waited_from = time.perf_counter()
        budget = self._batch_budget_seconds(pcm) - (waited_from - take.started_at)
        try:
            text = await asyncio.wait_for(asyncio.shield(take.task), timeout=max(budget, 0.1))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the normal full transcription still runs
            return {**fields, "pauseAhead": "failed", "pauseAheadError": type(exc).__name__}
        if not (text or "").strip():
            return {**fields, "pauseAhead": "empty"}
        fields["pauseAheadHeadStartMs"] = take.head_start_ms
        fields["pauseAheadWaitMs"] = int((time.perf_counter() - waited_from) * 1000)
        return TranscriptionResult(text.strip(), "pause_ahead", fields)

    async def respond(
        self,
        text: str,
        ctx: Any,
        *,
        speak_policy: Callable[[str, Any], Awaitable[None]],
        # Optional: play an audio clip instead of speaking. Surfaces that
        # cannot play files simply omit it, and the keyboard style degrades
        # to silence rather than failing the turn.
        play_clip_policy: Callable[[str, Any], Awaitable[bool]] | None = None,
        instant_ack_pool: str | None = None,
        suppress_preamble: bool = False,
        opening_gate: OpeningPreambleGate | None = None,
    ) -> AsyncIterator[str]:
        """Yield agent sentences while the runtime owns voice-policy tasks."""
        first_sentence = asyncio.Event()
        response_done = asyncio.Event()
        agent_activity = asyncio.Event()
        tool_seen = asyncio.Event()
        text_seen = asyncio.Event()
        current_tool = [""]
        current_detail = [""]
        spoken_narration_keys: set[str] = set()
        generic_narration_spoken = [False]
        first_tool_task: asyncio.Task[None] | None = None
        used_reassure: set[str] = set()
        started_at = time.monotonic()
        cwd = str(getattr(self.agent, "cwd", "") or "")
        # Background jobs: the agent may hand long work off in a <job> block,
        # and hears how a running job is going (sleeptalk/jobs.py).
        jobs = ctx.extras.get("jobs")
        job_filter = JobMarkerFilter() if jobs is not None else None
        job_started = [False]
        job_refused = [False]
        # The reply's raw text: a job's brief is taken from it, not from
        # sentences shaped for speech. Most adapters report each chunk twice
        # (typed, and as a workbench "text" event); the second copy is used
        # only from adapters that send nothing typed, or every chunk doubled
        # ("expect it on expect it on" in a Claude Code job's brief).
        raw_reply: list[str] = []
        raw_untyped: list[str] = []
        raw_taken = [0, 0]
        # The line to say after a <job-cancel/> if the reply says nothing else.
        job_cancel = [""]
        job_preface = (
            "\n\n".join(part for part in (HANDOFF_INSTRUCTION, jobs.context_block()) if part)
            if jobs is not None
            else ""
        )
        agent_prompt = prepare_voice_prompt(
            text,
            cwd=cwd,
            preface=job_preface,
            # Summarized asks the agent for a short answer as well as trimming
            # what comes back. The ask is the half that actually shortens the
            # turn; the budget below is the backstop for when it is ignored.
            brief=reply_length_or_default(getattr(self.policy, "reply_length", "full"))
            == "summarized",
            concise=reply_length_or_default(getattr(self.policy, "reply_length", "full"))
            == "concise",
        )
        opening_gate = opening_gate or _OpeningPreambleGate(
            already_claimed_by=(
                "suppressed"
                if suppress_preamble
                else f"instant_ack:{instant_ack_pool}"
                if instant_ack_pool
                else ""
            )
        )
        roster = ctx.extras.get("roster")
        # What a "what are you doing?" mid-work is answered from.
        self._working_now = (ctx, current_tool, current_detail, started_at, speak_policy, roster)
        worker_voice = ctx.extras.get("worker_voice")
        if worker_voice is None:
            worker_voice = WorkerVoiceState()
            ctx.extras["worker_voice"] = worker_voice
        bot_speaking = [False]
        # Reply-length budget: characters of answer already spoken this turn,
        # and the sentences the budget held back from the mouth. Both stay
        # empty on the default "full" setting.
        spoken_chars = 0
        held_back: list[str] = []
        # The last spoken chunk was a phrase cut, not a sentence end.
        mid_sentence = False
        # Everything this turn said, for the first handoff's keep-talking line.
        spoken_lines: list[str] = []

        def emit_spoken(line: str, **marks: bool) -> str:
            if line.strip():
                spoken_lines.append(line)
                ctx.emit(
                    agent_event(
                        ctx.session_id,
                        ctx.turn_id,
                        "sentence",
                        text=line,
                        # SentenceStreamer cuts only at spaces.
                        spaced=True,
                        **marks,
                    )
                )
            return line

        if roster is not None and is_status_question(text):
            snapshot = roster.snapshot()
            if snapshot.workers:
                yield emit_spoken(status_line(snapshot))
                return

        def on_agent_event(raw: Any) -> None:
            nonlocal first_tool_task
            if isinstance(raw, PermissionRequested):
                # The agent waits on the caller: ask out loud. Nothing asked
                # it before, so an agent set to ask hung on its first write.
                self._pending_permission = (ctx, raw)
                ctx.emit(agent_event(ctx.session_id, ctx.turn_id, "permission",
                                     requestId=raw.request_id, prompt=raw.prompt))
                ctx.emit(progress(ctx.session_id, ctx.turn_id, pool="permission",
                                  text=permission_question(raw.prompt, spoken=False)))
                task = asyncio.create_task(
                    speak_policy(permission_question(raw.prompt), ctx),
                    name=f"voice-policy-permission-{ctx.turn_id}",
                )
                self._tasks.setdefault(ctx.turn_id, set()).add(task)
                agent_activity.set()
                return
            if isinstance(raw, WorkerEvent):
                self._on_worker_event(
                    raw,
                    ctx=ctx,
                    roster=roster,
                    worker_voice=worker_voice,
                    bot_speaking=bot_speaking[0],
                )
                agent_activity.set()
                return
            if isinstance(raw, AssistantDelta):
                raw_reply.append(raw.text)
            if isinstance(raw, AssistantDelta) and raw.text.strip():
                ctx.emit(
                    agent_event(
                        ctx.session_id,
                        ctx.turn_id,
                        "text",
                        text=raw.text,
                    )
                )
                text_seen.set()
                agent_activity.set()
                _mark_latency(
                    ctx,
                    "agent_first_text_delta",
                    once=True,
                    provider=type(self.agent).__name__,
                    operation="agent.stream",
                )
                return
            emitted_tool = False
            if isinstance(raw, ToolEvent):
                if raw.phase in {ToolPhase.COMPLETED, ToolPhase.FAILED} or (
                    raw.phase is ToolPhase.STARTED and raw.name
                ):
                    ctx.emit(adapter_session_event(ctx.session_id, ctx.turn_id, raw))
                    emitted_tool = True
            signal = agent_signal(raw)
            if signal is None:
                agent_activity.set()
                return
            name, fields = signal
            specific_tool = ""
            event_fields = {
                ("toolName" if key == "name" else key): value
                for key, value in fields.items()
                if key != "raw"
            }
            if name == "latency":
                stage = fields.get("stage", "")
                if stage in {"agent_admission_started", "agent_admitted", "agent_prompt_sent"}:
                    _mark_latency(ctx, stage, **{
                        key: value for key, value in fields.items()
                        if key not in {"stage", "kind", "name", "raw"}
                    })
                return
            if name == "thinking":
                _mark_latency(ctx, "agent_first_thinking")
            if name in {"tool", "tool_start"}:
                _mark_latency(ctx, "agent_first_tool")
            if name == "tooldetail":
                detail = str(fields.get("detail", "")).strip()
                if detail:
                    current_detail[0] = detail
            elif name in {"tool", "tool_start"}:
                specific_tool = str(fields.get("tool") or fields.get("name") or "other")
                phrases = getattr(self.policy, "narration_phrases", {}) or {}
                if specific_tool:
                    current_tool[0] = (
                        specific_tool if specific_tool in phrases else "other"
                    )
                tool_seen.set()
                if isinstance(raw, ToolEvent) and raw.detail is not None:
                    current_detail[0] = raw.detail
                elif name == "tool_start":
                    current_detail[0] = str(fields.get("detail") or "")
            elif name == "text":
                if not isinstance(raw, AssistantDelta):
                    raw_untyped.append(str(fields.get("text") or ""))
                text_seen.set()
                _mark_latency(
                    ctx,
                    "agent_first_text_delta",
                    once=True,
                    provider=type(self.agent).__name__,
                    operation="agent.stream",
                )
            agent_activity.set()
            ctx.emit(agent_event(ctx.session_id, ctx.turn_id, name, **event_fields))
            if name in {"tool", "tool_start"}:
                if not emitted_tool:
                    ctx.emit(
                        tool_event(
                            ctx.session_id,
                            ctx.turn_id,
                            SessionEventType.TOOL_STARTED,
                            name=str(fields.get("name") or current_tool[0] or "tool"),
                            detail=current_detail[0],
                            call_id=str(fields.get("tool_call_id") or "") or None,
                        )
                    )
                style = narration_style_or_default(
                    getattr(self.policy, "narration_style", "narrated")
                )
                # Every turn that uses tools must say so, even with optional
                # narration/acks disabled. Later tool chatter keeps its style.
                first_tool = first_tool_task is None
                if style == "silence" and not first_tool:
                    return
                if style == "keyboard" and not first_tool:
                    if not first_sentence.is_set():
                        # The waiting sound is already typing for this.
                        return
                    task = asyncio.create_task(
                        play_tool_keyboard(
                            opening_gate,
                            ctx=ctx,
                            tool=current_tool[0] or "tool",
                            play_clip=play_clip_policy,
                        ),
                        name=f"voice-policy-tool-clip-{ctx.turn_id}",
                    )
                    self._tasks.setdefault(ctx.turn_id, set()).add(task)
                    return
                phrases = getattr(self.policy, "narration_phrases", {}) or {}
                chosen = choose_tool_narration(
                    current_tool[0],
                    current_detail[0],
                    phrases,
                    spoken_narration_keys,
                    generic_narration_spoken[0],
                )
                if first_tool and (chosen is None or not is_hearable(chosen[1])):
                    chosen = (current_tool[0], tool_status_line(specific_tool))
                if chosen is None:
                    return
                kind, narration = chosen
                if narration and is_hearable(narration):
                    spoken_narration_keys.add(
                        f"{kind}:{current_detail[0].strip().lower()}"
                    )
                    generic_narration_spoken[0] = not bool(current_detail[0].strip())
                    current_tool[0] = kind
                    task = asyncio.create_task(
                        speak_tool_progression(
                            opening_gate,
                            ctx=ctx,
                            narration=narration,
                            tool=kind,
                            speak_policy=speak_policy,
                        ),
                        name=f"voice-policy-tool-{ctx.turn_id}",
                    )
                    self._tasks.setdefault(ctx.turn_id, set()).add(task)
                    if first_tool:
                        first_tool_task = task
            elif name == "tool_result":
                failed = bool(fields.get("is_error") or fields.get("status") == "failed")
                kind = (
                    SessionEventType.TOOL_FAILED
                    if failed
                    else SessionEventType.TOOL_COMPLETED
                )
                ctx.emit(
                    tool_event(
                        ctx.session_id,
                        ctx.turn_id,
                        kind,
                        name=str(fields.get("name") or current_tool[0] or "tool"),
                        call_id=str(fields.get("tool_call_id") or "") or None,
                        detail=str(fields.get("detail") or ""),
                        content=str(fields.get("content") or ""),
                    )
                )

        tasks = {
            asyncio.create_task(
                self._preamble_controller(
                    agent_prompt,
                    ctx,
                    first_sentence,
                    text_seen,
                    tool_seen,
                    speak_policy,
                    instant_ack_pool,
                    opening_gate,
                ),
                name=f"voice-policy-preamble-{ctx.turn_id}",
            ),
            asyncio.create_task(
                self._keepalive_controller(
                    ctx,
                    response_done,
                    agent_activity,
                    current_tool,
                    current_detail,
                    used_reassure,
                    started_at,
                    speak_policy,
                ),
                name=f"voice-policy-progress-{ctx.turn_id}",
            ),
            asyncio.create_task(
                self._working_sound_controller(ctx, first_sentence, response_done, play_clip_policy),
                name=f"voice-policy-working-sound-{ctx.turn_id}",
            ),
        }
        self._tasks[ctx.turn_id] = tasks
        try:
            request_epoch = self._begin_agent_request(ctx.turn_id)
            _mark_latency(
                ctx,
                "agent_request_started",
                once=True,
                provider=type(self.agent).__name__,
                operation="agent.stream",
            )
            method = self.agent.respond_streaming
            parameters = inspect.signature(method).parameters
            stream = (
                method(agent_prompt, on_event=typed_events(on_agent_event))
                if "on_event" in parameters
                else method(agent_prompt)
            )
            iterator = stream.__aiter__()
            while ctx.turn_id not in self._cancelled and not self._closed:
                next_sentence = asyncio.create_task(
                    anext(iterator),
                    name=f"voice-policy-agent-{ctx.turn_id}",
                )
                self._tasks.setdefault(ctx.turn_id, set()).add(next_sentence)
                # Keep the same iterator alive through long tool calls.
                # Progress narration runs independently; only actual user
                # cancellation/closure or an adapter error ends this wait.
                await asyncio.wait({next_sentence})
                self._tasks.get(ctx.turn_id, set()).discard(next_sentence)
                try:
                    sentence = next_sentence.result()
                except StopAsyncIteration:
                    if first_tool_task is not None:
                        await first_tool_task
                    self._finish_agent_request(ctx.turn_id, request_epoch)
                    if job_filter is not None:
                        for line in await self._apply_job_actions(jobs, with_raw_bodies(job_filter.flush(), "".join(raw_reply or raw_untyped), raw_taken), ctx, job_started, job_refused, text, job_cancel):
                            yield emit_spoken(line)
                        if job_started[0] and not bot_speaking[0]:
                            # Handed off without a sentence to say.
                            bot_speaking[0] = True
                            yield emit_spoken(STARTED_LINE)
                        elif job_cancel[0] and not bot_speaking[0]:
                            bot_speaking[0] = True
                            yield emit_spoken(job_cancel[0])
                        if job_started[0]:
                            keep_talking = jobs.keep_talking_line(" ".join(spoken_lines))
                            if keep_talking:
                                yield emit_spoken(keep_talking)
                    for line in self._flush_worker_speech(worker_voice):
                        yield emit_spoken(line)
                    closing = summarize_tail(held_back)
                    if closing:
                        # Its words were emitted already, held, with the
                        # sentence they came from. Marked as the summary,
                        # the feed shows what the caller heard and keeps
                        # the held sentences for the full answer.
                        ctx.emit(agent_event(ctx.session_id, ctx.turn_id, "sentence",
                                             text=closing, spaced=True, summary=True))
                        _note_answer(
                            ctx,
                            _mark_latency(
                                ctx,
                                "agent_first_speakable_segment",
                                once=True,
                                provider=type(self.agent).__name__,
                                textChars=len(closing),
                            ),
                        )
                        yield closing
                    if reply_length_or_default(getattr(self.policy, "reply_length", "full")) == "concise":
                        written = "".join(raw_reply or raw_untyped).strip()
                        # Handoff instructions belong to workers, not the
                        # caller's written answer. Keep the filtered speech
                        # transcript for those replies.
                        if written and "<job" not in written:
                            ctx.emit(agent_event(ctx.session_id, ctx.turn_id, "full_answer", text=written))
                    return
                # A fast answer must not cancel the required tool cue before
                # slow/local synthesis gets audio to the caller.
                if first_tool_task is not None:
                    await first_tool_task
                if job_filter is None and "<job" in sentence:
                    _logger.warning("the agent wrote a <job> tag, but background jobs are off for this call; it is spoken as text")
                if job_filter is not None:
                    sentence, actions = job_filter.feed(sentence)
                    actions = with_raw_bodies(actions, "".join(raw_reply or raw_untyped), raw_taken)
                    for line in await self._apply_job_actions(jobs, actions, ctx, job_started, job_refused, text, job_cancel):
                        bot_speaking[0] = True
                        first_sentence.set()
                        yield emit_spoken(line)
                    if not any(char.isalnum() for char in sentence) or job_refused[0]:
                        # Inside a job block or what is left around one
                        # ("—"), or after a refused handoff whose own "on it"
                        # line would now be wrong.
                        agent_activity.set()
                        continue
                agent_activity.set()
                first_sentence.set()
                # A canned line still being made is dropped now (the route).
                ctx.extras["_answer_ready"] = True
                generic = is_generic_preamble(sentence, self.policy)
                if generic and not await opening_gate.claim("agent_preamble"):
                    ctx.emit(
                        progress(
                            ctx.session_id,
                            ctx.turn_id,
                            pool="preamble_dropped",
                            text=sentence,
                            suppressed=True,
                        )
                    )
                    continue
                for line in take_immediate(worker_voice):
                    yield emit_spoken(line)
                bot_speaking[0] = True
                # Read per sentence, so flipping the setting mid-answer
                # lands on the next one rather than the next turn.
                budget = spoken_budget_chars(
                    getattr(self.policy, "reply_length", "full")
                )
                # Past the budget the mouth still finishes the sentence it
                # is in, so the caller never hears half of one; the 2x cap
                # keeps an unpunctuated list from running on regardless.
                finishing = mid_sentence and spoken_chars < 2 * budget
                if budget and spoken_chars >= budget and not finishing:
                    # Past the listening budget. The sentence still
                    # goes in the transcript; the mouth stops here and
                    # the tail speaks for it once the agent lands.
                    held_back.append(emit_spoken(sentence, held=True))
                    continue
                spoken_chars += len(sentence)
                mid_sentence = not ends_sentence(sentence)
                if generic:
                    _note_opening(
                        ctx,
                        _mark_latency(
                            ctx,
                            "preamble_played",
                            once=True,
                            pool="agent_preamble",
                        ),
                    )
                else:
                    _note_answer(
                        ctx,
                        _mark_latency(
                            ctx,
                            "agent_first_speakable_segment",
                            once=True,
                            provider=type(self.agent).__name__,
                            textChars=len(sentence),
                        ),
                    )
                yield emit_spoken(sentence)
        finally:
            first_sentence.set()
            ctx.extras["_answer_ready"] = True
            response_done.set()
            try:
                if (sys.exception() is None and opening_gate.audio_committing
                        and ctx.turn_id not in self._cancelled):
                    for task in tuple(self._tasks.get(ctx.turn_id, ())):
                        if task.get_name().startswith("voice-policy-preamble-") and not task.done():
                            await task
            finally:
                pending = self._pending_permission
                if pending is not None and pending[0].turn_id == ctx.turn_id:
                    self._pending_permission = None  # the turn is over; nothing waits on an answer
                if self._working_now is not None and self._working_now[0] is ctx:
                    self._working_now = None
                await self._cancel_tasks(ctx.turn_id)
            try:
                _mark_turn_text_summary(ctx)
            except Exception:
                pass

    async def _apply_job_actions(
        self,
        jobs: Any,
        actions: list[Any],
        ctx: Any,
        started: list[bool],
        refused: list[bool],
        caller_text: str = "",
        cancel_line: list[str] | None = None,
    ) -> list[str]:
        """Carry out the job tags in a reply; return any line the caller must hear."""
        lines: list[str] = []
        for action in actions:
            if isinstance(action, StartJob):
                if not action.brief:
                    continue
                outcome = jobs.start(action.title, action.brief, turn_id=ctx.turn_id, asked=caller_text)
                if outcome.job is None:
                    refused[0] = True
                    lines.append(outcome.line)
                else:
                    started[0] = True
                    if outcome.line:
                        # Queued behind the running jobs: say so, or it
                        # sounds like the work began.
                        lines.append(outcome.line)
                    self._job_turns[ctx.turn_id] = jobs
                    while len(self._job_turns) > 8:
                        self._job_turns.pop(next(iter(self._job_turns)))
                _mark_latency(ctx, "job_handoff", once=False, accepted=outcome.job is not None,
                              queued=bool(outcome.job is not None and outcome.job.queued))
            elif isinstance(action, JobNote):
                target, how = jobs.resolve(caller_text, named=action.job, kind="note")
                if target is None:
                    continue
                await jobs.note(action.text, job=target)
                if how in {"selected", "referent"} and len(jobs.live) > 1:
                    # Several jobs and none named: name the one it went to,
                    # so a wrong pick is heard and corrected at once.
                    lines.append(f"That's for the {target.title} job.")
            elif isinstance(action, CancelJob):
                target, how = jobs.resolve(caller_text, named=action.job, kind="cancel")
                if target is None:
                    continue
                said = cancel_line if cancel_line is not None else [""]
                if not cancel_meant(caller_text, target.title):
                    # "Okay, you can stop now" was about the agent's talking.
                    _logger.info("job cancel ignored: the caller did not ask to stop the job")
                    said[0] = f"Okay. The {target.title} job keeps going."
                elif how in {"named", "only"}:
                    await jobs.cancel(target)
                    said[0] = f"Stopped the {target.title} job."
                else:
                    # Several jobs, none named: confirm by name first. The
                    # agent's own "stopped it" would now be wrong, so the
                    # rest of its reply is dropped.
                    refused[0] = True
                    lines.append(jobs.ask_cancel(target))
        return lines

    async def cancel(self, turn_id: int) -> None:
        self._cancelled.add(turn_id)
        handed_off = self._job_turns.pop(turn_id, None)
        if handed_off is not None:
            handed_off.turn_cancelled(turn_id)
        # Abort alongside the cancel, not after it: a cancelled stream's
        # cleanup can wait on the agent until the abort lands (Claude Code
        # took 5 s to let go of "stop", the caller hearing nothing).
        await asyncio.gather(self._cancel_tasks(turn_id), self._abort_agent_once(turn_id))

    async def answer_status(self, text: str) -> bool:
        """Answer "what are you doing?" asked while the agent works, and leave
        the work running. Asking used to interrupt it: Claude Code's three
        reviewers were "rejected before launching" by the question."""
        live = self._working_now
        if live is None or not is_mid_turn_status_question(text):
            return False
        ctx, current_tool, current_detail, started_at, speak, roster = live
        snapshot = roster.snapshot() if roster is not None else None
        if snapshot is not None and any(row.phase.value not in {"completed", "failed", "cancelled"}
                                        for row in snapshot.workers):
            line = status_line(snapshot)
        else:
            detail = current_detail[0].strip().rstrip(".")
            if detail:
                core = f"I'm still {detail[0].lower()}{detail[1:]}"
            elif current_tool[0]:
                phrase = tool_status_line(current_tool[0]).rstrip(".") or "working on it"
                core = f"I'm still {phrase[0].lower()}{phrase[1:]}"
            else:
                core = "I'm still working on it"
            line = f"{core}{elapsed_suffix(time.monotonic() - started_at)}. I'll tell you when it's done."
        ctx.emit(progress(ctx.session_id, ctx.turn_id, pool="status", text=line))
        await speak(line, ctx)
        return True

    def _permission_pending_for(self, turn_id: int) -> bool:
        pending = self._pending_permission
        return pending is not None and pending[0].turn_id == turn_id

    async def answer_pending(self, text: str) -> bool:
        """The caller's yes or no to the agent's waiting permission request.

        True when it answered one (the work goes on); False when nothing is
        waiting or the words aren't a yes or a no.
        """
        pending = self._pending_permission
        decision = permission_answer(text)
        if pending is None or decision is None:
            return False
        ctx, request = pending
        self._pending_permission = None
        result = await adapter_control(self.agent, ResolvePermission(request.request_id, decision))
        ctx.emit(agent_event(ctx.session_id, ctx.turn_id, "permission_resolved",
                             requestId=request.request_id, decision=decision.value,
                             accepted=isinstance(result, ControlAccepted)))
        return isinstance(result, ControlAccepted)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._streamed_participants.clear()
        self._stream_errors.clear()
        if self._pause_ahead is not None:
            self._pause_ahead.close()
        for turn_id in tuple(self._tasks):
            await self.cancel(turn_id)
        async def close_provider(provider: Any) -> None:
            close = getattr(provider, "close", None)
            if provider is None or not callable(close) or id(provider) in self._providers_closed:
                return
            self._providers_closed.add(id(provider))
            result = close()
            if inspect.isawaitable(result):
                await result

        results = await asyncio.gather(
            *(close_provider(provider) for provider in (self.streaming_stt, self.stt)),
            return_exceptions=True,
        )
        failures = [result for result in results if isinstance(result, Exception)]
        if failures:
            raise ExceptionGroup("STT provider cleanup failed", failures)

    async def _preamble_controller(
        self,
        text: str,
        ctx: Any,
        first_sentence: asyncio.Event,
        text_seen: asyncio.Event,
        tool_seen: asyncio.Event,
        speak: Callable[[str, Any], Awaitable[None]],
        instant_ack_pool: str | None,
        opening_gate: OpeningPreambleGate,
    ) -> None:
        if self.instant_ack_mode == "off":
            return
        if self.instant_ack_mode == "delayed":
            waited = time.monotonic() - float(ctx.extras.get("_turn_started_at") or time.monotonic())
            if await self._wait_for(first_sentence, max(2.5 - waited, 0.0)):
                return
            pool = "working" if tool_seen.is_set() else "ack"
            line = self._next_policy_line(pool)
            if line:
                await opening_gate.speak(owner="policy:delayed", pool=pool, line=line, ctx=ctx, speak=speak)
            return
        # The caller has waited since their turn was judged over,
        # transcription included; the route records when that was.
        waited = time.monotonic() - float(ctx.extras.get("_turn_started_at") or time.monotonic())
        if await self._wait_for(first_sentence, max(self.timings.ack_delay_seconds - waited, 0.0)):
            return
        if text_seen.is_set() and await self._wait_for(
            first_sentence, self.timings.sentence_grace_seconds
        ):
            return
        if instant_ack_pool is None:
            selected = (
                select_ack_pool(text)
                if getattr(self.policy, "instant_ack_mode", "smart") == "smart"
                else "ack"
            )
            # "none" (a bare "yeah", "thanks") gets no acknowledgement; a
            # turn that runs long still gets the action line below.
            line = self._next_policy_line(selected) if selected != "none" else ""
            if line and await opening_gate.speak(
                owner=f"policy:{selected}",
                pool=selected,
                line=line,
                ctx=ctx,
                speak=speak,
            ):
                if not ctx.extras.get("_opening_dropped"):
                    _note_opening(
                        ctx,
                        _mark_latency(ctx, "preamble_played", once=True, pool=selected),
                    )
                return
        if await self._wait_for(first_sentence, self.timings.action_delay_seconds):
            return
        # ``ack_action`` is deliberately a combined acknowledgement and
        # action promise.  Once it has played, another generic action line is
        # the exact "Okay, let me check ... Let me check" double-preamble the
        # smart policy exists to prevent.
        if instant_ack_pool == "ack_action":
            return
        pool = "working" if tool_seen.is_set() else "action"
        line = self._next_policy_line(pool)
        if line and await opening_gate.speak(
            owner=f"policy:{pool}",
            pool=pool,
            line=line,
            ctx=ctx,
            speak=speak,
        ) and not ctx.extras.get("_opening_dropped"):
            _note_opening(
                ctx,
                _mark_latency(ctx, "preamble_played", once=True, pool=pool),
            )

    @property
    def instant_ack_mode(self) -> str:
        """Resolved acknowledgement mode owned by the canonical runtime."""
        # Snappy gives the audio lane to the answer. A canned opening that
        # starts first otherwise holds the audio lock until it finishes.
        if getattr(self.policy, "snappy_responsiveness", False):
            return "off"
        if not getattr(self.policy, "instant_ack", False):
            return "off"
        return str(getattr(self.policy, "instant_ack_mode", "smart"))

    @property
    def smart_ack_budget_seconds(self) -> float:
        """Maximum smart-STT wait before a safe generic acknowledgement."""
        milliseconds = float(getattr(self.policy, "smart_ack_budget_ms", 1200) or 0)
        return max(milliseconds, 0.0) / 1000.0

    async def play_instant_ack(
        self,
        text: str,
        ctx: Any,
        *,
        speak_policy: Callable[[str, Any], Awaitable[None]],
        blind: bool = False,
        opening_gate: OpeningPreambleGate | None = None,
    ) -> str | None:
        """Select and play one configured acknowledgement through AudioOutput.

        Returns the pool that actually crossed the output boundary.  The
        route passes that receipt back to :meth:`respond` so its tiered
        controller cannot replay the same acknowledgement or follow a
        combined ``ack_action`` with a redundant action line.
        """
        if self.instant_ack_mode in {"off", "delayed"}:
            return None
        selected = "ack" if blind else select_ack_pool(text)
        if selected == "none":
            return None
        line = self._next_policy_line(selected)
        if not line:
            return None
        if opening_gate is not None and not await opening_gate.claim(f"instant_ack:{selected}"):
            return None
        ctx.emit(progress(ctx.session_id, ctx.turn_id, pool=selected, text=line))
        await speak_policy(line, ctx)
        _note_opening(
            ctx,
            _mark_latency(
                ctx,
                "instant_ack_played",
                once=True,
                pool=selected,
                mode="blind" if blind else "smart",
            ),
        )
        return selected

    async def _keepalive_controller(
        self,
        ctx: Any,
        response_done: asyncio.Event,
        activity: asyncio.Event,
        current_tool: list[str],
        current_detail: list[str],
        used: set[str],
        started_at: float,
        speak: Callable[[str, Any], Awaitable[None]],
    ) -> None:
        interval = self.timings.keepalive_interval_seconds
        if interval is None:
            interval = float(getattr(self.policy, "keepalive_interval_s", 0.0) or 0.0)
        if interval <= 0:
            return
        while not response_done.is_set():
            activity.clear()
            try:
                await asyncio.wait_for(activity.wait(), timeout=interval)
                continue
            except TimeoutError:
                pass
            if not await self._caller_heard_silence(ctx, interval, activity, response_done):
                continue
            if self._permission_pending_for(ctx.turn_id):
                # "Still writing it" is wrong while the agent waits on the
                # caller, and talked over their "yes". Ask once more, then wait.
                pending = self._pending_permission
                again = f"Still waiting on you. {permission_question(pending[1].prompt)}" if pending else ""
                if again and again not in used:
                    used.add(again)
                    await speak(again, ctx)
                continue
            line = _shared_next_reassure_line(
                current_tool[0],
                current_detail[0],
                time.monotonic() - started_at,
                used,
                policy=self.policy,
            )
            if not line or not is_hearable(line):
                continue
            used.add(line)
            ctx.emit(progress(ctx.session_id, ctx.turn_id, pool="reassure", text=line))
            await speak(line, ctx)

    async def _caller_heard_silence(
        self,
        ctx: Any,
        interval: float,
        activity: asyncio.Event,
        response_done: asyncio.Event,
    ) -> bool:
        """Wait until the caller has heard nothing for ``interval``.

        The agent going quiet is not the caller hearing silence: while a long
        answer is still being read out, "still going" only interrupts it.
        False when the agent stirred or finished during the wait.
        """
        quiet_for = ctx.extras.get("_voice_quiet_for")
        if not callable(quiet_for):
            return True
        while (hold := interval - quiet_for()) > 0:
            if await self._wait_for(response_done, hold) or activity.is_set():
                return False
        return True

    async def _working_sound_controller(
        self,
        ctx: Any,
        first_sentence: asyncio.Event,
        response_done: asyncio.Event,
        play_clip: Callable[..., Awaitable[bool]] | None,
    ) -> None:
        """Keyboard style: type while the agent works, until its first words.

        The clip plays in short pieces, each paced to its own length, so a
        surface that queues audio never holds more than one piece ahead of
        the answer, and the sound stops within a piece of the answer starting.
        """
        if play_clip is None:
            return
        style = narration_style_or_default(getattr(self.policy, "narration_style", "narrated"))
        if style != "keyboard":
            return
        slices = working_slices(style)
        if not slices:
            return
        if await self._wait_for(first_sentence, self.timings.working_sound_delay_seconds):
            return
        loop = asyncio.get_running_loop()
        announced = False
        for path, seconds in itertools.cycle(slices):
            if first_sentence.is_set() or response_done.is_set() or ctx.turn_id in self._cancelled:
                return
            if self._permission_pending_for(ctx.turn_id):
                # Waiting on the caller, not working: typing over the
                # question hid their "yes" behind a barge-in.
                return
            started = loop.time()
            if not await play_clip("working", ctx, path=path):
                return
            if not announced:
                announced = True
                ctx.emit(progress(ctx.session_id, ctx.turn_id, pool="working_sound", text=""))
            remaining = seconds - (loop.time() - started)
            if remaining > 0 and await self._wait_for(first_sentence, remaining):
                return

    def _begin_agent_request(self, turn_id: int) -> int:
        epoch = self._agent_request_epochs.get(turn_id, 0) + 1
        self._agent_request_epochs[turn_id] = epoch
        self._active_agent_requests[turn_id] = epoch
        return epoch

    def _finish_agent_request(self, turn_id: int, epoch: int) -> None:
        if self._active_agent_requests.get(turn_id) == epoch:
            self._active_agent_requests.pop(turn_id, None)

    async def _abort_agent_once(self, turn_id: int) -> None:
        epoch = self._active_agent_requests.get(turn_id, 0)
        key = (turn_id, epoch)
        if epoch <= 0 or key in self._agent_aborted_epochs:
            return
        self._agent_aborted_epochs.add(key)
        self._active_agent_requests.pop(turn_id, None)
        abort = getattr(self.agent, "abort", None)
        if not callable(abort):
            return
        result = abort()
        if inspect.isawaitable(result):
            await result

    async def _cancel_tasks(self, turn_id: int) -> None:
        tasks = self._tasks.pop(turn_id, set())
        current = asyncio.current_task()
        for task in tasks:
            if task is not current and not task.done():
                task.cancel()
        for task in tasks:
            if task is current:
                continue
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _wait_for(self, event: asyncio.Event, seconds: float) -> bool:
        if seconds <= 0:
            return event.is_set()
        try:
            await asyncio.wait_for(event.wait(), timeout=seconds)
            return True
        except TimeoutError:
            return event.is_set()

    def _pool_candidates(self, pool: str) -> tuple[str, ...]:
        pools = getattr(self.policy, "preamble_pools", {}) or {}
        candidates = tuple(pools.get(pool, ()))
        if not candidates and ":" in pool:
            candidates = tuple(pools.get(pool.split(":", 1)[0], ()))
        if not candidates and pool == "ack_action":
            candidates = tuple(pools.get("action", ()))
        return candidates

    def _next_policy_line(self, pool: str) -> str:
        candidates = self._pool_candidates(pool)
        if not candidates:
            return ""
        index = self._pool_rotation.get(pool, 0) % len(candidates)
        self._pool_rotation[pool] = index + 1
        return str(candidates[index])

    def next_opening_lines(self) -> tuple[str, ...]:
        """The line each opening pool would say next, without using it up."""
        if self.instant_ack_mode == "off":
            return ()
        lines: list[str] = []
        for pool in OPENING_POOLS:
            candidates = self._pool_candidates(pool)
            if candidates:
                line = str(candidates[self._pool_rotation.get(pool, 0) % len(candidates)])
                if line not in lines:
                    lines.append(line)
        return tuple(lines)

    def _stt_timeout(self, name: str, default: float) -> float:
        return float(getattr(getattr(self.config, "stt", None), name, default) or default)

    def _batch_budget_seconds(self, pcm: bytes) -> float:
        """How long the batch listener may take over this much audio.

        A listener on this machine decodes at its own pace, so one fixed
        timeout cannot serve both a short answer and a minute-long
        explanation: a caller who talked past the budget had the turn dropped
        on them, though every word of it had streamed past as a caption. Such
        a listener declares its seconds per second of audio and gets a budget
        that follows the recording. A hosted listener declares none and keeps
        the configured timeout, whose cost is the network, not the length.
        """
        fixed = max(self._stt_timeout("rest_timeout_seconds", 8.0), 0.1)
        rate = float(getattr(self.stt, "rest_seconds_per_audio_second", 0.0) or 0.0)
        if rate <= 0 or not pcm:
            return fixed
        return max(fixed, len(pcm) / (48_000 * 2) * rate)

    def _streaming_statistics(self, participant_id: int) -> dict[str, Any]:
        getter = getattr(self.streaming_stt, "last_turn_stats", None)
        if not callable(getter):
            return {}
        value = getter(participant_id)
        if hasattr(value, "__dataclass_fields__"):
            return asdict(value)
        if isinstance(value, dict):
            return dict(value)
        return {
            name: getattr(value, name)
            for name in dir(value)
            if not name.startswith("_") and not callable(getattr(value, name))
        }

    @staticmethod
    def _looks_truncated(
        pcm: bytes,
        transcript_text: str,
        statistics: dict[str, Any] | None = None,
    ) -> bool:
        audio_ms = len(pcm) / (48_000 * 2) * 1000
        transcript_audio_ms = (statistics or {}).get("transcript_audio_ms")
        if isinstance(transcript_audio_ms, (int, float)) and transcript_audio_ms > 0:
            # Captured buffers intentionally include endpointing silence. Use
            # the provider's recognized-audio cursor when available so that
            # trailing quiet does not falsely trigger an 8-second REST call.
            audio_ms = min(audio_ms, float(transcript_audio_ms))
        if audio_ms < 1500:
            return False
        expected_chars = (audio_ms / 1000) * 11.0
        return len(transcript_text.strip()) < max(0.35 * expected_chars, 12.0)

    def _on_worker_event(
        self,
        event: WorkerEvent,
        *,
        ctx: Any,
        roster: Any,
        worker_voice: WorkerVoiceState,
        bot_speaking: bool,
    ) -> None:
        if roster is None:
            return
        change = admit_worker(roster, ctx.emit, event)
        if change is None:
            return
        note_start(worker_voice, change, time.monotonic())
        started = worker_voice.started_mono.get(change.worker_id)
        quiet = None if started is None else time.monotonic() - started
        floor = FloorState.BOT_SPEAKING if bot_speaking else FloorState.THINKING
        if ctx.turn_id in self._cancelled:
            floor = FloorState.USER_SPEAKING
        decision = decide_worker_speech(
            change,
            snapshot=roster.snapshot(),
            floor=floor,
            state=worker_voice,
            quiet_elapsed_s=quiet,
        )
        if decision.kind is SpeechKind.ACK:
            remember_ack(worker_voice, change)
        append_probe_record(
            {
                "type": "probe.worker_speech",
                "sessionId": ctx.session_id,
                "turnId": ctx.turn_id,
                "workerId": change.worker_id,
                "phase": change.phase.value,
                "kind": decision.kind.value,
                "queue": decision.queue,
                "text": decision.text,
                "floor": floor.value,
            }
        )
        if decision.kind is SpeechKind.SILENT or not decision.text:
            return
        if decision.queue:
            worker_voice.queued.append(decision)
            return
        worker_voice.immediate.append(decision.text)

    @staticmethod
    def _flush_worker_speech(worker_voice: WorkerVoiceState) -> list[str]:
        lines = take_immediate(worker_voice)
        lines.extend(item.text for item in take_queued(worker_voice) if item.text)
        return lines
