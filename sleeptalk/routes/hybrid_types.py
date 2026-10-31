"""Domain types and the deterministic ownership gate for the hybrid route.

The gate is deliberately provider-free.  A committed utterance becomes one
and only one ``OwnedIntent`` before either OpenAI Realtime or the configured
specialist is allowed to answer it.
"""

from __future__ import annotations

import enum
import re
import uuid
from dataclasses import dataclass, field, replace
from typing import Any, Literal, TypeAlias


class SemanticOwner(enum.Enum):
    LOCAL = "local"
    REALTIME = "realtime"
    SPECIALIST = "specialist"


@dataclass(frozen=True, slots=True)
class HybridRoleContract:
    """Public identity shared by the route and its Realtime projection."""

    display_name: str

    def __post_init__(self) -> None:
        raw_display_name = self.display_name.strip()
        if "\n" in raw_display_name or "\r" in raw_display_name:
            raise ValueError("hybrid display name must be one line")
        display_name = " ".join(raw_display_name.split())
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _'-]{0,63}", display_name) is None:
            raise ValueError(
                "hybrid display name must be 1-64 letters, numbers, spaces, apostrophes, "
                "underscores, or hyphens"
            )
        object.__setattr__(self, "display_name", display_name)

    @property
    def realtime_instructions(self) -> str:
        return self.realtime_instructions_for()

    def realtime_instructions_for(self, *, brief: bool = False) -> str:
        length = (
            "those notes are present, answer in at most three short sentences: lead "
            "with what it comes to, leave out the steps, and say they can ask for the "
            "rest."
            if brief
            else "those notes are present, discuss them at conversational length."
        )
        return (
            f"You are the conversational voice of {self.display_name}, one composite "
            "assistant. The connected specialist is the working part. You are the mouth. "
            "Speak as that assistant. Answer only from the conversation visible to you, "
            "stable general knowledge, and any specialist notes you were given. When "
            f"{length} The "
            "specialist owns persistent memory, private "
            "state, tools, skills, current external state, and actions. Never say you "
            "have no memory, tools, or past with the user. Those live with the "
            "specialist. Do not offer to check them. You cannot invoke the connected "
            "specialist yourself. The orchestration layer routes work before you are "
            "asked to respond. Do not invent beyond the specialist notes. Answer "
            "general knowledge, definitions, simple math and everyday explanations "
            "yourself, directly. If you are asked about the user's own things — a "
            "repository, files, commits, projects, tasks, accounts — the current state "
            "of any system, or live information that changes (news, weather, prices), "
            "and you have no specialist notes about it, say you will check rather than "
            "answering from guesswork. A plausible answer you made up is worse than "
            "admitting you need to look. When asked to say a short control line, say "
            "those words."
        )

    @property
    def topology_answer(self) -> str:
        return (
            f"I'm {self.display_name}. I handle quick conversation here, while my "
            "connected specialist handles memory, capabilities, private context, and work."
        )

    def hands_answer(self, catalog: "HandsCatalog") -> str:
        names = catalog.spoken_names()
        if not names:
            offered = "the work you give it"
        elif len(names) == 1:
            offered = names[0]
        elif len(names) == 2:
            offered = f"{names[0]} and {names[1]}"
        else:
            offered = ", ".join(names[:-1]) + f", and {names[-1]}"
        return (
            f"I'm {self.display_name}. My connected specialist can handle {offered}."
        )


@dataclass(frozen=True, slots=True)
class IntentId:
    value: str

    @classmethod
    def mint(cls) -> "IntentId":
        return cls(f"intent-{uuid.uuid4().hex[:12]}")


@dataclass(frozen=True, slots=True)
class TaskId:
    value: str

    @classmethod
    def mint(cls) -> "TaskId":
        return cls(f"task-{uuid.uuid4().hex[:12]}")


@dataclass(frozen=True, slots=True, order=True)
class PresentationEpoch:
    value: int


@dataclass(frozen=True, slots=True)
class CommittedIntent:
    intent_id: IntentId
    turn_id: int
    transcript: str
    presentation_epoch: PresentationEpoch


class LocalOwnershipReason(enum.Enum):
    PENDING_PERMISSION = "permission"
    PENDING_CLARIFICATION = "clarification"
    PRESENCE = "presence"
    REPEAT = "repeat"
    TASK_STATUS = "status"
    INTERRUPT_PRESENTATION = "interrupt_speech"
    CANCEL_TASK = "cancel_task"
    ROUTE_TOPOLOGY = "route_topology"


