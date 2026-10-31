"""Public session lifecycle, turn ownership, and normalized events.

The builder supplies components; routes own conversation behavior; transports
own capture and playback. RealtimeSession coordinates their shared lifecycle.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import time
import inspect
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from sleeptalk.adapter import WorkerEvent, adapter_capabilities
from sleeptalk.session_event_log import append_session_event
from sleeptalk.session_events import (
    InterruptionSource,
    SessionEvent,
    SessionState,
    agent_event,
    roster_session_event,
    session_state,
)
from sleeptalk.policy import narration_style_or_default, reply_length_or_default
from sleeptalk.speech_speed import next_speed, snap_speed
from sleeptalk.subagent_roster import RosterChange, SubagentRoster
from sleeptalk.voice.subagent import WorkerVoiceState

__all__ = ["RealtimeSession", "SessionLifecycleError", "TurnLifecycle"]

# How long a failed route prepare is remembered. Long enough that audio
# ingress cannot re-trigger a warmup per frame, short enough that a transient
# failure still recovers without restarting the bot.
_PREPARE_RETRY_COOLDOWN_S = 5.0
# Quiet needed before the session speaks on its own (a job's announcement):
# since the last turn ended and since the caller last started talking.
_ANNOUNCE_GAP_S = 1.5

logger = logging.getLogger("sleeptalk.session")



# A voice ID a call peer may ask for: printable ASCII with no whitespace.
# Provider IDs are UUIDs, slugs, or storage paths, all within this; it keeps
# control characters and non-scalar text out of URLs and the profile file.
VOICE_ID_PATTERN = re.compile(r"[\x21-\x7e]{1,200}")


class VoiceUnavailableError(RuntimeError):
    """A requested voice did not synthesize; ``code`` is a fixed page code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _switch_adapter_voice(adapter: Any, voice: str) -> None:
    setter = getattr(adapter, "set_voice", None)
    if callable(setter):
        setter(voice)
    else:
        adapter.spec = replace(adapter.spec, voice_id=voice)


def _voice_failure_code(exc: Exception) -> str:
    if isinstance(exc, asyncio.TimeoutError):
        return "network_timeout"
    status = getattr(exc, "status", None)
    if status is None:
        import re
        match = re.search(r"\b(401|403|404|429)\b", str(exc))
        status = int(match[0]) if match else None
    return {401: "invalid_credential", 403: "permission_denied", 404: "voice_unavailable", 429: "quota_or_rate_limit"}.get(status, "voice_unavailable")

@dataclass(frozen=True)
class TurnLifecycle:
    """Transport presentation hooks inherited by automatic continuation turns."""

    started: Callable[[int], None] | None = None
    finished: Callable[[int, Any], Awaitable[None]] | None = None


class SessionLifecycleError(RuntimeError):
    """Illegal lifecycle transition for the session."""


#: Lifecycle transitions (ADR 0003 §2): CREATED -> CONNECTING -> ACTIVE
#: -> CLOSING -> CLOSED, with RECOVERING between ACTIVE and ACTIVE.
_ALLOWED: dict[SessionState, set[SessionState]] = {
    SessionState.CREATED: {SessionState.CONNECTING},
    SessionState.CONNECTING: {SessionState.ACTIVE, SessionState.CLOSED},
    SessionState.ACTIVE: {SessionState.RECOVERING, SessionState.CLOSING},
    SessionState.RECOVERING: {SessionState.ACTIVE, SessionState.CLOSED},
    SessionState.CLOSING: {SessionState.CLOSED},
    SessionState.CLOSED: set(),
}


