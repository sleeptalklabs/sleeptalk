"""Shared setup answers, profile persistence, and credential validation.

Browser settings, agent commands, and the terminal interview use the same
operations. This module never imports the terminal wizard or prompts a user.
"""

from __future__ import annotations

import json
import locale
import os
import re
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from sleeptalk import discord_check, language_support
from sleeptalk.defaults import register_defaults
from sleeptalk.registries import AGENTS


PROFILES_DIR = Path("config/profiles")  # legacy default; layout-aware paths win
ENV_PATH = Path(".env")


COMMON_LANGUAGES = [
    ("en-US", "English (US)"),
    ("en-GB", "English (UK)"),
    ("es-ES", "Spanish (Spain)"),
    ("es-MX", "Spanish (Mexico)"),
    ("fr-FR", "French"),
    ("de-DE", "German"),
    ("pt-BR", "Portuguese (Brazil)"),
    ("it-IT", "Italian"),
    ("nl-NL", "Dutch"),
    ("pl-PL", "Polish"),
    ("hi-IN", "Hindi"),
    ("ur-PK", "Urdu"),
    ("ja-JP", "Japanese"),
    ("ko-KR", "Korean"),
    ("zh-CN", "Chinese (Mandarin, Simplified)"),
]

# Sample line spoken during voice auditions, per primary subtag.
SAMPLE_LINES = {
    "en": "Hi! This is what I'll sound like.",
    "es": "¡Hola! Así es como sonaré.",
    "fr": "Salut ! Voici ma voix.",
    "de": "Hallo! So werde ich klingen.",
    "pt": "Olá! É assim que eu vou soar.",
    "it": "Ciao! Ecco come suonerò.",
    "nl": "Hoi! Zo zal ik klinken.",
    "pl": "Cześć! Tak będę brzmieć.",
    "hi": "नमस्ते! मैं ऐसी आवाज़ में बोलूँगा।",
    "ur": "السلام علیکم! میری آواز ایسی ہوگی۔",
    "ja": "こんにちは！私の声はこんな感じです。",
    "ko": "안녕하세요! 제 목소리는 이렇습니다.",
    "zh": "你好！这就是我的声音。",
}

_BCP47 = re.compile(r"^[a-zA-Z]{2,3}(-[a-zA-Z0-9]{2,8})*$")


# ---------------------------------------------------------------------------
# Answers + pure output builders (testable without a terminal)
# ---------------------------------------------------------------------------


SurfaceName = Literal["discord", "web", "terminal"]
SURFACES: tuple[SurfaceName, ...] = ("discord", "web", "terminal")
KNOWN_SURFACES: tuple[str, ...] = (*SURFACES, "null")
SURFACE_TRANSPORT: dict[str, str] = {
    "discord": "discord",
    "web": "web",
    "terminal": "terminal",
}
from sleeptalk.speech.openai_audio import PRESETS as _AUDIO_PRESETS

TTS_WIZARD_PROVIDERS = ("cartesia", "deepgram", "elevenlabs", *_AUDIO_PRESETS)
STT_WIZARD_PROVIDERS = ("deepgram", "cartesia", "elevenlabs", *_AUDIO_PRESETS, "local")
STT_DEFAULT_MODELS = {"deepgram": "nova-3", "cartesia": "ink-2", "elevenlabs": "scribe_v2", "local": "moonshine-small-streaming-en",
                      **{name: preset.stt_model for name, preset in _AUDIO_PRESETS.items()}}
# The same key serves both directions on each of these providers.
_STT_KEY_ENV = {"deepgram": "DEEPGRAM_API_KEY", "cartesia": "CARTESIA_API_KEY", "elevenlabs": "ELEVENLABS_API_KEY",
                **{name: preset.env for name, preset in _AUDIO_PRESETS.items()}}
TTS_MODES = ("explore", "known")
ROUTE_TYPES = ("cascade", "hybrid")
DEFAULT_REALTIME_MODEL = "gpt-realtime-2.1"
DEFAULT_REALTIME_VOICE = "marin"
# The credential-store provider an OpenAI Realtime key is kept under.
REALTIME_PROVIDER = "openai"
DEFAULT_SPEECH_MODEL = "gpt-4o-mini-tts"
DEFAULT_TRANSCRIPTION_MODEL = "gpt-4o-mini-transcribe"
# Voice from https://docs.cartesia.ai/api-reference/tts/websocket.
from sleeptalk.voice_catalog import default_voice_id as _default_voice_id

DEFAULT_CARTESIA_VOICE = _default_voice_id("cartesia")
# Deepgram Flux voice model (https://developers.deepgram.com/docs/flux-tts/voices).
DEFAULT_DEEPGRAM_VOICE = _default_voice_id("deepgram")
# One entry per product. Sleeptalk picks the connection: the richer gateway or
# SDK when it works, the agent's ACP entry point otherwise. `runtime` in a
# profile still pins one for advanced use.
# oh-my-pi comes first: it's the default (pi has no web search), picked
# when it's ready or when no agent is ready at all.
PRODUCT_AGENTS = (
    ("omp", "oh-my-pi (default)"),
    ("pi", "Pi"),
    ("hermes", "Hermes"),
    ("claude_code", "Claude Code"),
    ("codex", "Codex"),
    ("cursor", "Cursor"),
    ("openclaw", "OpenClaw (preview)"),
    ("model", "Just a model — no agent, no tools; one OpenRouter, OpenAI, or Anthropic key"),
)
AGENT_LABELS = dict(PRODUCT_AGENTS)
# Connection-specific registrations reached through a product entry.
CONNECTION_REGISTRATIONS = frozenset({"acp", "hermes_gateway", "cursor_acp", "openclaw_acp"})


@dataclass
class SetupAnswers:
    surface: str = "web"
    # web only: "tailnet" publishes calls on the host's tailnet (see docs/tailnet-calls.md)
    delivery: str = ""
    # terminal only: "headphones" or "speakers" (see TransportConfig.capture_mode)
    capture_mode: str = ""
    # narrated | silence | keyboard (see PolicyConfig.narration_style)
    narration_style: str = "silence"
    # full | summarized (see PolicyConfig.reply_length)
    reply_length: str = "concise"
    instant_ack_mode: str = "delayed"
    # relaxed | balanced | aggressive (see PolicyConfig.endpointing); "" leaves it unwritten
    endpointing: str = ""
    route_type: str = "cascade"
    agent_type: str = "pi"
    workspace_dir: str = "."
    # Bring-your-own agents (command/http/bridge): [agent.options], the ACP
    # permission mode, and the bearer token stored under options.token_env.
    agent_options: dict = field(default_factory=dict)
    agent_permission_mode: str = ""
    agent_token: str = ""
    # OpenClaw: the session a call joins ("agent:main:main" continues the main
    # conversation; empty starts fresh) and the gateway token stored in .env.
    gateway_session_id: str = ""
    openclaw_token: str = ""
    gateway_url: str = ""
    gateway_api_key: str = ""
    gateway_session_token: str = ""
    # Model only (type = "model"): the vendor preset (openrouter | openai |
    # anthropic | other), the pasted base URL for "other", the model name, the
    # key (stored privately, never written to a profile or .env), and the
    # credential reference the profile's [agent.auth] carries.
    model_vendor: str = ""
    model_base_url: str = ""
    model: str = ""
    model_key: str = ""
    model_credential_ref: str = ""
    name: str = "sleeptalk"
    slug: str = "sleeptalk"
    guild_id: str = ""
    client_id: str = ""
    # Who may talk to the bot. False (written explicitly) means anyone in the server.
    authorization_required: bool = True
    operator_ids: str = ""
    allowed_channel_ids: str = ""
    # Empty = derive <SLUG>_DISCORD_BOT_TOKEN (repo convention: one env var
    # per bot, so a new profile never clobbers another bot's token in .env).
    token_env: str = ""
    token: str = ""
    language: str = "en-US"
    realtime_key: str = ""
    realtime_model: str = DEFAULT_REALTIME_MODEL
    realtime_voice: str = DEFAULT_REALTIME_VOICE
    speech_model: str = DEFAULT_SPEECH_MODEL
    transcription_model: str = DEFAULT_TRANSCRIPTION_MODEL
    # One provider for both directions (deepgram | cartesia) and its key. When
    # set, they populate the stt_*/tts_* fields below; leave empty to mix.
    # (Answers files that omit tts_provider keep the historical Cartesia voice.)
    speech_provider: str = ""
    speech_key: str = ""
    stt_provider: str = "deepgram"
    stt_model: str = "nova-3"
    stt_key: str = ""
    tts_provider: str = "cartesia"
    tts_model: str = ""
    tts_voice_id: str = ""
    tts_key: str = ""
    tts_user_id: str = ""  # PlayHT's second credential; stored only in .env
    tts_mode: str = ""
    tts_speed: float = 1.0
    # Per-turn latency traces, shared by every surface (see LogConfig.latency).
    latency_log: bool = False


