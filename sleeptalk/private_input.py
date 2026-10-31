"""Bounded loopback credential entry. Never launch this in an agent-owned browser.

The page authenticates possession of a local invitation, not a remote operator.
An unrestricted process running as the same OS user can read the private store.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import tomllib
import uuid
from typing import Literal

from aiohttp import web
from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

from sleeptalk.config import AgentConfig, SleeptalkConfig
from sleeptalk.agent_auth import AgentAuthChoice, AgentAuthConfig, validate_model_auth_labels
from sleeptalk.credential_store import CredentialError, CredentialNotFoundError, CredentialStore
from sleeptalk.runtime_binding import bind, canonical_workspace, toml as binding_toml

LIFETIME = 900
VOICE_ID_MAX_LENGTH = 200
VOICE_ID_PATTERN = r"^[^\x00-\x1f\x7f]*$"
from sleeptalk.speech.openai_audio import PRESETS as _AUDIO_PRESETS

# Every credential a key page can collect and the env name its profile uses.
# The direct speech providers are listed here; the OpenAI-compatible audio
# services come from their presets, so a new service is added in one place.
ENV_NAMES = {"deepgram": "DEEPGRAM_API_KEY", "cartesia": "CARTESIA_API_KEY", "elevenlabs": "ELEVENLABS_API_KEY",
             **{name: preset.env for name, preset in _AUDIO_PRESETS.items()}, "discord": "DISCORD_BOT_TOKEN"}
SPEECH_PROVIDERS = tuple(name for name in ENV_NAMES if name != "discord")
# The same providers in the order the pages offer them: the direct ones first,
# then the services a user is likeliest to already hold a key for.
KEY_PROVIDERS = ("cartesia", "deepgram", "elevenlabs", "openrouter", "venice", "groq", "together")
assert set(KEY_PROVIDERS) == set(SPEECH_PROVIDERS)
_SPEECH_PROVIDER = "(" + "|".join(SPEECH_PROVIDERS) + ")"


class InputError(ValueError):
    pass


class ChoiceError(web.HTTPBadRequest):
    """A finish-step choice the host can't accept: a fixed code plus a message
    for the local page. The phone room sends only the code."""

    def __init__(self, code: str, text: str) -> None:
        super().__init__(text=text)
        self.code = code


class InputSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(pattern=r"^[A-Za-z0-9_-]{8,128}$")
    profile: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    workspace: str
    agent: str
    auth: AgentAuthConfig | None = None
    auth_request: AgentAuthChoice | None = None
    options: dict[str, JsonValue] = Field(default_factory=dict)
    surface: Literal["web", "terminal", "discord"]
    capture_mode: Literal["", "headphones", "speakers"] = ""
    user_terminal_local: bool = False
    runtime: Literal["", "native", "gateway", "acp"] = ""
    gateway_url: str = ""
    model: str = ""
    provider: str = ""
    pi_bin: str = ""
    guild_id: str = ""
    channel_id: str = ""
    operator_id: str = ""
    permission_mode: str = Field(default="", pattern=r"^[A-Za-z-]*$")
    # Speech providers. Both empty means the user picks on the key page, which
    # shows every provider with its free usage; the first key saved there
    # decides, and that provider then both listens and speaks. A speaking
    # provider alone means Deepgram listens.
    stt_provider: str = Field(default="", pattern=rf"^({_SPEECH_PROVIDER}|local)?$")
    tts_provider: str = Field(default="", pattern=rf"^({_SPEECH_PROVIDER}|local)?$")
    # The provider the page shows first when the choice is open (the agent's
    # or the user's earlier preference); every other one stays a tap away.
    preferred_provider: str = Field(default="", pattern=rf"^{_SPEECH_PROVIDER}?$")
    # A voice the caller asked for; the pages then validate exactly this one.
    tts_voice_id: str = Field(default="", max_length=VOICE_ID_MAX_LENGTH, pattern=VOICE_ID_PATTERN)

    @property
    def speech_providers(self) -> set[str]:
        return {provider for provider in (self.stt_provider, self.tts_provider) if provider}

    @property
    def speech_key_providers(self) -> set[str]:
        return self.speech_providers & set(KEY_PROVIDERS)

    @property
    def open_choice(self) -> bool:
        """The key page offers every provider; nothing has been chosen yet."""
        return not self.tts_provider

    def chosen(self, provider: str) -> "InputSpec":
        """This request with one provider taking both roles."""
        return self.model_copy(update={"stt_provider": provider, "tts_provider": provider})
    language: str = Field(default="en-US", pattern=r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*$")
    # Reuse the speech provider, voice, and stored key of this profile (a
    # slug); the page then only asks for what the new surface needs, such as
    # a Discord bot token. ``start`` fills the provider fields from it.
    speech_from_profile: str = Field(default="", pattern=r"^([a-z0-9][a-z0-9-]{0,63})?$")
    replace_reference: str = Field(default="", pattern=r"^(credential:[0-9a-f]{32})?$")
    affected_profiles: list[str] = Field(default_factory=list, max_length=100)
    profile_path: str = ""

    @field_validator("gateway_url")
    @classmethod
    def public_gateway_address(cls, value):
        if value:
            from sleeptalk.providers import check_gateway_address
            check_gateway_address(value)
        return value

    @model_validator(mode="after")
    def terminal_choices(self):
        if "local" in self.speech_providers and not self.speech_from_profile:
            raise ValueError("Local speech must be reused from an existing profile with --speech-from-profile.")
        if self.stt_provider and not self.tts_provider:
            raise ValueError("Name the speaking provider too, or leave both providers out and let the key page offer every one.")
        if self.tts_provider and not self.stt_provider:
            self.stt_provider = "deepgram"
        if self.tts_voice_id and not self.tts_provider:
            raise ValueError("A requested voice needs its provider; pass --speech-provider with --tts-voice-id.")
        if self.preferred_provider and self.tts_provider:
            raise ValueError("A preferred provider only applies when the page offers every provider.")
        if self.auth_request and (self.auth or self.replace_reference):
            raise ValueError("Private model collection cannot also select a saved reference or repair operation.")
        if self.auth_request:
            validate_model_auth_labels(self.model, self.provider)
        if self.surface == "terminal" and (not self.capture_mode or not self.user_terminal_local):
            raise ValueError("Terminal setup requires a capture mode and a user-controlled terminal on the audio host.")
        if self.surface != "terminal" and (self.capture_mode or self.user_terminal_local):
            raise ValueError("Terminal choices apply only to the Terminal surface.")
        return self

    def agent_config(self):
        return AgentConfig(type=self.agent, workspace_dir=self.workspace,
                           on_incompatible="fail", **({"auth": self.auth} if self.auth else {}),
                           **({"options": self.options} if self.options else {}), **{name: getattr(self, name)
                           for name in ("gateway_url", "runtime", "permission_mode", "model", "provider", "pi_bin") if getattr(self, name)})


class SetupState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["waiting", "complete", "cancelled", "expired"]
    expires_at: float = Field(allow_inf_nan=False)
    created_at: float = Field(default=0, allow_inf_nan=False)
    refs: dict[str, str]
    details: dict[str, dict] = Field(default_factory=dict)
    pending: dict[str, dict] = Field(default_factory=dict)
    generation: int | None = None
    mode: Literal["setup", "repair"] = "setup"
    last_validation: dict | None = None
    # The provider the user picked on the key page when the request left it open.
    provider: str = Field(default="", pattern=rf"^{_SPEECH_PROVIDER}?$")


def _state(path: Path) -> dict:
    try:
        state = SetupState.model_validate(_read(path)).model_dump(exclude_none=True)
        if not set(state["refs"]) <= ENV_NAMES.keys() or any(not re.fullmatch(r"credential:[0-9a-f]{32}", ref) for ref in state["refs"].values()):
            raise ValueError
        return state
    except ValueError:
        raise InputError("Setup state is damaged; start a new request.") from None


def state_root() -> Path:
    return Path.home() / ".local/share/sleeptalk/setup"


def atomic_json(path: Path, value: dict) -> None:
    atomic_write(path, json.dumps(value).encode())


def atomic_write(path: Path, value: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".setup-")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        parent_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    finally:
        Path(tmp).unlink(missing_ok=True)


@contextmanager
def lease(path: Path):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise InputError("Another private setup owns this profile.") from None
        yield
    finally:
        os.close(fd)


def _read(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (OSError, ValueError):
        raise InputError("Setup state is unavailable or damaged; start a new request.") from None


def _directory(request_id: str, root: Path | None = None) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", request_id):
        raise InputError("Use a request ID of 8–128 letters, digits, underscores, or hyphens.")
    return (root or state_root()) / request_id


def status(request_id: str, *, root: Path | None = None) -> dict:
    directory = _directory(request_id, root)
    state = _state(directory / "state.json")
    phase = state["status"]
    if phase == "waiting" and (directory / "cancel.json").exists():
        phase = "cancelled"
        (directory / "invitation.json").unlink(missing_ok=True)
    elif phase == "waiting" and time.time() >= state["expires_at"]:
        phase = "expired"
        (directory / "invitation.json").unlink(missing_ok=True)
    elif phase == "waiting" and time.time() - state.get("created_at", 0) > 10:
        try:
            with lease(directory / "running.lock"):
                phase = "interrupted"
                (directory / "invitation.json").unlink(missing_ok=True)
        except InputError:
            pass
    result = {"schema_version": 1, "operation": "private_input", "request_id": request_id,
              "status": phase, "expires_at": state["expires_at"],
              "completed_providers": sorted(state.get("refs", {})),
              "profile_ready": phase == "complete" and state["mode"] == "setup",
              "credential_replaced": phase == "complete" and state["mode"] == "repair", "audio_acceptance": "unknown"}
    if isinstance(state.get("last_validation"), dict):
        result["last_validation"] = state["last_validation"]
    if state.get("provider"):
        result["speech_provider"] = state["provider"]
    try:
        spec = InputSpec.model_validate(_read(directory / "request.json"))
    except (InputError, ValueError):
        return result
    # What the page still has to collect, so an agent can see at a glance
    # that a Discord request will ask for a bot token, or that nothing is left.
    result["surface"] = spec.surface
    speech = spec.speech_key_providers or ({"speech"} if spec.open_choice and not state.get("provider") else set())
    wanted = speech | ({"discord"} if spec.surface == "discord" else set())
    result["pending_providers"] = sorted(wanted - set(state.get("refs", {})))
    return result


async def wait(request_id: str, *, timeout: float = LIFETIME, root: Path | None = None) -> dict:
    """Return status once the request leaves ``waiting``, or at the timeout."""
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        result = status(request_id, root=root)
        if result["status"] != "waiting" or time.monotonic() >= deadline:
            return result
        await asyncio.sleep(min(1.0, max(0.05, deadline - time.monotonic())))


def cancel(request_id: str, *, root: Path | None = None) -> dict:
    directory = _directory(request_id, root)
    result = status(request_id, root=root)
    if result["status"] in {"waiting", "interrupted"}:
        atomic_json(directory / "cancel.json", {})
        (directory / "invitation.json").unlink(missing_ok=True)
        result["status"] = "cancelled"
    return result


async def start(spec: InputSpec, *, user_browser_local: bool, root: Path | None = None,
                entry: Literal["local", "link"] = "local") -> dict:
    """Start private key entry.

    ``local`` serves a loopback page for the user's browser on this host.
    ``link`` returns a single-use phone setup link through the hosted call
    service (``sleeptalk.setup_link``) for a host the user reaches by chat.
    """
    if entry == "local" and not user_browser_local:
        raise InputError("Private local entry requires the user's browser on this host. Without one, use host input link to send the user a phone setup link.")
    if entry == "link" and spec.surface == "terminal":
        raise InputError("Phone setup links set up Web calls and Discord bots. Terminal audio needs setup on the host itself.")
    if spec.auth_request:
        raise InputError("Use host auth start for local model entry or host remote setup for paired entry.")
    from sleeptalk.runtime_probe import probe_runtime
    from sleeptalk.host import resolve_profile_path
    spec = spec.model_copy(update={"workspace": str(canonical_workspace(spec.workspace)),
                                  "profile_path": str(resolve_profile_path(profile=spec.profile).resolve())})
    from sleeptalk.setup import ensure_dependencies
    problems = ensure_dependencies(spec.surface, repair=False)
    if problems:
        return {"schema_version": 1, "operation": "private_input", "status": "needs_input",
                "surface": spec.surface, "problems": list(problems),
                "next_action": "Install the dependencies named in `problems` into this same environment (for Discord: "
                               "`python -m pip install 'sleeptalk[discord]'` with the python that runs sleeptalk), then retry."}
    report = await probe_runtime(spec.agent_config())
    if report["status"] != "ready":
        return {"schema_version": 1, "operation": "private_input", "status": "needs_input", "runtime": report}
    seeded_refs: dict[str, str] = {}
    seeded_details: dict[str, dict] = {}
    if spec.speech_from_profile:
        spec, seeded_refs, seeded_details = speech_seed(spec)
    if spec.replace_reference:
        metadata = CredentialStore().status(spec.replace_reference)
        if metadata.provider not in (spec.speech_providers or set(KEY_PROVIDERS)) | {"discord"} or set(metadata.profiles) != set(spec.affected_profiles):
            raise InputError("Inspect the credential and explicitly confirm every affected profile before replacing it.")
    directory = _directory(spec.request_id, root)
    with lease(directory.parent / "start.lock"):
        request_path = directory / "request.json"
        if request_path.exists():
            try:
                previous = InputSpec.model_validate(_read(request_path)).model_dump()
            except ValueError:
                raise InputError("This request ID belongs to an unreadable earlier request. Start a new request ID.") from None
            if previous != spec.model_dump():
                raise InputError("This request ID already belongs to different setup choices.")
            result = status(spec.request_id, root=root)
            if result["status"] not in {"waiting", "interrupted"}:
                return result
        else:
            atomic_json(request_path, spec.model_dump())
            atomic_json(directory / "state.json", {"status": "waiting", "created_at": time.time(), "expires_at": time.time() + LIFETIME,
                                                   "refs": seeded_refs, "details": seeded_details})
        launched = directory / "launch.json"
        recent_launch = launched.exists() and time.time() - _read(launched).get("time", 0) < 10
        if not (directory / "invitation.json").exists() and not recent_launch:
            (directory / "failure.json").unlink(missing_ok=True)
            atomic_json(launched, {"time": time.time()})
            module = "sleeptalk.setup_link" if entry == "link" else "sleeptalk.private_input"
            subprocess.Popen([sys.executable, "-m", module, "--directory", str(directory)],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             close_fds=True, start_new_session=True)
    for _ in range(100):
        current = status(spec.request_id, root=root)
        if current["status"] == "complete":
            return current
        if (directory / "invitation.json").exists():
            return {**status(spec.request_id, root=root), **_read(directory / "invitation.json")}
        if (directory / "failure.json").exists():
            failure = _read(directory / "failure.json")
            if failure.get("code") == "call_service" and isinstance(failure.get("message"), str):
                raise InputError(failure["message"])
            raise InputError("Private setup could not start; check profile ownership and local networking.")
        await asyncio.sleep(0.05)
    raise InputError("Private setup did not become ready. Check status before retrying this request.")


def speech_seed(spec: InputSpec, *, source_path: Path | None = None) -> tuple[InputSpec, dict[str, str], dict[str, dict]]:
    """Take the speech providers, voice, and key references from an existing
    profile so this request only collects what its surface adds.

    Returns the request with its provider fields filled, plus the references
    and details to seed the state with. ``InputSession.restore`` drops any
    reference the store no longer holds, and the page asks for that key.
    """
    from sleeptalk.host import resolve_profile_path
    source = spec.speech_from_profile
    path = source_path if source_path is not None else resolve_profile_path(profile=source)
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        raise InputError(f"Profile {source!r} has no readable speech setup to reuse; set it up first.") from None
    stt = document.get("stt", {}) if isinstance(document.get("stt"), dict) else {}
    tts = document.get("tts", {}) if isinstance(document.get("tts"), dict) else {}
    credentials = document.get("credentials", {}) if isinstance(document.get("credentials"), dict) else {}
    refs = credentials.get("refs", {}) if isinstance(credentials.get("refs"), dict) else {}
    stt_provider, tts_provider = str(stt.get("provider", "")), str(tts.get("provider", ""))
    stt_model, voice_id = str(stt.get("model", "")), str(tts.get("voice_id", ""))
    reusable = {*KEY_PROVIDERS, "local"}
    if stt_provider not in reusable or tts_provider not in reusable or not stt_model or not voice_id:
        raise InputError(f"Profile {source!r} was not set up through private key entry, so its speech setup can't be reused. Leave --speech-from-profile out and let the page collect a key.")
    for given, name in ((spec.stt_provider, "stt_provider"), (spec.tts_provider, "tts_provider")):
        if given and given != (stt_provider if name == "stt_provider" else tts_provider):
            raise InputError("--speech-from-profile reuses that profile's providers; don't pass a provider flag with it.")
    seeded_refs: dict[str, str] = {}
    details: dict[str, dict] = {}
    for provider in (stt_provider, tts_provider):
        details.setdefault(provider, {})
        if provider == "local":
            continue
        reference = refs.get(ENV_NAMES[provider])
        if not isinstance(reference, str) or not re.fullmatch(r"credential:[0-9a-f]{32}", reference):
            raise InputError(f"Profile {source!r} keeps its {provider} key outside the private store, so it can't be reused. Leave --speech-from-profile out and let the page collect a key.")
        seeded_refs[provider] = reference
    details[stt_provider]["stt_model"] = stt_model
    details[tts_provider]["voice_id"] = voice_id
    if tts.get("model"):
        details[tts_provider]["tts_model"] = str(tts["model"])
    if "speed" in tts:
        details[tts_provider]["tts_speed"] = tts["speed"]
    language = str(tts.get("language") or stt.get("language") or spec.language)
    return spec.model_copy(update={"stt_provider": stt_provider, "tts_provider": tts_provider, "language": language}), seeded_refs, details


async def validate_provider(provider: str, value: str, spec: InputSpec, voice_id: str = "", *,
                            stt_model: str = "", tts_model: str = "") -> dict:
    """Prove one credential for every role the setup gives it (STT, voice, or both).

    ``stt_model`` and ``tts_model`` are models the person chose; the check then
    runs on those instead of the provider's defaults.
    """
    if provider == "discord":
        from dataclasses import asdict
        from sleeptalk.discord_check import fetch_application, list_guilds
        app = await fetch_application(value)
        guilds = await list_guilds(value)
        return {"application": asdict(app), "guilds": [asdict(g) for g in guilds]}
    details: dict = {}
    if provider == spec.stt_provider:
        from sleeptalk.stt_roundtrip import stt_roundtrip
        from sleeptalk.builder import stt_spec
        from sleeptalk.config import STTConfig
        from sleeptalk.setup_core import STT_DEFAULT_MODELS, SetupAnswers, apply_degraded_stt_model
        answers = SetupAnswers(stt_provider=provider, stt_model=stt_model or STT_DEFAULT_MODELS[provider],
                                language=spec.language)
        if not stt_model:
            apply_degraded_stt_model(answers)
        await stt_roundtrip(provider, stt_spec(STTConfig(provider=provider, model=answers.stt_model, language=spec.language)), value)
        details["stt_model"] = answers.stt_model
    if provider == spec.tts_provider:
        from sleeptalk.setup_core import (
            DEFAULT_VOICES,
            SetupAnswers,
            _prove_selected_voice,
            is_english,
        )
        answers = SetupAnswers(tts_provider=provider, tts_key=value, language=spec.language, tts_voice_id=voice_id,
                                tts_model=tts_model)
        if not answers.tts_voice_id and provider in DEFAULT_VOICES and is_english(spec.language):
            # The same English default the human wizard picks.
            answers.tts_voice_id = DEFAULT_VOICES[provider]
        if not answers.tts_voice_id:
            from sleeptalk.voice_discovery import default_catalogs, rank_voices, voices_for_language
            catalog = next((c for c in default_catalogs({provider: value}) if c.name == provider), None)
            if catalog is None:
                # No voice library to search (the aggregators): the language's recommended voice.
                from sleeptalk.speech_catalog import recommended_voice
                answers.tts_voice_id = recommended_voice(provider, spec.language)
            else:
                # rank_voices is free-text search; a BCP-47 tag such as en-US matches
                # nothing there. Filter by language first, then rank.
                picks = rank_voices(voices_for_language(await catalog.list_voices(), spec.language), "")
                answers.tts_voice_id = picks[0].voice.voice_id if picks else ""
            if not answers.tts_voice_id:
                raise InputError("voice_unavailable")
        sample = await _prove_selected_voice(answers)
        sample.unlink(missing_ok=True)
        details["voice_id"] = answers.tts_voice_id
    if not details:
        raise InputError(f"{provider} has no role in this setup.")
    return details


_VALIDATION_CODES = frozenset({"invalid_credential", "permission_denied", "voice_unavailable",
                               "quota_or_rate_limit", "network_timeout", "storage_unavailable"})


def _validation_code(exc: Exception) -> str:
    if isinstance(exc, InputError) and str(exc) in _VALIDATION_CODES:
        return str(exc)
    if isinstance(exc, TimeoutError):
        return "network_timeout"
    if isinstance(exc, CredentialError):
        return "storage_unavailable"
    # Legacy provider adapters include status in exception text. Never emit it.
    code = getattr(exc, "status", None)
    if code is None:
        match = re.search(r"\b(401|403|404|429)\b", str(exc))
        code = int(match[0]) if match else None
    return {401: "invalid_credential", 403: "permission_denied", 404: "voice_unavailable", 429: "quota_or_rate_limit"}.get(code, "validation_failed")


async def prepare_profile(spec: InputSpec, refs: dict, details: dict, store: CredentialStore,
                          body: dict, profile_path: Path, directory: Path, *, agent_from_profile: bool = False) -> str:
    """Validate provider choices and runtime, then return a reference-only profile.

    ``agent_from_profile``: the agent settings are copied from a profile the
    person already set up (a Discord bot made on the settings page), so the
    runtime probe, which checks an agent's own choices, is skipped. It can't
    verify every working setup: pi signed in without a native API key, or
    Hermes started per call with no dashboard URL.
    """
    if spec.open_choice:
        raise web.HTTPConflict(text="Choose a speech provider and validate its key before finishing.")
    required = spec.speech_key_providers | ({"discord"} if spec.surface == "discord" else set())
    if required - refs.keys():
        raise web.HTTPConflict(text="Validate each provider before finishing.")
    from sleeptalk.setup_core import SetupAnswers, profile_toml
    answers = SetupAnswers(name=spec.profile, slug=spec.profile, surface=spec.surface,
        capture_mode=spec.capture_mode,
        agent_type=spec.agent, workspace_dir=spec.workspace, tts_provider=spec.tts_provider,
        language=spec.language, tts_voice_id=details[spec.tts_provider]["voice_id"],
        tts_model=details[spec.tts_provider].get("tts_model", ""),
        tts_speed=details[spec.tts_provider].get("tts_speed", 1.0),
        stt_provider=spec.stt_provider, stt_model=details[spec.stt_provider]["stt_model"],
        token_env="DISCORD_BOT_TOKEN")
    discord_lines = ""
    if spec.surface == "discord":
        from sleeptalk.discord_check import list_voice_channels
        guild, channel, operator = (body.get(k, "") for k in ("guild_id", "channel_id", "operator_id"))
        if not all(isinstance(v, str) and re.fullmatch(r"[0-9]{1,20}", v) for v in (guild, channel, operator)):
            raise ChoiceError("discord_choices", "Choose a server, channel, and your Discord user ID.")
        discord_details = details["discord"]
        if guild not in {g["id"] for g in discord_details["guilds"]}:
            raise ChoiceError("bot_not_in_server", "The bot has not joined this server; add it with the invite link, then refresh the server list.")
        try:
            async with asyncio.timeout(20):
                channels = await list_voice_channels(store.get(refs["discord"]), guild)
        except Exception:
            raise ChoiceError("channel_unverified", "Channel permissions could not be verified.") from None
        if not any(c.id == channel and c.eligible for c in channels):
            raise ChoiceError("channel_not_allowed", "Choose a voice channel where the bot can connect and speak.")
        answers.guild_id, answers.client_id = guild, discord_details["application"]["id"]
        answers.authorization_required = True
        answers.allowed_channel_ids, answers.operator_ids = channel, operator
        discord_lines = 'presence_auto_join = false\n'
    document = profile_toml(answers)
    agent_choices = {'on_incompatible': 'fail'}
    for name in ("runtime", "permission_mode", "gateway_url", "model", "provider", "pi_bin"):
        if getattr(spec, name):
            agent_choices[name] = getattr(spec, name)
    document = document.replace('[agent]\n', '[agent]\n' + ''.join(f'{key} = {json.dumps(value)}\n' for key, value in agent_choices.items()))
    if spec.auth:
        document += "\n[agent.auth]\n" + "".join(f'{key} = {json.dumps(value)}\n' for key, value in spec.auth.model_dump().items())
    if spec.options:
        from sleeptalk.agent_setup_options import toml_value
        document += "\n[agent.options]\n" + "".join(f'{json.dumps(key)} = {toml_value(value)}\n' for key, value in spec.options.items())
    if discord_lines:
        document = document.replace('[transport]\n', '[transport]\n' + discord_lines)
    from sleeptalk.config import _merge_toml, _validate_component_choices, packaged_example_root, shared_root_for_project, project_root_for_profiles
    home = project_root_for_profiles(profile_path.parent) or profile_path.parent
    with lease(directory / "shared-root.lock"):
        root = shared_root_for_project(home)
        if not root.exists():
            atomic_write(root, packaged_example_root().read_bytes())
    cfg = SleeptalkConfig.model_validate(_merge_toml(tomllib.loads(root.read_text()), tomllib.loads(document)))
    from sleeptalk.defaults import register_defaults
    register_defaults()
    if _validate_component_choices(cfg):
        raise web.HTTPConflict(text="These profile settings do not apply to the selected components. Correct the setup choices before retrying.")
    from sleeptalk.runtime_probe import probe_runtime
    if not agent_from_profile:
        report = await probe_runtime(cfg.agent)
        if report["status"] != "ready":
            raise web.HTTPConflict(text="Agent login or permission settings changed. Repair the runtime and retry.")
    document += binding_toml(bind(cfg.agent))
    document += "\n[credentials.refs]\n" + "".join(f'{ENV_NAMES[p]} = {json.dumps(r)}\n' for p, r in refs.items())
    return document


class InputSession:
    def __init__(self, directory: Path, *, store: CredentialStore | None = None, validator=validate_provider):
        self.directory = directory
        self.spec = InputSpec.model_validate(_read(directory / "request.json"))
        self.state = _state(directory / "state.json")
        self.store = store or CredentialStore()
        self.validator = validator
        self.lock = asyncio.Lock()
        self.profile_path = Path(self.spec.profile_path) if self.spec.profile_path else Path.home() / ".sleeptalk/profiles" / (self.spec.profile + ".toml")
        self.profile_id = hashlib.sha256(str(self.profile_path.resolve()).encode()).hexdigest()
        self.profile_label = self.spec.profile + "@" + self.profile_id[:12]
        self.progress_path = directory.parent / (self.profile_id + ".progress.json")
        self.repair_provider = self.store.status(self.spec.replace_reference).provider if self.spec.replace_reference else None
        if self.repair_provider and "generation" not in self.state:
            self.state["mode"] = "repair"
            self.state["generation"] = self.store.status(self.spec.replace_reference).generation
            self.save()

    def restore(self):
        if self.repair_provider and self.store.replacement_completed(self.spec.replace_reference, self.spec.request_id):
            self.state["status"] = "complete"
            self.state["refs"][self.repair_provider] = self.spec.replace_reference
            self.save()
            return
        choices = self.spec.model_dump(exclude={"request_id"})
        if self.progress_path.exists() and not self.repair_provider:
            progress = _read(self.progress_path)
            if progress.get("choices") == choices:
                self.state["refs"].update(progress.get("refs", {}))
                self.state.setdefault("details", {}).update(progress.get("details", {}))
        for provider, details in self.state.get("pending", {}).items():
            reference = "credential:" + uuid.uuid5(uuid.NAMESPACE_URL, provider + "\0" + self.spec.request_id + ":" + provider).hex
            try:
                if self.store.status(reference).available:
                    self.state["refs"][provider] = reference
                    self.state.setdefault("details", {})[provider] = details
            except CredentialNotFoundError:
                pass
        for provider, reference in list(self.state["refs"].items()):
            try:
                metadata = self.store.status(reference)
                valid = metadata.available and metadata.provider == provider
            except CredentialNotFoundError:
                valid = False
            if not valid:
                del self.state["refs"][provider]
                self.state.get("details", {}).pop(provider, None)
                self.state.get("pending", {}).pop(provider, None)
        if self.spec.open_choice and not self.state.get("provider"):
            for provider in KEY_PROVIDERS:
                if provider in self.state["refs"]:
                    self.state["provider"] = provider
                    break
        self.save()

    @property
    def choices(self) -> InputSpec:
        """The request with the page's provider choice applied, once it is made."""
        if self.spec.open_choice and self.state.get("provider"):
            return self.spec.chosen(self.state["provider"])
        return self.spec

    def save(self):
        atomic_json(self.directory / "state.json", self.state)
        if not self.repair_provider:
            atomic_json(self.progress_path, {"choices": self.spec.model_dump(exclude={"request_id"}),
                                           "refs": self.state["refs"], "details": self.state.get("details", {})})

    def active(self):
        if self.state["status"] != "waiting" or time.time() >= self.state["expires_at"] or (self.directory / "cancel.json").exists():
            raise web.HTTPGone(text="This setup invitation ended. Return to your agent for a new request.")

    def allowed_providers(self) -> set[str]:
        if self.repair_provider:
            return {self.repair_provider}
        choices = self.choices
        speech = set(KEY_PROVIDERS) if choices.open_choice else choices.speech_key_providers
        return speech | ({"discord"} if choices.surface == "discord" else set())

    async def accept(self, provider, value, voice="", *, confirm_replacement: bool = False) -> dict:
        """Validate one provider credential and store it.

        Returns ``already_validated``, ``validated``, ``complete`` (repair), or
        ``needs_input`` with a fixed code. Raises an aiohttp HTTP error for a
        request this setup can't accept; callers other than the page map it.
        """
        async with self.lock:
            self.active()
            if self.repair_provider and not confirm_replacement:
                raise web.HTTPBadRequest(text="Confirm replacement for every affected profile.")
            if not isinstance(provider, str) or provider not in self.allowed_providers() or not isinstance(value, str) or not 1 <= len(value) <= 8192 or not isinstance(voice, str) or len(voice) > 200:
                raise web.HTTPBadRequest()
            if provider in self.state["refs"]:
                return {"status": "already_validated"}
            # An open request is decided by the first key the page saves: that
            # provider then listens and speaks.
            spec = self.choices
            deciding = spec.open_choice and provider in KEY_PROVIDERS
            if deciding:
                spec = spec.chosen(provider)
            try:
                async with asyncio.timeout(45):
                    if provider == spec.tts_provider and spec.tts_voice_id:
                        if voice and voice != spec.tts_voice_id:
                            raise web.HTTPBadRequest(text="This request is bound to the selected voice. Start a new request to change it.")
                        voice = spec.tts_voice_id
                    details = await self.validator(provider, value, spec, voice)
                self.active()
                if deciding:
                    self.state["provider"] = provider
                self.state.setdefault("pending", {})[provider] = details
                self.save()
                if self.repair_provider:
                    self.store.replace_validated(self.spec.replace_reference, value, affected_profiles=self.spec.affected_profiles,
                                                 expected_generation=self.state["generation"], request_id=self.spec.request_id)
                    reference = self.spec.replace_reference
                else:
                    reference = self.store.put_validated(provider, value, request_id=self.spec.request_id + ":" + provider)
                self.state["refs"][provider] = reference
                self.state.setdefault("details", {})[provider] = details
                self.save()
                if self.repair_provider:
                    self.state["status"] = "complete"
                    self.save()
            except web.HTTPException:
                raise
            except Exception as exc:
                # A value-free record of why the check failed, for `host input
                # status`: the class and HTTP status, never the message.
                http_status = getattr(exc, "status", None)
                self.state["last_validation"] = {
                    "provider": provider, "code": _validation_code(exc), "error": type(exc).__name__,
                    "http_status": http_status if isinstance(http_status, int) else None,
                }
                self.save()
                return {"status": "needs_input", "code": _validation_code(exc)}
            finally:
                value = ""
            return {"status": "complete" if self.repair_provider else "validated"}

    def discord_summary(self) -> dict:
        """What the page needs after the bot token is saved: the application's
        name and invite link, and the servers the bot is in so far."""
        details = self.state.get("details", {}).get("discord", {})
        application = details.get("application", {})
        return {"application": {"name": application.get("name", ""), "invite_url": application.get("invite_url", "")},
                "guilds": details.get("guilds", [])}

    async def discord_options(self, guild_id) -> dict:
        """Refresh the bot's servers and list one server's voice channels.

        Raises ``LookupError`` before the token is saved and ``RuntimeError``
        when Discord can't be asked; callers map those to their own errors.
        """
        if "discord" not in self.state["refs"]:
            raise LookupError("discord token not saved")
        from dataclasses import asdict
        from sleeptalk.discord_check import list_guilds, list_voice_channels
        async with self.lock:
            try:
                async with asyncio.timeout(20):
                    token = self.store.get(self.state["refs"]["discord"])
                    guilds = await list_guilds(token)
                    # No server picked yet (or the bot left it): show the first one's channels.
                    if guilds and guild_id not in {g.id for g in guilds}:
                        guild_id = guilds[0].id
                    channels = await list_voice_channels(token, guild_id) if guilds else []
            except Exception:
                raise RuntimeError("discord unavailable") from None
            finally:
                token = ""
            self.state["details"]["discord"]["guilds"] = [asdict(g) for g in guilds]
            self.save()
            return {"guilds": [asdict(g) for g in guilds], "guild_id": guild_id if guilds else "",
                    "channels": [asdict(c) for c in channels]}

    async def complete(self, body: dict) -> dict:
        """Write the reference-only profile once every provider is validated."""
        async with self.lock:
            self.active()
            references = list(self.state["refs"].values())
            if self.spec.auth:
                references.append(self.spec.auth.credential_ref)
            generations = {ref: self.store.status(ref).generation for ref in references}
            document = await prepare_profile(self.choices, self.state["refs"], self.state["details"],
                self.store, body, self.profile_path, self.directory.parent)
            self.active()
            if self.profile_path.exists() and self.profile_path.read_text() != document:
                raise web.HTTPConflict(text="This profile already exists. Choose another profile or repair it explicitly.")
            # Registration precedes the atomic profile write, so interrupted writes
            # conservatively report an affected profile instead of losing ownership.
            with self.store.bind_profile_generation(generations, self.profile_label):
                atomic_write(self.profile_path, document.encode())
            self.state["status"] = "complete"
            self.save()
            (self.directory / "invitation.json").unlink(missing_ok=True)
            message = ("Profile saved. Start voice from your own terminal on this audio host, approve microphone access, and verify that you can hear and be heard."
                       if self.spec.surface == "terminal" else "Profile saved. Return to your agent to start voice and verify audio.")
            return {"status": "complete", "message": message}