class RealtimeOwnershipReason(enum.Enum):
    GENERAL_CONVERSATION = "general_conversation"
    # Realtime heard the audio and began answering before the transcript
    # existed; its own ask_specialist call is the routing decision.
    AUDIO_FIRST = "audio_first"


class SpecialistOwnershipReason(enum.Enum):
    ASSISTANT_MEMORY = "assistant_memory"
    ASSISTANT_CAPABILITY = "assistant_capability"
    ASSISTANT_PRIVATE_STATE = "assistant_private_state"
    AUTHORITY = "authority"
    SPECIALIST_FOLLOWUP = "specialist_followup"
    HANDS = "hands"


OwnershipReason: TypeAlias = (
    LocalOwnershipReason | RealtimeOwnershipReason | SpecialistOwnershipReason
)


@dataclass(frozen=True, slots=True)
class LocalOwned:
    intent: CommittedIntent
    reason: LocalOwnershipReason
    owner: Literal[SemanticOwner.LOCAL] = field(
        default=SemanticOwner.LOCAL, init=False
    )


@dataclass(frozen=True, slots=True)
class RealtimeOwned:
    intent: CommittedIntent
    reason: RealtimeOwnershipReason
    owner: Literal[SemanticOwner.REALTIME] = field(
        default=SemanticOwner.REALTIME, init=False
    )


@dataclass(frozen=True, slots=True)
class SpecialistOwned:
    intent: CommittedIntent
    task_id: TaskId
    reason: SpecialistOwnershipReason
    owner: Literal[SemanticOwner.SPECIALIST] = field(
        default=SemanticOwner.SPECIALIST, init=False
    )


OwnedIntent: TypeAlias = LocalOwned | RealtimeOwned | SpecialistOwned


class TaskPhase(enum.Enum):
    ACCEPTED = "accepted"
    RUNNING = "running"
    NEEDS_INPUT = "needs_input"
    READY = "ready"
    PRESENTING = "presenting"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


class DeliveryPhase(enum.Enum):
    PENDING = "pending"
    PRESENTING = "presenting"
    DELIVERED = "delivered"
    SUPPRESSED = "suppressed"


@dataclass(frozen=True, slots=True)
class TaskRecord:
    task_id: TaskId
    origin_intent_id: IntentId
    phase: TaskPhase
    transcript: str
    result: tuple[str, ...] = ()
    agent_session_ref: str | None = None
    error: str | None = None
    delivery: DeliveryPhase = DeliveryPhase.PENDING


@dataclass(frozen=True, slots=True)
class GroundedBrief:
    task_id: TaskId
    user_ask: str
    findings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class OwnershipSnapshot:
    last_owner: SemanticOwner | None = None
    active_task_id: TaskId | None = None
    pending_permission: bool = False
    pending_clarification: bool = False


@dataclass(frozen=True, slots=True)
class HybridEventRecord:
    """One immutable reducer fact, ordered for the whole voice session."""

    sequence: int
    kind: str
    owner: SemanticOwner | None = None
    reason: OwnershipReason | None = None
    intent_id: IntentId | None = None
    task_id: TaskId | None = None
    presentation_epoch: PresentationEpoch | None = None
    payload: tuple[tuple[str, Any], ...] = ()


_TERMINAL_TASK_PHASES = frozenset(
    {TaskPhase.COMPLETED, TaskPhase.CANCELLED, TaskPhase.FAILED}
)


