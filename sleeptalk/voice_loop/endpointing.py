from __future__ import annotations

import re
from dataclasses import dataclass
from sleeptalk.voice.configs import (  # noqa: F401 — re-export
    AdaptiveEndpointingConfig,
    TurnCompletionSignal,
)





@dataclass(frozen=True)
class EndpointDecision:
    should_endpoint: bool
    silence_ms: int
    threshold_ms: int
    mode: str
    semantic_score: float
    filler_detected: bool
    reason: str
    source: str = ""
    transcript: str = ""


_WORD_RE = re.compile(r"[a-z0-9']+")
# A finished sentence's end mark, in the scripts the page speaks.
_SENTENCE_ENDS = ("?", "!", ".", "？", "！", "。", "।", "｡")
_TRAILING_FILLER_RE = re.compile(
    r"(?:^|\b)(?:uh+|um+|erm+|hmm+|mm+|like|so|well|and|but|or|because|cause|"
    r"basically|actually|you know|i mean|hold on|one sec|wait|"
    # Adverbs that trail a clause the speaker clearly intends to continue:
    # "and, also", "but anyway", "plus" are floor holders, not endings.
    r"also|anyway|anyways|literally|plus)(?:[\s,.!?-]*)$",
    re.IGNORECASE,
)
_TRAILING_INCOMPLETE_WORDS = {
    "a",
    "an",
    "the",
    "to",
    "for",
    "with",
    "without",
    "of",
    "in",
    "on",
    "at",
    "from",
    "into",
    "and",
    "but",
    "or",
    "so",
    "because",
    "cause",
    "if",
    "when",
    "while",
    "that",
    "which",
    "who",
    "what",
    "where",
    "how",
    "is",
    "are",
    "was",
    "were",
    "be",
    "being",
    # More prepositions and possessives a thought doesn't end on
    # ("I'd like to know about", "sort them by", "better than your").
    "about",
    "by",
    "through",
    "than",
    "your",
    "our",
    "their",
    "its",
    # Subject pronouns and possessives almost never end a thought:
    # "what should we", "can you tell my", "did they" all trail off here.
    "i",
    "we",
    "they",
    "he",
    "she",
    "my",
    "our",
    "your",
    "his",
    "her",
    "their",
    "its",
}
_TRAILING_UNCERTAIN_WORDS = {
    # Auxiliaries/modals are common cut points in an open request ("so I
    # could", "what I did"), but they can also end a terse answer ("I
    # could."). Give them the uncertain budget rather than the much slower
    # incomplete-clause budget. This still holds well past the ~955ms
    # production cutoffs without adding 3.5s to legitimate short answers.
    "can",
    "could",
    "will",
    "would",
    "shall",
    "should",
    "may",
    "might",
    "must",
    "do",
    "does",
    "did",
    "have",
    "has",
    "had",
    "not",
}
_QUESTION_STARTERS = {
    "who",
    "what",
    "when",
    "where",
    "why",
    "how",
    "can",
    "could",
    "would",
    "should",
    "do",
    "does",
    "did",
    "is",
    "are",
    "was",
    "were",
}
_SHORT_COMPLETE_UTTERANCES = {
    "yes",
    "yeah",
    "yep",
    "no",
    "nope",
    "okay",
    "ok",
    "sure",
    "thanks",
    "thank you",
    "right",
    "correct",
}


def decide_endpoint(
    *,
    signal: TurnCompletionSignal | str | None,
    speech_duration_ms: int,
    silence_ms: int,
    base_silence_ms: int,
    config: AdaptiveEndpointingConfig | None = None,
) -> EndpointDecision:
    config = config or AdaptiveEndpointingConfig()
    if isinstance(signal, str):
        signal = TurnCompletionSignal(transcript=signal)
    signal = signal or TurnCompletionSignal()
    mode = turn_mode(speech_duration_ms, config)
    transcript = _clean_text(signal.transcript)

    if not config.enabled:
        threshold_ms = max(base_silence_ms, 80)
        return EndpointDecision(
            should_endpoint=silence_ms >= threshold_ms,
            silence_ms=silence_ms,
            threshold_ms=threshold_ms,
            mode=mode,
            semantic_score=0.0,
            filler_detected=False,
            reason="fixed_silence",
            source=signal.source,
            transcript=transcript,
        )

    filler_detected = has_floor_holder(transcript)
    score = signal.score
    if score is None:
        score = estimate_completion_score(transcript)
    # `speech_final` is an acoustic pause hint, not proof that the thought is
    # semantically complete. Do not promote a middling transcript into the
    # fast bucket merely because the STT heard silence. The local silence
    # clock already accounts for that evidence; double-counting it caused
    # ordinary hesitant pauses to commit prefixes as complete turns.
    threshold_ms, reason = _threshold_for(
        score=float(score),
        mode=mode,
        filler_detected=filler_detected,
        base_silence_ms=base_silence_ms,
        config=config,
        has_transcript=bool(transcript),
        has_signal_source=bool(signal.source),
        speech_duration_ms=speech_duration_ms,
    )
    if (config.local_streaming_input and signal.source == "moonshine_streaming"
            and signal.is_final and transcript and not filler_detected):
        # A finished acoustic segment is useful evidence, not semantic VAD.
        # Keep extra room for incomplete clauses, and never bypass floor holders.
        cap = (config.local_complete_cap_ms if float(score) >= 0.55
               else config.local_incomplete_cap_ms)
        threshold_ms = min(threshold_ms, max(base_silence_ms, cap))
        reason = "local_stream_final"
    return EndpointDecision(
        should_endpoint=silence_ms >= threshold_ms,
        silence_ms=silence_ms,
        threshold_ms=threshold_ms,
        mode=mode,
        semantic_score=round(float(score), 4),
        filler_detected=filler_detected,
        reason=reason,
        source=signal.source,
        transcript=transcript,
    )


