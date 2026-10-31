"""Background jobs: long work in a second session of the caller's own agent.

While voice is on, the foreground agent is told (``HANDOFF_INSTRUCTION``) to
hand long work off instead of doing it in the reply: it writes
``<job title="todo app">a self-contained brief</job>`` and one short sentence
to say. ``JobMarkerFilter`` takes the block out of the speech, and
``JobManager`` starts a fresh instance of the same agent with the brief while
the conversation carries on. Each foreground turn is told how the job is going
(``JobManager.context_block``), and the result is announced at a quiet moment.

Up to three run at once (``policy.max_background_jobs``) and the rest
queue; nothing is refused. The caller's words reach a
job by its title, else by the rules in ``JobManager.resolve``. A job ends
with the session; it does not outlive a hangup. See
docs/plans/background-jobs.md and docs/plans/concurrent-background-jobs.md.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import itertools
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sleeptalk.adapter import (
    AgentError,
    AnswerClarification,
    AssistantDelta,
    ClarificationRequested,
    ControlAccepted,
    PermissionDecision,
    PermissionRequested,
    ReasoningDelta,
    ResolvePermission,
    Steer,
    ToolEvent,
    ToolPhase,
    adapter_control,
    agent_signal,
    typed_events,
)

from sleeptalk.text import is_hearable
from sleeptalk.voice_tool_narration import tool_status_line

logger = logging.getLogger("sleeptalk.jobs")

HANDOFF_INSTRUCTION = (
    "Voice call rule: hand bigger work to a background job so the caller can keep talking. "
    "Bigger work is building something new (an app, a page, a script or a feature), even a "
    "simple one; changes across several files; installing or deploying; or research that takes "
    "several searches. To hand it off, do not start the work and do not use your own subagent, "
    "task or delegate tools; Sleeptalk runs the job itself. Write <job title=\"name\">a complete, "
    "self-contained brief for another agent: the goal, every detail the caller gave, where to "
    "work, and what to report back, such as links or file paths</job> and then one short "
    "sentence to say out loud. The name is two or three words, such as todo app. Do everything "
    "else yourself in this reply: questions, explanations, looking something up, reading files, "
    "running a command, and small changes to a file that already exists, such as adding, "
    "removing or fixing a few lines. If the caller asks you to do it right now, here, do it "
    "yourself; if they ask for it in the background, hand it off."
)

JOB_CONTRACT = (
    "You are running as a background job for someone on a voice call. Nobody is watching or "
    "listening while you work, so do not stop to ask questions unless you are truly blocked; "
    "make sensible choices and mention them at the end. When you finish, write a spoken-style "
    "summary of at most two sentences with no markdown, then put each link or file path the "
    "person needs on its own line. Do not make git commits: other jobs may be changing files in the "
    "same folder, so leave your changes in the working tree and name the files you changed in "
    "your summary."
)

STARTED_LINE = "I'll work on that in the background."
# Said once per call, after the first handoff, so the caller knows talking
# won't interrupt the work. Skipped if the agent already said as much.
KEEP_TALKING_LINE = "We can keep talking while it works."
_KEEP_TALKING_RE = re.compile(
    r"\b(?:keep|carry on|go on)\s+(?:talking|chatting)\b"
    r"|\b(?:talk|chat)\s+(?:about anything|while|in the meantime)\b"
    r"|\bin the meantime\b|\bmeanwhile\b"
    r"|\bwhile (?:it|that|the job|the worker)(?:'s| is)? (?:works|working|runs|running|builds|building)\b",
    re.IGNORECASE,
)
UNAVAILABLE_LINE = "I can't start background work on this call."

ACTIVE = frozenset({"starting", "running"})
# Jobs that run at once unless the policy says otherwise; more queue.
DEFAULT_MAX_JOBS = 3
# The queued line counts the waiting jobs from this many on.
_QUEUE_NOTICE = 5
# Finished jobs kept for routing, status and follow-ups (live and queued are always kept).
_FINISHED_KEEP = 5
# A finished job's status stays answerable this long ("is it done?").
_STATUS_WINDOW_S = 600.0
# A note with no job named goes to the job just announced, this soon after.
_REFERENT_S = 20.0
# Words that never tell one job's title from another's.
_ROUTING_STOPWORDS = frozenset({
    "a", "an", "and", "the", "or", "of", "to", "for", "in", "on", "with", "my", "your", "it", "that",
    "this", "job", "jobs", "one", "thing", "work", "task", "background", "new",
})
# A finished job's spoken summary: one sentence of at most this, or none.
_SPOKEN_SUMMARY_CHARS = 140
# A job's permission request or question is said only if the job is still
# waiting this long after asking. Some agents (Cursor in auto mode) report a
# request and then answer it themselves.
_ASK_GRACE_S = 2.0
# A job whose handoff turn was cut short this soon may be replaced.
_REPLACE_WINDOW_S = 30.0
# ...and only by a handoff whose caller words hold this share of its own.
_SAME_REQUEST_SHARE = 0.6
# The call page hears of the job's thinking at most this often.
_THINKING_EVERY_S = 1.5
# An adapter's turn deadline for a job with no limit: further off than any call.
_UNLIMITED_TURN_S = 7 * 24 * 3600.0

# Notes and cancels may name their job: <job-note job="todo app">, <job-cancel job="todo app"/>.
_TAG_RE = re.compile(
    r"<\s*(?P<cancel>job-cancel)(?:\s+job\s*=\s*[\"'](?P<cancel_job>[^\"']*)[\"'])?\s*/?\s*>"
    r"|<\s*(?P<note>job-note)(?:\s+job\s*=\s*[\"'](?P<note_job>[^\"']*)[\"'])?\s*>"
    r"|<\s*(?P<job>job)(?:\s+title\s*=\s*[\"'](?P<title>[^\"']*)[\"'])?\s*>",
    re.IGNORECASE,
)
# An opening tag the stream cut short: local voices and Snappy release
# phrases of about 64 characters, and a cut landed inside
# `<job title="eat work">`, so neither half matched and the brief was spoken.
_PARTIAL_OPEN_RE = re.compile(r"<\s*(?:j(?:o(?:b[^<>]*)?)?)?$", re.IGNORECASE)
_CLOSE_RE = {
    "job": re.compile(r"<\s*/\s*job\s*>", re.IGNORECASE),
    "note": re.compile(r"<\s*/\s*job-note\s*>", re.IGNORECASE),
}
_LINK_RE = re.compile(r"(?:https?|file)://[^\s<>\"')\]]+|(?<![\w/])(?:~|/)[\w.@-]+(?:/[\w.@-]+)+\.\w{1,8}\b")
_SENTENCE_RE = re.compile(r"(.+?[.!?])(?:\s|$)", re.DOTALL)

# "How's it going?", "is it done yet?", "any update on the todo app?"
_JOB_STATUS_RE = re.compile(
    r"\b(?:how'?s (?:it|that|the [\w ]{1,30}?) (?:going|coming(?: along)?|doing)"
    r"|how is (?:it|that|the [\w ]{1,30}?) (?:going|coming(?: along)?|doing)"
    r"|(?:is|are) (?:it|that|the [\w ]{1,30}?|you) (?:done|finished|ready)"
    r"|any (?:progress|updates?|news)"
    r"|(?:what'?s|give me) (?:the|a) status|status (?:update|check)"
    r"|what'?s the (?:job|background job|build) doing|where are (?:you|we) at"
    # "What's the latest thing the worker has done?", "did anything come in from the worker?"
    r"|(?:what(?:'s| is| has)?|did|has|is|any)\b[^.?!]{0,40}\b(?:the worker|the background job)\b"
    r"|\b(?:the worker|the background job)\b[^.?!]{0,30}\b(?:doing|done|up to|working on|progress))\b",
    re.IGNORECASE,
)
# "Stop everything", "cancel all the jobs", "cancel both": every job, no question.
_JOB_CANCEL_ALL_RE = re.compile(
    r"^(?:ok(?:ay)?,? |please )?(?:cancel|stop|kill|abort|drop) (?:everything|all(?: of them| the jobs| jobs)?"
    r"|both(?: of them| jobs)?|every job)(?: please)?$",
    re.IGNORECASE,
)
_JOB_CANCEL_RE = re.compile(
    r"^(?:ok(?:ay)?,? |please )?(?:cancel|stop|kill|abort|drop) (?:the |that )?(?:background )?"
    r"(?:job|build|task|work)(?: please)?$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class StartJob:
    title: str
    brief: str


@dataclass(frozen=True)
class JobNote:
    text: str
    # The job the agent named in the marker, if any.
    job: str = ""


@dataclass(frozen=True)
class CancelJob:
    job: str = ""


JobAction = StartJob | JobNote | CancelJob


class JobMarkerFilter:
    """Take job tags out of a reply's sentence stream.

    Sentences arrive one at a time, so a ``<job>`` block usually spans several.
    ``feed`` returns what is still meant to be heard plus any completed
    actions; text inside an open block is held until its closing tag.
    """

    def __init__(self) -> None:
        self._held = ""

    @property
    def holding(self) -> bool:
        return bool(self._held)

    def feed(self, sentence: str) -> tuple[str, list[JobAction]]:
        text = f"{self._held} {sentence}" if self._held else sentence
        self._held = ""
        spoken: list[str] = []
        actions: list[JobAction] = []
        while text:
            match = _TAG_RE.search(text)
            if match is None:
                partial = _PARTIAL_OPEN_RE.search(text)
                if partial is not None:
                    # Hold the start of a tag until the rest of it arrives.
                    spoken.append(text[: partial.start()])
                    self._held = text[partial.start():]
                else:
                    spoken.append(text)
                break
            spoken.append(text[: match.start()])
            if match.group("cancel"):
                actions.append(CancelJob(" ".join((match.group("cancel_job") or "").split())))
                text = text[match.end():]
                continue
            kind = "note" if match.group("note") else "job"
            close = _CLOSE_RE[kind].search(text, match.end())
            if close is None:
                self._held = text[match.start():]
                break
            actions.append(_action(kind, _label(match), text[match.end(): close.start()]))
            text = text[close.end():]
        return " ".join(part.strip() for part in spoken if part.strip()), actions

    def flush(self) -> list[JobAction]:
        """The reply ended inside a block: treat the rest as its body."""
        held, self._held = self._held, ""
        match = _TAG_RE.match(held)
        if match is None or match.group("cancel"):
            return []
        kind = "note" if match.group("note") else "job"
        return [_action(kind, _label(match), held[match.end():])]


def with_raw_bodies(actions: list[JobAction], raw: str, taken: list[int]) -> list[JobAction]:
    """The same actions with their bodies taken from the agent's raw text.

    Sentences are shaped for speech, which drops code, ids, markdown and parts
    of paths that another agent needs ("…/716355ce-5e90-…/" became "…/-5e90-…/").
    ``taken`` counts the jobs and notes already matched this reply."""
    if not raw or not actions:
        return actions
    blocks: dict[str, list[tuple[str, str]]] = {"job": [], "note": []}
    at = 0
    while (match := _TAG_RE.search(raw, at)) is not None:
        if match.group("cancel"):
            at = match.end()
            continue
        kind = "note" if match.group("note") else "job"
        close = _CLOSE_RE[kind].search(raw, match.end())
        body = raw[match.end(): close.start() if close else len(raw)].strip()
        blocks[kind].append((" ".join((match.group("title") or "").split()), body))
        at = close.end() if close else len(raw)
    out: list[JobAction] = []
    for action in actions:
        if isinstance(action, StartJob):
            index, taken[0] = taken[0], taken[0] + 1
            if index < len(blocks["job"]) and blocks["job"][index][1]:
                title, body = blocks["job"][index]
                action = StartJob(title=action.title or title, brief=body)
        elif isinstance(action, JobNote):
            index, taken[1] = taken[1], taken[1] + 1
            if index < len(blocks["note"]) and blocks["note"][index][1]:
                action = JobNote(blocks["note"][index][1], action.job)
        out.append(action)
    return out


def _label(match: re.Match[str]) -> str:
    """A job's title, or the job a note names."""
    return " ".join((match.group("title") or match.group("note_job") or "").split())


