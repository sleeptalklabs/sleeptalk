"""Spoken skill invoke for every voice agent.

The catalog is whoever's SKILL.md tree is on disk. This package does not
ship a skill list. It reads the usual host folders and plugin caches, then
rewrites spoken slash or "use the X skill" to `/slug`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_FILLER = re.compile(r"(?i)^(hey|ok|okay|please|um|uh|so)\s+")
_SLASH_HEAD = re.compile(r"(?is)^(?:slash|/)\s*(\S+)(?:\s+(.*))?$")
_NATURAL_SKILL = re.compile(
    r"(?is)^(?:(?:hey|ok|okay|please|um|uh|so)\s+)*"
    r"(?:can you |could you )?(?:please )?"
    r"(?:use|run|invoke)\s+(?:the\s+)?"
    r"(?:skill\s+(?P<after_skill>.+)|(?P<before_skill>.+?)\s+skill(?:\s+(?P<after_name>.+))?)"
    r"$"
)
_SKILL_NAME = re.compile(r"(?m)^name:\s*(.+?)\s*$")

_PROJECT_SKILL_DIRS: tuple[tuple[str, ...], ...] = (
    (".cursor", "skills"),
    (".cursor", "skills-cursor"),
    (".agents", "skills"),
    (".claude", "skills"),
    (".codex", "skills"),
    (".pi", "skills"),
)
_HOME_SKILL_DIRS: tuple[tuple[str, ...], ...] = (
    (".cursor", "skills"),
    (".cursor", "skills-cursor"),
    (".agents", "skills"),
    (".claude", "skills"),
    (".codex", "skills"),
    (".pi", "agent", "skills"),
    (".pi", "skills"),
)
_HOME_PLUGIN_CACHES: tuple[tuple[str, ...], ...] = (
    (".cursor", "plugins", "cache"),
    (".claude", "plugins", "cache"),
)


@dataclass(frozen=True)
class SpokenSlash:
    token: str
    rest: str
    raw: str


def parse_spoken_slash(text: str) -> SpokenSlash | None:
    remainder = _strip_filler(text)
    match = _SLASH_HEAD.match(remainder)
    if match is None:
        return None
    token = match.group(1).lstrip("/").strip()
    rest = (match.group(2) or "").strip()
    if not token:
        return None
    return SpokenSlash(token=token, rest=rest, raw=text)


def parse_spoken_skill(text: str) -> SpokenSlash | None:
    slash = parse_spoken_slash(text)
    if slash is not None:
        return slash
    remainder = _strip_filler(text)
    match = _NATURAL_SKILL.match(remainder)
    if match is None:
        return None
    after_skill = (match.group("after_skill") or "").strip()
    before_skill = (match.group("before_skill") or "").strip()
    after_name = (match.group("after_name") or "").strip()
    if after_skill:
        words = after_skill.split()
        return SpokenSlash(token=words[0], rest=" ".join(words[1:]), raw=text)
    if before_skill:
        return SpokenSlash(token=before_skill, rest=after_name, raw=text)
    return None


def match_skill(token: str, catalog: frozenset[str]) -> str | None:
    folded = _fold(token)
    if not folded:
        return None
    for name in catalog:
        if _fold(name) == folded:
            return name
    return None


def rewrite_spoken_skill(
    text: str, catalog: frozenset[str], *, invoke: str = "slash"
) -> str:
    parsed = parse_spoken_skill(text)
    if parsed is None:
        return text
    words = [parsed.token, *parsed.rest.split()] if parsed.rest else [parsed.token]
    matched = parsed.token
    used = 1
    if catalog:
        for count in range(1, len(words) + 1):
            hit = match_skill(" ".join(words[:count]), catalog)
            if hit is not None:
                matched = hit
                used = count
    rest = " ".join(words[used:])
    if invoke == "slash":
        return f"/{matched} {rest}".strip()
    if rest:
        return f"Use the {matched} skill {rest}"
    return f"Use the {matched} skill."


def discover_skills(
    cwd: str | Path = "",
    extra_roots: list[Path] | None = None,
) -> frozenset[str]:
    """Slash slugs for every SKILL.md reachable from cwd and the user home."""
    names: set[str] = set()
    if cwd:
        root = Path(cwd)
        for parts in _PROJECT_SKILL_DIRS:
            names.update(_names_in_skill_root(root.joinpath(*parts)))
        names.update(_names_in_skill_root(root / ".cursor" / "plugins", nested=True))
    home = Path.home()
    for parts in _HOME_SKILL_DIRS:
        names.update(_names_in_skill_root(home.joinpath(*parts)))
    for parts in _HOME_PLUGIN_CACHES:
        names.update(_names_in_skill_root(home.joinpath(*parts), nested=True))
    if extra_roots:
        for extra in extra_roots:
            path = Path(extra)
            names.update(_names_in_skill_root(path))
            names.update(_names_in_skill_root(path, nested=True))
    return frozenset(name for name in names if name)


def _names_in_skill_root(root: Path, *, nested: bool = False) -> set[str]:
    if not root.is_dir():
        return set()
    pattern = "**/skills/*/SKILL.md" if nested else "*/SKILL.md"
    names: set[str] = set()
    for skill_file in root.glob(pattern):
        names.update(_skill_catalog_names(skill_file))
    return names


def _skill_catalog_names(skill_file: Path) -> set[str]:
    names = {_slug(skill_file.parent.name)}
    front = _slug(_skill_file_name(skill_file))
    if front:
        names.add(front)
    return {name for name in names if name}


def _strip_filler(text: str) -> str:
    remainder = text.strip()
    while True:
        match = _FILLER.match(remainder)
        if match is None:
            return remainder
        remainder = remainder[match.end() :]


def _fold(value: str) -> str:
    return re.sub(r"[\s_-]+", "", value.casefold())


def _slug(value: str) -> str:
    text = re.sub(r"[\s_]+", "-", value.strip().strip("'\""))
    return re.sub(r"-{2,}", "-", text).strip("-").casefold()


def _skill_file_name(skill_file: Path) -> str:
    try:
        raw = skill_file.read_text(encoding="utf-8")
    except OSError:
        return skill_file.parent.name
    if raw.startswith("---"):
        end = raw.find("\n---", 4)
        if end > 0:
            match = _SKILL_NAME.search(raw[4:end])
            if match:
                return match.group(1).strip().strip("'\"")
    return skill_file.parent.name