class HybridSessionReducer:
    """Single-writer ownership/task journal that survives provider reconnects."""

    def __init__(self) -> None:
        self._sequence = 0
        self._epoch = PresentationEpoch(0)
        self._events: list[HybridEventRecord] = []
        self._tasks: dict[TaskId, TaskRecord] = {}
        self._last_owner: SemanticOwner | None = None
        self._pending_permission = False
        self._pending_clarification = False

    @property
    def events(self) -> tuple[HybridEventRecord, ...]:
        return tuple(self._events)

    @property
    def presentation_epoch(self) -> PresentationEpoch:
        return self._epoch

    def snapshot(self) -> OwnershipSnapshot:
        active = next(
            (
                task.task_id
                for task in reversed(tuple(self._tasks.values()))
                if task.phase not in _TERMINAL_TASK_PHASES
            ),
            None,
        )
        return OwnershipSnapshot(
            last_owner=self._last_owner,
            active_task_id=active,
            pending_permission=self._pending_permission,
            pending_clarification=self._pending_clarification,
        )

    def commit(self, owned: OwnedIntent) -> HybridEventRecord:
        """Record the one ownership decision before response work starts."""
        self._last_owner = owned.owner
        task_id = owned.task_id if isinstance(owned, SpecialistOwned) else None
        if isinstance(owned, SpecialistOwned):
            self._tasks[task_id] = TaskRecord(
                task_id=task_id,
                origin_intent_id=owned.intent.intent_id,
                phase=TaskPhase.ACCEPTED,
                transcript=owned.intent.transcript,
            )
        return self._append(
            "intent.committed",
            owner=owned.owner,
            reason=owned.reason,
            intent_id=owned.intent.intent_id,
            task_id=task_id,
            presentation_epoch=owned.intent.presentation_epoch,
        )

    def transition_task(
        self,
        task_id: TaskId,
        phase: TaskPhase,
        *,
        result: tuple[str, ...] | None = None,
        agent_session_ref: str | None = None,
        error: str | None = None,
    ) -> TaskRecord:
        current = self._tasks[task_id]
        if current.phase in _TERMINAL_TASK_PHASES:
            if phase is current.phase:
                return current
            raise ValueError(f"terminal task {task_id.value} cannot reopen")
        updated = replace(
            current,
            phase=phase,
            result=current.result if result is None else result,
            agent_session_ref=agent_session_ref or current.agent_session_ref,
            error=error,
        )
        self._tasks[task_id] = updated
        self._append(
            "task.transitioned",
            owner=SemanticOwner.SPECIALIST,
            intent_id=updated.origin_intent_id,
            task_id=task_id,
            presentation_epoch=self._epoch,
            phase=phase.value,
        )
        return updated

    def task(self, task_id: TaskId) -> TaskRecord | None:
        return self._tasks.get(task_id)

    def set_delivery(self, task_id: TaskId, phase: DeliveryPhase) -> None:
        current = self._tasks[task_id]
        if current.delivery is DeliveryPhase.SUPPRESSED:
            return
        self._tasks[task_id] = replace(current, delivery=phase)
        self._append(
            "task.delivery", task_id=task_id,
            intent_id=current.origin_intent_id, phase=phase.value,
        )

    def delegate(self, owned: SpecialistOwned) -> None:
        """Transfer an existing intent without committing a second user turn."""
        self._last_owner = SemanticOwner.SPECIALIST
        self._tasks[owned.task_id] = TaskRecord(
            owned.task_id, owned.intent.intent_id, TaskPhase.ACCEPTED,
            owned.intent.transcript,
        )
        self._append(
            "intent.delegated", intent_id=owned.intent.intent_id,
            task_id=owned.task_id, owner=SemanticOwner.SPECIALIST,
        )

    def set_pending_input(self, *, permission: bool, clarification: bool) -> None:
        self._pending_permission = permission
        self._pending_clarification = clarification

    def revoke_presentation(self) -> PresentationEpoch:
        self._epoch = PresentationEpoch(self._epoch.value + 1)
        self._append("presentation.revoked", presentation_epoch=self._epoch)
        return self._epoch

    def record(
        self,
        kind: str,
        *,
        owner: SemanticOwner | None = None,
        reason: OwnershipReason | None = None,
        intent_id: IntentId | None = None,
        task_id: TaskId | None = None,
        presentation_epoch: PresentationEpoch | None = None,
        **payload: Any,
    ) -> HybridEventRecord:
        """Append a correlated fact without exposing reducer internals."""
        return self._append(
            kind,
            owner=owner,
            reason=reason,
            intent_id=intent_id,
            task_id=task_id,
            presentation_epoch=presentation_epoch,
            **payload,
        )

    def _append(
        self,
        kind: str,
        *,
        owner: SemanticOwner | None = None,
        reason: OwnershipReason | None = None,
        intent_id: IntentId | None = None,
        task_id: TaskId | None = None,
        presentation_epoch: PresentationEpoch | None = None,
        **payload: Any,
    ) -> HybridEventRecord:
        self._sequence += 1
        event = HybridEventRecord(
            sequence=self._sequence,
            kind=kind,
            owner=owner,
            reason=reason,
            intent_id=intent_id,
            task_id=task_id,
            presentation_epoch=presentation_epoch,
            payload=tuple(sorted(payload.items())),
        )
        self._events.append(event)
        return event


