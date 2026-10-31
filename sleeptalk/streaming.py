"""Shared streaming layer above the adapter seam.

Everything the four agent adapters used to hand-roll, extracted so an
adapter is transport + event-name translation + a vocab table:

- SentenceStreamer: text deltas -> speakable sentences (first-clause
  fast-flush included). Pure and synchronous; the audible path.
- EventGate: first-of-kind event dedup (text / thinking / tool per turn).
- DeadlineClock + await_activity: the idle-timeout + hard-deadline wait
  loop (activity resets idle, hard never resets), plus keepalive ticks.
- ToolVocab + narrate_tool: gerund tool narration driven by per-adapter
  vocab tables (kind map + which arg field holds the subject).
- next_reassure_line / compose_progress_line: keepalive/reassure line
  picking, moved here from pi_discord_bot so every transport shares it.

Behavioral invariants preserved from the adapters (do not "fix"):
- The first-clause flush fires at most once per turn, only when the buffer
  holds no complete sentence yet (guarded by the same yielded_any flag the
  sentence loop sets).
- On timeout/abort/error the partial buffer is dropped, never flushed.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import subprocess
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from sleeptalk.agent_environment import agent_environment

from sleeptalk.text import (
    SpeakableStreamFilter,
    extract_first_clause,
    extract_sentence,
    shape_voice_reply,
    speakable_subject,
)


LOCAL_PHRASE_STREAMING: ContextVar[bool] = ContextVar("local_phrase_streaming", default=False)
SNAPPY_PHRASE_STREAMING: ContextVar[bool] = ContextVar("snappy_phrase_streaming", default=False)


def _maybe_decimal(buffer: str, sentence: str) -> bool:
    """A period after a digit, with nothing after it yet: the rest of a
    number ("5.3", "3.14") may be in the next delta. flush() still says it
    if the reply really ends there."""
    stripped = buffer.rstrip()
    return (stripped.endswith(".") and len(stripped) >= 2 and stripped[-2].isdigit()
            and stripped == buffer and sentence.rstrip() == stripped.strip())


class SentenceStreamer:
    """Text deltas in, speakable sentences out.

    Applies SpeakableStreamFilter, then the first-clause fast-flush (voice
    latency: don't wait for a full-sentence terminator on the first output),
    then full-sentence extraction. Call flush() at turn end for the tail.
    """

    def __init__(self) -> None:
        # Task-local: local browser speech cannot change another call's
        # adapter, including adapters which instantiate this helper themselves.
        self._local_phrases = LOCAL_PHRASE_STREAMING.get()
        self._phrase_limits = (24, 64) if SNAPPY_PHRASE_STREAMING.get() else (64, 140)
        self._filter = SpeakableStreamFilter()
        self._buffer = ""
        self._yielded_any = False

    def _safe_cut(self, prefix: str) -> bool:
        if not self._local_phrases:
            return True
        from sleeptalk.speech.local_text import safe_speech_boundary

        return safe_speech_boundary(self._buffer, self._buffer.index(prefix) + len(prefix))

    def feed(self, delta: str) -> list[str]:
        self._buffer += self._filter.feed(delta)
        out: list[str] = []
        if not self._yielded_any and not extract_sentence(self._buffer)[0]:
            clause, _remainder = extract_first_clause(self._buffer)
            if clause and self._safe_cut(clause):
                # Keep trailing whitespace in the unfinished tail, otherwise
                # the next delta can turn "It " + "costs" into "Itcosts".
                self._buffer = self._buffer[self._buffer.index(clause) + len(clause):].lstrip()
                spoken = shape_voice_reply(clause)
                if spoken:
                    self._yielded_any = True
                    out.append(spoken)
        while True:
            sentence, _remainder = extract_sentence(self._buffer)
            if not sentence or not self._safe_cut(sentence):
                break
            if _maybe_decimal(self._buffer, sentence):
                break  # "GLM 5." may go on "3 from Z-AI": wait for the next delta
            self._buffer = self._buffer[self._buffer.index(sentence) + len(sentence):].lstrip()
            spoken = shape_voice_reply(sentence)
            if spoken:
                self._yielded_any = True
                out.append(spoken)
        if self._local_phrases:
            # Offline local voices need coherent phrases, not one request per
            # word. Release a bounded opening even without sentence punctuation.
            limit = self._phrase_limits[1] if self._yielded_any else self._phrase_limits[0]
            while len(self._buffer) > limit:
                cut = self._buffer.rfind(" ", 0, limit + 1)
                if cut < limit // 2:
                    break  # never split a long word, path, or URL
                prefix = self._buffer[:cut]
                if not self._safe_cut(prefix):
                    break
                if any(prefix.count(mark) % 2 for mark in ("**", "__")):
                    break  # let the formatting closer arrive before shaping
                spoken = shape_voice_reply(prefix)
                self._buffer = self._buffer[cut:].lstrip()
                if spoken:
                    self._yielded_any = True
                    out.append(spoken)
                limit = self._phrase_limits[1]
        return out

    def flush(self) -> str:
        """Trailing partial sentence at clean turn end; '' if none."""
        tail = shape_voice_reply(self._buffer)
        self._buffer = ""
        return tail

    def clear_buffer(self) -> None:
        """Drop buffered text without touching the first-clause state.

        pi resets its buffer on agent_start (a fresh low-level run within
        the turn); the yielded_any history must survive so the clause
        flush stays once-per-turn.
        """
        self._buffer = ""


class EventGate:
    """First-of-kind event dedup for one turn.

    fire("text"|"thinking"|"tool") emits once per kind per turn; emit()
    always passes through (tool:<kind>, tooldetail:<detail>, keepalive...).
    """

    def __init__(self, on_event: Callable[[str], None] | None = None) -> None:
        self._on_event = on_event
        self._fired: set[str] = set()

    def fire(self, name: str) -> bool:
        if name in self._fired:
            return False
        self._fired.add(name)
        sink = self._on_event
        if sink is not None and not getattr(sink, "_skip_legacy_fire", False):
            sink(name)
        return True

    def emit(self, event: str) -> None:
        if self._on_event is not None:
            self._on_event(event)


class DeadlineClock:
    """Idle-timeout + hard-deadline clock for one turn.

    note_activity() (any transport event) resets the idle clock; the hard
    deadline never resets. note_audible() (a yielded sentence/clause or a
    tool narration — anything the user can hear) resets the keepalive
    timer: keepalives fill *silent* stretches, so thinking deltas and raw
    transport activity do NOT defer them. keepalive_interval_s=0 disables.
    """

    def __init__(
        self,
        timeout: float,
        *,
        idle_error: Callable[[], Exception],
        hard_error: Callable[[], Exception],
        keepalive_interval_s: float = 0.0,
        on_keepalive: Callable[[], None] | None = None,
    ) -> None:
        now = asyncio.get_running_loop().time()
        self._timeout = timeout
        self._idle_deadline = now + timeout
        self._hard_deadline = now + timeout
        self._idle_error = idle_error
        self._hard_error = hard_error
        self._keepalive_interval = keepalive_interval_s
        self._on_keepalive = on_keepalive
        self._next_keepalive = (
            now + keepalive_interval_s if keepalive_interval_s > 0 else None
        )

    def note_activity(self) -> None:
        self._idle_deadline = asyncio.get_running_loop().time() + self._timeout

    def note_audible(self) -> None:
        if self._keepalive_interval > 0:
            self._next_keepalive = (
                asyncio.get_running_loop().time() + self._keepalive_interval
            )

    def remaining(self) -> float:
        """Seconds until the next deadline; raises when one has passed."""
        now = asyncio.get_running_loop().time()
        if now >= self._hard_deadline:
            raise self._hard_error()
        if now >= self._idle_deadline:
            raise self._idle_error()
        return min(self._idle_deadline, self._hard_deadline) - now

    def raise_for_timeout(self) -> None:
        """Raise after a wait timed out: idle error if idle passed, else hard."""
        now = asyncio.get_running_loop().time()
        if now >= self._idle_deadline:
            raise self._idle_error()
        raise self._hard_error()

    def keepalive_due(self) -> bool:
        return (
            self._next_keepalive is not None
            and asyncio.get_running_loop().time() >= self._next_keepalive
        )

    def keepalive_in(self) -> float:
        """Seconds until the next keepalive tick (inf when disabled)."""
        if self._next_keepalive is None:
            return float("inf")
        return max(0.0, self._next_keepalive - asyncio.get_running_loop().time())

    def fire_keepalive(self) -> None:
        """Emit the keepalive signal and rearm."""
        if self._on_keepalive is not None:
            self._on_keepalive()
        self.note_audible()  # a spoken keepalive is audible progress


#: Sentinel returned by await_activity when a keepalive fired instead of an event.
KEEPALIVE = "keepalive"


async def await_activity(
    clock: DeadlineClock, waiters: Mapping[str, Awaitable[Any]]
) -> str:
    """Wait for the first of ``waiters``, a keepalive tick, or a deadline.

    Returns the name of the completed waiter, or KEEPALIVE when a keepalive
    was emitted (callers re-arm and re-await). Raises the clock's idle/hard
    error when a deadline passes with no activity.

    Waiters must be Tasks/Futures (ensure_future returns them unchanged, so
    the caller keeps ownership): completing waiters are left for the caller
    to result(); pending waiters are NOT cancelled — adapters differ here
    on purpose (pi awaits its cancellations, hermes doesn't).
    """
    remaining = min(clock.remaining(), clock.keepalive_in())
    tasks = {name: asyncio.ensure_future(w) for name, w in waiters.items()}
    done, _pending = await asyncio.wait(
        set(tasks.values()), timeout=remaining, return_when=asyncio.FIRST_COMPLETED
    )
    if not done:
        if clock.keepalive_due():
            clock.fire_keepalive()
            return KEEPALIVE
        # The wait consumed the full remaining budget: a deadline passed.
        # Idle first: the idle clock is always >= the hard deadline, and a
        # silent turn (idle == hard) is the common case — adapters pin the
        # idle message ("timed out") there.
        clock.raise_for_timeout()
    for name, task in tasks.items():
        if task in done:
            return name
    raise RuntimeError("await_activity: completed waiter not found")


# ---------------------------------------------------------------------------
# Tool narration
# ---------------------------------------------------------------------------

DEFAULT_TOOL_VERBS: dict[str, str] = {
    "bash": "running",
    "read": "reading",
    "write": "writing",
    "edit": "editing",
    "search": "searching for",
}

SEARCH_SUBJECT_LIMIT = 60


@dataclass(frozen=True)
class ToolVocab:
    """Per-adapter narration vocabulary.

    kinds:        lowercased tool name/kind -> narration kind
                  ("bash" | "read" | "write" | "edit" | "search" | "other").
    subject_args: narration kind -> arg fields tried in order for the
                  spoken subject (e.g. ("command",), ("path", "file_path")).
                  Kinds absent here never produce a subject from args.
    verbs:        narration kind -> gerund; a kind with no verb yields the
                  subject verbatim when fallback_to_subject, else "".
    fallback_to_subject: hermes-ACP behavior — an unmapped kind narrates
                  its title as-is instead of staying silent.
    """

    kinds: Mapping[str, str]
    subject_args: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    verbs: Mapping[str, str] = field(default_factory=lambda: dict(DEFAULT_TOOL_VERBS))
    fallback_to_subject: bool = False


#: MCP tool tails whose naive gerund sounds wrong spoken aloud.
_MCP_GERUND_OVERRIDES: dict[str, str] = {
    "deep_research": "researching",
}


def _gerund(word: str) -> str:
    """Naive English gerund: analyze -> analyzing, check -> checking."""
    if word.endswith("e") and not word.endswith(("ee", "ye")):
        return word[:-1] + "ing"
    return word + "ing"


def humanize_mcp_tool(name: str) -> str:
    """mcp__research__check_new_uploads -> 'checking new uploads'.

    MCP tool names carry their own verb, so the detail phrase is derived
    from the name itself — every MCP server gets tailored narration
    without per-server config.
    """
    tail = name.split("__")[-1].strip().lower()
    if tail in _MCP_GERUND_OVERRIDES:
        return _MCP_GERUND_OVERRIDES[tail]
    words = [w for w in tail.replace("-", "_").split("_") if w]
    if not words:
        return ""
    return " ".join([_gerund(words[0]), *words[1:]])


def narrate_tool(
    name: str, args: Any, vocab: ToolVocab
) -> tuple[str, str]:
    """(narration kind, spoken detail) for one tool invocation.

    ``args`` is the tool's argument dict, or a pre-extracted subject string
    (hermes-ACP titles). Detail is a gerund phrase ("running git status")
    or "" when there's nothing worth saying.
    """
    if name.strip().lower().startswith("mcp__"):
        return "mcp", humanize_mcp_tool(name)
    kind = vocab.kinds.get(name.strip().lower(), "other")
    subject = ""
    if isinstance(args, str):
        subject = speakable_subject(args)
    elif isinstance(args, dict):
        for key in vocab.subject_args.get(kind, ()):
            value = str(args.get(key) or "").strip()
            if value:
                limit = SEARCH_SUBJECT_LIMIT if kind == "search" else 70
                subject = speakable_subject(value, limit)
                if subject:
                    break
    if not subject:
        return kind, ""
    verb = vocab.verbs.get(kind)
    if verb is None:
        return kind, subject if vocab.fallback_to_subject else ""
    return kind, f"{verb} {subject}"


# ---------------------------------------------------------------------------
# Keepalive / reassure line picking (moved from pi_discord_bot)
# ---------------------------------------------------------------------------

def elapsed_suffix(elapsed_seconds: float) -> str:
    minutes = int(elapsed_seconds // 60)
    if minutes <= 0:
        return ""
    unit = "minute" if minutes == 1 else "minutes"
    return f" — about {minutes} {unit} in"


def compose_progress_line(current_detail: str, elapsed_seconds: float) -> str:
    """Live progress line from the current tool detail, e.g.
    "Still searching the web for overnight news — about a minute in."
    Empty string when no detail is known (canned pools take over)."""
    if not current_detail:
        return ""
    return f"Still {current_detail}{elapsed_suffix(elapsed_seconds)}."


DEFAULT_QUIET_WORK_LINE = "I'll work quietly and tell you when I'm done."


def next_quiet_progress_line(
    current_detail: str,
    elapsed_seconds: float,
    used: set[str],
    *,
    quiet_line: str = DEFAULT_QUIET_WORK_LINE,
) -> str:
    """At most one spoken progress line per turn. Live tool detail wins."""
    if used:
        return ""
    if current_detail:
        return compose_progress_line(current_detail, elapsed_seconds)
    return quiet_line


def next_reassure_line(
    current_tool: str,
    current_detail: str,
    elapsed_seconds: float,
    used: set[str],
    policy: Any = None,
) -> str:
    """Pick the next reassure line: live progress first, then tool-specific,
    then the generic gold pool — never repeating within a turn. Empty string
    when every candidate has been said (the knock carries the silence)."""
    from sleeptalk.policy import VoicePolicy  # lazy: avoid import cycle

    pools = policy or VoicePolicy()
    candidates: list[str] = []
    dynamic = compose_progress_line(current_detail, elapsed_seconds)
    if dynamic:
        candidates.append(dynamic)
    if current_tool:
        candidates.extend(pools.reassure_tool_pools.get(current_tool, ()))
    candidates.extend(pools.preamble_pools["reassure"])
    for line in candidates:
        if line not in used:
            return line
    return ""


# ---------------------------------------------------------------------------
# Smart ack selection + preamble dedupe (VOICE_PREAMBLES_PLAN.md)
# ---------------------------------------------------------------------------

# Tone safety first: an emotional utterance must never get a cheerful
# "On it." — the research's "psychotic mismatch".
_EMPATHY_RE = re.compile(
    r"\b(died|death|passed away|sick|hospital|sad|depressed|angry|furious|"
    r"frustrated|upset|worried|anxious|scared|divorce|fired|laid off|"
    r"broke up|dumped|terrible|awful|horrible|devastated|grieving)\b",
    re.IGNORECASE,
)
# Lookup/question: the answer probably needs a tool or a search — a slow
# turn whose honest preamble is "Let me check that."
_LOOKUP_RE = re.compile(
    r"(^(what|who|whom|whose|when|where|why|how|which)\b"
    r"|\b(check|look ?up|search|find out|google|verify|"
    r"latest|news|current|currently|weather|forecast|price of|status of|"
    r"tell me (about|what|who|when|where|why|how))\b"
    r"|\?\s*$)",
    re.IGNORECASE,
)
# Imperative task: the agent will do work — "On it."
_TASK_RE = re.compile(
    r"^(please\s+)?(add|fix|run|make|create|delete|remove|update|change|"
    r"edit|write|build|deploy|restart|reboot|commit|push|pull|install|"
    r"set up|setup|implement|refactor|rename|move|open|close|stop|start|"
    r"kill|test|debug|review|summari[sz]e|explain|show|list|give|read)\b",
    re.IGNORECASE,
)
# Bare backchannels/confirmations: no ack at all (the turn is fast).
_SHORT_CONFIRM_RE = re.compile(
    r"^(yeah|yep|yup|yes|ok|okay|sure|no|nope|nah|right|correct|"
    r"thanks|thank you|cool|nice|great|got it|mmhmm|uh huh|go ahead|"
    r"continue|sounds good|please do)\.?\s*$",
    re.IGNORECASE,
)


def select_ack_pool(transcript: str) -> str:
    """Pick the instant-ack preamble pool for a committed utterance.

    Returns "none" (stay silent — predicted-fast turn; the runner's tiered
    coverage still applies), "ack_action" (combined ack+action clip for
    lookup turns; caller falls back to "action" when the pool is empty),
    "action:task", "ack:empathy", or "ack" (generic fallback for long
    ambiguous utterances). Tone safety ranks first: an emotional utterance
    never gets a cheerful task ack.
    """
    text = transcript.strip()
    if not text:
        return "none"
    if _SHORT_CONFIRM_RE.match(text):
        return "none"
    if _EMPATHY_RE.search(text):
        return "ack:empathy"
    if _LOOKUP_RE.search(text):
        return "ack_action"
    if _TASK_RE.match(text):
        return "action:task"
    # Long utterances are slow turns even when the shape is ambiguous.
    if len(text) >= 120:
        return "ack"
    return "none"


def _normalize_preamble(text: str) -> str:
    return " ".join(text.strip().lower().split()).rstrip(".!?")


def is_generic_preamble(text: str, policy: Any = None) -> bool:
    """True when a sentence is itself a canned-style preamble line.

    Used to dedupe the agent's own first-sentence preamble against an ack
    clip that already played: two generic preambles in one turn is the
    "Okay. ... Let me check that." anti-pattern from the plan.
    """
    from sleeptalk.policy import (  # lazy: avoid import cycle
        DEFAULT_FILLER_GENERAL,
        DEFAULT_FILLER_WEB_SEARCH,
        DEFAULT_PREAMBLE_POOLS,
    )

    if policy is not None:
        # A supplied policy fully replaces the defaults (same semantics as
        # VoicePolicy.from_config) — never merged.
        pools: Mapping[str, tuple[str, ...]] = (
            getattr(policy, "preamble_pools", None) or DEFAULT_PREAMBLE_POOLS
        )
        lines: set[str] = set(getattr(policy, "filler_general", ()) or ())
        lines.update(getattr(policy, "filler_web_search", ()) or ())
    else:
        pools = DEFAULT_PREAMBLE_POOLS
        lines = set(DEFAULT_FILLER_GENERAL) | set(DEFAULT_FILLER_WEB_SEARCH)
    for pool_lines in pools.values():
        lines.update(pool_lines)
    normalized = _normalize_preamble(text)
    if not normalized:
        return False
    for line in lines:
        candidate = _normalize_preamble(line)
        if not candidate:
            continue
        if normalized == candidate:
            return True
        # "Let me check that for you." still leads with the canned line.
        # Require enough substance that tiny acks ("Mm.") can't prefix-match.
        if (
            sum(1 for c in candidate if c.isalnum()) >= 6
            and normalized.startswith(candidate)
        ):
            return True
    return False


# ---------------------------------------------------------------------------
# Stdio JSON-RPC process lifecycle (component 5)
# ---------------------------------------------------------------------------


class StdioJsonRpcProcess:
    """Process lifecycle shared by the stdio adapters (pi_rpc, claude_code).

    Subclasses supply launch args and a message-routing function; the base
    owns binary resolution (with install hint), the NDJSON stdout reader,
    _terminate (5s grace -> kill), and close.

    Nothing here is a turn concept — no deadlines, no events. One process,
    one reader task, one routing callback.
    """

    reader_task_name = "stdio-jsonrpc-reader"

    def __init__(
        self,
        *,
        cwd: str,
        error_cls: type[Exception],
        binary: str = "",
        binary_fallback: str = "",
        binary_fallbacks: tuple[str, ...] = (),
        install_hint: str = "",
        env: dict[str, str] | None = None,
    ) -> None:
        self._subprocess_env = agent_environment(env)
        self.cwd = cwd
        self._error_cls = error_cls
        self._binary = binary
        self._binary_fallback = binary_fallback
        # Extra well-known locations tried in order when PATH lookup fails
        # (e.g. ~/.local/bin, which vendor installers use but shells don't
        # always export). `binary_fallback` stays as the last resort so the
        # error names a concrete path.
        self._binary_fallbacks = tuple(binary_fallbacks)
        self._install_hint = install_hint
        self._proc: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task | None = None
        self._closed = False

    # ---- subclass hooks ----

    async def _route_message(self, message: dict[str, Any]) -> None:
        """Handle one parsed NDJSON line (enqueue, answer, intercept)."""
        raise NotImplementedError

    # ---- binary ----

    def _resolve_binary(self, explicit: str = "") -> str:
        """Resolve the binary path: explicit config, PATH, then fallback."""
        if explicit:
            candidate = os.path.expanduser(explicit)
            if os.sep in candidate:
                return candidate
            return shutil.which(candidate, path=self._subprocess_env.get("PATH")) or candidate
        if self._binary:
            found = shutil.which(self._binary, path=self._subprocess_env.get("PATH"))
            if found:
                return found
        for candidate in self._binary_fallbacks:
            expanded = os.path.expanduser(candidate)
            if os.sep in expanded:
                if os.access(expanded, os.X_OK):
                    return expanded
            else:
                found = shutil.which(expanded, path=self._subprocess_env.get("PATH"))
                if found:
                    return found
        return os.path.expanduser(self._binary_fallback) if self._binary_fallback else ""

    def _check_binary(self, path: str) -> None:
        if not path or not Path(path).exists():
            hint = self._install_hint or f"{self._binary or path} binary not found."
            raise self._error_cls(hint)

    # ---- spawn / reader ----

    async def _spawn(self, args: list[str], env: dict[str, str] | None = None) -> None:
        self._proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=self.cwd,
            env=agent_environment(env if env is not None else self._subprocess_env),
        )
        self._reader_task = asyncio.create_task(
            self._read_stdout(), name=self.reader_task_name
        )

    async def _read_stdout(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        buffer = b""
        try:
            while True:
                chunk = await self._proc.stdout.read(4096)
                if not chunk:
                    break
                buffer += chunk
                while True:
                    newline = buffer.find(b"\n")
                    if newline == -1:
                        break
                    line = buffer[:newline]
                    if line.endswith(b"\r"):
                        line = line[:-1]
                    buffer = buffer[newline + 1 :]
                    if not line:
                        continue
                    try:
                        message = json.loads(line.decode("utf-8"))
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        continue
                    await self._route_message(message)
        except asyncio.CancelledError:
            pass
        finally:
            if buffer:
                try:
                    message = json.loads(buffer.decode("utf-8"))
                    await self._route_message(message)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    pass

    async def _send_json(self, payload: dict[str, Any]) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        self._proc.stdin.write(
            json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n"
        )
        await self._proc.stdin.drain()

    # ---- teardown ----

    async def _terminate(self) -> None:
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader_task
        self._reader_task = None
        if self._proc and self._proc.returncode is None:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                self._proc.kill()
                await self._proc.wait()
        self._proc = None

    async def close(self) -> None:
        self._closed = True
        await self._terminate()


# ---------------------------------------------------------------------------
# Generic stdio agent — the "new agent = config, not code" end state
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TurnEvent:
    """One normalized turn event, produced by an adapter's translate fn."""

    kind: str  # "thinking" | "text" | "tool" | "other"
    text: str = ""  # text delta (kind == "text")
    tool_name: str = ""  # raw tool name (kind == "tool")
    tool_args: Any = None  # tool args dict (kind == "tool")
    terminal: bool = False  # turn is complete after this event
    error: str = ""  # raise error_cls(error) on this event


@dataclass(frozen=True)
class StdioAgentSpec:
    """Everything a generic stdio agent needs: a command, a translation
    function, and a narration vocab. Registration lives beside it."""

    args: tuple[str, ...]  # argv, starting with the binary
    make_prompt: Callable[[str], Mapping[str, Any]]  # text -> stdin NDJSON payload
    translate: Callable[[Mapping[str, Any]], TurnEvent]  # stdout line -> event
    vocab: ToolVocab
    binary: str = ""  # for resolution/hints; defaults to args[0]
    binary_fallback: str = ""
    install_hint: str = ""


class GenericStdioAgent(StdioJsonRpcProcess):
    """AgentAdapter for any NDJSON-over-stdio agent, driven by a spec.

    No per-agent streaming or lifecycle code: the loop is the shared one
    (SentenceStreamer + EventGate + DeadlineClock + narrate_tool). New
    agent = a StdioAgentSpec + a registration.
    """

    def __init__(
        self,
        spec: StdioAgentSpec,
        *,
        cwd: str | None = None,
        name: str = "generic-stdio",
        error_cls: type[Exception] = RuntimeError,
        keepalive_interval_s: float = 0.0,
    ) -> None:
        super().__init__(
            cwd=cwd or os.getcwd(),
            error_cls=error_cls,
            binary=spec.binary or (spec.args[0] if spec.args else ""),
            binary_fallback=spec.binary_fallback,
            install_hint=spec.install_hint,
        )
        self.spec = spec
        self.name = name
        self.keepalive_interval_s = keepalive_interval_s
        self._lock = asyncio.Lock()
        self._event_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def _route_message(self, message: dict[str, Any]) -> None:
        await self._event_queue.put(message)

    async def _ensure_started(self) -> None:
        if self._proc is not None and self._proc.returncode is None:
            return
        if self._closed:
            raise self._error_cls(f"{self.name} client is closed.")
        binary = self._resolve_binary(self.spec.args[0] if self.spec.args else "")
        self._check_binary(binary)
        await self._spawn([binary, *self.spec.args[1:]])

    async def respond(self, text: str, timeout: float = 300.0) -> str:
        parts: list[str] = []
        async for sentence in self.respond_streaming(text, timeout=timeout):
            parts.append(sentence)
        from sleeptalk.text import extract_speakable_text

        return extract_speakable_text(" ".join(parts))

    async def respond_streaming(
        self,
        text: str,
        timeout: float = 300.0,
        on_event: Callable[[str], None] | None = None,
    ) -> AsyncIterator[str]:
        async with self._lock:
            await self._ensure_started()
            while not self._event_queue.empty():
                try:
                    self._event_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
            await self._send_json(dict(self.spec.make_prompt(text)))

            streamer = SentenceStreamer()
            gate = EventGate(on_event)
            clock = DeadlineClock(
                timeout,
                idle_error=lambda: self._error_cls(f"{self.name} response timed out."),
                hard_error=lambda: self._error_cls(
                    f"{self.name} response exceeded maximum time."
                ),
                keepalive_interval_s=self.keepalive_interval_s,
                on_keepalive=lambda: gate.emit("keepalive"),
            )
            while True:
                get_task = asyncio.ensure_future(self._event_queue.get())
                which = await await_activity(clock, {"event": get_task})
                if which == KEEPALIVE:
                    get_task.cancel()
                    continue
                event = self.spec.translate(get_task.result())
                clock.note_activity()
                if event.error:
                    raise self._error_cls(event.error)
                if event.kind == "thinking":
                    gate.fire("thinking")
                elif event.kind == "text":
                    gate.fire("text")
                    for sentence in streamer.feed(event.text):
                        clock.note_audible()
                        yield sentence
                elif event.kind == "tool":
                    gate.fire("tool")
                    kind, detail = narrate_tool(
                        event.tool_name, event.tool_args, self.spec.vocab
                    )
                    # Detail BEFORE the kind event (same-tick narration).
                    if detail:
                        gate.emit(f"tooldetail:{detail}")
                        clock.note_audible()
                    gate.emit(f"tool:{kind}")
                if event.terminal:
                    break

            tail = streamer.flush()
            if tail:
                yield tail

    async def abort(self) -> None:
        async with self._lock:
            await self._terminate()

    async def warmup(self) -> None:
        await self.respond("Say OK.", timeout=30.0)
