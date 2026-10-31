"""Private, crash-scavenged storage for legacy file-only TTS consumers."""

from __future__ import annotations

import atexit
import contextlib
import os
import shutil
import stat
import tempfile
import uuid
from pathlib import Path

# Per user, since the temp directory is often shared (/tmp on Linux).
TTS_TEMP_ROOT = Path(tempfile.gettempdir()) / f"sleeptalk-tts-{os.getuid()}"
_PROCESS_DIR = TTS_TEMP_ROOT / str(os.getpid())


def _is_private_dir(path: Path) -> bool:
    """A real directory (not a symlink) that only this user can enter."""
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return (
        stat.S_ISDIR(info.st_mode)
        and info.st_uid == os.getuid()
        and not info.st_mode & 0o077
    )


def _process_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def scavenge_tts_temp_dir() -> None:
    """Remove crash leftovers without disrupting another live bot process."""

    global TTS_TEMP_ROOT, _PROCESS_DIR
    TTS_TEMP_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not _is_private_dir(TTS_TEMP_ROOT):
        # Someone else made (or can write) the shared name first; never put
        # speech audio there. A fresh private directory works the same.
        TTS_TEMP_ROOT = Path(tempfile.mkdtemp(prefix="sleeptalk-tts-"))
        _PROCESS_DIR = TTS_TEMP_ROOT / str(os.getpid())
    for child in TTS_TEMP_ROOT.iterdir():
        try:
            owner_pid = int(child.name)
        except ValueError:
            owner_pid = -1
        if child == _PROCESS_DIR or not _process_is_running(owner_pid):
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child, ignore_errors=True)
            else:
                with contextlib.suppress(FileNotFoundError):
                    child.unlink()
    _PROCESS_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)


def write_tts_temp_file(audio: bytes, *, prefix: str = "speech") -> Path:
    """Write one mode-0600 WAV inside the managed per-process directory."""

    _PROCESS_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = _PROCESS_DIR / f"{prefix}-{uuid.uuid4().hex}.wav"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(audio)
    return path


def clear_tts_temp_files() -> None:
    """Remove this process's managed TTS files (safe at session/process close)."""

    shutil.rmtree(_PROCESS_DIR, ignore_errors=True)
    with contextlib.suppress(OSError):
        TTS_TEMP_ROOT.rmdir()


scavenge_tts_temp_dir()
atexit.register(clear_tts_temp_files)