_PRESENCE = re.compile(
    r"^(?:(?:hey|hi|hello|uh|um)[,.\s]+)*"
    r"(?:are you there|can you hear me|you there|hello|hi|hey)[?!. ]*$",
    re.IGNORECASE,
)
_REPEAT = re.compile(r"^(?:please\s+)?(?:repeat that|say that again)[?!. ]*$", re.IGNORECASE)
_STATUS = re.compile(
    r"^(?:what(?:'s| is) (?:the )?status|how(?:'s| is) (?:it|the task) going|"
    r"what is still running)[?!. ]*$",
    re.IGNORECASE,
)
_INTERRUPT = re.compile(r"^(?:stop(?: talking)?|be quiet|pause|hold on)[?!. ]*$", re.IGNORECASE)
_CANCEL = re.compile(
    r"^(?:stop|cancel|abort)(?: the| that| this)? "
    r"(?:(?:research|review)(?: (?:task|job|work))?|task|job|work|agent)"
    r"[?!. ]*$"
    r"|^(?:stop|cancel) everything[?!. ]*$",
    re.IGNORECASE,
)
# "Wait, stop. What's the capital of France?": a stop that carries a new
# request. The running task is cancelled and the rest is the request.
_REDIRECT = re.compile(
    r"^(?:(?:wait|okay|ok|no|hey|actually)[,.!]?\s+)?"
    r"(?:stop|cancel(?: that| it)?|never ?mind|forget (?:it|that)|hold on|scratch that)[,.!;]+\s*(?P<rest>\S.*)$",
    re.IGNORECASE,
)


def redirect_request(transcript: str) -> str:
    """The new request after a leading stop ("Wait, stop. <request>"), or ""."""
    match = _REDIRECT.match(" ".join(str(transcript or "").split()))
    if match is None:
        return ""
    rest = match.group("rest").strip()
    return rest if len(rest.split()) >= 2 else ""


_AFFIRM = re.compile(
    r"^(?:yes|yep|yeah|allow|approve|go ahead|continue)(?:[, ]+(?:please|go ahead))?[?!. ]*$",
    re.IGNORECASE,
)
_DENY = re.compile(
    r"^(?:no|nope|deny|decline|do not|don't)(?:[, ]+(?:please|stop))?[?!. ]*$",
    re.IGNORECASE,
)

_ASSISTANT_MEMORY = re.compile(
    r"\b(?:do you have (?:any )?(?:memory|memories)\b|"
    r"what (?:memory|memories) do you have\b|"
    r"(?:can|could|do) you (?:remember|recall) (?:me|my\b|what\b|anything\b|that\b)|"
    r"will you remember (?:me|my\b|that\b|this\b|what\b)|"
    r"have we (?:met|spoken|talked|chatted) before|"
    r"what do you (?:know|remember|recall) about me|"
    r"what do you remember from (?:before|prior|previous sessions?)|"
    r"what did we (?:discuss|talk about) (?:last time|yesterday|before)|"
    r"do you know my\b|"
    r"remember (?:that|this|to)\b)",
    re.IGNORECASE,
)
_ASSISTANT_PRIVATE_STATE = re.compile(
    r"\b(?:can you|could you|do you) (?:access|see|read|check|look at) "
    r"(?:any of )?my (?:notes?|calendars?|email|messages?|documents?|accounts?|"
    r"projects?|profile|history|preferences?|screen|files?|google drive|drive)\b|"
    r"\bwhat (?:accounts?|notes?|calendars?|documents?|projects?|files?) can you access\b|"
    r"\b(?:what do you know about|what are) my "
    r"(?:projects?|profile|history|accounts?|preferences?)\b",
    re.IGNORECASE,
)
_ASSISTANT_CAPABILITY = re.compile(
    r"\b(?:(?:what|which) (?:tools|skills|plugins|integrations|capabilities)\b|"
    r"(?:tell|list|show)(?: me)? (?:your|the) (?:tools|skills|plugins|capabilities)\b|"
    r"do you (?:have|support) (?:any )?(?:tools|skills|plugins|integrations|capabilities)\b|"
    r"what can you do\b|"
    r"what (?:model|provider|runtime|version) are you (?:using|running)\b|"
    r"what services are you connected to\b|"
    r"are you connected to (?:the )?internet\b|"
    r"(?:can|could) you (?:browse|search)(?: the)? web\b|"
    r"(?:can|could) you access (?!my\b).+|"
    r"(?:can|could) you use (?:the )?(?:.+ )?(?:tool|skill|plugin|integration|service|agent)\b|"
    r"are you able to (?:use|access) .+|"
    r"do you have access to .+)",
    re.IGNORECASE,
)
_REALTIME_STYLE_REQUEST = re.compile(
    r"^(?:can|could) you use (?:simpler|different|plain|fewer|more concise) "
    r"(?:words|language)\b|"
    r"^(?:can|could) you use (?:an?|another) (?:analogy|example|metaphor)\b",
    re.IGNORECASE,
)
_ASSISTANT_AMBIGUOUS_USE = re.compile(
    r"\b(?:can|could) you use .+",
    re.IGNORECASE,
)
_ROUTE_TOPOLOGY = re.compile(
    r"^(?:who are you|what is your name|what's your name|whats your name|"
    r"are you one assistant|"
    r"how do you decide which part answers|which part answers|"
    r"which part handles (?:conversation|memory|tools|skills|research|work|tasks|"
    r"private context))"
    r"[?!. ]*$",
    re.IGNORECASE,
)
_ROUTE_TOPOLOGY_COMPOSITE = re.compile(
    r"\b(?:do you have (?:an |a )?(?:agent|specialist)\b|"
    r"(?:have |got )?access to (?:an |a )?(?:agent|specialist)\b|"
    r"are you connected to (?:an |a )?(?:agent|specialist)\b)",
    re.IGNORECASE,
)

