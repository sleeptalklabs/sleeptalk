"""Locating the shipped narration clips.

The clips ship inside the package (``sleeptalk/assets``) so a wheel install
plays them too. A ``sounds/`` directory at or above the working directory,
as in a checkout, takes precedence so a clip can be re-cut without a
release.
"""

from __future__ import annotations

import atexit
import inspect
import shutil
import sys
import tempfile
import wave
from array import array
from functools import lru_cache
from pathlib import Path
from typing import Any

# Style -> clip filename. Only styles that make a sound appear here.
#
# Clips must be WAV: every play_file on every surface decodes WAV and nothing
# else. To re-cut one from a source recording (mp3/m4a/...), decode with
# to_ndarray rather than reading plane buffers — bytes(plane) appends the
# allocation padding and turns the clip into bursts of noise:
#
#     resampler = av.AudioResampler(format="s16", layout="stereo", rate=48000)
#     chunks = [o.to_ndarray() for f in container.decode(audio=0)
#               for o in resampler.resample(f)]
#     chunks += [o.to_ndarray() for o in resampler.resample(None)]  # flush
NARRATION_CLIPS: dict[str, str] = {"keyboard": "keyboard.wav"}


def voice_unavailable_clip_path() -> Path | None:
    """The tone for a turn whose voice failed: nothing could be said, not even
    the apology, so the caller hears this instead of silence."""
    packaged = Path(__file__).with_name("assets") / "voice-unavailable.wav"
    return packaged if packaged.is_file() else None


@lru_cache(maxsize=1)
def sounds_dir() -> Path | None:
    """Nearest `sounds/` directory at or above the working directory."""
    cwd = Path.cwd().resolve()
    for parent in (cwd, *cwd.parents):
        candidate = parent / "sounds"
        if candidate.is_dir():
            return candidate
    return None


def narration_clip_path(style: object) -> Path | None:
    """Clip for this narration style, or None when there is nothing to play.

    None covers every reason a clip is unavailable — a style that makes no
    sound, no sounds directory, a missing file — because every one of them
    means the same thing to a caller: stay quiet.
    """
    name = NARRATION_CLIPS.get(str(style or "").strip().lower())
    if name is None:
        return None
    directory = sounds_dir()
    if directory is not None and (directory / name).is_file():
        return directory / name
    packaged = Path(__file__).with_name("assets") / name
    return packaged if packaged.is_file() else None


# How long each piece of the waiting sound lasts. It is also the most the
# answer can wait behind it: a surface that queues audio holds at most one.
WORKING_SLICE_SECONDS = 0.4
_FADE_SECONDS = 0.003


@lru_cache(maxsize=4)
def working_slices(style: str) -> tuple[tuple[Path, float], ...]:
    """The style's clip cut into short WAV pieces, with each piece's length.

    Played back to back they are the whole clip, looping; stopping between
    two of them stops the sound within one piece. Empty when the style makes
    no sound or the clip can't be read. Written once per process to a
    private temporary directory.
    """
    path = narration_clip_path(style)
    if path is None:
        return ()
    try:
        with wave.open(str(path), "rb") as source:
            channels, width, rate = source.getnchannels(), source.getsampwidth(), source.getframerate()
            frames = source.readframes(source.getnframes())
    except (OSError, EOFError, wave.Error):
        return ()
    if width != 2 or not frames:
        return ()
    samples = array("h", frames)
    if sys.byteorder == "big":
        samples.byteswap()
    step = max(1, int(rate * WORKING_SLICE_SECONDS)) * channels
    fade = max(1, int(rate * _FADE_SECONDS))
    directory = Path(tempfile.mkdtemp(prefix="sleeptalk-sounds-"))
    atexit.register(shutil.rmtree, directory, True)
    pieces: list[tuple[Path, float]] = []
    for index, start in enumerate(range(0, len(samples), step)):
        piece = samples[start:start + step]
        count = len(piece) // channels
        # A short ramp at each edge, so a stop between pieces doesn't click.
        for frame in range(min(fade, count // 2)):
            gain = frame / fade
            for channel in range(channels):
                head = frame * channels + channel
                tail = (count - 1 - frame) * channels + channel
                piece[head] = int(piece[head] * gain)
                piece[tail] = int(piece[tail] * gain)
        if sys.byteorder == "big":
            piece.byteswap()
        target = directory / f"{Path(path).stem}-{index:03d}.wav"
        with wave.open(str(target), "wb") as out:
            out.setnchannels(channels)
            out.setsampwidth(2)
            out.setframerate(rate)
            out.writeframes(piece.tobytes())
        pieces.append((target, count / rate))
    return tuple(pieces)


async def play_clip_on(play_file: Any, path: Path, turn_id: int) -> bool:
    """Call a surface's play_file, handing it the turn when it wants one.

    Surfaces that stream per turn (Discord) need the id to queue the clip
    inside the right response and fence it with that turn's cancellation;
    device-backed surfaces (terminal, web) take only the path. A False
    return means the surface refused the clip; anything else counts as played.
    """
    accepts_turn = False
    try:
        accepts_turn = "turn_id" in inspect.signature(play_file).parameters
    except (TypeError, ValueError):
        pass
    result = await (play_file(path, turn_id=turn_id) if accepts_turn else play_file(path))
    return result is not False
