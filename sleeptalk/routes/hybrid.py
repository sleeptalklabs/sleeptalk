from __future__ import annotations

import asyncio
import enum
import inspect
import logging
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Iterable
from dataclasses import dataclass, replace
from typing import Any, Protocol, TypeAlias, runtime_checkable

from sleeptalk.adapter import (
    AdapterEvent,
    AgentError,
    AnswerClarification,
    Cancel,
    ClarificationRequested,
    ControlAccepted,
    ControlRejected,
    PermissionDecision,
    PermissionRequested,
    ResolvePermission,
    SessionIdentified,
    StatusChanged,
    ToolEvent,
    ToolPhase,
    adapter_control,
    typed_events,
)
from sleeptalk.policy import wants_brief_speech
from sleeptalk.routes.audio_output import AudioOutput, InFlightTurn
from sleeptalk.routes.base import (
    InterruptionGranularity,
    RecoveryOutcome,
    RecoveryResult,
    RouteCapabilities,
    RouteInput,
    RouteRuntimeError,
    RouteStateError,
    RouteType,
    TurnContext,
    TurnOutcome,
)
from sleeptalk.routes.hybrid_mouth import (
    Finding,
    HybridMouth,
    Script,
)
from sleeptalk.routes.hybrid_types import (
    CommittedIntent,
    GroundedBrief,
    HybridRoleContract,
    HybridSessionReducer,
    IntentId,
    LocalOwned,
    LocalOwnershipReason,
    OwnedIntent,
    PresentationEpoch,
    RealtimeOwned,
    RealtimeOwnershipReason,
    SemanticOwner,
    SpecialistOwned,
    SpecialistOwnershipReason,
    TaskId,
    TaskPhase,
    TaskRecord,
    assign_owner,
    is_general_knowledge,
    redirect_request,
    hands_catalog_from_agent,
    is_hands_question,
)
from sleeptalk.session_events import (
    EventError,
    InterruptionPhase,
    InterruptionSource,
    SessionEvent,
    SessionEventType,
    Speaker,
    adapter_session_event,
    agent_event,
    control_session_event,
    error,
    interruption,
    response,
    transcript,
)
from sleeptalk.speech_speed import snap_speed
from sleeptalk.voice_prompt import BRIEF_SPEECH_CONTRACT

__all__ = [
    "HybridFailure",
    "HybridFailureCode",
    "HybridRoute",
    "RealtimeAudio",
    "RealtimeOutput",
    "RealtimeProvider",
    "RealtimeTranscript",
    "HybridMouth",
]


@dataclass(frozen=True, slots=True)
class RealtimeAudio:
    """One canonical 48 kHz int16 mono output chunk."""

    pcm: bytes


@dataclass(frozen=True, slots=True)
class RealtimeTranscript:
    """One text item emitted by the native response or audio commit."""

    text: str
    final: bool = True


RealtimeOutput: TypeAlias = bytes | RealtimeAudio | RealtimeTranscript | SessionEvent


@runtime_checkable
class RealtimeProvider(Protocol):
    """Exact provider seam consumed by :class:`HybridRoute`.

    ``prepare`` receives an empty ``tools`` tuple by construction. ``commit``
    converts one committed PCM input into a transcript. Text inputs bypass it.
    ``respond`` is called only after a ``RealtimeOwned`` decision is journaled.
    """

    async def prepare(self, *, session_id: str, tools: tuple[()] = ()) -> None: ...

    async def append_audio(self, pcm: bytes, *, participant_id: int = 0) -> None: ...

    def commit(
        self, route_input: RouteInput, *, turn_id: int
    ) -> str | RealtimeTranscript | AsyncIterator[RealtimeOutput] | Awaitable[Any]: ...

    def respond(
        self, transcript: str, *, turn_id: int
    ) -> AsyncIterator[RealtimeOutput] | Awaitable[Any]: ...

    def tell(
        self, material: Any, *, turn_id: int
    ) -> AsyncIterator[RealtimeOutput] | Awaitable[Any]: ...

    async def discard_committed_input(self, *, turn_id: int) -> None: ...

    async def cancel_response(self, *, turn_id: int | None = None) -> None: ...

    async def recover(self, *, session_id: str) -> bool | RecoveryResult: ...

    async def close(self) -> None: ...

    def current_tts_speed(self) -> float: ...

    def set_tts_speed(self, value: float | None) -> float: ...

    def cycle_tts_speed(self) -> float: ...


class HybridFailureCode(enum.Enum):
    REALTIME = "realtime"
    SPECIALIST = "specialist"
    CONTROL = "control"


@dataclass(frozen=True, slots=True)
class HybridFailure:
    code: HybridFailureCode
    message: str
    error_type: str
    retryable: bool = False

    def diagnostic(self) -> str:
        return f"{self.code.value}:{self.error_type}: {self.message}"