def turn_mode(speech_duration_ms: int, config: AdaptiveEndpointingConfig | None = None) -> str:
    config = config or AdaptiveEndpointingConfig()
    if speech_duration_ms < config.responsive_mode_ms:
        return "responsive"
    if speech_duration_ms >= config.monologue_mode_ms:
        return "monologue"
    return "balanced"


def has_floor_holder(transcript: str) -> bool:
    text = _clean_text(transcript)
    if not text:
        return False
    if text.endswith(("...", "-", "--")):
        return True
    if _TRAILING_FILLER_RE.search(text):
        return True
    return bool(re.search(r"\b[a-z]*[aeiou]{3,}[a-z]*$", text, re.IGNORECASE))


def estimate_completion_score(transcript: str) -> float:
    text = _clean_text(transcript)
    if not text:
        return 0.0
    if has_floor_holder(text):
        return 0.18
    words = _WORD_RE.findall(text.lower())
    if not words:
        # Chinese, Japanese, Korean, Hindi, ...: no Latin words to weigh, so
        # the end mark decides. Scoring these 0.0 held every such turn for
        # the incomplete budget (3.5 s) however finished it sounded.
        if text.endswith(_SENTENCE_ENDS):
            return 0.92
        return 0.66 if any(ch.isalnum() for ch in text) else 0.0
    joined = " ".join(words)
    if joined in _SHORT_COMPLETE_UTTERANCES:
        return 0.95
    # Check lexical incompleteness before punctuation. Streaming STT often
    # adds a period to a stable segment such as "so I could." even though the
    # speaker has not completed the request.
    if words[-1] in _TRAILING_INCOMPLETE_WORDS:
        return 0.28
    if words[-1] in _TRAILING_UNCERTAIN_WORDS:
        return 0.58
    if text.endswith(_SENTENCE_ENDS):
        return 0.92
    if text.endswith((",", ":", ";")):
        return 0.32
    # A question starter alone says nothing about whether the *end* is
    # complete ("Can you make it so I could"). Without terminal punctuation,
    # keep the normal uncertain budget instead of forcing the fast bucket.
    if words[0] in _QUESTION_STARTERS and len(words) >= 2:
        return 0.66
    if len(words) <= 2:
        return 0.58
    return 0.66


def _threshold_for(
    *,
    score: float,
    mode: str,
    filler_detected: bool,
    base_silence_ms: int,
    config: AdaptiveEndpointingConfig,
    has_transcript: bool,
    has_signal_source: bool = False,
    speech_duration_ms: int = 0,
) -> tuple[int, str]:
    base_silence_ms = max(base_silence_ms, 80)
    if filler_detected:
        threshold = max(base_silence_ms, config.filler_reset_ms)
        if mode == "monologue":
            threshold = max(threshold, config.monologue_incomplete_silence_ms)
        return threshold, "floor_holder"
    if not has_transcript:
        # No transcript signal at endpoint-check time (streaming STT interim
        # still in flight, or provider gave nothing). A bare acoustic
        # threshold here cuts people mid-thought the moment they pause,
        # because the VAD stop already guarantees base_silence_ms of quiet.
        # When a transcript source exists, the transcript is EXPECTED — its
        # absence means the STT is lagging, so wait the "uncertain" budget
        # to let an interim land and re-gate (production traces showed real
        # utterances flushed at 450ms with transcriptChars=0).
        if has_signal_source:
            return max(base_silence_ms, config.uncertain_silence_ms), "acoustic_fallback"
        # No source at all: acoustic-only pipeline. Give sustained speech
        # the same patience; keep blips snappy.
        if speech_duration_ms >= 1_500:
            return max(base_silence_ms, config.uncertain_silence_ms), "acoustic_fallback"
        return base_silence_ms, "acoustic_fallback"
    if score >= config.completion_threshold:
        if mode == "monologue":
            return max(base_silence_ms, config.monologue_complete_silence_ms), "complete"
        if mode == "balanced":
            return max(base_silence_ms, config.balanced_complete_silence_ms), "complete"
        if has_signal_source:
            return max(base_silence_ms, config.semantic_complete_silence_ms), "complete"
        return max(base_silence_ms, config.complete_silence_ms), "complete"
    if score >= 0.55:
        return max(base_silence_ms, config.uncertain_silence_ms), "uncertain"
    if mode == "monologue":
        return max(base_silence_ms, config.monologue_incomplete_silence_ms), "incomplete"
    return max(base_silence_ms, config.incomplete_silence_ms), "incomplete"


def _clean_text(text: str) -> str:
    return " ".join(str(text or "").strip().split())
