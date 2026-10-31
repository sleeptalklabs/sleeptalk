"""Default component registrations — the only bridge between sleeptalk and
the rest of the app. All app imports happen lazily inside factory bodies so
this package stays self-contained and cheap to import.

Adding a component = a new class + one register() call here (or anywhere —
tests register fakes directly). Zero edits to the builder or config loader.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

from sleeptalk.harness_versions import install_spec, omp_install_command

from sleeptalk.agents.profiles.cursor import (
    CURSOR_CLI_BINARY_FALLBACKS,
    CURSOR_CLI_INSTALL_COMMAND,
    CURSOR_CLI_INSTALL_HINT,
)

# oh-my-pi's official installer (https://omp.sh) puts a standalone binary in
# ~/.local/bin, which a fresh shell or a service may not have on PATH yet.
OMP_INSTALL_COMMAND = omp_install_command()
OMP_BINARY_FALLBACKS = ("~/.local/bin/omp",)

_REGISTERED = False


def _provider_model(
    default: str, prefixes: tuple[str, ...]
) -> Callable[[str], str]:
    def normalize(model: str) -> str:
        selected = (model or "").strip()
        if not selected or not selected.startswith(prefixes):
            return default
        return selected

    return normalize


def register_defaults() -> None:
    global _REGISTERED
    if _REGISTERED:
        return
    _REGISTERED = True

    from sleeptalk.registries import AGENTS, STT_PROVIDERS, TRANSPORTS, TTS_PROVIDERS
    from sleeptalk.transports.web import WebOptions
    from sleeptalk.agents.hermes_bridge import HermesOptions
    from sleeptalk.harness_versions import pi_install_spec

    # ---- transports ------------------------------------------------------
    TRANSPORTS.register(
        "discord",
        _build_discord_transport_v2,
        required_env=lambda config: {
            config.transport.token_env: (
                "create/copy a bot token at discord.com/developers/applications "
                f"-> your app -> Bot -> Reset Token, and add {config.transport.token_env}=... "
                "to .env"
            ),
        },
        config_fields={
            "guild_id",
            "client_id",
            "allowed_channel_ids",
            "authorization_required",
            "operator_ids",
            "speaker_ids",
            "listener_ids",
            "token_env",
            "opus_library",
            "dave_library",
            "native_voice_enabled",
            "voice_only",
            "presence_auto_join",
            "post_replies",
            "post_caller_speech",
            "debug",
        },
    )
    TRANSPORTS.register(
        "null",
        _build_null_transport,
        config_fields={"log_path"},
    )
    TRANSPORTS.register(
        "web",
        _build_web_transport,
        config_fields={"log_path"},
        options_schema=WebOptions,
    )
    # Terminal surface (r2-terminal): local microphone/speaker conversation
    # via the cascade route; needs the sounddevice package and audio
    # devices (clear errors otherwise — see terminal_audio).
    TRANSPORTS.register(
        "terminal",
        _build_terminal_transport,
        config_fields={"input_device", "output_device", "log_path", "capture_mode"},
    )

    # ---- agents ----------------------------------------------------------
    AGENTS.register(
        "pi",
        _build_pi_agent,
        binary=lambda agent: agent.pi_bin or "pi",
        install_hint=f"install pi: npm i -g {pi_install_spec()} (existing Pi installations are retained)",
        binary_install=("npm", "install", "-g", pi_install_spec()),
        config_fields={
            "name",
            "slug",
            "persona_instructions",
            "workspace_dir",
            "pi_bin",
            "model",
            "provider",
            "provider_slug",
            "permission_mode",
            "runtime",
            "on_incompatible",
        },
    )
    AGENTS.register(
        "omp",
        _build_omp_agent,
        binary=lambda agent: agent.pi_bin or "omp",
        binary_fallbacks=OMP_BINARY_FALLBACKS,
        binary_install=("sh", "-c", OMP_INSTALL_COMMAND),
        install_hint=f"install oh-my-pi CLI: {OMP_INSTALL_COMMAND} (existing installations are retained; guarded pending/deny mode requires the separately selected npm SDK and Bun)",
        config_fields={
            "name",
            "slug",
            "persona_instructions",
            "workspace_dir",
            "pi_bin",
            "model",
            "provider",
            "provider_slug",
            "permission_mode",
            "runtime",
            "on_incompatible",
        },
    )
    AGENTS.register(
        "acp",
        _build_acp_agent,
        aliases=("hermes", "hermes-acp", "hermes_acp"),
        binary=lambda agent: agent.pi_bin or "hermes",
        install_hint="install Hermes: see the hermes repo's README (provides `hermes acp`)",
        options_schema=HermesOptions,
        config_fields={
            "options", "pi_bin", "model", "provider", "auth",
            "name",
            "slug",
            "persona_instructions",
            "workspace_dir",
            "permission_mode",
            "runtime",
            "on_incompatible",
        },
    )
    AGENTS.register(
        "openclaw",
        _build_openclaw_agent,
        binary=lambda agent: "" if getattr(agent, "gateway_url", "").strip() else "openclaw",
        required_env=lambda agent: (
            {
                "OPENCLAW_GATEWAY_TOKEN": (
                    "copy gateway.auth.token from ~/.openclaw/openclaw.json on the gateway "
                    "host (`openclaw config get` shows a redacted placeholder), then add "
                    "OPENCLAW_GATEWAY_TOKEN=... to .env "
                    "(OPENCLAW_GATEWAY_PASSWORD also works at runtime)"
                ),
            }
            if getattr(agent, "gateway_url", "").strip()
            else {}
        ),
        install_hint=(
            f"install OpenClaw with supported Node: npm i --engine-strict -g {install_spec('openclaw')}, then run `openclaw onboard` "
            "once (voice talks to the Gateway WebSocket; `openclaw acp` is fallback)"
        ),
        binary_install=("npm", "install", "--engine-strict", "-g", install_spec("openclaw")),
        config_fields={
            "name",
            "slug",
            "persona_instructions",
            "workspace_dir",
            "permission_mode",
            "gateway_url",
            "gateway_session_id",
            "runtime",
            "on_incompatible",
        },
    )
    AGENTS.register(
        "openclaw_acp",
        _build_openclaw_acp_agent,
        aliases=("openclaw-acp",),
        binary="openclaw",
        install_hint=(
            f"install OpenClaw with Node 24.16+ (24.x) or 26.1+: npm i --engine-strict -g {install_spec('openclaw')}, then run `openclaw onboard` once"
        ),
        binary_install=("npm", "install", "--engine-strict", "-g", install_spec("openclaw")),
        config_fields={
            "name",
            "slug",
            "persona_instructions",
            "workspace_dir",
            "permission_mode",
            "gateway_url",
            "gateway_session_id",
            "runtime",
            "on_incompatible",
        },
    )
    AGENTS.register(
        "codex",
        _build_codex_agent,
        aliases=("codex_app_server", "codex-app-server"),
        binary="codex",
        install_hint=f"install Codex CLI: npm i -g {install_spec('codex')} (stdio app-server)",
        binary_install=("npm", "install", "-g", install_spec("codex")),
        config_fields={
            "name",
            "slug",
            "persona_instructions",
            "workspace_dir",
            "model",
            "permission_mode",
            "gateway_session_id",
            "runtime",
            "on_incompatible",
        },
    )
    AGENTS.register(
        "claude_code",
        _build_claude_code_agent,
        aliases=("claude-code",),
        binary="claude",
        install_hint=f"install Claude Code with Node 22+: npm i --engine-strict -g {install_spec('claude_code')}",
        binary_install=("npm", "install", "--engine-strict", "-g", install_spec("claude_code")),
        python_module="claude_agent_sdk",
        python_extra="sleeptalk[claude]",
        conversation_context=_claude_conversation_context,
        config_fields={
            "name",
            "slug",
            "persona_instructions",
            "workspace_dir",
            "model",
            "permission_mode",
            "runtime",
            "on_incompatible",
        },
    )
    AGENTS.register(
        "hermes_gateway",
        _build_hermes_gateway_agent,
        aliases=("hermes-gateway",),
        binary=lambda agent: (
            "" if getattr(agent, "gateway_url", "").strip() else (agent.pi_bin or "hermes")
        ),
        binary_fallbacks=("~/.hermes/hermes-agent/venv/bin/python",),
        install_hint=(
            "install Hermes and its TUI gateway, or set agent.gateway_url to a "
            "running Hermes dashboard"
        ),
        required_env=_hermes_gateway_required_env,
        conversation_context=_hermes_conversation_context,
        options_schema=HermesOptions,
        config_fields={
            "options", "pi_bin", "model", "provider", "auth", "permission_mode",
            "name",
            "slug",
            "persona_instructions",
            "workspace_dir",
            "gateway_url",
            "gateway_session_id",
            "runtime",
            "on_incompatible",
        },
    )
    AGENTS.register(
        "cursor",
        _build_cursor_agent,
        python_module="cursor_sdk",
        python_extra="sleeptalk[cursor]",
        config_fields={
            "name",
            "slug",
            "persona_instructions",
            "workspace_dir",
            "model",
            "permission_mode",
            "gateway_session_id",
            "runtime",
            "on_incompatible",
        },
    )
    AGENTS.register(
        "cursor_acp",
        _build_cursor_acp_agent,
        aliases=("cursor-acp",),
        binary="agent",
        binary_fallbacks=CURSOR_CLI_BINARY_FALLBACKS,
        binary_install=("bash", "-c", CURSOR_CLI_INSTALL_COMMAND),
        install_hint=CURSOR_CLI_INSTALL_HINT,
        config_fields={
            "name",
            "slug",
            "persona_instructions",
            "workspace_dir",
            "model",
            "permission_mode",
            "gateway_session_id",
            "runtime",
            "on_incompatible",
        },
    )
    # Bring your own agent: command (Bridge Lite or ACP), http, bridge.
    from sleeptalk.agents import byo

    byo.register(AGENTS)

    # Talk to a model directly: no agent framework, no tools, one API key.
    from sleeptalk.agents import model_only

    model_only.register(AGENTS)

    # ---- STT ---------------------------------------------------------------
    STT_PROVIDERS.register(
        "deepgram",
        _build_deepgram_stt,
        required_env={
            "DEEPGRAM_API_KEY": (
                "get a key at console.deepgram.com -> API Keys and add "
                "DEEPGRAM_API_KEY=... to .env"
            ),
        },
        config_fields={
            "base_url",
            "model",
            "language",
            "keyterms",
            "pause_ahead_ms",
            "rest_timeout_seconds",
            "streaming_timeout_seconds",
            "endpointing_ms",
        },
        check=_check_deepgram,
    )

    STT_PROVIDERS.register(
        "cartesia",
        _build_cartesia_stt,
        required_env={
            "CARTESIA_API_KEY": (
                "get a key at play.cartesia.ai -> API Keys and add "
                "CARTESIA_API_KEY=... to .env (the same key serves TTS)"
            ),
        },
        config_fields={
            "base_url",
            "model",
            "language",
            "keyterms",
            "pause_ahead_ms",
            "rest_timeout_seconds",
            "streaming_timeout_seconds",
            "endpointing_ms",
        },
        # ink-2 covers en, es, fr, hi, ja; ink-whisper covers other languages and batch REST.
        provider_defaults={"model": "ink-2"},
        normalize_model=_provider_model("ink-2", ("ink-",)),
        check=_check_cartesia_stt,
    )

    # Scribe v2 Realtime with REST recovery; explicit older models remain batch.
    STT_PROVIDERS.register(
        "elevenlabs",
        _build_elevenlabs_stt,
        required_env={
            "ELEVENLABS_API_KEY": (
                "get a key at elevenlabs.io -> Developers -> API Keys and add "
                "ELEVENLABS_API_KEY=... to .env (the same key serves TTS)"
            ),
        },
        config_fields={
            "base_url",
            "model",
            "language",
            "keyterms",
            "pause_ahead_ms",
            "rest_timeout_seconds",
            "streaming_timeout_seconds",
            "endpointing_ms",
        },
        provider_defaults={"model": "scribe_v2"},
        normalize_model=_provider_model("scribe_v2", ("scribe",)),
        check=_check_elevenlabs_stt,
    )

    # Local speech: no key, models cached on this machine (speech/local_models.py).
    STT_PROVIDERS.register(
        "local",
        _build_local_stt,
        # The same common fields the cloud providers take: settings keeps a
        # profile's [stt] keys when the provider changes, so Local must accept
        # (and ignore) base_url and keyterms rather than reject the profile.
        config_fields={"base_url", "model", "language", "keyterms", "rest_timeout_seconds",
                       "streaming_timeout_seconds", "endpointing_ms", "pause_ahead_ms"},
        provider_defaults={"model": "moonshine-small-streaming-en"},
        normalize_model=_provider_model("moonshine-small-streaming-en", ("moonshine",)),
        check=_check_local_stt,
        install_hint="choose Local speech in sleeptalk settings to install the local runtimes and download the models",
    )

    _register_openai_audio_stt()

    # ---- TTS ---------------------------------------------------------------
    TTS_PROVIDERS.register(
        "local",
        _build_local_tts,
        config_fields={"base_url", "model", "voice_id", "language", "speed", "output_format", "fallback"},
        provider_defaults={"model": "kokoro-en"},
        normalize_model=_provider_model("kokoro-en", ("piper", "kokoro", "pocket")),
        check=_check_local_tts,
        install_hint="choose Local speech in sleeptalk settings to install the local runtimes and download the models",
    )
    _register_openai_audio_tts()
    TTS_PROVIDERS.register(
        "deepgram",
        _build_deepgram_tts,
        required_env={
            "DEEPGRAM_API_KEY": (
                "get a key at console.deepgram.com -> API Keys and add "
                "DEEPGRAM_API_KEY=... to .env (the same key serves STT)"
            ),
        },
        config_fields={
            "base_url", "model", "voice_id", "language", "speed",
            "output_format", "fallback",
        },
        # The model is the voice: flux-* streams over websocket (English),
        # aura-2-* takes chunked REST (en, es, de, fr, nl, it, ja).
        provider_defaults={"model": "flux-hannah-en", "output_format": "wav"},
        normalize_model=_provider_model("flux-hannah-en", ("flux-", "aura-")),
        check=_check_deepgram_tts,
    )
    TTS_PROVIDERS.register(
        "cartesia",
        _build_cartesia_tts,
        required_env={
            "CARTESIA_API_KEY": (
                "get a key at play.cartesia.ai -> API Keys and add "
                "CARTESIA_API_KEY=... to .env"
            ),
        },
        config_fields={
            "base_url", "model", "voice_id", "language", "speed",
            "output_format", "fallback",
        },
        provider_defaults={"model": "sonic-3.6", "output_format": "wav"},
        normalize_model=_provider_model("sonic-3.6", ("sonic",)),
        check=_check_cartesia,
    )
    TTS_PROVIDERS.register(
        "elevenlabs",
        _build_elevenlabs_tts,
        required_env={
            "ELEVENLABS_API_KEY": (
                "get a key at elevenlabs.io -> Developers -> API Keys and add "
                "ELEVENLABS_API_KEY=... to .env"
            ),
        },
        config_fields={
            "base_url",
            "model",
            "voice_id",
            "language",
            "speed",
            "output_format",
            "fallback",
        },
        provider_defaults={
            "model": "eleven_multilingual_v2",
            "output_format": "wav_24000",
        },
        normalize_model=_provider_model("eleven_multilingual_v2", ("eleven_",)),
        check=_check_elevenlabs,
    )
    TTS_PROVIDERS.register(
        "playht",
        _build_playht_tts,
        aliases=("playai",),
        required_env={
            "PLAYHT_API_KEY": "get API credentials at play.ht -> API Access",
            "PLAYHT_USER_ID": "copy the user ID beside your PlayHT API key",
        },
        provider_defaults={"model": "Play3.0-mini", "output_format": "wav"},
        normalize_model=_provider_model("Play3.0-mini", ("Play",)),
        config_fields={"model", "voice_id", "language", "speed", "output_format", "fallback"},
        check=_check_playht,
    )
    TTS_PROVIDERS.register(
        "rime",
        _build_rime_tts,
        required_env={
            "RIME_API_KEY": "get a key from the Rime developer dashboard",
        },
        provider_defaults={"model": "mistv3", "output_format": "wav"},
        normalize_model=_provider_model("mistv3", ("mist", "arcana")),
        config_fields={"model", "voice_id", "language", "speed", "output_format", "fallback"},
        check=_check_rime,
    )
    TTS_PROVIDERS.register(
        "hume",
        _build_hume_tts,
        aliases=("hume-octave",),
        required_env={
            "HUME_API_KEY": "get a key at platform.hume.ai -> API Keys",
        },
        provider_defaults={"output_format": "wav"},
        config_fields={"model", "voice_id", "language", "speed", "output_format", "fallback"},
        check=_check_hume,
    )


# ---------------------------------------------------------------------------
# Transport factories
# ---------------------------------------------------------------------------


def _build_null_transport(config, env: dict[str, str]):
    from sleeptalk.null_transport import NullTransport

    return NullTransport(config, env)


def _build_web_transport(config, env: dict[str, str]):
    from sleeptalk.transports.web import WebTransport

    return WebTransport(config, env)


def _build_discord_transport_v2(config, env: dict[str, str]):
    """Thin Discord transport adapter (r2-discord-adapter-refactor):
    transports/discord.py — session components injected, never built.

    This factory imports no app.*: the packaged bot starts from
    SleeptalkConfig and DiscordEnv. The transport owns guild/channel
    lifecycle and session injection."""
    from sleeptalk.transports.discord import DiscordTransport

    return DiscordTransport(config, env)


def _build_terminal_transport(config, env: dict[str, str]):
    from sleeptalk.transports.terminal import TerminalTransport

    return TerminalTransport(config, env)


# ---------------------------------------------------------------------------
# Agent factories
# ---------------------------------------------------------------------------


def _agent_cwd(config) -> str:
    from pathlib import Path
    import os

    workspace = config.workspace_dir.strip()
    if workspace:
        path = Path(workspace).expanduser()
        if not path.is_absolute():
            # Relative workspace_dir (the wizard writes ".") anchors to the
            # profile's project root, NOT the caller's cwd: the self-contained
            # folder is the unit, no matter where sleeptalk is invoked from.
            anchor = _profile_root(getattr(config, "source_path", ""))
            if anchor is not None:
                path = anchor / path
        path.mkdir(parents=True, exist_ok=True)
        return str(path.resolve())
    # Default to the current working directory — the standard CLI pattern.
    # The folder you run sleeptalk from IS the agent's workspace, so claude
    # picks up local .mcp.json, CLAUDE.md, .claude/settings.json, etc.
    return os.getcwd()


def _profile_root(source_path: str):
    """Project root for a loaded config: <root>/config/profiles/<slug>.toml
    (tier-3 repo layout) and <root>/profiles/<slug>.toml (tier-2
    SLEEPTALK_HOME layout) both map to <root>; anything else maps to the
    config file's own directory."""
    from pathlib import Path

    if not source_path.strip():
        return None
    path = Path(source_path).resolve()
    if path.parent.name == "profiles":
        if path.parent.parent.name == "config":
            return path.parent.parent.parent
        return path.parent.parent
    return path.parent