def _action(kind: str, title: str, body: str) -> JobAction:
    body = " ".join(body.split())
    if kind == "note":
        return JobNote(body, title)
    title = " ".join(title.split()) or _title_from(body)
    return StartJob(title=title, brief=body)


def _title_from(brief: str) -> str:
    words = re.findall(r"[\w'-]+", brief)[:3]
    return " ".join(words).lower() or "background"


def extract_links(text: str) -> tuple[str, ...]:
    seen: dict[str, None] = {}
    for match in _LINK_RE.finditer(text or ""):
        seen.setdefault(match.group(0).rstrip(".,;:"), None)
    return tuple(seen)


def first_sentences(text: str, count: int = 2, limit: int = 280) -> str:
    """The opening of a report, without links, cut at a word when long.
    A bare "Done." says nothing the announcement doesn't, so it is skipped."""
    without_links = " ".join(_LINK_RE.sub("", text or "").split())
    sentences = [match.group(1).strip() for match in _SENTENCE_RE.finditer(without_links)]
    sentences = [sentence for sentence in sentences if len(sentence.split()) >= 3]
    picked = " ".join(sentences[:count]) if sentences else without_links
    if len(picked) <= limit:
        return picked
    return picked[:limit].rsplit(" ", 1)[0].rstrip(" ,;:—-") + "…"


def is_job_status_question(text: str) -> bool:
    return bool(_JOB_STATUS_RE.search(" ".join((text or "").split())))