async def serve(directory: Path):
    session = InputSession(directory)
    lock_name = hashlib.sha256(str(session.profile_path).encode()).hexdigest()
    with lease(directory.parent / (lock_name + ".lock")), lease(directory / "running.lock"):
        session.active()
        session.restore()
        if session.state["status"] == "complete":
            return
        # The request opens in the settings page, on this machine's loopback.
        from sleeptalk.settings_page import SettingsPage

        page = SettingsPage(session.profile_path, profiles_dir=session.profile_path.parent, request=session)
        ready: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        server = asyncio.create_task(page.serve(open_browser=False, ready=lambda url: ready.done() or ready.set_result(url)))
        try:
            url = await asyncio.wait_for(ready, 15)
            atomic_json(directory / "invitation.json", {"input_url": url})
            await _serve_until_done(session, directory)
        finally:
            (directory / "invitation.json").unlink(missing_ok=True)
            server.cancel()
            await asyncio.gather(server, return_exceptions=True)


# After the request completes the page stays up this long, so the person can
# keep adjusting settings before closing it.
AFTER_COMPLETE_SECONDS = 10 * 60


async def _serve_until_done(session: "InputSession", directory: Path, *, stop=None) -> None:
    """Wait while the request is open; record a cancel or an expiry; then allow a
    short grace for settings after completion. ``stop`` ends it early."""
    completed_at = None
    while not (stop is not None and stop.is_set()):
        await asyncio.sleep(0.25)
        status_now = session.state["status"]
        if status_now == "complete":
            completed_at = completed_at or time.monotonic()
            if time.monotonic() - completed_at >= AFTER_COMPLETE_SECONDS:
                return
        elif status_now == "waiting":
            if (directory / "cancel.json").exists():
                session.state["status"] = "cancelled"
                session.save()
                return
            if time.time() >= session.state["expires_at"]:
                session.state["status"] = "expired"
                session.save()
                return
        else:
            return


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    try:
        asyncio.run(serve(args.directory))
    except Exception:
        atomic_json(args.directory / "failure.json", {"code": "setup_unavailable"})
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
