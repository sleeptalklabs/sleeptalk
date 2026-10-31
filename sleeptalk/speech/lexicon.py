"""TTS pronunciation lexicon.

Loads tts_lexicon.json and rewrites dev terms, extensions, and acronyms
into the forms a human would actually say, so spoken replies stop sounding
like someone spelling aloud. Applied at every TTS text entry point.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

_LEXICON_PATH = Path(__file__).with_name("tts_lexicon.json")

_IPV4_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")
_DECIMAL_RE = re.compile(r"^\d+\.\d+$")
_SEMVER_RE = re.compile(r"^v?\d+\.\d+\.\d+$")
_DOTTED_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9_/])"
    r"([A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+)"
    r"(?![A-Za-z0-9_/])"
)
_SPOKEN_HOST_RE = re.compile(r"\w+(?:\s+dot\s+\w+)+")


def _load_sections() -> dict[str, dict[str, str]]:
    try:
        data = json.loads(_LEXICON_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    sections: dict[str, dict[str, str]] = {}
    for section, entries in data.items():
        if section.startswith("_") or not isinstance(entries, dict):
            continue
        cleaned = {
            term: spoken
            for term, spoken in entries.items()
            if isinstance(term, str) and isinstance(spoken, str)
        }
        if cleaned:
            sections[section] = cleaned
    return sections


def _merge(*groups: dict[str, str]) -> dict[str, str]:
    merged: dict[str, str] = {}
    for group in groups:
        merged.update(group)
    return merged


def _build_pattern(terms: list[str]) -> re.Pattern[str] | None:
    if not terms:
        return None
    parts = []
    for term in sorted(terms, key=len, reverse=True):
        escaped = re.escape(term)
        if term.startswith("."):
            parts.append(rf"(?<!\.){escaped}\b")
        else:
            parts.append(rf"\b{escaped}\b")
    return re.compile("|".join(parts), re.IGNORECASE)


def _lookup(mapping: dict[str, str]) -> dict[str, str]:
    return {term.lower(): spoken for term, spoken in mapping.items()}


_SECTIONS = _load_sections()
_PROJECT = _SECTIONS.get("project_specific", {})
_EXTENSIONS = _SECTIONS.get("extensions", {})
_STANDALONE = _merge(
    _SECTIONS.get("formats_and_protocols", {}),
    _SECTIONS.get("tools_and_names", {}),
    _SECTIONS.get("dev_words", {}),
    _SECTIONS.get("commands", {}),
)
_EXTENSION_SUFFIXES = tuple(
    sorted((ext.lower() for ext in _EXTENSIONS), key=len, reverse=True)
)

_PROJECT_PATTERN = _build_pattern(list(_PROJECT))
_PROJECT_LOOKUP = _lookup(_PROJECT)
_EXT_PATTERN = _build_pattern(list(_EXTENSIONS))
_EXT_LOOKUP = _lookup(_EXTENSIONS)
_STANDALONE_PATTERN = _build_pattern(list(_STANDALONE))
_STANDALONE_LOOKUP = _lookup(_STANDALONE)


def _stash(spoken: str, parts: list[str]) -> str:
    token = f"\x00p{len(parts)}\x00"
    parts.append(spoken)
    return token


def _restore(text: str, parts: list[str]) -> str:
    for index, spoken in enumerate(parts):
        text = text.replace(f"\x00p{index}\x00", spoken)
    return text


def _spoken_for(match: re.Match[str], lookup: dict[str, str]) -> str | None:
    spoken = lookup.get(match.group(0).lower())
    if spoken is None:
        return None
    if match.group(0).startswith("."):
        preceding = match.string[match.start() - 1] if match.start() else " "
        return spoken if preceding.isspace() else f" {spoken}"
    return spoken


def _apply_terms(
    text: str,
    pattern: re.Pattern[str] | None,
    lookup: dict[str, str],
    parts: list[str],
) -> str:
    if pattern is None:
        return text

    def _sub(match: re.Match[str]) -> str:
        spoken = _spoken_for(match, lookup)
        if spoken is None:
            return match.group(0)
        return _stash(spoken, parts)

    return pattern.sub(_sub, text)


def _is_hostname(token: str) -> bool:
    if not any(char.isalpha() for char in token):
        return False
    if _IPV4_RE.fullmatch(token):
        return False
    if _DECIMAL_RE.fullmatch(token):
        return False
    if _SEMVER_RE.fullmatch(token):
        return False
    lowered = token.lower()
    for suffix in _EXTENSION_SUFFIXES:
        if lowered.endswith(suffix) and len(token) > len(suffix):
            return False
    return True


def _speak_dots(token: str) -> str:
    return " ".join(token.replace(".", " dot ").split())


def _apply_hostnames(text: str, parts: list[str]) -> str:
    def _sub(match: re.Match[str]) -> str:
        token = match.group(1)
        if not _is_hostname(token):
            return token
        return _stash(_speak_dots(token), parts)

    return _DOTTED_TOKEN_RE.sub(_sub, text)


def _protect_spoken_hosts(text: str, parts: list[str]) -> str:
    return _SPOKEN_HOST_RE.sub(lambda match: _stash(match.group(0), parts), text)


def apply_pronunciation(text: str, language: str = "") -> str:
    """Rewrite lexicon terms in text into their spoken forms.

    The spoken forms are English ("ay pee eye", "dot com"): a voice speaking
    another language reads them as its own letters, so for any language but
    English the text goes through unchanged except for underscores.
    """
    if not text:
        return text
    primary = (language or "").strip().lower().replace("_", "-").split("-", 1)[0]
    if primary not in ("", "en"):
        return text.replace("_", " ")

    parts: list[str] = []
    rewritten = _apply_terms(text, _PROJECT_PATTERN, _PROJECT_LOOKUP, parts)
    rewritten = _apply_hostnames(rewritten, parts)
    rewritten = _apply_terms(rewritten, _EXT_PATTERN, _EXT_LOOKUP, parts)
    rewritten = _protect_spoken_hosts(rewritten, parts)
    rewritten = _apply_terms(rewritten, _STANDALONE_PATTERN, _STANDALONE_LOOKUP, parts)
    rewritten = rewritten.replace("_", " ")
    return _restore(rewritten, parts)