def is_job_cancel(text: str) -> bool:
    return bool(_JOB_CANCEL_RE.match(" ".join((text or "").split()).strip(" .!?")))


def is_job_cancel_all(text: str) -> bool:
    return bool(_JOB_CANCEL_ALL_RE.match(" ".join((text or "").split()).strip(" .!?")))


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", (text or "").lower())


def _title_key(title: str) -> str:
    return " ".join(_words(title))


def _same_request(job: Job, title: str, asked: str) -> bool:
    """Is this handoff the cut-short job's request asked again?

    A sentence that went on is asked again with both halves, so the new words
    hold most of the old ones ("make a tic tac toe game" … "with an AI
    opponent"). A new request made over the agent's answer ("another job:
    six flags valuation") does not, and must not stop the first job. Without
    the caller's words, the same title is the same request."""
    if job.asked and asked:
        before = set(_words(job.asked))
        return bool(before) and len(before & set(_words(asked))) >= _SAME_REQUEST_SHARE * len(before)
    return _title_key(job.title) == _title_key(title)


_COUNT_WORDS = ("no", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten")


def _count_word(count: int) -> str:
    return _COUNT_WORDS[count] if 0 <= count < len(_COUNT_WORDS) else str(count)


def _join_titles(jobs: list[Job]) -> str:
    """'The todo app', 'The todo app and the valuation', 'The a, the b and the c'."""
    names = [f"the {job.title}" for job in jobs]
    joined = names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"
    return joined[:1].upper() + joined[1:]


def _minutes(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes < 1:
        return "under a minute"
    return "a minute" if minutes == 1 else f"{minutes} minutes"


@dataclass
class Job:
    id: str
    title: str
    brief: str
    started_at: float
    phase: str = "starting"
    finished_at: float | None = None
    last_tool: str = ""
    last_detail: str = ""
    last_text: str = ""
    result: str = ""
    links: tuple[str, ...] = ()
    error: str = ""
    notes: list[str] = field(default_factory=list)
    reported: bool = False
    # The foreground turn that handed it off, the caller's words in it, and
    # whether that turn was cut short soon after (the caller kept talking):
    # its brief may be half a request, so a handoff of the whole request
    # replaces it rather than starting another.
    turn_id: int | None = None
    asked: str = ""
    replaceable: bool = False
    # The caller has heard how it ended (announcement or status reply).
    told: bool = False
    # Tool calls so far, and the last step the call page was shown.
    steps: int = 0
    shown_step: tuple[str, int] = ("", 0)
    # What the page shows for the current step ("grep · TODO in src"), the
    # last few, and when the current one began (wall clock, for its timer).
    last_label: str = ""
    recent: list[str] = field(default_factory=list)
    step_started: float = 0.0
    # The model's reasoning as it streams, while it has not acted yet.
    thinking: str = ""
    thinking_started: float = 0.0
    thinking_shown_at: float = 0.0
    # "thinking", "tool" or "text": what the job did last.
    activity: str = ""
    # The running job's own task and agent, and what it is waiting on.
    task: asyncio.Task[None] | None = field(default=None, repr=False, compare=False)
    agent: Any = field(default=None, repr=False, compare=False)
    permission: PermissionRequested | None = field(default=None, repr=False, compare=False)
    clarification: ClarificationRequested | None = field(default=None, repr=False, compare=False)
    ask_task: asyncio.Task[None] | None = field(default=None, repr=False, compare=False)

    @property
    def active(self) -> bool:
        return self.phase in ACTIVE

    @property
    def queued(self) -> bool:
        return self.phase == "queued"

    @property
    def waiting(self) -> bool:
        """Asked the caller something that is still open."""
        return self.permission is not None or self.clarification is not None


AgentFactory = Callable[[], Any]


@dataclass(frozen=True)
class Started:
    """What ``JobManager.start`` did: the job (None if it could not start)
    and a line the caller must hear, if any (it queued, or jobs are off)."""

    job: Job | None
    line: str = ""


class JobManager:
    """Runs a session's background jobs: up to ``max_jobs`` at once, the
    rest queued in order, none ever refused.

    ``agent_factory`` builds a fresh instance of the caller's agent, with no
    saved conversation. ``emit(name, fields)`` reports job changes to the
    session; ``announce(line)`` asks the session to say a line when nobody is
    talking (``urgent=True`` puts it first, when the session takes it).

    Several jobs mean the caller's words need a target. ``resolve`` picks one
    by the plan's routing rules: the job named, the only one, the one asking,
    the one just announced, then the selected one. The selection lives here,
    not in a page: Discord and the terminal route by it too. A bare cancel
    with several jobs live is confirmed by name before anything stops, and
    only one question (a job's request or that confirmation) is open at once,
    so a "yes" always has exactly one meaning.
    """

    def __init__(
        self,
        agent_factory: AgentFactory,
        *,
        emit: Callable[[str, dict[str, Any]], None],
        announce: Callable[..., None],
        max_seconds: float | None = None,
        max_jobs: int = DEFAULT_MAX_JOBS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._factory = agent_factory
        self._emit = emit
        self._announce = announce
        self._urgent_ok = _accepts(announce, "urgent")
        self._max_seconds = max_seconds
        self._max_jobs = max(1, int(max_jobs))
        self._clock = clock
        self._ids = itertools.count(1)
        self._jobs: list[Job] = []
        self._selected: str | None = None
        # The job whose question the caller was asked and has not answered.
        self._floor: Job | None = None
        # A bare cancel waiting for its yes.
        self._confirm: Job | None = None
        # The last spoken line that named exactly one job, and when.
        self._named: tuple[str, float] | None = None
        self._retiring: set[asyncio.Task[None]] = set()
        self._closed = False
        self._keep_talking_said = False

    # -- what is running -------------------------------------------------

    @property
    def jobs(self) -> tuple[Job, ...]:
        return tuple(self._jobs)

    @property
    def live(self) -> list[Job]:
        return [job for job in self._jobs if job.active]

    @property
    def queued(self) -> list[Job]:
        return [job for job in self._jobs if job.queued]

    @property
    def selected(self) -> Job | None:
        return next((job for job in self._jobs if job.id == self._selected), None)

    @property
    def job(self) -> Job | None:
        """The job a single-job caller means: the selected one, else the latest."""
        return self.selected or (self._jobs[-1] if self._jobs else None)

    @property
    def active(self) -> Job | None:
        """A live job: the selected one when it is live, else the newest."""
        selected = self.selected
        if selected is not None and selected.active:
            return selected
        live = self.live
        return live[-1] if live else None

    @property
    def asking(self) -> Job | None:
        """The job whose question the caller was asked and has not answered."""
        return self._floor

    @property
    def permission_pending(self) -> bool:
        return self._floor is not None and self._floor.permission is not None

    @property
    def confirming(self) -> Job | None:
        """The job a bare cancel asked about, still waiting for a yes."""
        return self._confirm

    def select(self, job_id: str) -> bool:
        """The caller picked a tab."""
        job = next((job for job in self._jobs if job.id == job_id), None)
        if job is None:
            return False
        if self._selected != job_id:
            self._selected = job_id
            self._publish(job, "job.selected")
        return True

    def keep_talking_line(self, said: str) -> str:
        """After the call's first handoff: the line that says talking won't
        interrupt the job, or '' (said already, or the agent said as much)."""
        if self._keep_talking_said:
            return ""
        self._keep_talking_said = True
        return "" if _KEEP_TALKING_RE.search(said or "") else KEEP_TALKING_LINE

    # -- which job the caller means ----------------------------------------

    def resolve(self, text: str = "", *, named: str = "", kind: str = "note") -> tuple[Job | None, str]:
        """The job the caller's words are aimed at, and the rule that chose it.

        ``kind`` is "note" (live jobs, or a finished one for a follow-up) or
        "cancel" (live and queued jobs). Rules, first that fires: "named"
        (``named`` is a title from a marker, or the words name a job),
        "referent" (within seconds of a spoken line that named one job: a
        note right after "the landing page is done" is about the landing
        page, even with another job running), "only" (one candidate),
        "asker" (a note answering the open question), then "selected".
        (None, "") when there is no candidate at all.
        """
        pending = [job for job in self._jobs if job.active or job.queued]
        if kind == "cancel":
            candidates = pending
        else:
            candidates = pending or [job for job in reversed(self._jobs) if job.phase == "completed"][:1]
        if not candidates:
            return None, ""
        everyone = pending + [job for job in self._jobs if job not in pending]
        if named:
            target = self._by_title(named, everyone)
            if target is not None and (target in candidates or kind == "note" and target.phase == "completed"):
                return target, "named"
        target = self._named_in(text, everyone)
        if target is not None and (target in candidates or kind == "note" and target.phase == "completed"):
            return target, "named"
        if self._named is not None and self._clock() - self._named[1] <= _REFERENT_S:
            target = next((job for job in everyone if job.id == self._named[0]), None)
            if target is not None and (target in candidates or kind == "note" and target.phase == "completed"):
                return target, "referent"
        if len(candidates) == 1:
            return candidates[0], "only"
        floor = self._floor
        if kind == "note" and floor is not None and floor.clarification is not None and floor in candidates:
            return floor, "asker"
        return self._default_target(candidates), "selected"

    def _default_target(self, candidates: list[Job]) -> Job:
        """The selected job when it can take the words, else the one asking,
        else the newest live one."""
        selected = self.selected
        if selected is not None and selected in candidates:
            return selected
        floor = self._floor
        if floor is not None and floor in candidates:
            return floor
        live = [job for job in candidates if job.active]
        return (live or candidates)[-1]

    @staticmethod
    def _by_title(title: str, jobs: list[Job]) -> Job | None:
        wanted = _title_key(title)
        exact = [job for job in jobs if _title_key(job.title) == wanted]
        if exact:
            return exact[0]
        return JobManager._named_in(title, jobs)

    @staticmethod
    def _named_in(text: str, jobs: list[Job]) -> Job | None:
        """The job whose distinctive title words the text uses most; None on a tie."""
        said = set(_words(text))
        if not said or not jobs:
            return None
        titles = {job.id: set(_words(job.title)) - _ROUTING_STOPWORDS for job in jobs}
        scores: dict[str, int] = {}
        for job in jobs:
            others = set().union(*(words for other, words in titles.items() if other != job.id))
            distinctive = titles[job.id] - others
            scores[job.id] = len(distinctive & said)
        best = max(scores.values())
        if best == 0:
            return None
        winners = [job for job in jobs if scores[job.id] == best]
        return winners[0] if len(winners) == 1 else None

    # -- foreground actions ------------------------------------------------

    def start(self, title: str, brief: str, *, turn_id: int | None = None, asked: str = "",
              reuse: Job | None = None) -> Started:
        """Start a job, or queue it behind the running ones.

        A job whose handoff turn was cut short is replaced by the same request
        asked again whole; another request starts beside it. ``asked`` is the
        caller's words in the handing-off turn. ``reuse`` is a finished job
        this one follows up: it keeps that job's id, so it takes over its tab."""
        if self._closed:
            return Started(None, UNAVAILABLE_LINE)
        stale = next((job for job in self._jobs if (job.active or job.queued) and job.replaceable
                      and _same_request(job, title, asked)), None)
        if stale is not None:
            self._retire(stale)
        if reuse is not None:
            job_id, title = reuse.id, reuse.title
            self._jobs.remove(reuse)
        else:
            job_id = f"job-{next(self._ids)}"
            title = self._unique_title(title, brief)
        # Queued until launched: a job must not count itself as running.
        job = Job(id=job_id, title=title, brief=brief, started_at=self._clock(), turn_id=turn_id, asked=asked,
                  phase="queued")
        self._jobs.append(job)
        self._trim()
        if self.selected is None or stale is not None and self._selected == stale.id:
            # The first job is selected; a new one never takes the selection
            # from another. A replaced job hands its selection on.
            self._selected = job.id
            self._publish(job, "job.selected")
        if len(self.live) >= self._max_jobs:
            job.phase = "queued"
            self._publish(job, "job.queued", position=len(self.queued))
            return Started(job, self._queued_line(job))
        self._launch(job)
        return Started(job)

    def _launch(self, job: Job) -> None:
        job.phase = "starting"
        job.started_at = self._clock()
        self._publish(job, "job.started", startedAt=int(time.time() * 1000))
        job.task = asyncio.create_task(self._run(job), name=f"background-{job.id}")

    def _start_next(self) -> None:
        """A slot opened: start the queued jobs that now fit, oldest first."""
        while not self._closed and len(self.live) < self._max_jobs:
            waiting = self.queued
            if not waiting:
                return
            self._launch(waiting[0])

    def _queued_line(self, job: Job) -> str:
        live = self.live
        if len(live) == 1:
            line = f"I'm still working on the {live[0].title} job, so the {job.title} job will start when it finishes."
        else:
            line = (f"{_count_word(len(live)).capitalize()} jobs are already running, so the {job.title} job "
                    "will start when one of them finishes.")
        waiting = len(self.queued)
        if waiting >= _QUEUE_NOTICE:
            line += f" That's {_count_word(waiting)} waiting."
        return line

    def _unique_title(self, title: str, brief: str) -> str:
        """A title no other job of this call has: routing by voice needs them to differ."""
        # A cancelled or replaced job can't be routed to, so its title is free.
        taken = {_title_key(job.title) for job in self._jobs if job.phase != "cancelled"}
        if _title_key(title) not in taken:
            return title
        have = set(_words(title))
        for word in _words(brief):
            if len(word) >= 4 and word not in have and word not in _ROUTING_STOPWORDS:
                candidate = f"{title} {word}"
                if _title_key(candidate) not in taken:
                    return candidate
        number = 2
        while _title_key(f"{title} {number}") in taken:
            number += 1
        return f"{title} {number}"

    def _trim(self) -> None:
        """Keep every live and queued job, and the newest finished ones."""
        done = [job for job in self._jobs if not (job.active or job.queued)]
        extra = len(done) - _FINISHED_KEEP
        for job in done[:max(0, extra)]:
            self._jobs.remove(job)

    async def note(self, text: str, *, job: Job | None = None) -> None:
        """Pass the caller's words to a job: an answer, a change, an addition.

        ``job`` is the target ``resolve`` chose; without one, the only or
        selected job. Words for a finished job run as its follow-up."""
        text = " ".join((text or "").split())
        if not text or self._closed:
            return
        if job is None:
            job, _how = self.resolve(kind="note")
            if job is None:
                return
        if job.queued:
            job.brief = f"{job.brief}\n\nThe person on the call adds: {text}"
            self._publish(job, "job.note_queued", note=text)
            return
        if not job.active:
            if job.phase == "completed":
                # Sent to a job that already ended: the foreground just told
                # the caller it was passed on, so run it as a follow-up.
                self.start(job.title, _follow_up_brief(job, text), reuse=job)
            return
        agent = job.agent
        if job.clarification is not None and agent is not None:
            request, job.clarification = job.clarification, None
            self._question_closed(job)
            if isinstance(await adapter_control(agent, AnswerClarification(request.request_id, text)), ControlAccepted):
                return
        if agent is not None and isinstance(await adapter_control(agent, Steer(text)), ControlAccepted):
            self._publish(job, "job.steered", note=text)
            return
        job.notes.append(text)
        self._publish(job, "job.note_queued", note=text)

    def turn_cancelled(self, turn_id: int) -> None:
        """The turn that handed a job off was cut short (barge-in, the
        caller's sentence continuing): a new handoff may replace it."""
        for job in self._jobs:
            if (job.active or job.queued) and job.turn_id == turn_id \
                    and self._clock() - job.started_at < _REPLACE_WINDOW_S:
                job.replaceable = True

    def _retire(self, job: Job) -> None:
        """Stop a replaced job in the background, without an announcement."""
        job.phase = "cancelled"
        job.finished_at = self._clock()
        job.reported = True
        task, agent = job.task, job.agent
        job.task = None
        job.agent = None
        self._job_moved_on(job)
        if task is not None:
            task.cancel()

        async def stop() -> None:
            if agent is not None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(agent.abort(), 5)
            if task is not None:
                await asyncio.gather(task, return_exceptions=True)

        self._retiring.add(asyncio.create_task(stop(), name=f"background-{job.id}-retire"))
        self._retiring = {pending for pending in self._retiring if not pending.done()}
        with contextlib.suppress(Exception):
            self._emit("job.replaced", {"jobId": job.id, "title": job.title, "phase": job.phase})

    async def cancel(self, job: Job | None = None) -> bool:
        """Stop a live or queued job (by default the one a bare cancel means)."""
        if job is None:
            job, _how = self.resolve(kind="cancel")
        if job is None or not (job.active or job.queued):
            return False
        if self._confirm is job:
            self._confirm = None
        job.phase = "cancelled"
        job.finished_at = self._clock()
        job.reported = True
        task, agent = job.task, job.agent
        self._job_moved_on(job)
        if task is not None:
            task.cancel()
            if agent is not None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(agent.abort(), 5)
            await asyncio.gather(task, return_exceptions=True)
        self._publish(job, "job.cancelled")
        self._start_next()
        return True

    async def cancel_all(self) -> int:
        """"Stop everything": every queued job, then every live one."""
        self._confirm = None
        targets = [*self.queued, *self.live]
        for job in targets:
            await self.cancel(job)
        return len(targets)

    def ask_cancel(self, job: Job) -> str:
        """A bare cancel with several jobs live: the line that confirms it by
        name. It takes the asking floor, so it waits behind an open request."""
        floor = self._floor
        if floor is not None and floor.waiting and floor is not job:
            return f"First, the {floor.title} job is waiting for your answer: {self._question_words(floor)}"
        self._confirm = job
        return f"Cancel the {job.title} job?"

    async def answer_cancel(self, yes: bool) -> str:
        """The caller's answer to "Cancel the … job?"."""
        job, self._confirm = self._confirm, None
        self._floor_freed()
        if job is None:
            return ""
        if not yes:
            return f"Okay, the {job.title} job keeps going."
        if await self.cancel(job):
            return f"Okay, I stopped the {job.title} job."
        return f"The {job.title} job had already stopped."

    def drop_confirmation(self) -> None:
        """The caller said something else: the question lapses."""
        if self._confirm is not None:
            self._confirm = None
            self._floor_freed()

    async def answer_permission(self, decision: PermissionDecision) -> bool:
        job = self._floor
        if job is None or job.permission is None or job.agent is None:
            return False
        request, job.permission = job.permission, None
        self._question_closed(job)
        result = await adapter_control(job.agent, ResolvePermission(request.request_id, decision))
        self._publish(job, "job.permission_resolved", decision=decision.value)
        return isinstance(result, ControlAccepted)

    # -- what the conversation is told -------------------------------------

    def status_line(self, text: str = "") -> str:
        """The spoken answer to "how's it going?": one job by name when the
        caller named one or only one is relevant, else all of them in a line."""
        relevant = self._status_jobs()
        if not relevant:
            return ""
        named = self._named_in(text, relevant) if text else None
        if named is not None:
            return self._job_status(named)
        if len(relevant) == 1:
            return self._job_status(relevant[0])
        going = [job for job in relevant if job.active or job.queued]
        ended = [job for job in relevant if not (job.active or job.queued)]
        for job in ended:
            job.told = True
        parts: list[str] = []
        live = [job for job in going if job.active]
        waiting_on_you = [job for job in live if job.waiting]
        if len(live) > 2:
            first = waiting_on_you[:1] + sorted(
                (job for job in live if job not in waiting_on_you), key=lambda job: job.started_at, reverse=True)
            picked = first[:2]
            described = " and ".join(self._brief_status(job) for job in picked)
            parts.append(f"{_count_word(len(live)).capitalize()} jobs are going: {described}. "
                         "Ask me about any of them by name.")
        elif live:
            described = " and ".join(self._brief_status(job) for job in live)
            parts.append(f"{'Two jobs are' if len(live) == 2 else 'One job is'} going: {described}.")
        queued = [job for job in going if job.queued]
        if queued:
            names = _join_titles(queued)
            parts.append(f"{names} {'job is' if len(queued) == 1 else 'jobs are'} waiting to start.")
        for job in ended:
            parts.append(self._ended_words(job))
        return " ".join(parts)

    def _status_jobs(self) -> list[Job]:
        now = self._clock()
        return [
            job for job in self._jobs
            if job.active or job.queued
            or job.finished_at is not None and now - job.finished_at < _STATUS_WINDOW_S
            and job.phase in {"completed", "failed"}
        ]

    def _job_status(self, job: Job) -> str:
        if not (job.active or job.queued):
            job.told = True
            return self._ended_words(job)
        if job.queued:
            return f"The {job.title} job is waiting to start."
        step = "thinking it through" if job.activity == "thinking" else _step(job)
        doing = f" Right now it's {_doing(step)}." if step else ""
        return f"The {job.title} job is still going, {_minutes(self._clock() - job.started_at)} in.{doing}"

    def _brief_status(self, job: Job) -> str:
        if job.waiting:
            return f"the {job.title} job, which is waiting on you"
        return f"the {job.title} job, {_minutes(self._clock() - job.started_at)} in"

    def _ended_words(self, job: Job) -> str:
        if job.phase == "completed":
            # Said once nobody is talking: short, the rest is in the chat and
            # the agent can tell more when asked.
            summary = first_sentences(job.result, count=1, limit=_SPOKEN_SUMMARY_CHARS)
            said = f" {summary}" if summary and not summary.endswith("…") else ""
            where = " The link is in the chat." if job.links else ""
            return f"The {job.title} job is done.{said}{where}"
        if job.phase == "failed":
            return f"The {job.title} job stopped: {job.error or 'it failed'}."
        return f"The {job.title} job was cancelled."

    def context_block(self) -> str:
        """What the foreground agent is told about the jobs this turn."""
        if not self._jobs:
            return ""
        going = [job for job in self._jobs if job.active or job.queued]
        news = [job for job in self._jobs if not (job.active or job.queued) and not job.reported]
        if len(going) == 1 and not news:
            return self._one_job_block(going[0])
        if not going:
            if news:
                return " ".join(self._report(job) for job in news)
            last = self._jobs[-1]
            # Every later turn: without this the agent kept passing notes
            # to a job that had ended, or left its files alone.
            return (
                f'[No background job is running. The last one, "{last.title}", {last.phase}. '
                "Its files are yours to change again: small follow-ups you do yourself, "
                "bigger ones go to a new job.]"
            )
        lines = [(f"[Background jobs, {len(going)} going. You can't see their processes; this is everything known "
                  "about them, so answer questions about them from here.")]
        for job in going:
            lines.append(f"- {self._job_summary(job)}")
        lines.append(
            "Each job owns the files it is working on, so don't edit them yourself. If the caller adds to, "
            "changes, or answers anything about one, pass their words on by writing "
            '<job-note job="its title">the words</job-note> and say which job you passed it to. Write '
            '<job-cancel job="its title"/> only when the caller asks to stop that job; "stop" on its own means '
            f"stop talking. {self._more_work()}]"
        )
        block = "\n".join(lines)
        if news:
            block += " " + " ".join(self._report(job) for job in news)
        return block

    def _one_job_block(self, job: Job) -> str:
        if job.queued:
            return (f'[Background job "{job.title}" is waiting to start. If the caller adds to it, write '
                    "<job-note>the words</job-note>; write <job-cancel/> only when they ask to drop it.]")
        lines = [
            f'[Background job "{job.title}" is running ({_minutes(self._clock() - job.started_at)} so far, '
            f"{job.steps} tool calls). You can't see its process, and its files may not exist yet; "
            "this is everything known about it, so answer questions about it from here."
        ]
        if job.recent:
            lines.append(f"Recent steps: {'; '.join(job.recent)}.")
        if job.activity == "thinking":
            lines.append("Right now it is thinking before its next step.")
        if job.last_text:
            lines.append(f'It last wrote: "{_clip(job.last_text, 200)}"')
        if job.permission is not None:
            lines.append(f"It is waiting for permission: {job.permission.prompt}")
        if job.clarification is not None:
            lines.append(f"It asked: {job.clarification.prompt}")
        lines.append(
            "It owns the files in this folder, so don't edit them yourself. If the caller adds "
            "to, changes, or answers anything about that work, pass their words on by writing "
            "<job-note>the words</job-note> and say you've passed it on. Write <job-cancel/> "
            "only when the caller asks to stop or cancel this job; \"stop\" on its own means "
            f"stop talking. {self._more_work()}]"
        )
        return " ".join(lines)

    def _more_work(self) -> str:
        """The rule for another handoff while jobs run. Told only the limit
        ("Up to 1 jobs can run at once"), an agent refused the handoff itself
        and promised to start it later, which nothing does: the request was lost."""
        limit = "one job runs" if self._max_jobs == 1 else f"{_count_word(self._max_jobs)} jobs run"
        return (
            "New bigger work still goes to a new job with a <job> block, even while this runs: "
            f"{limit} at once, and Sleeptalk queues any more and starts each by itself when a slot "
            "frees. Never hold a handoff back or promise to start it later yourself."
        )

    def _job_summary(self, job: Job) -> str:
        """One line per job: the context block is on every foreground turn."""
        if job.queued:
            return f'"{job.title}": waiting to start.'
        parts = [f'"{job.title}": running {_minutes(self._clock() - job.started_at)}, {job.steps} tool calls']
        if job.recent:
            parts.append(f"last step {job.recent[-1]}")
        if job.permission is not None:
            parts.append(f"waiting for permission: {_clip(job.permission.prompt, 120)}")
        elif job.clarification is not None:
            parts.append(f"asked: {_clip(job.clarification.prompt, 120)}")
        return "; ".join(parts) + "."

    def _report(self, job: Job) -> str:
        job.reported = True
        if job.phase == "completed":
            links = " ".join(job.links)
            return (
                f'[Background job "{job.title}" finished. Its report: "{_clip(job.result, 1200)}"'
                + (f" Links: {links}" if links else "")
                + "]"
            )
        return f'[Background job "{job.title}" {job.phase}: {job.error or "no details"}]'

    # -- the job itself ----------------------------------------------------

    async def _run(self, job: Job) -> None:
        agent = None
        try:
            agent = self._factory()
            if not job.active:
                return  # replaced before it began
            job.agent = agent
            job.phase = "running"
            self._publish(job, "job.running")
            async with asyncio.timeout(self._max_seconds):
                full, final = await self._turn(agent, job, f"{JOB_CONTRACT}\n\n{job.brief}")
                while job.notes:
                    note = job.notes.pop(0)
                    more = await self._turn(agent, job, f"The person on the call adds: {note}")
                    if any(more):
                        full, final = more
            if job.phase not in ACTIVE:
                return  # cancelled while the agent was wrapping up
            # The report is what the agent wrote after its last tool call;
            # earlier text is its narration while working.
            job.result = _last_message(final) or full
            job.links = extract_links(final) or extract_links(full)
            job.phase = "completed"
        except asyncio.CancelledError:
            if job.phase in ACTIVE:
                job.phase = "cancelled"
            raise
        except TimeoutError:
            if job.phase not in ACTIVE:
                return
            job.phase = "failed"
            job.error = (f"it ran out of time after {_minutes(self._max_seconds)}"
                         if self._max_seconds is not None else "its agent stopped answering")
        except Exception as exc:  # noqa: BLE001 - any agent failure ends the job, not the call
            logger.warning("background job %s failed: %s: %s", job.id, type(exc).__name__, exc)
            if job.phase not in ACTIVE:
                return
            job.phase = "failed"
            job.error = _failure(exc)
        finally:
            job.finished_at = self._clock()
            job.agent = None
            job.task = None
            self._job_moved_on(job)
            if agent is not None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(agent.close(), 10)
        if job.phase == "completed":
            self._publish(job, "job.completed", summary=first_sentences(job.result), result=job.result,
                          links=list(job.links))
            self._announce(self._ending_line(job))
        elif job.phase == "failed":
            self._publish(job, "job.failed", error=job.error)
            self._announce(self._ending_line(job))
        # A cancelled job's slot is refilled by cancel(), a replaced one's by start().
        self._start_next()

    def _ending_line(self, job: Job) -> Callable[[], str]:
        """Worked out when it is about to be said: nothing if a status reply
        already told the caller or a follow-up took the job's place. Jobs
        that ended while it waited are said in the same line, not one by one."""
        def line() -> str:
            if job.told or job not in self._jobs:
                return ""
            ended = [other for other in self._jobs
                     if other.phase in {"completed", "failed"} and not other.told]
            if job not in ended:
                ended.append(job)
            for other in ended:
                other.told = True
            if len(ended) == 1:
                self._named = (job.id, self._clock())
                return self._ended_words(job)
            self._named = None
            done = [other for other in ended if other.phase == "completed"]
            failed = [other for other in ended if other.phase == "failed"]
            parts: list[str] = []
            if done:
                both = " both" if len(done) == 2 else ""
                verb = "is" if len(done) == 1 else "are"
                parts.append(f"{_join_titles(done)} {'job ' if len(done) == 1 else 'jobs '}{verb}{both} done.")
                if any(other.links for other in done):
                    parts.append("The links are in the chat.")
            parts.extend(self._ended_words(other) for other in failed)
            return " ".join(parts)
        return line

    async def _turn(self, agent: Any, job: Job, prompt: str) -> tuple[str, str]:
        """Run one job turn: (everything it wrote, what it wrote after its last tool)."""
        deltas: list[str] = []
        sentences: list[str] = []
        tail: list[str] = []
        tail_sentences: list[str] = []
        failure: AgentError | None = None

        def on_event(raw: Any) -> None:
            nonlocal failure
            if not job.active:
                return  # a replaced or cancelled job's last words
            if isinstance(raw, AgentError):
                failure = raw
                return
            if isinstance(raw, AssistantDelta):
                if raw.text:
                    deltas.append(raw.text)
                    tail.append(raw.text)
                    job.last_text = "".join(tail)[-300:].strip()
                    job.activity = "text"
                self._job_moved_on(job)
            elif isinstance(raw, ReasoningDelta):
                self._show_thinking(job, raw.text)
            elif isinstance(raw, PermissionRequested):
                job.permission = raw
                self._ask_later(job, raw)
            elif isinstance(raw, ClarificationRequested):
                job.clarification = raw
                self._ask_later(job, raw)
            else:
                self._job_moved_on(job)
                if isinstance(raw, ToolEvent):
                    # Any tool activity: text before it was narration.
                    tail.clear()
                    tail_sentences.clear()
                    if raw.detail:
                        job.last_detail = raw.detail
                    if raw.display_name or raw.label:
                        job.last_label = " · ".join(part for part in (raw.display_name, raw.label) if part)
                    elif raw.phase is ToolPhase.STARTED:
                        job.last_label = ""
                    if raw.phase in {ToolPhase.STARTED, ToolPhase.UPDATED}:
                        job.activity = "tool"
                        job.thinking = ""
                signal = agent_signal(raw)
                if signal is None:
                    return
                name, fields = signal
                if name in {"tool", "tool_start", "tool_result", "tooldetail"}:
                    tail.clear()
                    tail_sentences.clear()
                if name in {"tool", "tool_start"}:
                    job.steps += 1
                if name == "tool" and fields.get("tool"):
                    job.last_tool = str(fields["tool"])
                    job.last_detail = "" if not (isinstance(raw, ToolEvent) and raw.detail) else job.last_detail
                elif name == "tooldetail" and fields.get("detail"):
                    job.last_detail = str(fields["detail"])
                self._show_step(job)

        method = agent.respond_streaming
        kwargs: dict[str, Any] = {"on_event": typed_events(on_event)}
        if _accepts(method, "timeout"):
            # An adapter's own limit is sized for a spoken reply (Hermes: five
            # minutes). A job has no limit unless one was set: calls run long
            # (a Claude Code job was cut off at the old half hour).
            kwargs["timeout"] = (
                _UNLIMITED_TURN_S if self._max_seconds is None
                else max(60.0, self._max_seconds - (self._clock() - job.started_at))
            )
        try:
            stream = method(prompt, **kwargs)
        except TypeError:
            stream = method(prompt)
        async for sentence in stream:
            sentences.append(sentence)
            tail_sentences.append(sentence)
            job.last_text = sentence
            # A line of bare links is left to the Done line, which shows them whole.
            if job.active and len(_LINK_RE.sub("", sentence).split()) >= 2:
                self._publish(job, "job.said", text=_clip(sentence, 400))
        if failure is not None:
            raise RuntimeError(failure.message or "The worker reported an error; no successful outcome was confirmed.")
        # Raw text keeps what sentence shaping drops (code, odd paths).
        if deltas:
            return "".join(deltas).strip(), "".join(tail).strip()
        return " ".join(sentences).strip(), " ".join(tail_sentences).strip()

    # -- one question at a time --------------------------------------------

    def _ask_later(self, job: Job, request: PermissionRequested | ClarificationRequested) -> None:
        if job.ask_task is not None:
            job.ask_task.cancel()
        job.ask_task = asyncio.create_task(self._ask(job, request), name=f"background-{job.id}-ask")

    async def _ask(self, job: Job, request: PermissionRequested | ClarificationRequested) -> None:
        await asyncio.sleep(_ASK_GRACE_S)
        job.ask_task = None
        if not job.active or (job.permission is not request and job.clarification is not request):
            return
        self._put_question(job)

    def _put_question(self, job: Job) -> None:
        """Ask the caller, or hold the question while another one is open."""
        floor = self._floor
        if self._confirm is not None or floor is not None and floor is not job and floor.waiting:
            return  # held; asked when the floor frees, unless the job moves on first
        self._floor = job
        if job.permission is not None:
            self._publish(job, "job.permission", prompt=job.permission.prompt)
        else:
            assert job.clarification is not None
            self._publish(job, "job.question", prompt=job.clarification.prompt)
        self._say(self._question_line(job), urgent=True)

    def _question_line(self, job: Job) -> Callable[[], str]:
        def line() -> str:
            if self._floor is not job or not job.waiting or not job.active:
                return ""
            self._named = (job.id, self._clock())
            if job.permission is not None:
                return f"The {job.title} job {_permission_words(job.permission.prompt)} Say yes or no."
            assert job.clarification is not None
            return f"Quick question from the {job.title} job: {job.clarification.prompt}"
        return line

    def _question_words(self, job: Job) -> str:
        if job.permission is not None:
            return f"it {_permission_words(job.permission.prompt)} Say yes or no."
        if job.clarification is not None:
            return job.clarification.prompt
        return ""

    def _question_closed(self, job: Job) -> None:
        if self._floor is job:
            self._floor_freed()

    def _floor_freed(self) -> None:
        """The open question was answered or lapsed: ask the next one held."""
        if self._floor is not None and self._floor.waiting and self._floor.active:
            return  # still open
        self._floor = None
        if self._confirm is not None:
            return
        for job in self._jobs:
            if job.active and job.waiting and job.ask_task is None:
                self._put_question(job)
                return

    def _say(self, line: str | Callable[[], str], *, urgent: bool = False) -> None:
        if urgent and self._urgent_ok:
            self._announce(line, urgent=True)
        else:
            self._announce(line)

    def _show_step(self, job: Job) -> None:
        """Tell the call page what the job is doing, once per change."""
        label = _step_label(job)
        if not label or not job.active or (label, job.steps) == job.shown_step:
            return
        if label != job.shown_step[0]:
            job.step_started = time.time()
            job.recent = [*job.recent, label][-3:]
        job.shown_step = (label, job.steps)
        self._publish(job, "job.step", text=label, steps=job.steps, startedAt=int(job.step_started * 1000))

    def _show_thinking(self, job: Job, text: str) -> None:
        """The model's reasoning before it acts (Hermes spent two minutes
        thinking before its first tool call, with nothing on the page)."""
        if not job.active or not text:
            return
        if job.activity != "thinking":
            job.activity = "thinking"
            job.thinking = ""
            job.thinking_started = time.time()
            job.thinking_shown_at = 0.0
        job.thinking = (job.thinking + text)[-600:]
        now = self._clock()
        if now - job.thinking_shown_at < _THINKING_EVERY_S:
            return
        if not job.thinking_shown_at and len(job.thinking.split()) < 6:
            return  # a first word alone ("Let") says nothing yet
        job.thinking_shown_at = now
        self._publish(job, "job.thinking", text=_tail_words(job.thinking, 160),
                      startedAt=int(job.thinking_started * 1000))

    def _job_moved_on(self, job: Job) -> None:
        """The job kept working (or ended), so whatever it asked was answered
        without us; a question it held on the floor gives the floor up."""
        had_question = job.waiting
        job.permission = None
        job.clarification = None
        if job.ask_task is not None:
            job.ask_task.cancel()
            job.ask_task = None
        if had_question or self._floor is job:
            self._question_closed(job)

    def _publish(self, job: Job, name: str, **fields: Any) -> None:
        with contextlib.suppress(Exception):
            self._emit(name, {"jobId": job.id, "title": job.title, "phase": job.phase, **fields})

    async def close(self) -> None:
        self._closed = True
        for job in [*self.queued, *self.live]:
            await self.cancel(job)
        if self._retiring:
            await asyncio.gather(*self._retiring, return_exceptions=True)


# Two of the agent's messages run together with no space between them
# ("…the delete checks:Done. Your app is…"): an adapter's sentence buffer
# held an unfinished line across a tool call.
_GLUED_RE = re.compile(r"(?<=[a-z0-9)][:.!?])(?=[A-Z][a-z])")


def _last_message(text: str) -> str:
    """The agent's closing message, without narration glued to its front."""
    text = (text or "").strip()
    tail = _GLUED_RE.split(text)[-1].strip()
    return tail if len(tail) >= 20 else text


def _follow_up_brief(job: Job, note: str) -> str:
    return (
        f"This follows up an earlier background job.\n\nThe earlier brief: {job.brief}\n\n"
        f"It reported: {_clip(job.result, 1200)}\n\nNow the person on the call wants: {note}"
    )


def _accepts(method: Any, name: str) -> bool:
    try:
        parameters = inspect.signature(method).parameters
    except (TypeError, ValueError):
        return False
    return name in parameters or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values())


def cancel_meant(text: str, title: str) -> bool:
    """Whether the caller's words ask to stop the job, not the agent's talking
    ("okay, you can stop now" once cancelled a game halfway through).
    "Cancel" is only ever about work; "stop" needs the job named."""
    words = set(re.findall(r"[a-z]+", (text or "").lower()))
    if words & {"cancel", "abort", "kill", "scrap"}:
        return True
    if not words & {"stop", "halt", "quit", "end", "drop"}:
        return False
    named = {"job", "worker", "background", "build", "building", "task"}
    named |= {word for word in re.findall(r"[a-z]+", title.lower()) if len(word) >= 4}
    return bool(words & named)


def _tail_words(text: str, limit: int) -> str:
    """The end of ``text``, starting at a word, for a live line."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[-limit:]
    return "…" + cut[cut.find(" ") + 1:] if " " in cut else "…" + cut


def _step_label(job: Job) -> str:
    """The current step for the page: the harness's tool and what it is on."""
    if job.last_label:
        return job.last_label
    if job.last_tool and job.last_detail:
        return f"{job.last_tool} · {job.last_detail}"
    return job.last_tool or _step(job)


def _step(job: Job) -> str:
    """The job's current step as words: its tool's detail, or what the tool does."""
    if not (job.last_detail or job.last_tool):
        return ""
    return tool_status_line(job.last_tool, job.last_detail)


def _permission_words(prompt: str) -> str:
    """'wants to: Run npm install?' for a readable request; a command line
    is not read out."""
    asked = " ".join((prompt or "").split()).strip(" ?.")
    if not asked or not is_hearable(asked) or len(asked) > 120:
        return "wants to run a command."
    return f"asks: {asked}?"


def _doing(step: str) -> str:
    step = step.strip().rstrip(".")
    if not step:
        return ""
    return step[0].lower() + step[1:]


def _clip(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _failure(exc: BaseException) -> str:
    message = " ".join(str(exc).split())[:160]
    return message or type(exc).__name__
