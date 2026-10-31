"""Where a Discord bot was asked to sit, so it can sit there again.

A bot told to join a voice channel stays there, silent, until someone says
``leave``: through hours of nobody talking, a voice connection Discord drops,
and a restart of the bot. This module is the bot's memory of that seat (one
per server) and of the folder its agent keeps the conversation in. Files are
private to the OS user and keyed by the bot's Discord ID, the same key that
stops two runtimes of one bot (``runtime_lease``).
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path


def _root(root: Path | None) -> Path:
    return root or Path.home() / ".sleeptalk"


@dataclass(frozen=True)
class Seat:
    guild_id: int
    channel_id: int
    # Where notes about the seat go (the chat /join was typed in); 0: the
    # voice channel's own chat.
    text_channel_id: int = 0


def _seats_path(bot_id: int, root: Path | None) -> Path:
    return _root(root) / "discord-stay" / f"{int(bot_id)}.json"


def seats(bot_id: int, *, root: Path | None = None) -> list[Seat]:
    """Every seat this bot remembers; empty when none or unreadable."""
    path = _seats_path(bot_id, root)
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return []
    try:
        with os.fdopen(fd, encoding="utf-8") as stream:
            data = json.load(stream)
        return [Seat(int(item["guild_id"]), int(item["channel_id"]), int(item.get("text_channel_id") or 0))
                for item in data.get("seats", [])]
    except (OSError, ValueError, KeyError, TypeError):
        return []


def seat(bot_id: int, guild_id: int, *, root: Path | None = None) -> Seat | None:
    return next((s for s in seats(bot_id, root=root) if s.guild_id == int(guild_id)), None)


def _write(bot_id: int, items: list[Seat], root: Path | None) -> None:
    path = _seats_path(bot_id, root)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".stay-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"saved_at": time.time(), "seats": [asdict(s) for s in items]}, stream)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def remember(bot_id: int, new: Seat, *, root: Path | None = None) -> None:
    """Sit here in this server from now on (replaces the server's old seat)."""
    try:
        _write(bot_id, [s for s in seats(bot_id, root=root) if s.guild_id != new.guild_id] + [new], root)
    except OSError as exc:
        print(f"Could not remember the voice channel: {exc}")


def forget(bot_id: int, guild_id: int, *, root: Path | None = None) -> None:
    """Leave for good in this server: no rejoin after a drop or a restart."""
    kept = [s for s in seats(bot_id, root=root) if s.guild_id != int(guild_id)]
    try:
        if kept:
            _write(bot_id, kept, root)
        else:
            _seats_path(bot_id, root).unlink(missing_ok=True)
    except OSError as exc:
        print(f"Could not forget the voice channel: {exc}")


def agent_session_dir(bot_id: int, *, root: Path | None = None) -> Path:
    """The private folder this bot's agent keeps its conversation in."""
    path = _root(root) / "discord-sessions" / str(int(bot_id))
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


__all__ = ["Seat", "agent_session_dir", "forget", "remember", "seat", "seats"]