def default_language() -> str:
    """System locale as BCP-47, falling back to en-US."""
    try:
        lang = locale.getlocale()[0] or ""
    except ValueError:
        lang = ""
    tag = lang.replace("_", "-").strip()
    return tag if _BCP47.match(tag) else "en-US"


def sample_line(language: str) -> str:
    primary = language.strip().lower().replace("_", "-").split("-", 1)[0]
    return SAMPLE_LINES.get(primary, SAMPLE_LINES["en"])


def _primary_language(language: str) -> str:
    return (language or "").strip().lower().replace("_", "-").split("-", 1)[0]


def is_english(language: str) -> bool:
    return _primary_language(language) in ("en", "")


def language_label(language: str) -> str:
    """"French" for "fr-FR"; the tag itself when the list has no name for it."""
    tag = (language or "").strip().replace("_", "-")
    names = dict(COMMON_LANGUAGES)
    if tag in names:
        return names[tag]
    primary = _primary_language(tag)
    for known, label in COMMON_LANGUAGES:
        if _primary_language(known) == primary:
            return label.split(" (", 1)[0]
    return tag


def language_notes(answers: SetupAnswers) -> list[str]:
    """What this language gets end to end, in the words the wizard prints.

    Capability warnings come from language_support; this adds the one line
    the tables decide: whether the first thing spoken is in the language.
    """
    if answers.route_type != "cascade" or is_english(answers.language):
        return []
    from sleeptalk import language_support
    from sleeptalk.spoken_lines import for_language

    label = language_label(answers.language)
    notes: list[str] = []
    if for_language(answers.language) is None:
        notes.append(
            f"{label}: no spoken-line table yet, so acks and tool narration stay in English "
            f"(tables exist for {', '.join(language_support_tables())})."
        )
    else:
        notes.append(f"{label}: acks and tool narration will be spoken in {label}; instant ack runs blind.")
    stt_model = language_support.resolve_stt_model(answers.stt_provider, answers.language, answers.stt_model)
    if stt_model != answers.stt_model:
        notes.append(f"{label}: {STT_PROVIDER_LABELS.get(answers.stt_provider, answers.stt_provider)} will use {stt_model} at call time.")
    return notes


def language_support_tables() -> list[str]:
    from sleeptalk.spoken_lines import available

    return [language_label(tag) for tag in available()]


def refresh_voice_for_language(answers: SetupAnswers, previous_language: str) -> None:
    """A language change moves the voice: back to the provider default for
    English, or to empty so the language-ranked pick chooses."""
    if _primary_language(previous_language) == _primary_language(answers.language):
        return
    defaults = set(DEFAULT_VOICES.values())
    if is_english(answers.language):
        if not answers.tts_voice_id or answers.tts_voice_id not in defaults:
            answers.tts_voice_id = DEFAULT_VOICES.get(answers.tts_provider, "")
    elif answers.tts_voice_id in defaults:
        answers.tts_voice_id = ""


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug or "sleeptalk"


_PROFILE_SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")


def profile_name_slug(name: str) -> str:
    """The profile (and Discord command) a new name makes, or "" when it can't
    be one. The settings page and the terminal both name profiles by this."""
    from sleeptalk.launcher import TEMPLATE_PROFILE

    name = str(name or "").strip()
    # A name with no Latin letters or digits has no slug of its own.
    slug = slugify(name) if re.search(r"[a-z0-9]", name.lower()) else ""
    if not name or len(name) > 64 or not _PROFILE_SLUG.fullmatch(slug) or slug == Path(str(TEMPLATE_PROFILE)).stem:
        return ""
    return slug


def detect_agents() -> dict[str, str]:
    """Registered agents whose declared binary is on PATH: name -> binary."""
    from sleeptalk.agents.readiness import probe

    found: dict[str, str] = {}
    register_defaults()
    for name in AGENTS.names():
        report = probe(name)
        for need in report.needs:
            if need.kind == "binary" and need.found:
                found[name] = need.key
                break
    return found


def parse_surface(raw: str) -> str:
    key = (raw or "discord").strip().lower()
    if key == "browser":
        key = "web"
    if key not in KNOWN_SURFACES:
        raise ValueError(f"unknown surface {raw!r} — valid: {', '.join(SURFACES)}")
    return key


def parse_route_type(raw: str) -> str:
    key = (raw or "cascade").strip().lower()
    if key not in ROUTE_TYPES:
        raise ValueError(f"unknown route_type {raw!r} — valid: {', '.join(ROUTE_TYPES)}")
    return key


def voice_engine_choices() -> tuple[tuple[str, str], ...]:
    """The stable route choices shown by the interactive setup step."""
    return (
        ("cascade", "Speech service: your agent answers, a speech service listens and speaks"),
        ("hybrid", "OpenAI Realtime: hears you and talks back itself"),
    )


def transport_for_surface(surface: str) -> str:
    key = parse_surface(surface)
    if key == "null":
        return "null"
    return SURFACE_TRANSPORT[key]


def surface_for_transport(transport_type: str) -> str:
    key = (transport_type or "").strip().lower()
    if key == "browser":
        return "web"
    if key in KNOWN_SURFACES:
        return key
    return "discord"


def parse_tts_mode(raw: str) -> str:
    key = (raw or "").strip().lower()
    if key not in TTS_MODES:
        raise ValueError(f"unknown tts_mode {raw!r} — valid: {', '.join(TTS_MODES)}")
    return key


def launch_command(answers: SetupAnswers, profile_path: Path | str) -> str:
    if parse_surface(answers.surface) == "web":
        return f"sleeptalk link --config {profile_path} --foreground"
    return f"sleeptalk run --config {profile_path}"


def _accepted_url(raw: object) -> str:
    if not isinstance(raw, str):
        return ""
    return raw.strip()


def _url_from_file(path: Path | None) -> str:
    if path is None or not path.is_file():
        return ""
    from dotenv import dotenv_values
    from sleeptalk.call_provider import RENDEZVOUS_URL_ENV

    return _accepted_url(dotenv_values(path).get(RENDEZVOUS_URL_ENV) or "")


def resolve_rendezvous_url(
    environ: Mapping[str, str],
    home_env: Path | None,
) -> str:
    from sleeptalk.call_provider import DEFAULT_RENDEZVOUS_URL, RENDEZVOUS_URL_ENV

    url = _accepted_url(environ.get(RENDEZVOUS_URL_ENV, ""))
    if url:
        return url
    url = _url_from_file(home_env)
    if url:
        return url
    return DEFAULT_RENDEZVOUS_URL


def resolved_rendezvous_url(environ: Mapping[str, str] | None = None) -> str:
    from sleeptalk.home import resolve_layout

    env = dict(os.environ if environ is None else environ)
    layout = resolve_layout(env=env)
    return resolve_rendezvous_url(env, layout.env_path)


def required_answer_fields(answers: SetupAnswers) -> tuple[str, ...]:
    if parse_route_type(answers.route_type) == "hybrid":
        shared = ("name", "realtime_key")
    else:
        shared = ("name",
                  *(() if answers.stt_provider == "local" else ("stt_key",)),
                  *(() if answers.tts_provider == "local" else ("tts_key",)))
    if answers.agent_type == "model":
        from sleeptalk.model_only_setup import key_required

        needs_key = not answers.model_credential_ref and key_required(answers.model_vendor, answers.model_base_url)
        shared = (*shared, "model", *(("model_key",) if needs_key else ()))
        if answers.model_vendor.strip().lower() == "other":
            shared = (*shared, "model_base_url")
    if parse_surface(answers.surface) == "discord":
        return (*shared, "guild_id", "client_id", "token")
    return shared


def system_dep_names(surface: str) -> tuple[str, ...]:
    if parse_surface(surface) == "discord":
        return ("ffmpeg", "libopus", "davey")
    return ()


def missing_runtime_extra(surface: str) -> str | None:
    from sleeptalk import sysdeps

    kind = parse_surface(surface)
    if kind == "terminal":
        return sysdeps.missing_terminal_extra()
    if kind == "discord":
        return sysdeps.missing_discord_extra()
    if kind == "web":
        return sysdeps.missing_web_extra()
    return None


