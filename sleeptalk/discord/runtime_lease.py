"""Same-user, same-host duplicate bot protection across profile directories."""
from __future__ import annotations

import fcntl
import os
from pathlib import Path

from sleeptalk.discord_check import DiscordCheckError


def acquire_bot_lease(bot_id: int, *, root: Path | None = None) -> int:
    if not isinstance(bot_id, int) or bot_id <= 0:
        raise DiscordCheckError("Discord did not establish a bot identity.")
    directory = root or Path.home() / ".sleeptalk" / "discord-runtime-locks"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(directory / f"{bot_id}.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        os.close(descriptor)
        raise DiscordCheckError(
            "discord_runtime_conflict: this bot already runs under this OS user. "
            "Stop that runtime or use a separate Discord application."
        ) from exc
    os.set_inheritable(descriptor, False)
    return descriptor


def release_bot_lease(descriptor: int) -> None:
    os.close(descriptor)
