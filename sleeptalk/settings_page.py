"""The settings page: one profile, every section, in the browser on this host.

``sleeptalk settings --open`` serves it on the loopback interface with a
single-use token in the URL fragment, exactly like the private key page, and
opens the browser. The page reads the profile as it is on disk, offers the
choices the wizard offers, and writes them straight back to the profile:
voice, speed, language, delivery, and behaviour need no secret; switching
the speech service takes the key on the page itself, which is safe here
because the page is loopback-only behind a single-use token, the same
boundary the private key page relies on. The key is proved, stored in the
private credential store, and the profile keeps a reference. Changing the
agent needs its own login, so that card points at the terminal.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import platform
import re
import secrets
import sys
import time
import tomllib
import webbrowser
from pathlib import Path
from typing import Any

from aiohttp import web

from sleeptalk.speech_speed import SPEED_STEPS

logger = logging.getLogger("sleeptalk.settings_page")

SECTIONS = (
    ("agent", "Agent"),
    ("speech", "Speech service"),
    ("voice", "Voice"),
    ("language", "Language"),
    ("delivery", "Call delivery"),
    ("discord", "Discord"),
    ("behaviour", "Behaviour"),
    ("keys", "Keys"),
)

# What the direct speech services listen and speak with; the aggregators
# carry their own defaults on their presets.
_DIRECT_SPEECH = {
    "deepgram": {"listen": "Nova-3, streamed", "speak": "Flux voices", "note": "Streams words as you say them and decides the end of your turn itself."},
    "cartesia": {"listen": "Ink-2, streamed", "speak": "Sonic 3.6 voices", "note": "Streams words as you say them and decides the end of your turn itself."},
    "elevenlabs": {"listen": "Scribe v2", "speak": "ElevenLabs voices", "note": "Listens one turn at a time."},
    "local": {"listen": "Moonshine", "speak": "Piper or Kokoro", "note": "Runs on this machine; no key; lower voice quality."},
}

# OpenAI Realtime: the hybrid route, offered as a speech
# service. One OpenAI key; Realtime hears the audio and speaks itself, and
# hands work to the agent. The profile keeps it as [route], not [stt]/[tts].
REALTIME = "openai"
_REALTIME_FACTS = {
    "id": REALTIME, "name": "OpenAI Realtime",
    "tagline": "Hears you and talks back itself, and hands work to your agent.",
    "free": "No free usage", "card": "Needs an OpenAI account with billing set up.",
    "fine_print": "Realtime decides what to hand to your agent; ask it to check with the agent when it should.",
    "key_url": "https://platform.openai.com/api-keys",
    "key_help": "Create a key under API keys.",
    "usage": "Billed by OpenAI per minute of audio in and out, which costs more than the other services for the same call. It hears you and speaks in one step, so it starts answering sooner, and it hands anything it can't know to your agent.",
    "listen": "Realtime, hears you directly", "speak": "Realtime voices",
    "note": "Hears you and answers in one step; hands work to your agent.",
}


def _is_realtime(document: dict) -> bool:
    return str(_get(document, "route", "type", default="cascade")) == "hybrid"


def _realtime_voices() -> tuple[str, ...]:
    from sleeptalk.voice_catalog import curated

    return tuple(voice.voice_id for voice in curated(REALTIME))


def _default_realtime_model(surface: str = "web") -> str:
    from sleeptalk.setup_core import DEFAULT_REALTIME_MODEL

    return "gpt-live-1" if surface == "web" else DEFAULT_REALTIME_MODEL


def _realtime_card(document: dict, refs: Any, language: str) -> dict[str, Any]:
    """The Realtime service in the same shape as the catalog's services."""
    from sleeptalk.setup_core import DEFAULT_REALTIME_MODEL, DEFAULT_REALTIME_VOICE

    in_use = _is_realtime(document)
    default_model = _default_realtime_model(str(_get(document, "transport", "type", default="web")))
    model = str(_get(document, "route", "model", default="") or DEFAULT_REALTIME_MODEL) if in_use else default_model
    voice = str(_get(document, "route", "voice", default="") or DEFAULT_REALTIME_VOICE)
    common = {"provider": REALTIME, "id": model, "label": "GPT Realtime", "languages": None, "detects_language": True,
              "transport": "streaming", "usd_per_hour": None, "model_is_voice": False, "no_hint_for": [],
              "usable": True, "covers_language": True, "source": "curated", "recommended": True, "latency_ms": None}
    models = {
        "stt": {"models": [{**common, "role": "stt", "voices": None, "default_voice": "",
                            "notes": "Hears your audio directly; the transcript is only for the record."}],
                "recommended": default_model, "measured_on": ""},
        "tts": {"models": [{**common, "role": "tts", "voices": list(_realtime_voices()), "default_voice": "marin",
                            "notes": "Speaks the answer itself, in one of its own voices."}],
                "recommended": default_model, "measured_on": ""},
    }
    for role in ("stt", "tts"):
        template = models[role]["models"][0]
        choices = list(dict.fromkeys(["gpt-live-1", DEFAULT_REALTIME_MODEL, model]))
        models[role]["models"] = [
            {**template, "id": choice,
             "label": "GPT Live" if choice == "gpt-live-1" else "GPT Realtime",
             "recommended": False,
             "notes": "Full duplex browser calls: listens while speaking. Voice is fixed for each call."
                      if choice == "gpt-live-1" else template["notes"]}
            for choice in choices
        ]
    facts = dict(_REALTIME_FACTS)
    if model == "gpt-live-1":
        facts.update(listen="GPT-Live-1, continuous audio", speak="GPT-Live-1 voices",
                     note="Listens while speaking and delegates work to your agent.",
                     usage="OpenAI bills the whole Live session duration, including silence. Agent usage is separate.")
    return {**facts, "primary": False, "in_use": in_use, "takes_key": True, "models": models,
            "current": {"stt": model, "tts": model}, "voice": voice,
            "key_saved": bool(in_use and isinstance(refs, dict) and "OPENAI_API_KEY" in refs)}


# Languages the page itself is written in (English is the source; the rest are in _settings/i18n).
UI_LANGUAGES = ("es", "fr", "de", "it", "pt", "nl", "ja", "ko", "zh", "hi")
_LANGUAGES = ("en-US", "en-GB", "es-ES", "es-MX", "fr-FR", "de-DE", "it-IT", "pt-BR", "nl-NL", "ja-JP", "ko-KR", "zh-CN", "hi-IN")


_SAMPLES = Path(__file__).parent / "voice_samples"
_ICONS = Path(__file__).parent / "_settings" / "icons"


def brand_icons() -> list[str]:
    """Logos the page can show, by service or agent id (see _settings/icons/NOTICE.txt)."""
    return sorted(path.stem for path in _ICONS.glob("*.svg")) if _ICONS.is_dir() else []


