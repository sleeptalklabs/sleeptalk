"""Shared text-shaping helpers for voice agent adapters.

Sentence extraction and speakable filtering for streaming agent output:
agents emit text deltas; the voice runtime needs speakable sentences.
"""

from __future__ import annotations

import json
import re

CODE_BLOCK_RE = re.compile(r"```[\s\S]*?```")
INLINE_CODE_RE = re.compile(r"`([^`]+)`")
SENTENCE_END_RE = re.compile(r"^(.*?[.!?])\s*(.*)$", re.DOTALL)
CLAUSE_BOUNDARY_RE = re.compile(r"^(.{25,120}?[,;:\u2014])\s+(.*)$", re.DOTALL)
FIRST_FLUSH_MIN_CHARS = 45
# Chinese and Japanese punctuation ends a sentence or clause with no space
# after it; ASCII punctuation straight into an ideograph does too (casual
# Chinese typing often uses half-width marks).
_CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_WIDE_SENTENCE_ENDS = "。！？"
_WIDE_CLAUSE_ENDS = "，；："
_CLOSERS = "”’」』）》】\"')"
# One ideograph is about one spoken syllable: roughly five characters of
# English, so clause lengths are measured in speaking time, not characters.
_CJK_WEIGHT = 5


def _spoken_length(text: str) -> int:
    ideographs = len(_CJK_RE.findall(text))
    return len(text) - ideographs + ideographs * _CJK_WEIGHT


def _absorb_closers(text: str, end: int) -> int:
    """Extend past repeated terminators and closing quotes: 真的吗？！” stays whole."""
    while end < len(text) and (text[end] in _CLOSERS or text[end] in _WIDE_SENTENCE_ENDS + "!?"):
        end += 1
    return end
_HEADING_RE = re.compile(r"^#{1,6}\s+", re.MULTILINE)
_BOLD_RE = re.compile(r"(\*\*|__)(.+?)\1")
_ITALIC_RE = re.compile(r"(?<!\w)([*_])([^*_\s][^*_]*?)\1(?!\w)")
_SHA_RE = re.compile(
    r"\b(?=[0-9a-fA-F]*[0-9])(?=[0-9a-fA-F]*[a-fA-F])[0-9a-fA-F]{7,40}\b"
)
_LIST_MARK_RE = re.compile(r"^[\s]*[-*+]\s+", re.MULTILINE)
_HASH_LEFTOVER_RE = re.compile(r"(^|\s)#(\d+)\b")
_NUMBER_ONLY_RE = re.compile(r"^\d+\.$")


def extract_first_clause(text: str) -> tuple[str, str]:
    """Early-flush the opening clause of a long first sentence.

    Voice latency: the first TTS segment shouldn't wait for a full-sentence
    terminator. If the buffer is long and has a clause boundary, flush there.
    """
    if _spoken_length(text) < FIRST_FLUSH_MIN_CHARS:
        return "", text
    match = CLAUSE_BOUNDARY_RE.match(text)
    if match:
        clause = match.group(1).strip()
        remainder = match.group(2).strip()
        if clause:
            return clause, remainder
    for index, char in enumerate(text):
        if char not in _WIDE_CLAUSE_ENDS:
            continue
        length = _spoken_length(text[: index + 1])
        if length > 120:
            break
        if length >= 25:
            clause = text[: index + 1].strip()
            remainder = text[index + 1 :].strip()
            if clause:
                return clause, remainder
    return "", text


def extract_sentence(text: str) -> tuple[str, str]:
    """Extract the first complete sentence (ending in ., !, ?) from text.

    A period inside an identifier is not a sentence end: ``lib/content.ts``,
    ``sleep.talk``, ``asddsdsds.com``. The terminator must sit at the end of
    the text or be followed by whitespace.

    Chinese and Japanese ``。！？`` end a sentence with nothing after them, as
    does ASCII punctuation followed directly by an ideograph.

    Returns (sentence, remainder). If no complete sentence, returns ("", text).
    """
    for match in re.finditer(r"[.!?。！？]", text):
        end = match.end()
        rest = text[end:]
        wide = match.group() in _WIDE_SENTENCE_ENDS
        if not wide and rest and not rest[0].isspace():
            if match.group() == "." or not _CJK_RE.match(rest[0]):
                continue
        if wide:
            end = _absorb_closers(text, end)
            rest = text[end:]
        sentence = text[:end].strip()
        remainder = rest.strip()
        if sentence:
            return sentence, remainder
    return "", text