def _build_pi_agent(config, env: dict[str, str]):
    from sleeptalk.agents.pi_rpc import PiRPCClient

    return PiRPCClient(
        env=env,
        cwd=_agent_cwd(config),
        model=config.model,
        provider=config.provider if config.provider != "pi" else "",
        provider_slug=config.provider_slug,
        permission_mode=config.permission_mode or "auto",
        name=config.slug,
        pi_bin=config.pi_bin,
    )


def _locate_omp() -> str:
    """``omp`` when it's on PATH; otherwise where its installer put it."""
    import shutil

    from sleeptalk.registries import locate_binary

    if shutil.which("omp"):
        return "omp"
    return locate_binary("omp", OMP_BINARY_FALLBACKS) or "omp"


def _build_omp_agent(config, env: dict[str, str]):
    from sleeptalk.agents.pi_rpc import OmpRPCClient

    return OmpRPCClient(
        env=env,
        cwd=_agent_cwd(config),
        model=config.model,
        provider=config.provider if config.provider != "pi" else "",
        provider_slug=config.provider_slug,
        permission_mode=config.permission_mode or "auto",
        name=config.slug,
        # omp is a first-class registered runtime: its binary is the
        # registration's declared binary unless explicitly overridden, found
        # where its installer puts it when that isn't on PATH.
        pi_bin=config.pi_bin or _locate_omp(),
    )