def ensure_runtime_extra(surface: str) -> str | None:
    from sleeptalk import sysdeps

    kind = parse_surface(surface)
    if kind == "terminal":
        return sysdeps.install_terminal_extra()
    if kind == "discord":
        return sysdeps.install_discord_extra()
    if kind == "web":
        return sysdeps.install_web_extra()
    return None


def can_auto_install(cmd: list[str] | None) -> bool:
    return bool(cmd) and cmd[0] != "sudo"


BYO_AGENT_TYPES = ("command", "http", "bridge")
OPENCLAW_TOKEN_ENV = "OPENCLAW_GATEWAY_TOKEN"
OPENCLAW_MAIN_SESSION = "agent:main:main"
OPENCLAW_CONFIG = Path("~/.openclaw/openclaw.json")


def read_openclaw_gateway_token(path: Path = OPENCLAW_CONFIG) -> str:
    """The local OpenClaw gateway token, or "" (`openclaw config get` redacts it)."""
    try:
        config = json.loads(path.expanduser().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    gateway = config.get("gateway") if isinstance(config, dict) else None
    auth = gateway.get("auth") if isinstance(gateway, dict) else None
    token = auth.get("token") if isinstance(auth, dict) else None
    return token.strip() if isinstance(token, str) else ""
OWN_AGENT_CHOICE = "__own__"
OWN_AGENT_KINDS = (
    ("command", "A program on this machine (reads and prints JSON lines)"),
    ("acp", "An agent that already speaks ACP (Gemini CLI, Goose, Kiro, ...)"),
    ("http", "A web service I already run"),
    ("bridge", "A remote agent over Agent Bridge v1"),
    ("starter", "I haven't built it yet: create a starter agent"),
)


def own_agent_settings(
    kind: str,
    *,
    command: str = "",
    env_names: str = "",
    url: str = "",
    token_env: str = "",
) -> tuple[str, dict]:
    """Turn "My own agent" answers into (agent type, validated options).

    Raises ValueError with a message fit to show the person answering.
    """
    import shlex

    register_defaults()
    names = [name.strip() for name in re.split(r"[,\s]+", env_names) if name.strip()]
    if kind in {"command", "acp"}:
        try:
            argv = shlex.split(command)
        except ValueError as exc:
            raise ValueError(f"could not read that command: {exc}") from None
        if not argv:
            raise ValueError("enter the command that starts your agent, e.g. node agent.mjs")
        agent_type = "command"
        options: dict = {"command": argv}
        if kind == "acp":
            options["protocol"] = "acp"
            # Existing ACP CLIs read their own login and provider settings.
            options["inherit_env"] = True
        if names:
            options["env"] = names
    elif kind in {"http", "bridge"}:
        agent_type = kind
        options = {"url": url.strip()}
        if token_env.strip():
            options["token_env"] = token_env.strip()
    else:
        raise ValueError(f"unknown agent kind {kind!r}")
    try:
        validated = AGENTS.get(agent_type).validate_options(options)
    except ValueError as exc:
        raise ValueError(_first_validation_message(exc)) from None
    return agent_type, {key: value for key, value in validated.items() if key in options}


def _first_validation_message(exc: Exception) -> str:
    errors = getattr(exc, "errors", None)
    if callable(errors):
        for error in errors():
            where = ".".join(str(part) for part in error.get("loc", ()))
            message = str(error.get("msg", "")).removeprefix("Value error, ")
            return f"{where}: {message}" if where else message
    return str(exc)


def _toml_key(key: str) -> str:
    """Bare when TOML allows it, quoted otherwise ("ack:empathy")."""
    return key if re.fullmatch(r"[A-Za-z0-9_-]+", key) else json.dumps(key)


def _toml_string(value: object) -> str:
    """A TOML basic string: quotes, backslashes, and line breaks escaped, so a
    value can never end its line and start a setting of its own."""
    return json.dumps(str(value), ensure_ascii=False)


def _toml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{_toml_key(str(key))} = {_toml_value(item)}" for key, item in value.items()) + " }"
    return json.dumps(str(value))


def agent_menu_entries(detected: dict[str, str] | None = None, *, ready_first: bool = False) -> list[tuple[str, str]]:
    from sleeptalk.agents.readiness import AgentReadiness, probe

    register_defaults()
    hidden = CONNECTION_REGISTRATIONS | set(BYO_AGENT_TYPES)
    known = {name for name, _label in PRODUCT_AGENTS}
    names = [name for name, _label in PRODUCT_AGENTS]
    # Installed extension packages still appear, after the built-in products.
    names += [name for name in AGENTS.names() if name not in known and name not in hidden]
    entries: list[tuple[str, str]] = []
    ready_names: set[str] = set()
    for name in names:
        label = AGENT_LABELS.get(name, name)
        if detected is not None:
            found = detected.get(name) or (detected.get("acp") if name == "hermes" else None)
            suffix = f"detected on PATH ({found})" if found else "not installed"
            if found:
                ready_names.add(name)
        else:
            report: AgentReadiness = probe(name)
            suffix = report.menu_suffix()
            if report.ready:
                ready_names.add(name)
        entries.append((f"{label} — {suffix}", name))
    # "Just a model" is always ready; it doesn't jump ahead of the default
    # oh-my-pi on a machine with no agent installed.
    ready_names.discard("model")
    return sorted(entries, key=lambda entry: entry[1] not in ready_names) if ready_first else entries

def resolved_token_env(answers: SetupAnswers) -> str:
    if answers.token_env.strip():
        return answers.token_env.strip()
    return f"{answers.slug.upper().replace('-', '_')}_DISCORD_BOT_TOKEN"


def _tts_model_line(answers: SetupAnswers) -> str:
    from sleeptalk.builder import tts_model_for_provider

    return tts_model_for_provider(answers.tts_provider, answers.tts_model)