_AUTHORITY = re.compile(
    r"\b(?:permission|approve|delegate|background)\b",
    re.IGNORECASE,
)
_PHATIC = re.compile(
    r"^(?:thanks|thank you|cheers|good morning|good night|good evening|"
    r"how are you|how(?:'s| is) it going|you(?:'re| are) welcome|"
    r"tell me a (?:short )?joke|i love you|love you)[?!. ]*$",
    re.IGNORECASE,
)
# Plain imperatives are how people actually ask for something. Without them
# "Give me Bitcoin's live price" fell through to the mouth, which had no tools
# and improvised, while the specialist was only ever asked the *capability*
# question that preceded it. Brief follow-ups are matched before this, so
# discussing an answer still stays with the mouth.
_ASSISTANT_REQUEST = re.compile(
    r"(?:(?:^|\band )(?:check|look|find|get|give|show|pull|fetch|list|run|"
    r"read|count|summari[sz]e|explain|compare|update|build|make|debug|"
    r"research|create|open|search|browse|inspect|write|edit|install|fix|"
    r"use|tell me)\b|"
    r"\b(?:can|could|would) you\b|"
    r"\bplease\b)",
    re.IGNORECASE,
)
# Questions about state the mouth cannot see. "What are the latest commits"
# routed to the mouth, which has no repository and answered with an invented
# commit name — a plausible answer being far worse than "let me check".
# Verb lists cannot enumerate how people ask about the world; this covers the
# common question forms until the mouth can dispatch for itself.
_EXTERNAL_STATE = re.compile(
    r"\b(?:commit|commits|merge|merges|branch|branches|repo|repository|"
    r"pull request|pull requests|pr|prs|diff|file|files|directory|folder|"
    r"test|tests|build|builds|log|logs|error|errors|issue|issues|"
    r"changelog|version|status)\b",
    re.IGNORECASE,
)
_QUESTION_FORM = re.compile(
    r"^(?:what|which|who|where|when|how many|how much|any|is there|are there|"
    r"do we|did we|does it|has|have)\b",
    re.IGNORECASE,
)
# A general-knowledge question: a question or "explain" form with nothing
# of the user's own, nothing live, and no work in it.
_GENERAL_QUESTION = re.compile(
    r"^(?:(?:ok(?:ay)?|so|hey|and|also)[, ]+)*"
    r"(?:what(?:'s| is| are| was| were| does| do)|who(?:'s| is| was| were)|where(?:'s| is| was)|"
    r"when (?:is|was|did)|why (?:is|are|do|does|did)|how (?:does|do|is|are|many|much|far|long|big|old)|"
    r"explain|tell me (?:about|how|why|what|who|where|when)|define|what does .+ mean)\b",
    re.IGNORECASE,
)
_PERSONAL_OR_LIVE = re.compile(
    r"\b(?:my|mine|our|ours|we|us|i|you|your|yours|you're|yourself|this|these|that one|here|"
    r"today|tonight|tomorrow|yesterday|now|right now|currently|current|latest|recent|recently|"
    r"news|weather|price|prices|stock|score|scores|"
    r"search|look up|web|online|internet)\b",
    re.IGNORECASE,
)