def _build_acp_agent(config, env: dict[str, str]):
    from sleeptalk.agents.hermes_adapter import HermesACPClient
    from sleeptalk.agents.hermes_bridge import protected_config

    return HermesACPClient(
        env=env,
        cwd=_agent_cwd(config),
        name=config.slug,
        permission_mode=config.permission_mode or "auto",
        bridge_config=protected_config(config),
    )


def _build_openclaw_agent(config, env: dict[str, str]):
    from sleeptalk.agents.openclaw_gateway_adapter import (
        DEFAULT_GATEWAY_URL,
        OpenClawGatewayClient,
    )

    return OpenClawGatewayClient(
        url=(config.gateway_url or "").strip() or DEFAULT_GATEWAY_URL,
        token=env.get("OPENCLAW_GATEWAY_TOKEN", "").strip(),
        password=env.get("OPENCLAW_GATEWAY_PASSWORD", "").strip(),
        session_key=config.gateway_session_id,
        name=config.slug,
        cwd=_agent_cwd(config),
        permission_mode=config.permission_mode or "auto",
        ca_file=env.get("OPENCLAW_GATEWAY_CA_FILE", "").strip(),
    )


def _build_openclaw_acp_agent(config, env: dict[str, str]):
    """Run the existing generic ACP client with OpenClaw's documented command."""

    from sleeptalk.agents.hermes_adapter import HermesACPClient

    return HermesACPClient(
        env=env,
        cwd=_agent_cwd(config),
        name=config.slug,
        binary="openclaw",
        binary_fallback="",
        install_hint=(
            f"openclaw binary not found. Install with supported Node: npm i --engine-strict -g {install_spec('openclaw')} "
            "(then `openclaw onboard` once)"
        ),
        permission_mode=config.permission_mode or "auto",
    )


