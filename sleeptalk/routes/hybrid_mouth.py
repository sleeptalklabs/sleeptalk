from __future__ import annotations

import asyncio
import enum
import time
import zlib
from collections.abc import AsyncIterator, Awaitable
from dataclasses import dataclass
from typing import Any, Literal, Protocol, TypeAlias

from sleeptalk.policy import narration_style_or_default
from sleeptalk.routes.hybrid_types import (
    DeliveryPhase,
    GroundedBrief,
    LocalOwnershipReason,
    PresentationEpoch,
    SpecialistOwnershipReason,
    TaskId,
    TaskPhase,
)
from sleeptalk.session_events import (
    SessionEventType,
    progress,
    response,
)
from sleeptalk.sounds import narration_clip_path, play_clip_on
from sleeptalk.text import brief_finding, unwrap_script_transcript

STATUS_INTERVAL_S = 8.0

CHECK_PREAMBLES = (
    "I'll check that now.",
    "Let me check that.",
    "I'm checking that now.",
    "I'll look that up.",
    "Let me look that up.",
    "I'll pull that up.",
)


def check_preamble(task_id: TaskId) -> str:
    digest = zlib.crc32(task_id.value.encode())
    return CHECK_PREAMBLES[digest % len(CHECK_PREAMBLES)]