# Live facts and the user's own things: questions the voice can't answer.
_LIVE_OR_OWN = re.compile(
    r"\b(?:my|our|today|tonight|tomorrow|yesterday|now|right now|currently|current|latest|"
    r"recent|recently|news|weather|price|prices|stock|score|scores)\b",
    re.IGNORECASE,
)


# "I'd like to know about X" asks the same thing as "tell me about X".
_ASK_LEAD = re.compile(
    r"^(?:(?:ok(?:ay)?|so|hey|and|also|please)[, ]+)*"
    r"(?:i'?d (?:like|love) to (?:know|hear|learn)|i want to (?:know|hear|learn)|i wonder|"
    r"i'?m curious|can you|could you|would you|please)\b[, ]*",
    re.IGNORECASE,
)
# "Don't search the web, just explain it" rules the tools out; it isn't a search.
_NO_TOOLS = re.compile(
    r"\b(?:don'?t|do not|no need to|without|no)\s+"
    r"(?:search(?:ing)?|look(?:ing)?(?: (?:it|that|anything))? up|brows(?:e|ing)|"
    r"go(?:ing)? online|us(?:e|ing) (?:the )?(?:web|internet|tools?))"
    r"(?:\s+(?:on\s+)?(?:the\s+)?(?:web|internet|online))?",
    re.IGNORECASE,
)
# The voice heard the call; repeating it back is not agent work.
_CONVERSATION_RECALL = re.compile(
    r"\b(?:repeat (?:back|after me|that|what i)|say (?:that|it) (?:again|back)|"
    r"what (?:did|was it) i (?:just )?(?:say|said)|the (?:first|last) word i (?:just )?said)\b",
    re.IGNORECASE,
)


def is_general_knowledge(text: str) -> bool:
    """A question the voice answers itself: general, not the user's, not live, no work."""
    cleaned = " ".join(str(text or "").split())
    if not cleaned:
        return False
    if _CONVERSATION_RECALL.search(cleaned) and not _EXTERNAL_STATE.search(cleaned):
        return True
    cleaned = _NO_TOOLS.sub(" ", cleaned)
    asked = _ASK_LEAD.sub("", cleaned, count=1)
    if asked != cleaned and re.match(r"about\b", asked, re.IGNORECASE):
        asked = "tell me " + asked
    if not _GENERAL_QUESTION.match(asked):
        return False
    return not (_PERSONAL_OR_LIVE.search(asked) or _EXTERNAL_STATE.search(asked))


_INVENTORY_LEFTOVER = re.compile(
    r"^(?:(?:do )?you have|do you support|are you|can you use|have you got)?$",
    re.IGNORECASE,
)
_HAND_TOKEN = re.compile(r"[a-z0-9]{3,}")
_SPECIALIST_FOLLOWUP = re.compile(
    r"^(?:(?:ok(?:ay)?|yeah|yep|sure|please|go ahead|uh|um)[, ]+)*"
    r"(?:why|how|what about|and|also|then|can you explain|tell me more|fix it|"
    r"keep going|continue|what happened|summarize|"
    r"do (?:it|that)|"
    r"it didn'?t|that didn'?t|this didn'?t|didn'?t work|try again|"
    r"do (?:it|that) again|you (?:forgot|didn'?t|missed)|open it)\b",
    re.IGNORECASE,
)
_GROUNDED_DISCUSSION = re.compile(
    r"^(?:(?:ok(?:ay)?|yeah|yep|sure|please|go ahead|uh|um)[, ]+)*"
    r"(?:tell me more|what else|that'?s all|can you explain|"
    r"what does that mean|what about(?: that)?|why|how)"
    r"[?!. ]*$",
    re.IGNORECASE,
)
_BRIEF_RESUME = re.compile(
    r"\b(?:"
    r"keep going|continue|"
    r"what happened|what went wrong|"
    r"summarize|summary|recap"
    r")\b",
    re.IGNORECASE,
)