def _build_codex_agent(config, env: dict[str, str]):
    from sleeptalk.agents.codex_app_server import CodexAppServerClient

    return CodexAppServerClient(
        env=env,
        cwd=_agent_cwd(config),
        name=config.slug,
        model=config.model,
        permission_mode=config.permission_mode,
        resume_thread_id=config.gateway_session_id,
    )


def _build_claude_code_agent(config, env: dict[str, str]):
    from sleeptalk.agents.claude_code_adapter import ClaudeCodeClient

    return ClaudeCodeClient(
        env=env,
        cwd=_agent_cwd(config),
        name=config.slug,
        model=config.model,
        permission_mode=config.permission_mode or "skip",
        persona_instructions=config.persona_instructions,
    )


def _hermes_gateway_required_env(agent) -> dict[str, str]:
    url = getattr(agent, "gateway_url", "") or ""
    if not url.strip():
        return {}
    from sleeptalk.hermes_connect import SESSION_TOKEN_ENV, is_loopback_origin, parse_gateway_target

    target = parse_gateway_target(url)
    # A dashboard on this machine is asked for its current token at call time.
    if target.token or is_loopback_origin(target.origin):
        return {}
    return {
        SESSION_TOKEN_ENV: (
            "run `sleeptalk connect hermes` or add "
            f"{SESSION_TOKEN_ENV}=... to .env"
        ),
    }