class MouthHost(Protocol):
    _floor_context: Any
    _task_contexts: dict[TaskId, Any]
    _closed: bool
    _presentation_lock: asyncio.Lock

    @property
    def active_task_id(self) -> TaskId | None: ...

    def _epoch_is_current(self, epoch: PresentationEpoch) -> bool: ...

    def _presentation_forbidden(self, phase: TaskPhase) -> bool: ...

    def _set_presenting(self, turn_id: int) -> None: ...

    def _clear_presenting(self, turn_id: int) -> None: ...

    async def _finish_audio_output(self, turn_id: int) -> None: ...

    def _presentation_complete(self) -> bool: ...

    def _emit_failure(self, ctx: Any, failure: Any, **fields: Any) -> None: ...

    def _record_delivery(
        self, ctx: Any, text: str, *, speech_kind: str,
        generated_bytes: int = 0, interrupted: bool = False,
        task_id: TaskId | None = None, source_turn_id: int | None = None,
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class Script:
    text: str
    reason: LocalOwnershipReason | Literal["ack", "needs_input", "realtime_text"]
    task_id: TaskId | None = None

    def __post_init__(self) -> None:
        cleaned = " ".join(self.text.strip().split())
        if not cleaned:
            raise ValueError("script text must not be blank")
        if "\n" in self.text or "\r" in self.text:
            raise ValueError("script text must be one line")
        object.__setattr__(self, "text", cleaned)

    @classmethod
    def ack(cls, reason: SpecialistOwnershipReason, task_id: TaskId) -> Script:
        if reason is SpecialistOwnershipReason.ASSISTANT_MEMORY:
            text = "I'll check what the connected agent remembers about you."
        elif reason is SpecialistOwnershipReason.ASSISTANT_CAPABILITY:
            text = "I'll check what the connected agent can do."
        elif reason is SpecialistOwnershipReason.ASSISTANT_PRIVATE_STATE:
            text = "I'll check what the connected agent can access."
        else:
            text = check_preamble(task_id)
        return cls(text, "ack", task_id)


class FindingKind(enum.Enum):
    FACT = "fact"
    NEEDS_INPUT = "needs_input"


@dataclass(frozen=True, slots=True)
class Finding:
    task_id: TaskId
    sequence: int
    kind: FindingKind
    body: str
    source_turn_id: int

    @classmethod
    def from_agent_yield(
        cls, task_id: TaskId, raw: object, source_turn_id: int
    ) -> Finding | None:
        body = brief_finding(str(raw))
        if not body:
            return None
        return cls(task_id, 0, FindingKind.FACT, body, source_turn_id)

    @classmethod
    def needs_input(
        cls, task_id: TaskId, prompt: str, source_turn_id: int
    ) -> Finding:
        body = " ".join(prompt.strip().split())
        if not body:
            raise ValueError("needs-input finding must not be blank")
        return cls(task_id, 0, FindingKind.NEEDS_INPUT, body, source_turn_id)


@dataclass(frozen=True, slots=True)
class BriefSnapshot:
    task_id: TaskId
    findings: tuple[Finding, ...]
    told_through_before: int
    user_ask: str = ""

    def __post_init__(self) -> None:
        if not self.findings:
            raise ValueError("brief snapshot must contain findings")

    def as_grounded(self) -> GroundedBrief:
        return GroundedBrief(
            self.task_id,
            self.user_ask,
            tuple(item.body for item in self.findings if item.body.strip()),
        )


MouthMaterial: TypeAlias = Script | BriefSnapshot


class SpecialistBrief:
    def __init__(self, task_id: TaskId, user_ask: str = "") -> None:
        self.task_id = task_id
        self.user_ask = user_ask
        self._findings: list[Finding] = []
        self._told_through = 0

    def record(self, finding: Finding) -> Finding:
        if finding.task_id != self.task_id:
            raise ValueError("finding task does not match brief")
        for existing in self._findings:
            if (
                existing.source_turn_id == finding.source_turn_id
                and existing.kind is finding.kind
                and existing.body == finding.body
            ):
                return existing
        sequenced = Finding(
            finding.task_id,
            len(self._findings) + 1,
            finding.kind,
            finding.body,
            finding.source_turn_id,
        )
        self._findings.append(sequenced)
        return sequenced

    def untold(self) -> BriefSnapshot | None:
        leftover = tuple(self._findings[self._told_through :])
        if not leftover:
            return None
        return BriefSnapshot(self.task_id, leftover, self._told_through, self.user_ask)

    def fact_bodies(self) -> tuple[str, ...]:
        return tuple(
            item.body for item in self._findings if item.kind is FindingKind.FACT
        )

    def mark_told(self, snapshot: BriefSnapshot) -> None:
        if snapshot.task_id != self.task_id:
            return
        if snapshot.told_through_before != self._told_through:
            return
        self._told_through = snapshot.told_through_before + len(snapshot.findings)

    def withhold(self) -> int:
        """Mark pending findings as spoken-for without discarding history.

        A stopped answer must not be replayed. Clearing outright would also
        erase what was already told, which `repeat` still needs.
        """
        pending = len(self._findings) - self._told_through
        self._told_through = len(self._findings)
        return pending

    def drop(self) -> None:
        self._findings.clear()
        self._told_through = 0


def material_spoken_text(material: MouthMaterial) -> str:
    if isinstance(material, Script):
        return material.text
    return " ".join(item.body for item in material.findings)


class HybridMouth:
    def __init__(
        self,
        realtime: Any,
        reducer: Any,
        host: MouthHost,
        audio_output: Any | None = None,
        narration_style: object = "narrated",
    ) -> None:
        self._realtime = realtime
        self._reducer = reducer
        self._host = host
        self._audio_output = audio_output
        self._narration_style = narration_style
        self._briefs: dict[TaskId, SpecialistBrief] = {}
        #: Tasks the user talked over, pending what they say next.
        self._barged: set[TaskId] = set()
        #: Tasks a later request superseded. Their answer is no longer
        #: volunteered; "repeat that" still reaches it.
        self._held: set[TaskId] = set()
        self._paused = False
        self._jobs: set[asyncio.Task[Any]] = set()
        self._last_told: BriefSnapshot | None = None
        self._last_status_at = 0.0
        self._last_spoke_at = 0.0

    def set_audio_output(self, output: Any) -> None:
        self._audio_output = output

    def brief(self, task_id: TaskId, user_ask: str = "") -> SpecialistBrief:
        found = self._briefs.get(task_id)
        if found is None:
            found = SpecialistBrief(task_id, user_ask)
            self._briefs[task_id] = found
        elif user_ask:
            found.user_ask = user_ask
        return found

    def grounded(self, task_id: TaskId) -> GroundedBrief | None:
        held = self._briefs.get(task_id)
        if held is None:
            return None
        findings = held.fact_bodies()
        if not findings:
            return None
        return GroundedBrief(task_id, held.user_ask, findings)

    async def speak(self, script: Script, ctx: Any, epoch: PresentationEpoch) -> bool:
        return await self._tell(script, ctx, epoch, foreground=True)

    def note(self, finding: Finding | None) -> None:
        if finding is None or self._host._closed:
            return
        record = self._reducer.task(finding.task_id)
        if record is not None and record.delivery is DeliveryPhase.SUPPRESSED:
            self.brief(finding.task_id).record(finding)
            return
        if finding.kind is FindingKind.NEEDS_INPUT:
            self._schedule_script(Script(finding.body, "needs_input", finding.task_id))
            return
        self.brief(finding.task_id).record(finding)

    def answer_pending(self) -> bool:
        """True when a brief holds findings that have not been spoken yet.

        The specialist streams into the brief rather than out loud, so the
        mouth is silent while it works. Once findings exist the answer is
        imminent, and a reassurance lands on top of it.
        """
        return any(brief.untold() is not None for brief in self._briefs.values())

    def quiet_for(self) -> float:
        """Seconds since anything was last sent to the voice."""
        if not self._last_spoke_at:
            return float("inf")
        return time.monotonic() - self._last_spoke_at

    def _background_allowed(self, task_id: TaskId) -> bool:
        record = self._reducer.task(task_id)
        return (record is not None and record.delivery is not DeliveryPhase.SUPPRESSED
                and record.phase not in {TaskPhase.COMPLETED, TaskPhase.FAILED, TaskPhase.CANCELLED})

    def reassure(self, line: str, *, task_id: TaskId) -> None:
        """A timer-driven "still working" line, outside any tool event."""
        if self._host._closed or self._paused or not self._background_allowed(task_id) or not line.strip():
            return
        self._last_spoke_at = time.monotonic()
        self._schedule_script(Script(line, LocalOwnershipReason.TASK_STATUS, task_id))

    def status(self, tool: str, detail: str = "", *, task_id: TaskId) -> None:
        """Narrate a tool start without reading the invocation out loud."""
        from sleeptalk.voice_tool_narration import tool_status_line

        if not self._background_allowed(task_id):
            return
        value = (
            self._narration_style()
            if callable(self._narration_style)
            else self._narration_style
        )
        style = narration_style_or_default(value)
        if style == "silence":
            return
        spoken = tool_status_line(tool, detail)
        if not spoken:
            return
        now = time.monotonic()
        if self._last_status_at and now - self._last_status_at < STATUS_INTERVAL_S:
            return
        self._last_status_at = now
        self._last_spoke_at = now
        if style == "keyboard":
            self._schedule_keyboard(tool, task_id)
            return
        if spoken[0].islower():
            spoken = spoken[0].upper() + spoken[1:]
        self._schedule_script(Script(spoken, LocalOwnershipReason.TASK_STATUS, task_id))

    def _schedule_keyboard(self, tool: str, task_id: TaskId) -> None:
        path = narration_clip_path("keyboard")
        play_file = getattr(self._audio_output, "play_file", None)
        ctx = self._host._floor_context
        if path is None or not callable(play_file) or ctx is None:
            return
        task = asyncio.create_task(
            self._play_keyboard(play_file, path, tool, task_id, ctx),
            name="hybrid-mouth-keyboard",
        )
        self._jobs.add(task)
        task.add_done_callback(self._jobs.discard)

    async def _play_keyboard(
        self, play_file: Any, path: Any, tool: str, task_id: TaskId, ctx: Any
    ) -> None:
        async with self._host._presentation_lock:
            if not self._background_allowed(task_id):
                return
            if not await play_clip_on(play_file, path, ctx.turn_id):
                return
            self._last_spoke_at = time.monotonic()
            ctx.emit(
                progress(
                    ctx.session_id,
                    ctx.turn_id,
                    pool="tool_keyboard",
                    text="",
                    tool=tool,
                )
            )

    def complete_task(self, task_id: TaskId) -> None:
        self._schedule_snapshot(task_id)

    def drop_task(self, task_id: TaskId) -> None:
        self._held.discard(task_id)
        self._barged.discard(task_id)
        brief = self._briefs.pop(task_id, None)
        if brief is not None:
            brief.drop()
        if self._last_told is not None and self._last_told.task_id == task_id:
            self._last_told = None

    def pause(self, *, withhold: bool = False) -> int:
        """Stop speaking, and stop volunteering what was not said yet.

        Suspending alone means every later run_turn's `resume` replays
        whatever is pending, so the abandoned answer arrives in the middle
        of the next question. Measured on web, Discord and terminal alike:
        talk over "I'll look that up", ask something else, and the first
        question gets answered on top of the second one.

        Being talked over says the user moved on, so neither kind of stop
        volunteers the answer again. They differ in what "repeat that" can
        still reach: a barge-in leaves the findings retrievable, a
        deliberate stop gives them up.
        """
        self._paused = True
        if not withhold:
            # Provisional: what the barge meant depends on what is said
            # next. Holding here outright silenced a task the user had only
            # just asked for — they heard "I'll look that up" and then
            # nothing, because on speakers the acknowledgement's own audio
            # trips the barge detector. `supersede` decides.
            self._barged.update(self._briefs)
            return sum(
                1 for brief in self._briefs.values() if brief.untold() is not None
            )
        for task_id in self._briefs:
            self._reducer.set_delivery(task_id, DeliveryPhase.SUPPRESSED)
        return sum(brief.withhold() for brief in self._briefs.values())

    def supersede(self) -> int:
        """A new request took the floor, so the barge meant "I've moved on".

        Answers talked over and then left behind stop being volunteered.
        A barge nobody followed up on meant only "stop talking": that task
        keeps its answer and delivers it when it is ready.
        """
        superseded = self._barged - self._held
        self._held.update(self._barged)
        self._barged.clear()
        return len(superseded)

    def keep_barged(self) -> None:
        """The user asked for the answer instead of moving past it."""
        self._barged.clear()

    def _owed(self, material: MouthMaterial) -> bool:
        """Is this an answer the user is still waiting on?

        `pause` mutes the mouth until the next turn's `resume`, on the
        assumption that a barge is someone about to speak. A barge nobody
        follows up breaks it — and on speakers the assistant's own
        acknowledgement trips the detector, so the assumption fails on the
        very turn that promised an answer. The user hears "I'll look that
        up" and then nothing, for the rest of the session.

        An answer to a question the user has neither repeated nor replaced
        is still owed, so it is not what the mute was for.
        """
        if not isinstance(material, BriefSnapshot):
            return False
        return material.task_id in self._barged and material.task_id not in self._held

    def _summary_ready(self, task_id: TaskId, *, on_request: bool = False) -> bool:
        """Is this task's answer ready to speak?

        `on_request` is "repeat that": the user is asking for the held
        answer, so suppression — which only means "do not volunteer" —
        stops applying.
        """
        record = self._reducer.task(task_id)
        if record is None or self._host._presentation_forbidden(record.phase):
            return False
        if not on_request and (
            task_id in self._held or record.delivery is DeliveryPhase.SUPPRESSED
        ):
            return False
        return record.phase is TaskPhase.COMPLETED

    async def resume(self, ctx: Any) -> None:
        self._paused = False
        epoch = self._reducer.presentation_epoch
        for task_id, brief in tuple(self._briefs.items()):
            if not self._summary_ready(task_id):
                continue
            snapshot = brief.untold()
            if snapshot is None:
                continue
            wrote = await self._tell(snapshot, ctx, epoch, foreground=False)
            if wrote:
                self._reducer.record(
                    "presentation.resumed",
                    presentation_epoch=epoch,
                    sourceTurnId=snapshot.findings[0].source_turn_id,
                    floorTurnId=ctx.turn_id,
                )

    async def repeat(self, ctx: Any) -> tuple[bool, str]:
        self._paused = False
        epoch = self._reducer.presentation_epoch
        for task_id, brief in tuple(self._briefs.items()):
            # Asked for, so a held answer counts: being talked over stops an
            # answer being volunteered, it does not throw it away.
            if not self._summary_ready(task_id, on_request=True):
                continue
            snapshot = brief.untold()
            if snapshot is None:
                continue
            wrote = await self._tell(
                snapshot, ctx, epoch, foreground=True, replay=True
            )
            return wrote, material_spoken_text(snapshot)
        if self._last_told is not None:
            snapshot = self._last_told
            wrote = await self._tell(
                snapshot, ctx, epoch, foreground=True, replay=True
            )
            return wrote, material_spoken_text(snapshot)
        return False, ""

    async def wait(self) -> None:
        jobs = tuple(self._jobs)
        if jobs:
            await asyncio.gather(*jobs, return_exceptions=True)

    async def close(self) -> None:
        self._paused = True
        for task in tuple(self._jobs):
            if not task.done():
                task.cancel()
        await self.wait()

    def _live(self, material: MouthMaterial, epoch: PresentationEpoch, *, foreground: bool) -> bool:
        host = self._host
        if host._closed or not host._epoch_is_current(epoch):
            return False
        if self._paused and not foreground and not self._owed(material):
            return False
        if not isinstance(material, BriefSnapshot):
            if material.task_id is None:
                return True
            record = self._reducer.task(material.task_id)
            return (record is not None and record.delivery is not DeliveryPhase.SUPPRESSED
                    and record.phase is not TaskPhase.CANCELLED
                    and (foreground or material.reason == "realtime_text"
                         or self._background_allowed(material.task_id)))
        if self._briefs.get(material.task_id) is None:
            return False
        record = self._reducer.task(material.task_id)
        return (record is not None and not host._presentation_forbidden(record.phase)
                and (foreground or (material.task_id not in self._held
                                    and record.delivery is not DeliveryPhase.SUPPRESSED)))

    async def _play_tell(
        self,
        tell: Any,
        material: MouthMaterial,
        ctx: Any,
        epoch: PresentationEpoch,
        *,
        foreground: bool,
        generated: list[str],
    ) -> tuple[bool, bool]:
        wrote = False
        aborted = False
        async for chunk in _items(tell(material, turn_id=ctx.turn_id)):
            if not self._live(material, epoch, foreground=foreground):
                return wrote, True
            if isinstance(chunk, str):
                spoken = unwrap_script_transcript(chunk)
                if spoken:
                    generated.append(spoken)
                continue
            pcm = _pcm(chunk)
            if pcm is None:
                continue
            if self._audio_output is not None:
                await self._audio_output.write(pcm, ctx.turn_id)
                wrote = True
        return wrote, aborted

    def _schedule_script(self, script: Script) -> None:
        ctx = self._host._floor_context
        if ctx is None:
            return
        epoch = self._reducer.presentation_epoch
        task = asyncio.create_task(
            self._tell(script, ctx, epoch, foreground=False),
            name="hybrid-mouth-script",
        )
        self._jobs.add(task)
        task.add_done_callback(self._jobs.discard)

    def _schedule_snapshot(self, task_id: TaskId) -> None:
        ctx = self._host._floor_context
        if ctx is None:
            return
        epoch = self._reducer.presentation_epoch
        task = asyncio.create_task(
            self._tell_task(task_id, ctx, epoch),
            name=f"hybrid-mouth-{task_id.value}",
        )
        self._jobs.add(task)
        task.add_done_callback(self._jobs.discard)

    async def _tell_task(
        self, task_id: TaskId, ctx: Any, epoch: PresentationEpoch
    ) -> None:
        brief = self._briefs.get(task_id)
        if brief is None:
            return
        snapshot = brief.untold()
        if snapshot is None:
            return
        await self._tell(snapshot, ctx, epoch, foreground=False)

    async def _tell(
        self, material: MouthMaterial, ctx: Any, epoch: PresentationEpoch, *,
        foreground: bool, replay: bool = False,
    ) -> bool:
        host = self._host
        if host._closed:
            return False
        if self._paused and not foreground and not self._owed(material):
            return False
        try:
            async with host._presentation_lock:
                is_brief = isinstance(material, BriefSnapshot)
                task_id = material.task_id
                source_turn = host._task_contexts.get(task_id, ctx).turn_id if task_id else ctx.turn_id
                given_up = False
                if is_brief:
                    record = self._reducer.task(task_id)
                    if record is None or host._presentation_forbidden(record.phase):
                        return False
                    given_up = record.delivery is DeliveryPhase.SUPPRESSED
                    if (given_up or task_id in self._held) and not replay:
                        return False
                    if not replay:
                        brief = self._briefs.get(task_id)
                        material = brief.untold() if brief else None
                        if material is None:
                            return False
                    source_turn = material.findings[0].source_turn_id
                    ctx = host._floor_context or ctx
                    epoch = self._reducer.presentation_epoch
                if not self._live(material, epoch, foreground=foreground):
                    return False
                if self._owed(material):
                    self._paused = False
                self._last_spoke_at = time.monotonic()
                host._set_presenting(ctx.turn_id)
                if is_brief:
                    self._reducer.set_delivery(task_id, DeliveryPhase.PRESENTING)
                wrote = False
                aborted = False
                generated: list[str] = []
                _emit_mouth_started(ctx)
                try:
                    wrote, aborted = await self._play_tell(
                        self._realtime.tell, material, ctx, epoch, foreground=foreground,
                        generated=generated,
                    )
                    if not wrote and not aborted and generated and not isinstance(material, Script):
                        fallback = Script(" ".join(generated), "realtime_text", task_id)
                        generated.clear()
                        wrote, aborted = await self._play_tell(
                            self._realtime.tell, fallback,
                            ctx, epoch, foreground=foreground,
                            generated=generated,
                        )
                except BaseException:
                    aborted = True
                    raise
                finally:
                    await host._finish_audio_output(ctx.turn_id)
                    if not aborted and not host._epoch_is_current(epoch):
                        # The stream ran to its end and the output drained it.
                        # A barge-in aimed at the silence afterwards revoked
                        # nothing; calling it an interruption left the answer
                        # untold, and the next turn said all of it again.
                        aborted = not host._presentation_complete()
                    spoken = " ".join(part for part in generated if part.strip())
                    # A script is the requested wording, not proof of delivered text.
                    # Without a provider transcript, report audio only.
                    kind = "answer" if is_brief else (
                        "ack" if material.reason == "ack" else
                        "answer" if material.reason == "realtime_text" else "progress"
                    )
                    delivered = host._record_delivery(
                        ctx, spoken, speech_kind=kind, interrupted=aborted,
                        task_id=task_id, source_turn_id=source_turn,
                    )
                    host._clear_presenting(ctx.turn_id)
                    ctx.emit(response(ctx.session_id, ctx.turn_id,
                        SessionEventType.RESPONSE_CANCELLED if aborted else SessionEventType.RESPONSE_COMPLETED,
                        owner="realtime", delivered=delivered))
                if is_brief:
                    # An answer that was given up and then asked for stays
                    # given up if it did not land. Falling back to PENDING
                    # would put it back in the queue `resume` drains, which
                    # is the replay this is here to stop.
                    self._reducer.set_delivery(task_id, DeliveryPhase.DELIVERED if delivered
                                               else DeliveryPhase.SUPPRESSED if given_up
                                               else DeliveryPhase.PENDING)
                    if delivered:
                        self._held.discard(task_id)
                        self._barged.discard(task_id)
                        brief = self._briefs.get(task_id)
                        if brief is not None:
                            brief.mark_told(material)
                            self._last_told = material
                return delivered
        except Exception as exc:
            from sleeptalk.routes.hybrid import HybridFailure, HybridFailureCode
            host._emit_failure(ctx, HybridFailure(
                HybridFailureCode.REALTIME, str(exc), type(exc).__name__, retryable=True,
            ), retained=(material_spoken_text(material),))
            return False


def _pcm(item: Any) -> bytes | None:
    if isinstance(item, (bytes, bytearray)) and item:
        return bytes(item)
    pcm = getattr(item, "pcm", None)
    if isinstance(pcm, (bytes, bytearray)) and pcm:
        return bytes(pcm)
    return None


async def _items(source: Any) -> AsyncIterator[Any]:
    if inspect_awaitable(source):
        source = await source
    if hasattr(source, "__aiter__"):
        async for item in source:
            yield item
        return
    if isinstance(source, (bytes, bytearray)):
        yield bytes(source)
        return
    if isinstance(source, list | tuple):
        for item in source:
            yield item
        return
    if source is not None:
        yield source


def inspect_awaitable(source: Any) -> bool:
    return isinstance(source, Awaitable) and not hasattr(source, "__aiter__")


def _emit_mouth_started(ctx: Any) -> None:
    emit = getattr(ctx, "emit", None)
    if not callable(emit):
        return
    emit(
        response(
            ctx.session_id,
            ctx.turn_id,
            SessionEventType.RESPONSE_STARTED,
            output_kind="audio",
            owner="realtime",
        )
    )
