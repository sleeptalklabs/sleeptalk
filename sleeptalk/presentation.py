"""Display-only conversation feed for web and terminal.

A reducer turns session events into one ``PresentationState``. Surfaces
render the snapshot. Nothing here writes audio or cancels a turn.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, replace
from typing import Any, Literal

from sleeptalk.session_events import SessionEvent, SessionEventType

Presence = Literal["idle", "listening", "thinking", "speaking"]
LineKind = Literal["user", "assistant", "notes", "tool", "progress", "worker"]

_MAX_LINES = 80
_MS_PER_CHAR = 70
_MS_PER_WORD = 90
_MS_MIN_WORD = 140
_SENTENCE_END = re.compile(r"[.?!][\"']?$")
_JOIN_STOP = frozenset(
    {"a", "an", "the", "to", "for", "of", "in", "on", "at", "by", "or", "and", "is", "it", "we", "you", "i"}
)


@dataclass(frozen=True)
class Word:
    text: str
    start_ms: int | None = None
    end_ms: int | None = None


@dataclass(frozen=True)
class Line:
    kind: LineKind
    text: str
    words: tuple[Word, ...] = ()
    spoken_index: int = 0
    call_id: str | None = None
    phase: str | None = None
    plus: int | None = None
    minus: int | None = None
    # A background job's line: tool calls so far, and links it reported.
    steps: int | None = None
    links: tuple[str, ...] = ()
    # A reply shortened for speech: ``text`` is what was said, this is the
    # whole answer the agent wrote, for the page's "Full answer".
    full: str = ""

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"kind": self.kind, "text": self.text}
        if self.kind == "assistant":
            if self.full:
                payload["full"] = self.full
            # The page needs a reply's words only where they differ from its
            # text. Their timings drive the terminal's spoken-word clock, not
            # the page, and sending them made a long call's snapshot outgrow
            # the data channel.
            words = [word.text for word in self.words]
            if words != self.text.split():
                payload["words"] = [{"text": word} for word in words]
            payload["spokenIndex"] = self.spoken_index
        if self.call_id is not None:
            payload["callId"] = self.call_id
        if self.phase is not None:
            payload["phase"] = self.phase
        if self.plus is not None:
            payload["plus"] = self.plus
        if self.minus is not None:
            payload["minus"] = self.minus
        if self.steps is not None:
            payload["steps"] = self.steps
        if self.links:
            payload["links"] = list(self.links)
        return payload


@dataclass(frozen=True)
class WorkerEntry:
    """One item in a background job's own feed (the worker panel)."""

    kind: str  # thinking | step | said | note | ask | done | stopped
    text: str
    steps: int | None = None
    started_at: int | None = None  # epoch ms, for the live timer
    links: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"kind": self.kind, "text": self.text}
        if self.steps is not None:
            payload["steps"] = self.steps
        if self.started_at is not None:
            payload["startedAt"] = self.started_at
        if self.links:
            payload["links"] = list(self.links)
        return payload


@dataclass(frozen=True)
class WorkerJob:
    """A background job as the worker panel shows it."""

    job_id: str
    title: str
    phase: str = "running"  # running | completed | failed | cancelled | replaced
    started_at: int | None = None
    steps: int = 0
    waiting: str = ""  # a permission or question it is waiting on
    entries: tuple[WorkerEntry, ...] = ()

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "jobId": self.job_id,
            "title": self.title,
            "phase": self.phase,
            "steps": self.steps,
            "entries": [entry.to_json() for entry in self.entries],
        }
        if self.started_at is not None:
            payload["startedAt"] = self.started_at
        if self.waiting:
            payload["waiting"] = self.waiting
        return payload