def _hermes_conversation_context(agent, conversation_id: str) -> str:
    selected = conversation_id.strip() or agent.gateway_session_id.strip()
    if not selected:
        # No session to continue (a Hermes started per call, as setup saves
        # it): the gateway opens a new one when the call starts, as Claude does.
        return f"hermes:{agent.slug}:new-web-call"
    agent.gateway_session_id = selected
    return selected


def _claude_conversation_context(agent, conversation_id: str) -> str:
    return conversation_id.strip() or f"claude:{agent.slug}:new-web-call"


def _cursor_api_key_present() -> bool:
    """CURSOR_API_KEY in the process env or in a .env the launcher would load."""
    if os.environ.get("CURSOR_API_KEY", "").strip():
        return True
    from dotenv import dotenv_values

    from sleeptalk.home import resolve_layout

    candidates = [resolve_layout().env_path]
    for candidate in candidates:
        try:
            if candidate.is_file() and str(
                dotenv_values(candidate).get("CURSOR_API_KEY") or ""
            ).strip():
                return True
        except OSError:
            continue
    return False


def _build_cursor_agent(config, env: dict[str, str]):
    from sleeptalk.agents.cursor_sdk_adapter import CursorSdkClient

    return CursorSdkClient(
        cwd=_agent_cwd(config),
        name=config.slug,
        model=config.model,
        api_key=env.get("CURSOR_API_KEY", ""),
        agent_id=config.gateway_session_id,
        permission_mode=config.permission_mode or "auto",
    )


def _build_cursor_acp_agent(config, env: dict[str, str]):
    from sleeptalk.agents.cursor_acp import CursorAcpClient

    return CursorAcpClient(
        cwd=_agent_cwd(config),
        model=config.model,
        session_id=config.gateway_session_id or None,
        permission_mode=config.permission_mode or "auto",
        env=env,
    )


def _build_hermes_gateway_agent(config, env: dict[str, str]):
    from sleeptalk.agents.hermes_gateway_adapter import HermesGatewayClient
    from sleeptalk.agents.hermes_bridge import protected_config
    from sleeptalk.hermes_connect import resolve_session_token

    target_url = (config.gateway_url or "").strip()
    return HermesGatewayClient(
        env=env,
        base_url=target_url,
        api_key=env.get("HERMES_GATEWAY_API_KEY", ""),
        session_token=resolve_session_token(target_url, env),
        session_id=config.gateway_session_id,
        name=config.slug,
        cwd=_agent_cwd(config),
        permission_mode=config.permission_mode or "auto",
        bridge_config=protected_config(config),
    )