class SpeakableStreamFilter:
    """Incrementally strip fenced code blocks and backticks from text deltas.

    - ``` fences and their contents are dropped (tracked across delta boundaries)
    - single backticks are dropped but their contents are kept, so
      "I updated `main.py`" becomes "I updated main.py" rather than losing the word
    """

    def __init__(self) -> None:
        self._in_fence = False
        self._pending_ticks = 0

    def feed(self, delta: str) -> str:
        text = "`" * self._pending_ticks + delta
        self._pending_ticks = 0
        out: list[str] = []
        i = 0
        n = len(text)
        while i < n:
            ch = text[i]
            if ch == "`":
                j = i
                while j < n and text[j] == "`":
                    j += 1
                run = j - i
                if j == n and run < 3:
                    # Possibly a fence split across deltas; hold for next feed.
                    self._pending_ticks = run
                    break
                if run >= 3:
                    self._in_fence = not self._in_fence
                # Single/double backticks: drop the ticks, keep content.
                i = j
            else:
                if not self._in_fence:
                    out.append(ch)
                i += 1
        return "".join(out)


def shape_voice_reply(text: str) -> str:
    """Make agent text coworker-speakable: drop markdown chrome and SHAs."""
    text = _HEADING_RE.sub("", text)
    text = _BOLD_RE.sub(r"\2", text)
    text = _ITALIC_RE.sub(r"\2", text)
    # Streaming can split emphasis around a sentence/early-flush boundary.
    # Unpaired markers are formatting too; never send them to the voice.
    text = re.sub(r"[*`]+|(?<!\w)_{2,}|_{2,}(?!\w)", "", text)
    text = _LIST_MARK_RE.sub("", text)
    text = _SHA_RE.sub("", text)
    text = _HASH_LEFTOVER_RE.sub(r"\1number \2", text)
    text = " ".join(text.split()).strip()
    if _NUMBER_ONLY_RE.match(text):
        return ""
    return text


def extract_speakable_text(text: str) -> str:
    """Strip code blocks and tool markup; keep natural language for TTS."""
    text = CODE_BLOCK_RE.sub("", text)
    text = INLINE_CODE_RE.sub(r"\1", text)
    text = shape_voice_reply(text)
    if not text:
        return "I've made the changes. Let me know if you need anything else."
    return text


_MOUTH_PATHISH = re.compile(
    r"(https?://\S+)"
    r"|(\b[\w.-]+\.(ts|tsx|js|jsx|css|html|png|json|toml|py|md|com)\b)"
    r"|(\b[\w.-]+:\d+\b)"
    r"|(\.[A-Za-z]{2,}\b)"
    r"|(/[\w./-]+)"
)
_MOUTH_CODEY = re.compile(r"[{}<>;`=]")
_MOUTH_CSS_VAR = re.compile(r"--[\w-]+")
_MOUTH_FRACTION = re.compile(r"\d+\.\d+(px)?\b")
_MOUTH_MIN_LETTERS = 8


def brief_finding(text: str) -> str:
    """What the specialist found, kept whole for the mouth to reword.

    A finding is data, not a script. It reaches the realtime model as JSON,
    under instructions that already say to describe commands, paths and code
    in English rather than read them out. Running the spoken-word filter
    over it first deleted the answer instead: "Found 2 hits:
    routes/hybrid.py:925 and tests/test_hybrid_route.py:982" arrived as
    "Found 2 hits: and", and the mouth correctly reported that it could not
    tell what had happened.

    Structure still goes. A fenced block is not prose, and markdown chrome
    reads as noise — but inline code is unwrapped rather than dropped,
    because the identifier inside it is usually the whole answer.
    """
    text = CODE_BLOCK_RE.sub(" ", text)
    text = INLINE_CODE_RE.sub(r"\1", text)
    spoken = shape_voice_reply(text)
    if _prose_letters(spoken) < _MOUTH_MIN_LETTERS:
        # No sentence around the syntax: a stray fragment of someone else's
        # JSON is not a finding, however long it happens to be.
        return ""
    return spoken