def profile_toml(answers: SetupAnswers) -> str:
    route_type = parse_route_type(answers.route_type)
    lines = [
        f'[agent]',
        f'type = {_toml_string(answers.agent_type)}',
        f'name = {_toml_string(answers.name)}',
        f'slug = {_toml_string(answers.slug)}',
        f'workspace_dir = {json.dumps(answers.workspace_dir)}',
    ]
    if answers.gateway_url:
        from sleeptalk.hermes_connect import parse_gateway_target

        lines.append(
            f'gateway_url = {_toml_string(parse_gateway_target(answers.gateway_url).ws_url)}'
        )
    if answers.gateway_session_id:
        lines.append(f"gateway_session_id = {_toml_value(answers.gateway_session_id)}")
    if answers.agent_permission_mode:
        lines.append(f"permission_mode = {_toml_value(answers.agent_permission_mode)}")
    if answers.agent_type == "model":
        from sleeptalk.model_only_setup import auth_choice

        if answers.model:
            lines.append(f"model = {_toml_value(answers.model)}")
        if answers.model_credential_ref:
            choice = auth_choice(answers.model_vendor, answers.model_base_url)
            lines += [
                "",
                "[agent.auth]",
                f"mode = {_toml_value(choice.mode)}",
                f"protocol = {_toml_value(choice.protocol)}",
                f"base_url = {_toml_value(choice.base_url)}",
                f"credential_ref = {_toml_value(answers.model_credential_ref)}",
            ]
    if answers.agent_options:
        lines += ["", "[agent.options]"]
        lines += [f"{_toml_key(key)} = {_toml_value(value)}" for key, value in answers.agent_options.items()]
    if route_type == "hybrid":
        lines += [
            "",
            "[route]",
            'type = "hybrid"',
            'provider = "openai-realtime"',
            f'model = {_toml_string(answers.realtime_model or DEFAULT_REALTIME_MODEL)}',
            f'voice = {_toml_string(answers.realtime_voice or DEFAULT_REALTIME_VOICE)}',
            f'speech_model = {_toml_string(answers.speech_model or DEFAULT_SPEECH_MODEL)}',
            f'transcription_model = {_toml_string(answers.transcription_model or DEFAULT_TRANSCRIPTION_MODEL)}',
            f'language = {_toml_string(answers.language)}',
        ]
    else:
        lines += ["", "[route]", 'type = "cascade"']
    transport_type = transport_for_surface(answers.surface)
    lines += ["", "[transport]", f'type = {_toml_string(transport_type)}']
    if transport_type == "discord":
        lines += [
            f'guild_id = {_toml_string(answers.guild_id)}',
            f'client_id = {_toml_string(answers.client_id)}',
            f'token_env = {_toml_string(resolved_token_env(answers))}',
        ]
        if answers.authorization_required:
            lines += [
                "authorization_required = true",
                f'operator_ids = {_toml_string(answers.operator_ids)}',
                f'allowed_channel_ids = {_toml_string(answers.allowed_channel_ids)}',
            ]
        else:
            lines += ["authorization_required = false"]
    if transport_type == "terminal" and answers.capture_mode:
        lines += [f'capture_mode = {_toml_string(answers.capture_mode)}']
    if transport_type == "web" and answers.delivery in ("tailnet", "local"):
        lines += ["", "[transport.options]", f"delivery = {_toml_string(answers.delivery)}"]
    policy_lines = [
        f'{_toml_key(key)} = {_toml_string(value)}'
        for key, value in (
            ("narration_style", answers.narration_style),
            ("reply_length", answers.reply_length),
            ("instant_ack_mode", answers.instant_ack_mode),
            ("endpointing", answers.endpointing),
        )
        if value
    ]
    spoken = None
    if route_type == "cascade" and not is_english(answers.language):
        from sleeptalk.spoken_lines import for_language

        spoken = for_language(answers.language)
    if spoken is not None and answers.instant_ack_mode != "delayed":
        # The smart ack classifies the transcript with English regexes; for
        # another language it would spend its budget matching nothing. Blind
        # plays the localized "ack" pool at commit and is the honest ack.
        policy_lines = [line for line in policy_lines if not line.startswith("instant_ack_mode =")]
        policy_lines.append('instant_ack_mode = "blind"')
    if policy_lines:
        # One header for the whole section: a second [policy] table would be
        # a duplicate-key error the moment both settings are answered.
        lines += ["", "[policy]", *policy_lines]
    if spoken is not None:
        # Cascade reads these the way it reads any override, wholesale: every
        # key is present (tests/test_spoken_lines.py). Edit a line here to
        # change what the assistant says first.
        for table, values in spoken.as_policy_overrides().items():
            lines += ["", f"[policy.{table}]"]
            lines += [f"{_toml_key(key)} = {_toml_value(value)}" for key, value in values.items()]
    if route_type == "cascade":
        lines += [
            "",
            "[stt]",
            f'provider = {_toml_string(answers.stt_provider)}',
            f'model = {_toml_string(answers.stt_model)}',
            f'language = {_toml_string(answers.language)}',
            "",
            "[tts]",
            f'provider = {_toml_string(answers.tts_provider)}',
            *([f'model = {_toml_string(_tts_model_line(answers))}'] if _tts_model_line(answers) else []),
            f'voice_id = {_toml_string(answers.tts_voice_id)}',
            f'language = {_toml_string(answers.language)}',
            f"speed = {answers.tts_speed}",
            "",
        ]
    else:
        lines += ["", "[tts]", f"speed = {answers.tts_speed}", ""]
    # Written even when it matches the default: a knob nobody can see in the
    # file they own is a knob nobody turns off.
    lines += ["", "[log]", f"latency = {str(answers.latency_log).lower()}", ""]
    return "\n".join(lines)


def env_entries(answers: SetupAnswers) -> dict[str, str]:
    route_type = parse_route_type(answers.route_type)
    entries: dict[str, str] = {}
    if parse_surface(answers.surface) == "discord":
        entries[resolved_token_env(answers)] = answers.token
    if route_type == "hybrid":
        entries["OPENAI_API_KEY"] = answers.realtime_key
    else:
        stt_env = _STT_KEY_ENV.get(answers.stt_provider)
        if stt_env:
            entries[stt_env] = answers.stt_key
        if answers.tts_provider == "cartesia":
            entries["CARTESIA_API_KEY"] = answers.tts_key
        elif answers.tts_provider == "deepgram":
            entries["DEEPGRAM_API_KEY"] = answers.tts_key
        elif answers.tts_provider == "elevenlabs":
            entries["ELEVENLABS_API_KEY"] = answers.tts_key
        elif answers.tts_provider == "playht":
            entries["PLAYHT_API_KEY"] = answers.tts_key
            entries["PLAYHT_USER_ID"] = answers.tts_user_id
        elif answers.tts_provider == "rime":
            entries["RIME_API_KEY"] = answers.tts_key
        elif answers.tts_provider in _AUDIO_PRESETS:
            entries[_AUDIO_PRESETS[answers.tts_provider].env] = answers.tts_key
        elif answers.tts_provider == "hume":
            entries["HUME_API_KEY"] = answers.tts_key
    if answers.agent_type == "hermes_gateway":
        from sleeptalk.hermes_connect import SESSION_TOKEN_ENV, parse_gateway_target

        token = (
            answers.gateway_session_token.strip()
            or parse_gateway_target(answers.gateway_url).token
        )
        if token:
            entries[SESSION_TOKEN_ENV] = token
        if answers.gateway_api_key:
            entries["HERMES_GATEWAY_API_KEY"] = answers.gateway_api_key
    token_env = str(answers.agent_options.get("token_env") or "")
    if answers.agent_type in BYO_AGENT_TYPES and token_env and answers.agent_token:
        entries[token_env] = answers.agent_token
    if answers.agent_type == "openclaw" and answers.openclaw_token:
        entries[OPENCLAW_TOKEN_ENV] = answers.openclaw_token
    return {key: value for key, value in entries.items() if value}


_TTS_KEY_ENV = {
    "cartesia": "CARTESIA_API_KEY",
    "deepgram": "DEEPGRAM_API_KEY",
    "elevenlabs": "ELEVENLABS_API_KEY",
    "playht": "PLAYHT_API_KEY",
    "rime": "RIME_API_KEY",
    "hume": "HUME_API_KEY",
    **{name: preset.env for name, preset in _AUDIO_PRESETS.items()},
}

STT_PROVIDER_LABELS = {"deepgram": "Deepgram", "cartesia": "Cartesia", "elevenlabs": "ElevenLabs", "local": "Local",
                       **{name: preset.label for name, preset in _AUDIO_PRESETS.items()}}
TTS_PROVIDER_LABELS = {"deepgram": "Deepgram", "cartesia": "Cartesia", "elevenlabs": "ElevenLabs", "local": "Local",
                       **{name: preset.label for name, preset in _AUDIO_PRESETS.items()}}
STT_KEY_PAGES = {
    "deepgram": "[link]https://console.deepgram.com[/] -> API Keys",
    "cartesia": "[link]https://play.cartesia.ai[/] -> API Keys",
    "elevenlabs": "[link]https://elevenlabs.io[/] -> Developers -> API Keys",
    **{name: f"[link]{preset.console}[/]" for name, preset in _AUDIO_PRESETS.items()},
}
# Degraded language tiers fall back to the provider's wider model, honestly explained.
_STT_DEGRADED_MODELS = {
    ("deepgram", "nova-3"): "nova-2",
    ("cartesia", "ink-2"): "ink-whisper",
}


# ElevenLabs "Rachel", the premade voice the connectivity check already uses.
DEFAULT_ELEVENLABS_VOICE = _default_voice_id("elevenlabs")
# Kokoro's Heart; the other Kokoro voices and Piper's Lessac are offered in the voice step.
DEFAULT_LOCAL_VOICE = "af_heart"
DEFAULT_VOICES = {"cartesia": DEFAULT_CARTESIA_VOICE, "deepgram": DEFAULT_DEEPGRAM_VOICE,
                  "elevenlabs": DEFAULT_ELEVENLABS_VOICE, "local": DEFAULT_LOCAL_VOICE,
                  **{name: preset.tts_voice for name, preset in _AUDIO_PRESETS.items()}}


# Every speech service, in the order both the settings page and this menu show them.
SPEECH_SERVICES = ("cartesia", "deepgram", "elevenlabs", "openrouter", "venice", "groq", "together", "local")


def speech_menu_order() -> tuple[str, ...]:
    """The speech menu after any found keys: Local first, then the key services."""
    return ("local", *(name for name in SPEECH_SERVICES if name != "local"))


def speech_service_facts() -> dict[str, dict[str, Any]]:
    """providers.json by id: the free-tier and card facts the key pages show."""
    try:
        path = Path(__file__).parent / "_setup" / "providers.json"
        return {item["id"]: item for item in json.loads(path.read_text(encoding="utf-8"))["providers"]}
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def speech_service_label(provider: str, facts: dict[str, dict[str, Any]]) -> str:
    fact = facts.get(provider, {})
    name = fact.get("name") or STT_PROVIDER_LABELS.get(provider, provider)
    tagline = str(fact.get("tagline") or "").rstrip(".")
    return f"{name} — {tagline}" if tagline else name


