"""Deterministic English speech forms for the local voices.

Apply only at the audio boundary: display text and agent context keep their
original amounts, links, and formatting. No network or extra model inference.
"""
from __future__ import annotations

import re
import unicodedata
from decimal import Decimal
from urllib.parse import urlsplit

from sleeptalk.text import shape_voice_reply

_LINK = re.compile(r'\[([^\]\n]+)\]\((?:<(?:https?://|www\.)[^<>\s]+>|(?:https?://|www\.)(?:[^()\s<>]|\([^()\s]*\))+)(?:\s+"[^"]*")?\)', re.I)
_URL = re.compile(r"(?<![\w@])(?:https?://|www\.)[^\s<>\[\]`\"]+", re.I)
_MONEY = re.compile(r"(?<!\w)([$£€])\s*(-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?)([kKmMbB])?(?![\w.]\d|\w)")
_UNITS = {"$": ("dollar", "cent"), "£": ("pound", "penny"), "€": ("euro", "cent")}
_SCALE = {"k": "thousand", "m": "million", "b": "billion"}


def safe_speech_boundary(text: str, cut: int) -> bool:
    """Don't release half a streamed URL, amount, or Markdown link to TTS.

    A token at the end of a delta may still grow. Whitespace or the caller's
    end-of-turn flush resolves it; ordinary prose keeps its early flush.
    """
    for token in re.finditer(r"(?:https?://|www\.|[$£€]\s*-?\d)\S*", text, re.I):
        if token.start() < cut < token.end() or (cut == token.end() == len(text)):
            return False
    for opener in re.finditer(r"\[", text):
        start = opener.start()
        closing = text.find("]", start)
        if closing == -1 or closing == len(text) - 1:
            end = len(text) + 1
        elif text[closing + 1] == "(":
            depth = 1
            end = closing + 2
            while end < len(text) and depth:
                if text[end] == "(":
                    depth += 1
                elif text[end] == ")":
                    depth -= 1
                end += 1
            if depth:
                end = len(text) + 1
        else:
            continue
        if start < cut < end:
            return False
    return True


def _url(match: re.Match[str]) -> str:
    raw = match.group()
    address = raw.rstrip(".,;:!?)}'")
    punctuation = raw[len(address):]
    try:
        host = urlsplit(address if "://" in address else "https://" + address).hostname
    except ValueError:
        host = None
    # A malformed URL still should not turn into a long recital of syntax.
    spoken = host.removeprefix("www.").replace(".", " dot ") if host else "the link"
    return spoken + punctuation


def _money(match: re.Match[str]) -> str:
    currency, raw, scale = match.groups()
    amount = Decimal(raw.replace(",", ""))
    unit, fraction = _UNITS[currency]
    if scale:
        return f"{raw} {_SCALE[scale.lower()]} {unit}s"
    negative = "minus " if amount < 0 else ""
    amount = abs(amount)
    whole = int(amount)
    cents = int((amount - whole) * 100)
    parts = []
    if whole or not cents:
        parts.append(f"{whole} {unit if whole == 1 else unit + 's'}")
    if cents:
        plural = "pence" if fraction == "penny" else fraction + "s"
        parts.append(f"{cents} {fraction if cents == 1 else plural}")
    return negative + " and ".join(parts)


def prepare_local_speech(text: str) -> str:
    """Render common prose notation without spelling punctuation aloud.

    Preserve useful amounts and prose, then remove formatting and symbols
    that local engines otherwise spell out. Sentence punctuation still
    controls pauses; the display and agent context retain the original text.
    """
    if not any(char.isalnum() for char in text):
        return ""
    # Named links speak their label; bare links speak just the host. Do this
    # before currency/ampersands so query parameters never become speech.
    text = _LINK.sub(r"\1", text)
    text = re.sub(r"<((?:https?://|www\.)[^<>\s]+)>", r"\1", text, flags=re.I)
    text = _URL.sub(_url, text)
    text = re.sub(r"~~([^~]+)~~", r"\1", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"(?<!\w)[~≈]\s*(?=[$£€]?\s*-?\d)", "about ", text)
    text = re.sub(r"(?<!\S)~/([^\s,;]*)", lambda m: "your home folder, " + m[1].replace("/", ", "), text)
    text = _MONEY.sub(_money, text)
    text = re.sub(r"(?<=\d)\s*%", " percent", text)
    text = re.sub(r"\s+&\s+", " and ", text)
    text = shape_voice_reply(text)
    text = "".join(
        char if char in ".,!?;:'’‘-–—…" or unicodedata.category(char)[0] not in {"P", "S", "C"}
        else " "
        for char in text
    )
    text = " ".join(text.split())
    text = re.sub(r"\s+([.,!?;:])", r"\1", text)
    return text if any(char.isalnum() for char in text) else ""