@dataclass(frozen=True)
class PresentationState:
    presence: Presence = "idle"
    lines: tuple[Line, ...] = ()
    muted: bool = False
    hearing: bool = False
    partial: str = ""
    latency_ms: int | None = None
    stack: tuple[str, ...] = ()
    speaker: str = "agent"
    tts_speed: float = 1.0
    narration_style: str = "narrated"
    reply_length: str = "full"
    tts_provider: str = ""
    tts_voice_id: str = ""
    tts_language: str = ""
    voice_notice: str = ""
    # Whether this call can open the full settings page on the host, and
    # what happened the last time the page asked.
    settings_available: bool = False
    settings_notice: str = ""
    # A single-use link to the settings page for the caller's device, once asked for.
    settings_url: str = ""
    diagnostics_enabled: bool = False
    # Background jobs, each with its own feed: the worker panel, kept apart
    # from the conversation (which shows only a job's milestones).
    jobs: tuple[WorkerJob, ...] = ()
    # The job the caller's bare words go to, as the host keeps it: the page
    # shows its tab. A new job never takes it (docs/plans/concurrent-background-jobs.md).
    selected_job: str = ""

    def voice_json(self) -> dict[str, Any] | None:
        if not self.tts_provider:
            return None
        from sleeptalk.voice_catalog import catalog_payload, find

        catalog = catalog_payload((self.tts_provider,), self.tts_language).get(self.tts_provider, {"voices": [], "library_url": ""})
        known = find(self.tts_provider, self.tts_voice_id)
        return {
            "provider": self.tts_provider,
            "voiceId": self.tts_voice_id,
            "name": known.name if known else ("Custom" if self.tts_voice_id else ""),
            "curated": catalog["voices"],
            "libraryUrl": catalog["library_url"],
            "notice": self.voice_notice,
        }

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "presence": self.presence,
            "lines": [line.to_json() for line in self.lines],
            "muted": self.muted,
            "hearing": self.hearing,
            "partial": self.partial,
            "stack": list(self.stack),
            "speaker": self.speaker,
            "ttsSpeed": self.tts_speed,
            "narrationStyle": self.narration_style,
            "replyLength": self.reply_length,
            "diagnostics": self.diagnostics_enabled,
        }
        if self.latency_ms is not None:
            payload["latencyMs"] = self.latency_ms
        voice = self.voice_json()
        if voice is not None:
            payload["voice"] = voice
        payload["settings"] = {"available": self.settings_available, "notice": self.settings_notice,
                               **({"url": self.settings_url} if self.settings_url else {})}
        if self.jobs:
            payload["jobs"] = [job.to_json() for job in self.jobs]
            if self.selected_job:
                payload["selectedJob"] = self.selected_job
        return payload


# A finished job keeps this many of its entries when a snapshot must shrink.
_FINISHED_JOB_KEEP = 20


def fit_snapshot(payload: dict[str, Any], max_bytes: int) -> dict[str, Any]:
    """``payload`` (a ``to_json`` snapshot) cut to encode within ``max_bytes``.

    A long call outgrew the page's data channel, and an oversized snapshot
    was dropped, so the page stopped updating for the rest of the call.
    Finished jobs give up their older entries first, then the oldest
    conversation lines go, then a running job's oldest entries: the newest
    of each, which the page shows, stay.
    """
    if _encoded_size(payload) <= max_bytes:
        return payload
    out = dict(payload)
    jobs = [dict(job) for job in payload.get("jobs") or ()]
    for job in jobs:
        if job.get("phase") not in {"running", "starting"}:
            job["entries"] = list(job.get("entries") or ())[-_FINISHED_JOB_KEEP:]
    if jobs:
        out["jobs"] = jobs
    over = _encoded_size(out) - max_bytes
    lines = list(payload.get("lines") or ())
    while over > 0 and len(lines) > 1:
        over -= _encoded_size(lines.pop(0)) + 1
    out["lines"] = lines
    for job in jobs:
        entries = list(job.get("entries") or ())
        while over > 0 and len(entries) > 1:
            over -= _encoded_size(entries.pop(0)) + 1
        job["entries"] = entries
    return out


def _encoded_size(value: Any) -> int:
    return len(json.dumps(value, separators=(",", ":")).encode())


def words_from_text(text: str) -> tuple[Word, ...]:
    """Split text and attach a speech-rate clock when Cartesia timings are absent."""
    elapsed = 0
    words: list[Word] = []
    for token in text.split():
        if not token:
            continue
        words.append(Word(token, start_ms=elapsed))
        letters = sum(1 for char in token if char.isalnum())
        elapsed += max(_MS_MIN_WORD, letters * _MS_PER_CHAR + _MS_PER_WORD)
    return tuple(words)