def found_speech_keys() -> dict[str, Any]:
    """Keys already on this machine that listen and speak, by provider (sources only)."""
    try:
        from sleeptalk.key_discovery import discover

        return {key.provider: key for key in discover()}
    except Exception:  # noqa: BLE001 — a courtesy, never a blocker
        return {}


def found_key_choices(found: dict[str, Any], choice) -> list:
    """Menu entries offering each found key first: nothing to sign up for or paste."""
    from sleeptalk.speech.openai_audio import PRESETS

    entries = []
    for provider, key in found.items():
        preset = PRESETS[provider]
        entries.append(choice(
            f"{preset.label} — use your key from {key.source} (listens with {preset.stt_model}, "
            f"speaks with {preset.tts_voice}); nothing to paste, a copy goes into Sleeptalk's store",
            value=f"found:{provider}",
        ))
    return entries


def speech_provider_of(answers: SetupAnswers) -> str:
    """The single provider serving both directions, or "" when they are mixed."""
    if answers.stt_provider == answers.tts_provider and answers.stt_provider in STT_WIZARD_PROVIDERS:
        return answers.stt_provider
    return ""


def apply_speech_provider(answers: SetupAnswers, provider: str) -> list[str]:
    """Point both directions at one provider; returns any language notes."""
    provider = provider.strip().lower()
    if provider not in STT_WIZARD_PROVIDERS:
        raise ValueError(f"speech_provider must be one of {', '.join(STT_WIZARD_PROVIDERS)}")
    if answers.stt_provider != provider:
        answers.stt_provider = provider
        answers.stt_model = STT_DEFAULT_MODELS[provider]
    if answers.tts_provider != provider or not answers.tts_voice_id.strip():
        answers.tts_provider = provider
        # The provider default is an English voice. Any other language
        # leaves the voice empty so the language-ranked pick chooses one.
        answers.tts_voice_id = DEFAULT_VOICES[provider] if is_english(answers.language) else ""
        answers.tts_model = ""
    answers.speech_provider = provider
    if answers.speech_key:
        answers.stt_key = answers.tts_key = answers.speech_key
    return apply_degraded_stt_model(answers)


def shares_speech_key(answers: SetupAnswers) -> bool:
    """True when the STT and voice providers are the same account (one key serves both)."""
    stt_env = _STT_KEY_ENV.get(answers.stt_provider)
    return bool(stt_env) and _TTS_KEY_ENV.get(answers.tts_provider) == stt_env


def capability_providers(answers: SetupAnswers) -> list[str]:
    """language_support keys for the configured STT and TTS providers."""
    stt = "deepgram" if answers.stt_provider == "deepgram" else f"{answers.stt_provider}-stt"
    tts = "deepgram-tts" if answers.tts_provider == "deepgram" else answers.tts_provider
    return [stt, tts]


def apply_degraded_stt_model(answers: SetupAnswers) -> list[str]:
    """Swap a launch model that lacks the language for the provider's wider one."""
    fallback = _STT_DEGRADED_MODELS.get((answers.stt_provider, answers.stt_model))
    if not fallback:
        return []
    key = "deepgram" if answers.stt_provider == "deepgram" else f"{answers.stt_provider}-stt"
    if language_support.lookup(answers.language, key).tier != language_support.TIER_DEGRADED:
        return []
    launch, answers.stt_model = answers.stt_model, fallback
    return [
        f"{STT_PROVIDER_LABELS[answers.stt_provider]} {launch} doesn't cover '{answers.language}'; "
        f"using {fallback} instead (stt.model = \"{fallback}\" in the profile)."
    ]

def load_existing_secrets(
    answers: SetupAnswers, env_path: Path, profile_path: Path | None = None
) -> SetupAnswers:
    """Fill blank secret fields from the profile's private-store references,
    then from the home .env (older profiles). Never overwrites a typed value."""
    if profile_path is not None:
        hydrate_answers_from_env(answers, _stored_secrets(profile_path))
    if not env_path.is_file():
        return answers
    from dotenv import dotenv_values

    env = {key: (value or "") for key, value in dotenv_values(env_path).items()}
    return hydrate_answers_from_env(answers, env)