# ---------------------------------------------------------------------------
# STT factories
# ---------------------------------------------------------------------------
# STT/TTS factories take (ProviderSpec, env). The builder converts config
# sections into specs; the profile path in discord_agent.py already has specs.
# `env` carries secrets only.


class STTComponents:
    """What an STT registration builds: the REST fallback + streaming STT."""

    def __init__(self, *, rest, streaming) -> None:
        self.rest = rest
        self.streaming = streaming


def _build_deepgram_stt(spec, env: dict[str, str]):
    from sleeptalk.speech.deepgram import DeepgramSTT, DeepgramStreamingSTT

    api_key = env.get("DEEPGRAM_API_KEY", "")
    return STTComponents(
        rest=DeepgramSTT(spec, api_key=api_key),
        streaming=DeepgramStreamingSTT(spec, api_key=api_key),
    )


def _build_local_stt(spec, env: dict[str, str]):
    from sleeptalk.speech.local_stt import LocalSTT

    if not spec.model or spec.model == "moonshine-small-streaming-en":
        from sleeptalk.speech.local_streaming_stt import LocalStreamingSTT
        stt = LocalStreamingSTT(spec)
        return STTComponents(rest=stt, streaming=stt)
    return STTComponents(rest=LocalSTT(spec), streaming=None)


def _build_local_tts(spec, env: dict[str, str]):
    from sleeptalk.speech.local_tts import LocalTTS

    return LocalTTS(spec)


async def _check_local_stt(config, env: dict[str, str]) -> None:
    from sleeptalk import sysdeps
    from sleeptalk.builder import stt_model_for_provider
    from sleeptalk.speech.local_models import get_model, is_ready

    extra = sysdeps.missing_local_extra()
    if extra:
        raise RuntimeError(extra)
    model = get_model(stt_model_for_provider("local", config.model), "stt")
    if model.runtime == "moonshine" and sysdeps.missing_streaming_extra():
        raise RuntimeError(sysdeps.missing_streaming_extra())
    if not is_ready(model):
        raise RuntimeError(f"{model.label} is not downloaded; choose Local speech in sleeptalk settings.")


async def _check_local_tts(config, env: dict[str, str]) -> None:
    from sleeptalk import sysdeps
    from sleeptalk.builder import tts_model_for_provider
    from sleeptalk.speech.local_models import is_ready, resolve_tts_model

    extra = sysdeps.missing_local_extra()
    if extra:
        raise RuntimeError(extra)
    model = resolve_tts_model(tts_model_for_provider("local", config.model), config.voice_id)
    if not is_ready(model):
        raise RuntimeError(f"{model.label} is not downloaded; choose Local speech in sleeptalk settings.")


def _build_elevenlabs_stt(spec, env: dict[str, str]):
    from sleeptalk.speech.elevenlabs_stt import ElevenLabsSTT
    from sleeptalk.speech.elevenlabs_streaming_stt import ElevenLabsStreamingSTT

    api_key = env.get("ELEVENLABS_API_KEY", "")
    return STTComponents(
        rest=ElevenLabsSTT(spec, api_key=api_key),
        streaming=(ElevenLabsStreamingSTT(spec, api_key=api_key)
                   if spec.model in {"", "scribe_v2", "scribe_v2_realtime"} else None),
    )


def _build_cartesia_stt(spec, env: dict[str, str]):
    from sleeptalk.speech.cartesia_stt import CartesiaSTT, CartesiaStreamingSTT

    api_key = env.get("CARTESIA_API_KEY", "")
    return STTComponents(
        rest=CartesiaSTT(spec, api_key=api_key),
        streaming=CartesiaStreamingSTT(spec, api_key=api_key),
    )


# ---------------------------------------------------------------------------
# TTS factories
# ---------------------------------------------------------------------------


def _register_openai_audio_stt() -> None:
    """OpenRouter, Groq, Together, Venice: one key, OpenAI-shaped audio routes."""
    from sleeptalk.registries import STT_PROVIDERS
    from sleeptalk.speech.openai_audio import PRESETS

    for preset in PRESETS.values():
        STT_PROVIDERS.register(
            preset.name,
            _build_openai_audio_stt,
            required_env={preset.env: f"get a key at {preset.console} and add {preset.env}=... to .env (the same key speaks)"},
            config_fields={"base_url", "model", "language", "keyterms", "rest_timeout_seconds",
                           "streaming_timeout_seconds", "endpointing_ms", "pause_ahead_ms"},
            provider_defaults={"model": preset.stt_model},
            normalize_model=_any_model(preset.stt_model),
            check=_check_openai_audio_stt,
        )


def _register_openai_audio_tts() -> None:
    from sleeptalk.registries import TTS_PROVIDERS
    from sleeptalk.speech.openai_audio import PRESETS

    for preset in PRESETS.values():
        TTS_PROVIDERS.register(
            preset.name,
            _build_openai_audio_tts,
            required_env={preset.env: f"get a key at {preset.console} and add {preset.env}=... to .env (the same key listens)"},
            config_fields={"base_url", "model", "voice_id", "language", "speed", "output_format", "fallback"},
            provider_defaults={"model": preset.tts_model},
            normalize_model=_any_model(preset.tts_model),
            check=_check_openai_audio_tts,
        )


