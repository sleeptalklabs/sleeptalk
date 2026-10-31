"""Terminal voice audition through the OS audio output.

Discovery prefers provider-hosted previews before credentials.  After a
selection, live synthesis proves the configured voice, key, and audio output.
Neither kind of sample is bundled or permanently mirrored.

File lifecycle: sample files are deleted as soon as playback succeeds (or
after each failed player attempt). The file is left behind ONLY when no
player exists on the system, with the path printed so the user can play it
manually — that's the no-player fallback.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import aiohttp

_PLAYERS = (
    ("afplay", ["afplay", "{file}"]),  # macOS
    ("ffplay", ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", "{file}"]),
    ("paplay", ["paplay", "{file}"]),  # PulseAudio
    ("aplay", ["aplay", "-q", "{file}"]),  # ALSA
)


def find_player() -> list[str] | None:
    """First available player as an argv template, or None."""
    for binary, argv in _PLAYERS:
        if shutil.which(binary):
            return argv
    return None


async def play_file(path: Path) -> bool:
    """Play a WAV via the system player. Returns False when no player exists.

    Deletes ``path`` after a successful (attempted) playback; on no-player,
    leaves the file behind for manual playback.
    """
    player = find_player()
    if player is None:
        return False
    argv = [part.format(file=str(path)) for part in player]
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await process.wait()
    with contextlib.suppress(FileNotFoundError):
        path.unlink()
    return process.returncode == 0


async def play_url(url: str, *, max_bytes: int = 15 * 1024 * 1024) -> tuple[bool, Path | None]:
    """Temporarily download and play a provider-hosted preview.

    The preview is never packaged or permanently mirrored.  Successful
    playback removes the temporary file through :func:`play_file`; when no
    player exists the path is returned so the operator can open it manually.
    """
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("voice preview URL must be HTTPS")
    suffix = Path(parsed.path).suffix.lower()
    if suffix not in {".wav", ".mp3", ".ogg", ".webm", ".flac", ".m4a"}:
        suffix = ".audio"
    async with aiohttp.ClientSession() as session, session.get(
        url, timeout=aiohttp.ClientTimeout(total=30)
    ) as response:
        if response.status != 200:
            raise RuntimeError(f"preview download failed (HTTP {response.status})")
        length = response.content_length
        if length is not None and length > max_bytes:
            raise RuntimeError("preview is larger than the temporary download limit")
        audio = await response.read()
    if len(audio) > max_bytes:
        raise RuntimeError("preview is larger than the temporary download limit")
    handle = tempfile.NamedTemporaryFile(prefix="sleeptalk-preview-", suffix=suffix, delete=False)
    try:
        handle.write(audio)
    finally:
        handle.close()
    path = Path(handle.name)
    played = await play_file(path)
    return played, None if played else path