def _hand_tokens(*parts: str) -> frozenset[str]:
    found: set[str] = set()
    for part in parts:
        folded = re.sub(r"[^a-z0-9]+", " ", part.casefold())
        found.update(_HAND_TOKEN.findall(folded))
    return frozenset(found)


@dataclass(frozen=True, slots=True)
class Hand:
    name: str
    tokens: frozenset[str]
    kind: Literal["tool", "skill"]

    @classmethod
    def named(cls, name: str, kind: Literal["tool", "skill"], *extra: str) -> "Hand":
        cleaned = name.strip()
        if not cleaned:
            raise ValueError("hand name must not be blank")
        return cls(cleaned, _hand_tokens(cleaned, *extra), kind)


@dataclass(frozen=True, slots=True)
class HandsCatalog:
    hands: tuple[Hand, ...] = ()
    open: bool = False

    def matching(self, transcript: str) -> tuple[Hand, ...]:
        words = _hand_tokens(transcript)
        if not words:
            return ()
        return tuple(hand for hand in self.hands if hand.tokens & words)

    def spoken_names(self) -> tuple[str, ...]:
        return tuple(hand.name for hand in self.hands)


def hands_catalog_from_agent(agent: Any, *, cwd: str = "") -> HandsCatalog:
    offered = getattr(agent, "hands_catalog", None)
    if isinstance(offered, HandsCatalog):
        return offered
    hands: list[Hand] = []
    names = getattr(agent, "hand_names", ())
    if isinstance(names, (tuple, list, set, frozenset)):
        hands.extend(
            Hand.named(str(name), "tool")
            for name in names
            if str(name).strip()
        )
    root = cwd or str(getattr(agent, "cwd", "") or "")
    if root:
        from sleeptalk.voice_skills import discover_skills

        hands.extend(Hand.named(name, "skill") for name in sorted(discover_skills(root)))
    return HandsCatalog(hands=tuple(hands), open=True)


def _is_pure_capability_inventory(text: str) -> bool:
    match = _ASSISTANT_CAPABILITY.search(text)
    if match is None:
        return False
    leftover = text[match.end() :].strip(" ?!.,;")
    return leftover == "" or _INVENTORY_LEFTOVER.match(leftover) is not None


def is_hands_question(text: str) -> bool:
    if _ROUTE_TOPOLOGY_COMPOSITE.search(text) is not None:
        return True
    return _is_pure_capability_inventory(text)