async def _resolve(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def _items(source: Any) -> AsyncIterator[Any]:
    source = await _resolve(source)
    if source is None:
        return
    if hasattr(source, "__aiter__"):
        async for item in source:
            yield item
        return
    if isinstance(source, (bytes, bytearray, str, RealtimeTranscript, SessionEvent)):
        yield source
        return
    if isinstance(source, Iterable):
        for item in source:
            yield item
        return
    yield source


def _specialist_failure_line(exc: BaseException) -> str:
    blob = f"{type(exc).__name__} {exc}".lower()
    if "newer version of codex" in blob:
        return "Codex needs an upgrade before it can use the selected model. I couldn't complete that request."
    if "maximum buffer" in blob or "clijsondecode" in blob:
        return HybridRoute.SPECIALIST_IMAGE_FAILED_SENTENCE
    if "exceeded maximum time" in blob or "timed out" in blob:
        return HybridRoute.SPECIALIST_TIMEOUT_SENTENCE
    return HybridRoute.SPECIALIST_FAILED_SENTENCE


# How much of the conversation the specialist is shown. Enough to answer
# in context, bounded so a long session does not grow every dispatch.
# What Realtime is told when it hands the agent a question it can answer itself.
VOICE_ANSWERS_TOOL_OUTPUT = (
    "Not needed: this is general knowledge. Answer it yourself now, directly and briefly, "
    "without calling any tool."
)


def _mark(ctx: TurnContext, stage: str, **fields: Any) -> None:
    """A stage on the turn's latency trace, like the cascade's (first answer audio, transcript)."""
    from sleeptalk.voice_policy_runtime import _mark_latency

    _mark_latency(ctx, stage, **fields)


logger = logging.getLogger("sleeptalk.routes.hybrid")

_HOLDING_LINE = "One moment."

_CONTEXT_EXCHANGES = 12


def _is_specialist_request(item: Any) -> bool:
    """A tool call from the voice, without importing the provider here."""
    return (
        hasattr(item, "call_id")
        and hasattr(item, "question")
        and not isinstance(item, (str, bytes))
    )


class HybridRoute:
    """Realtime conversation plus one durable, detached specialist task."""

    BUSY_SENTENCE = "I'm still working on the current task."
    CANCELLED_SENTENCE = "Cancelled the task."
    SPECIALIST_FAILED_SENTENCE = (
        "The coding agent stopped. I still have what I found. Ask me to keep going."
    )
    SPECIALIST_TIMEOUT_SENTENCE = (
        "The review ran out of time. I still have what I found. Ask me to keep going."
    )
    SPECIALIST_IMAGE_FAILED_SENTENCE = (
        "The image was too large for the coding agent to load. "
        "I still have the code. Ask me to finish without it."
    )

    def __init__(
        self,
        *,
        role_contract: HybridRoleContract,
        agent: Any,
        realtime: RealtimeProvider,
        audio_output: AudioOutput | None = None,
        keepalive_interval_s: float = 10.0,
        first_token_budget_ms: int = 800,
        speculative_audio: bool = False,
        audio_first: bool = False,
        reply_length: object = "full",
        narration_style: object = "narrated",
    ) -> None:
        self._role_contract = role_contract
        self._speculative_audio = speculative_audio
        self._audio_first = audio_first and speculative_audio
        #: The setting, or a reader for it — see policy.wants_brief_speech.
        self._reply_length = reply_length
        self._narration_style = narration_style
        self._tool_calls: dict[str, str] = {}
        # General-knowledge questions sent back to the voice once (see _dispatch_from_mouth).
        self._declined_questions: set[str] = set()
        self._task_prompts: dict[TaskId, str] = {}
        self._turn_started: dict[int, float] = {}
        self._playback_start = 0
        self._playback_generated_start = 0
        self._playback_accounting = None
        self._playback_turn_id = None
        self._keepalive_interval_s = max(float(keepalive_interval_s), 0.0)
        self._first_token_budget_s = max(float(first_token_budget_ms), 0.0) / 1000
        self._agent = agent
        self._realtime = realtime
        self._audio_output = audio_output
        self._reducer = HybridSessionReducer()
        self.capabilities = RouteCapabilities(
            route_type=RouteType.HYBRID,
            streaming_output=True,
            interruption=True,
            interruption_granularity=InterruptionGranularity.PLAYOUT_FRACTION,
            resume_after_interruption=False,
            usage_operations=(),
        )
        self._prepared = False
        self._closed = False
        self._session_id = ""
        self._emit: Any = None
        self._in_flight: InFlightTurn | None = None
        self._presentation_lock = asyncio.Lock()
        self._presenting_turn: int | None = None
        self._presentation_task: asyncio.Task[Any] | None = None
        self._interrupted_presentations: set[tuple[int | None, int]] = set()
        self._detached: dict[TaskId, asyncio.Task[None]] = {}
        self._task_contexts: dict[TaskId, TurnContext] = {}
        self._floor_context: TurnContext | None = None
        self._pending_permission: tuple[TaskId, PermissionRequested] | None = None
        self._pending_clarification: tuple[TaskId, ClarificationRequested] | None = None
        self._cancel_signalled: set[TaskId] = set()
        self._settled_turns: set[int] = set()
        self._last_specialist_text: tuple[str, ...] = ()
        self._brief: GroundedBrief | None = None
        self._mouth = HybridMouth(
            realtime,
            self._reducer,
            self,
            audio_output=audio_output,
            narration_style=narration_style,
        )
        self._hands = hands_catalog_from_agent(
            agent,
            cwd=str(getattr(agent, "cwd", "") or ""),
        )
        self._current_tool: str = ""
        self._keepalive: asyncio.Task[None] | None = None
        self._capture_held = False
        self._exchanges: deque[tuple[str, str]] = deque(maxlen=_CONTEXT_EXCHANGES)

    @property
    def reducer(self) -> HybridSessionReducer:
        return self._reducer

    @property
    def role_contract(self) -> HybridRoleContract:
        return self._role_contract

    def current_tts_speed(self) -> float:
        reader = getattr(self._realtime, "current_tts_speed", None)
        return snap_speed(reader()) if callable(reader) else 1.0

    def set_tts_speed(self, value: float | None) -> float:
        setter = getattr(self._realtime, "set_tts_speed", None)
        return snap_speed(setter(value)) if callable(setter) else 1.0

    def cycle_tts_speed(self) -> float:
        cycle = getattr(self._realtime, "cycle_tts_speed", None)
        return snap_speed(cycle()) if callable(cycle) else 1.0

    @property
    def tasks(self) -> tuple[TaskRecord, ...]:
        seen: set[TaskId] = set()
        records: list[TaskRecord] = []
        for event in self._reducer.events:
            task_id = event.task_id
            if task_id is None or task_id in seen:
                continue
            seen.add(task_id)
            record = self._reducer.task(task_id)
            if record is not None:
                records.append(record)
        return tuple(records)

    @property
    def active_task_id(self) -> TaskId | None:
        return self._reducer.snapshot().active_task_id

    def bind(self, session_id: str, emit: Any) -> None:
        self._session_id = session_id
        self._emit = emit

    async def prepare(self, session: Any) -> None:
        if self._closed:
            raise RouteStateError("prepare after close")
        if self._prepared:
            return
        self._session_id = getattr(session, "session_id", self._session_id)
        self._emit = getattr(session, "emit", self._emit)
        # Judge "is the caller done?" by the pause's early text, as a batch
        # cascade listener does: Realtime transcribes only committed turns, so
        # without it every turn ended on silence alone (1.8 s).
        policy = getattr(session, "policy", None)
        endpointing = policy.endpointing_config() if callable(getattr(policy, "endpointing_config", None)) else None
        self._early_transcript = bool(getattr(endpointing, "early_transcript", True))
        try:
            await _resolve(self._realtime.prepare(session_id=self._session_id, tools=()))
        except Exception as exc:
            raise RouteRuntimeError(f"hybrid prepare failed: {exc}") from exc
        warmup = getattr(self._agent, "warmup", None)
        if callable(warmup):
            try:
                await _resolve(warmup())
            except Exception as exc:
                # Warmup is "Say OK." — it primes the agent, it does not make
                # the route work. Failing the session over it turned one busy
                # or cold agent into a bot that would not start at all, and
                # the retry that followed reported "Agent is already
                # processing" because the first attempt was still running.
                self._reducer.record(
                    "agent.warmup_failed",
                    errorType=type(exc).__name__,
                    message=str(exc)[:200],
                )
                # Non-fatal must not mean invisible: the session now starts
                # degraded, and a silent degradation is harder to chase than
                # the failure to start ever was.
                logger.warning(
                    "agent warmup failed; starting anyway, first turn will be "
                    "slower: %s: %s",
                    type(exc).__name__,
                    exc,
                )
        self._prepared = True

    async def append_audio(self, pcm: bytes, *, participant_id: int = 0) -> None:
        self._require_ready("append_audio")
        if self._capture_held:
            return
        await _resolve(
            self._realtime.append_audio(pcm, participant_id=participant_id)
        )

    def listens_ahead(self) -> bool:
        """A pause brings an early transcript endpointing can judge the turn by."""
        return getattr(self, "_early_transcript", True) and callable(getattr(self._realtime, "transcribe_ahead", None))

    def transcribe_ahead(self, pcm: bytes, *, participant_id: int = 0) -> Any:
        """Start an early transcript of the utterance so far, at a pause.

        Endpointing judges the turn by it (``early_transcript``), and a gated
        turn (a task running, a question pending) uses it as its text. With
        ``early_transcript`` off, only gated turns spend the request.
        """
        if not getattr(self, "_early_transcript", True) and self._audio_first and self._may_speak_first():
            return None
        start = getattr(self._realtime, "transcribe_ahead", None)
        return start(pcm, participant_id=participant_id) if callable(start) else None

    async def transcribe(self, pcm: bytes, *, participant_id: int = 0) -> Any:
        """The words of one utterance, outside a turn (a soft barge's, to decide it)."""
        from sleeptalk.voice_policy_runtime import TranscriptionResult

        transcribe = getattr(self._realtime, "transcribe_text", None)
        if not callable(transcribe):
            raise RouteRuntimeError("this Realtime session can't transcribe outside a turn")
        return TranscriptionResult(str(await transcribe(pcm) or "").strip(), "openai_http", {})

    async def _first_transcript(self, route_input: RouteInput, ctx: TurnContext, transcript_task: asyncio.Task) -> str:
        """Realtime's transcript, or the early one if it heard this turn and lands first."""
        take = getattr(self._realtime, "take_early", None)
        early = take(route_input.pcm, participant_id=int(ctx.extras.get("participant_id", 0))) if callable(take) else None
        if early is None:
            return await transcript_task
        done, _ = await asyncio.wait({transcript_task, early.task}, return_when=asyncio.FIRST_COMPLETED)
        if transcript_task in done and transcript_task.exception() is None:
            return transcript_task.result()
        try:
            text = str(await early.task or "").strip()
        except Exception:  # noqa: BLE001 — Realtime's own transcript still decides
            text = ""
        if not text:
            return await transcript_task
        # Realtime's transcription finishes on its own and releases its item.
        transcript_task.add_done_callback(lambda task: task.cancelled() or task.exception())
        return text

    async def stream_ahead(self, generation: object, pcm: bytes) -> None:
        """The utterance being spoken, sent to Realtime while it is spoken."""
        stream = getattr(self._realtime, "stream_ahead", None)
        if callable(stream):
            await stream(generation, pcm)

    def turn_completion_signal(self, participant_id: int = 0) -> Any:
        signal = getattr(self._realtime, "turn_completion_signal", None)
        if not callable(signal):
            return None
        return signal(participant_id)

    async def run_turn(self, route_input: RouteInput, ctx: TurnContext) -> TurnOutcome:
        self._require_ready("run_turn")
        self._emit = ctx.emit
        self._in_flight = InFlightTurn(ctx.turn_id, asyncio.current_task())
        self._turn_started[ctx.turn_id] = time.monotonic()
        speculative_task = None
        speculative_events = None
        speculative_consumed = False
        transcript_task = None
        try:
            fast = self._speculative_audio and bool(route_input.pcm)
            holding = None if fast else self._start_holding_line(route_input, ctx)
            spoke_holding = False
            commit_started = time.perf_counter()
            heard_ms: int | None = None
            audio_first = False
            try:
                if fast:
                    await self._realtime.commit_audio(route_input, turn_id=ctx.turn_id)
                    speculative_events = asyncio.Queue(maxsize=128)
                    spoke = asyncio.Event()
                    speculative_task = asyncio.create_task(
                        self._buffer_response(ctx.turn_id, speculative_events, spoke),
                        name=f"hybrid-speculative-{ctx.turn_id}",
                    )
                    transcript_task = asyncio.create_task(
                        self._realtime.transcript_for(turn_id=ctx.turn_id),
                        name=f"hybrid-transcript-{ctx.turn_id}",
                    )
                    if self._audio_first and self._may_speak_first():
                        audio_first = await self._audio_beat_transcript(
                            transcript_task, speculative_task, spoke
                        )
                    if not audio_first:
                        user_text = await self._first_transcript(route_input, ctx, transcript_task)
                else:
                    user_text = await self._committed_text(route_input, ctx)
                if not audio_first:
                    heard_ms = int((time.perf_counter() - commit_started) * 1000)
                    _mark(ctx, "stt_completed", sttSource="realtime", textChars=len(user_text or ""))
            except Exception as exc:
                failure = HybridFailure(
                    HybridFailureCode.REALTIME,
                    str(exc),
                    type(exc).__name__,
                    retryable=True,
                )
                self._emit_failure(ctx, failure)
                return TurnOutcome(
                    status="failed",
                    turn_id=ctx.turn_id,
                    played=[],
                    generated=[],
                    error=failure.diagnostic(),
                )
            finally:
                spoke_holding = await self._settle_holding_line(holding)

            if audio_first:
                speculative_consumed = True
                return await self._run_audio_first(
                    route_input, ctx, speculative_events, transcript_task, commit_started
                )

            self._hear(route_input, ctx, user_text)
            intent = CommittedIntent(
                intent_id=IntentId.mint(),
                turn_id=ctx.turn_id,
                transcript=user_text,
                presentation_epoch=self._reducer.presentation_epoch,
            )
            previously_active = self.active_task_id
            redirect = redirect_request(user_text) if previously_active is not None else ""
            if redirect:
                # "Wait, stop. <a new request>": stop the running task and take
                # the request, instead of answering "still working on it".
                if speculative_task is not None and not speculative_consumed:
                    # Cancelling the task cancels Realtime's live response,
                    # which is this reply in progress; a fresh one follows.
                    await self._realtime.cancel_response(turn_id=ctx.turn_id)
                    speculative_task.cancel()
                    await asyncio.gather(speculative_task, return_exceptions=True)
                    discard = getattr(self._realtime, "discard_output", None)
                    if callable(discard):
                        await discard(turn_id=ctx.turn_id)
                    speculative_consumed = True
                    speculative_events = None
                    fast = False
                await self.cancel_task(previously_active.value)
                self._reducer.record("task.redirected", taskId=previously_active.value)
                previously_active = self.active_task_id
                intent = replace(intent, transcript=redirect)
            owned = assign_owner(
                intent,
                self._reducer.snapshot(),
                self._hands,
                brief=self._brief,
            )
            if fast and not isinstance(owned, RealtimeOwned):
                await self._realtime.cancel_response(turn_id=ctx.turn_id)
                speculative_task.cancel()
                await asyncio.gather(speculative_task, return_exceptions=True)
                discard = getattr(self._realtime, "discard_output", None)
                if callable(discard):
                    await discard(turn_id=ctx.turn_id)
                speculative_consumed = True
            self._reducer.commit(owned)
            self._emit_ownership(ctx, owned)
            self._floor_context = ctx
            # What a barge meant is settled by what is said next. A new
            # request means the earlier answer was left behind; a control
            # line ("repeat that", "stop") is not a new question, so the
            # answer it interrupted is still owed.
            if isinstance(owned, LocalOwned):
                self._mouth.keep_barged()
            else:
                self._supersede(intent)

            if route_input.pcm and not isinstance(owned, RealtimeOwned):
                try:
                    await self._realtime.discard_committed_input(turn_id=ctx.turn_id)
                except Exception as exc:
                    failure = HybridFailure(
                        HybridFailureCode.REALTIME,
                        str(exc),
                        type(exc).__name__,
                        retryable=True,
                    )
                    if isinstance(owned, SpecialistOwned):
                        self._reducer.transition_task(
                            owned.task_id,
                            TaskPhase.FAILED,
                            error=failure.diagnostic(),
                        )
                    self._emit_failure(
                        ctx,
                        failure,
                        task_id=(owned.task_id if isinstance(owned, SpecialistOwned) else None),
                    )
                    return TurnOutcome(
                        status="failed",
                        turn_id=ctx.turn_id,
                        played=[],
                        generated=[],
                        error=failure.diagnostic(),
                    )

            if isinstance(owned, LocalOwned):
                outcome = await self._run_local(owned, ctx)
                if owned.reason in {
                    LocalOwnershipReason.INTERRUPT_PRESENTATION,
                    LocalOwnershipReason.REPEAT,
                }:
                    return replace(outcome, heard=user_text, heard_ms=heard_ms)
            elif isinstance(owned, RealtimeOwned):
                outcome = await self._run_realtime(owned, ctx, speculative_events)
                speculative_consumed = True
            else:
                outcome = await self._start_specialist(
                    owned, ctx, previously_active, spoke_holding=spoke_holding
                )
            await self._mouth.resume(ctx)
            # The surface never saw this audio as text; hand it back so a
            # channel transcript is possible at all.
            return replace(outcome, heard=user_text, heard_ms=heard_ms)
        finally:
            if transcript_task is not None and not transcript_task.done():
                transcript_task.cancel()
                await asyncio.gather(transcript_task, return_exceptions=True)
            if speculative_task is not None:
                if not speculative_task.done():
                    speculative_task.cancel()
                await asyncio.gather(speculative_task, return_exceptions=True)
                if not speculative_consumed:
                    await self._realtime.cancel_response(turn_id=ctx.turn_id)
                    discard = getattr(self._realtime, "discard_output", None)
                    if callable(discard):
                        await discard(turn_id=ctx.turn_id)
                    await self._realtime.discard_committed_input(turn_id=ctx.turn_id)
            if self._in_flight is not None and self._in_flight.turn_id == ctx.turn_id:
                self._in_flight = None

    async def _buffer_response(
        self, turn_id: int, queue: asyncio.Queue, spoke: asyncio.Event | None = None
    ) -> None:
        try:
            async for event in _items(self._realtime.respond("", turn_id=turn_id)):
                await queue.put(event)
                if spoke is not None:
                    spoke.set()
        except Exception as exc:
            await queue.put(exc)
        await queue.put(None)

    def _may_speak_first(self) -> bool:
        """Only an idle floor may skip the transcript gate.

        With a task running, "stop" and "cancel the task" have to reach the
        local controls, and a pending permission or clarification is answered
        by exactly what the user just said. Those turns keep the gate.
        """
        snapshot = self._reducer.snapshot()
        return (
            snapshot.active_task_id is None
            and not snapshot.pending_permission
            and not snapshot.pending_clarification
        )

    @staticmethod
    async def _audio_beat_transcript(
        transcript_task: asyncio.Task, speculative_task: asyncio.Task, spoke: asyncio.Event
    ) -> bool:
        """Whether Realtime produced output before transcription finished.

        Transcription only starts once the utterance is committed, so gating
        playback on it put a whole transcription round trip in front of
        every answer. When the reply is ready first, Realtime has already
        heard the audio and chosen between answering and ask_specialist.
        """
        spoke_wait = asyncio.ensure_future(spoke.wait())
        try:
            waiters = {transcript_task, spoke_wait}
            if not speculative_task.done():
                waiters.add(speculative_task)
            await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
            # A reply that ended with no output leaves only the transcript.
            if not transcript_task.done() and not spoke.is_set():
                await asyncio.wait(
                    {transcript_task, spoke_wait}, return_when=asyncio.FIRST_COMPLETED
                )
        finally:
            spoke_wait.cancel()
        return spoke.is_set() and not transcript_task.done()

    async def _run_audio_first(
        self,
        route_input: RouteInput,
        ctx: TurnContext,
        events: asyncio.Queue,
        transcript_task: asyncio.Task,
        commit_started: float,
    ) -> TurnOutcome:
        intent = CommittedIntent(
            intent_id=IntentId.mint(),
            turn_id=ctx.turn_id,
            transcript="",
            presentation_epoch=self._reducer.presentation_epoch,
        )
        owned = RealtimeOwned(intent, RealtimeOwnershipReason.AUDIO_FIRST)
        self._reducer.commit(owned)
        self._emit_ownership(ctx, owned)
        self._floor_context = ctx
        self._supersede(intent)
        heard: dict[str, Any] = {}

        def hear(task: asyncio.Task) -> None:
            # The words arrive while the answer plays; show them as soon as
            # they exist rather than after the reply.
            if heard or task.cancelled() or task.exception() is not None:
                return
            heard["text"] = task.result()
            heard["ms"] = int((time.perf_counter() - commit_started) * 1000)
            self._hear(route_input, ctx, heard["text"])

        transcript_task.add_done_callback(hear)
        outcome = await self._run_realtime(owned, ctx, events)
        await self._mouth.resume(ctx)
        try:
            await transcript_task
        except Exception as exc:
            # The answer already played from the audio; only the record is short.
            logger.warning("hybrid audio-first transcript failed: %s", type(exc).__name__)
        else:
            hear(transcript_task)
        return replace(outcome, heard=heard.get("text", ""), heard_ms=heard.get("ms"))

    def _hear(self, route_input: RouteInput, ctx: TurnContext, user_text: str) -> None:
        ctx.emit(
            transcript(
                ctx.session_id,
                ctx.turn_id,
                speaker=Speaker.USER,
                text=user_text,
                final=True,
                sttSource="provided_text" if route_input.text else "openai_realtime",
            )
        )
        self._remember("user", user_text)

    def _supersede(self, intent: CommittedIntent) -> None:
        superseded = self._mouth.supersede()
        if superseded:
            self._reducer.record(
                "presentation.superseded",
                presentation_epoch=self._reducer.presentation_epoch,
                intent_id=intent.intent_id,
                findings=superseded,
            )

    @staticmethod
    async def _consume_buffer(queue: asyncio.Queue) -> AsyncIterator[Any]:
        while True:
            event = await queue.get()
            if event is None:
                return
            if isinstance(event, Exception):
                raise event
            yield event

    async def _committed_text(self, route_input: RouteInput, ctx: TurnContext) -> str:
        if route_input.text:
            return " ".join(route_input.text.strip().split())
        committed = self._realtime.commit(route_input, turn_id=ctx.turn_id)
        final_text = ""
        async for item in _items(committed):
            if isinstance(item, RealtimeTranscript):
                if item.final:
                    final_text = item.text
            elif isinstance(item, str):
                final_text = item
            elif isinstance(item, SessionEvent):
                ctx.emit(item)
            elif isinstance(item, dict) and item.get("type") in {
                "transcript",
                "transcript.final",
            }:
                final_text = str(item.get("text", ""))
        final_text = " ".join(final_text.strip().split())
        if not final_text:
            raise RouteRuntimeError("Realtime committed audio without a transcript")
        return final_text

    def _emit_ownership(self, ctx: TurnContext, owned: OwnedIntent) -> None:
        fields: dict[str, Any] = {
            "owner": owned.owner.value,
            "reason": owned.reason.value,
            "intentId": owned.intent.intent_id.value,
            "presentationEpoch": owned.intent.presentation_epoch.value,
        }
        if isinstance(owned, SpecialistOwned):
            fields["taskId"] = owned.task_id.value
        ctx.emit(agent_event(ctx.session_id, ctx.turn_id, "hybrid.intent.committed", **fields))

    async def _run_realtime(
        self, owned: RealtimeOwned, ctx: TurnContext, buffered: asyncio.Queue | None = None
    ) -> TurnOutcome:
        epoch = owned.intent.presentation_epoch
        generated: list[str] = []
        bytes_generated = 0
        first_audio = False
        tool_requests: list[Any] = []
        ctx.emit(
            response(ctx.session_id, ctx.turn_id, SessionEventType.RESPONSE_STARTED)
        )
        try:
            async with self._presentation_lock:
                if not self._epoch_is_current(epoch):
                    return self._cancelled_outcome(ctx, generated)
                self._set_presenting(ctx.turn_id)
                try:
                    output = (self._consume_buffer(buffered) if buffered is not None else
                              self._realtime.respond(owned.intent.transcript, turn_id=ctx.turn_id))
                    async for item in _items(output):
                        if not self._epoch_is_current(epoch) or self._closed:
                            break
                        if isinstance(item, SessionEvent):
                            ctx.emit(item)
                            continue
                        if isinstance(item, RealtimeTranscript):
                            if item.text:
                                generated.append(item.text)
                            continue
                        if _is_specialist_request(item):
                            tool_requests.append(item)
                            await self._realtime.cancel_response(turn_id=ctx.turn_id)
                            break
                        if isinstance(item, str):
                            generated.append(item)
                            continue
                        pcm = self._realtime_pcm(item)
                        if pcm is None:
                            continue
                        bytes_generated += len(pcm)
                        if self._audio_output is not None:
                            await self._audio_output.write(pcm, ctx.turn_id)
                        if not first_audio:
                            first_audio = True
                            _mark(ctx, "answer_first_output_enqueue", audioCategory="answer")
                            ctx.emit(
                                response(
                                    ctx.session_id,
                                    ctx.turn_id,
                                    SessionEventType.RESPONSE_FIRST_OUTPUT,
                                    output_kind="audio",
                                )
                            )
                finally:
                    await self._finish_audio_output(ctx.turn_id)
                    self._clear_presenting(ctx.turn_id)
        except Exception as exc:
            self._record_delivery(ctx, " ".join(generated), speech_kind="answer",
                                  generated_bytes=bytes_generated, interrupted=True)
            failure = HybridFailure(
                HybridFailureCode.REALTIME,
                str(exc),
                type(exc).__name__,
                retryable=True,
            )
            self._emit_failure(ctx, failure, retained=tuple(generated))
            return TurnOutcome(
                status="failed",
                turn_id=ctx.turn_id,
                played=self._played_fraction(),
                generated=list(generated),
                error=failure.diagnostic(),
            )

        spoken = " ".join(" ".join(item.split()) for item in generated if item.strip())
        if not self._epoch_is_current(epoch):
            self._record_delivery(ctx, spoken, speech_kind="answer",
                                  generated_bytes=bytes_generated, interrupted=True)
            return self._cancelled_outcome(ctx, generated)
        if spoken and bytes_generated == 0:
            delivered = await self._mouth.speak(
                Script(spoken, "realtime_text"), ctx, epoch,
            )
        else:
            delivered = bool(bytes_generated) and self._record_delivery(
                ctx, spoken, speech_kind="answer", generated_bytes=bytes_generated,
            )
        for request in tool_requests:
            try:
                tool_outcome = await self._dispatch_from_mouth(request, ctx, owned.intent)
            except Exception as exc:
                failure = HybridFailure(
                    HybridFailureCode.REALTIME, str(exc), type(exc).__name__, retryable=True,
                )
                self._emit_failure(ctx, failure, retained=tuple(generated))
                return TurnOutcome(
                    "failed", ctx.turn_id, played=self._played_fraction(), generated=generated,
                    error=failure.diagnostic(),
                )
            if tool_outcome is not None:
                generated.extend(tool_outcome.generated or [])
                delivered = delivered or bool(tool_outcome.played)
        if not delivered:
            failure = HybridFailure(HybridFailureCode.REALTIME,
                                    "Realtime returned no audible response", "SilentResponse")
            self._emit_failure(ctx, failure)
            return TurnOutcome("failed", ctx.turn_id, played=[], generated=generated,
                               error=failure.diagnostic())
        ctx.emit(
            response(
                ctx.session_id,
                ctx.turn_id,
                SessionEventType.RESPONSE_COMPLETED,
                generatedAudioBytes=bytes_generated,
            )
        )
        return TurnOutcome(
            status="completed",
            turn_id=ctx.turn_id,
            played=self._played_fraction(),
            generated=list(generated),
            pending_task_id=self.active_task_id.value if self.active_task_id else None,
        )

    def _start_holding_line(
        self, route_input: RouteInput, ctx: TurnContext
    ) -> "asyncio.Task[bool] | None":
        """Say something if transcription is taking a while.

        Hybrid must have text before it can route, so it waits one OpenAI
        round trip to transcribe and a second to speak — a measured 2.0 s
        median to first audio against cascade's 0.9 s. Cascade never pays the
        first: Deepgram transcribes while the user is still talking.

        This does not make transcription faster. It stops the wait being
        silent, which is the part a person actually feels.
        """
        if route_input.text or not self._first_token_budget_s:
            return None
        started = asyncio.Event()
        task = asyncio.create_task(
            self._holding_line(ctx, started), name="hybrid-holding-line"
        )
        return (task, started)

    async def _holding_line(self, ctx: TurnContext, started: asyncio.Event) -> bool:
        await asyncio.sleep(self._first_token_budget_s)
        started.set()
        return bool(
            await self._mouth.speak(
                Script(_HOLDING_LINE, "ack"), ctx, self._reducer.presentation_epoch
            )
        )

    async def _settle_holding_line(self, holding: Any) -> bool:
        """Stop waiting to fill a gap that has closed — but never mid-word.

        Cancelling a line that had already begun cut it off within a few
        hundred milliseconds and left the presentation lock held, so the
        acknowledgement that followed was silent too while its text still
        reached the channel. A line that has started gets to finish.
        """
        if holding is None:
            return False
        task, started = holding
        if not started.is_set():
            task.cancel()
            return False
        try:
            return bool(await task)
        except (asyncio.CancelledError, Exception):
            return False

    async def _start_specialist(
        self,
        owned: SpecialistOwned,
        ctx: TurnContext,
        previously_active: TaskId | None,
        *,
        spoke_holding: bool = False,
    ) -> TurnOutcome:
        if previously_active is not None:
            failure = HybridFailure(
                HybridFailureCode.SPECIALIST,
                f"task {previously_active.value} is still active",
                "Busy",
            )
            self._reducer.transition_task(
                owned.task_id,
                TaskPhase.FAILED,
                error=failure.diagnostic(),
            )
            busy_played = await self._mouth.speak(
                Script(self.BUSY_SENTENCE, LocalOwnershipReason.TASK_STATUS, previously_active),
                ctx,
                owned.intent.presentation_epoch,
            )
            return TurnOutcome(
                status="completed" if busy_played else "failed",
                turn_id=ctx.turn_id,
                error=None if busy_played else "Busy response produced no audio",
                played=[self.BUSY_SENTENCE] if busy_played else [],
                generated=[self.BUSY_SENTENCE],
            )

        self._task_contexts[owned.task_id] = ctx
        self._task_prompts[owned.task_id] = self._specialist_prompt(owned.intent.transcript)
        self._mouth.brief(owned.task_id, owned.intent.transcript)
        acknowledgment = Script.ack(owned.reason, owned.task_id)
        ack_played = spoke_holding
        if not spoke_holding:
            ack_played = await self._mouth.speak(
                acknowledgment,
                ctx,
                owned.intent.presentation_epoch,
            )
        # Report what was actually said. Reporting the skipped acknowledgement
        # put "I'll pull that up" in the Discord channel while the voice had
        # said "One moment", so the two feeds disagreed on every turn.
        spoken_text = _HOLDING_LINE if spoke_holding else acknowledgment.text
        record = self._reducer.task(owned.task_id)
        if self._closed or record is None or self._terminal(record.phase):
            return self._cancelled_outcome(ctx, [spoken_text] if ack_played else [])
        task = asyncio.create_task(
            self._run_specialist(owned, ctx),
            name=f"hybrid-specialist-{owned.task_id.value}",
        )
        self._detached[owned.task_id] = task
        task.add_done_callback(lambda _done, task_id=owned.task_id: self._detached_done(task_id))
        return TurnOutcome(
            status=("cancelled" if not self._epoch_is_current(owned.intent.presentation_epoch)
                    else "completed" if ack_played else "failed"),
            turn_id=ctx.turn_id,
            error=None if ack_played else "Specialist accepted, but acknowledgement produced no audio",
            played=[spoken_text] if ack_played else [],
            generated=[spoken_text],
            pending_task_id=owned.task_id.value,
        )

    async def _dispatch_from_mouth(
        self, request: Any, ctx: TurnContext, origin: CommittedIntent
    ) -> TurnOutcome | None:
        call_id = str(request.call_id)
        resolve = getattr(self._realtime, "resolve_specialist", None)
        if not callable(resolve):
            raise RouteRuntimeError("Realtime provider cannot resolve specialist calls")
        if call_id in self._tool_calls:
            return None
        if not self._epoch_is_current(origin.presentation_epoch):
            await _resolve(resolve(call_id, "Cancelled before dispatch."))
            return self._cancelled_outcome(ctx, [])
        question = " ".join(str(request.question).split())
        # Realtime rewords the question for the tool; the caller's own words
        # count too ("Wait, stop. What's the capital of France?").
        said = " ".join(str(origin.transcript or "").split())
        said = redirect_request(said) or said
        general = is_general_knowledge(question) or bool(said and is_general_knowledge(said))
        if general and question.lower() not in self._declined_questions:
            # "What's the capital of France?" handed to the agent: slow, and the
            # agent adds nothing. Send it back; the voice answers it itself. A
            # question Realtime insists on a second time goes to the agent.
            self._declined_questions.add(question.lower())
            self._tool_calls[call_id] = VOICE_ANSWERS_TOOL_OUTPUT
            await _resolve(resolve(call_id, VOICE_ANSWERS_TOOL_OUTPUT))
            self._reducer.record("specialist.declined", reason="general_knowledge")
            owned = RealtimeOwned(replace(origin, transcript=question), RealtimeOwnershipReason.GENERAL_CONVERSATION)
            return await self._run_realtime(owned, ctx)
        previously_active = self.active_task_id
        message = "Busy. The current task is still running." if previously_active else "Accepted."
        intent = replace(origin, transcript=question)
        owned = SpecialistOwned(intent, TaskId.mint(), SpecialistOwnershipReason.HANDS)
        self._reducer.delegate(owned)
        self._tool_calls[call_id] = message
        try:
            await _resolve(resolve(call_id, message))
        except BaseException:
            self._tool_calls.pop(call_id, None)
            record = self._reducer.task(owned.task_id)
            if not self._terminal(record.phase):
                self._reducer.transition_task(owned.task_id, TaskPhase.CANCELLED)
            raise
        record = self._reducer.task(owned.task_id)
        if not self._epoch_is_current(origin.presentation_epoch) or self._terminal(record.phase):
            if not self._terminal(record.phase):
                self._reducer.transition_task(owned.task_id, TaskPhase.CANCELLED)
            return self._cancelled_outcome(ctx, [])
        return await self._start_specialist(owned, ctx, previously_active)

    def _specialist_prompt(self, transcript: str) -> str:
        """What the specialist is actually asked.

        `reply_length` used to reach only cascade, so a profile set to
        summarized still got hybrid's full-length answers. Asking for a
        short answer is the half that shortens the turn: the mouth reads
        the findings back, so a specialist that wrote four paragraphs is
        four paragraphs to listen to however briefly the mouth is told to
        put it. There is no tail trim to fall back on here — the answer is
        generated as audio, and cutting audio short cuts it mid-word.
        """
        asked = self._conversation_context(transcript) + transcript
        # Concise shortens the voice model's reply, while keeping the
        # specialist's complete written findings. Summarized also asks less.
        value = self._reply_length() if callable(self._reply_length) else self._reply_length
        if str(value).strip().lower() == "summarized":
            return f"{BRIEF_SPEECH_CONTRACT}\n\n{asked}"
        return asked

    def _remember(self, speaker: str, text: str) -> None:
        cleaned = " ".join(text.split())
        if cleaned:
            self._exchanges.append((speaker, cleaned))

    def _conversation_context(self, current_request: str) -> str:
        """The recent exchange, for a specialist that never hears any of it.

        The specialist keeps its own session, but that session only contains
        what was dispatched to it — never the user turns the mouth answered,
        and never a word the mouth said. A partial record reads as a complete
        one, so it answers confidently about a conversation it did not have.
        """
        # The utterance being dispatched is already the request; repeating it
        # as its own context is noise on the very first turn.
        earlier = list(self._exchanges)
        if earlier and earlier[-1] == ("user", current_request):
            earlier.pop()
        if not earlier:
            return ""
        lines = "\n".join(
            f"{'User' if who == 'user' else self._role_contract.display_name}: {what}"
            for who, what in earlier
        )
        return (
            "Conversation so far, for context only — you are the working half "
            "and did not hear it:\n"
            f"{lines}\n\nThe request to act on:\n"
        )

    async def _keepalive_loop(self, task_id: TaskId) -> None:
        """Say something while the specialist works.

        Cascade narrates on a timer. Hybrid only ever spoke when an adapter
        reported a tool, so adapters that report none left the user in silence
        for the length of the task — a measured 49 seconds, ended by the user
        interrupting to check whether it was still alive.
        """
        from sleeptalk.policy import DEFAULT_REASSURE_TOOL_POOLS
        from sleeptalk.voice_tool_narration import narration_kind

        interval = self._keepalive_interval_s
        said = 0
        while not self._closed:
            await asyncio.sleep(interval)
            if self._mouth.quiet_for() < interval:
                continue
            if self._mouth.answer_pending():
                # Findings are in; delivery is next. Saying "still working"
                # here is what made every answer arrive with a reassurance
                # stapled to the end of it.
                continue
            pool = DEFAULT_REASSURE_TOOL_POOLS.get(
                narration_kind(self._current_tool),
                DEFAULT_REASSURE_TOOL_POOLS["other"],
            )
            self._mouth.reassure(pool[said % len(pool)], task_id=task_id)
            said += 1

    def _start_keepalive(self) -> None:
        self._stop_keepalive()
        task_id = self.active_task_id
        if self._keepalive_interval_s <= 0 or task_id is None:
            return
        self._keepalive = asyncio.create_task(
            self._keepalive_loop(task_id), name="hybrid-keepalive"
        )

    def _stop_keepalive(self) -> None:
        task, self._keepalive = self._keepalive, None
        if task is not None and not task.done():
            task.cancel()

    async def _run_specialist(self, owned: SpecialistOwned, ctx: TurnContext) -> None:
        task_id = owned.task_id
        lines: list[str] = []
        self._mouth.brief(task_id, owned.intent.transcript)
        self._current_tool = ""
        # Discord ends the ack turn and keeps appending mic frames into the
        # same OpenAI session the mouth uses for keepalive and the answer.
        self._capture_held = True
        self._start_keepalive()
        try:
            self._reducer.transition_task(task_id, TaskPhase.RUNNING)
            ctx.emit(agent_event(ctx.session_id, ctx.turn_id, "hybrid.task.started",
                                 taskId=task_id.value, sourceTurnId=ctx.turn_id))
            ctx.emit(
                response(
                    ctx.session_id,
                    ctx.turn_id,
                    SessionEventType.RESPONSE_STARTED,
                    owner=SemanticOwner.SPECIALIST.value,
                    taskId=task_id.value,
                )
            )
            sink = typed_events(
                lambda event: self._on_adapter_event(task_id, ctx, event)
            )
            async for raw_sentence in self._agent.respond_streaming(
                self._task_prompts[task_id],
                on_event=sink,
            ):
                sentence = " ".join(str(raw_sentence).strip().split())
                if not sentence:
                    continue
                current = self._reducer.task(task_id)
                if current is None or self._terminal(current.phase):
                    self._journal(
                        "task.late_output",
                        task_id=task_id,
                        intent_id=owned.intent.intent_id,
                        text=sentence,
                    )
                    continue
                lines.append(sentence)
                self._last_specialist_text = tuple(lines)
                self._reducer.transition_task(
                    task_id,
                    current.phase,
                    result=tuple(lines),
                )
                self._mouth.note(
                    Finding.from_agent_yield(task_id, sentence, ctx.turn_id)
                )
                self._sync_grounded_brief(task_id)

            current = self._reducer.task(task_id)
            if current is not None and current.phase is TaskPhase.FAILED:
                await self._report_specialist_failure(
                    task_id,
                    ctx,
                    HybridFailure(
                        HybridFailureCode.SPECIALIST,
                        current.error or "The specialist failed.",
                        "AgentError",
                    ),
                    tuple(lines),
                )
                return
            if current is None or self._terminal(current.phase):
                self._journal(
                    "task.late_completion",
                    task_id=task_id,
                    intent_id=owned.intent.intent_id,
                )
                return
            if not lines:
                raise RouteRuntimeError("The specialist completed without returning an answer.")
            # The agent has finished; nothing after this is waiting.
            self._stop_keepalive()
            self._reducer.transition_task(
                task_id,
                TaskPhase.COMPLETED,
                result=tuple(lines),
            )
            self._mouth.complete_task(task_id)
            self._sync_grounded_brief(task_id)
            ctx.emit(agent_event(
                ctx.session_id, ctx.turn_id, "hybrid.task.completed",
                taskId=task_id.value, sourceTurnId=ctx.turn_id,
                elapsedMs=self._elapsed_ms(ctx.turn_id),
                delivery=self._reducer.task(task_id).delivery.value,
            ))
            await self._mouth.wait()
            ctx.emit(
                response(
                    ctx.session_id,
                    ctx.turn_id,
                    SessionEventType.RESPONSE_COMPLETED,
                    owner=SemanticOwner.SPECIALIST.value,
                    taskId=task_id.value,
                )
            )
        except asyncio.CancelledError:
            current = self._reducer.task(task_id)
            if current is not None and not self._terminal(current.phase):
                self._reducer.transition_task(task_id, TaskPhase.CANCELLED, result=tuple(lines))
            raise
        except Exception as exc:
            current = self._reducer.task(task_id)
            if current is not None and current.phase in {TaskPhase.CANCELLED, TaskPhase.COMPLETED}:
                self._journal(
                    "task.late_failure",
                    task_id=task_id,
                    intent_id=owned.intent.intent_id,
                    errorType=type(exc).__name__,
                    message=str(exc),
                )
                return
            failure = HybridFailure(
                HybridFailureCode.SPECIALIST,
                str(exc),
                type(exc).__name__,
                retryable=True,
            )
            await self._report_specialist_failure(task_id, ctx, failure, tuple(lines))
        finally:
            self._stop_keepalive()
            self._clear_pending_for(task_id)
            try:
                await self._mouth.wait()
            finally:
                self._capture_held = False

    async def _report_specialist_failure(
        self,
        task_id: TaskId,
        ctx: TurnContext,
        failure: HybridFailure,
        retained: tuple[str, ...],
    ) -> None:
        self._stop_keepalive()
        current = self._reducer.task(task_id)
        if current.phase is not TaskPhase.FAILED:
            self._reducer.transition_task(
                task_id, TaskPhase.FAILED, result=retained, error=failure.diagnostic(),
            )
            self._emit_failure(ctx, failure, task_id=task_id, retained=retained)
        ctx.emit(
            agent_event(
                ctx.session_id,
                ctx.turn_id,
                "hybrid.task.failed",
                taskId=task_id.value,
                sourceTurnId=ctx.turn_id,
                elapsedMs=self._elapsed_ms(ctx.turn_id),
                errorType=failure.error_type,
                code=failure.code.value,
            )
        )
        line = _specialist_failure_line(RuntimeError(failure.message))
        if not retained and line == self.SPECIALIST_FAILED_SENTENCE:
            line = "I couldn't complete that request. The coding agent failed before returning an answer."
        await self._mouth.speak(
            Script(line, LocalOwnershipReason.TASK_STATUS, task_id),
            self._floor_context or ctx,
            self._reducer.presentation_epoch,
        )
        self._sync_grounded_brief(task_id)

    def _on_adapter_event(
        self, task_id: TaskId, ctx: TurnContext, event: AdapterEvent
    ) -> None:
        current = self._reducer.task(task_id)
        if current is None:
            return
        if self._terminal(current.phase):
            self._journal(
                "task.late_event",
                task_id=task_id,
                intent_id=current.origin_intent_id,
                eventKind=event.kind,
            )
            return

        self._emit_adapter_event(ctx, task_id, event)
        if isinstance(event, ToolEvent) and event.phase is ToolPhase.STARTED:
            self._current_tool = event.name
            self._mouth.status(event.name, event.detail or "", task_id=task_id)
            return
        if isinstance(event, SessionIdentified):
            self._reducer.transition_task(
                task_id,
                current.phase,
                agent_session_ref=event.upstream_session_id,
            )
            return
        if isinstance(event, PermissionRequested):
            self._pending_permission = (task_id, event)
            self._reducer.set_pending_input(
                permission=True,
                clarification=self._pending_clarification is not None,
            )
            self._reducer.transition_task(task_id, TaskPhase.NEEDS_INPUT)
            self._mouth.note(Finding.needs_input(task_id, event.prompt, ctx.turn_id))
            return
        if isinstance(event, ClarificationRequested):
            self._pending_clarification = (task_id, event)
            self._reducer.set_pending_input(
                permission=self._pending_permission is not None,
                clarification=True,
            )
            self._reducer.transition_task(task_id, TaskPhase.NEEDS_INPUT)
            self._mouth.note(Finding.needs_input(task_id, event.prompt, ctx.turn_id))
            return
        if isinstance(event, AgentError):
            failure = HybridFailure(
                HybridFailureCode.SPECIALIST,
                event.message,
                event.code,
                retryable=event.retryable,
            )
            self._reducer.transition_task(
                task_id,
                TaskPhase.FAILED,
                error=failure.diagnostic(),
            )
            self._emit_failure(ctx, failure, task_id=task_id)
            return
        if isinstance(event, StatusChanged):
            phase_by_state = {
                "working": TaskPhase.RUNNING,
                "blocked": TaskPhase.NEEDS_INPUT,
            }
            phase = phase_by_state.get(event.state.value)
            if phase is not None:
                self._reducer.transition_task(task_id, phase)

    def _emit_adapter_event(
        self, ctx: TurnContext, task_id: TaskId, event: AdapterEvent
    ) -> None:
        if isinstance(event, SessionIdentified):
            ctx.emit(
                agent_event(
                    ctx.session_id,
                    ctx.turn_id,
                    "session.identified",
                    taskId=task_id.value,
                    upstreamSessionId=event.upstream_session_id,
                    resumable=event.resumable,
                )
            )
            return
        try:
            normalized = adapter_session_event(ctx.session_id, ctx.turn_id, event)
        except EventError:
            normalized = agent_event(
                ctx.session_id,
                ctx.turn_id,
                event.kind,
                taskId=task_id.value,
            )
        else:
            normalized.payload.setdefault("taskId", task_id.value)
        normalized.payload.setdefault("owner", SemanticOwner.SPECIALIST.value)
        ctx.emit(normalized)

    async def _run_local(self, owned: LocalOwned, ctx: TurnContext) -> TurnOutcome:
        reason = owned.reason
        generated: list[str] = []
        if reason is LocalOwnershipReason.INTERRUPT_PRESENTATION:
            await self.interrupt_presentation(ctx.turn_id, source=InterruptionSource.CANCEL)
            return TurnOutcome("completed", ctx.turn_id, played=[], generated=[])
        if reason is LocalOwnershipReason.CANCEL_TASK:
            task_id = self.active_task_id
            cancelled = task_id is not None and await self.cancel_task(task_id.value)
            sentence = self.CANCELLED_SENTENCE if cancelled else "No task is running."
        elif reason is LocalOwnershipReason.TASK_STATUS:
            sentence = (
                self.BUSY_SENTENCE if self.active_task_id is not None else "No task is running."
            )
        elif reason is LocalOwnershipReason.PRESENCE:
            sentence = "I'm here."
        elif reason is LocalOwnershipReason.REPEAT:
            played, spoken = await self._mouth.repeat(ctx)
            if spoken:
                generated.append(spoken)
            else:
                spoken = "I don't have anything to repeat."
                generated.append(spoken)
                played = await self._mouth.speak(
                    Script(spoken, reason),
                    ctx,
                    self._reducer.presentation_epoch,
                )
            return TurnOutcome(
                "completed" if played else "failed",
                ctx.turn_id,
                error=None if played else "Repeat response produced no completed audio",
                played=list(generated) if played else [],
                generated=generated,
            )
        elif reason is LocalOwnershipReason.PENDING_PERMISSION:
            sentence = await self._resolve_permission(owned, ctx)
        elif reason is LocalOwnershipReason.PENDING_CLARIFICATION:
            sentence = await self._answer_clarification(owned, ctx)
        elif reason is LocalOwnershipReason.ROUTE_TOPOLOGY:
            if is_hands_question(owned.intent.transcript):
                sentence = self._role_contract.hands_answer(self._hands)
            else:
                sentence = self._role_contract.topology_answer
        else:
            sentence = "I couldn't apply that control."
        generated.append(sentence)
        played = await self._mouth.speak(
            Script(sentence, reason),
            ctx,
            self._reducer.presentation_epoch,
        )
        return TurnOutcome(
            "completed" if played else "failed",
            ctx.turn_id,
            error=None if played else "Control response produced no completed audio",
            played=list(generated) if played else [],
            generated=generated,
        )

    async def _resolve_permission(self, owned: LocalOwned, ctx: TurnContext) -> str:
        if ctx.extras.get("permission_authorized") is False:
            return "Only an authorized operator can answer this permission request."
        pending = self._pending_permission
        if pending is None:
            return "No permission request is pending."
        task_id, request = pending
        lowered = owned.intent.transcript.lower()
        decision = (
            PermissionDecision.DENY
            if lowered.startswith(("no", "deny", "decline", "do not", "don't"))
            else PermissionDecision.ALLOW_ONCE
        )
        command = ResolvePermission(request.request_id, decision)
        result = await adapter_control(self._agent, command)
        ctx.emit(control_session_event(ctx.session_id, ctx.turn_id, command))
        if isinstance(result, ControlAccepted):
            self._pending_permission = None
            self._refresh_pending_flags()
            self._resume_task(task_id)
            return "Permission denied." if decision is PermissionDecision.DENY else "Permission granted."
        return self._control_rejected(result)

    async def _answer_clarification(self, owned: LocalOwned, ctx: TurnContext) -> str:
        pending = self._pending_clarification
        if pending is None:
            return "No clarification is pending."
        task_id, request = pending
        command = AnswerClarification(request.request_id, owned.intent.transcript)
        result = await adapter_control(self._agent, command)
        ctx.emit(control_session_event(ctx.session_id, ctx.turn_id, command))
        if isinstance(result, ControlAccepted):
            self._pending_clarification = None
            self._refresh_pending_flags()
            self._resume_task(task_id)
            return "Got it."
        return self._control_rejected(result)

    @staticmethod
    def _control_rejected(result: Any) -> str:
        if isinstance(result, ControlRejected):
            return f"The specialist rejected that control: {result.reason}"
        return "The specialist rejected that control."

    def _resume_task(self, task_id: TaskId) -> None:
        record = self._reducer.task(task_id)
        if record is not None and not self._terminal(record.phase):
            self._reducer.transition_task(task_id, TaskPhase.RUNNING)

    async def interrupt_presentation(
        self,
        turn_id: int | None = None,
        *,
        source: InterruptionSource = InterruptionSource.USER_SPEECH,
    ) -> None:
        """Stop speaking.

        Neither kind of stop volunteers the answer afterwards. Talking over
        "I'll look that up" and then asking something else used to get the
        first question answered on top of the second one, because every
        later turn's `resume` drained whatever was still pending.

        `USER_SPEECH` is someone talking over the answer: the specialist's
        work survives and "repeat that" still reaches it. `CANCEL` is a
        deliberate stop, and stop has to mean stop — the pending findings
        are given up, so there is nothing left to ask for either.
        """
        if self._closed:
            return
        epoch = self._reducer.presentation_epoch
        target_turn = self._presenting_turn if self._presenting_turn is not None else turn_id
        fence = (target_turn, epoch.value)
        if fence in self._interrupted_presentations:
            return
        self._interrupted_presentations.add(fence)
        self._reducer.revoke_presentation()
        played_ms = self._presentation_played_bytes() // 96
        played_fraction = self._played_fraction()
        gave_up = source is InterruptionSource.CANCEL
        withheld = self._mouth.pause(withhold=gave_up)
        if self._audio_output is not None and target_turn is not None:
            await self._audio_output.discard_turn(target_turn, "interrupted")
        await _resolve(self._realtime.cancel_response(turn_id=target_turn))
        truncate = getattr(self._realtime, "truncate_output", None)
        if callable(truncate):
            await truncate(played_ms)
        if withheld:
            self._reducer.record(
                "presentation.withheld" if gave_up else "presentation.held",
                presentation_epoch=epoch,
                findings=withheld,
            )
        emit = self._emit
        if callable(emit) and target_turn is not None:
            emit(
                interruption(
                    self._session_id,
                    target_turn,
                    source=source,
                    phase=InterruptionPhase.ANSWERING,
                    played=played_fraction,
                    generated=None,
                )
            )

    async def cancel_turn(self, turn_id: int) -> None:
        await self.interrupt_presentation(turn_id)

    async def cancel_task(self, task_id: str) -> bool:
        selected = next((record for record in self.tasks if record.task_id.value == task_id), None)
        if selected is None or self._terminal(selected.phase):
            return False
        if selected.task_id in self._cancel_signalled:
            return False
        self._cancel_signalled.add(selected.task_id)
        self._reducer.transition_task(
            selected.task_id,
            TaskPhase.CANCELLED,
            result=selected.result,
        )
        self._clear_pending_for(selected.task_id)
        self._mouth.drop_task(selected.task_id)
        self._clear_grounded_brief(selected.task_id)
        await _resolve(self._realtime.cancel_response(turn_id=self._presenting_turn))
        command = Cancel(target_id=selected.task_id.value)
        ctx = self._task_contexts.get(selected.task_id)
        result: Any = ControlRejected(reason="control failed")
        try:
            result = await adapter_control(self._agent, command)
            if ctx is not None:
                ctx.emit(control_session_event(ctx.session_id, ctx.turn_id, command))
        except Exception as exc:
            if ctx is not None:
                self._emit_failure(
                    ctx,
                    HybridFailure(
                        HybridFailureCode.CONTROL,
                        str(exc),
                        type(exc).__name__,
                        retryable=True,
                    ),
                    task_id=selected.task_id,
                )
        try:
            if not isinstance(result, ControlAccepted):
                abort = getattr(self._agent, "abort", None)
                if callable(abort):
                    await _resolve(abort())
        finally:
            detached = self._detached.get(selected.task_id)
            if detached is not None and not detached.done():
                detached.cancel()
        self._journal(
            "task.cancelled",
            task_id=selected.task_id,
            intent_id=selected.origin_intent_id,
            targeted=isinstance(result, ControlAccepted),
        )
        return True

    async def recover(self) -> RecoveryResult:
        reducer = self._reducer
        try:
            await self.interrupt_presentation(self._presenting_turn)
            provider_result = await _resolve(
                self._realtime.recover(session_id=self._session_id)
            )
        except Exception as exc:
            return RecoveryResult(
                RecoveryOutcome.FAILED,
                self._session_id,
                f"{type(exc).__name__}: {exc}",
            )
        assert self._reducer is reducer
        if isinstance(provider_result, RecoveryResult):
            return RecoveryResult(
                provider_result.outcome,
                self._session_id,
                provider_result.detail,
            )
        return RecoveryResult(
            RecoveryOutcome.RESTORED if provider_result is not False else RecoveryOutcome.FAILED,
            self._session_id,
        )

    def active_turn(self) -> InFlightTurn | None:
        if self._in_flight is not None:
            return self._in_flight
        if self._presenting_turn is not None:
            return InFlightTurn(self._presenting_turn, self._presentation_task)
        return None

    def output(self) -> AudioOutput | None:
        return self._audio_output

    def set_audio_output(self, output: AudioOutput) -> None:
        self._audio_output = output
        self._mouth.set_audio_output(output)

    async def settle_usage(self, turn_id: int, *, status: str) -> None:
        del status
        self._settled_turns.add(turn_id)

    async def wait_for_detached(self) -> None:
        tasks = tuple(self._detached.values())
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self._mouth.wait()

    async def close(self) -> None:
        if self._closed:
            return
        for record in self.tasks:
            if not self._terminal(record.phase):
                await self.cancel_task(record.task_id.value)
        await self.interrupt_presentation(self._presenting_turn)
        self._closed = True
        for task in tuple(self._detached.values()):
            if not task.done():
                task.cancel()
        await self.wait_for_detached()
        await self._mouth.close()
        await _resolve(self._realtime.close())
        close_agent = getattr(self._agent, "close", None)
        if callable(close_agent):
            await _resolve(close_agent())

    def _require_ready(self, operation: str) -> None:
        if self._closed:
            raise RouteStateError(f"{operation} after close")
        if not self._prepared:
            raise RouteStateError(f"{operation} before prepare")

    def _set_presenting(self, turn_id: int) -> None:
        self._presenting_turn = turn_id
        self._playback_accounting = self._audio_output.accounting() if self._audio_output else None
        self._playback_start = self._playback_accounting.bytes_written if self._playback_accounting else 0
        self._playback_generated_start = (
            self._playback_accounting.bytes_generated if self._playback_accounting else 0
        )
        self._playback_turn_id = self._playback_accounting.turn_id if self._playback_accounting else None
        self._presentation_task = asyncio.current_task()

    def _clear_presenting(self, turn_id: int) -> None:
        if self._presenting_turn == turn_id:
            self._presenting_turn = None
            self._presentation_task = None

    def _epoch_is_current(self, epoch: PresentationEpoch) -> bool:
        return epoch == self._reducer.presentation_epoch

    def _presentation_played_bytes(self) -> int:
        if self._audio_output is None:
            return 0
        accounting = self._audio_output.accounting()
        # Some outputs reset accounting when a new turn starts.
        baseline = self._playback_start if (self._playback_accounting is not None
                    and accounting.turn_id == self._playback_turn_id) else 0
        return max(0, accounting.bytes_written - baseline)

    def _presentation_complete(self) -> bool:
        """Did every byte of this presentation cross the playout boundary?

        An answer that finished speaking, then met a barge-in aimed at the
        silence after it, was still recorded as interrupted — so it was
        never marked told, and the next turn said the whole thing again.
        Outputs that cannot report a drained queue answer False and keep the
        cautious reading.
        """
        if self._audio_output is None or self._playback_accounting is None:
            return False
        accounting = self._audio_output.accounting()
        if accounting.turn_id != self._playback_turn_id:
            return False
        generated = accounting.bytes_generated - self._playback_generated_start
        if generated <= 0:
            return False
        return self._presentation_played_bytes() >= generated

    def _elapsed_ms(self, turn_id: int) -> int:
        return int((time.monotonic() - self._turn_started.get(turn_id, time.monotonic())) * 1000)

    def _record_delivery(
        self, ctx: TurnContext, text: str, *, speech_kind: str,
        generated_bytes: int = 0, interrupted: bool = False,
        task_id: TaskId | None = None, source_turn_id: int | None = None,
    ) -> bool:
        played = self._presentation_played_bytes()
        source_turn_id = source_turn_id if source_turn_id is not None else ctx.turn_id
        status = "interrupted" if interrupted else "delivered" if played else "silent"
        fields = dict(sourceTurnId=source_turn_id, deliveryTurnId=ctx.turn_id,
                      taskId=task_id.value if task_id else None, speechKind=speech_kind,
                      elapsedMs=self._elapsed_ms(source_turn_id), bytesPlayed=played,
                      generatedAudioBytes=generated_bytes, status=status)
        ctx.emit(agent_event(ctx.session_id, ctx.turn_id, "hybrid.delivery", **fields))
        if text and played:
            self._remember("assistant", text + (" [interrupted; only partially heard]" if interrupted else ""))
            ctx.emit(transcript(ctx.session_id, ctx.turn_id, speaker=Speaker.ASSISTANT,
                                text=text, final=True, delivered=True, owner="realtime",
                                interrupted=interrupted, **fields))
        return bool(played) and not interrupted

    def _played_fraction(self) -> float:
        if self._audio_output is None:
            return 0.0
        return self._audio_output.accounting().played_fraction

    async def _finish_audio_output(self, turn_id: int) -> None:
        finish = getattr(self._audio_output, "finish_turn", None)
        if callable(finish):
            await _resolve(finish(turn_id))

    @staticmethod
    def _realtime_pcm(item: Any) -> bytes | None:
        if isinstance(item, RealtimeAudio):
            return item.pcm
        if isinstance(item, (bytes, bytearray)):
            return bytes(item)
        if isinstance(item, dict) and item.get("type") in {"audio", "audio.delta"}:
            pcm = item.get("pcm", item.get("audio"))
            if isinstance(pcm, (bytes, bytearray)):
                return bytes(pcm)
        return None

    def _cancelled_outcome(
        self, ctx: TurnContext, generated: list[str]
    ) -> TurnOutcome:
        ctx.emit(
            response(ctx.session_id, ctx.turn_id, SessionEventType.RESPONSE_CANCELLED)
        )
        return TurnOutcome(
            status="cancelled",
            turn_id=ctx.turn_id,
            played=self._played_fraction(),
            generated=list(generated),
        )

    def _emit_failure(
        self,
        ctx: TurnContext,
        failure: HybridFailure,
        *,
        task_id: TaskId | None = None,
        retained: tuple[str, ...] = (),
    ) -> None:
        fields: dict[str, Any] = {
            "code": failure.code.value,
            "errorType": failure.error_type,
            "retryable": failure.retryable,
            "turnId": ctx.turn_id,
            "retainedText": list(retained),
        }
        if task_id is not None:
            fields["taskId"] = task_id.value
        logger.warning(
            "hybrid turn %s failed (%s, %s): %s",
            ctx.turn_id, failure.code.value, failure.error_type, failure.message,
        )
        ctx.emit(error(ctx.session_id, failure.message, fatal=False, **fields))

    def _journal(
        self,
        kind: str,
        *,
        task_id: TaskId,
        intent_id: IntentId,
        **payload: Any,
    ) -> None:
        self._reducer.record(
            kind,
            owner=SemanticOwner.SPECIALIST,
            task_id=task_id,
            intent_id=intent_id,
            presentation_epoch=self._reducer.presentation_epoch,
            **payload,
        )

    def _sync_grounded_brief(self, task_id: TaskId) -> None:
        brief = self._mouth.grounded(task_id)
        if brief is None:
            return
        self._brief = brief
        set_brief = getattr(self._realtime, "set_brief", None)
        if callable(set_brief):
            set_brief(brief)

    def _clear_grounded_brief(self, task_id: TaskId) -> None:
        if self._brief is None or self._brief.task_id != task_id:
            return
        self._brief = None
        set_brief = getattr(self._realtime, "set_brief", None)
        if callable(set_brief):
            set_brief(None)

    def _detached_done(self, task_id: TaskId) -> None:
        self._detached.pop(task_id, None)

    def _clear_pending_for(self, task_id: TaskId) -> None:
        if self._pending_permission is not None and self._pending_permission[0] == task_id:
            self._pending_permission = None
        if (
            self._pending_clarification is not None
            and self._pending_clarification[0] == task_id
        ):
            self._pending_clarification = None
        self._refresh_pending_flags()

    def _refresh_pending_flags(self) -> None:
        self._reducer.set_pending_input(
            permission=self._pending_permission is not None,
            clarification=self._pending_clarification is not None,
        )

    @staticmethod
    def _terminal(phase: TaskPhase) -> bool:
        return phase in {TaskPhase.COMPLETED, TaskPhase.CANCELLED, TaskPhase.FAILED}

    @staticmethod
    def _presentation_forbidden(phase: TaskPhase) -> bool:
        return phase is TaskPhase.CANCELLED
