"""Which stored keys a profile still uses, and removing the ones none do.

The store records references and providers; profiles record which reference
they use. A key is in use when any profile Sleeptalk knows about names its
reference: the home's profiles, ``$SLEEPTALK_HOME``'s, every trusted repo's
``config/profiles``, and any directory the caller adds (the profile being
edited). Anything else is left over from a replaced key or a deleted
profile, and is removed from this computer (never revoked at the provider).
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Iterable

_REFERENCE = re.compile(r"credential:[0-9a-f]{32}")


def profile_dirs(extra: Iterable[Path] = (), *, env: dict[str, str] | None = None) -> list[Path]:
    from sleeptalk.home import DEFAULT_HOME, trusted_repos

    env = dict(os.environ) if env is None else env
    candidates = [DEFAULT_HOME / "profiles", *(Path(repo) / "config" / "profiles" for repo in trusted_repos()), *extra]
    home = env.get("SLEEPTALK_HOME", "").strip()
    if home:
        candidates.append(Path(home).expanduser() / "profiles")
    seen: list[Path] = []
    for directory in candidates:
        resolved = Path(directory).expanduser().resolve()
        if resolved not in seen:
            seen.append(resolved)
    return seen


def references_in(path: Path) -> set[str]:
    """Every credential reference a profile file names, wherever it names it."""
    try:
        return set(_REFERENCE.findall(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeDecodeError):
        return set()


def references_in_use(dirs: Iterable[Path]) -> dict[str, list[str]]:
    """Reference -> the profiles (by name) that use it."""
    used: dict[str, list[str]] = {}
    for directory in dirs:
        if not Path(directory).is_dir():
            continue
        for path in sorted(Path(directory).glob("*.toml")):
            for reference in references_in(path):
                used.setdefault(reference, []).append(path.stem)
    return used


def inventory(*, store=None, extra_dirs: Iterable[Path] = ()) -> list[dict[str, Any]]:
    """Every stored key: its reference, provider, and the profiles using it. No values."""
    from sleeptalk.credential_store import CredentialStore

    store = store or CredentialStore()
    used = references_in_use(profile_dirs(extra_dirs))
    return [{"reference": reference, "provider": provider, "used_by": sorted(set(used.get(reference, []))),
             "unused": reference not in used}
            for reference, provider in store.references()]


def profile_ids(path: Path) -> set[str]:
    """Every name a profile's store bindings may use (they grew several spellings)."""
    import hashlib

    resolved = Path(path).resolve()
    digest = hashlib.sha256(str(resolved).encode()).hexdigest()
    return {resolved.stem, f"{resolved.stem}@{digest[:12]}", digest, str(resolved)}


def pending_references(root: Path | None = None) -> set[str]:
    """Keys an open key request has saved but whose profile isn't written yet."""
    import json

    root = root or Path.home() / ".local" / "share" / "sleeptalk" / "setup"
    found: set[str] = set()
    if not root.is_dir():
        return found
    for state in list(root.glob("*/state.json")) + list(root.glob("*.progress.json")):
        try:
            document = json.loads(state.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if state.name == "state.json" and document.get("status") not in (None, "waiting", "interrupted"):
            continue
        refs = document.get("refs") if isinstance(document, dict) else None
        if isinstance(refs, dict):
            found.update(str(ref) for ref in refs.values() if _REFERENCE.fullmatch(str(ref)))
    return found


def forget_unused(references: Iterable[str], *, store=None, extra_dirs: Iterable[Path] = (),
                  only_bound_to: set[str] | None = None) -> list[str]:
    """Remove each reference no known profile names; returns those removed.

    A key an open key request just saved is kept. With ``only_bound_to`` (the
    automatic cleanup after one profile changes), a key is removed only if
    the store binds it to that profile alone; a binding to anything else means
    a profile this scan can't see may still use it.
    """
    from sleeptalk.credential_store import CredentialError, CredentialStore

    wanted = {reference for reference in references if _REFERENCE.fullmatch(str(reference))}
    if not wanted:
        return []
    store = store or CredentialStore()
    used = references_in_use(profile_dirs(extra_dirs))
    keep = pending_references()
    removed: list[str] = []
    for reference in sorted(wanted - set(used) - keep):
        try:
            bound = store.status(reference).profiles
            if only_bound_to is not None and set(bound) - only_bound_to:
                continue
            store.remove(reference, affected_profiles=bound)
        except CredentialError:
            continue
        removed.append(reference)
    return removed


def forget_dropped(before: Iterable[str], path: Path, *, store=None) -> list[str]:
    """After ``path`` was rewritten: remove the keys it stopped naming that no
    other profile names. Cleanup never fails the save that triggered it."""
    dropped = set(before) - references_in(path)
    if not dropped:
        return []
    try:
        return forget_unused(dropped, store=store, extra_dirs=[path.parent], only_bound_to=profile_ids(path))
    except Exception:  # noqa: BLE001 — bookkeeping after a successful save
        return []