# Launch defaults of the direct providers: left in a profile that moved to an
# aggregator they are not a choice, so they snap to the aggregator's default.
_FOREIGN_DEFAULTS = frozenset({
    "nova-3", "nova-2", "ink-2", "ink-whisper", "scribe_v2", "moonshine-tiny-en", "moonshine-small-streaming-en",
    "sonic-3.6", "sonic-3", "sonic-2", "eleven_multilingual_v2", "flux-haley-en", "flux-hannah-en",
    "piper-lessac-medium", "kokoro-en", "pocket-tts-en",
})


def _any_model(default: str):
    """These services route many vendors' models by slug: any other non-empty value is a choice."""
    def normalize(model: str) -> str:
        selected = (model or "").strip()
        return default if not selected or selected in _FOREIGN_DEFAULTS else selected
    return normalize


def _build_openai_audio_stt(spec, env: dict[str, str]):
    """Batch listening, with optional browser preview hooks for aggregators."""
    from sleeptalk.speech.openai_audio import preset_for
    from sleeptalk.speech.batch_preview_stt import BatchPreviewSTT

    return STTComponents(rest=BatchPreviewSTT(spec, api_key=env.get(preset_for(spec.provider).env, "")), streaming=None)


def _build_openai_audio_tts(spec, env: dict[str, str]):
    from sleeptalk.speech.openai_audio import OpenAIAudioTTS, preset_for

    return OpenAIAudioTTS(spec, api_key=env.get(preset_for(spec.provider).env, ""))


async def _check_openai_audio_stt(config, env: dict[str, str]) -> None:
    from sleeptalk.speech.common import SpeechError
    from sleeptalk.speech.openai_audio import check_key, preset_for

    preset = preset_for(config.provider)
    try:
        await check_key(preset, env.get(preset.env, ""), config.base_url)
    except SpeechError as exc:
        raise RuntimeError(str(exc)) from None


async def _check_openai_audio_tts(config, env: dict[str, str]) -> None:
    from sleeptalk.speech.common import SpeechError
    from sleeptalk.speech.openai_audio import check_key, preset_for

    preset = preset_for(config.provider)
    try:
        await check_key(preset, env.get(preset.env, ""), config.base_url)
    except SpeechError as exc:
        raise RuntimeError(str(exc)) from None


def _voice_id_with_env_fallback(spec, env: dict[str, str], env_var: str):
    if spec.voice_id.strip():
        return spec
    from dataclasses import replace

    return replace(spec, voice_id=env.get(env_var, "").strip())


def _build_deepgram_tts(spec, env: dict[str, str]):
    from sleeptalk.speech.deepgram_tts import DeepgramTTS

    return DeepgramTTS(spec, api_key=env.get("DEEPGRAM_API_KEY", ""))


def _build_cartesia_tts(spec, env: dict[str, str]):
    from sleeptalk.speech.cartesia import CartesiaTTS

    spec = _voice_id_with_env_fallback(spec, env, "CARTESIA_VOICE_ID")
    return CartesiaTTS(spec, api_key=env.get("CARTESIA_API_KEY", ""))


def _build_elevenlabs_tts(spec, env: dict[str, str]):
    from sleeptalk.speech.elevenlabs import ElevenLabsTTS

    spec = _voice_id_with_env_fallback(spec, env, "ELEVENLABS_VOICE_ID")
    return ElevenLabsTTS(spec, api_key=env.get("ELEVENLABS_API_KEY", ""))


def _build_playht_tts(spec, env: dict[str, str]):
    from sleeptalk.speech.playht import PlayHTTTS

    return PlayHTTTS(
        spec,
        api_key=env.get("PLAYHT_API_KEY", ""),
        user_id=env.get("PLAYHT_USER_ID", ""),
    )


def _build_rime_tts(spec, env: dict[str, str]):
    from sleeptalk.speech.rime import RimeTTS

    return RimeTTS(spec, api_key=env.get("RIME_API_KEY", ""))


def _build_hume_tts(spec, env: dict[str, str]):
    from sleeptalk.speech.hume import HumeTTS

    return HumeTTS(spec, api_key=env.get("HUME_API_KEY", ""))


# ---------------------------------------------------------------------------
# Connectivity checks (cheap, authenticated, read-only)
# ---------------------------------------------------------------------------


async def _http_auth_check(url: str, headers: dict[str, str], label: str) -> None:
    import aiohttp

    async with aiohttp.ClientSession() as session, session.get(
        url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)
    ) as response:
        if response.status in (200, 204):
            return
        if response.status in (401, 403):
            raise RuntimeError(f"{label} rejected the API key (HTTP {response.status}).")
        raise RuntimeError(f"{label} connectivity check failed (HTTP {response.status}).")


async def _check_deepgram(config, env: dict[str, str]) -> None:
    from sleeptalk.builder import stt_spec
    from sleeptalk.providers import provider_http_url

    await _http_auth_check(
        provider_http_url(
            stt_spec(config),
            "https://api.deepgram.com",
            "/v1/projects",
        ),
        {"Authorization": f"Token {env.get('DEEPGRAM_API_KEY', '')}"},
        "Deepgram",
    )