def _profile_refs(profile_path: Path) -> dict[str, str]:
    try:
        raw = tomllib.loads(profile_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    refs = (raw.get("credentials") or {}).get("refs") or {}
    return {str(name): str(ref) for name, ref in refs.items() if isinstance(ref, str)}


def _stored_secrets(profile_path: Path) -> dict[str, str]:
    """The values a profile's [credentials.refs] point at, where readable."""
    refs = _profile_refs(profile_path)
    if not refs:
        return {}
    from sleeptalk.credential_store import CredentialError, CredentialStore

    store = CredentialStore()
    values: dict[str, str] = {}
    for name, reference in refs.items():
        try:
            values[name] = store.get(reference)
        except CredentialError:
            continue
    return values


def _credential_fields(answers: SetupAnswers) -> dict[str, str]:
    """Environment names the profile can reference in the private store, with
    the provider each reference must belong to (as config resolves them)."""
    from sleeptalk.config import _CREDENTIAL_PROVIDERS

    fields = dict(_CREDENTIAL_PROVIDERS)
    if parse_surface(answers.surface) == "discord":
        fields[resolved_token_env(answers)] = "discord"
    fields.update(agent_token_fields(answers))
    return fields


def agent_token_fields(answers: SetupAnswers) -> dict[str, str]:
    """The agent tokens these answers carry (gateway sessions, bridge tokens), for the private store."""
    from sleeptalk.config import _AGENT_TOKEN_PROVIDERS, _CREDENTIAL_PROVIDERS

    fields = dict(_AGENT_TOKEN_PROVIDERS)
    token_env = str(answers.agent_options.get("token_env") or "").strip()
    if answers.agent_type in BYO_AGENT_TYPES and token_env and token_env not in _CREDENTIAL_PROVIDERS:
        fields[token_env] = "agent-token"
    return fields


def store_agent_tokens(answers: SetupAnswers, profile_path: Path, env_path: Path) -> list[str]:
    """Keep the agent tokens in the private store and reference them from the
    profile (for editors that splice only [agent]); drops their .env copies."""
    from sleeptalk.key_inventory import forget_dropped, references_in
    from sleeptalk.profile_edit import set_string_key

    fields = agent_token_fields(answers)
    tokens = {name: value for name, value in env_entries(answers).items() if name in fields and value}
    if not tokens:
        return []
    before = references_in(profile_path)
    refs = _store_credentials(tokens, fields, _profile_refs(profile_path), profile_path.stem)
    for name in tokens:
        set_string_key(profile_path, "credentials.refs", name, refs[name])
    if env_path.is_file():
        merge_env_entries({}, env_path, drop=set(tokens))
    forget_dropped(before, profile_path)
    return sorted(tokens)


def _store_credentials(
    secrets: dict[str, str], fields: dict[str, str], existing: dict[str, str], profile_id: str
) -> dict[str, str]:
    """Keep each secret in the private store; return the profile's references.

    An unchanged value keeps its reference, so rerunning setup rewrites nothing.
    """
    if not secrets:
        return dict(existing)
    from sleeptalk.credential_store import CredentialError, CredentialStore

    store = CredentialStore()
    refs = dict(existing)
    for name, value in secrets.items():
        current = refs.get(name)
        if current:
            try:
                if store.status(current).provider == fields[name] and store.get(current) == value:
                    continue
            except CredentialError:
                pass
        refs[name] = store.put_validated(fields[name], value, profile_id=profile_id)
    return refs


def _drop_env_keys(text: str, names: set[str]) -> str:
    """Remove exactly these bindings from .env text, keeping everything else."""
    if not text or not names:
        return text
    import io

    from dotenv.parser import parse_stream

    return "".join(
        binding.original.string
        for binding in parse_stream(io.StringIO(text))
        if binding.key not in names
    )


def hydrate_answers_from_env(
    answers: SetupAnswers, env: Mapping[str, str]
) -> SetupAnswers:
    """Fill blank secret answers from an environment-shaped mapping."""
    if not answers.token:
        answers.token = (
            env.get(resolved_token_env(answers), "") or env.get("DISCORD_BOT_TOKEN", "")
        )
    if not answers.realtime_key:
        answers.realtime_key = env.get("OPENAI_API_KEY", "")
    if not answers.stt_key:
        answers.stt_key = env.get(_STT_KEY_ENV.get(answers.stt_provider, ""), "")
    if not answers.tts_key:
        answers.tts_key = env.get(_TTS_KEY_ENV.get(answers.tts_provider, ""), "")
    if answers.tts_provider == "playht" and not answers.tts_user_id:
        answers.tts_user_id = env.get("PLAYHT_USER_ID", "")
    if answers.agent_type == "hermes_gateway" and not answers.gateway_api_key:
        answers.gateway_api_key = env.get("HERMES_GATEWAY_API_KEY", "")
        from sleeptalk.hermes_connect import SESSION_TOKEN_ENV

        if not answers.gateway_session_token:
            answers.gateway_session_token = env.get(SESSION_TOKEN_ENV, "")
    token_env = str(answers.agent_options.get("token_env") or "")
    if answers.agent_type in BYO_AGENT_TYPES and token_env and not answers.agent_token:
        answers.agent_token = env.get(token_env, "")
    if answers.agent_type == "openclaw" and not answers.openclaw_token:
        answers.openclaw_token = env.get(OPENCLAW_TOKEN_ENV, "")
    return answers


def load_existing_answers(slug: str, profiles_dir: Path = PROFILES_DIR, *, resolved: bool = False) -> SetupAnswers | None:
    """Prefill answers from an existing profile, for edit-don't-wipe reruns."""
    path = profiles_dir / f"{slug}.toml"
    if not path.is_file():
        return None
    if resolved:
        from sleeptalk.config import resolve_config_document

        raw = resolve_config_document(path).raw
    else:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    answers = SetupAnswers()
    agent, transport = raw.get("agent", {}), raw.get("transport", {})
    route = raw.get("route", {})
    stt, tts = raw.get("stt", {}), raw.get("tts", {})
    answers.agent_type = agent.get("type", answers.agent_type)
    answers.workspace_dir = agent.get("workspace_dir", answers.workspace_dir)
    answers.gateway_url = agent.get("gateway_url", "")
    # Kept verbatim so editing another section never drops private settings.
    answers.agent_options = dict(agent.get("options", {}))
    answers.agent_permission_mode = agent.get("permission_mode", "")
    answers.gateway_session_id = agent.get("gateway_session_id", "")
    if answers.agent_type == "model":
        from sleeptalk.model_only_setup import vendor_for_base_url

        auth = agent.get("auth", {})
        answers.model = str(agent.get("model", ""))
        base_url = str(auth.get("base_url", ""))
        answers.model_vendor = vendor_for_base_url(base_url) if base_url else ""
        answers.model_base_url = base_url if answers.model_vendor == "other" else ""
        answers.model_credential_ref = str(auth.get("credential_ref", ""))
    answers.name = agent.get("name", answers.name)
    answers.slug = agent.get("slug", slug)
    answers.surface = surface_for_transport(transport.get("type", "discord"))
    answers.delivery = str((transport.get("options") or {}).get("delivery", "") or "")
    answers.capture_mode = transport.get("capture_mode", "")
    answers.narration_style = raw.get("policy", {}).get("narration_style", "")
    answers.reply_length = raw.get("policy", {}).get("reply_length", "")
    answers.instant_ack_mode = raw.get("policy", {}).get("instant_ack_mode", "smart")
    answers.endpointing = raw.get("policy", {}).get("endpointing", "")
    answers.guild_id = transport.get("guild_id", "")
    answers.client_id = transport.get("client_id", "")
    answers.authorization_required = bool(transport.get("authorization_required", True))
    answers.operator_ids = str(transport.get("operator_ids", "") or "")
    answers.allowed_channel_ids = str(transport.get("allowed_channel_ids", "") or "")
    answers.token_env = transport.get("token_env", answers.token_env)
    answers.route_type = parse_route_type(route.get("type", answers.route_type))
    answers.realtime_model = route.get("model", answers.realtime_model)
    answers.realtime_voice = route.get("voice", answers.realtime_voice)
    answers.speech_model = route.get("speech_model", answers.speech_model)
    answers.transcription_model = route.get(
        "transcription_model", answers.transcription_model
    )
    answers.language = route.get("language", stt.get("language", answers.language))
    answers.stt_provider = stt.get("provider", answers.stt_provider)
    answers.stt_model = stt.get("model", answers.stt_model)
    answers.tts_provider = tts.get("provider", answers.tts_provider)
    answers.tts_model = tts.get("model", "")
    answers.tts_voice_id = tts.get("voice_id", "")
    answers.tts_speed = float(tts.get("speed", 1.0))
    answers.latency_log = bool(raw.get("log", {}).get("latency", False))
    return answers


def existing_profiles(profiles_dir: Path = PROFILES_DIR) -> list[str]:
    if not profiles_dir.is_dir():
        return []
    return sorted(path.stem for path in profiles_dir.glob("*.toml"))


def allocate_slug(name: str, taken: Iterable[str]) -> str:
    base = slugify(name)
    occupied = set(taken)
    if base not in occupied:
        return base
    index = 2
    candidate = f"{base}-{index}"
    while candidate in occupied:
        index += 1
        candidate = f"{base}-{index}"
    return candidate


def apply_create_slug(
    answers: SetupAnswers,
    profiles_dir: Path,
    *,
    editing: bool = False,
) -> None:
    if editing:
        return
    answers.slug = allocate_slug(answers.name, existing_profiles(profiles_dir))


def _env_line(key: str, value: str) -> str:
    """One ``KEY='value'`` line that dotenv reads back exactly.

    Single quotes keep a value literal (no escapes, no ``${}``), so a token
    with ``#``, spaces, or ``$`` survives. A line break or other control
    character could smuggle in a second variable, so it is refused.
    """
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
        raise ValueError(f"{key!r} is not a valid environment variable name.")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError(f"The value for {key} contains a line break or control character.")
    if "'" not in value:
        return f"{key}='{value}'"
    if not any(c in value for c in '"\\$`'):
        return f'{key}="{value}"'
    raise ValueError(f"The value for {key} mixes quote characters and can't be stored safely in .env.")


def _merge_env_text(existing: str, entries: dict[str, str]) -> str:
    lines = existing.splitlines()
    for key, value in entries.items():
        prefix = f"{key}="
        line_for = _env_line(key, value)
        replaced = False
        for i, line in enumerate(lines):
            if line.startswith(prefix):
                lines[i] = line_for
                replaced = True
                break
        if not replaced:
            lines.append(line_for)
    return "\n".join(lines).strip() + "\n"


def merge_env_entries(
    entries: dict[str, str], env_path: Path, *, drop: set[str] | frozenset[str] = frozenset()
) -> bool:
    """Atomically merge secret entries, preserving unrelated keys.

    ``drop`` removes bindings whose value now lives in the private store.
    Returns ``True`` only when the file's bytes or permissions changed.
    """
    import contextlib

    existing = env_path.read_text(encoding="utf-8") if env_path.is_file() else ""
    kept = {key: value for key, value in entries.items() if value}
    if not existing and not kept:
        return False
    merged = _drop_env_keys(existing, set(drop))
    merged = _merge_env_text(merged, kept) if kept else merged
    mode_ok = True
    if env_path.is_file():
        with contextlib.suppress(OSError):
            mode_ok = (env_path.stat().st_mode & 0o777) == 0o600
    if existing == merged and mode_ok:
        return False

    env_path.parent.mkdir(parents=True, exist_ok=True)
    temp = env_path.with_name(f".{env_path.name}.tmp-{os.getpid()}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(temp, flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(merged)
        os.replace(temp, env_path)
        with contextlib.suppress(OSError):
            os.chmod(env_path, 0o600)
    except Exception:
        with contextlib.suppress(FileNotFoundError):
            temp.unlink()
        raise
    return True


def store_profile_secrets(
    answers: SetupAnswers, profile_path: Path, env_path: Path
) -> dict[str, str]:
    """Save the answers' keys for an existing profile.

    Keys the profile can reference go to the private store, with the
    profile's [credentials.refs] updated in place and any plain-text copy
    removed from .env; the rest merge into .env. Returns every entry, for
    the caller's runtime environment.
    """
    from sleeptalk.profile_edit import set_string_key

    entries = env_entries(answers)
    fields = _credential_fields(answers)
    secrets = {name: value for name, value in entries.items() if name in fields}
    rest = {name: value for name, value in entries.items() if name not in fields}
    existing = _profile_refs(profile_path)
    refs = _store_credentials(secrets, fields, existing, answers.slug or profile_path.stem)
    for name, reference in sorted(refs.items()):
        if existing.get(name) != reference:
            set_string_key(profile_path, "credentials.refs", name, reference)
    merge_env_entries(rest, env_path, drop=set(secrets))
    return entries


def write_files(
    answers: SetupAnswers,
    *,
    profiles_dir: Path = PROFILES_DIR,
    env_path: Path = ENV_PATH,
    bind_runtime: bool = False,
) -> list[str]:
    """Write profile TOML, keep keys in the private store, merge the rest into .env.

    Speech, realtime, and Discord keys go to the private store and the profile
    references them; any plain-text copy of those in .env is removed. Agent
    tokens the profile cannot reference yet still go to .env (mode 0600).
    Profile and secrets are staged, then secrets commit before the profile.
    The profile is the bootable commit point: an interruption may leave
    unused credentials, but never a new profile with missing credentials.
    Re-running converges that recoverable state. Identical bytes are a no-op.
    """
    import contextlib

    profile_text = profile_toml(answers)
    existing_profile = profiles_dir / f"{answers.slug}.toml"
    if existing_profile.is_file():
        # Keep settings the answers don't carry (made on the settings page, by `host`, or by hand).
        from sleeptalk.profile_edit import keep_unmodelled_settings

        profile_text = keep_unmodelled_settings(profile_text, existing_profile.read_text(encoding="utf-8"))
    if bind_runtime:
        from sleeptalk.config import AgentConfig
        from sleeptalk.runtime_binding import bind, toml
        agent = AgentConfig.model_validate(tomllib.loads(profile_text)["agent"])
        profile_text += toml(bind(agent))
    entries = env_entries(answers)
    fields = _credential_fields(answers)
    secrets = {name: value for name, value in entries.items() if name in fields}
    entries = {name: value for name, value in entries.items() if name not in fields}
    profile_path = profiles_dir / f"{answers.slug}.toml"
    # Validates against the real schema before touching disk.
    from sleeptalk.config import SleeptalkConfig, ensure_shared_root, project_root_for_profiles

    SleeptalkConfig.model_validate(tomllib.loads(profile_text))
    before_refs = _profile_refs(profile_path)
    refs = _store_credentials(secrets, fields, before_refs, answers.slug)
    if refs:
        profile_text = profile_text.rstrip("\n") + "\n\n[credentials.refs]\n" + "".join(
            f"{name} = {json.dumps(reference)}\n" for name, reference in sorted(refs.items())
        )
        SleeptalkConfig.model_validate(tomllib.loads(profile_text))

    written: list[str] = []
    profiles_dir.mkdir(parents=True, exist_ok=True)
    project_root = project_root_for_profiles(profiles_dir)
    if project_root is not None:
        seeded = ensure_shared_root(project_root)
        if seeded is not None:
            written.append(str(seeded))

    existing_env = env_path.read_text(encoding="utf-8") if env_path.is_file() else ""
    env_text = _drop_env_keys(existing_env, set(refs))
    env_text = _merge_env_text(env_text, entries) if entries else env_text

    profile_unchanged = (
        profile_path.is_file() and profile_path.read_text(encoding="utf-8") == profile_text
    )
    # With every key in the private store there may be nothing for .env at all.
    env_unchanged = (existing_env == env_text) if env_path.is_file() else not env_text.strip()
    env_mode_ok = True
    if env_path.is_file():
        with contextlib.suppress(OSError):
            env_mode_ok = (env_path.stat().st_mode & 0o777) == 0o600

    if profile_unchanged and env_unchanged and env_mode_ok:
        return written

    profile_tmp = profile_path.with_name(f".{profile_path.name}.tmp-{os.getpid()}")
    env_tmp = env_path.with_name(f".{env_path.name}.tmp-{os.getpid()}")
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        if not profile_unchanged:
            # Profiles name key references and endpoints: private, like .env.
            with os.fdopen(os.open(profile_tmp, flags, 0o600), "w", encoding="utf-8") as handle:
                handle.write(profile_text)
        if not (env_unchanged and env_mode_ok):
            env_path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(env_tmp, flags, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(env_text)
        if not (env_unchanged and env_mode_ok):
            os.replace(env_tmp, env_path)
            with contextlib.suppress(OSError):
                os.chmod(env_path, 0o600)
            written.append(str(env_path))
        if not profile_unchanged:
            os.replace(profile_tmp, profile_path)
            written.append(str(profile_path))
            # A key this profile no longer names goes, unless another profile uses it.
            from sleeptalk.key_inventory import forget_dropped

            forget_dropped(before_refs.values(), profile_path)
    except Exception:
        with contextlib.suppress(FileNotFoundError):
            profile_tmp.unlink()
        with contextlib.suppress(FileNotFoundError):
            env_tmp.unlink()
        raise

    gitignore = Path(".gitignore")
    if gitignore.is_file() and ".env" not in gitignore.read_text().splitlines():
        with gitignore.open("a", encoding="utf-8") as handle:
            handle.write("\n# sleeptalk secrets\n.env\n")
        written.append(str(gitignore) + " (.env entry added)")
    return written


async def auto_pick_voice(answers: SetupAnswers) -> tuple[list[str], str]:
    """Choose a voice for the profile language; returns (notes, failure).

    Filters the provider's catalog by declared language first, then ranks.
    ``rank_voices`` alone never constrained on a BCP-47 tag ("fr-FR" is one
    token to it), which is why the pick used to land on whatever came
    first. A language with no tagged voice falls back to the catalog with a
    note, so setup still completes and the note says what to fix.
    """
    from sleeptalk.voice_discovery import default_catalogs, rank_voices, voices_for_language

    try:
        credentials = {
            answers.tts_provider: answers.tts_key,
            "cartesia": answers.tts_key if answers.tts_provider == "cartesia" else "",
            "deepgram": answers.tts_key if answers.tts_provider == "deepgram" else "",
            "elevenlabs": answers.tts_key if answers.tts_provider == "elevenlabs" else "",
            "hume": answers.tts_key if answers.tts_provider == "hume" else "",
        }
        provider = next(
            (catalog for catalog in default_catalogs(credentials) if catalog.name == answers.tts_provider),
            None,
        )
        if provider is None:
            # No live voice list (OpenRouter, Venice, Groq, Together): the
            # catalog's recommendation for the language, as the settings page picks.
            return _catalog_voice_pick(answers)
        voices = await provider.list_voices()
    except Exception as exc:
        return [], f"couldn't fetch voices to auto-pick one: {exc}"
    if not voices:
        return [], "no voices available to auto-pick — set tts_voice_id explicitly"
    notes: list[str] = []
    candidates = voices_for_language(voices, answers.language)
    if not candidates:
        label = language_label(answers.language)
        notes.append(
            f"{TTS_PROVIDER_LABELS.get(answers.tts_provider, answers.tts_provider)} has no voice tagged "
            f"{label}; using an English voice — pick another under Voice, or switch provider."
        )
        candidates = voices
    picks = rank_voices(candidates, language_label(answers.language).split(" (", 1)[0].lower())
    selected = picks[0].voice if picks else candidates[0]
    answers.tts_voice_id = selected.voice_id
    from sleeptalk.builder import tts_model_for_provider

    answers.tts_model = tts_model_for_provider(
        answers.tts_provider,
        str(selected.provider_metadata.get("modelId") or answers.tts_model),
    )
    return notes, ""


def _catalog_voice_pick(answers: SetupAnswers) -> tuple[list[str], str]:
    from sleeptalk import speech_catalog

    voice = speech_catalog.recommended_voice(answers.tts_provider, answers.language)
    if not voice:
        return [], "no voices available to auto-pick — set tts_voice_id explicitly"
    option = speech_catalog.recommended(answers.tts_provider, speech_catalog.TTS, answers.language)
    answers.tts_voice_id = voice
    answers.tts_model = speech_catalog.profile_model(option, voice) if option is not None else ""
    return [], ""

async def _prove_selected_voice(answers: SetupAnswers) -> Path:
    """Validate provider credentials by synthesizing the selected voice."""
    from sleeptalk.builder import tts_spec
    from sleeptalk.config import TTSConfig
    from sleeptalk.defaults import register_defaults
    from sleeptalk.registries import TTS_PROVIDERS

    register_defaults()
    spec = tts_spec(TTSConfig(
        provider=answers.tts_provider,
        model=answers.tts_model,
        voice_id=answers.tts_voice_id,
        language=answers.language,
    ))
    tts = TTS_PROVIDERS.build(answers.tts_provider, spec, env_entries(answers))
    try:
        return await tts.synthesize_wav(sample_line(answers.language))
    finally:
        close = getattr(tts, "close", None)
        if callable(close):
            await close()

REQUIRED_ANSWER_FIELDS = ("name", "guild_id", "client_id", "token", "stt_key", "tts_key")


def answers_from_mapping(raw: dict) -> SetupAnswers:
    """Answers file (TOML) -> SetupAnswers. Unknown keys are a hard error."""
    known = set(SetupAnswers.__dataclass_fields__)
    unknown = sorted(set(raw) - known)
    if unknown:
        raise ValueError(
            f"unknown answers key(s) {unknown} — valid keys: {sorted(known)}"
        )
    answers = SetupAnswers()
    for key, value in raw.items():
        if key == "agent_options":
            if not isinstance(value, dict):
                raise ValueError("agent_options must be a table, e.g. agent_options = { command = [\"node\", \"agent.mjs\"] }")
            setattr(answers, key, dict(value))
            continue
        if key == "tts_speed":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError("tts_speed must be a number")
            value = float(value)
        else:
            value = str(value)
        setattr(answers, key, value)
    answers.surface = parse_surface(answers.surface)
    answers.route_type = parse_route_type(answers.route_type)
    if answers.tts_mode.strip():
        answers.tts_mode = parse_tts_mode(answers.tts_mode)
    if not answers.slug or answers.slug == "sleeptalk":
        answers.slug = slugify(answers.name)
    if answers.speech_provider.strip():
        # Pointing both directions at the provider resets the models to its
        # defaults; models the answers name themselves stay.
        chosen = {key: raw[key] for key in ("stt_model", "tts_model") if str(raw.get(key) or "").strip()}
        apply_speech_provider(answers, answers.speech_provider)
        for key, value in chosen.items():
            setattr(answers, key, str(value))
    elif answers.speech_key:
        raise ValueError("speech_key needs speech_provider (deepgram, cartesia, or elevenlabs)")
    if answers.speech_provider.strip() == "local" and answers.speech_key:
        raise ValueError("speech_provider = \"local\" takes no speech_key")
    if answers.agent_type == "model":
        from sleeptalk.model_only_setup import ModelSetupError, normalize_vendor

        try:
            answers.model_vendor = normalize_vendor(answers.model_vendor)
        except ModelSetupError as exc:
            raise ValueError(str(exc)) from None
    # Same degraded-tier fallback the interactive interview applies, unless
    # the answers chose the listening model themselves.
    if not str(raw.get("stt_model") or "").strip():
        apply_degraded_stt_model(answers)
    return answers


def missing_answer_fields(answers: SetupAnswers) -> list[str]:
    missing = [
        f"{field} missing — provide it in the answers file or via --{field.replace('_', '-')}"
        for field in required_answer_fields(answers)
        if not getattr(answers, field).strip()
    ]
    if answers.tts_provider == "playht" and not answers.tts_user_id.strip():
        missing.append(
            "tts_user_id missing — PlayHT requires it beside --tts-key; "
            "provide it via --tts-user-id"
        )
    return missing


async def hybrid_connectivity_check(answers: SetupAnswers) -> None:
    """Prove the exact Realtime, transcription, voice, and Speech choices."""
    from sleeptalk.openai_realtime import (
        OpenAIRealtimeConfig,
        check_openai_connectivity,
    )

    check = check_openai_connectivity
    if answers.realtime_model == "gpt-live-1":
        from sleeptalk.openai_live import check_openai_live_connectivity
        check = check_openai_live_connectivity
    await check(
        OpenAIRealtimeConfig(
            api_key=answers.realtime_key,
            model=answers.realtime_model or DEFAULT_REALTIME_MODEL,
            voice=answers.realtime_voice or DEFAULT_REALTIME_VOICE,
            speech_model=answers.speech_model or DEFAULT_SPEECH_MODEL,
            transcription_model=(
                answers.transcription_model or DEFAULT_TRANSCRIPTION_MODEL
            ),
        )
    )


async def validate_answers(answers: SetupAnswers, *, live: bool = True) -> list[str]:
    """Live-validate a complete answer set; returns fix-stating problems.

    Mirrors the interactive interview's checks. Hybrid validates Discord and
    gateway credentials, then delegates its OpenAI route check to
    ``hybrid_connectivity_check``; cascade keeps the established STT/TTS
    probes.
    """
    problems: list[str] = []
    if answers.agent_type != "hermes_gateway":
        from sleeptalk.agents.readiness import probe as agent_probe
        from sleeptalk.defaults import register_defaults
        from sleeptalk.registries import AGENTS

        register_defaults()
        registration = AGENTS.get_optional(answers.agent_type)
        if registration is None:
            problems.append(f"unknown agent type {answers.agent_type!r}")
        elif answers.agent_type in BYO_AGENT_TYPES:
            try:
                registration.validate_options(answers.agent_options)
            except ValueError as exc:
                problems.append(f"agent_options: {_first_validation_message(exc)}")
            token_env = str(answers.agent_options.get("token_env") or "")
            if token_env and not (answers.agent_token or os.environ.get(token_env)):
                problems.append(
                    f"agent_token missing — {token_env} is not set; provide it in the answers file or export it"
                )
        else:
            report = agent_probe(answers.agent_type)
            if not report.ready:
                problems.extend(report.missing_lines())
    model_choice = None
    if answers.agent_type == "model":
        from sleeptalk import model_only_setup as setup

        try:
            model_choice = setup.auth_choice(answers.model_vendor, answers.model_base_url)
        except setup.ModelSetupError as exc:
            problems.append(str(exc))
        if (not answers.model_key and not answers.model_credential_ref
                and setup.key_required(answers.model_vendor, answers.model_base_url)):
            problems.append("model_key missing — provide it in the answers file")
    if not live:
        return problems

    if model_choice is not None and (answers.model_key or not answers.model_credential_ref):
        login = await setup.verify_key(model_choice, answers.model, answers.model_key)
        if login == "unavailable" and not setup.key_optional(model_choice):
            problems.append(f"model_key: {setup.rejected_message(answers.model_vendor)}")

    if answers.agent_type == "hermes_gateway":
        from sleeptalk.hermes_connect import DEFAULT_ORIGIN, Ready, attach_hermes, probe_message

        probe = await attach_hermes(
            answers,
            raw_url=answers.gateway_url or DEFAULT_ORIGIN,
            token=answers.gateway_session_token,
        )
        if not isinstance(probe, Ready):
            problems.append(probe_message(probe))

    if parse_surface(answers.surface) == "discord":
        try:
            await discord_check.check_token(answers.token)
        except discord_check.DiscordCheckError as exc:
            problems.append(str(exc))
            return problems  # guild/intents need a working token
        try:
            await discord_check.check_guild_membership(
                answers.token, answers.guild_id, answers.client_id
            )
        except discord_check.DiscordCheckError as exc:
            problems.append(str(exc))
        try:
            await discord_check.probe_gateway_intents(
                answers.token, discord_check.requested_intents_value_for(voice_only=False)
            )
        except discord_check.DiscordCheckError as exc:
            problems.append(str(exc))

    if parse_route_type(answers.route_type) == "hybrid":
        try:
            await hybrid_connectivity_check(answers)
        except Exception as exc:
            problems.append(str(exc))
        return problems

    from sleeptalk.builder import stt_spec
    from sleeptalk.config import STTConfig
    from sleeptalk.stt_roundtrip import stt_roundtrip

    try:
        await stt_roundtrip(
            answers.stt_provider,
            stt_spec(STTConfig(provider=answers.stt_provider, model=answers.stt_model, language=answers.language)),
            answers.stt_key,
        )
    except Exception as exc:
        problems.append(str(exc))

    if not answers.tts_voice_id.strip():
        notes, failure = await auto_pick_voice(answers)
        if failure:
            problems.append(failure)
            return problems
        problems.extend(notes)

    # Prove both credentials and the exact selected/pasted voice with a real
    # synthesis request, then delete the transient sample.
    try:
        path = await _prove_selected_voice(answers)
        path.unlink(missing_ok=True)
    except Exception as exc:
        problems.append(f"voice '{answers.tts_voice_id}' failed to synthesize: {exc}")
    return problems
