"""SLEEPTALK_HOME state-directory resolution.

One directory holds everything sleeptalk writes: profiles/<slug>.toml,
sleeptalk.toml (shared defaults), .env, logs/. One directory = one volume
mount, one backup, one `rm -rf` uninstall.

Resolution order (highest wins):

  1. explicit --config PATH         (handled by callers; orthogonal to home)
  2. $SLEEPTALK_HOME                (env-only, no --home flag — supervisors
                                     set env naturally: Docker ENV, systemd
                                     Environment=)
  3. repo context detected from cwd, only for a repo the user trusted with
     `sleeptalk trust` (NEVER silent: a line names what was detected and
     that SLEEPTALK_HOME overrides it)
  4. ~/.sleeptalk

Repo detection (tier 3): walk up from cwd, at most 3 levels and never past
$HOME, looking for config/profiles/*.toml or a pyproject.toml that declares
the sleeptalk entry point.

A repo's profiles choose the agent command, provider endpoints, and the
.env that feeds the agent, so a cloned repo is never used on sight: an
untrusted one is named and skipped, and the default home is used instead.
"""

from __future__ import annotations

import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

ENV_VAR = "SLEEPTALK_HOME"
DEFAULT_HOME = Path.home() / ".sleeptalk"
_MAX_WALK_UP = 3
TRUST_FILE = "trusted-repos"


@dataclass(frozen=True)
class Layout:
    """Where state lives for this invocation."""

    root: Path
    tier: int  # 2 = SLEEPTALK_HOME, 3 = repo context, 4 = default home
    reason: str
    # A repo context that was found but not trusted, so not used.
    untrusted_repo: Path | None = None

    @property
    def profiles_dir(self) -> Path:
        return self.root / "config" / "profiles" if self.tier == 3 else self.root / "profiles"

    @property
    def env_path(self) -> Path:
        return self.root / ".env"

    @property
    def root_config(self) -> Path:
        local = self.root / "sleeptalk.toml"
        if local.is_file():
            return local
        example = self.root / "sleeptalk.example.toml"
        if example.is_file():
            return example
        return local

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"


def _pyproject_declares_sleeptalk(path: Path) -> bool:
    pyproject = path / "pyproject.toml"
    if not pyproject.is_file():
        return False
    try:
        text = pyproject.read_text(encoding="utf-8")
    except OSError:
        return False
    return "[project.scripts]" in text and "sleeptalk" in text


def detect_repo_context(start: Path) -> tuple[Path, str] | None:
    """Repo root + what was detected, or None. Never crosses $HOME."""
    start = start.resolve()
    home = Path.home().resolve()
    current = start
    for _ in range(_MAX_WALK_UP + 1):
        if (current / "config" / "profiles").is_dir() and any(
            (current / "config" / "profiles").glob("*.toml")
        ):
            return current, f"found {current / 'config' / 'profiles'}/*.toml"
        if _pyproject_declares_sleeptalk(current):
            return current, f"found {current / 'pyproject.toml'} declaring the sleeptalk entry point"
        if current == home or current.parent == current:
            break
        current = current.parent
    return None


def resolve_layout(*, cwd: Path | None = None, env: dict[str, str] | None = None) -> Layout:
    """Tiers 2-4. Tier 1 (explicit --config) is the caller's business."""
    env = env if env is not None else dict(os.environ)
    cwd = (cwd or Path.cwd()).resolve()

    explicit = env.get(ENV_VAR, "").strip()
    if explicit:
        root = Path(explicit).expanduser()
        return Layout(root, 2, f"${ENV_VAR}={root}")

    detected = detect_repo_context(cwd)
    if detected is not None:
        root, what = detected
        if root.resolve() in trusted_repos():
            return Layout(
                root, 3,
                f"repo context: {what} (set {ENV_VAR} to override)",
            )
        return Layout(
            DEFAULT_HOME, 4,
            f"default home ({DEFAULT_HOME}); ignored untrusted repo config at {root}, "
            f"run `sleeptalk trust` there to use it",
            untrusted_repo=root,
        )

    return Layout(DEFAULT_HOME, 4, f"default home ({DEFAULT_HOME})")


def _trust_path() -> Path:
    return DEFAULT_HOME / TRUST_FILE


def trusted_repos() -> frozenset[Path]:
    """Repos the user trusted. A file others could have written trusts nothing."""
    path = _trust_path()
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return frozenset()
    try:
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            return frozenset()
        with os.fdopen(fd, encoding="utf-8") as stream:
            fd = -1
            lines = stream.read().splitlines()
    finally:
        if fd >= 0:
            os.close(fd)
    return frozenset(Path(line) for line in lines if line.startswith("/"))


def set_trusted(repo: Path, trusted: bool) -> Path:
    """Add or remove one repo root; returns the resolved root."""
    root = repo.expanduser().resolve()
    current = set(trusted_repos())
    if trusted:
        current.add(root)
    else:
        current.discard(root)
    path = _trust_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".trust-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write("".join(f"{item}\n" for item in sorted(map(str, current))))
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)
    return root


def announce(layout: Layout) -> str:
    """The one-line resolution statement (doctor's first line; tier-3 noise)."""
    return f"state directory: {layout.root} (tier {layout.tier}: {layout.reason})"