class RealtimeSession:
    """The route-agnostic session contract (ADR 0003 §1/§2/§6).

    Attributes:
        session_id: stable opaque identity for the session's whole life —
            survives recovery/rollover (never derived from object ids).
        state: the lifecycle state (§2).
        route_type: the assembly strategy of the wrapped route, if any.
    """

    def __new__(cls, built: Any):
        # Compatibility callers from the incremental cutover may still call
        # ``RealtimeSession(build_session(...))``.  Construction is an
        # identity operation for an existing kernel, never a second lifecycle
        # owner around the same route/providers.
        if isinstance(built, cls):
            return built
        return super().__new__(cls)

    def __init__(self, built: Any) -> None:
        if getattr(self, "_initialized", False):
            return
        self._initialized = True
        self._built = built
        self.session_id = (
            getattr(getattr(built, "usage_meter", None), "session_id", None)
            or uuid.uuid4().hex[:12]
        )
        self._state = SessionState.CREATED
        self._events: list[SessionEvent] = []
        self._subscribers: set[Callable[[SessionEvent], Any]] = set()
        self._subscriber_tasks: set[asyncio.Task[Any]] = set()
        route = getattr(built, "route", None)
        self._route = route
        self.route_type = getattr(getattr(route, "capabilities", None), "route_type", None)
        self._prepared = False
        self._prepare_failure: BaseException | None = None
        self._prepare_failed_at = 0.0
        self._prepare_lock = asyncio.Lock()
        self._prepare_task: asyncio.Task[Any] | None = None
        self._audio_output: Any = None
        self._turn_id = 0
        self._turn_lock = asyncio.Lock()
        self._interruption_lock = asyncio.Lock()
        self._turn_task: asyncio.Task[Any] | None = None
        self._continuation_generation = 0
        self._resources_closed = False
        self._close_lock = asyncio.Lock()
        self._roster: SubagentRoster | None = None
        self._worker_voice = WorkerVoiceState()
        # Background jobs (sleeptalk/jobs.py) and the lines they ask to say.
        self._jobs: Any = None
        self._announcements: list[str | Callable[[], str]] = []
        self._announce_task: asyncio.Task[None] | None = None
        self._last_turn_end = 0.0
        self._caller_heard_at = 0.0

    # -- identity + events ---------------------------------------------------

    @property
    def state(self) -> SessionState:
        return self._state

    @property
    def events(self) -> list[SessionEvent]:
        """Normalized session events, in emission order."""
        return list(self._events)

    def emit(self, event: SessionEvent) -> None:
        """Record and publish one normalized event without exposing internals."""
        self._events.append(event)
        append_session_event(event)
        for callback in tuple(self._subscribers):
            try:
                result = callback(event)
                if inspect.isawaitable(result):
                    task = asyncio.create_task(result)
                    self._subscriber_tasks.add(task)
                    task.add_done_callback(self._subscriber_tasks.discard)
            except Exception:  # noqa: BLE001,S112 - observers are isolated by contract
                continue  # observers must never break the voice turn

    def subscribe(self, callback: Callable[[SessionEvent], Any]) -> Callable[[], None]:
        """Subscribe to live normalized events; returns an idempotent unsubscribe."""
        if not callable(callback):
            raise TypeError("event subscriber must be callable")
        self._subscribers.add(callback)

        def unsubscribe() -> None:
            self._subscribers.discard(callback)

        return unsubscribe

    def unsubscribe(self, callback: Callable[[SessionEvent], Any]) -> None:
        """Remove a previously registered event subscriber."""
        self._subscribers.discard(callback)

    @property
    def roster(self) -> SubagentRoster | None:
        return self._roster

    @property
    def worker_voice(self) -> WorkerVoiceState:
        return self._worker_voice

    def attach_roster(self) -> SubagentRoster:
        if self._roster is None:
            self._roster = SubagentRoster.from_manifest(
                self.session_id, adapter_capabilities(self.agent)
            )
        return self._roster

    @property
    def jobs(self) -> Any:
        """The background-job manager, when this call can run one."""
        return self._jobs

    def _attach_jobs(self) -> None:
        factory = getattr(self._built, "job_agent_factory", None)
        if self._jobs is not None:
            return
        # Said once per call, so a handoff that never happens can be traced
        # (an agent remembering the <job> format from an earlier call writes
        # tags that nothing here would catch).
        if not callable(factory):
            logger.info("background jobs off for this call: no job agent for this route or agent type")
            return
        if getattr(getattr(self._built, "policy", None), "background_jobs", True) is False:
            logger.info("background jobs off for this call: policy.background_jobs is false")
            return
        from sleeptalk.jobs import JobManager

        max_jobs = getattr(getattr(self._built, "policy", None), "max_background_jobs", None)
        self._jobs = JobManager(factory, emit=self._job_event, announce=self.announce,
                                **({"max_jobs": max_jobs} if isinstance(max_jobs, int) else {}))
        logger.info("background jobs on for this call")

    def _job_event(self, name: str, fields: dict[str, Any]) -> None:
        # The call page and terminal show these as the worker's lines
        # (sleeptalk.presentation); Discord posts them to its text channel.
        self.emit(agent_event(self.session_id, self._turn_id, name, **fields))

    def note_caller_speech(self) -> None:
        """A surface heard the caller start talking: hold announcements back."""
        self._caller_heard_at = time.monotonic()

    def announce(self, line: str | Callable[[], str], *, urgent: bool = False) -> None:
        """Say ``line`` on its own at the next quiet moment.

        It runs as a turn of its own, so barge-in, captions and the audio
        path behave as for any reply. It never interrupts a turn or a caller
        who is talking. A callable is asked for its words only then, and an
        empty answer drops the line (the news was already given). An urgent
        line (a job waiting on the caller) goes ahead of the others: a job
        blocked on a question burns time, a finished one does not."""
        if self._resources_closed or not (callable(line) or line.strip()):
            return
        if urgent:
            self._announcements.insert(0, line)
        else:
            self._announcements.append(line)
        logger.info("announcement queued (%d waiting)", len(self._announcements))
        if self._announce_task is None or self._announce_task.done():
            self._announce_task = asyncio.create_task(self._deliver_announcements(), name="session-announcements")

    async def _deliver_announcements(self) -> None:
        from sleeptalk.routes.base import RouteInput

        while self._announcements and not self._resources_closed:
            await self._wait_for_quiet()
            if self._resources_closed or self._audio_output is None and not self._prepared:
                return
            pending = self._announcements.pop(0)
            line = " ".join((pending() if callable(pending) else pending).split())
            if not line:
                logger.info("announcement dropped: already told")
                continue
            logger.info("announcement speaking")
            try:
                live_announce = getattr(self._route, "announce", None)
                if self.continuous_input() and callable(live_announce):
                    await live_announce(line)
                    continue
                await self._run_turn(
                    RouteInput(text=line), output=None, extras={"announcement": True},
                    turn_id=None, lifecycle=None,
                )
            except Exception:  # noqa: BLE001 - an announcement never ends the call
                logger.warning("announcement failed", exc_info=True)

    async def _wait_for_quiet(self, gap_s: float = _ANNOUNCE_GAP_S) -> None:
        waited = 0
        while not self._resources_closed:
            now = time.monotonic()
            if (not self._turn_lock.locked() and now - self._last_turn_end >= gap_s
                    and now - self._caller_heard_at >= gap_s):
                return
            waited += 1
            if waited % 40 == 0:
                logger.info("announcement waiting: turn=%s sinceTurn=%.1fs sinceCaller=%.1fs",
                            self._turn_lock.locked(), now - self._last_turn_end, now - self._caller_heard_at)
            await asyncio.sleep(0.25)

    def admit_worker(self, event: WorkerEvent) -> RosterChange | None:
        change = self.attach_roster().apply(event)
        if change is not None:
            self.emit(roster_session_event(change))
        return change

    # -- lifecycle (§2) ---------------------------------------------------------

    def connect(self) -> None:
        """Acquire transport and route resources (idempotent while
        CONNECTING/ACTIVE — a second call is a no-op)."""
        if self._state in (SessionState.CONNECTING, SessionState.ACTIVE):
            return
        self._transition(SessionState.CONNECTING)
        self._transition(SessionState.ACTIVE)

    def close(self) -> None:
        """Finish the synchronous lifecycle state transition, idempotently.

        Existing embedded runtimes call this method from synchronous teardown
        hooks.  New async owners call :meth:`aclose`, which first cancels and
        releases route resources and then finishes this transition.
        """
        if self._state in (SessionState.CLOSED, SessionState.CLOSING):
            return
        self._continuation_generation += 1
        if self._state is not SessionState.ACTIVE:
            # CREATED/CONNECTING also close directly.
            self._state = SessionState.CLOSING
        else:
            self._transition(SessionState.CLOSING)
        self._transition(SessionState.CLOSED)

    async def aclose(self) -> None:
        """Cancel the active turn and close route/agent/meter exactly once."""
        async with self._close_lock:
            if self._resources_closed:
                self.close()
                return
            self._resources_closed = True
            self._continuation_generation += 1
            async with self._interruption_lock:
                pass
            first_error: BaseException | None = None
            announcing = self._announce_task
            if announcing is not None and announcing is not asyncio.current_task():
                announcing.cancel()
                await asyncio.gather(announcing, return_exceptions=True)
            if self._jobs is not None:
                try:
                    await self._jobs.close()
                except Exception as exc:  # noqa: BLE001 - continue teardown
                    first_error = exc
            prepare_task = self._prepare_task
            if prepare_task is not None and prepare_task is not asyncio.current_task():
                prepare_task.cancel()
                try:
                    await prepare_task
                except asyncio.CancelledError:
                    pass
                except Exception as exc:
                    first_error = exc
            route = self._route
            if route is not None:
                active_turn_id: int | None = None
                try:
                    active = route.active_turn()
                    if active is not None:
                        active_turn_id = active.turn_id
                        interrupt = getattr(route, "interrupt_presentation", None)
                        if callable(interrupt):
                            await interrupt(active_turn_id)
                        else:
                            await route.cancel_turn(active_turn_id)
                except Exception as exc:  # noqa: BLE001 - finish every boundary
                    first_error = exc
                if active_turn_id is not None:
                    try:
                        await route.settle_usage(active_turn_id, status="session_closed")
                    except Exception as exc:  # noqa: BLE001 - continue teardown
                        first_error = first_error or exc
                turn_task = self._turn_task
                if turn_task is not None and turn_task is not asyncio.current_task():
                    turn_task.cancel()
                    try:
                        await turn_task
                    except asyncio.CancelledError:
                        pass
                    except Exception as exc:
                        first_error = first_error or exc
                try:
                    await route.close()
                except Exception as exc:  # noqa: BLE001 - continue teardown
                    first_error = first_error or exc
            meter = self.usage_meter
            if meter is not None:
                try:
                    meter.finish_session()
                except Exception as exc:
                    first_error = first_error or exc
            if self._roster is not None:
                try:
                    self._roster.close()
                except Exception as exc:
                    first_error = first_error or exc
            self.close()
            if first_error is not None:
                raise first_error

    def _transition(self, target: SessionState) -> None:
        if target not in _ALLOWED[self._state]:
            raise SessionLifecycleError(
                f"illegal session transition {self._state.value} -> {target.value}"
            )
        self._state = target
        self.emit(session_state(self.session_id, target))

    # -- kernel handoff -----------------------------------------------------------

    @property
    def route(self) -> Any:
        """Configured conversation route.

        Transports submit turns, bind output, and cancel through the session.
        """
        return self._route

    @property
    def config(self) -> Any:
        return getattr(self._built, "config", None)

    @property
    def policy(self) -> Any:
        return getattr(self._built, "policy", None)

    @property
    def transport(self) -> Any:
        return getattr(self._built, "transport", None)

    @property
    def agent(self) -> Any:
        return getattr(self._built, "agent", None)

    @property
    def stt(self) -> Any:
        return getattr(self._built, "stt", None)

    @property
    def streaming_stt(self) -> Any:
        return getattr(self._built, "streaming_stt", None)

    @property
    def tts(self) -> Any:
        return getattr(self._built, "tts", None)

    @property
    def tts_fallbacks(self) -> list[Any]:
        return list(getattr(self._built, "tts_fallbacks", ()))

    def current_tts_speed(self) -> float:
        spec = getattr(self.tts, "spec", None)
        if spec is not None:
            return snap_speed(spec.speed)
        reader = getattr(self._route, "current_tts_speed", None)
        return snap_speed(reader()) if callable(reader) else 1.0

    def cycle_tts_speed(self) -> float:
        adapters = self._speed_adapters()
        if adapters:
            return self._apply_tts_speed(adapters, next_speed(adapters[0].spec.speed))
        cycle = getattr(self._route, "cycle_tts_speed", None)
        return snap_speed(cycle()) if callable(cycle) else 1.0

    def set_tts_speed(self, value: float | None) -> float:
        """Set an absolute step. A menu picks a value; a hotkey cycles.

        Returns the step actually applied, which is the caller's value snapped
        to SPEED_STEPS — never the raw request.
        """
        adapters = self._speed_adapters()
        if adapters:
            return self._apply_tts_speed(adapters, snap_speed(value))
        setter = getattr(self._route, "set_tts_speed", None)
        return snap_speed(setter(value)) if callable(setter) else 1.0

    def current_tts_voice(self) -> tuple[str, str]:
        """``(provider, voice_id)`` of the live voice; Deepgram's model is its voice."""
        spec = getattr(self.tts, "spec", None)
        if spec is None:
            return "", ""
        provider = str(getattr(spec, "provider", "") or "")
        voice = str(getattr(spec, "voice_id", "") or "")
        if provider == "deepgram" and not voice:
            voice = str(getattr(spec, "model", "") or "")
        return provider, voice

    def current_tts_language(self) -> str:
        """The language the live voice speaks (the profile's tts.language)."""
        return str(getattr(getattr(self.tts, "spec", None), "language", "") or "")

    async def set_tts_voice(self, voice_id: str) -> str:
        """Switch the live voice after proving it synthesizes.

        The primary adapter tries the new voice on one sample line; only on
        success do the primary and its fallbacks switch, so a bad ID never
        silences a call. Raises ``VoiceUnavailableError`` otherwise.
        """
        from sleeptalk.setup_core import sample_line

        voice = voice_id.strip()
        adapters = self._speed_adapters()
        if not adapters or not VOICE_ID_PATTERN.fullmatch(voice):
            raise VoiceUnavailableError("voice_unavailable")
        primary = adapters[0]
        if getattr(primary.spec, "provider", "") == "local" and not callable(getattr(primary, "set_voice", None)):
            # A local synthesizer that can't switch voices would keep its
            # start-up voice; refuse rather than save an unchecked ID.
            raise VoiceUnavailableError("voice_unsupported")
        previous = [adapter.spec for adapter in adapters]
        try:
            _switch_adapter_voice(primary, voice)
            sample = await asyncio.wait_for(primary.synthesize_wav(sample_line(getattr(self.policy, "language", "") or "en-US")), 45)
            Path(sample).unlink(missing_ok=True)
        except Exception as exc:
            # Put the old voice back through the adapter too, so state it
            # derives from the voice (a local model and speaker) follows.
            with contextlib.suppress(Exception):
                if callable(getattr(primary, "set_voice", None)) and previous[0].voice_id:
                    primary.set_voice(previous[0].voice_id)
            primary.spec = previous[0]
            if getattr(primary, "_detach_socket", None):
                primary._detach_socket()
            raise VoiceUnavailableError(_voice_failure_code(exc)) from exc
        for adapter in adapters[1:]:
            _switch_adapter_voice(adapter, voice)
        return voice

    def set_narration_style(self, value: object) -> str:
        """Apply the browser or terminal narration choice to the live policy."""
        applied = narration_style_or_default(value)
        self.policy.narration_style = applied
        return applied

    def set_reply_length(self, value: object) -> str:
        """Apply the browser or terminal response-length choice to the live policy."""
        applied = reply_length_or_default(value)
        self.policy.reply_length = applied
        return applied

    def _speed_adapters(self) -> list[Any]:  # also the voice adapters
        return [
            adapter
            for adapter in (self.tts, *self.tts_fallbacks)
            if adapter is not None and getattr(adapter, "spec", None) is not None
        ]

    def _apply_tts_speed(self, adapters: list[Any], step: float) -> float:
        # Fallbacks move with the primary: a mid-call failover must not also
        # change how fast the agent talks.
        for adapter in adapters:
            adapter.spec = replace(adapter.spec, speed=step)
        return step

    @property
    def usage_meter(self) -> Any:
        return getattr(self._built, "usage_meter", None)

    async def run(self) -> None:
        """Run the configured transport with this exact canonical session."""
        if self.transport is None:
            raise SessionLifecycleError("session has no configured transport")
        if self.continuous_input() and self.config.transport.type != "web":
            raise SessionLifecycleError("GPT-Live currently supports browser calls; use sleeptalk link")
        self.connect()
        try:
            await self.transport.run(self)
        finally:
            await self.aclose()

    async def prepare(self) -> None:
        """Prepare the route once under the session lifecycle boundary.

        A failure is remembered for a cooldown rather than retried on the next
        call. append_audio prepares lazily, so a route whose warmup failed was
        re-preparing on every captured frame — fifty times a second — which
        turned one slow agent warmup into "Agent is already processing" and
        then a wall of timeouts. Retrying is still allowed, just not at the
        rate audio arrives.
        """
        if self._resources_closed or self._state in (SessionState.CLOSING, SessionState.CLOSED):
            raise SessionLifecycleError("prepare after close")
        if self._prepared:
            return
        async with self._prepare_lock:
            if self._resources_closed:
                raise SessionLifecycleError("prepare after close")
            if self._prepared:
                return
            failure = self._prepare_failure
            if failure is not None:
                if time.monotonic() - self._prepare_failed_at < _PREPARE_RETRY_COOLDOWN_S:
                    raise failure
                self._prepare_failure = None
            self.connect()
            route = self._route
            if route is None:
                raise SessionLifecycleError("session has no configured route")
            bind = getattr(route, "bind", None)
            if callable(bind):
                bind(self.session_id, self.emit)
            self.attach_roster()
            self._attach_jobs()
            self._prepare_task = asyncio.current_task()
            try:
                await route.prepare(self)
            except BaseException as exc:
                self._prepare_failure = exc
                self._prepare_failed_at = time.monotonic()
                raise
            finally:
                self._prepare_task = None
            self._prepared = True

    def set_audio_output(self, output: Any) -> None:
        """Bind a transport-owned playout boundary without exposing route use."""
        route = self._route
        if route is None:
            raise SessionLifecycleError("session has no configured route")
        setter = getattr(route, "set_audio_output", None)
        if not callable(setter):
            raise SessionLifecycleError("configured route does not accept audio output")
        setter(output)
        self._audio_output = output

    async def run_turn(
        self,
        route_input: Any = None,
        *,
        text: str = "",
        pcm: bytes = b"",
        output: Any = None,
        extras: dict[str, Any] | None = None,
        turn_id: int | None = None,
        lifecycle: TurnLifecycle | None = None,
    ) -> Any:
        """Run one committed turn, serializing identity and transport presentation."""
        from sleeptalk.routes.base import RouteInput

        if route_input is None:
            route_input = RouteInput(text=text, pcm=pcm)
        elif text or pcm:
            raise ValueError("pass route_input or text/pcm, not both")
        if not isinstance(route_input, RouteInput):
            raise TypeError("route_input must be a RouteInput")
        return await self._run_turn(
            route_input, output=output, extras=dict(extras or {}),
            turn_id=turn_id, lifecycle=lifecycle,
        )

    async def _run_turn(
        self, route_input: Any, *, output: Any, extras: dict[str, Any],
        turn_id: int | None, lifecycle: TurnLifecycle | None,
        continuation_generation: int | None = None,
    ) -> Any:
        from sleeptalk.routes.base import RouteInput, TurnContext

        await self.prepare()
        async with self._turn_lock:
            async with self._interruption_lock:
                pass
            if self._resources_closed or self._state is not SessionState.ACTIVE:
                raise SessionLifecycleError("run_turn after close")
            if continuation_generation is not None:
                if continuation_generation != self._continuation_generation:
                    return None
            if turn_id is None:
                turn_id = self._turn_id + 1
            elif turn_id <= self._turn_id:
                raise SessionLifecycleError("turn_id must increase monotonically")
            self._turn_id = turn_id
            if continuation_generation is None:
                self._continuation_generation += 1
            generation = self._continuation_generation
            if output is not None:
                self.set_audio_output(output)
            else:
                output = self._audio_output
            turn_extras = dict(extras)
            turn_extras.setdefault("roster", self.attach_roster())
            turn_extras.setdefault("worker_voice", self.worker_voice)
            if self._jobs is not None:
                turn_extras.setdefault("jobs", self._jobs)

            async def followup(text: str, followup_extras: dict[str, Any]) -> Any:
                if generation != self._continuation_generation or self._resources_closed:
                    return None
                return await self._run_turn(
                    RouteInput(text=text), output=output,
                    extras={**extras, **followup_extras}, turn_id=None,
                    lifecycle=lifecycle, continuation_generation=generation,
                )

            ctx = TurnContext(
                session_id=self.session_id, turn_id=turn_id, emit=self.emit,
                extras=turn_extras, schedule_followup=followup,
            )
            self._turn_task = asyncio.current_task()
            outcome = None
            try:
                if lifecycle is not None and lifecycle.started is not None:
                    lifecycle.started(turn_id)
                outcome = await self._route.run_turn(route_input, ctx)
                return outcome
            finally:
                try:
                    if not getattr(outcome, "pending_task_id", None):
                        finish = getattr(output, "finish_turn", None)
                        if callable(finish):
                            await finish(turn_id)
                finally:
                    try:
                        if lifecycle is not None and lifecycle.finished is not None:
                            await lifecycle.finished(turn_id, outcome)
                    finally:
                        self._turn_task = None
                        self._last_turn_end = time.monotonic()

    async def transcribe(self, pcm: bytes, *, participant_id: int = 0) -> Any:
        """Finalize capture through the configured route's transcription policy."""
        await self.prepare()
        transcribe = getattr(self._route, "transcribe", None)
        if not callable(transcribe):
            raise SessionLifecycleError("configured route does not support transcription")
        return await transcribe(pcm, participant_id=participant_id)

    async def answer_pending(self, text: str) -> bool:
        """Answer the agent's waiting permission question with the caller's words.

        A surface hearing speech while the agent works asks this first: a
        "yes" or "no" then resolves the question instead of interrupting the
        work. False on routes without one (Realtime asks its own way).
        """
        answer = getattr(self._route, "answer_pending", None)
        return bool(await answer(text)) if callable(answer) else False

    async def answer_status(self, text: str) -> bool:
        """Answer "what are you doing?" asked while the agent works, without
        interrupting it. False on routes without one."""
        answer = getattr(self._route, "answer_status", None)
        return bool(await answer(text)) if callable(answer) else False

    async def reset_capture(self) -> None:
        """Release a disconnected surface's capture state for the next call."""
        self._continuation_generation += 1
        reset = getattr(self._route, "reset_capture", None)
        if callable(reset):
            await reset()

    def continuous_input(self) -> bool:
        """Whether the provider consumes audio continuously, including playback."""
        return bool(getattr(getattr(self._route, "capabilities", None), "continuous_input", False))

    async def append_audio(self, pcm: bytes, *, participant_id: int = 0) -> None:
        """Feed live PCM to the canonical route's streaming-STT ingress."""
        await self.prepare()
        append = getattr(self._route, "append_audio", None)
        if not callable(append):
            raise SessionLifecycleError("configured route does not accept streaming audio")
        await append(pcm, participant_id=participant_id)

    async def stream_ahead(self, generation: object, pcm: bytes) -> None:
        """Hand the growing utterance to a route that listens as it arrives."""
        stream = getattr(self._route, "stream_ahead", None)
        if callable(stream):
            await stream(generation, pcm)

    def streams_ahead(self) -> bool:
        return callable(getattr(self._route, "stream_ahead", None))

    async def keep_warm(self) -> None:
        """Reopen speech connections that idled shut, when the route has them."""
        warm = getattr(self._route, "keep_warm", None)
        if callable(warm):
            await warm()

    def listens_ahead(self) -> bool:
        """Whether a pause brings an early transcript (the route's batch listener)."""
        listens = getattr(self._route, "listens_ahead", None)
        return bool(listens()) if callable(listens) else False

    def transcribe_ahead(self, pcm: bytes, *, participant_id: int = 0) -> Any:
        """Start an early transcription at a pause, when the route has one; its task or None."""
        start = getattr(self._route, "transcribe_ahead", None)
        return start(pcm, participant_id=participant_id) if callable(start) else None

    def input_transcript_preview(self, participant_id: int = 0) -> str:
        """Revisable captions that cannot start or end an agent turn."""
        preview = getattr(self._route, "input_transcript_preview", None)
        return preview(participant_id) if callable(preview) else ""

    def turn_completion_signal(self, participant_id: int = 0) -> Any:
        """Return live endpointing evidence without exposing the STT adapter."""
        route = self._route
        signal = getattr(route, "turn_completion_signal", None)
        if not callable(signal):
            return None
        return signal(participant_id)

    def active_turn(self) -> Any:
        """Return the route's current presentation turn, if any."""
        route = self._route
        if route is None:
            return None
        return route.active_turn()

    @property
    def active_task_id(self) -> Any:
        """Return the active specialist task without exposing route internals."""
        return getattr(self._route, "active_task_id", None)

    async def interrupt_presentation(
        self,
        turn_id: int | None = None,
        *,
        source: InterruptionSource = InterruptionSource.USER_SPEECH,
    ) -> None:
        """Stop current speech without implicitly aborting specialist work."""
        self._continuation_generation += 1
        if self._resources_closed:
            return
        route = self._route
        if route is None:
            return
        if turn_id is None:
            active = route.active_turn()
            if active is None:
                return
            turn_id = active.turn_id
        async with self._interruption_lock:
            if self._resources_closed:
                return
            interrupt = getattr(route, "interrupt_presentation", None)
            if callable(interrupt):
                # Routes that predate the reason keep their single-argument form.
                try:
                    await interrupt(turn_id, source=source)
                except TypeError:
                    await interrupt(turn_id)
            else:
                await route.cancel_turn(turn_id)
            settle = getattr(route, "settle_usage", None)
            if (
                self.route_type is not None
                and self.route_type.value == "cascade"
                and callable(settle)
            ):
                await settle(turn_id, status="cancelled")

    async def cancel_task(self, task_id: Any) -> bool:
        """Explicitly abort one specialist task; idle/repeated calls are safe."""
        route = self._route
        cancel_task = getattr(route, "cancel_task", None)
        if route is None or not callable(cancel_task):
            return False
        self._continuation_generation += 1
        value = getattr(task_id, "value", task_id)
        return bool(await cancel_task(str(value)))

    async def cancel(self, turn_id: int | None = None) -> None:
        """Compatibility alias for presentation interruption."""
        await self.interrupt_presentation(turn_id)

    async def recover(self) -> Any:
        """Recover the route while preserving session identity."""
        if self._state is not SessionState.ACTIVE:
            raise SessionLifecycleError("recover requires an active session")
        self._transition(SessionState.RECOVERING)
        result = await self._route.recover()
        from sleeptalk.routes.base import RecoveryOutcome

        if result.outcome is RecoveryOutcome.FAILED:
            self._transition(SessionState.CLOSED)
        else:
            self._transition(SessionState.ACTIVE)
        return result

    def summary(self) -> dict[str, Any]:
        """Redacted session summary for logs/QA — identity and state only."""
        return {
            "sessionId": self.session_id,
            "state": self._state.value,
            "routeType": getattr(self.route_type, "value", None),
            "eventCount": len(self._events),
            "eventTypes": sorted({e.type.value for e in self._events}),
        }
