"""Reply length: trimming a long answer down to something listenable.

Cascade speaks sentence by sentence, so a summary can never be computed
from a whole answer without first holding the whole answer back — and
holding it back spends exactly the latency that the opening-preamble gate,
the instant ack, and the smart ack all exist to protect.

So the budget guard in ``voice_policy_runtime`` speaks the opening of an
answer verbatim and hands only the overrun here, as one tail. That is what
makes the setting free: by the time a tail exists the agent has already
finished, so nothing in this module ever sits on the path to first audio.

The trim is extractive and local — no model call, no network, no key. An
agent answer opens by saying what it did and closes by saying where it
landed; the middle is the detail a listener asked not to hear. So the tail
is represented by its closing sentence.

The invariant that keeps this honest: summarized speech only ever *removes*
words, never invents them. Every line this module returns is a span of what
the agent actually said, so choosing brevity can never make the agent sound
different — only shorter.
"""

from __future__ import annotations

from collections.abc import Sequence

from sleeptalk.policy import reply_length_or_default
from sleeptalk.text import extract_first_clause, extract_sentence, shape_voice_reply

# Characters of an answer spoken verbatim before the rest is condensed.
# Three short spoken sentences — the length a realtime voice model answers in,
# and the same length BRIEF_SPEECH_CONTRACT asks the agent for. The contract
# is what usually keeps a turn this short; this is the backstop for an agent
# that answers at length anyway.
SPOKEN_BUDGET_CHARS = 200

_SENTENCE_ENDS = tuple(".!?。！？")
_CLOSERS = "”’」』）》】\"')"


def spoken_budget_chars(reply_length: object) -> int:
    """Characters to speak verbatim before condensing. 0 means no limit."""
    return 0 if reply_length_or_default(reply_length) == "full" else SPOKEN_BUDGET_CHARS


def summarize_tail(
    chunks: Sequence[str], *, budget_chars: int = SPOKEN_BUDGET_CHARS
) -> str:
    """One spoken line standing in for every sentence the budget held back.

    Empty when the tail holds nothing sayable, in which case the caller
    simply stops talking — the opening already carried the answer.

    The chunks are joined before the last sentence is found: live calls cut
    a long sentence into phrases at word boundaries, so the last chunk can
    be the back half of the closing sentence ("the branch so you can try
    it."), never a sentence of its own.
    """
    closing = _last_sentence(shape_voice_reply(" ".join(chunks)))
    if not closing:
        return ""
    if len(closing) <= budget_chars:
        return closing
    clause, _rest = extract_first_clause(closing)
    return clause or _trim_to_word(closing, budget_chars)


def ends_sentence(chunk: str) -> bool:
    """Does this spoken chunk finish a sentence, not just a phrase?

    The budget guard only stops the mouth between sentences: stopping after
    a phrase cut would leave the caller hearing half a sentence.
    """
    return chunk.rstrip().rstrip(_CLOSERS)[-1:] in _SENTENCE_ENDS


def _last_sentence(text: str) -> str:
    """The final sentence of a chunk.

    Adapters yield "sentences", but a chunk can still arrive as a whole
    paragraph, and reading a paragraph aloud would defeat the setting that
    asked for less.
    """
    remainder = text.strip()
    last = ""
    while remainder:
        sentence, rest = extract_sentence(remainder)
        if not sentence:
            return remainder
        last, remainder = sentence, rest
    return last


def _trim_to_word(text: str, limit: int) -> str:
    """Last resort for a run-on with no clause boundary to cut at.

    Cuts on a word so TTS never voices half of one, and lands the sentence
    so it does not read as trailing off. The period is punctuation, not a
    word: the spoken content is still the agent's own.
    """
    head = text[:limit].rsplit(" ", 1)[0].rstrip(" ,;:—-")
    return f"{head}." if head else text