def words_from_cartesia_frame(message: dict[str, Any]) -> tuple[Word, ...]:
    """Parse a Cartesia timestamps websocket frame into Word timings.

    Cartesia reports start/end in seconds. Unknown shapes return empty.
    """
    block = message.get("word_timestamps")
    if not isinstance(block, dict):
        block = message.get("timestamps")
    if not isinstance(block, dict):
        return ()
    words = block.get("words")
    starts = block.get("start")
    ends = block.get("end")
    if not isinstance(words, list) or not isinstance(starts, list) or not isinstance(ends, list):
        return ()
    parsed: list[Word] = []
    for index, token in enumerate(words):
        text = str(token).strip()
        if not text:
            continue
        start = starts[index] if index < len(starts) else None
        end = ends[index] if index < len(ends) else None
        parsed.append(
            Word(
                text,
                start_ms=_seconds_to_ms(start),
                end_ms=_seconds_to_ms(end),
            )
        )
    return tuple(parsed)


def _seconds_to_ms(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(round(float(value) * 1000))
    except (TypeError, ValueError):
        return None


def tidy_spoken(text: str) -> str:
    def glue(match: re.Match[str]) -> str:
        left, right = match.group(1), match.group(2)
        if left.lower() in _JOIN_STOP:
            return f"{left} {right}"
        if left.isalpha() and right[:1].islower():
            return f"{left}{right}"
        return f"{left} {right}"

    text = re.sub(r"(\w+)\n+(\S+)", glue, text)
    return " ".join(text.split())


def normalize_spoken(text: str) -> str:
    return "".join(tidy_spoken(text).lower().split())


def apply_playback_ms(state: PresentationState, elapsed_ms: int) -> PresentationState:
    """Advance karaoke on the current assistant line. Words without timings stay dim."""
    index = _last_kind_index(state.lines, "assistant")
    if index is None:
        return state
    line = state.lines[index]
    if not line.words or all(word.start_ms is None for word in line.words):
        return state
    spoken = 0
    for word in line.words:
        if word.start_ms is None or word.start_ms > elapsed_ms:
            break
        spoken += 1
    if spoken == line.spoken_index:
        return state
    return _replace_line(state, index, replace(line, spoken_index=spoken))


def apply_sentence_lit(state: PresentationState) -> PresentationState:
    """ElevenLabs snap. Light every word on the current assistant line."""
    index = _last_kind_index(state.lines, "assistant")
    if index is None:
        return state
    line = state.lines[index]
    if line.spoken_index == len(line.words):
        return state
    return _replace_line(state, index, replace(line, spoken_index=len(line.words)))


def start_hearing(state: PresentationState) -> PresentationState:
    if state.hearing:
        return state
    return replace(state, hearing=True, presence="listening")


def reduce(state: PresentationState, event: SessionEvent) -> PresentationState:
    kind = event.type
    payload = event.payload
    if kind is SessionEventType.SESSION_STATE:
        return _session_state(state, payload)
    if kind is SessionEventType.TRANSCRIPT_PARTIAL:
        return _transcript_partial(state, payload)
    if kind is SessionEventType.TRANSCRIPT_FINAL:
        return _transcript_final(state, payload)
    if kind is SessionEventType.RESPONSE_STARTED:
        return replace(state, presence="thinking", hearing=False)
    if kind is SessionEventType.RESPONSE_FIRST_OUTPUT:
        return _first_output(state, payload)
    if kind is SessionEventType.RESPONSE_COMPLETED:
        return replace(apply_sentence_lit(state), presence="listening")
    if kind in {
        SessionEventType.RESPONSE_CANCELLED,
        SessionEventType.INTERRUPTION,
    }:
        return _cut_assistant(state)
    if kind is SessionEventType.TOOL_STARTED:
        return _tool(state, payload, phase="started", presence="thinking")
    if kind is SessionEventType.TOOL_COMPLETED:
        return _tool(state, payload, phase="completed", presence=state.presence)
    if kind is SessionEventType.TOOL_FAILED:
        return _tool(state, payload, phase="failed", presence=state.presence)
    if kind is SessionEventType.PROGRESS:
        return _progress(state, payload)
    if kind is SessionEventType.AGENT_EVENT:
        return _agent_event(state, payload)
    if kind is SessionEventType.ERROR:
        return replace(state, presence="idle", hearing=False)
    return state


def _session_state(state: PresentationState, payload: dict[str, Any]) -> PresentationState:
    raw = str(payload.get("state") or "")
    if raw == "active":
        return replace(state, presence="listening")
    if raw in {"closing", "closed"}:
        return replace(state, presence="idle", hearing=False)
    if raw == "recovering":
        return replace(state, presence="thinking")
    return state


def _latency_ms(payload: dict[str, Any]) -> int | None:
    for key in ("latency_ms", "latencyMs"):
        raw = payload.get(key)
        if raw is None:
            continue
        try:
            value = int(round(float(raw)))
        except (TypeError, ValueError):
            continue
        if value >= 0:
            return value
    return None


def _transcript_partial(state: PresentationState, payload: dict[str, Any]) -> PresentationState:
    if payload.get("source") == "openai-live" and payload.get("caption_id"):
        # Both speakers can update independently; captions are display segments,
        # never invented provider turn-completion events.
        identity = str(payload["caption_id"])
        kind = "assistant" if payload.get("speaker") == "assistant" else "user"
        text = str(payload.get("text") or "")
        line = Line(kind=kind, text=text, call_id=identity)
        lines = list(state.lines)
        index = next((i for i, old in enumerate(lines) if old.call_id == identity), None)
        if index is None:
            lines.append(line)
        else:
            lines[index] = line
        return replace(state, lines=tuple(lines[-_MAX_LINES:]), partial="")
    text = str(payload.get("text") or "").strip()
    speaker = str(payload.get("speaker") or "user")
    latency = _latency_ms(payload)
    fields: dict[str, Any] = {"presence": "listening"}
    if speaker != "assistant":
        fields["partial"] = text
    if latency is not None:
        fields["latency_ms"] = latency
    return replace(state, **fields)


def _cut_assistant(state: PresentationState) -> PresentationState:
    index = _last_kind_index(state.lines, "assistant")
    if index is None:
        return replace(state, presence="listening")
    line = state.lines[index]
    text = line.text.rstrip()
    if not text or text.endswith("—") or text.endswith("--"):
        return replace(state, presence="listening")
    return _replace_line(state, index, replace(line, text=f"{text}—"), presence="listening")


def _transcript_final(state: PresentationState, payload: dict[str, Any]) -> PresentationState:
    speaker = str(payload.get("speaker") or "")
    text = str(payload.get("text") or "").strip()
    if speaker == "user" and text:
        return _user_text(state, text)
    if speaker == "assistant" and text:
        if str(payload.get("owner") or "") == "specialist":
            return _notes_text(state, text)
        presence: Presence = "speaking" if state.presence == "speaking" else "thinking"
        # A reply's rollup repeats sentences already shown one by one; text
        # matching can't always tell (a tool line splits them, or the
        # wording was tidied), so a flagged rollup only fills in a reply
        # that showed nothing.
        if payload.get("rollup") and _assistant_since_user(state.lines):
            return replace(state, presence=presence)
        return _spoken_text(state, text, presence=presence)
    if speaker == "assistant":
        return replace(state, presence="speaking")
    return replace(state, presence="thinking", hearing=False, partial="")


def _assistant_since_user(lines: tuple[Line, ...]) -> bool:
    """Whether the current reply (everything after the last user line) has spoken text."""
    for line in reversed(lines):
        if line.kind == "user":
            return False
        if line.kind == "assistant" and line.text.strip():
            return True
    return False


def _first_output(state: PresentationState, payload: dict[str, Any]) -> PresentationState:
    text = str(payload.get("text") or "").strip()
    if text:
        return _spoken_text(state, text, presence="speaking")
    return replace(state, presence="speaking")


def _user_text(state: PresentationState, text: str) -> PresentationState:
    last = _last_kind(state.lines, "user")
    if last is not None and normalize_spoken(last.text) == normalize_spoken(text):
        return replace(state, presence="thinking", hearing=False, partial="")
    index = _last_kind_index(state.lines, "user")
    if (
        index is not None
        and not any(line.kind in {"assistant", "worker"} for line in state.lines[index + 1:])
        and _letters(text).startswith(_letters(state.lines[index].text))
    ):
        # The caller kept going after a pause ("Make a todo app." then "Make a
        # todo app. Keep it local."): one line that grows, not three.
        return _replace_line(state, index, Line(kind="user", text=text), presence="thinking")
    return replace(
        state,
        presence="thinking",
        hearing=False,
        lines=_append(state.lines, Line(kind="user", text=text)),
        partial="",
    )


def _notes_text(state: PresentationState, text: str) -> PresentationState:
    text = tidy_spoken(text)
    if not text:
        return state
    last = state.lines[-1] if state.lines else None
    if last is not None and last.kind == "notes":
        last_norm = normalize_spoken(last.text)
        new_norm = normalize_spoken(text)
        if last_norm == new_norm:
            return state
        if new_norm.startswith(last_norm) or last_norm.startswith(new_norm):
            keep = text if len(new_norm) >= len(last_norm) else last.text
            return _replace_line(state, len(state.lines) - 1, Line(kind="notes", text=keep))
    return replace(state, lines=_append(state.lines, Line(kind="notes", text=text)))


def _spoken_text(
    state: PresentationState, text: str, *, presence: Presence, lead: bool = False
) -> PresentationState:
    lead = lead or bool(text[:1].isspace())
    text = tidy_spoken(text)
    if not text or not _meaningful_norm(text):
        return replace(state, presence=presence)
    merged = _merge_assistant(state.lines, text, lead=lead)
    if merged is state.lines:
        return replace(state, presence=presence)
    last = merged[-1] if merged else None
    sealed = tuple(
        _seal_assistant(line) if line is not last else line for line in merged
    )
    return replace(state, presence=presence, lines=sealed)


def _reply_lines(lines: tuple[Line, ...]) -> list[int]:
    """Indexes of the current reply's assistant lines (since the last user line)."""
    indexes: list[int] = []
    for index in range(len(lines) - 1, -1, -1):
        if lines[index].kind == "user":
            break
        if lines[index].kind == "assistant":
            indexes.append(index)
    return indexes[::-1]


def _held_text(state: PresentationState, text: str) -> PresentationState:
    """A sentence written but not said: it joins the reply's full answer only.

    The feed shows what the caller heard; the full answer stays one tap away.
    """
    text = tidy_spoken(text)
    reply = _reply_lines(state.lines)
    if not text or not reply:
        # Nothing was said yet to hang it on (cannot happen: the budget
        # always speaks the first sentence). Show it rather than lose it.
        return _spoken_text(state, text, presence=state.presence, lead=True)
    last = state.lines[reply[-1]]
    base = last.full or " ".join(state.lines[i].text for i in reply)
    return _replace_line(state, reply[-1], replace(last, full=tidy_spoken(f"{base} {text}")))


def _keep_full(before: PresentationState, after: PresentationState) -> PresentationState:
    """Merging rebuilds reply lines; the full answer moves to the new last one."""
    old = _reply_lines(before.lines)
    full = before.lines[old[-1]].full if old else ""
    new = _reply_lines(after.lines)
    if not full or not new:
        return after
    lines = list(after.lines)
    for index in new[:-1]:
        if lines[index].full:
            lines[index] = replace(lines[index], full="")
    lines[new[-1]] = replace(lines[new[-1]], full=lines[new[-1]].full or full)
    return replace(after, lines=tuple(lines))


def _meaningful_norm(text: str) -> str:
    norm = normalize_spoken(text)
    return norm if any(char.isalnum() for char in norm) else ""


def _is_assistant_rollup(trailing: tuple[Line, ...], text: str) -> bool:
    """True when ``text`` is cascade ``_commit_played`` joining streamed sentences."""
    new_norm = _meaningful_norm(text)
    parts = [_meaningful_norm(line.text) for line in trailing]
    parts = [part for part in parts if part]
    if not new_norm or len(parts) < 2:
        return False
    cursor = 0
    for part in parts:
        at = new_norm.find(part, cursor)
        if at < 0:
            return False
        cursor = at + len(part)
    return True


def _merge_assistant(
    lines: tuple[Line, ...], text: str, *, lead: bool = False
) -> tuple[Line, ...]:
    new_norm = normalize_spoken(text)
    start = len(lines)
    while start > 0 and lines[start - 1].kind == "assistant":
        start -= 1
    trailing = lines[start:]
    rest = lines[:start]
    if not trailing:
        return _append(lines, _assistant_line(text))
    if _is_assistant_rollup(trailing, text):
        return lines
    last = trailing[-1]
    last_norm = normalize_spoken(last.text)
    if last_norm == new_norm:
        return lines
    if new_norm.startswith(last_norm) or last_norm.startswith(new_norm):
        keep = text if len(new_norm) >= len(last_norm) else last.text
        return (*rest, *trailing[:-1], _assistant_line(keep, spoken_index=last.spoken_index))
    if new_norm and new_norm in last_norm:
        return lines
    concat_norm = "".join(normalize_spoken(line.text) for line in trailing)
    if (
        new_norm == concat_norm
        or new_norm.startswith(concat_norm)
        or concat_norm.startswith(new_norm)
        or concat_norm in new_norm
        or new_norm in concat_norm
    ):
        keep = text if len(new_norm) >= len(concat_norm) else tidy_spoken(
            " ".join(line.text for line in trailing)
        )
        return (*rest, _assistant_line(keep))
    if text[:1].islower() or not _complete(last.text):
        return (
            *rest,
            *trailing[:-1],
            _assistant_line(_join_fragments(last.text, text, lead=lead)),
        )
    if _complete(text):
        return _append(tuple(_seal_assistant(line) for line in lines), _assistant_line(text))
    return _append(lines, _assistant_line(text))


def _assistant_line(text: str, *, spoken_index: int = 0) -> Line:
    words = words_from_text(text)
    return Line(kind="assistant", text=text, words=words, spoken_index=spoken_index)


def _complete(text: str) -> bool:
    stripped = text.rstrip()
    if _SENTENCE_END.search(stripped):
        return True
    return len(stripped.split()) >= 4


def _join_fragments(prev: str, nxt: str, *, lead: bool = False) -> str:
    prev = prev.rstrip()
    nxt = nxt.lstrip()
    if not prev:
        return nxt
    last_tok = prev.split()[-1]
    # Mid-word wraps have no leading space ("Everyt" + "hing's"). A new
    # word already carries its space; stripping it and gluing produces
    # "runningasthepi".
    if (
        not lead
        and last_tok.isalpha()
        and nxt[:1].islower()
        and last_tok.lower() not in _JOIN_STOP
    ):
        return tidy_spoken(prev + nxt)
    return tidy_spoken(f"{prev} {nxt}")


def _seal_assistant(line: Line) -> Line:
    if line.kind != "assistant":
        return line
    return replace(line, spoken_index=len(line.words))


def _letters(text: str) -> str:
    """Letters and digits only: "local." and "local," are the same words."""
    return re.sub(r"[\W_]+", "", text.lower())


def _progress(state: PresentationState, payload: dict[str, Any]) -> PresentationState:
    text = str(payload.get("text") or payload.get("detail") or "").strip()
    if not text:
        # Nothing to say (a bare "working" line said nothing the orb doesn't).
        return replace(state, presence="thinking")
    return replace(
        state,
        presence="thinking",
        lines=_append(state.lines, Line(kind="progress", text=text)),
    )


def _agent_event(state: PresentationState, payload: dict[str, Any]) -> PresentationState:
    name = str(payload.get("name") or "").lower()
    text = str(payload.get("text") or "")
    if name == "full_answer" and text.strip():
        reply = _reply_lines(state.lines)
        if reply:
            last = state.lines[reply[-1]]
            return _replace_line(state, reply[-1], replace(last, full=text))
        # Even an answer containing only code or a link must stay available.
        return replace(state, lines=_append(state.lines, Line(kind="assistant", text="", full=text)))
    if name == "sentence" and text.strip():
        if str(payload.get("owner") or "") == "specialist":
            return _notes_text(state, text)
        presence: Presence = "speaking" if state.presence == "speaking" else "thinking"
        if payload.get("held"):
            return _held_text(state, text)
        if payload.get("summary"):
            # The one line spoken for everything held back: marked as a jump.
            text = f"… {text.strip()}"
        # Pieces the voice pipeline marks as cut at a space (sentences, or
        # the shorter phrases local voices and Snappy release) keep it when
        # joined ("work" + "in the" was shown as "workin the"); unmarked
        # chunks may split a word and are glued as before.
        return _keep_full(state, _spoken_text(state, text, presence=presence, lead=bool(payload.get("spaced"))))
    if name.startswith("job."):
        return _worker(state, name[4:], payload)
    if name in {"thinking", "text"}:
        return replace(state, presence="thinking")
    return state


# A job's feed keeps this many entries; a call keeps this many jobs.
_WORKER_ENTRIES = 200
# Tabs kept: three live, queued ones, and finished ones (dropped oldest first).
_WORKER_JOBS = 8
_WORKER_LIVE = frozenset({"running", "queued"})
_JOB_ENDINGS = {"completed": "completed", "failed": "failed", "cancelled": "cancelled", "replaced": "replaced"}


def _fit_jobs(jobs: list[WorkerJob]) -> list[WorkerJob]:
    """At most _WORKER_JOBS tabs: finished jobs go first, oldest first; a
    running or queued job is never dropped for one that ended."""
    jobs = list(jobs)
    while len(jobs) > _WORKER_JOBS:
        ended = next((index for index, job in enumerate(jobs) if job.phase not in _WORKER_LIVE), 0)
        jobs.pop(ended)
    return jobs


def _placeholder_for(shown: str, step: str) -> bool:
    """Whether ``shown`` is a tool call still being written ("write_file ·
    writing…") and ``step`` is that call starting."""
    tool, _, rest = shown.partition(" · ")
    return rest == "writing…" and (step == tool or step.startswith(f"{tool} · ")) and step != shown


def _worker(state: PresentationState, event: str, payload: dict[str, Any]) -> PresentationState:
    """A background job's progress. Its thinking, steps and narration go to
    the job's own feed (the worker panel); the conversation gets only what
    concerns the caller: it started, it needs them, it ended. Neither ever
    changes presence: the call may be idle while the job works."""
    job_id = str(payload.get("jobId") or "") or "job"
    title = str(payload.get("title") or "background job")
    jobs = list(state.jobs)
    index = next((i for i, job in enumerate(jobs) if job.job_id == job_id), None)
    if index is None:
        started = payload.get("startedAt")
        jobs.append(WorkerJob(job_id=job_id, title=title,
                              started_at=int(started) if isinstance(started, (int, float)) else None))
        index = len(jobs) - 1
    job = jobs[index]
    entries = list(job.entries)
    fields: dict[str, Any] = {}
    milestone: Line | None = None
    started_at = payload.get("startedAt")
    started_at = int(started_at) if isinstance(started_at, (int, float)) else None
    if event == "selected":
        return replace(state, selected_job=job_id, jobs=tuple(_fit_jobs(jobs)))
    if event == "queued":
        fields.update(phase="queued", waiting="")
        milestone = Line(kind="worker", text=f"Queued: {title}.", phase="queued", call_id=job_id)
    elif event == "started":
        milestone = Line(kind="worker", text=f"Working on {title}.", phase="started", call_id=job_id)
        # A follow-up reuses its finished job's id: that tab is running again.
        fields.update(phase="running", waiting="")
        if started_at is not None:
            fields["started_at"] = started_at
    elif event in {"step", "thinking"}:
        text = " ".join(str(payload.get("text") or "").split())
        steps = int(payload.get("steps") or 0)
        if not text:
            return state
        entry = WorkerEntry(kind=event, text=text, steps=steps or None, started_at=started_at)
        last = entries[-1] if entries else None
        if last is not None and last.kind == event and (
            event == "thinking" or last.text == text or _placeholder_for(last.text, text)
        ):
            # The same thought growing, the same step again, or the real
            # step taking the place of "terminal · writing…".
            entries[-1] = entry
        else:
            entries.append(entry)
        if steps:
            fields["steps"] = steps
    elif event == "said":
        text = tidy_spoken(re.sub(r"\*\*|__|`", "", str(payload.get("text") or "")))
        last = entries[-1] if entries else None
        if not text or (last is not None and last.kind == "said" and last.text.endswith(text)):
            pass
        elif last is not None and last.kind == "said" and len(last.text) + len(text) < 600:
            # One paragraph per stretch of narration, not a row per sentence.
            entries[-1] = replace(last, text=f"{last.text} {text}")
        else:
            entries.append(WorkerEntry(kind="said", text=text))
    elif event in {"steered", "note_queued"}:
        entries.append(WorkerEntry(kind="note", text=f"Your note: {payload.get('note', '')}"))
    elif event in {"permission", "question"}:
        prompt = str(payload.get("prompt") or "")
        text = f"Waiting for your OK: {prompt}" if event == "permission" else f"Asks: {prompt}"
        entries.append(WorkerEntry(kind="ask", text=text))
        fields["waiting"] = text
        milestone = Line(kind="worker", text=text, phase=event, call_id=job_id)
    elif event == "permission_resolved":
        fields["waiting"] = ""
    elif event in _JOB_ENDINGS:
        links = tuple(str(link) for link in payload.get("links") or () if str(link).strip())
        text = {
            "completed": "Done.",
            "failed": f"Stopped: {payload.get('error') or 'it failed'}.",
            "cancelled": "Cancelled.",
            "replaced": "Stopped for your newer request.",
        }[event]
        entries.append(WorkerEntry(kind="done" if event == "completed" else "stopped", text=text, links=links))
        fields.update(phase=_JOB_ENDINGS[event], waiting="")
        milestone = Line(kind="worker", text=text, phase=event, call_id=job_id, links=links)
    else:
        return state
    # Every entry carries when it happened, for the timeline's times.
    now_ms = int(time.time() * 1000)
    entries = [entry if entry.started_at is not None else replace(entry, started_at=now_ms) for entry in entries]
    jobs[index] = replace(job, entries=tuple(entries[-_WORKER_ENTRIES:]), **fields)
    state = replace(state, jobs=tuple(_fit_jobs(jobs)))
    if milestone is not None:
        state = replace(state, lines=_append(state.lines, milestone))
    return state


def _tool(
    state: PresentationState,
    payload: dict[str, Any],
    *,
    phase: str,
    presence: Presence,
) -> PresentationState:
    name = str(payload.get("name") or "tool")
    detail = str(payload.get("detail") or payload.get("result_summary") or "").strip()
    call_id = payload.get("callId") or payload.get("call_id")
    call_key = str(call_id) if call_id else None
    path = _path_from_payload(payload)
    title = name
    if path:
        title = f"{name}  {path}"
    elif detail:
        title = f"{name}  {detail}"
    line = Line(
        kind="tool",
        text=title,
        call_id=call_key,
        phase=phase,
    )
    if call_key:
        for index, existing in enumerate(state.lines):
            if existing.kind == "tool" and existing.call_id == call_key:
                return _replace_line(state, index, line, presence=presence)
    return replace(state, presence=presence, lines=_append(state.lines, line))


def _path_from_payload(payload: dict[str, Any]) -> str:
    for key in ("path", "file_path", "filePath"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    raw_input = payload.get("input")
    if isinstance(raw_input, dict):
        for key in ("path", "file_path", "filePath"):
            value = raw_input.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    detail = str(payload.get("detail") or "")
    if "/" in detail or "." in detail:
        return detail.split()[-1] if detail.split() else detail
    return ""


def _last_kind(lines: tuple[Line, ...], kind: LineKind) -> Line | None:
    index = _last_kind_index(lines, kind)
    return None if index is None else lines[index]


def _last_kind_index(lines: tuple[Line, ...], kind: LineKind) -> int | None:
    for index in range(len(lines) - 1, -1, -1):
        if lines[index].kind == kind:
            return index
    return None


def _replace_line(
    state: PresentationState,
    index: int,
    line: Line,
    *,
    presence: Presence | None = None,
) -> PresentationState:
    lines = list(state.lines)
    lines[index] = line
    return replace(
        state,
        presence=state.presence if presence is None else presence,
        lines=tuple(lines),
    )


def _append(lines: tuple[Line, ...], line: Line) -> tuple[Line, ...]:
    next_lines = (*lines, line)
    if len(next_lines) > _MAX_LINES:
        return next_lines[-_MAX_LINES:]
    return next_lines