def _require_voice_id(config, env: dict[str, str], env_var: str, label: str) -> None:
    """A provider that speaks in a chosen voice cannot speak without one.

    The key checks pass with an empty voice_id, so a profile `sleeptalk init`
    has just written reports healthy and then raises SpeechError on its first
    spoken word — silence with the diagnosis buried in a JSONL log.
    """
    from sleeptalk.builder import tts_spec

    spec = _voice_id_with_env_fallback(tts_spec(config), env, env_var)
    if not spec.voice_id.strip():
        raise RuntimeError(
            f"no {label} voice selected — set [tts] voice_id in the profile "
            f"or export {env_var}"
        )


async def _check_elevenlabs_stt(config, env: dict[str, str]) -> None:
    """A tiny real transcription: ElevenLabs' list endpoints reject restricted keys."""
    from sleeptalk.builder import stt_spec
    from sleeptalk.speech.common import SpeechError
    from sleeptalk.stt_roundtrip import elevenlabs_roundtrip

    try:
        await elevenlabs_roundtrip(stt_spec(config), env.get("ELEVENLABS_API_KEY", ""), timeout=15.0)
    except SpeechError as exc:
        raise RuntimeError(str(exc)) from None


async def _check_cartesia_stt(config, env: dict[str, str]) -> None:
    from sleeptalk.builder import stt_spec
    from sleeptalk.providers import provider_http_url

    await _http_auth_check(
        provider_http_url(stt_spec(config), "https://api.cartesia.ai", "/voices"),
        {
            "Cartesia-Version": "2026-03-01",
            "Authorization": f"Bearer {env.get('CARTESIA_API_KEY', '')}",
        },
        "Cartesia",
    )


async def _check_deepgram_tts(config, env: dict[str, str]) -> None:
    from sleeptalk.builder import tts_spec
    from sleeptalk.providers import provider_http_url

    await _http_auth_check(
        provider_http_url(tts_spec(config), "https://api.deepgram.com", "/v1/projects"),
        {"Authorization": f"Token {env.get('DEEPGRAM_API_KEY', '')}"},
        "Deepgram",
    )


async def _check_cartesia(config, env: dict[str, str]) -> None:
    from sleeptalk.builder import tts_spec
    from sleeptalk.providers import provider_http_url

    _require_voice_id(config, env, "CARTESIA_VOICE_ID", "Cartesia")
    await _http_auth_check(
        provider_http_url(
            tts_spec(config),
            "https://api.cartesia.ai",
            "/voices",
        ),
        {
            "Cartesia-Version": "2026-03-01",
            "Authorization": f"Bearer {env.get('CARTESIA_API_KEY', '')}",
        },
        "Cartesia",
    )


async def _check_elevenlabs(config, env: dict[str, str]) -> None:
    """Prove the key with one real (tiny) TTS request.

    A GET on /v1/models or /v1/user rejects *restricted* keys that are
    perfectly valid for speech — the only capability the bot needs — so the
    check exercises text-to-speech itself (voice 21m00... = Rachel, present
    on every account). Costs a couple of characters.
    """
    import aiohttp

    from sleeptalk.builder import tts_spec
    from sleeptalk.providers import provider_http_url

    _require_voice_id(config, env, "ELEVENLABS_VOICE_ID", "ElevenLabs")
    async with aiohttp.ClientSession() as session, session.post(
        provider_http_url(
            tts_spec(config),
            "https://api.elevenlabs.io",
            "/v1/text-to-speech/21m00Tcm4TlvDq8ikWAM",
        ),
        params={"output_format": "mp3_44100_32"},
        json={"text": "Hi", "model_id": "eleven_multilingual_v2"},
        headers={
            "xi-api-key": env.get("ELEVENLABS_API_KEY", ""),
            "Content-Type": "application/json",
        },
        timeout=aiohttp.ClientTimeout(total=15),
    ) as response:
        if response.status == 200:
            return
        body = (await response.text())[:300]
        if response.status in (401, 403):
            raise RuntimeError(
                f"ElevenLabs rejected the API key (HTTP {response.status}): {body}"
            )
        raise RuntimeError(
            f"ElevenLabs connectivity check failed (HTTP {response.status}): {body}"
        )


async def _check_playht(config, env: dict[str, str]) -> None:
    await _http_auth_check(
        "https://api.play.ht/api/v2/voices",
        {
            "Authorization": env.get("PLAYHT_API_KEY", ""),
            "X-USER-ID": env.get("PLAYHT_USER_ID", ""),
        },
        "PlayHT",
    )


async def _check_rime(config, env: dict[str, str]) -> None:
    # Rime has no cheap authenticated account endpoint; the wizard and
    # doctor prove this key with the selected voice's synthesis request.
    if not env.get("RIME_API_KEY", "").strip():
        raise RuntimeError("Rime API key is missing.")


async def _check_hume(config, env: dict[str, str]) -> None:
    await _http_auth_check(
        "https://api.hume.ai/v0/tts/voices?provider=HUME_AI&page_size=1",
        {"X-Hume-Api-Key": env.get("HUME_API_KEY", "")},
        "Hume",
    )