def assign_owner(
    intent: CommittedIntent,
    snapshot: OwnershipSnapshot = OwnershipSnapshot(),
    catalog: HandsCatalog = HandsCatalog(),
    brief: GroundedBrief | None = None,
) -> OwnedIntent:
    """Assign exactly one semantic owner to a committed utterance.

    Local controls win first. A brief with findings makes discussion Realtime
    before catalog matching. Then this agent's hands. Realtime keeps leftover
    chat that no hand matches.
    """

    text = " ".join(intent.transcript.strip().split())
    if snapshot.pending_permission and (_AFFIRM.match(text) or _DENY.match(text)):
        return LocalOwned(intent, LocalOwnershipReason.PENDING_PERMISSION)
    if _PRESENCE.match(text):
        return LocalOwned(intent, LocalOwnershipReason.PRESENCE)
    if _REPEAT.match(text):
        return LocalOwned(intent, LocalOwnershipReason.REPEAT)
    if _STATUS.match(text):
        return LocalOwned(intent, LocalOwnershipReason.TASK_STATUS)
    if _INTERRUPT.match(text):
        return LocalOwned(intent, LocalOwnershipReason.INTERRUPT_PRESENTATION)
    if _CANCEL.search(text):
        return LocalOwned(intent, LocalOwnershipReason.CANCEL_TASK)
    if _ASSISTANT_MEMORY.search(text):
        return SpecialistOwned(
            intent,
            TaskId.mint(),
            SpecialistOwnershipReason.ASSISTANT_MEMORY,
        )
    if _ASSISTANT_PRIVATE_STATE.search(text):
        return SpecialistOwned(
            intent,
            TaskId.mint(),
            SpecialistOwnershipReason.ASSISTANT_PRIVATE_STATE,
        )
    if _ROUTE_TOPOLOGY.match(text) or _ROUTE_TOPOLOGY_COMPOSITE.search(text):
        return LocalOwned(intent, LocalOwnershipReason.ROUTE_TOPOLOGY)
    if _REALTIME_STYLE_REQUEST.match(text):
        return RealtimeOwned(intent, RealtimeOwnershipReason.GENERAL_CONVERSATION)
    known_hands = bool(catalog.hands) or catalog.open
    capability = _ASSISTANT_CAPABILITY.search(text)
    leftover = "" if capability is None else text[capability.end() :].strip(" ?!.,;")
    leftover_is_inventory = leftover == "" or _INVENTORY_LEFTOVER.match(leftover) is not None
    asking_capability = capability is not None or _ASSISTANT_AMBIGUOUS_USE.search(text) is not None
    asking_work = capability is not None and not leftover_is_inventory
    if known_hands and asking_capability and not asking_work:
        return LocalOwned(intent, LocalOwnershipReason.ROUTE_TOPOLOGY)
    if asking_capability and not asking_work:
        return SpecialistOwned(
            intent,
            TaskId.mint(),
            SpecialistOwnershipReason.ASSISTANT_CAPABILITY,
        )
    if snapshot.pending_clarification and text:
        return LocalOwned(intent, LocalOwnershipReason.PENDING_CLARIFICATION)
    if is_general_knowledge(text):
        # "What's the capital of France?", "Explain how a bicycle works": the
        # voice answers these itself, directly, instead of handing them to
        # the agent (slow, and the agent adds nothing).
        return RealtimeOwned(intent, RealtimeOwnershipReason.GENERAL_CONVERSATION)
    # Open catalog treats "tell me" as work. Brief discussion has to win first.
    if brief is not None and brief.findings and is_brief_followup(text):
        return RealtimeOwned(intent, RealtimeOwnershipReason.GENERAL_CONVERSATION)
    if catalog.matching(text):
        return SpecialistOwned(intent, TaskId.mint(), SpecialistOwnershipReason.HANDS)
    if catalog.open and not _PHATIC.match(text) and _ASSISTANT_REQUEST.search(text):
        return SpecialistOwned(intent, TaskId.mint(), SpecialistOwnershipReason.HANDS)
    if (
        catalog.open
        and not _PHATIC.match(text)
        and _QUESTION_FORM.match(text)
        and (_EXTERNAL_STATE.search(text) or _LIVE_OR_OWN.search(text))
    ):
        return SpecialistOwned(intent, TaskId.mint(), SpecialistOwnershipReason.HANDS)
    if _AUTHORITY.search(text):
        return SpecialistOwned(intent, TaskId.mint(), SpecialistOwnershipReason.AUTHORITY)
    if (
        snapshot.last_owner is SemanticOwner.SPECIALIST
        and _SPECIALIST_FOLLOWUP.match(text)
    ):
        return SpecialistOwned(
            intent,
            TaskId.mint(),
            SpecialistOwnershipReason.SPECIALIST_FOLLOWUP,
        )
    return RealtimeOwned(intent, RealtimeOwnershipReason.GENERAL_CONVERSATION)


def is_grounded_discussion(text: str) -> bool:
    cleaned = " ".join(text.strip().split())
    return bool(cleaned and _GROUNDED_DISCUSSION.match(cleaned))


def is_brief_followup(text: str) -> bool:
    cleaned = " ".join(text.strip().split())
    if not cleaned:
        return False
    return is_grounded_discussion(cleaned) or bool(_BRIEF_RESUME.search(cleaned))


__all__ = [
    "CommittedIntent",
    "GroundedBrief",
    "Hand",
    "HandsCatalog",
    "HybridEventRecord",
    "HybridRoleContract",
    "HybridSessionReducer",
    "IntentId",
    "LocalOwned",
    "LocalOwnershipReason",
    "OwnedIntent",
    "OwnershipReason",
    "OwnershipSnapshot",
    "PresentationEpoch",
    "RealtimeOwned",
    "RealtimeOwnershipReason",
    "SemanticOwner",
    "SpecialistOwned",
    "SpecialistOwnershipReason",
    "TaskId",
    "TaskPhase",
    "TaskRecord",
    "assign_owner",
    "redirect_request",
    "is_general_knowledge",
    "hands_catalog_from_agent",
    "is_brief_followup",
    "is_grounded_discussion",
    "is_hands_question",
]
