"""Utterance classification patterns for Discord live voice."""
from __future__ import annotations

import re

_MIN_SPEAKABLE_ALNUM = 3
_SHORT_CONFIRMATION_RE = re.compile(
    r"^(yes|yeah|yep|yup|no|nope|nah|ok|okay|thanks|thank you|stop|cancel|nevermind|never mind|sure|right|correct|exactly|nope\.)[.!]?$",
    re.IGNORECASE,
)
_BACKCHANNEL_RE = re.compile(
    r"^(?:m+[- ]?h+m+|uh[- ]?huh|yeah|yep|yup|okay|ok|right|sure|thanks|thank you)[.!]?$",
    re.IGNORECASE,
)
_CONTROL_CORE = (
    r"(?:stop|cancel|never ?mind|forget it|hold on|hold up|enough|"
    r"that'?s enough|be quiet|stop talking|wait)"
)
_CONTROL_FILLER = (
    r"(?:please|okay|ok|hey|just|now|it|pipeline|pi|bot|control|"
    r"slash|forward|command)"
)
_CONTROL_WORD_RE = re.compile(
    rf"^(?:{_CONTROL_FILLER}[\s,]+)*{_CONTROL_CORE}"
    rf"(?:[\s,]+(?:{_CONTROL_CORE}|{_CONTROL_FILLER}))*[.!]?$",
    re.IGNORECASE,
)
# Answers to the canned join offer ("Want me to run through the latest from
# your scheduled jobs?"); anything else is a normal turn.
_RESUME_YES_RE = re.compile(
    r"^(?:(?:oh|um|uh|well|okay|ok),?\s+)?(?:yes|yeah|yep|yup|sure|please|okay|ok|"
    r"go ahead|go for it|do it|sounds good|why not|absolutely|definitely)"
    r"(?:[\s,]+(?:please|sure|go ahead|do it|thanks|thank you))*[.!]?$",
    re.IGNORECASE,
)
_RESUME_NO_RE = re.compile(
    r"^(?:(?:oh|um|uh|well),?\s+)?(?:no|nope|nah|not now|not right now|no thanks|"
    r"no thank you|maybe later|later|skip it|skip|pass)"
    r"(?:[\s,]+(?:thanks|thank you|not now|maybe later))*[.!]?$",
    re.IGNORECASE,
)
_CONTINUE_RE = re.compile(
    r"^(?:please\s+)?(?:continue|go on|keep going|keep talking|carry on|"
    r"resume|finish|finish it|finish that|finish what you were saying|"
    r"you were saying|as you were|what were you saying|"
    r"continue what you were saying|tell me the rest)[.!?]?$",
    re.IGNORECASE,
)
