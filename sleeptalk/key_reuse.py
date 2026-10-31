"""Turn a key the user already has into a working voice profile, on the host.

``host ensure`` only offers a found key. The user says yes in chat; the
agent runs ``host ensure --reuse-key <provider>``. This module then reads
the key where ``key_discovery`` found it, proves it listens and speaks
(small paid requests to that provider), copies it into the private
credential store, and writes a reference-only profile through the same code the key
pages use. The value never appears in a receipt, a file, or the agent's
context.
"""
from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import Any

from sleeptalk.credential_store import CredentialStore
from sleeptalk.key_discovery import ReusableKey, discover, read_value
from sleeptalk.private_input import InputError, InputSpec, atomic_write, prepare_profile, validate_provider, _validation_code
from sleeptalk.speech.openai_audio import PRESETS


class ReuseError(RuntimeError):
    """A fixed, value-free code plus a sentence the agent may relay."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def pick(provider: str, keys: list[ReusableKey]) -> ReusableKey:
    for key in keys:
        if key.provider == provider:
            return key
    raise ReuseError("key_not_found", f"No {provider} key was found on this host to reuse.")


def profile_summary(provider: str) -> dict[str, str]:
    """What the agent tells the user: which models will listen and speak."""
    preset = PRESETS[provider]
    return {"provider": provider, "label": preset.label, "listening_model": preset.stt_model,
            "speaking_model": preset.tts_model, "voice": preset.tts_voice, "free": preset.free_line}


async def reuse_key(spec: InputSpec, key: ReusableKey, *, store: CredentialStore | None = None,
                    environment: dict[str, str] | None = None, state_root: Path | None = None) -> dict[str, Any]:
    """Validate, store, and write the profile. Returns a value-free receipt block."""
    if key.provider not in PRESETS:
        raise ReuseError("unsupported", f"{key.provider} cannot listen and speak.")
    store = store or CredentialStore()
    spec = spec.chosen(key.provider) if spec.open_choice else spec
    value = read_value(key, environment=environment, store=store)
    if not value:
        raise ReuseError("key_not_found", f"The {key.provider} key at {key.source} is empty.")
    try:
        async with asyncio.timeout(60):
            details = await validate_provider(key.provider, value, spec, spec.tts_voice_id)
    except Exception as exc:  # the code is enough; the message may quote provider bodies
        raise ReuseError(_validation_code(exc), f"{PRESETS[key.provider].label} did not accept that key for voice.") from None
    created = False
    if key.kind == "credential" and store.status(key.location).provider == key.provider:
        reference = key.location
    else:
        # A model credential is scoped to its endpoint; voice needs its own record.
        reference = store.put_validated(key.provider, value, request_id=spec.request_id + ":" + key.provider)
        created = True
    value = ""
    refs = {key.provider: reference}
    profile_path = Path(spec.profile_path)
    state_dir = state_root or (Path.home() / ".local/share/sleeptalk/setup")
    try:
        document = await prepare_profile(spec, refs, {key.provider: details}, store, {}, profile_path, state_dir)
        if profile_path.exists() and profile_path.read_text() != document:
            raise ReuseError("profile_exists", "This profile already exists with different settings; choose another profile name.")
        profile_id = hashlib.sha256(str(profile_path.resolve()).encode()).hexdigest()
        generations = {reference: store.status(reference).generation}
        with store.bind_profile_generation(generations, spec.profile + "@" + profile_id[:12]):
            atomic_write(profile_path, document.encode())
    except BaseException:
        # No profile means no owner for the record we just made; leave nothing behind.
        if created:
            try:
                store.remove(reference, affected_profiles=[])
            except Exception:  # noqa: BLE001 — the original failure is what matters
                pass
        raise
    return {"reused_key": key.public(), "speech": profile_summary(key.provider), "profile_path": str(profile_path)}


def spec_for_reuse(*, request_id: str, profile: str, workspace: str, agent: str, surface: str,
                   profile_path: str, language: str = "en-US", **agent_fields: Any) -> InputSpec:
    """The same request shape the key pages use, with the provider left to the key."""
    return InputSpec(request_id=request_id, profile=profile, workspace=workspace, agent=agent, surface=surface,
                     profile_path=profile_path, language=language, **agent_fields)


def find(provider: str, **discover_kwargs: Any) -> ReusableKey:
    return pick(provider, discover(**discover_kwargs))


__all__ = ["ReuseError", "ReusableKey", "discover", "find", "pick", "profile_summary", "reuse_key", "spec_for_reuse", "InputError"]
