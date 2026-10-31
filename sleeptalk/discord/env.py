from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from sleeptalk.config import SleeptalkConfig

_RUNTIME_KIND = {
    "pi": "pi-direct",
    "omp": "omp-direct",
    "acp": "hermes-acp",
    "openclaw": "openclaw-acp",
    "claude_code": "claude-code",
    "hermes_gateway": "hermes-gateway",
    "codex": "codex-app-server",
    "codex_app_server": "codex-app-server",
    "codex-app-server": "codex-app-server",
}


def _env_flag(raw: str | None, default: bool = False) -> bool:
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _channel_ids(raw: object) -> str:
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    return ",".join(str(item) for item in raw)


@dataclass(frozen=True, repr=False)
class DiscordEnv:
    token: str
    rendezvous_url: str = ""
    profile_path: str = ""
    transcript_debug: bool = False
    voice_text_echo: bool = True
    voice_chat_log_enabled: bool = False
    voice_chat_log_path: str = ""
    opus_library: str = ""

    @classmethod
    def from_mapping(cls, config: SleeptalkConfig, env: Mapping[str, str]) -> DiscordEnv:
        token = env.get(config.transport.token_env, "").strip()
        if not token:
            raise RuntimeError(
                f"{config.transport.token_env} missing — cannot start Discord."
            )
        return cls(
            token=token,
            rendezvous_url=env.get("SLEEPTALK_RENDEZVOUS_URL", "").strip(),
            profile_path=env.get("SLEEPTALK_PROFILE", "").strip(),
            transcript_debug=_env_flag(env.get("DISCORD_TRANSCRIPT_DEBUG")),
            voice_text_echo=_env_flag(env.get("DISCORD_VOICE_TEXT_ECHO"), default=True),
            voice_chat_log_enabled=_env_flag(env.get("VOICE_CHAT_LOG_ENABLED")),
            voice_chat_log_path=env.get("VOICE_CHAT_LOG_PATH", "").strip(),
            opus_library=(
                env.get("DISCORD_OPUS_LIBRARY", "").strip()
                or config.transport.opus_library
            ),
        )

    def __repr__(self) -> str:
        return "DiscordEnv(token=<redacted>)"


@dataclass(frozen=True)
class DiscordSurface:
    guild_id: str = ""
    voice_only: bool = False
    presence_auto_join: bool = False
    native_voice_enabled: bool = True
    allowed_channel_ids: str = ""
    authorization_required: bool = True
    operator_ids: str = ""
    speaker_ids: str = ""
    listener_ids: str = ""
    post_replies: bool = True
    post_caller_speech: bool = False


@dataclass(frozen=True)
class DiscordBotProfile:
    slug: str
    name: str
    persona_instructions: str
    participation_mode: str
    addressable_name: str = ""
    proactive_triggers: tuple[str, ...] = ()
    workspace_dir: str = ""
    runtime_kind: str = ""
    discord: DiscordSurface = DiscordSurface()

    @classmethod
    def from_config(cls, config: SleeptalkConfig) -> DiscordBotProfile:
        agent = config.agent
        transport = config.transport
        agent_type = getattr(agent, "type", "pi") or "pi"
        return cls(
            slug=agent.slug,
            name=agent.name,
            persona_instructions=getattr(agent, "persona_instructions", "") or "",
            participation_mode=getattr(agent, "participation_mode", None)
            or "conversationalist",
            addressable_name=getattr(agent, "addressable_name", "") or "",
            proactive_triggers=tuple(getattr(agent, "proactive_triggers", None) or ()),
            workspace_dir=getattr(agent, "workspace_dir", "") or "",
            runtime_kind=_RUNTIME_KIND.get(agent_type, agent_type),
            discord=DiscordSurface(
                authorization_required=bool(getattr(transport, "authorization_required", True)),
                operator_ids=_channel_ids(getattr(transport, "operator_ids", "")),
                speaker_ids=_channel_ids(getattr(transport, "speaker_ids", "")),
                listener_ids=_channel_ids(getattr(transport, "listener_ids", "")),
                guild_id=getattr(transport, "guild_id", "") or "",
                voice_only=bool(getattr(transport, "voice_only", False)),
                presence_auto_join=bool(getattr(transport, "presence_auto_join", False)),
                native_voice_enabled=bool(
                    getattr(transport, "native_voice_enabled", True)
                ),
                allowed_channel_ids=_channel_ids(
                    getattr(transport, "allowed_channel_ids", "")
                ),
                post_replies=bool(getattr(transport, "post_replies", True)),
                post_caller_speech=bool(
                    getattr(transport, "post_caller_speech", False)
                ),
            ),
        )


@dataclass(frozen=True)
class DiscordBotSettings:
    discord_transcript_debug: bool = False
    voice_chat_log_enabled: bool = False
    voice_chat_log_path: str = ""
    discord_voice_text_echo: bool = True

    @classmethod
    def from_env(cls, env: DiscordEnv) -> DiscordBotSettings:
        return cls(
            discord_transcript_debug=env.transcript_debug,
            voice_chat_log_enabled=env.voice_chat_log_enabled,
            voice_chat_log_path=env.voice_chat_log_path,
            discord_voice_text_echo=env.voice_text_echo,
        )
