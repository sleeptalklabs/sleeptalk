"""Instant voice cloning through fixed provider endpoints; no reference audio on disk."""
from __future__ import annotations

import asyncio
import base64
import binascii
import io
import re
import secrets
import struct
import time
import wave

import aiohttp

PROVIDERS = {"cartesia", "elevenlabs"}
LIMITS = {"cartesia": (10, 60), "elevenlabs": (10, 120)}
CARTESIA_VERSION = "2026-08-14"
LANGUAGES = set("en fr de es pt zh ja hi it ko nl pl ru sv tr tl bg ro ar cs el fi hr ms sk da ta uk hu no vi bn th he ka id te gu kn ml mr pa or ur".split())
_ID = re.compile(r"[A-Za-z0-9_-]{1,200}")


class CloneError(ValueError):
    """Only fixed, user-facing messages cross the settings boundary."""


class Upload:
    """One bounded, expiring upload, bound to its profile credential and metadata."""

    def __init__(self):
        self.pending: dict | None = None

    def accept(self, body: dict, binding: str) -> tuple[str, bytes | None]:
        fields = {"provider", "name", "language", "consent", "audio", "offset", "upload_id", "final"}
        if (not isinstance(body, dict) or set(body) != fields
                or not isinstance(body.get("provider"), str) or body["provider"] not in PROVIDERS or body.get("consent") is not True
                or not isinstance(body.get("name"), str) or not 1 <= len(body["name"].strip()) <= 64
                or any(ord(c) < 32 for c in body["name"])
                or not isinstance(body.get("language"), str) or body["language"] not in LANGUAGES
                or not isinstance(body.get("audio"), str) or len(body["audio"]) > 12000
                or type(body.get("offset")) is not int or type(body.get("final")) is not bool
                or not isinstance(body.get("upload_id"), str)):
            raise CloneError("Choose a recording, name, language, and confirm permission to clone it.")
        try:
            chunk = base64.b64decode(body["audio"], validate=True)
        except (ValueError, binascii.Error):
            raise CloneError("The recording upload is invalid. Choose the file again.") from None
        metadata = (binding, body["provider"], body["name"], body["language"])
        first = body["offset"] == 0 and not body["upload_id"]
        if first:
            self.pending = {"id": secrets.token_hex(16), "metadata": metadata,
                            "audio": bytearray(), "expires": time.monotonic() + 600}
        pending = self.pending
        if (pending is None or time.monotonic() > pending["expires"] or metadata != pending["metadata"]
                or (not first and body["upload_id"] != pending["id"])
                or body["offset"] != len(pending["audio"])):
            raise CloneError("Upload interrupted. Choose the recording again.")
        limit = LIMITS[body["provider"]][1] * 96000 + 4096
        if not chunk or len(chunk) > 8192 or len(pending["audio"]) + len(chunk) > limit:
            self.pending = None
            raise CloneError("The recording exceeds this provider's upload limit.")
        pending["audio"].extend(chunk)
        pending["expires"] = time.monotonic() + 600
        if not body["final"]:
            return pending["id"], None
        self.pending = None
        audio = bytes(pending["audio"])
        validate_audio(body["provider"], audio)
        return pending["id"], audio


def validate_audio(provider: str, audio: bytes) -> None:
    minimum, maximum = LIMITS[provider]
    try:
        with wave.open(io.BytesIO(audio), "rb") as clip:
            if (clip.getnchannels(), clip.getsampwidth(), clip.getframerate()) != (1, 2, 48000):
                raise CloneError("Upload mono 48 kHz, 16-bit WAV audio.")
            frames = clip.getnframes()
            if not minimum * 48000 <= frames <= maximum * 48000:
                raise CloneError(f"Use a recording between {minimum} and {maximum} seconds.")
            pcm = clip.readframes(frames)
            if len(pcm) != frames * 2:
                raise CloneError("The recording is incomplete. Choose the file again.")
    except (wave.Error, EOFError):
        raise CloneError("That file could not be read as WAV audio.") from None
    if max(abs(s[0]) for s in struct.iter_unpack("<h", pcm)) < 32:
        raise CloneError("This recording is silent. Choose a clip with clear speech.")


def _headers(provider: str, key: str) -> dict:
    if provider == "elevenlabs":
        return {"xi-api-key": key}
    return {"Authorization": f"Bearer {key}", "Cartesia-Version": CARTESIA_VERSION}


async def _request(session, method: str, url: str, **kwargs) -> dict:
    try:
        async with session.request(method, url, allow_redirects=False, **kwargs) as response:
            if response.status in {401, 403}:
                raise CloneError("The provider denied access. Check your API key permissions and cloning plan.")
            if response.status == 429:
                raise CloneError("The provider is busy or your quota is exhausted. Check your account before retrying.")
            if not 200 <= response.status < 300:
                raise CloneError("The provider rejected the request. Check the recording and your account in its voice dashboard.")
            payload = await response.json()
            if not isinstance(payload, dict):
                raise ValueError()
            return payload
    except CloneError:
        raise
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        # Never retry creation automatically: the provider may already have created it.
        raise CloneError("The provider response could not be confirmed. Refresh your voices before uploading again.") from None


def _voice(provider: str, item: dict, *, name: str = "") -> dict:
    voice_id = item.get("voice_id" if provider == "elevenlabs" else "id")
    if not isinstance(voice_id, str) or not _ID.fullmatch(voice_id):
        raise CloneError("The provider returned an invalid voice. Check its voice dashboard.")
    verification = item.get("voice_verification") or {}
    safety = item.get("safety_control", "NONE")
    requires = (item.get("requires_verification") is True
                or (verification.get("requires_verification") is True and verification.get("is_verified") is not True)
                or safety in {"CAPTCHA", "ENTERPRISE_CAPTCHA"})
    blocked = safety in {"BAN", "ENTERPRISE_BAN"} or item.get("status", "active") != "active"
    return {"voice_id": voice_id, "name": str(item.get("name") or name or voice_id)[:64],
            "requires_verification": requires, "available": not (requires or blocked),
            "status": "unavailable" if blocked else "verification_required" if requires else "ready"}


async def create_voice(provider: str, key: str, name: str, language: str, audio: bytes) -> dict:
    validate_audio(provider, audio)
    form = aiohttp.FormData()
    form.add_field("name", name.strip())
    if provider == "cartesia":
        url = "https://api.cartesia.ai/voices/clone"
        form.add_field("language", language)
        form.add_field("access", "private")
        field = "clip"
    else:
        url = "https://api.elevenlabs.io/v1/voices/add"
        field = "files"
    form.add_field(field, audio, filename="reference.wav", content_type="audio/wav")
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as session:
        payload = await _request(session, "POST", url, headers=_headers(provider, key), data=form)
    return _voice(provider, payload, name=name)


async def list_voices(provider: str, key: str) -> list[dict]:
    """Provider storage survives settings reloads; only return custom account voices."""
    voices = []
    params = {"limit": "100", "is_owner": "true"} if provider == "cartesia" else {}
    url = "https://api.cartesia.ai/voices" if provider == "cartesia" else "https://api.elevenlabs.io/v1/voices"
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
        for _ in range(5):
            payload = await _request(session, "GET", url, headers=_headers(provider, key), params=params)
            for item in payload.get("data" if provider == "cartesia" else "voices", []):
                if provider == "elevenlabs" and item.get("category") != "cloned":
                    continue
                voices.append(_voice(provider, item))
            cursor = payload.get("next_page") if provider == "cartesia" else None
            if not cursor:
                break
            params["starting_after"] = str(cursor)
    return voices
