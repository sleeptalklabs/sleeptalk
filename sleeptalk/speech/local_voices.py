"""Private, persistent Pocket reference recordings, addressed by opaque voice IDs."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import re
import struct
import uuid
import wave

MODEL = "pocket-tts-en"
MAX_REFERENCE_SECONDS = 30
MAX_AUDIO_BYTES = 24_000 * 2 * MAX_REFERENCE_SECONDS + 4096
_ID = re.compile(r"pocket-custom-[0-9a-f]{32}")


def voices_dir() -> Path:
    # Independent of the working directory, shared by settings and call workers.
    home = os.environ.get("SLEEPTALK_HOME", "").strip()
    return (Path(home).expanduser() if home else Path.home() / ".sleeptalk") / "voices" / "pocket"


def is_custom(voice: str) -> bool:
    return bool(_ID.fullmatch(voice))


def reference_path(voice: str) -> Path:
    if not is_custom(voice):
        raise ValueError("Invalid custom voice ID.")
    path = voices_dir() / f"{voice}.wav"
    if path.is_symlink() or not path.is_file():
        raise ValueError("This saved voice is missing. Upload its recording again.")
    return path


def list_voices() -> list[dict]:
    result = []
    for path in sorted(voices_dir().glob("pocket-custom-*.json")):
        if not is_custom(path.stem) or path.is_symlink():
            continue
        try:
            reference_path(path.stem)
            meta = json.loads(path.read_text())
            name = meta["name"]
            if not isinstance(name, str) or not 1 <= len(name) <= 64:
                continue
            result.append({"voice_id": path.stem, "name": name, "model": MODEL,
                           "description": "Your uploaded voice", "default": False})
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return result


def save_voice(name: str, audio: bytes) -> dict:
    name = name.strip()
    if not 1 <= len(name) <= 64 or any(ord(c) < 32 for c in name):
        raise ValueError("Give this voice a name of 1 to 64 characters.")
    if len(audio) > MAX_AUDIO_BYTES:
        raise ValueError("Use a recording between 3 and 30 seconds.")
    try:
        with wave.open(io.BytesIO(audio), "rb") as clip:
            if (clip.getnchannels(), clip.getsampwidth(), clip.getframerate()) != (1, 2, 24_000):
                raise ValueError("Upload mono 24 kHz, 16-bit WAV audio.")
            frames = clip.getnframes()
            if not 3 * 24_000 <= frames <= MAX_REFERENCE_SECONDS * 24_000:
                raise ValueError("Use a recording between 3 and 30 seconds.")
            pcm = clip.readframes(frames)
            if len(pcm) != frames * 2:
                raise ValueError("The recording is incomplete. Choose the file again.")
    except (wave.Error, EOFError):
        raise ValueError("That file could not be read as WAV audio.") from None
    if max(abs(s[0]) for s in struct.iter_unpack("<h", pcm)) < 32:
        raise ValueError("This recording is silent. Choose a clip with clear speech.")
    root = voices_dir()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    voice = "pocket-custom-" + uuid.uuid4().hex
    paths = [root / f"{voice}.wav", root / f"{voice}.json"]
    try:
        for path, data in zip(paths, [audio, json.dumps({"name": name}).encode()]):
            with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as file:
                file.write(data)
    except BaseException:
        for path in paths:
            path.unlink(missing_ok=True)
        raise
    return {"voice_id": voice, "name": name, "model": MODEL,
            "description": "Your uploaded voice", "default": False}