def _prose_letters(text: str) -> int:
    """Letters carried by ordinary words, ignoring paths and code tokens.

    The same patterns `mouth_script` strips by, used to judge a line rather
    than to gut it, against the same bar. A path among sentences is
    information and stays; a path among braces is somebody's code and the
    line goes. Counting letters rather than words keeps a terse answer —
    the kind the brevity contract asks for — from being read as soup.
    """
    return sum(
        sum(1 for char in token if char.isalpha())
        for token in text.split()
        if not _MOUTH_PATHISH.search(token)
        and not _MOUTH_CODEY.search(token)
        and not _MOUTH_CSS_VAR.search(token)
    )


def mouth_script(text: str) -> str:
    """Coworker English for the hybrid mouth. Empty if the line is code soup.

    Specialist streams dump markdown, paths, and mid-token fragments.
    Realtime will phonetically read that and sound like another language.
    """
    text = CODE_BLOCK_RE.sub(" ", text)
    text = INLINE_CODE_RE.sub(" ", text)
    text = shape_voice_reply(text)
    words: list[str] = []
    for token in text.split():
        if (
            _MOUTH_PATHISH.search(token)
            or _MOUTH_CODEY.search(token)
            or _MOUTH_CSS_VAR.search(token)
            or _MOUTH_FRACTION.search(token)
        ):
            continue
        words.append(token)
    spoken = " ".join(words).strip()
    letters = sum(1 for char in spoken if char.isalpha())
    if letters < _MOUTH_MIN_LETTERS:
        return ""
    return spoken


def unwrap_script_transcript(text: str) -> str:
    """If Realtime spoke the tell JSON, keep the inner script line."""
    raw = " ".join(text.split())
    if not raw.startswith("{") or "script" not in raw:
        return raw
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    if isinstance(payload, dict):
        script = payload.get("script")
        if isinstance(script, str) and script.strip():
            return " ".join(script.split())
    return raw


# Hearability gate: EVERY line spoken aloud must pass this. If a composed
# line contains machine syntax (paths, code, identifiers, URLs), it must not
# reach TTS — fall back to a human canned line instead.
_HEARABLE_BAD_RE = re.compile(r"[/\\_`{}\[\]()<>|=&^~$#@]|https?://")


def is_hearable(text: str) -> bool:
    """True if text is natural spoken language a human can hear comfortably.

    Rejects paths (/ or \\), identifiers (underscores), code punctuation
    ({} [] () = | &), URLs. File names like main.py are fine — humans say
    "main dot py". This is the gate of last resort for every composed
    status line.
    """
    return bool(text.strip()) and not _HEARABLE_BAD_RE.search(text)


_SEGMENT_SPLIT_RE = re.compile(r"\s*(?:&&|\|\||\||;|>>?|<)\s*")
# Flags whose VALUE is code/payload, never speakable: -c, -e, --eval...
_PAYLOAD_FLAGS = {"-c", "-e", "--command", "--eval", "--execute", "-exec"}
_MAX_SUBJECT_WORDS = 3


def speakable_subject(text: str, limit: int = 70) -> str:
    """Summarize a tool-call subject into something speakable aloud.

    Rules of voice: never read a path (basenames only), never read code
    (python3 -c "..." becomes "a python snippet"), never read flags or
    punctuation soup. Keep the meaningful head of the command, at most a
    few words — "uv run pytest", "git status", "main.py".
    """
    # Pipelines/chains: the action is the first segment that isn't a cd.
    segment = ""
    for candidate in _SEGMENT_SPLIT_RE.split(text):
        if candidate.strip() and not candidate.strip().startswith("cd "):
            segment = candidate
            break
    words: list[str] = []
    skip_value = False  # dropped flag's value goes with it
    for token in segment.split():
        token = token.strip("'\"`")
        if not token:
            continue
        if token.startswith("-"):
            skip_value = True
            continue
        if skip_value:
            skip_value = False
            continue
        if "/" in token or "\\" in token:
            token = token.replace("\\", "/").rsplit("/", 1)[-1]
        if token:
            words.append(token)
        if len(words) >= _MAX_SUBJECT_WORDS:
            break
    if not words:
        return ""
    if words[0].startswith("python"):
        # A script speaks by its name; a bare interpreter means the payload
        # was code (python3 -c "..."), which is never read aloud.
        script = next((w for w in words[1:] if w.endswith(".py")), None)
        return (script or "a python snippet").replace("_", " ")
    cleaned = " ".join(words).replace("_", " ")
    if len(cleaned) > limit:
        cleaned = cleaned[: limit - 1].rstrip() + "\u2026"
    return cleaned