def voice_samples(language: str = "en-US") -> dict[str, dict[str, dict[str, str]]]:
    """Recorded samples that ship with Sleeptalk, in one language: provider -> voice -> URL.

    Only samples spoken in the page's language are offered, so a Spanish page
    never plays an English preview. They play with no key, on every card;
    scripts/make_voice_samples.py makes them.
    """
    try:
        manifest = json.loads((_SAMPLES / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    wanted = (language or "en").strip().lower().replace("_", "-").split("-", 1)[0] or "en"
    out: dict[str, dict[str, dict[str, str]]] = {}
    for provider, voices in manifest.items():
        for key, item in (voices or {}).items():
            if not isinstance(item, dict) or str(item.get("language", "en")) != wanted:
                continue
            if (_SAMPLES / str(item.get("file", ""))).is_file():
                voice = str(item.get("voice") or key.split("@", 1)[0])
                out.setdefault(provider, {})[voice] = {"url": "/samples/" + item["file"], "language": wanted,
                                                       "name": str(item.get("name", "")),
                                                       "model": str(item.get("model", ""))}
    return out


def _sample_files() -> set[str]:
    """Every file the manifest lists, in any language: the only paths /samples serves."""
    try:
        manifest = json.loads((_SAMPLES / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    files = {str(item.get("file", "")) for voices in manifest.values() for item in (voices or {}).values() if isinstance(item, dict)}
    return {name for name in files if name and (_SAMPLES / name).is_file()}


def _read(path: Path) -> dict:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def _get(document: dict, *keys: str, default: Any = "") -> Any:
    node: Any = document
    for key in keys:
        node = node.get(key) if isinstance(node, dict) else None
    return default if node is None else node


def _missing_discord_extra() -> str:
    try:
        from sleeptalk.sysdeps import missing_discord_extra

        return missing_discord_extra() or ""
    except Exception:  # noqa: BLE001
        return ""


def _free_slug(profiles_dir: Path, base: str) -> str:
    slug, n = base, 2
    while (profiles_dir / f"{slug}.toml").exists():
        slug, n = f"{base}-{n}", n + 1
    return slug


class DiscordSetup:
    """The bot token, once proved and stored, plus what Discord told us about it."""

    def __init__(self) -> None:
        self.reference = ""
        self.details: dict[str, Any] = {}

    async def accept_token(self, value: str, *, store, profile_path: Path) -> dict[str, Any]:
        from sleeptalk.private_input import InputSpec, _validation_code, validate_provider

        if not isinstance(value, str) or not value.strip() or len(value) > 8192:
            raise ValueError("invalid_credential")
        spec = InputSpec(request_id="settings-discord-token", profile=profile_path.stem, workspace=str(Path.cwd()),
                         agent="pi", surface="discord", stt_provider="cartesia", tts_provider="cartesia")
        try:
            async with asyncio.timeout(45):
                self.details = await validate_provider("discord", value.strip(), spec, "")
        except Exception as exc:  # noqa: BLE001
            raise ValueError(_validation_code(exc)) from None
        self.reference = store.put_validated("discord", value.strip(), request_id=f"settings-discord:{secrets.token_hex(8)}")
        return self.summary()

    async def load_existing(self, profile_path: Path, *, store) -> dict[str, Any]:
        """Use the bot token a Discord profile already references (never shown)."""
        from dataclasses import asdict

        from sleeptalk.discord_check import fetch_application, list_guilds

        document = _read(profile_path)
        token_env = str(_get(document, "transport", "token_env", default="") or "DISCORD_BOT_TOKEN")
        refs = _get(document, "credentials", "refs", default={})
        reference = refs.get(token_env, "") if isinstance(refs, dict) else ""
        if not reference:
            raise ValueError("token_missing")
        token = store.get(reference)
        try:
            async with asyncio.timeout(20):
                application = await fetch_application(token)
                guilds = await list_guilds(token)
        except Exception:  # noqa: BLE001 — a code, never a Discord body
            raise ValueError("discord_unavailable") from None
        finally:
            token = ""
        self.reference = reference
        self.details = {"application": {"name": application.name, "invite_url": application.invite_url},
                        "guilds": [asdict(g) for g in guilds]}
        return self.summary()

    def summary(self) -> dict[str, Any]:
        application = self.details.get("application", {})
        return {"application": {"name": application.get("name", ""), "invite_url": application.get("invite_url", "")},
                "guilds": self.details.get("guilds", [])}

    async def options(self, guild_id: str, *, store, refresh: bool = True) -> dict[str, Any]:
        """Servers and one server's voice channels. ``refresh=False`` reuses the
        server list just fetched (Discord rate-limits back-to-back listings)."""
        from dataclasses import asdict

        from sleeptalk.discord_check import list_guilds, list_voice_channels

        token = store.get(self.reference)
        try:
            async with asyncio.timeout(20):
                if refresh or not self.details.get("guilds"):
                    self.details["guilds"] = [asdict(g) for g in await list_guilds(token)]
                known = [g.get("id") for g in self.details.get("guilds", [])]
                # No server picked yet (or the bot left it): show the first one's channels.
                if known and guild_id not in known:
                    guild_id = known[0]
                channels = await list_voice_channels(token, guild_id) if known else []
        finally:
            token = ""
        return {**self.summary(), "guild_id": guild_id if known else "", "channels": [asdict(c) for c in channels]}


async def create_discord_profile(source: Path, setup: DiscordSetup, *, slug: str, guild_id: str, channel_id: str,
                                 operator_id: str, store, state_dir: Path | None = None) -> dict[str, Any]:
    """Write a Discord profile beside ``source``, reusing its speech setup and the proved token."""
    import re
    import uuid

    from sleeptalk.private_input import InputSpec, atomic_write, prepare_profile, speech_seed

    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", slug):
        raise ValueError("bad_profile_name")
    path = source.parent / f"{slug}.toml"
    if path.exists():
        raise ValueError("profile_exists")
    if not setup.reference:
        raise ValueError("token_missing")
    document = _read(source)
    from sleeptalk.host import _CALLER_APPROVAL_AGENTS

    agent = str(_get(document, "agent", "type", default="pi"))
    # The bot runs the same agent as the profile it comes from: its runtime,
    # model, gateway, and options carry over. Agents that ask before using a
    # tool get "pending" (the caller is asked) when the source sets no
    # permission mode, as `sleeptalk host` does.
    fields: dict[str, Any] = {name: str(value) for name in ("runtime", "permission_mode", "gateway_url", "model", "provider", "pi_bin")
                              if (value := _get(document, "agent", name, default=""))}
    if agent in _CALLER_APPROVAL_AGENTS:
        fields.setdefault("permission_mode", "pending")
    options = _get(document, "agent", "options", default={})
    if isinstance(options, dict) and options:
        fields["options"] = options
    spec = InputSpec(request_id=f"settings-{uuid.uuid4().hex[:16]}", profile=slug,
                     workspace=str(_get(document, "agent", "workspace_dir", default="") or Path.cwd()),
                     agent=agent, surface="discord", speech_from_profile=source.stem, profile_path=str(path), **fields)
    spec, refs, details = speech_seed(spec, source_path=source)
    refs["discord"] = setup.reference
    details["discord"] = setup.details
    state = state_dir or (Path.home() / ".local/share/sleeptalk/setup")
    body = {"guild_id": guild_id, "channel_id": channel_id, "operator_id": operator_id}
    text = await prepare_profile(spec, refs, details, store, body, path, state, agent_from_profile=True)
    profile_id = hashlib.sha256(str(path.resolve()).encode()).hexdigest()
    generations = {ref: store.status(ref).generation for ref in refs.values()}
    with store.bind_profile_generation(generations, slug + "@" + profile_id[:12]):
        atomic_write(path, text.encode())
    return {"profile": slug, "path": str(path)}


async def lock_discord_profile(path: Path, setup: DiscordSetup, *, guild_id: str, channel_id: str,
                               operator_id: str, store) -> dict[str, Any]:
    """Finish a Discord profile in place: one voice channel, one operator, closed."""
    import re

    from sleeptalk.discord_check import list_voice_channels
    from sleeptalk.profile_edit import set_string_key

    if not all(isinstance(v, str) and re.fullmatch(r"[0-9]{1,20}", v) for v in (guild_id, channel_id, operator_id)):
        raise ValueError("discord_choices")
    if not setup.reference:
        raise ValueError("token_missing")
    if guild_id not in {g.get("id") for g in setup.details.get("guilds", [])}:
        raise ValueError("bot_not_in_server")
    token = store.get(setup.reference)
    try:
        async with asyncio.timeout(20):
            channels = await list_voice_channels(token, guild_id)
    except Exception:  # noqa: BLE001
        raise ValueError("channel_unverified") from None
    finally:
        token = ""
    if not any(c.id == channel_id and c.eligible for c in channels):
        raise ValueError("channel_not_allowed")
    set_string_key(path, "transport", "guild_id", guild_id)
    set_string_key(path, "transport", "allowed_channel_ids", channel_id)
    set_string_key(path, "transport", "operator_ids", operator_id)
    set_string_key(path, "transport", "authorization_required", True)
    return {"profile": path.stem, "guild_id": guild_id, "channel_id": channel_id}


def installed_agents() -> list[str]:
    """Product agents whose program is on this computer's PATH (Hermes counts by either entry)."""
    from sleeptalk.setup_core import PRODUCT_AGENTS, detect_agents

    try:
        found = set(detect_agents())
    except Exception:  # noqa: BLE001 — a courtesy for ordering the list
        return []
    if found & {"acp", "hermes_gateway"}:
        found.add("hermes")
    return [key for key, _label in PRODUCT_AGENTS if key in found]


def _key_inventory(profiles_dir: Path) -> list[dict[str, Any]]:
    """Every key in the store with the profiles using it; the page shows references, never values."""
    from sleeptalk.credential_store import default_root
    from sleeptalk.key_inventory import inventory

    if not (default_root() / "store.sqlite3").is_file():
        return []
    try:
        return inventory(extra_dirs=[profiles_dir])
    except Exception:  # noqa: BLE001 — a locked store just shows no list
        return []


def _key_store() -> dict[str, str]:
    """What the Keys page says about where keys are kept, from the store itself."""
    from sleeptalk.credential_store import default_root, keychain_available, storage_kind

    root = default_root()
    home = str(Path.home())
    shown = "~" + str(root)[len(home):] if str(root).startswith(home + os.sep) else str(root)
    kind = storage_kind(root)
    return {"kind": kind, "path": shown, "can_move": kind == "file" and keychain_available(),
            "os_store": "keychain" if sys.platform == "darwin" else "secret_service"}


def key_lookup_allowed(state_dir: Path) -> bool:
    """Whether the user asked this page to look for keys other apps keep.

    Off until they click "Look on this computer": reading another tool's
    config is theirs to allow, not a side effect of opening settings.
    """
    try:
        return json.loads((state_dir / "settings-page.json").read_text(encoding="utf-8")).get("look_for_keys") is True
    except (OSError, ValueError, AttributeError):
        return False


def set_key_lookup(state_dir: Path, allowed: bool) -> None:
    from sleeptalk.private_input import atomic_write

    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    atomic_write(state_dir / "settings-page.json", (json.dumps({"look_for_keys": bool(allowed)}) + "\n").encode())


def found_keys(environ: dict[str, str] | None, *, allowed: bool) -> dict[str, Any]:
    """Keys other apps keep that could listen and speak: where they are, never values."""
    if not allowed:
        return {}
    try:
        from sleeptalk.key_discovery import discover

        return {key.provider: key.public() for key in discover(environment=environ)}
    except Exception:  # noqa: BLE001 — a courtesy
        return {}


def key_lookup_payload(allowed: bool) -> dict[str, Any]:
    from sleeptalk.key_discovery import PLACES

    return {"allowed": allowed, "places": list(PLACES)}


def _found_key_value(provider: str, environ: dict[str, str] | None, *, allowed: bool) -> str:
    """Read one found key at the moment the user chose it; refuses when lookup is off."""
    if not allowed:
        raise ValueError("key_not_found")
    from sleeptalk.key_discovery import discover, read_value

    found = {key.provider: key for key in discover(environment=environ)}
    if provider not in found:
        raise ValueError("key_not_found")
    return read_value(found[provider], environment=environ)


def _effective_speech(document: dict) -> dict[str, str]:
    """The listening model, speaking model and voice a call on this profile uses.

    The builder follows the language: a launch-default model that doesn't
    cover it (Cartesia ink-2 in French, Parakeet in Chinese) is swapped at
    call time. The page shows what the call will use, not the saved default.
    """
    from sleeptalk.builder import stt_spec, tts_spec
    from sleeptalk.config import STTConfig, TTSConfig

    try:
        stt = stt_spec(STTConfig(provider=str(_get(document, "stt", "provider", default="deepgram")),
                                 model=str(_get(document, "stt", "model", default="")),
                                 language=str(_get(document, "stt", "language", default="en-US"))))
        tts = tts_spec(TTSConfig(provider=str(_get(document, "tts", "provider", default="cartesia")),
                                 model=str(_get(document, "tts", "model", default="")),
                                 voice_id=str(_get(document, "tts", "voice_id", default="")),
                                 language=str(_get(document, "tts", "language", default="en-US"))))
    except Exception:  # noqa: BLE001 - a profile the builder can't read shows as saved
        return {}
    return {"stt_model": stt.model, "tts_model": tts.model, "voice": tts.voice_id}


def _voices_for_model(provider: str, model: str, language: str) -> dict[str, Any]:
    """The Voice page's list: the speaking model's own voices when it names them."""
    from sleeptalk import speech_catalog
    from sleeptalk.voice_catalog import catalog_payload

    payload = catalog_payload((provider,), language).get(provider, {"voices": [], "library_url": ""})
    option = speech_catalog.find(provider, speech_catalog.TTS, model, cached=provider == "local") if model else None
    if option is None or not option.voices:
        return payload
    listed = {voice["voice_id"]: voice for voice in payload["voices"]}
    voices = [listed.get(voice_id) or {"voice_id": voice_id, "name": voice_id.split("_")[0].title(),
                                       "description": f"{option.label} voice", "default": voice_id == option.default_voice}
              for voice_id in option.voices]
    return {**payload, "voices": voices}


def describe_profile(path: Path, *, environ: dict[str, str] | None = None, look_for_keys: bool = False) -> dict[str, Any]:
    """Everything the page shows for one profile. Never a secret: references only."""
    from sleeptalk.builder import stt_model_for_provider, tts_model_for_provider
    from sleeptalk.private_input import ENV_NAMES, KEY_PROVIDERS
    from sleeptalk.speech.openai_audio import PRESETS
    from sleeptalk.voice_catalog import catalog_payload, find
    from sleeptalk.setup_core import AGENT_LABELS, PRODUCT_AGENTS

    document = _read(path)
    agent_type = str(_get(document, "agent", "type", default="pi"))
    stt_provider = str(_get(document, "stt", "provider", default="deepgram"))
    tts_provider = str(_get(document, "tts", "provider", default="cartesia"))
    effective = {} if _is_realtime(document) else _effective_speech(document)
    tts_voice = effective.get("voice") or str(_get(document, "tts", "voice_id", default=""))
    curated = find(tts_provider, tts_voice)
    delivery = str(_get(document, "transport", "options", "delivery", default=""))
    transport = str(_get(document, "transport", "type", default="web"))
    facts_path = Path(__file__).parent / "_setup" / "providers.json"
    facts = {item["id"]: item for item in json.loads(facts_path.read_text(encoding="utf-8"))["providers"]}
    from sleeptalk import speech_catalog

    realtime = _is_realtime(document)
    language = str((realtime and _get(document, "route", "language", default=""))
                   or _get(document, "tts", "language", default="") or _get(document, "stt", "language", default="en-US"))
    refs_now = _get(document, "credentials", "refs", default={})
    services = []
    for name in ("cartesia", "deepgram", "elevenlabs", "openrouter", "venice", "groq", "together", "local"):
        card = dict(facts.get(name, {"id": name, "name": name.title(), "tagline": "", "free": "", "card": "", "fine_print": "", "key_url": ""}))
        if name in PRESETS:
            preset = PRESETS[name]
            card.update({"listen": preset.stt_model, "speak": f"{preset.tts_voice} on {preset.tts_model}", "note": "Listens one turn at a time."})
        else:
            card.update(_DIRECT_SPEECH.get(name, {}))
        card["in_use"] = not realtime and name == tts_provider and name == stt_provider
        card["takes_key"] = name in KEY_PROVIDERS
        card["models"] = {role: speech_catalog.payload(name, role, language) for role in (speech_catalog.STT, speech_catalog.TTS)}
        card["current"] = {
            "stt": (effective.get("stt_model") or stt_model_for_provider(name, str(_get(document, "stt", "model", default=""))))
            if card["in_use"] else card["models"]["stt"]["recommended"],
            "tts": _current_tts(name, effective.get("tts_model") or str(_get(document, "tts", "model", default="")), tts_voice)
            if card["in_use"] else card["models"]["tts"]["recommended"],
        }
        env_name = ENV_NAMES.get(name, "")
        card["key_saved"] = bool(card["in_use"] and isinstance(refs_now, dict) and env_name in refs_now)
        services.append(card)
    services.append(_realtime_card(document, refs_now, language))
    refs = refs_now
    if realtime:
        # The voice and speech pages follow the service in use.
        stt_provider = tts_provider = REALTIME
        tts_voice = services[-1]["voice"]
        curated = find(REALTIME, tts_voice)
    reusable = found_keys(environ, allowed=look_for_keys)
    return {
        "profile": path.stem,
        "path": str(path),
        "samples": voice_samples(language),
        "icons": brand_icons(),
        "sections": [{"id": key, "label": label} for key, label in SECTIONS],
        "agent": {
            "type": agent_type, "label": AGENT_LABELS.get(agent_type, agent_type),
            "name": str(_get(document, "agent", "name", default=path.stem)),
            "workspace": str(_get(document, "agent", "workspace_dir", default="")),
            "options": [{"id": key, "label": label} for key, label in PRODUCT_AGENTS],
            "found": installed_agents(),
        },
        "speech": {"stt_provider": stt_provider, "tts_provider": tts_provider,
                   "stt_model": services[-1]["current"]["stt"] if realtime else (effective.get("stt_model") or stt_model_for_provider(stt_provider, str(_get(document, "stt", "model", default="")))),
                   "tts_model": services[-1]["current"]["tts"] if realtime else (effective.get("tts_model") or tts_model_for_provider(tts_provider, str(_get(document, "tts", "model", default="")))),
                   "route": "hybrid" if realtime else "cascade",
                   "services": services, "voices_by_service": catalog_payload((*KEY_PROVIDERS, "local", REALTIME), language), "reusable": reusable,
                   "key_lookup": key_lookup_payload(look_for_keys)},
        "voice": {"voice_id": tts_voice, "name": curated.name if curated else ("Custom" if tts_voice else "Default"),
                  "speed": float(_get(document, "tts", "speed", default=1.0)), "speeds": list(SPEED_STEPS),
                  **(catalog_payload((tts_provider,), language).get(tts_provider, {"voices": [], "library_url": ""}) if realtime
                     else _voices_for_model(tts_provider, effective.get("tts_model", ""), language))},
        "language": {"value": language,
                     "options": list(_LANGUAGES)},
        "delivery": {"transport": transport, "value": delivery if delivery in ("tailnet", "local") else "relay"},
        "discord": {"enabled": transport == "discord",
                    # A missing field means closed (authorization_required defaults to true).
                    "open": _get(document, "transport", "authorization_required", default=True) is False,
                    "token_saved": isinstance(refs, dict) and str(_get(document, "transport", "token_env", default="") or "DISCORD_BOT_TOKEN") in refs,
                    "guild_id": str(_get(document, "transport", "guild_id", default="")),
                    "channel_ids": str(_get(document, "transport", "allowed_channel_ids", default="")),
                    "operator_ids": str(_get(document, "transport", "operator_ids", default="")),
                    "bot_name": str(_get(document, "agent", "name", default="")),
                    "speech_ready": (("OPENAI_API_KEY" in refs) if realtime
                                     else all(p == "local" or ENV_NAMES.get(p) in refs for p in {stt_provider, tts_provider})) if isinstance(refs, dict) else False,
                    "missing_extra": _missing_discord_extra(),
                    # Which service manager keeps a bot running, for the words beside Start.
                    "host_os": {"Darwin": "mac", "Linux": "linux"}.get(platform.system(), "other"),
                    "suggested_profile": _free_slug(path.parent, "discord")},
        "surface": {"type": transport, "capture_mode": str(_get(document, "transport", "capture_mode", default="") or ""),
                    "latency": bool(_get(document, "log", "latency", default=False))},
        "behaviour": {
            "reply_length": str(_get(document, "policy", "reply_length", default="full")),
            "narration_style": str(_get(document, "policy", "narration_style", default="silence")),
            "endpointing": str(_get(document, "policy", "endpointing", default="balanced")),
        },
        "keys": [{"env": env, "reference": str(ref)[:23] + "…" if len(str(ref)) > 24 else str(ref)}
                 for env, ref in (refs.items() if isinstance(refs, dict) else [])],
        "key_store": _key_store(),
        "key_inventory": _key_inventory(path.parent),
    }


# Keys the page may write, with the TOML section they live in and the values allowed.
_WRITABLE: dict[str, tuple[str, str, Any]] = {
    "tts.voice_id": ("tts", "voice_id", "voice"),
    "stt.model": ("stt", "model", "model"),
    "tts.model": ("tts", "model", "model"),
    "tts.speed": ("tts", "speed", tuple(SPEED_STEPS)),
    "language": ("*language", "language", _LANGUAGES),
    "transport.options.delivery": ("transport.options", "delivery", ("local", "tailnet", "relay")),
    "policy.reply_length": ("policy", "reply_length", ("full", "summarized", "concise")),
    "policy.narration_style": ("policy", "narration_style", ("narrated", "silence", "keyboard")),
    "policy.endpointing": ("policy", "endpointing", ("relaxed", "balanced", "aggressive")),
    # Where a (non-Discord) profile talks, and how the terminal hears you.
    "transport.type": ("transport", "type", ("web", "terminal")),
    "transport.capture_mode": ("transport", "capture_mode", ("headphones", "speakers")),
    "log.latency": ("log", "latency", (True, False)),
    # Discord bots: the display name and who may talk to it.
    "agent.name": ("agent", "name", "text"),
    "transport.operator_ids": ("transport", "operator_ids", "ids"),
    "transport.authorization_required": ("transport", "authorization_required", (True, False)),
}


def apply_setting(path: Path, setting: str, value: Any) -> None:
    """Write one allowed setting to the profile; raises ValueError for anything else."""
    from sleeptalk.profile_edit import set_string_key, set_tts_voice
    from sleeptalk.session import VOICE_ID_PATTERN

    if setting not in _WRITABLE:
        raise ValueError("That setting can't be changed here.")
    section, key, allowed = _WRITABLE[setting]
    if _is_realtime(_read(path)) and allowed in ("model", "voice"):
        # Realtime keeps its one model and its voice under [route].
        if allowed == "voice":
            if value not in _realtime_voices():
                raise ValueError("That voice isn't one of OpenAI Realtime's voices.")
            set_string_key(path, "route", "voice", value)
        else:
            from sleeptalk.setup_core import DEFAULT_REALTIME_MODEL
            if value not in {DEFAULT_REALTIME_MODEL, "gpt-live-1", _get(_read(path), "route", "model")} :
                raise ValueError("Choose an available OpenAI voice model.")
            set_string_key(path, "route", "model", value)
        return
    if allowed == "model":
        set_model(path, section, value)
        return
    if allowed == "voice":
        if not isinstance(value, str) or not VOICE_ID_PATTERN.fullmatch(value.strip()):
            raise ValueError("A voice ID is one word of plain characters.")
        set_tts_voice(path, value.strip())
        return
    document = _read(path)
    discord = str(_get(document, "transport", "type", default="web")) == "discord"
    if setting in ("transport.type", "transport.capture_mode", "transport.options.delivery") and discord:
        raise ValueError("A Discord bot profile talks in Discord; set up a browser or terminal profile instead.")
    if setting in ("agent.name", "transport.operator_ids", "transport.authorization_required") and not discord:
        raise ValueError("That setting is for Discord bot profiles.")
    if allowed == "text":
        if not isinstance(value, str) or not value.strip() or len(value.strip()) > 64 or any(ch in value for ch in '"\\\n\r'):
            raise ValueError("A name is 1 to 64 characters, without quotes or backslashes.")
        set_string_key(path, section, key, value.strip())
        return
    if allowed == "ids":
        ids = [part.strip() for part in str(value).split(",") if part.strip()] if isinstance(value, str) else []
        if not ids or not all(part.isdigit() and len(part) <= 20 for part in ids):
            raise ValueError("Discord user IDs are long numbers, separated by commas.")
        set_string_key(path, section, key, ",".join(ids))
        return
    if isinstance(allowed, tuple) and (value not in allowed or isinstance(value, bool) != isinstance(allowed[0], bool)):
        raise ValueError("That value isn't one of the choices.")

    if section == "*language":
        set_language(path, value)
        return
    if setting == "transport.options.delivery":
        set_string_key(path, "transport.options", "delivery", value if value in ("tailnet", "local") else "")
        return
    set_string_key(path, section, key, value)


def _profile_language(document: dict) -> str:
    return str(_get(document, "tts", "language", default="") or _get(document, "stt", "language", default="en-US"))


def _tts_option(provider: str, model: str, voice: str):
    """The catalog entry behind the profile's speaking model; for Deepgram the voice names it."""
    from sleeptalk.speech_choice import tts_option

    return tts_option(provider, model, voice)


def _current_tts(provider: str, model: str, voice: str) -> str:
    """The speaking model as the page lists it (Deepgram's voice names its family)."""
    from sleeptalk.builder import tts_model_for_provider

    stored = tts_model_for_provider(provider, model)
    option = _tts_option(provider, stored, voice)
    return option.id if option is not None else stored


def _speech_choice(document: dict):
    from sleeptalk.speech_choice import SpeechChoice

    return SpeechChoice(
        stt_provider=str(_get(document, "stt", "provider", default="")),
        stt_model=str(_get(document, "stt", "model", default="")),
        tts_provider=str(_get(document, "tts", "provider", default="")),
        tts_model=str(_get(document, "tts", "model", default="")),
        voice=str(_get(document, "tts", "voice_id", default="")),
        language=_profile_language(document),
    )


def _write_choice(path: Path, before, after) -> None:
    from sleeptalk.profile_edit import set_string_key, set_tts_voice

    if after.stt_model != before.stt_model:
        set_string_key(path, "stt", "model", after.stt_model)
    if after.tts_model != before.tts_model:
        set_string_key(path, "tts", "model", after.tts_model)
    if after.voice != before.voice:
        set_tts_voice(path, after.voice)


def set_language(path: Path, value: str) -> list[str]:
    """Save the language and move the speech choices that followed the old one.

    The rules live in speech_choice.follow_language, shared with the terminal.
    Returns what moved: "listening model", "speaking model", "voice".
    """
    from sleeptalk.profile_edit import set_string_key
    from sleeptalk.speech_choice import follow_language

    document = _read(path)
    if _is_realtime(document):
        # Realtime hears and speaks any of them; only the transcription hint moves.
        set_string_key(path, "route", "language", value)
        set_string_key(path, "tts", "language", value)
        return []
    before = _speech_choice(document)
    after, changed = follow_language(before, value)
    set_string_key(path, "stt", "language", value)
    set_string_key(path, "tts", "language", value)
    _write_choice(path, before, after)
    return changed


def _usable_model(provider: str, role: str, model: Any):
    from sleeptalk.speech_choice import usable_model

    return usable_model(provider, role, model)


def set_model(path: Path, role: str, model: Any) -> None:
    """Point the profile's current provider at another model of the same role.

    The rules live in speech_choice.with_model, shared with the terminal.
    """
    from sleeptalk.speech_choice import with_model

    before = _speech_choice(_read(path))
    _write_choice(path, before, with_model(before, role, model))


async def preview_voice(path: Path, *, model: str, voice: str, text: str) -> bytes:
    """One spoken line in a voice of the profile's own speaking service, as WAV.

    Uses the key the profile already references; the page never sends one.
    """
    from sleeptalk.builder import tts_spec
    from sleeptalk.config import _load_env, load_resolved_config, resolve_credential_env
    from sleeptalk.provider_factory import build_tts
    from sleeptalk.session import VOICE_ID_PATTERN

    if not isinstance(voice, str) or not VOICE_ID_PATTERN.fullmatch(voice.strip()):
        raise ValueError("voice_unavailable")
    config = load_resolved_config(path, check_env=False, check_binaries=False, verify_binding=False)
    if config.route.type == "hybrid":
        if model == "gpt-live-1" or config.route.model == "gpt-live-1":
            raise ValueError("Try GPT-Live voices in a browser call; standalone voice previews are unavailable.")
        return await _preview_realtime(path, config, voice.strip(), text)
    if model:
        _usable_model(config.tts.provider, "tts", model)
    env = resolve_credential_env(config, _load_env(path))
    section = config.tts.model_copy(update={"voice_id": voice.strip(), **({"model": model} if model else {})})
    adapter = build_tts(tts_spec(section), env)
    try:
        async with asyncio.timeout(30):
            return await adapter.synthesize_wav_bytes(text)
    finally:
        env.clear()
        close = getattr(adapter, "aclose", None) or getattr(adapter, "close", None)
        if close is not None:
            result = close()
            if asyncio.iscoroutine(result):
                await result


async def _preview_realtime(path: Path, config: Any, voice: str, text: str) -> bytes:
    """A Realtime voice speaking one line, through OpenAI Speech and the profile's key."""
    from dataclasses import replace

    from sleeptalk.builder import openai_realtime_config
    from sleeptalk.config import _load_env, resolve_credential_env
    from sleeptalk.media_audio import pcm_to_wav
    from sleeptalk.openai_realtime import OpenAISpeech

    if voice not in _realtime_voices():
        raise ValueError("voice_unavailable")
    env = resolve_credential_env(config, _load_env(path))
    try:
        speech = OpenAISpeech(replace(openai_realtime_config(config, env), voice=voice))
    finally:
        env.clear()
    pcm = bytearray()
    async with asyncio.timeout(30):
        async for chunk in speech.synthesize(text):
            pcm += chunk
    return pcm_to_wav(bytes(pcm))


async def prove_speech_key(provider: str, value: str, voice: str, *, language: str, slug: str,
                           workspace: str, agent: str, profile_path: str = "", stt_model: str = "",
                           tts_model: str = "") -> tuple[dict[str, Any], str]:
    """The key pages' live check: transcribe the bundled clip, speak one line.

    Chosen models are checked as chosen; empty ones are the provider's defaults.

    Returns the provider's details and the request ID the check ran under.
    Raises ``ValueError`` with a fixed code; no provider text or key escapes.
    """
    import uuid

    from sleeptalk.private_input import InputSpec, _validation_code, validate_provider

    spec = InputSpec(request_id=f"settings-{uuid.uuid4().hex[:16]}", profile=slug, workspace=workspace, agent=agent,
                     surface="web", stt_provider=provider, tts_provider=provider, language=language,
                     profile_path=profile_path)
    try:
        async with asyncio.timeout(60):
            details = await validate_provider(provider, value.strip(), spec, voice,
                                              stt_model=stt_model, tts_model=tts_model)
    except Exception as exc:  # noqa: BLE001 — a code, never a provider body
        raise ValueError(_validation_code(exc)) from None
    return details, spec.request_id


async def switch_speech_service(path: Path, provider: str, value: str, voice: str, *, store=None,
                                stt_model: str = "", tts_model: str = "") -> dict[str, Any]:
    """Prove a key for one service, store it, and point the profile at that service.

    The same validation the key pages run: a transcription of the bundled
    clip and one synthesized line in the chosen voice. Raises ``ValueError``
    with a fixed code the page explains; the key never reaches the profile.
    """
    from sleeptalk.credential_store import CredentialStore
    from sleeptalk.private_input import ENV_NAMES, KEY_PROVIDERS
    from sleeptalk.profile_edit import set_string_key, set_tts_voice
    from sleeptalk.session import VOICE_ID_PATTERN

    if provider not in KEY_PROVIDERS:
        raise ValueError("unsupported")
    for role, chosen in (("stt", stt_model), ("tts", tts_model)):
        if chosen:
            try:
                _usable_model(provider, role, chosen)
            except ValueError:
                raise ValueError("unsupported") from None
    if not isinstance(value, str) or not value.strip() or len(value) > 8192:
        raise ValueError("invalid_credential")
    if voice and not VOICE_ID_PATTERN.fullmatch(voice):
        raise ValueError("voice_unavailable")
    document = _read(path)
    language = str(_get(document, "tts", "language", default="") or _get(document, "stt", "language", default="en-US"))
    details, request_id = await prove_speech_key(
        provider, value, voice, language=language, slug=path.stem,
        workspace=str(_get(document, "agent", "workspace_dir", default="") or Path.cwd()),
        agent=str(_get(document, "agent", "type", default="pi")), profile_path=str(path),
        stt_model=stt_model, tts_model=tts_model)
    store = store or CredentialStore()
    from sleeptalk.key_inventory import forget_dropped, references_in

    before = references_in(path)
    reference = store.put_validated(provider, value.strip(), request_id=request_id + ":" + provider)
    value = ""
    from sleeptalk.builder import stt_model_for_provider, tts_model_for_provider

    if _is_realtime(document):
        # Leaving Realtime: the route goes back to listening and speaking with this service.
        from sleeptalk.profile_edit import set_table

        set_table(path, "route", {"type": "cascade"})
        set_string_key(path, "stt", "language", language)
    set_string_key(path, "stt", "provider", provider)
    set_string_key(path, "stt", "model", str(details.get("stt_model") or stt_model_for_provider(provider)))
    set_string_key(path, "tts", "provider", provider)
    set_string_key(path, "tts", "model", tts_model_for_provider(provider))
    set_tts_voice(path, str(details.get("voice_id") or voice))
    set_string_key(path, "credentials.refs", ENV_NAMES[provider], reference)
    profile_id = hashlib.sha256(str(path.resolve()).encode()).hexdigest()
    store.bind_profile(reference, path.stem + "@" + profile_id[:12])
    if stt_model:
        set_model(path, "stt", stt_model)
    if tts_model:
        set_model(path, "tts", tts_model)
        if voice:
            set_tts_voice(path, voice)
    # The key this one replaced goes, unless another profile still uses it.
    forget_dropped(before, path, store=store)
    return {"provider": provider, "voice_id": str(_get(_read(path), "tts", "voice_id", default=voice))}


def _local_models(stt_model: str, tts_model: str, voice: str):
    """The local models a choice needs, or ValueError("unsupported") for one the page can't offer."""
    from sleeptalk.speech import local_models

    try:
        stt = local_models.get_model(stt_model or local_models.DEFAULT_STT_MODEL, "stt")
        tts = local_models.resolve_tts_model(tts_model, voice)
    except (KeyError, ValueError, local_models.LocalModelError):
        raise ValueError("unsupported") from None
    if voice and voice not in tts.voices:
        from sleeptalk.speech.local_voices import is_custom

        if not is_custom(voice):
            raise ValueError("voice_unavailable")
        try:
            local_models.voice_index(tts, voice)
        except local_models.LocalModelError:
            raise ValueError("voice_unavailable") from None
    return stt, tts


def _local_default_voice(tts) -> str:
    """The voice a local model starts on: its catalog default (Kokoro's Bella), else its first."""
    from sleeptalk import speech_catalog

    option = speech_catalog.find("local", speech_catalog.TTS, tts.id, cached=False)
    return (option.default_voice if option else "") or tts.voices[0]


def local_speech_status(stt_model: str = "", tts_model: str = "", voice: str = "") -> dict[str, Any]:
    """What switching to local speech still needs here: the runtime, and which models to download."""
    from sleeptalk import sysdeps
    from sleeptalk.speech import local_models

    stt, tts = _local_models(stt_model, tts_model, voice)
    streaming = stt.runtime == "moonshine"
    missing = sysdeps.missing_local_extra() or (sysdeps.missing_streaming_extra() if streaming else None)
    command = sysdeps.streaming_extra_install_command() if streaming else sysdeps.local_extra_install_command()
    return {"runtime": missing is None,
            "install_command": " ".join(command),
            "models": [{"id": m.id, "label": m.label, "size_mb": m.size_mb, "license": m.license,
                        "ready": local_models.is_ready(m), "download": local_models.download_info(m)} for m in (stt, tts)]}


class LocalSpeechJob:
    """Installing the local runtime, or switching a profile to local speech.

    The terminal's local setup, run for the page: the models are 130 to 450 MB,
    so the page starts the job and polls it rather than holding one request open.
    Progress fields are plain assignments from the download thread.
    """

    def __init__(self, kind: str) -> None:
        self.kind = kind  # "install" | "switch" | "create"
        self.stage = "install" if kind == "install" else "download"
        self.label = ""
        self.done = 0
        self.total = 0
        self.code = ""
        # The profile a "create" job made.
        self.profile = ""
        self.task: asyncio.Task | None = None

    @property
    def running(self) -> bool:
        return self.stage not in ("done", "failed")

    def payload(self) -> dict[str, Any]:
        percent = min(100, self.done * 100 // self.total) if self.total else 0
        return {"kind": self.kind, "stage": self.stage, "label": self.label, "percent": percent, "code": self.code,
                **({"profile": self.profile} if self.profile else {}),
                **({"message": _SPEECH_REFUSALS[self.code]} if self.code in _SPEECH_REFUSALS else {})}

    async def install(self, *, streaming: bool = False) -> None:
        from sleeptalk import sysdeps

        try:
            installer = sysdeps.install_streaming_extra if streaming else sysdeps.install_local_extra
            if await asyncio.to_thread(installer) is not None:
                self.stage, self.code = "failed", "local_install_failed"
                return
            self.stage = "done"
        except Exception:  # noqa: BLE001 — the page shows a code; the log has nothing secret either way
            logger.warning("local speech runtime install failed", exc_info=True)
            self.stage, self.code = "failed", "local_install_failed"

    async def create(self, make, on_created) -> None:
        """A new profile on local speech: ``make()`` downloads, proves, and writes it."""
        try:
            self.profile = await make(self)
            on_created(self.profile)
            self.stage = "done"
        except ValueError as exc:
            self.stage, self.code = "failed", str(exc)
        except Exception:  # noqa: BLE001
            logger.warning("creating a local speech profile failed", exc_info=True)
            self.stage, self.code = "failed", "local_check_failed"

    async def switch(self, path: Path, stt_model: str, tts_model: str, voice: str, lock: asyncio.Lock) -> None:
        try:
            await switch_to_local(path, stt_model, tts_model, voice, job=self, lock=lock)
            self.stage = "done"
        except ValueError as exc:
            self.stage, self.code = "failed", str(exc)
        except Exception:  # noqa: BLE001
            logger.warning("switching to local speech failed", exc_info=True)
            self.stage, self.code = "failed", "local_check_failed"


async def switch_to_local(path: Path, stt_model: str, tts_model: str, voice: str, *,
                          job: LocalSpeechJob | None = None, lock: asyncio.Lock | None = None) -> dict[str, Any]:
    """Point the profile at local speech: download what's missing, prove one turn, then save.

    The same steps as the terminal's local setup. Nothing is written until a
    test clip was transcribed and the voice spoke a line on this computer.
    """
    from sleeptalk.profile_edit import set_string_key, set_table, set_tts_voice

    job = job or LocalSpeechJob("switch")
    stt, tts = _local_models(stt_model, tts_model, voice)
    voice = voice or _local_default_voice(tts)
    language = _profile_language(_read(path))
    await prove_local_speech(stt, tts, voice, language, job)
    async with (lock or asyncio.Lock()):
        if _is_realtime(_read(path)):
            # Leaving Realtime: the route goes back to listening and speaking here.
            set_table(path, "route", {"type": "cascade"})
            set_string_key(path, "stt", "language", language)
        set_string_key(path, "stt", "provider", "local")
        set_string_key(path, "stt", "model", stt.id)
        set_string_key(path, "tts", "provider", "local")
        set_string_key(path, "tts", "model", tts.id)
        set_tts_voice(path, voice)
    return {"provider": "local", "voice_id": voice}


async def prove_local_speech(stt, tts, voice: str, language: str, job: "LocalSpeechJob") -> None:
    """Download what local speech is missing and prove one turn on this computer.

    Raises ValueError with a code the page explains; nothing is written.
    """
    from sleeptalk import sysdeps
    from sleeptalk.builder import stt_spec, tts_spec
    from sleeptalk.config import STTConfig, TTSConfig
    from sleeptalk.defaults import register_defaults
    from sleeptalk.language_support import primary_tag
    from sleeptalk.registries import TTS_PROVIDERS
    from sleeptalk.speech import local_models
    from sleeptalk.stt_roundtrip import stt_roundtrip
    from sleeptalk.setup_core import sample_line

    if primary_tag(language) != "en":
        raise ValueError("local_english_only")
    if sysdeps.missing_local_extra() is not None or (stt.runtime == "moonshine" and sysdeps.missing_streaming_extra()):
        raise ValueError("local_runtime_missing")
    for model in (stt, tts):
        if local_models.is_ready(model):
            continue
        job.stage, job.label, job.done, job.total = "download", model.label, 0, model.size_mb * 1_000_000

        def progress(done: int, total: int) -> None:
            job.done, job.total = done, total

        try:
            await asyncio.to_thread(local_models.ensure_model, model, progress=progress)
        except local_models.LocalModelError:
            logger.warning("downloading %s failed", model.label, exc_info=True)
            raise ValueError("local_download_failed") from None
    job.stage, job.label = "checking", ""
    try:
        await stt_roundtrip("local", stt_spec(STTConfig(provider="local", model=stt.id, language=language)), "")
        register_defaults()
        speaker = TTS_PROVIDERS.build("local", tts_spec(TTSConfig(provider="local", model=tts.id, voice_id=voice,
                                                                  language=language)), {})
        try:
            (await speaker.synthesize_wav(sample_line(language))).unlink(missing_ok=True)
        finally:
            close = getattr(speaker, "close", None)
            if callable(close):
                await close()
    except Exception:  # noqa: BLE001 — a code for the page; the log keeps the reason
        logger.warning("local speech test turn failed", exc_info=True)
        raise ValueError("local_check_failed") from None


# What each Realtime check step says when it fails, and OpenAI's own error
# names, mapped to codes the page explains. The page never sees the text.
_OPENAI_ERROR_NAMES = (("invalid_api_key", "invalid_credential"), ("incorrect api key", "invalid_credential"),
                       ("insufficient_quota", "quota_or_rate_limit"), ("rate_limit", "quota_or_rate_limit"),
                       ("model_not_found", "permission_denied"), ("does not have access", "permission_denied"))
_REALTIME_STEPS = (("did not transcribe the connectivity probe", "realtime_no_transcript"),
                   ("realtime confirmation", "realtime_settings_rejected"),
                   ("openai speech", "speech_failed"),
                   ("unable to connect to openai realtime", "realtime_unreachable"))


# The same words the page has for each code (settings.js KEY_FAILURES),
# sent along so a page built before a code existed still says something true.
_SPEECH_REFUSALS = {
    "invalid_credential": "The service rejected this key. Copy a current key from its dashboard.",
    "permission_denied": "This key lacks the permissions voice needs; create one with full access.",
    "voice_unavailable": "That voice isn't available on this account.",
    "quota_or_rate_limit": "This account is out of credit or rate limited.",
    "network_timeout": "The check took too long. Try again.",
    "storage_unavailable": "The credential store is locked; unlock it and retry.",
    "key_not_found": "That key isn't on this machine any more.",
    "unsupported": "That service or model can't be set up here.",
    "validation_failed": "The service didn't pass the check. Check the key and its account, then try again.",
    "realtime_no_transcript": "OpenAI Realtime connected but didn't transcribe the test clip. "
                              "Check that this key's project can use gpt-realtime and gpt-4o-mini-transcribe.",
    "realtime_settings_rejected": "OpenAI Realtime didn't accept the model, voice, or transcription settings for this key.",
    "realtime_unreachable": "The computer running your agent couldn't reach OpenAI Realtime. Check its internet connection.",
    "speech_failed": "OpenAI Speech didn't speak the test line. Check that this key's project can use gpt-4o-mini-tts.",
    "local_english_only": "Local speech listens and speaks English only. Choose English under Language first, or pick another service.",
    "local_runtime_missing": "The local speech runtime isn't installed on this computer yet. Install it first.",
    "local_install_failed": "Installing the local speech runtime failed. Run the command shown in a terminal on this computer, then check again.",
    "local_download_failed": "A model couldn't be downloaded. Check this computer's internet connection, then try again.",
    "local_check_failed": "Local speech couldn't run a test turn on this computer. Nothing was changed; the log has the reason.",
    "local_busy": "Local speech is already being set up. Wait for it to finish.",
}


def _refused_with_reason(message: str):
    """A refusal the page shows as written, or its generic "couldn't be written" line without one."""
    if message.strip():
        return web.json_response({"status": "refused", "code": "detail", "message": message.strip()}, status=422)
    return web.json_response({"status": "refused", "code": "profile_failed"}, status=422)


def _refused(code: str):
    return web.json_response({"status": "refused", "code": code,
                              **({"message": _SPEECH_REFUSALS[code]} if code in _SPEECH_REFUSALS else {})}, status=422)


def _realtime_failure_code(exc: BaseException) -> str:
    """Which part of the Realtime check failed, as a fixed code.

    A rejected key shows up as the handshake's status on the error's cause,
    or as OpenAI's error name in a Realtime error event; a failure with
    neither names the step that failed.
    """
    from sleeptalk.private_input import _validation_code

    chain: list[BaseException] = []
    current: BaseException | None = exc
    while current is not None and current not in chain:
        chain.append(current)
        current = current.__cause__ or current.__context__
    statuses = {401: "invalid_credential", 403: "permission_denied", 429: "quota_or_rate_limit"}
    for item in chain:
        status = getattr(item, "status", None)
        if isinstance(status, int) and status in statuses:
            return statuses[status]
    text = " ".join(str(item) for item in chain).lower()
    for needle, code in _OPENAI_ERROR_NAMES:
        if needle in text:
            return code
    code = _validation_code(exc if isinstance(exc, Exception) else Exception(str(exc)))
    if code != "validation_failed":
        return code
    for needle, step in _REALTIME_STEPS:
        if needle in text:
            return step
    if any(isinstance(item, TimeoutError) for item in chain):
        return "network_timeout"
    return code


async def switch_to_realtime(path: Path, value: str, voice: str, *, store=None, model: str = "") -> dict[str, Any]:
    """Prove an OpenAI key against Realtime, store it, and make the profile use Realtime.

    The same check `sleeptalk doctor` runs: Realtime transcribes the bundled
    clip and OpenAI Speech says one word. The [stt]/[tts] tables stay, so
    the speed and a later switch back keep what they had.
    """
    from sleeptalk.credential_store import CredentialStore
    from sleeptalk.key_inventory import forget_dropped, references_in
    from sleeptalk.openai_realtime import OpenAIRealtimeConfig, check_openai_connectivity
    from sleeptalk.private_input import _validation_code
    from sleeptalk.profile_edit import set_string_key, set_table
    from sleeptalk.setup_core import (
        DEFAULT_REALTIME_MODEL,
        DEFAULT_REALTIME_VOICE,
        DEFAULT_SPEECH_MODEL,
        DEFAULT_TRANSCRIPTION_MODEL,
    )

    if not isinstance(value, str) or not value.strip() or len(value) > 8192:
        raise ValueError("invalid_credential")
    voice = voice or DEFAULT_REALTIME_VOICE
    if voice not in _realtime_voices():
        raise ValueError("voice_unavailable")
    document = _read(path)
    language = _profile_language(document)
    default_model = DEFAULT_REALTIME_MODEL if _is_realtime(document) else _default_realtime_model(
        str(_get(document, "transport", "type", default="web")))
    route = {"type": "hybrid", "provider": "openai-realtime",
             "model": model or str(_get(document, "route", "model", default="") or default_model), "voice": voice,
             "speech_model": str(_get(document, "route", "speech_model", default="") or DEFAULT_SPEECH_MODEL),
             "transcription_model": str(_get(document, "route", "transcription_model", default="") or DEFAULT_TRANSCRIPTION_MODEL),
             "language": language}
    if model and model not in {DEFAULT_REALTIME_MODEL, "gpt-live-1"}:
        raise ValueError("unsupported")
    check = check_openai_connectivity
    if route["model"] == "gpt-live-1":
        from sleeptalk.openai_live import check_openai_live_connectivity
        check = check_openai_live_connectivity
    try:
        async with asyncio.timeout(60):
            await check(OpenAIRealtimeConfig(
                api_key=value.strip(), model=route["model"], voice=voice, speech_model=route["speech_model"],
                transcription_model=route["transcription_model"], language=language))
    except Exception as exc:  # noqa: BLE001 — a code, never a provider body
        raise ValueError(_realtime_failure_code(exc)) from None
    store = store or CredentialStore()
    before = references_in(path)
    import uuid

    reference = store.put_validated(REALTIME, value.strip(), request_id=f"settings-{uuid.uuid4().hex[:16]}:{REALTIME}")
    value = ""
    set_table(path, "route", route)
    set_string_key(path, "credentials.refs", "OPENAI_API_KEY", reference)
    profile_id = hashlib.sha256(str(path.resolve()).encode()).hexdigest()
    store.bind_profile(reference, path.stem + "@" + profile_id[:12])
    forget_dropped(before, path, store=store)
    return {"provider": REALTIME, "voice_id": voice}


def agent_readiness(name: str) -> dict[str, Any]:
    """Whether an agent's program and packages are here, and how to add what isn't."""
    from sleeptalk.agents.readiness import probe
    from sleeptalk.defaults import register_defaults

    register_defaults()
    try:
        report = probe(name)
    except Exception:  # noqa: BLE001 — an unknown or broken registration reads as not ready
        return {"ready": False, "missing": [{"hint": "This agent can't be checked here.", "command": "", "installable": False}]}
    return {"ready": report.ready,
            "missing": [{"hint": need.hint, "command": need.command_text, "installable": bool(need.install_cmd)}
                        for need in report.missing]}


def install_agent_pieces(name: str) -> dict[str, Any]:
    """Run the installs the terminal wizard offers for this agent (never sudo), then check again."""
    from sleeptalk.agents.readiness import install_missing, probe
    from sleeptalk.defaults import register_defaults

    register_defaults()
    lines = install_missing(probe(name))
    return {"lines": lines, **agent_readiness(name)}


def reusable_model_keys(profile_path: Path | None, environ: dict[str, str] | None, *, allowed: bool) -> dict[str, dict[str, str]]:
    """Keys already on this computer that "Just a model" could use, by vendor: where, never values.

    This profile's own speech key comes first (the same service listens,
    speaks, and answers), then keys other apps keep, once the user has let
    the page look for them.
    """
    from sleeptalk import model_only_setup as setup
    from sleeptalk.private_input import ENV_NAMES

    vendors = {vendor for vendor, _label in setup.VENDORS}
    found: dict[str, dict[str, str]] = {}
    if profile_path is not None and profile_path.is_file():
        document = _read(profile_path)
        provider = str(_get(document, "tts", "provider", default=""))
        refs = _get(document, "credentials", "refs", default={})
        if (provider in vendors and provider == str(_get(document, "stt", "provider", default=""))
                and isinstance(refs, dict) and ENV_NAMES.get(provider, "") in refs):
            found[provider] = {"source": "speech", "label": "this profile's speech key"}
    for provider, key in found_keys(environ, allowed=allowed).items():
        if provider in vendors and provider not in found:
            found[provider] = {"source": "found", "label": str(key.get("source", ""))}
    return found


def agent_setup_options(profile_path: Path | None, *, environ: dict[str, str] | None = None,
                        look_for_keys: bool = False) -> dict[str, Any]:
    """Everything the agent questions need: readiness, model vendors, what's saved now."""
    from sleeptalk import model_only_setup as setup
    from sleeptalk.setup_core import OPENCLAW_CONFIG, PRODUCT_AGENTS

    current: dict[str, Any] = {}
    if profile_path is not None and profile_path.is_file():
        document = _read(profile_path)
        agent = document.get("agent") or {}
        auth = agent.get("auth") or {}
        options = agent.get("options") or {}
        current = {"type": str(agent.get("type", "")), "workspace": str(agent.get("workspace_dir", "")),
                   "gateway_url": str(options.get("gateway_url", "") or ""),
                   "gateway_session_id": str(options.get("gateway_session_id", "") or options.get("session_id", "") or ""),
                   "model": str(auth.get("model", "") or agent.get("model", "") or ""),
                   "vendor": str(auth.get("vendor", "") or ""), "has_model_key": bool(auth.get("credential_ref")),
                   "own": {"command": " ".join(str(part) for part in options.get("command", [])) if isinstance(options.get("command"), list) else "",
                           "protocol": str(options.get("protocol", "") or ""), "url": str(options.get("url", "") or ""),
                           "token_env": str(options.get("token_env", "") or ""),
                           "env": ", ".join(str(n) for n in options.get("env", [])) if isinstance(options.get("env"), list) else "",
                           "permission": str(agent.get("permission_mode", "") or "")}}
    return {
        "agents": [{"id": key, "label": label, **agent_readiness(key)} for key, label in PRODUCT_AGENTS]
        + [{"id": OWN_AGENT, "label": "My own agent", "ready": True, "missing": []}],
        "found": installed_agents(),
        "vendors": [{"id": vendor, "label": label, "default_model": setup.default_model(vendor),
                     "console_url": setup.console_url(vendor)} for vendor, label in setup.VENDORS],
        "openclaw_config_found": OPENCLAW_CONFIG.expanduser().is_file(),
        "reusable_model_keys": reusable_model_keys(profile_path, environ, allowed=look_for_keys),
        "current": current,
    }


async def hermes_dashboard() -> dict[str, Any]:
    """A running Hermes dashboard on this machine, if any, and whether it needs a token."""
    from sleeptalk import hermes_connect
    from sleeptalk.hermes_connect import AuthRequired, Ready, TokenDead
    from sleeptalk.setup_core import SetupAnswers

    for origin in hermes_connect.running_dashboard_origins():
        found = await hermes_connect.attach_hermes(SetupAnswers(), raw_url=origin)
        if isinstance(found, Ready):
            return {"status": "ready", "origin": origin}
        if isinstance(found, (AuthRequired, TokenDead)):
            return {"status": "token", "origin": origin}
    return {"status": "none"}


async def ollama_model_names(base_url: str = "") -> list[str]:
    from sleeptalk import model_only_setup as setup

    try:
        choice = setup.auth_choice(setup.OLLAMA_VENDOR, base_url)
        return await setup.ollama_models(choice.base_url)
    except Exception:  # noqa: BLE001 — none listed
        return []


def _speech_key_for(answers: Any, vendor: str) -> str:
    """The speech key these answers already hold for this service, if any."""
    for provider, key in ((answers.speech_provider, answers.speech_key), (answers.stt_provider, answers.stt_key),
                          (answers.tts_provider, answers.tts_key)):
        if provider == vendor and key:
            return key
    return ""


async def resolve_agent_choice(answers: Any, choice: dict[str, Any], *, store: Any = None, home: Path | None = None,
                               environ: dict[str, str] | None = None, look_for_keys: bool = False) -> dict[str, Any]:
    """Apply an agent choice to wizard answers, asking what the terminal asks.

    Hermes joins a running dashboard or starts per call; OpenClaw takes its
    gateway token (read, pasted, or none for ``openclaw acp``) and which
    conversation to continue; Just a model verifies and stores its key. Raises
    ``ValueError`` with a fixed code, or ``detail:`` plus a message to show.
    """
    from sleeptalk.setup_core import (
        OPENCLAW_MAIN_SESSION,
        PRODUCT_AGENTS,
        read_openclaw_gateway_token,
    )

    agent = str(choice.get("agent", ""))
    if agent not in {key for key, _label in PRODUCT_AGENTS} | {OWN_AGENT}:
        raise ValueError("unsupported")
    answers.agent_options = {}
    answers.agent_permission_mode = ""
    answers.agent_token = ""
    answers.gateway_url = ""
    answers.gateway_session_token = ""
    answers.gateway_session_id = ""
    answers.openclaw_token = ""
    outcome: dict[str, Any] = {}
    if agent == "hermes":
        hermes = choice.get("hermes") if isinstance(choice.get("hermes"), dict) else {}
        if hermes.get("mode") == "join":
            from sleeptalk import hermes_connect
            from sleeptalk.hermes_connect import DEFAULT_ORIGIN, Ready, probe_message

            found = await hermes_connect.attach_hermes(answers, raw_url=str(hermes.get("url", "") or DEFAULT_ORIGIN),
                                                        token=str(hermes.get("token", "") or ""))
            if not isinstance(found, Ready):
                raise ValueError("detail:" + probe_message(found))
            answers.agent_type = "hermes_gateway"
        else:
            # Start Hermes' gateway per call from the Hermes install (falls back to ACP).
            answers.agent_type = "hermes_gateway"
        return outcome
    if agent == OWN_AGENT:
        return await _own_agent(answers, choice.get("own") if isinstance(choice.get("own"), dict) else {}, home=home)
    if agent == "openclaw":
        claw = choice.get("openclaw") if isinstance(choice.get("openclaw"), dict) else {}
        source = str(claw.get("token_source", "skip"))
        token = ""
        if source == "read":
            token = read_openclaw_gateway_token()
            if not token:
                raise ValueError("openclaw_token_missing")
        elif source == "paste":
            token = str(claw.get("token", "") or "").strip()
            if not token or len(token) > 4096:
                raise ValueError("openclaw_token_missing")
        answers.openclaw_token = token
        answers.gateway_session_id = OPENCLAW_MAIN_SESSION if token and claw.get("memory") != "fresh" else ""
        answers.agent_type = "openclaw"
        return outcome
    if agent == "model":
        from sleeptalk import model_only_setup as setup

        chosen = choice.get("model") if isinstance(choice.get("model"), dict) else {}
        vendor = setup.normalize_vendor(str(chosen.get("vendor", "") or setup.DEFAULT_VENDOR))
        base_url = str(chosen.get("base_url", "") or "") if vendor == setup.OTHER_VENDOR else ""
        try:
            auth = setup.auth_choice(vendor, base_url)
        except setup.ModelSetupError as exc:
            raise ValueError("detail:" + str(exc)) from None
        model = str(chosen.get("model", "") or "").strip() or setup.default_model(vendor)
        if len(model) > 200:
            raise ValueError("unsupported")
        key = str(chosen.get("key", "") or "")
        if not key and chosen.get("reuse") is True:
            # A key already on this computer: the profile's speech key for the
            # same service, else one another app keeps. Read here, never sent.
            key = _speech_key_for(answers, vendor) or _found_key_value(vendor, environ, allowed=look_for_keys)
        if store is None:
            from sleeptalk.credential_store import CredentialStore

            store = CredentialStore()
        optional = setup.key_optional(auth)
        same_endpoint = (setup.normalize_vendor(answers.model_vendor or setup.DEFAULT_VENDOR) == vendor
                         and (vendor != setup.OTHER_VENDOR or answers.model_base_url == auth.base_url))
        keep = bool(chosen.get("keep")) and same_endpoint and setup.saved_key_usable(store, answers.model_credential_ref, auth)
        try:
            if vendor == setup.OLLAMA_VENDOR:
                login = await setup.verify_key(auth, model, "")
                reference = setup.store_key(store, auth, "")
            elif not key and keep:
                reference = answers.model_credential_ref
                login = await setup.verify_key(auth, model, store.get(reference))
            elif not key and not optional:
                raise ValueError("model_key_required")
            else:
                login = await setup.verify_key(auth, model, key)
                if login == "unavailable" and not optional:
                    raise ValueError("detail:" + setup.rejected_message(vendor))
                reference = setup.store_key(store, auth, key)
        finally:
            key = ""
        answers.model_vendor = vendor
        answers.model_base_url = auth.base_url if vendor == setup.OTHER_VENDOR else ""
        answers.model = model
        answers.model_key = ""
        answers.model_credential_ref = reference
        answers.agent_type = "model"
        if login == "available":
            outcome["note"] = f"{setup.vendor_name(vendor)}: {model} answers here."
        elif vendor == setup.OLLAMA_VENDOR:
            outcome["note"] = setup.missing_ollama_model_message(model)
        else:
            outcome["note"] = setup.unconfirmed_message(model)
        return outcome
    answers.agent_type = agent
    return outcome


# "My own agent": a harness Sleeptalk doesn't ship, as the terminal's kinds.
OWN_AGENT = "own"
OWN_KINDS = ("command", "acp", "http", "bridge", "starter")


async def _own_agent(answers: Any, own: dict[str, Any], *, home: Path | None) -> dict[str, Any]:
    """Configure a bring-your-own agent and check it without sending a prompt.

    The same steps as the terminal's "My own agent": a starter scaffold when
    asked, the command/URL settings validated by the adapter's own schema,
    the known-ACP suggestion, and the offline contract check. A failing check
    refuses unless ``keep`` says to save anyway.
    """
    from sleeptalk.agents.bridge_lite import KNOWN_ACP_COMMANDS, command_setup_hint
    from sleeptalk.config import AgentConfig
    from sleeptalk.conformance.runner import run_adapter_test
    from sleeptalk.defaults import register_defaults
    from sleeptalk.registries import AGENTS
    from sleeptalk.setup_core import own_agent_settings

    kind = str(own.get("kind", "command"))
    if kind not in OWN_KINDS:
        raise ValueError("unsupported")
    command = str(own.get("command", "") or "")
    env_names = str(own.get("env", "") or "")
    url = str(own.get("url", "") or "")
    token_env = str(own.get("token_env", "") or "")
    token = str(own.get("token", "") or "")
    permission = str(own.get("permission", "") or "")
    if permission not in ("", "pending", "deny"):
        raise ValueError("unsupported")
    outcome: dict[str, Any] = {}
    if kind == "starter":
        from io import StringIO

        from sleeptalk.adapter_cli import scaffold

        language = "node" if own.get("language") == "node" else "python"
        target = ((home or Path.home() / ".sleeptalk") / "agents" / answers.slug).resolve()
        if not (target.exists() and any(target.iterdir())):
            scaffold(answers.slug, kind="command", language=language, directory=str(target), out=StringIO())
        program = target / ("agent.py" if language == "python" else "agent.mjs")
        command = f"{'python3' if language == 'python' else 'node'} {program}"
        outcome["starter"] = str(program)
        kind, env_names, url, token_env = "command", "", "", ""
    try:
        agent_type, options = own_agent_settings(kind, command=command, env_names=env_names, url=url, token_env=token_env)
    except ValueError as exc:
        raise ValueError("detail:" + str(exc)) from None
    if agent_type == "command":
        hint = command_setup_hint(options["command"], options.get("protocol", "lite"))
        if hint:
            import os as _os
            import shlex

            program = options["command"][0]
            outcome["hint"] = hint
            outcome["suggested"] = shlex.join([program, *KNOWN_ACP_COMMANDS[_os.path.basename(program)][0][1:]])
    if kind == "acp" and not permission:
        permission = "pending"
    register_defaults()
    section = AgentConfig(type=agent_type, workspace_dir=str(Path(answers.workspace_dir or ".").resolve()),
                          permission_mode=permission, options=options)
    # The check sees only what a program needs to start, plus a token typed on
    # the page: never this process's own secrets under a name the page picks.
    check_env = {name: os.environ[name] for name in ("PATH", "HOME", "USER", "LANG", "LC_ALL", "TMPDIR", "SHELL", "TERM")
                 if name in os.environ}
    if token_env and token:
        check_env[token_env] = token
    report = await run_adapter_test(
        lambda: AGENTS.get(agent_type).factory(section.model_copy(deep=True), dict(check_env)),
        adapter=agent_type, mode="offline")
    failures = [f"{check.title}: {check.detail}" for check in report.checks if check.status == "fail"]
    outcome["checked"] = not report.failed
    if report.failed and not own.get("keep"):
        raise ValueError("check:" + json.dumps({"failures": failures[:6], **{k: v for k, v in outcome.items() if k in ("hint", "suggested")}}))
    answers.agent_type = agent_type
    answers.agent_options = options
    answers.agent_permission_mode = permission
    answers.agent_token = token
    return outcome


def splice_agent_section(path: Path, answers: Any) -> None:
    """Replace only the profile's [agent] tables (and its [binding], if it has one).

    The new tables come from the wizard's own serializer, so they read exactly
    as terminal setup writes them; everything else in the file, including
    settings the terminal doesn't model, is kept byte for byte.
    """
    from sleeptalk.config import AgentConfig, SleeptalkConfig
    from sleeptalk.profile_edit import toml_tables as _tables
    from sleeptalk.setup_core import profile_toml

    is_agent = lambda name: name == "agent" or name.startswith("agent.")  # noqa: E731
    fresh = "".join(piece for name, piece in _tables(profile_toml(answers)) if is_agent(name))
    fresh = fresh.rstrip("\n") + "\n\n"
    out: list[str] = []
    placed = False
    had_binding = False
    for name, piece in _tables(path.read_text(encoding="utf-8")):
        if is_agent(name):
            if not placed:
                out.append(fresh)
                placed = True
            continue
        if name == "binding":
            had_binding = True
            continue
        out.append(piece)
    if not placed:
        out.insert(0, fresh)
    text = "".join(out).rstrip("\n") + "\n"
    if had_binding:
        from sleeptalk.runtime_binding import bind, toml

        text += toml(bind(AgentConfig.model_validate(tomllib.loads(text)["agent"])))
    SleeptalkConfig.model_validate(tomllib.loads(text))
    from sleeptalk.private_input import atomic_write

    # A private temporary file (0600), like every other profile write: the
    # profile stays readable only by this user.
    atomic_write(path, text.encode("utf-8"))


def change_workspace(path: Path, profiles_dir: Path, workspace: str) -> str:
    """Move the profile's workspace; the agent and its answers stay as they are."""
    from sleeptalk.setup_core import load_existing_answers

    folder = Path(str(workspace or "").strip()).expanduser()
    if not str(workspace or "").strip() or not folder.is_absolute() or not folder.is_dir():
        raise ValueError("bad_workspace")
    answers = load_existing_answers(path.stem, profiles_dir)
    if answers is None:
        raise ValueError("unsupported")
    answers.workspace_dir = str(folder.resolve())
    splice_agent_section(path, answers)
    return answers.workspace_dir


async def change_agent(path: Path, profiles_dir: Path, env_path: Path, choice: dict[str, Any], *,
                       environ: dict[str, str] | None = None, look_for_keys: bool = False) -> dict[str, Any]:
    """Point an existing profile at another agent (or the same one, reconfigured)."""
    from sleeptalk.setup_core import (
        _credential_fields,
        env_entries,
        load_existing_answers,
        load_existing_secrets,
        merge_env_entries,
        store_agent_tokens,
    )

    answers = load_existing_answers(path.stem, profiles_dir)
    if answers is None:
        raise ValueError("unsupported")
    load_existing_secrets(answers, env_path, path)
    workspace = str(choice.get("workspace", "") or "").strip()
    if workspace:
        folder = Path(workspace).expanduser()
        if not folder.is_absolute() or not folder.is_dir():
            raise ValueError("bad_workspace")
        answers.workspace_dir = str(folder.resolve())
    outcome = await resolve_agent_choice(answers, choice, home=profiles_dir.parent, environ=environ,
                                         look_for_keys=look_for_keys)
    splice_agent_section(path, answers)
    # Agent tokens (OpenClaw, Hermes, a bridge's) go to the private store, as in the terminal.
    store_agent_tokens(answers, path, env_path)
    fields = _credential_fields(answers)
    rest = {name: value for name, value in env_entries(answers).items() if name not in fields and value}
    if rest:
        merge_env_entries(rest, env_path)
    return outcome




def new_profile_options(profiles_dir: Path, language: str, *, environ: dict[str, str] | None = None,
                        look_for_keys: bool = False) -> dict[str, Any]:
    """What the new-profile flow offers for one language. Never a key value."""
    from sleeptalk import speech_catalog
    from sleeptalk.language_support import primary_tag
    from sleeptalk.private_input import KEY_PROVIDERS
    from sleeptalk.voice_catalog import catalog_payload
    from sleeptalk.setup_core import PRODUCT_AGENTS

    if language not in _LANGUAGES:
        language = "en-US"
    facts_path = Path(__file__).parent / "_setup" / "providers.json"
    facts = {item["id"]: item for item in json.loads(facts_path.read_text(encoding="utf-8"))["providers"]}
    services = []
    for name in KEY_PROVIDERS:
        card = {key: facts.get(name, {}).get(key, "") for key in ("id", "name", "free", "usage", "card", "fine_print", "key_url")}
        card["id"] = name
        card["name"] = card["name"] or name.title()
        card["models"] = {role: speech_catalog.payload(name, role, language) for role in (speech_catalog.STT, speech_catalog.TTS)}
        card["voice"] = speech_catalog.recommended_voice(name, language)
        card["takes_key"] = True
        services.append(card)
    services.append(_realtime_card({}, {}, language))
    # Local speech: no key, no account. Offered whatever keys are found; it
    # listens and speaks English only, which the page says for other languages.
    local = {key: facts.get("local", {}).get(key, "") for key in ("id", "name", "free", "usage", "card", "fine_print", "key_url")}
    local.update({"id": "local", "name": local["name"] or "Local", "takes_key": False,
                  "english_only": primary_tag(language) != "en",
                  "models": {role: speech_catalog.payload("local", role, "en-US") for role in (speech_catalog.STT, speech_catalog.TTS)},
                  "voice": speech_catalog.recommended_voice("local", "en-US")})
    services.append(local)
    reusable = found_keys(environ, allowed=look_for_keys)
    existing = set(editable_profiles(profiles_dir)) if profiles_dir.is_dir() else set()
    base = "default" if "default" not in existing else "voice"
    return {
        "language": language,
        "languages": list(_LANGUAGES),
        "workspace": str(Path.cwd().resolve()),
        "suggested_name": _free_slug(profiles_dir, base) if profiles_dir.is_dir() else base,
        "existing": sorted(existing),
        # The template's name can't be a profile's: the page says so on the name step.
        "reserved": [_template_slug()],
        "agents": [{"id": key, "label": label, "web": True, **agent_readiness(key)} for key, label in PRODUCT_AGENTS],
        "found_agents": installed_agents(),
        "services": services,
        "voices_by_service": {**catalog_payload((*KEY_PROVIDERS, REALTIME), language), **catalog_payload(("local",), "en-US")},
        "samples": voice_samples(language),
        "reusable": reusable,
        "key_lookup": key_lookup_payload(look_for_keys),
    }


async def create_profile(profiles_dir: Path, env_path: Path, choice: dict[str, Any], *, environ: dict[str, str] | None = None,
                         look_for_keys: bool = False, local_job: "LocalSpeechJob | None" = None,
                         lock: asyncio.Lock | None = None) -> str:
    """Create one profile from the browser, the way the terminal wizard does.

    The speech key is proven first (a transcription and one spoken line), then
    the answers go through the same ``ensure_setup`` the wizard and ``host
    ensure`` use, which writes the profile and keeps the key in the private
    store. Raises ``ValueError`` with a fixed code, or with the setup's own
    problems after ``setup:``.
    """
    from sleeptalk.private_input import KEY_PROVIDERS
    from sleeptalk.profile_edit import set_tts_voice
    from sleeptalk.session import VOICE_ID_PATTERN
    from sleeptalk.setup import ensure_setup
    from sleeptalk.speech_choice import for_new_profile
    from sleeptalk.setup_core import answers_from_mapping, profile_name_slug

    name = str(choice.get("name", "")).strip()
    # A name with no Latin letters or digits has no slug of its own (the page says so too).
    slug = profile_name_slug(name)
    if not slug:
        raise ValueError("bad_profile_name")
    if (profiles_dir / f"{slug}.toml").exists():
        raise ValueError("profile_exists")
    workspace = Path(str(choice.get("workspace", "")).strip() or ".").expanduser()
    if not workspace.is_absolute() or not workspace.is_dir():
        raise ValueError("bad_workspace")
    agent = str(choice.get("agent", ""))
    from sleeptalk.setup_core import PRODUCT_AGENTS

    if agent not in {key for key, _label in PRODUCT_AGENTS} | {OWN_AGENT}:
        raise ValueError("unsupported")
    language = str(choice.get("language", "en-US"))
    if language not in _LANGUAGES:
        raise ValueError("unsupported")
    provider = str(choice.get("provider", ""))
    if provider not in (*KEY_PROVIDERS, REALTIME, "local"):
        raise ValueError("unsupported")
    surface = str(choice.get("surface", "web"))
    if surface not in ("web", "terminal"):
        raise ValueError("unsupported")
    delivery = str(choice.get("delivery", "")) if surface == "web" and choice.get("delivery") in ("tailnet", "local") else ""
    capture = str(choice.get("capture_mode", "")) if surface == "terminal" else ""
    if capture not in ("", "headphones", "speakers"):
        raise ValueError("unsupported")
    voice = str(choice.get("voice", "") or "")
    if voice and not VOICE_ID_PATTERN.fullmatch(voice):
        raise ValueError("voice_unavailable")
    models = {role: str(choice.get(f"{role}_model", "") or "") for role in ("stt", "tts")}
    if provider == REALTIME:
        return await _create_realtime_profile(
            profiles_dir, env_path, choice, name=name, slug=slug, workspace=workspace, agent=agent, language=language,
            surface=surface, delivery=delivery, capture=capture, voice=voice, models=models, environ=environ,
            look_for_keys=look_for_keys)
    for role, chosen in models.items():
        if chosen:
            try:
                _usable_model(provider, role, chosen)
            except ValueError:
                raise ValueError("unsupported") from None
    if provider == "local":
        return await _create_local_profile(
            profiles_dir, env_path, choice, name=name, slug=slug, workspace=workspace, agent=agent, language=language,
            surface=surface, delivery=delivery, capture=capture, voice=voice, models=models, environ=environ,
            look_for_keys=look_for_keys, job=local_job or LocalSpeechJob("create"), lock=lock)
    # The language's recommendation with the person's picks on top; a voice the
    # chosen speaking model can't speak moves to one it can.
    speech = for_new_profile(provider, language, stt_model=models["stt"], tts_model=models["tts"], voice=voice)

    value = str(choice.get("value", "") or "")
    if choice.get("reuse") is True:
        value = _found_key_value(provider, environ, allowed=look_for_keys)
    if not value.strip() or len(value) > 8192:
        raise ValueError("invalid_credential")
    try:
        details, _request = await prove_speech_key(provider, value, speech.voice, language=language, slug=slug,
                                                   workspace=str(workspace.resolve()), agent=agent,
                                                   stt_model=speech.stt_model, tts_model=speech.tts_model)
        answers = answers_from_mapping({
            "name": name, "slug": slug, "surface": surface, "route_type": "cascade", "agent_type": agent,
            "workspace_dir": str(workspace.resolve()), "language": language, "speech_provider": provider,
            "speech_key": value.strip(), "delivery": delivery, "capture_mode": capture,
            "stt_model": speech.stt_model, "tts_model": _profile_tts_model(speech, details),
        })
        # The agent's own questions (Hermes connection, OpenClaw token, a model's key).
        await resolve_agent_choice(answers, choice, home=profiles_dir.parent, environ=environ,
                                   look_for_keys=look_for_keys)
        profiles_dir.mkdir(parents=True, exist_ok=True)
        result = await ensure_setup(answers, profiles_dir=profiles_dir, env_path=env_path, live=False)
    finally:
        value = ""
    if result.status != "ready":
        raise ValueError("setup:" + " ".join(str(message) for message in result.messages)[:600])
    path = profiles_dir / f"{slug}.toml"
    chosen_voice = speech.voice or str(details.get("voice_id") or "")
    if chosen_voice:
        set_tts_voice(path, chosen_voice)
    return slug


async def _create_realtime_profile(profiles_dir: Path, env_path: Path, choice: dict[str, Any], *, name: str, slug: str,
                                   workspace: Path, agent: str, language: str, surface: str, delivery: str, capture: str,
                                   voice: str, models: dict[str, str], environ: dict[str, str] | None,
                                   look_for_keys: bool) -> str:
    """Prove the selected OpenAI protocol before writing a hybrid profile."""
    from sleeptalk.openai_realtime import OpenAIRealtimeConfig, check_openai_connectivity
    from sleeptalk.setup import ensure_setup
    from sleeptalk.setup_core import (
        DEFAULT_REALTIME_MODEL,
        DEFAULT_REALTIME_VOICE,
        answers_from_mapping,
    )

    model = models["tts"] or models["stt"] or _default_realtime_model(surface)
    if model not in {DEFAULT_REALTIME_MODEL, "gpt-live-1"} or any(value and value != model for value in models.values()):
        raise ValueError("unsupported")
    if model == "gpt-live-1" and surface != "web":
        raise ValueError("setup:GPT-Live-1 requires browser calls.")
    voice = voice or DEFAULT_REALTIME_VOICE
    if voice not in _realtime_voices():
        raise ValueError("voice_unavailable")
    value = str(choice.get("value", "") or "")
    if choice.get("reuse") is True:
        value = _found_key_value(REALTIME, environ, allowed=look_for_keys)
    if not value.strip() or len(value) > 8192:
        raise ValueError("invalid_credential")
    check = check_openai_connectivity
    if model == "gpt-live-1":
        from sleeptalk.openai_live import check_openai_live_connectivity
        check = check_openai_live_connectivity
    try:
        try:
            async with asyncio.timeout(60):
                await check(OpenAIRealtimeConfig(api_key=value.strip(), model=model, voice=voice, language=language))
        except Exception as exc:  # noqa: BLE001 — never expose a provider body or key
            raise ValueError(_realtime_failure_code(exc)) from None
        answers = answers_from_mapping({
            "name": name, "slug": slug, "surface": surface, "route_type": "hybrid", "agent_type": agent,
            "workspace_dir": str(workspace.resolve()), "language": language, "realtime_key": value.strip(),
            "realtime_model": model, "realtime_voice": voice, "delivery": delivery, "capture_mode": capture,
        })
        await resolve_agent_choice(answers, choice, home=profiles_dir.parent, environ=environ,
                                   look_for_keys=look_for_keys)
        profiles_dir.mkdir(parents=True, exist_ok=True)
        result = await ensure_setup(answers, profiles_dir=profiles_dir, env_path=env_path, live=False)
    finally:
        value = ""
    if result.status != "ready":
        raise ValueError("setup:" + " ".join(str(message) for message in result.messages)[:600])
    return slug


async def _create_local_profile(profiles_dir: Path, env_path: Path, choice: dict[str, Any], *, name: str, slug: str,
                                workspace: Path, agent: str, language: str, surface: str, delivery: str, capture: str,
                                voice: str, models: dict[str, str], environ: dict[str, str] | None, look_for_keys: bool,
                                job: "LocalSpeechJob", lock: asyncio.Lock | None) -> str:
    """A new profile on local speech: no key. The models download and one turn
    is proven on this computer before anything is written, as when an existing
    profile switches to local."""
    from sleeptalk.profile_edit import set_tts_voice
    from sleeptalk.setup import ensure_setup
    from sleeptalk.setup_core import answers_from_mapping

    stt, tts = _local_models(models["stt"], models["tts"], voice)
    voice = voice or _local_default_voice(tts)
    await prove_local_speech(stt, tts, voice, language, job)
    answers = answers_from_mapping({
        "name": name, "slug": slug, "surface": surface, "route_type": "cascade", "agent_type": agent,
        "workspace_dir": str(workspace.resolve()), "language": language, "speech_provider": "local",
        "delivery": delivery, "capture_mode": capture, "stt_model": stt.id, "tts_model": tts.id,
        "tts_voice_id": voice,
    })
    await resolve_agent_choice(answers, choice, home=profiles_dir.parent, environ=environ, look_for_keys=look_for_keys)
    async with (lock or asyncio.Lock()):
        if (profiles_dir / f"{slug}.toml").exists():
            raise ValueError("profile_exists")
        profiles_dir.mkdir(parents=True, exist_ok=True)
        result = await ensure_setup(answers, profiles_dir=profiles_dir, env_path=env_path, live=False)
        if result.status != "ready":
            raise ValueError("setup:" + " ".join(str(message) for message in result.messages)[:600])
        set_tts_voice(profiles_dir / f"{slug}.toml", voice)
    return slug


def _profile_tts_model(speech, details: dict[str, Any]) -> str:
    """The speaking model as the profile writes it; for Deepgram that is the voice."""
    from sleeptalk import speech_catalog
    from sleeptalk.speech_choice import tts_option

    voice = speech.voice or str(details.get("voice_id") or "")
    option = tts_option(speech.tts_provider, speech.tts_model, voice)
    if option is not None and option.model_is_voice:
        return speech_catalog.profile_model(option, voice)
    return speech.tts_model


_PROFILE_SLUG_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")


def _template_slug() -> str:
    from sleeptalk.launcher import TEMPLATE_PROFILE

    return Path(str(TEMPLATE_PROFILE)).stem


async def replace_discord_token(path: Path, value: str, *, store=None) -> dict[str, Any]:
    """Prove a new bot token and point this Discord profile at it."""
    from sleeptalk.credential_store import CredentialStore
    from sleeptalk.profile_edit import set_string_key

    from sleeptalk.key_inventory import forget_dropped, references_in

    store = store or CredentialStore()
    setup = DiscordSetup()
    before = references_in(path)
    summary = await setup.accept_token(value, store=store, profile_path=path)
    token_env = str(_get(_read(path), "transport", "token_env", default="") or "DISCORD_BOT_TOKEN")
    set_string_key(path, "credentials.refs", token_env, setup.reference)
    store.bind_profile(setup.reference, path.stem + "@" + hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:12])
    forget_dropped(before, path, store=store)
    return summary


def stop_profile_calls(path: Path) -> int:
    """End this profile's running calls, as `sleeptalk host stop` does; how many ended.

    A call that was closed can keep running while it shuts down, and holds
    the profile until it exits ("already has a live call"). TERM first, KILL
    after five seconds.
    """
    from sleeptalk.call_registry import (
        CallNotFoundError,
        ProfileIdentity,
        list_live_calls,
        registry_root_for_config,
        stop_call,
    )

    root = registry_root_for_config(path)
    key = ProfileIdentity.from_config(config_path=str(path), profile=path.stem).key
    stopped = 0
    for record in list_live_calls(root=root):
        if record.profile_key != key:
            continue
        try:
            stop_call(record.call_id, root=root)
        except CallNotFoundError:
            pass  # It finished on its own meanwhile.
        stopped += 1
    return stopped


def start_call(path: Path, *, here: bool, replace: bool = False) -> dict[str, Any]:
    """Start a browser call on this profile, as `sleeptalk host start-voice` does.

    ``here`` opens it on this computer (unless the profile delivers over the
    tailnet); otherwise the link travels the profile's way, for a phone.
    ``replace`` ends a call this profile still has running first.
    """
    from sleeptalk.host import start_voice_web

    if replace:
        stop_profile_calls(path)
    tailnet = str(_get(_read(path), "transport", "options", "delivery", default="") or "") == "tailnet"
    receipt = start_voice_web(profile=path.stem, config=str(path), local=here and not tailnet, tailnet=tailnet,
                              platform_user_id="settings-page")
    if receipt.get("code") == "busy":
        # The page offers to end it and start again.
        raise ValueError("call_running")
    if receipt.get("status") == "error" or not receipt.get("join_url"):
        reason = str(receipt.get("error") or receipt.get("message") or receipt.get("say") or "The call couldn't start.")
        raise ValueError("detail:" + reason.replace("sleeptalk config has 1 problem(s):", "").strip())
    result = {"join_url": receipt["join_url"], "delivery": receipt.get("delivery", ""), "say": receipt.get("say", "")}
    if not here:
        # The other device is usually a phone: it scans this off the screen.
        from sleeptalk.qr import qr_svg

        result["qr_svg"] = qr_svg(receipt["join_url"])
    return result


def rename_profile(path: Path, profiles_dir: Path, new_name: str) -> Path:
    """Give a profile a new name: move its file, keep its keys attached."""
    from sleeptalk.credential_store import CredentialStore
    from sleeptalk.setup_core import load_existing_answers, slugify

    slug = slugify(str(new_name or ""))
    if not slug or not _PROFILE_SLUG_PATTERN.fullmatch(slug) or slug == _template_slug():
        raise ValueError("bad_profile_name")
    target = profiles_dir / f"{slug}.toml"
    if target.exists():
        raise ValueError("profile_exists")
    answers = load_existing_answers(path.stem, profiles_dir)
    if answers is None:
        raise ValueError("unsupported")
    old_id = path.stem + "@" + hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:12]
    os.replace(path, target)
    answers.slug = slug
    splice_agent_section(target, answers)
    new_id = slug + "@" + hashlib.sha256(str(target.resolve()).encode()).hexdigest()[:12]
    refs = _get(_read(target), "credentials", "refs", default={})
    if isinstance(refs, dict) and refs:
        try:
            store = CredentialStore()
            for reference in refs.values():
                store.bind_profile(str(reference), new_id)
                store.unbind_profile(str(reference), old_id)
        except Exception:  # noqa: BLE001 — bookkeeping; reading keys never depends on it
            pass
    return target


def delete_profile(path: Path, *, store=None) -> list[str]:
    """Remove a profile file, and the keys no other profile uses (never revoked
    at the provider). Returns the references removed."""
    from sleeptalk.key_inventory import forget_unused, references_in

    if not path.is_file() or path.stem == _template_slug():
        raise ValueError("unsupported")
    from sleeptalk.key_inventory import profile_ids

    before = references_in(path)
    ids = profile_ids(path)
    path.unlink()
    try:
        return forget_unused(before, store=store, extra_dirs=[path.parent], only_bound_to=ids)
    except Exception:  # noqa: BLE001 — the profile is gone either way
        return []


# Per-agent answers a request may carry: one flat object of strings and booleans each.
_AGENT_DETAIL = {"hermes", "openclaw", "model", "own"}


def _flat(value: Any) -> bool:
    return isinstance(value, dict) and len(value) <= 12 and all(
        isinstance(k, str) and isinstance(v, (str, bool)) and len(str(v)) <= 8192 for k, v in value.items())


def _refusal(exc: ValueError):
    code = str(exc)
    if code.startswith("setup:"):
        return web.json_response({"status": "refused", "code": "setup", "message": code[6:]}, status=422)
    if code.startswith("check:"):
        return web.json_response({"status": "refused", "code": "check", **json.loads(code[6:])}, status=422)
    if code.startswith("detail:"):
        return web.json_response({"status": "refused", "code": "detail", "message": code[7:]}, status=422)
    return web.json_response({"status": "refused", "code": code}, status=422)


# Minutes of no use after which the settings session ends.
IDLE_SECONDS = 30 * 60

# A page reached from somewhere else (a phone link, or Advanced settings in a
# call) changes Sleeptalk's settings, can install one of the listed agents
# (its fixed, built-in command: the page picks the agent, never the command),
# and can set up, start, or stop a Discord bot (Sleeptalk's own command for
# that profile; it can't open one to everyone, but it picks the operator).
# It never runs a program of its own choosing.
# These stay with someone at the computer itself.
REMOTE_REFUSED = frozenset({"/api/keys/move"})
REMOTE_REFUSED_SETTINGS = frozenset({"transport.authorization_required", "transport.operator_ids"})
ON_HOST_ONLY = ("This change can only be made on the computer running your agent: "
                "run `sleeptalk settings` there.")
# While an agent's key request is open, its page does only that request.
REQUEST_PATHS = frozenset({"/api/state", "/api/request/key", "/api/request/discord", "/api/request/finish",
                           "/api/preview", "/api/keys/lookup"})


class SettingsPage:
    """Loopback server for one profile's settings page, with the key page's boundary rules."""

    def __init__(self, profile_path: Path, *, profiles_dir: Path | None = None, environ: dict[str, str] | None = None,
                 env_path: Path | None = None, state_dir: Path | None = None, request=None) -> None:
        # The profile may not exist yet: a first run opens straight into New profile.
        self.profile_path = profile_path
        # An agent's key request (`host input link`), when this page is answering one:
        # the page opens on it, and saving goes through the request's own checks.
        self.request = request
        self._request_models: dict[str, dict[str, str]] = {}
        # Set when the page is reached through the sealed channel (remote_settings).
        self.remote = False
        self.state_dir = state_dir or Path.home() / ".local/share/sleeptalk"
        self.profiles_dir = profiles_dir or profile_path.parent
        self.env_path = env_path or self.profiles_dir.parent / ".env"
        self.environ = dict(os.environ) if environ is None else environ
        self.token = secrets.token_urlsafe(32)
        # Opened with `sleeptalk settings`, the link opens the page in any
        # browser here (Safari and Chrome alike): it is loopback-only and
        # appears only in this user's terminal and browser. A link that passed
        # through someone else (an agent's key request, or the sealed channel
        # to a phone) opens one session; after that only the browser already
        # holding the session cookie can exchange it again, and a copy can't.
        self.token_used = False
        self.cookie = secrets.token_urlsafe(32)
        self.cookie_name = "settings_" + hashlib.sha256(str(profile_path).encode()).hexdigest()[:16]
        self.csrf = secrets.token_urlsafe(32)
        self.last_used = time.monotonic()
        self.origin = ""
        self.lock = asyncio.Lock()
        self.preview_lock = asyncio.Lock()
        self.discord = DiscordSetup()
        self.local_job: LocalSpeechJob | None = None
        self.voice_upload: dict | None = None
        from sleeptalk.speech.hosted_voices import Upload
        self.hosted_voice_upload = Upload()
        self.clone_lock = asyncio.Lock()

    @property
    def link_shared(self) -> bool:
        """The link passed through someone else: an agent's key request, or the sealed channel."""
        return self.remote or self.request is not None

    @property
    def look_for_keys(self) -> bool:
        return key_lookup_allowed(self.state_dir)

    def _authenticated(self, request) -> None:
        if not secrets.compare_digest(request.cookies.get(self.cookie_name, ""), self.cookie):
            raise web.HTTPUnauthorized()
        if not secrets.compare_digest(request.headers.get("X-Settings-CSRF", ""), self.csrf):
            raise web.HTTPForbidden()
        # A shared link's page (an agent's key request, or a phone through the
        # sealed channel) ends its session when left idle: a link left open
        # elsewhere can't change keys an hour later. The page polls only while
        # local speech downloads, so idle means nobody has used it. The page opened with `sleeptalk
        # settings` stays open while it runs: it is loopback-only, and whoever
        # can reach it can already read the profiles it edits.
        now = time.monotonic()
        if self.link_shared and now - self.last_used > IDLE_SECONDS:
            self.cookie = secrets.token_urlsafe(32)
            raise web.HTTPUnauthorized()
        self.last_used = now
        if self.remote and request.path in REMOTE_REFUSED:
            raise web.HTTPForbidden(text=ON_HOST_ONLY)
        if self.request is not None and self.request.state["status"] != "complete" and request.path not in REQUEST_PATHS:
            raise web.HTTPForbidden(text="Finish the voice setup your agent asked for first.")

    async def exchange(self, request):
        body = await request.json()
        if not isinstance(body, dict) or not isinstance(body.get("token"), str) or not secrets.compare_digest(body["token"], self.token):
            raise web.HTTPUnauthorized()
        if self.link_shared and self.token_used and not secrets.compare_digest(request.cookies.get(self.cookie_name, ""), self.cookie):
            raise web.HTTPUnauthorized()
        self.token_used = True
        self.last_used = time.monotonic()
        response = web.json_response({"csrf": self.csrf})
        # On a page opened here the cookie lasts as long as this server; a shared link's keeps its six hours.
        response.set_cookie(self.cookie_name, self.cookie, httponly=True, samesite="Strict",
                            max_age=6 * 3600 if self.link_shared else None)
        return response

    async def state(self, request):
        self._authenticated(request)
        from sleeptalk import tailnet as tailnet_driver
        summary = await tailnet_driver.detect(environ=self.environ)
        tailnet = {"available": bool(summary.get("available")), **({"name": summary["name"]} if summary.get("name") else {}),
                   **({"reason": summary["code"]} if summary.get("code") else {})}
        extra = {"request": self.request_view()} if self.request is not None else {}
        if not self.profile_path.is_file():
            # Nothing to describe yet: the page opens on New profile (or the agent's request).
            language = self.request.spec.language if self.request is not None else "en-US"
            return web.json_response({"profile": None, "profiles": editable_profiles(self.profiles_dir) if self.profiles_dir.is_dir() else [],
                                      "language": {"value": language if language in _LANGUAGES else "en-US", "options": list(_LANGUAGES)},
                                      "icons": brand_icons(), "tailnet": tailnet, "here": not self.remote, **extra})
        return web.json_response({
            **describe_profile(self.profile_path, environ=self.environ, look_for_keys=self.look_for_keys),
            "profiles": editable_profiles(self.profiles_dir),
            "tailnet": tailnet, "here": not self.remote, **extra,
        })

    # ---------- An agent's key request, answered on this page ----------

    def request_view(self) -> dict[str, Any]:
        """What the page shows for the agent's request: which services, what is
        saved, what's next. Never a key value."""
        from sleeptalk.private_input import KEY_PROVIDERS

        session = self.request
        spec, choices = session.spec, session.choices
        allowed = session.allowed_providers()
        refs = session.state["refs"]
        speech = [name for name in KEY_PROVIDERS if name in allowed]
        language = spec.language if spec.language in _LANGUAGES else "en-US"
        options = new_profile_options(self.profiles_dir, language, environ=self.environ, look_for_keys=self.look_for_keys)
        options["services"] = [item for item in options["services"] if item["id"] in speech]
        # A fixed request may name two services (one listens, one speaks); each needs its key.
        missing_speech = [name for name in speech if name not in refs]
        if session.state["status"] == "complete":
            step = "done"
        elif missing_speech:
            step = "speech"
        elif "discord" in allowed and "discord" not in refs:
            step = "discord_token"
        elif spec.surface == "discord" and not session.repair_provider:
            step = "discord_place"
        else:
            step = "done"
        return {
            "status": session.state["status"], "step": step,
            "mode": "repair" if session.repair_provider else "setup",
            "surface": spec.surface, "profile": spec.profile,
            "choice": "open" if choices.open_choice and not session.repair_provider else "fixed",
            "preferred": spec.preferred_provider if choices.open_choice else "",
            "voice_locked": choices.tts_voice_id or "",
            "saved": sorted(refs), "missing": missing_speech, "expires_at": int(session.state["expires_at"]),
            "discord": session.discord_summary() if "discord" in refs else None,
            "options": options,
        }

    def _request_done(self) -> None:
        """The request wrote its profile: apply the models chosen on the page, then edit that profile."""
        session = self.request
        # A replaced key keeps the profile's models; a new profile takes the ones picked on the page.
        chosen = {} if session.repair_provider else self._request_models.get(session.state.get("provider") or session.choices.tts_provider or "", {})
        for role in ("stt", "tts"):
            if chosen.get(role):
                try:
                    set_model(session.profile_path, role, chosen[role])
                except (ValueError, OSError):
                    pass  # the recommended model stays
        self.profile_path = session.profile_path

    async def request_key(self, request):
        """Save one key for the agent's request: a speech key (with its voice and models) or a bot token."""
        self._authenticated(request)
        if self.request is None:
            raise web.HTTPNotFound()
        body = await request.json()
        allowed_fields = {"provider", "value", "reuse", "voice_id", "stt_model", "tts_model"}
        if not isinstance(body, dict) or set(body) - allowed_fields or not isinstance(body.get("provider"), str):
            raise web.HTTPBadRequest()
        session, provider = self.request, body["provider"]
        voice = body.get("voice_id", "") if isinstance(body.get("voice_id", ""), str) else ""
        value = body.get("value", "")
        if body.get("reuse") is True:
            try:
                value = _found_key_value(provider, self.environ, allowed=self.look_for_keys)
            except ValueError:
                return web.json_response({"status": "refused", "code": "key_not_found"}, status=422)
        if not isinstance(value, str):
            raise web.HTTPBadRequest()
        models = {role: str(body.get(f"{role}_model") or "") for role in ("stt", "tts")}
        try:
            for role, chosen in models.items():
                if chosen:
                    _usable_model(provider, role, chosen)
        except ValueError:
            return web.json_response({"status": "refused", "code": "unsupported"}, status=422)
        try:
            result = await session.accept(provider, value, voice.strip(), confirm_replacement=bool(session.repair_provider))
        except web.HTTPGone:
            return web.json_response({"status": "refused", "code": "request_ended"}, status=410)
        except web.HTTPBadRequest:
            return web.json_response({"status": "refused", "code": "invalid_submission"}, status=422)
        finally:
            value = ""
            body.clear()
        if result["status"] == "needs_input":
            return web.json_response({"status": "refused", "code": result["code"]}, status=422)
        self._request_models[provider] = models
        view = self.request_view()
        if view["step"] == "done" and session.state["status"] != "complete":
            # Every key is in and nothing else to choose: write the profile now.
            try:
                await session.complete({})
            except web.HTTPGone:
                return web.json_response({"status": "refused", "code": "request_ended"}, status=410)
            except web.HTTPException as exc:
                return web.json_response({"status": "refused", "code": "detail", "message": exc.text or "The profile couldn't be written."}, status=422)
        if session.state["status"] == "complete":
            self._request_done()
        return web.json_response(self.request_view())

    async def request_discord(self, request):
        """The bot's servers, and one server's voice channels, for the request's Discord steps."""
        self._authenticated(request)
        if self.request is None:
            raise web.HTTPNotFound()
        body = await request.json()
        guild_id = body.get("guild_id", "") if isinstance(body, dict) else None
        if not isinstance(guild_id, str) or (guild_id and not re.fullmatch(r"[0-9]{1,20}", guild_id)):
            raise web.HTTPBadRequest()
        try:
            options = await self.request.discord_options(guild_id)
        except LookupError:
            return web.json_response({"status": "refused", "code": "not_ready"}, status=409)
        except RuntimeError:
            return web.json_response({"status": "refused", "code": "discord_unavailable"}, status=422)
        return web.json_response({**self.request.discord_summary(), "guild_id": guild_id, **options})

    async def request_finish(self, request):
        """Where the bot talks and who may use it: the request's last step, which writes the profile."""
        self._authenticated(request)
        if self.request is None:
            raise web.HTTPNotFound()
        from sleeptalk.private_input import ChoiceError

        body = await request.json()
        fields = {"guild_id", "channel_id", "operator_id"}
        if not isinstance(body, dict) or set(body) != fields or not all(
                isinstance(body[k], str) and re.fullmatch(r"[0-9]{1,20}", body[k]) for k in fields):
            return web.json_response({"status": "refused", "code": "discord_choices"}, status=422)
        try:
            await self.request.complete(dict(body))
        except ChoiceError as exc:
            return web.json_response({"status": "refused", "code": exc.code}, status=422)
        except web.HTTPGone:
            return web.json_response({"status": "refused", "code": "request_ended"}, status=410)
        except web.HTTPException as exc:
            return web.json_response({"status": "refused", "code": "detail", "message": exc.text or "The profile couldn't be written."}, status=422)
        self._request_done()
        return web.json_response(self.request_view())

    async def key_lookup(self, request):
        """Turn looking for keys other apps keep on or off; the answer is remembered."""
        self._authenticated(request)
        body = await request.json()
        if not isinstance(body, dict) or set(body) != {"allowed"} or not isinstance(body["allowed"], bool):
            raise web.HTTPBadRequest()
        set_key_lookup(self.state_dir, body["allowed"])
        return web.json_response({"status": "saved", "reusable": found_keys(self.environ, allowed=body["allowed"]),
                                  "key_lookup": key_lookup_payload(body["allowed"])})

    async def set_value(self, request):
        self._authenticated(request)
        body = await request.json()
        if not isinstance(body, dict) or set(body) != {"setting", "value"} or not isinstance(body["setting"], str):
            raise web.HTTPBadRequest()
        if self.remote and body["setting"] in REMOTE_REFUSED_SETTINGS:
            raise web.HTTPForbidden(text=ON_HOST_ONLY)
        async with self.lock:
            try:
                if body["setting"] == "language" and body["value"] in _LANGUAGES:
                    return web.json_response({"status": "saved", "changed": set_language(self.profile_path, body["value"])})
                apply_setting(self.profile_path, body["setting"], body["value"])
            except ValueError as exc:
                return web.json_response({"status": "refused", "message": str(exc)}, status=422)
            except OSError:
                return web.json_response({"status": "refused", "message": "The profile could not be written."}, status=500)
        return web.json_response({"status": "saved"})

    async def use_speech(self, request):
        """Switch the speech service: a pasted key, or one already on this machine."""
        self._authenticated(request)
        body = await request.json()
        if not isinstance(body, dict) or set(body) - {"provider", "value", "voice_id", "reuse", "stt_model", "tts_model"} or not isinstance(body.get("provider"), str):
            raise web.HTTPBadRequest()
        provider, voice = body["provider"], body.get("voice_id", "")
        models = {name: body.get(name, "") for name in ("stt_model", "tts_model")}
        if not all(isinstance(value, str) for value in models.values()):
            raise web.HTTPBadRequest()
        value = body.get("value", "")
        if body.get("reuse") is True:
            try:
                value = _found_key_value(provider, self.environ, allowed=self.look_for_keys)
            except ValueError:
                return _refused("key_not_found")
        try:
            async with self.lock:
                if provider == REALTIME:
                    result = await switch_to_realtime(self.profile_path, value, voice if isinstance(voice, str) else "",
                                                      **({"model": models["tts_model"] or models["stt_model"]} if any(models.values()) else {}))
                else:
                    result = await switch_speech_service(self.profile_path, provider, value, voice if isinstance(voice, str) else "", **models)
        except ValueError as exc:
            return _refused(str(exc))
        finally:
            value = ""
            body.clear()
        return web.json_response({"status": "saved", **result})

    @staticmethod
    def _local_choice(body: Any) -> tuple[str, str, str]:
        if not isinstance(body, dict) or set(body) - {"stt_model", "tts_model", "voice_id"} \
                or not all(isinstance(body.get(name, ""), str) and len(body.get(name, "")) <= 200
                           for name in ("stt_model", "tts_model", "voice_id")):
            raise web.HTTPBadRequest()
        return body.get("stt_model", ""), body.get("tts_model", ""), body.get("voice_id", "")

    async def local_status(self, request):
        """What switching to local speech still needs, and the job under way, if any."""
        self._authenticated(request)
        choice = self._local_choice(await request.json())
        try:
            status = await asyncio.to_thread(local_speech_status, *choice)
        except ValueError as exc:
            return _refused(str(exc))
        return web.json_response({**status, "job": self.local_job.payload() if self.local_job else None})

    def _start_local_job(self, job: LocalSpeechJob, work) -> web.Response:
        if self.local_job is not None and self.local_job.running:
            return _refused("local_busy")
        self.local_job = job
        job.task = asyncio.create_task(work, name=f"settings-local-{job.kind}")
        return web.json_response({"status": "started", "job": job.payload()})

    async def local_install(self, request):
        """Install the local speech runtime, after the page showed the exact command."""
        self._authenticated(request)
        body = await request.json()
        if body not in ({}, {"stt_model": "moonshine-small-streaming-en"}, {"stt_model": "moonshine-tiny-en"}):
            raise web.HTTPBadRequest()
        job = LocalSpeechJob("install")
        install = job.install(streaming=body.get("stt_model") != "moonshine-tiny-en")
        return self._start_local_job(job, install)

    async def local_switch(self, request):
        """Download, prove, and switch this profile to local speech; the page polls the status."""
        self._authenticated(request)
        stt_model, tts_model, voice = self._local_choice(await request.json())
        try:
            _local_models(stt_model, tts_model, voice)
        except ValueError as exc:
            return _refused(str(exc))
        job = LocalSpeechJob("switch")
        return self._start_local_job(job, job.switch(self.profile_path, stt_model, tts_model, voice, self.lock))

    def _hosted_voice_key(self, provider: str) -> tuple[str, str]:
        """Cloning uses only the selected service's saved profile credential."""
        from sleeptalk.credential_store import CredentialError, CredentialStore
        from sleeptalk.private_input import ENV_NAMES
        from sleeptalk.speech.hosted_voices import CloneError, PROVIDERS

        document = _read(self.profile_path)
        if (not isinstance(provider, str) or provider not in PROVIDERS or _is_realtime(document)
                or _get(document, "tts", "provider") != provider):
            raise CloneError("Switch to this speech service and save its key before cloning a voice.")
        reference = _get(document, "credentials", "refs", ENV_NAMES[provider])
        if not reference:
            raise CloneError("Save a key for this service before cloning a voice.")
        try:
            store = CredentialStore()
            if store.status(reference).provider != provider:
                raise CredentialError("Credential provider mismatch.")
            key = store.get(reference)
        except CredentialError:
            raise CloneError("The saved key is unavailable. Save the service key again.") from None
        return key, str(self.profile_path.resolve()) + ":" + reference + ":" + hashlib.sha256(key.encode()).hexdigest()

    async def hosted_voices(self, request):
        self._authenticated(request)
        from sleeptalk.speech.hosted_voices import CloneError, list_voices

        body = await request.json()
        if not isinstance(body, dict) or set(body) != {"provider"}:
            raise web.HTTPBadRequest()
        key = ""
        try:
            key, _ = self._hosted_voice_key(body["provider"])
            return web.json_response({"voices": await list_voices(body["provider"], key)})
        except CloneError as exc:
            return web.json_response({"message": str(exc)}, status=422)
        finally:
            key = ""

    async def clone_hosted_voice(self, request):
        self._authenticated(request)
        from sleeptalk.speech.hosted_voices import CloneError, create_voice

        body = await request.json()
        if not isinstance(body, dict):
            raise web.HTTPBadRequest()
        if self.clone_lock.locked():
            return web.json_response({"message": "A voice is being created. Wait for it to finish."}, status=409)
        key = ""
        async with self.clone_lock:
            try:
                key, binding = self._hosted_voice_key(body.get("provider"))
                upload_id, audio = self.hosted_voice_upload.accept(body, binding)
                if audio is None:
                    return web.json_response({"upload_id": upload_id})
                voice = await create_voice(body["provider"], key, body["name"], body["language"], audio)
                return web.json_response({"voice": voice})
            except CloneError as exc:
                return web.json_response({"message": str(exc)}, status=422)
            finally:
                key = ""
                body.clear()

    async def upload_local_voice(self, request):
        """Bounded chunks also fit the phone's authenticated settings channel."""
        self._authenticated(request)
        import base64
        import binascii
        from sleeptalk.speech.local_voices import MAX_AUDIO_BYTES, MODEL, list_voices, save_voice

        body = await request.json()
        if (not isinstance(body, dict) or set(body) != {"model", "name", "audio", "offset", "upload_id", "final"}
                or body["model"] != MODEL or not isinstance(body["name"], str)
                or not 1 <= len(body["name"].strip()) <= 64
                or not isinstance(body["audio"], str) or len(body["audio"]) > 12000
                or type(body["offset"]) is not int or type(body["final"]) is not bool
                or not isinstance(body["upload_id"], str)):
            raise web.HTTPBadRequest()
        try:
            chunk = base64.b64decode(body["audio"], validate=True)
        except (ValueError, binascii.Error):
            raise web.HTTPBadRequest() from None
        async with self.lock:
            first = body["offset"] == 0 and not body["upload_id"]
            if first:
                self.voice_upload = {"id": secrets.token_hex(16), "name": body["name"],
                                     "audio": bytearray(), "expires": time.monotonic() + 300}
            upload = self.voice_upload
            if (upload is None or time.monotonic() > upload["expires"]
                    or (not first and body["upload_id"] != upload["id"])
                    or body["offset"] != len(upload["audio"]) or body["name"] != upload["name"]):
                raise web.HTTPConflict(text="Upload interrupted. Choose the recording again.")
            if not chunk or len(upload["audio"]) + len(chunk) > MAX_AUDIO_BYTES:
                self.voice_upload = None
                raise web.HTTPRequestEntityTooLarge(max_size=MAX_AUDIO_BYTES, actual_size=len(upload["audio"]) + len(chunk))
            upload["audio"].extend(chunk)
            if not body["final"]:
                return web.json_response({"upload_id": upload["id"], "offset": len(upload["audio"])})
            self.voice_upload = None
            try:
                if len(list_voices()) >= 50:
                    raise ValueError("The custom voice library is full (50 voices).")
                voice = await asyncio.to_thread(save_voice, upload["name"], bytes(upload["audio"]))
            except ValueError as exc:
                return web.json_response({"message": str(exc)}, status=422)
        return web.json_response({"voice": voice})

    async def preview(self, request):
        """A short line in a voice, through the profile's own key; one at a time."""
        self._authenticated(request)
        body = await request.json()
        if not isinstance(body, dict) or set(body) - {"model", "voice_id"} or not isinstance(body.get("voice_id"), str) \
                or not isinstance(body.get("model", ""), str):
            raise web.HTTPBadRequest()
        from sleeptalk.setup_core import sample_line

        language = str(_get(_read(self.profile_path), "tts", "language", default="en-US") or "en-US")
        if self.preview_lock.locked():
            return web.json_response({"status": "refused", "code": "busy"}, status=429)
        async with self.preview_lock:
            try:
                if request.path == "/api/speech/local/voices/preview":
                    from sleeptalk.builder import tts_spec
                    from sleeptalk.config import TTSConfig
                    from sleeptalk.speech.local_tts import LocalTTS

                    if body.get("model") != "pocket-tts-en":
                        raise ValueError("unsupported")
                    speaker = LocalTTS(tts_spec(TTSConfig(provider="local", model="pocket-tts-en", voice_id=body["voice_id"])))
                    try:
                        if speaker.model.id != "pocket-tts-en":
                            raise ValueError("unsupported")
                        async with asyncio.timeout(30):
                            audio = await speaker.synthesize_wav_bytes(sample_line("en-US"))
                    finally:
                        await speaker.close()
                else:
                    audio = await preview_voice(self.profile_path, model=body.get("model", ""), voice=body["voice_id"],
                                                text=sample_line(language))
            except ValueError as exc:
                return web.json_response({"status": "refused", "code": str(exc)}, status=422)
            except Exception:  # noqa: BLE001 — a code, never a provider body
                return web.json_response({"status": "refused", "code": "preview_failed"}, status=502)
        return web.Response(body=audio, content_type="audio/wav")

    async def discord_token(self, request):
        self._authenticated(request)
        body = await request.json()
        if not isinstance(body, dict) or set(body) != {"value"}:
            raise web.HTTPBadRequest()
        from sleeptalk.credential_store import CredentialStore

        try:
            async with self.lock:
                result = await self.discord.accept_token(body["value"], store=CredentialStore(), profile_path=self.profile_path)
        except ValueError as exc:
            return web.json_response({"status": "refused", "code": str(exc)}, status=422)
        finally:
            body.clear()
        return web.json_response({"status": "saved", **result})

    async def discord_options(self, request):
        self._authenticated(request)
        body = await request.json()
        guild = body.get("guild_id", "") if isinstance(body, dict) else ""
        if not isinstance(guild, str) or (guild and not guild.isdigit()) or not self.discord.reference:
            raise web.HTTPBadRequest()
        from sleeptalk.credential_store import CredentialStore

        try:
            return web.json_response(await self.discord.options(guild, store=CredentialStore()))
        except Exception:  # noqa: BLE001
            return web.json_response({"status": "refused", "code": "discord_unavailable"}, status=422)

    async def discord_current(self, request):
        """Steps 2-4 for this profile's own bot, with the token it already has."""
        self._authenticated(request)
        from sleeptalk.credential_store import CredentialStore

        try:
            summary = await self.discord.load_existing(self.profile_path, store=CredentialStore())
            guild = str(_get(_read(self.profile_path), "transport", "guild_id", default=""))
            if summary.get("guilds"):
                summary = await self.discord.options(guild, store=CredentialStore(), refresh=False)
        except ValueError as exc:
            return web.json_response({"status": "refused", "code": str(exc)}, status=422)
        except Exception:  # noqa: BLE001
            return web.json_response({"status": "refused", "code": "discord_unavailable"}, status=422)
        return web.json_response(summary)

    async def discord_lock(self, request):
        self._authenticated(request)
        body = await request.json()
        keys = {"guild_id", "channel_id", "operator_id"}
        if not isinstance(body, dict) or set(body) != keys:
            raise web.HTTPBadRequest()
        from sleeptalk.credential_store import CredentialStore

        try:
            async with self.lock:
                result = await lock_discord_profile(self.profile_path, self.discord, store=CredentialStore(), **body)
        except ValueError as exc:
            return web.json_response({"status": "refused", "code": str(exc)}, status=422)
        return web.json_response({"status": "saved", **result})

    async def discord_finish(self, request):
        self._authenticated(request)
        body = await request.json()
        keys = {"profile", "guild_id", "channel_id", "operator_id"}
        if not isinstance(body, dict) or set(body) != keys or not all(isinstance(body[k], str) for k in keys):
            raise web.HTTPBadRequest()
        from sleeptalk.credential_store import CredentialStore
        from sleeptalk.private_input import ChoiceError, InputError

        try:
            async with self.lock:
                result = await create_discord_profile(self.profile_path, self.discord, slug=body["profile"], guild_id=body["guild_id"],
                                                      channel_id=body["channel_id"], operator_id=body["operator_id"], store=CredentialStore())
        except ChoiceError as exc:
            return web.json_response({"status": "refused", "code": exc.code}, status=422)
        except InputError as exc:
            return _refused_with_reason(str(exc))
        except ValueError as exc:
            return web.json_response({"status": "refused", "code": str(exc)}, status=422)
        except web.HTTPException as exc:
            # The check that refused says why ("Validate each provider before finishing."); show that.
            return _refused_with_reason(exc.text or "")
        return web.json_response({"status": "saved", **result})

    async def _discord_service_profile(self, request) -> tuple[str, Path]:
        """The Discord profile a Start, Stop, or status call names."""
        body = await request.json()
        slug = body.get("profile") if isinstance(body, dict) else None
        from sleeptalk.setup_core import existing_profiles

        if not isinstance(slug, str) or slug not in existing_profiles(self.profiles_dir):
            raise web.HTTPBadRequest()
        path = self.profiles_dir / f"{slug}.toml"
        if _get(_read(path), "transport", "type", default="") != "discord":
            raise web.HTTPBadRequest()
        return slug, path

    async def discord_status(self, request):
        self._authenticated(request)
        slug, _path = await self._discord_service_profile(request)
        from sleeptalk import service

        running = await asyncio.to_thread(service.running, slug)
        installed = service.unit_path_for(slug).is_file()
        # Installed but down: say why, in the bot's own last words.
        reason = await asyncio.to_thread(service.failure, slug) if installed and running is False else None
        return web.json_response({"running": running, "installed": installed, "reason": reason or ""})

    async def discord_start(self, request):
        """Keep a Discord bot running: write its service definition, then (re)start it."""
        self._authenticated(request)
        slug, path = await self._discord_service_profile(request)
        from sleeptalk import service

        def refused(code, message=""):
            return web.json_response({"status": "refused", "code": code, "message": message}, status=422)

        if _missing_discord_extra():
            return refused("discord_extra_missing", _missing_discord_extra())
        from types import SimpleNamespace

        from sleeptalk.discord.access import DiscordAccess

        try:
            DiscordAccess.from_surface(SimpleNamespace(**(_get(_read(path), "transport", default={}) or {}))).validate()
        except ValueError:
            return refused("discord_unfinished")
        report = await asyncio.to_thread(service.preflight, slug, path)
        if report["status"] != "ready":
            return refused("service_blocked", " ".join(report["problems"]))
        async with self.lock:
            if await asyncio.to_thread(service.install, slug, path, yes=True, out=lambda _line: None):
                return refused("service_blocked", "The service definition couldn't be written.")
            try:
                note = await asyncio.to_thread(service.start, slug, path, Path(report["workspace"]))
            except ValueError as exc:
                return refused("service_failed", str(exc))
        # A bot with a bad token or a missing library exits within a few seconds.
        await asyncio.sleep(3)
        running = await asyncio.to_thread(service.running, slug)
        reason = None if running else await asyncio.to_thread(service.failure, slug)
        return web.json_response({"status": "running" if running else "stopped", "log": service.log_hint(slug, path),
                                  "reason": reason or "", "note": note or ""})

    async def discord_stop(self, request):
        self._authenticated(request)
        slug, _path = await self._discord_service_profile(request)
        from sleeptalk import service

        try:
            await asyncio.to_thread(service.stop, slug)
        except ValueError as exc:
            return web.json_response({"status": "refused", "code": "service_failed", "message": str(exc)}, status=422)
        return web.json_response({"status": "stopped"})

    async def new_options(self, request):
        """Choices for a new profile in one language (?language=es-MX)."""
        self._authenticated(request)
        return web.json_response(new_profile_options(self.profiles_dir, request.query.get("language", "en-US"), environ=self.environ,
                                                     look_for_keys=self.look_for_keys))

    async def new_profile(self, request):
        """Create a profile from the browser, then edit it."""
        self._authenticated(request)
        body = await request.json()
        allowed = {"name", "workspace", "agent", "language", "provider", "value", "reuse", "voice",
                   "stt_model", "tts_model", "surface", "delivery", "capture_mode", "hermes", "openclaw", "model", "own"}
        if not isinstance(body, dict) or set(body) - allowed or not all(
                isinstance(value, (str, bool)) or (key in _AGENT_DETAIL and _flat(value)) for key, value in body.items()):
            raise web.HTTPBadRequest()
        if self.remote and (body.get("agent") == OWN_AGENT or "own" in body):
            # "My own agent" names a program to run: that is set up at the computer itself.
            raise web.HTTPForbidden(text=ON_HOST_ONLY)
        if body.get("provider") == "local":
            # Local models are 130 to 450 MB: the page polls the job, as when switching to local.
            choice = dict(body)
            body.clear()

            async def make(job: LocalSpeechJob) -> str:
                try:
                    return await create_profile(self.profiles_dir, self.env_path, choice, environ=self.environ,
                                                look_for_keys=self.look_for_keys, local_job=job, lock=self.lock)
                finally:
                    choice.clear()

            def created(slug: str) -> None:
                self.profile_path = self.profiles_dir / f"{slug}.toml"

            job = LocalSpeechJob("create")
            return self._start_local_job(job, job.create(make, created))
        try:
            async with self.lock:
                slug = await create_profile(self.profiles_dir, self.env_path, body, environ=self.environ,
                                            look_for_keys=self.look_for_keys)
        except ValueError as exc:
            return _refusal(exc)
        finally:
            body.clear()
        self.profile_path = self.profiles_dir / f"{slug}.toml"
        return web.json_response({"status": "created", "profile": slug})

    async def agent_options(self, request):
        self._authenticated(request)
        return web.json_response(await asyncio.to_thread(agent_setup_options, self.profile_path,
                                                         environ=self.environ, look_for_keys=self.look_for_keys))

    async def agent_install(self, request):
        """Run an agent's missing installs, after the page showed the exact commands."""
        self._authenticated(request)
        body = await request.json()
        from sleeptalk.setup_core import PRODUCT_AGENTS

        if not isinstance(body, dict) or set(body) != {"agent"} or body["agent"] not in {k for k, _l in PRODUCT_AGENTS}:
            raise web.HTTPBadRequest()
        async with self.lock:
            return web.json_response(await asyncio.to_thread(install_agent_pieces, body["agent"]))

    async def agent_hermes(self, request):
        self._authenticated(request)
        return web.json_response(await hermes_dashboard())

    async def agent_ollama(self, request):
        self._authenticated(request)
        body = await request.json()
        if not isinstance(body, dict) or set(body) - {"base_url"} or not isinstance(body.get("base_url", ""), str):
            raise web.HTTPBadRequest()
        return web.json_response({"models": await ollama_model_names(body.get("base_url", ""))})

    async def agent_models(self, request):
        """The chosen vendor's chat models, for the searchable model field."""
        self._authenticated(request)
        body = await request.json()
        if (not isinstance(body, dict) or set(body) - {"vendor", "key"} or not isinstance(body.get("vendor"), str)
                or not isinstance(body.get("key", ""), str) or len(body.get("key", "")) > 8192):
            raise web.HTTPBadRequest()
        from sleeptalk import model_only_setup as setup

        try:
            vendor = setup.normalize_vendor(body["vendor"])
        except setup.ModelSetupError:
            raise web.HTTPBadRequest() from None
        key = body.get("key", "")
        try:
            if not key and vendor in ("openai", "anthropic"):
                key = await asyncio.to_thread(self._saved_model_key, vendor)
            return web.json_response({"models": await setup.vendor_models(vendor, key)})
        finally:
            key = ""
            body.clear()

    def _saved_model_key(self, vendor: str) -> str:
        """This profile's saved model key, when it is for this vendor; "" otherwise."""
        from sleeptalk import model_only_setup as setup
        from sleeptalk.credential_store import CredentialError, CredentialStore
        from sleeptalk.setup_core import load_existing_answers

        answers = load_existing_answers(self.profile_path.stem, self.profiles_dir) if self.profile_path.is_file() else None
        if answers is None:
            return ""
        if answers.model_credential_ref and setup.normalize_vendor(answers.model_vendor or setup.DEFAULT_VENDOR) == vendor:
            store = CredentialStore()
            if setup.saved_key_usable(store, answers.model_credential_ref, setup.auth_choice(vendor)):
                try:
                    return store.get(answers.model_credential_ref)
                except CredentialError:
                    pass
        return ""

    async def agent_workspace(self, request):
        self._authenticated(request)
        body = await request.json()
        if not isinstance(body, dict) or set(body) != {"workspace"} or not isinstance(body["workspace"], str):
            raise web.HTTPBadRequest()
        try:
            async with self.lock:
                folder = await asyncio.to_thread(change_workspace, self.profile_path, self.profiles_dir, body["workspace"])
        except ValueError as exc:
            return _refusal(exc)
        return web.json_response({"status": "saved", "workspace": folder})

    async def agent_change(self, request):
        """Switch the profile's agent, or reconfigure it, the way the terminal does."""
        self._authenticated(request)
        body = await request.json()
        allowed = {"agent", "workspace", "hermes", "openclaw", "model", "own"}
        if not isinstance(body, dict) or set(body) - allowed or not isinstance(body.get("agent"), str) or not all(
                isinstance(value, str) or (key in _AGENT_DETAIL and _flat(value)) for key, value in body.items()):
            raise web.HTTPBadRequest()
        if self.remote and (body.get("agent") == OWN_AGENT or "own" in body):
            raise web.HTTPForbidden(text=ON_HOST_ONLY)
        try:
            async with self.lock:
                outcome = await change_agent(self.profile_path, self.profiles_dir, self.env_path, body,
                                             environ=self.environ, look_for_keys=self.look_for_keys)
        except ValueError as exc:
            return _refusal(exc)
        finally:
            body.clear()
        return web.json_response({"status": "saved", **outcome})

    async def discord_replace_token(self, request):
        self._authenticated(request)
        body = await request.json()
        if not isinstance(body, dict) or set(body) != {"value"} or not isinstance(body["value"], str):
            raise web.HTTPBadRequest()
        try:
            async with self.lock:
                summary = await replace_discord_token(self.profile_path, body["value"])
        except ValueError as exc:
            return _refusal(exc)
        finally:
            body.clear()
        return web.json_response({"status": "saved", **summary})

    async def call_start(self, request):
        """Start a call on this profile: here, or as a link for another device."""
        self._authenticated(request)
        body = await request.json()
        if (not isinstance(body, dict) or not {"here"} <= set(body) <= {"here", "replace"}
                or not all(isinstance(value, bool) for value in body.values())):
            raise web.HTTPBadRequest()
        try:
            result = await asyncio.to_thread(start_call, self.profile_path, here=body["here"],
                                             replace=body.get("replace", False))
        except ValueError as exc:
            return _refusal(exc)
        return web.json_response({"status": "started", **result})

    async def profile_rename(self, request):
        self._authenticated(request)
        body = await request.json()
        if not isinstance(body, dict) or set(body) != {"name"} or not isinstance(body["name"], str):
            raise web.HTTPBadRequest()
        try:
            async with self.lock:
                self.profile_path = await asyncio.to_thread(rename_profile, self.profile_path, self.profiles_dir, body["name"])
        except ValueError as exc:
            return _refusal(exc)
        return web.json_response({"status": "renamed", "profile": self.profile_path.stem})

    async def profile_delete(self, request):
        """Delete this profile (the page asks first), then edit another or start New profile."""
        self._authenticated(request)
        body = await request.json()
        if not isinstance(body, dict) or body != {"confirm": self.profile_path.stem}:
            raise web.HTTPBadRequest()
        try:
            async with self.lock:
                removed = delete_profile(self.profile_path)
        except ValueError as exc:
            return _refusal(exc)
        remaining = editable_profiles(self.profiles_dir) if self.profiles_dir.is_dir() else []
        self.profile_path = self.profiles_dir / f"{remaining[0] if remaining else 'default'}.toml"
        return web.json_response({"status": "deleted", "profile": remaining[0] if remaining else None,
                                  "keys_removed": len(removed)})

    async def keys_move(self, request):
        """Move every stored key into the OS store; the page asks first."""
        self._authenticated(request)
        body = await request.json()
        if body != {"confirm": True}:
            raise web.HTTPBadRequest()
        from sleeptalk.credential_store import CredentialError, CredentialStore

        try:
            async with self.lock:
                moved = CredentialStore().move_to_keyring()
        except CredentialError as exc:
            return web.json_response({"status": "refused", "code": "detail", "message": str(exc)}, status=422)
        return web.json_response({"status": "moved", "moved": moved})

    async def keys_remove_unused(self, request):
        """Remove the listed keys that no profile uses (checked again now); never revoked upstream."""
        self._authenticated(request)
        body = await request.json()
        refs = body.get("references") if isinstance(body, dict) and set(body) == {"references"} else None
        if not isinstance(refs, list) or not refs or len(refs) > 200 or not all(isinstance(r, str) for r in refs):
            raise web.HTTPBadRequest()
        from sleeptalk.key_inventory import forget_unused

        async with self.lock:
            removed = forget_unused(refs, extra_dirs=[self.profiles_dir])
        return web.json_response({"status": "removed", "removed": len(removed)})

    async def switch_profile(self, request):
        self._authenticated(request)
        body = await request.json()
        slug = body.get("profile") if isinstance(body, dict) else None
        if not isinstance(slug, str) or slug not in editable_profiles(self.profiles_dir):
            raise web.HTTPBadRequest()
        self.profile_path = self.profiles_dir / f"{slug}.toml"
        return web.json_response({"status": "switched", "profile": slug})

    def app(self) -> web.Application:
        @web.middleware
        async def boundary(request, handler):
            if request.headers.get("Host") != self.origin.removeprefix("http://"):
                raise web.HTTPForbidden()
            if request.method != "GET" and request.headers.get("Origin") != self.origin:
                raise web.HTTPForbidden()
            try:
                response = await handler(request)
            except web.HTTPException as exc:
                response = web.Response(status=exc.status, text=exc.text or "", content_type="text/plain")
            response.headers.update({"Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; media-src 'self' blob:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"})
            return response

        static = Path(__file__).parent / "_settings"
        types = {"index.html": "text/html", "settings.js": "application/javascript", "settings.css": "text/css"}

        async def sample(request):
            # Only files the manifest lists; nothing else under the package is reachable.
            name = request.match_info["name"]
            allowed = _sample_files()
            if name not in allowed:
                raise web.HTTPNotFound()
            return web.Response(body=(_SAMPLES / name).read_bytes(), content_type="audio/mpeg")

        async def logo(request):
            name = request.match_info["name"]
            if name not in brand_icons():
                raise web.HTTPNotFound()
            return web.Response(body=(_ICONS / f"{name}.svg").read_bytes(), content_type="image/svg+xml")

        async def strings(request):
            # The page's words in one of the shipped languages; nothing else under the package.
            name = request.match_info["lang"]
            if name not in UI_LANGUAGES:
                raise web.HTTPNotFound()
            return web.Response(body=(static / "i18n" / f"{name}.json").read_bytes(), content_type="application/json")

        async def asset(request):
            name = {"/": "index.html", "/settings.js": "settings.js", "/settings.css": "settings.css"}[request.path]
            return web.Response(body=(static / name).read_bytes(), content_type=types[name])

        app = web.Application(middlewares=[boundary], client_max_size=16384)
        for route in ("/", "/settings.js", "/settings.css"):
            app.router.add_get(route, asset)
        app.router.add_get("/icons/{name:[a-z_]+}.svg", logo)
        app.router.add_get("/i18n/{lang:[a-z]{2}}.json", strings)
        app.router.add_get("/samples/{name:[a-z]+/(?:[a-z]{2}/)?[A-Za-z0-9_.-]+\\.mp3}", sample)
        app.router.add_post("/session", self.exchange)
        app.router.add_get("/api/state", self.state)
        app.router.add_post("/api/set", self.set_value)
        app.router.add_post("/api/keys/lookup", self.key_lookup)
        app.router.add_post("/api/request/key", self.request_key)
        app.router.add_post("/api/request/discord", self.request_discord)
        app.router.add_post("/api/request/finish", self.request_finish)
        app.router.add_post("/api/keys/remove-unused", self.keys_remove_unused)
        app.router.add_post("/api/keys/move", self.keys_move)
        app.router.add_post("/api/speech", self.use_speech)
        app.router.add_post("/api/speech/local", self.local_switch)
        app.router.add_post("/api/speech/local/status", self.local_status)
        app.router.add_post("/api/speech/local/install", self.local_install)
        app.router.add_post("/api/speech/local/voices", self.upload_local_voice)
        app.router.add_post("/api/speech/voices", self.hosted_voices)
        app.router.add_post("/api/speech/voices/clone", self.clone_hosted_voice)
        app.router.add_post("/api/speech/local/voices/preview", self.preview)
        app.router.add_post("/api/preview", self.preview)
        app.router.add_post("/api/discord/token", self.discord_token)
        app.router.add_post("/api/discord/options", self.discord_options)
        app.router.add_post("/api/discord/finish", self.discord_finish)
        app.router.add_post("/api/discord/current", self.discord_current)
        app.router.add_post("/api/discord/lock", self.discord_lock)
        app.router.add_post("/api/discord/status", self.discord_status)
        app.router.add_post("/api/discord/start", self.discord_start)
        app.router.add_post("/api/discord/stop", self.discord_stop)
        app.router.add_post("/api/profile", self.switch_profile)
        app.router.add_get("/api/new", self.new_options)
        app.router.add_post("/api/new", self.new_profile)
        app.router.add_get("/api/agent", self.agent_options)
        app.router.add_post("/api/agent", self.agent_change)
        app.router.add_post("/api/agent/install", self.agent_install)
        app.router.add_post("/api/agent/workspace", self.agent_workspace)
        app.router.add_post("/api/agent/hermes", self.agent_hermes)
        app.router.add_post("/api/agent/ollama", self.agent_ollama)
        app.router.add_post("/api/agent/models", self.agent_models)
        app.router.add_post("/api/discord/replace-token", self.discord_replace_token)
        app.router.add_post("/api/call", self.call_start)
        app.router.add_post("/api/profile/rename", self.profile_rename)
        app.router.add_post("/api/profile/delete", self.profile_delete)
        return app

    async def serve(self, *, open_browser: bool = True, ready=None) -> None:
        runner = web.AppRunner(self.app(), access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = runner.addresses[0][1]
        self.origin = f"http://127.0.0.1:{port}"
        url = f"{self.origin}/#{self.token}"
        if ready is not None:
            ready(url)
        if open_browser:
            webbrowser.open(url)
        try:
            await asyncio.Event().wait()
        finally:
            await runner.cleanup()


def editable_profiles(profiles_dir: Path) -> list[str]:
    """Profiles a person edits: never the shipped template."""
    from sleeptalk.launcher import TEMPLATE_PROFILE
    from sleeptalk.setup_core import existing_profiles

    return [slug for slug in existing_profiles(profiles_dir) if slug != TEMPLATE_PROFILE]


_PAGE_FOOTER = ("Open it in any browser on this computer; it stays open until you press Ctrl-C.\n"
                "Terminal setup: sleeptalk settings --terminal. A terminal call: sleeptalk --terminal.")


def run(*, profile: str = "", open_browser: bool = True) -> int:
    """`sleeptalk settings --open`: serve the page for the workspace's profile and open it.

    With no profile named, it opens the one this folder last launched, then
    ``default``, then the first; the template profile is never offered.
    """
    from sleeptalk.home import resolve_layout
    from sleeptalk.launcher import selected_profile

    layout = resolve_layout()
    profiles = editable_profiles(layout.profiles_dir) if layout.profiles_dir.is_dir() else []
    if not profiles and not profile:
        # First run: the page opens on New profile.
        page = SettingsPage(layout.profiles_dir / "default.toml", profiles_dir=layout.profiles_dir, env_path=layout.env_path)

        def first(url: str) -> None:
            print(f"Set up Sleeptalk: {url}")
            print(_PAGE_FOOTER)

        try:
            asyncio.run(page.serve(open_browser=open_browser, ready=first))
        except KeyboardInterrupt:
            pass
        return 0
    remembered = selected_profile(Path.cwd().resolve())
    last = remembered.stem if remembered is not None and remembered.parent == layout.profiles_dir and remembered.stem in profiles else ""
    slug = profile or last or ("default" if "default" in profiles else profiles[0])
    if slug not in profiles:
        print(f"No profile named {slug!r}. Profiles here: {', '.join(profiles)}")
        return 2
    page = SettingsPage(layout.profiles_dir / f"{slug}.toml", profiles_dir=layout.profiles_dir, env_path=layout.env_path)

    def announce(url: str) -> None:
        print(f"Settings for {slug}: {url}")
        print(_PAGE_FOOTER)

    try:
        asyncio.run(page.serve(open_browser=open_browser, ready=announce))
    except KeyboardInterrupt:
        pass
    return 0
