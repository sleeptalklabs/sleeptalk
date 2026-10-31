"""sleeptalk.toml schema + loader.

TOML is the sole source for session composition and voice policy; env vars
carry secrets only. Unknown keys are a hard error naming the key and listing
valid options. Every field except discriminators has a sane default, so a
minimal config ([transport] + [agent]) runs.

Layered profile resolution (``load_profile_config``)
----------------------------------------------------
Precedence, lowest to highest:

1. Schema defaults (pydantic field defaults in this module).
2. Root ``sleeptalk.toml`` — shared composition + policy defaults.
3. ``config/profiles/<slug>.toml`` — per-bot overlay. Deep-merged over the
   root document: mappings merge field-by-field (a profile may override a
   single ``[policy.turns]`` key without restating the section); **lists and
   scalars replace wholesale, never element-merge** (e.g. a profile
   ``tts.fallback`` or ``filler_general`` list fully replaces the root list).
4. Environment / ``.env`` — secrets only (API keys, bot tokens referenced by
   ``transport.token_env``). Env never overrides a TOML composition/policy
   field.

A missing profile TOML for a launched slug is a hard startup error — there is
no silent fallback to env or the agent store for migrated composition fields.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal, TYPE_CHECKING

import tomllib
from dotenv import dotenv_values
from dotenv.variables import parse_variables
from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError, field_validator, model_validator

logger = logging.getLogger(__name__)

from sleeptalk.runtime_binding import RuntimeBinding
from sleeptalk.agent_auth import AgentAuthConfig

if TYPE_CHECKING:
    from sleeptalk.credential_store import CredentialStore


DEFAULT_CONFIG_PATH = Path("sleeptalk.toml")
EXAMPLE_CONFIG_PATH = Path("sleeptalk.example.toml")
_SHARED_ROOT_NAMES = ("sleeptalk.toml", "sleeptalk.example.toml")


@dataclass(frozen=True)
class ResolvedConfigDocument:
    """One selected config document after applying its shared root, if any."""

    raw: dict
    source_path: Path
    root_path: Path
    profile_path: Path | None


def default_config_candidates() -> list[Path]:
    """Search order for the default config: cwd, then the repo root."""
    repo_root = Path(__file__).resolve().parents[1]
    return [
        Path("sleeptalk.toml"),
        repo_root / "sleeptalk.toml",
        repo_root / EXAMPLE_CONFIG_PATH,
    ]


def shared_root_for_project(project_root: Path) -> Path:
    """Operator sleeptalk.toml if present, else the tracked example."""
    for name in _SHARED_ROOT_NAMES:
        candidate = project_root / name
        if candidate.is_file():
            return candidate
    return project_root / DEFAULT_CONFIG_PATH


def packaged_example_root() -> Path:
    """The voice-standard example shipped in the wheel, else the repo file."""
    packaged = Path(__file__).with_name("sleeptalk.example.toml")
    if packaged.is_file():
        return packaged
    return Path(__file__).resolve().parents[1] / EXAMPLE_CONFIG_PATH


def project_root_for_profiles(profiles_dir: Path) -> Path | None:
    """Home or repo root that owns a ``profiles/`` directory."""
    if profiles_dir.name != "profiles":
        return None
    parent = profiles_dir.parent
    return parent.parent if parent.name == "config" else parent


def ensure_shared_root(project_root: Path) -> Path | None:
    """Write sleeptalk.toml from the packaged example when the project has none."""
    if shared_root_for_project(project_root).is_file():
        return None
    source = packaged_example_root()
    if not source.is_file():
        raise ConfigError(
            [
                f"{source} not found — this sleeptalk install is missing "
                "sleeptalk.example.toml."
            ]
        )
    dest = project_root / DEFAULT_CONFIG_PATH
    dest.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    return dest


def load_config_or_default(
    path: str | Path | None = None,
    *,
    check_env: bool = False,
    check_binaries: bool = False,
) -> "SleeptalkConfig":
    """Legacy entrypoints: TOML when present (cwd or repo root), else defaults.

    A present-but-invalid file is still a hard error.
    """
    if path is not None:
        return load_resolved_config(
            path, check_env=check_env, check_binaries=check_binaries
        )
    for candidate in default_config_candidates():
        if candidate.is_file():
            return load_config(
                candidate, check_env=check_env, check_binaries=check_binaries
            )
    return SleeptalkConfig()

_ENDPOINTING_MODES = ("relaxed", "balanced", "aggressive")
_BARGE_IN_MODES = ("off", "soft", "immediate")
_PARTICIPATION_MODES = ("conversationalist", "attendee", "proactive")
_ROOM_AGENT_FIELDS = frozenset(
    {"participation_mode", "addressable_name", "proactive_triggers"}
)


class ConfigError(Exception):
    """All config problems, collected in one pass."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__(
            "sleeptalk config has "
            f"{len(problems)} problem(s):\n" + "\n".join(f"  - {p}" for p in problems)
        )


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------


class CascadeRouteConfig(_Strict):
    """The established STT -> agent -> TTS route."""

    type: Literal["cascade"] = "cascade"


class HybridRouteConfig(_Strict):
    """OpenAI Realtime in front of the configured specialist agent."""

    type: Literal["hybrid"] = "hybrid"
    provider: Literal["openai-realtime"] = "openai-realtime"
    model: str = "gpt-realtime-2.1"
    voice: str = "marin"
    speech_model: str = "gpt-4o-mini-tts"
    transcription_model: str = "gpt-4o-mini-transcribe"
    language: str = "en-US"
    base_url: str = "https://api.openai.com/v1"
    specialist_tool: bool = True
    speculative_audio: bool = True
    # Play Realtime's reply before the transcript exists; needs speculative_audio.
    audio_first: bool = True

    @field_validator("base_url")
    @classmethod
    def _safe_base_url(cls, value: str) -> str:
        from sleeptalk.providers import check_provider_base
        return check_provider_base(value)

    @model_validator(mode="after")
    def check_speculative_audio(self) -> HybridRouteConfig:
        if self.speculative_audio and not self.specialist_tool:
            raise ValueError("speculative_audio requires specialist_tool")
        return self


RouteConfig = Annotated[
    CascadeRouteConfig | HybridRouteConfig,
    Field(discriminator="type"),
]


class TransportConfig(_Strict):
    type: str = "discord"
    options: dict[str, JsonValue] = Field(default_factory=dict)
    # discord
    guild_id: str = ""
    client_id: str = ""
    allowed_channel_ids: str = ""
    # Closed unless a profile explicitly opts out with authorization_required = false.
    authorization_required: bool = True
    operator_ids: str = ""
    speaker_ids: str = ""
    listener_ids: str = ""
    # Name of the env var holding the Discord bot token (secrets never in TOML).
    token_env: str = "DISCORD_BOT_TOKEN"
    opus_library: str = ""
    dave_library: str = ""
    native_voice_enabled: bool = True
    voice_only: bool = False
    # Summon-only by default: the bot joins voice when called (/<slug> join),
    # not merely because a human appeared in a channel.
    presence_auto_join: bool = False
    # discord: write the call into the voice channel's own chat, one message
    # per finished agent turn, so the conversation is readable and not only
    # audible. The caller's transcribed speech is opt-in on top: the words
    # were already said out loud, and on a shared channel it doubles the
    # volume. Neither ever reaches a wider audience than the voice channel.
    post_replies: bool = True
    post_caller_speech: bool = False
    debug: bool = False
    # terminal: explicit device selection (name or index); empty = host default.
    # Invalid selection surfaces an actionable AudioDeviceError at startup
    # rather than silently falling back to another device.
    input_device: int | str = ""
    output_device: int | str = ""
    # terminal: "headphones" or "speakers" (empty = speakers).
    # Headphones mean the mic cannot hear the agent, so barge-in can run the
    # Silero speech detector web and Discord use — a beep or a door slam no
    # longer cuts the agent off. On speakers that same detector would hear the
    # agent's own voice and interrupt every sentence, so the default stays
    # energy-based. SLEEPTALK_TERMINAL_FULL_DUPLEX still overrides this.
    capture_mode: str = ""
    # null
    log_path: str = ""


class AgentConfig(_Strict):
    auth: AgentAuthConfig | None = None
    type: str = "pi"
    options: dict[str, JsonValue] = Field(default_factory=dict)
    name: str = "sleeptalk"
    slug: str = "sleeptalk"
    persona_instructions: str = ""
    # Path to a text/markdown persona file, for TOML-native profiles. Used
    # only when persona_instructions is empty (and, for migrated bots, the
    # agent store remains the persona source — see profile_from_config).
    persona_file: str = ""
    # pi / omp / acp / openclaw / claude_code / codex
    workspace_dir: str = ""
    # pi / omp (empty = the registration's declared binary)
    pi_bin: str = ""
    model: str = ""
    provider: str = "pi"
    provider_slug: str = ""
    # acp / openclaw / claude_code: how tool-permission prompts are handled.
    # claude_code: "skip" (default; --dangerously-skip-permissions) or a Claude
    #   --permission-mode value.
    # acp / openclaw: "auto" (default; auto-approve) or "deny".
    permission_mode: str = ""
    gateway_url: str = ""
    gateway_session_id: str = ""
    # Empty = family policy (gateway, then native, then ACP).
    runtime: str = ""
    # fallback (default) or fail when a preferred runtime is incompatible.
    on_incompatible: str = "fallback"
    # Set by the loader (copied from SleeptalkConfig.source_path); not part
    # of the TOML schema. Lets relative workspace_dir values (the wizard
    # writes ".") resolve against the profile's project root instead of
    # the caller's cwd.
    source_path: str = ""
    # Room policy. Applies to every agent type; excluded from per-runtime
    # config_fields so a profile can set them without listing them on each
    # registration.
    participation_mode: str = "conversationalist"
    addressable_name: str = ""
    proactive_triggers: list[str] = Field(default_factory=list)

    @field_validator("gateway_url")
    @classmethod
    def _safe_gateway_url(cls, value: str) -> str:
        from sleeptalk.providers import check_gateway_address
        return check_gateway_address(value) if value.strip() else value


class STTConfig(_Strict):
    provider: str = "deepgram"
    options: dict[str, JsonValue] = Field(default_factory=dict)
    base_url: str = ""
    model: str = "nova-3"
    language: str = "en-US"
    keyterms: str = ""
    rest_timeout_seconds: float = 8.0
    streaming_timeout_seconds: float = 1.4
    # Batch-only STT (no streaming socket): start transcribing this far into
    # a pause, so the round trip overlaps the endpoint wait. The early text
    # is used only when nothing but quiet follows; it never ends a turn.
    # 0 turns it off.
    pause_ahead_ms: int = Field(default=300, ge=0, le=2000)
    # Deepgram streaming endpointing: silence Deepgram needs before emitting
    # speech_final. speech_final promotes borderline transcripts to
    # "complete" in our endpoint decision, so a low value commits turns on
    # mid-sentence pauses and cuts trailing words (2026-08-15 deepre: ~1s
    # clause pauses finalized mid-thought). 1000 = prior default.
    endpointing_ms: int = 1000

    @field_validator("base_url")
    @classmethod
    def _safe_base_url(cls, value: str) -> str:
        from sleeptalk.providers import check_provider_base
        return check_provider_base(value) if value.strip() else value


class TTSSpecConfig(_Strict):
    provider: str
    options: dict[str, JsonValue] = Field(default_factory=dict)
    base_url: str = ""
    model: str = ""
    voice_id: str = ""
    language: str = ""
    speed: float | None = None
    output_format: str = ""

    @field_validator("base_url")
    @classmethod
    def _safe_base_url(cls, value: str) -> str:
        from sleeptalk.providers import check_provider_base
        return check_provider_base(value) if value.strip() else value


class TTSConfig(_Strict):
    provider: str = "cartesia"
    options: dict[str, JsonValue] = Field(default_factory=dict)
    base_url: str = ""
    model: str = ""  # provider default applies when empty
    voice_id: str = ""
    language: str = ""
    speed: float = 1.0
    output_format: str = ""
    fallback: list[TTSSpecConfig] = []

    @field_validator("base_url")
    @classmethod
    def _safe_base_url(cls, value: str) -> str:
        from sleeptalk.providers import check_provider_base
        return check_provider_base(value) if value.strip() else value


class EndpointingOverrides(_Strict):
    """Raw overrides for AdaptiveEndpointingConfig, applied after the preset."""

    enabled: bool = True
    responsive_mode_ms: int = 5_000
    monologue_mode_ms: int = 15_000
    completion_threshold: float = 0.72
    complete_silence_ms: int = 450
    balanced_complete_silence_ms: int = 750
    monologue_complete_silence_ms: int = 850
    uncertain_silence_ms: int = 1_800
    incomplete_silence_ms: int = 3_500
    monologue_incomplete_silence_ms: int = 5_000
    filler_reset_ms: int = 3_500
    semantic_complete_silence_ms: int = 650
    early_transcript: bool = True


class TurnPolicyConfig(_Strict):
    """[policy.turns] — turn-taking VAD thresholds."""

    vad_threshold: int = 500
    interrupt_min_ms: int = 250
    bot_grace_ms: int = 400
    silero_confidence: float = 0.70
    silero_start_ms: int = 220
    silero_stop_ms: int = 900
    silero_min_volume: float = 0.005
    post_interrupt_cooldown_ms: int = 1200
    # Duck-on-VAD: pause playback on early speech signs (reversible), resume
    # on quiet; the full barge-in gate still decides cancellation.
    duck_on_vad: bool = True
    duck_min_ms: int = 200
    duck_release_ms: int = 600
    duck_min_volume: float = 0.03
    duck_confidence: float = 0.70
    vad_profile: str = "default"
    debug: bool = False
    max_wait_seconds: float = 120.0
    # Legacy settings accepted for profile compatibility only. Long-running
    # work now stays in its original request; keepalive progress does not
    # abort it or ask for permission to continue.
    long_task_check_in_seconds: float = 180.0
    long_task_auto_continue_seconds: float = 30.0


class CapturePolicyConfig(_Strict):
    """[policy.capture] — utterance capture gates for the native sink."""

    silence_ms: int = 450
    min_utterance_ms: int = 450
    min_transcript_words: int = 4
    # 2026-08-14: 500 -> 300. DeepRe live-call probes (capture_vad_dropout
    # events) showed real quiet-speech segments at RMS 300-500 rejected by
    # both capture legs mid-utterance, freezing the buffer and truncating
    # the committed transcript at that point. 300 stays well above the
    # measured room-noise floor (~RMS 10-30).
    rms_threshold: int = 300
    # hybrid = union of Silero and the RMS floor: quiet speech Silero hears
    # is captured, energetic speech Silero doubts is captured. Silero failure
    # degrades loudly to RMS, never silently.
    vad_mode: str = "hybrid"
    # In "compare" mode, which leg drives utterance capture (the other only
    # logs). "hybrid" ignores this (union of both legs).
    compare_primary: str = "silero"
    # Audio-native turn detection (Smart Turn v3, bundled ONNX): consulted
    # only when the text heuristic is uncertain — accelerates genuine
    # end-of-turn out of the 1.8-5s buckets without interrupting.
    smart_turn: bool = True
    smart_turn_threshold: float = 0.6
    vad_confidence: float = 0.78
    vad_start_ms: int = 250
    vad_stop_ms: int = 900
    vad_min_volume: float = 0.005  # lowered with rms_threshold (see above)
    min_listen_utterance_ms: int = 450


class InterruptPolicyConfig(_Strict):
    """[policy.interrupt] Floor grace, hold, cooldown, and mode."""

    enabled: bool = True
    mode: Literal["off", "soft", "immediate"] = "immediate"
    min_duration_ms: int = 150
    grace_ms: int = 400
    gap_hold_ms: int = 600
    cooldown_ms: int = 1200
    min_volume: float = 0.03


# [policy.turns] keys from before the public release -> [policy.interrupt].
_RETIRED_TURN_KEYS = {
    "silero_barge_min_ms": "min_duration_ms",
    "silero_barge_min_volume": "min_volume",
}


class PolicyConfig(_Strict):
    @model_validator(mode="before")
    @classmethod
    def retire_local_experiment(cls, value):
        # Old profiles remain valid, but their retired toggle no longer controls
        # standard local speech behavior or appears in newly serialized config.
        if isinstance(value, dict) and "experimental_responsiveness" in value:
            value = dict(value)
            if type(value.pop("experimental_responsiveness")) is not bool:
                raise ValueError("experimental_responsiveness must be a boolean")
        return value

    @model_validator(mode="before")
    @classmethod
    def retire_turn_barge_keys(cls, value):
        # Configs from before the public release set the barge-in floor in
        # [policy.turns]; it lives in [policy.interrupt] now. Carry the old
        # keys over (an explicit [policy.interrupt] value wins) rather than
        # refuse to start a call.
        turns = value.get("turns") if isinstance(value, dict) else None
        if not isinstance(turns, dict) or not any(old in turns for old in _RETIRED_TURN_KEYS):
            return value
        value, turns = dict(value), dict(turns)
        interrupt = value.get("interrupt")
        interrupt = dict(interrupt) if isinstance(interrupt, dict) else {}
        for old, new in _RETIRED_TURN_KEYS.items():
            if old in turns:
                interrupt.setdefault(new, turns.pop(old))
        value["turns"], value["interrupt"] = turns, interrupt
        return value

    endpointing: Literal["relaxed", "balanced", "aggressive"] = "balanced"
    fillers: bool = True
    filler_delay_ms: int = 800
    first_token_budget_ms: int = 800
    filler_general: list[str] = [
        "Let me check that.",
        "One second, I am looking.",
        "I am checking now.",
        "Let me pull that together.",
    ]
    filler_web_search: list[str] = [
        "Let me check that.",
        "Checking now.",
        "I'll look that up.",
        "One second, checking.",
        "Let me verify that.",
    ]
    barge_in_min_chars: int = 4
    early_barge_in_ms: int = 900
    # DEPRECATED, kept only so existing profiles still parse: the proactive
    # "want me to continue?" offer was removed 2026-08-14. Interrupted-answer
    # context is still tracked and the user can just ask the bot to continue.
    resume_after_interrupt: bool = True
    resume_offer_text: str = "I was partway through that. Want me to continue?"
    # Fragment holding (see VoicePolicy). Must match VoicePolicy's default —
    # from_config copies this value over it (2026-08-15: the VoicePolicy
    # default was lowered to 1500 but this shadow copy kept live bots at
    # 4000).
    fragment_hold_ms: int = 1500
    fragment_barge_window_ms: int = 10000
    # Grace-period abort: speech resuming within this window of an utterance
    # commit means the endpoint fired mid-thought — cancel the in-flight turn
    # answering the fragment and re-ask with the merged transcript. 0 = off.
    resume_grace_ms: int = 750
    # Instant ack: play a micro-ack clip the moment an utterance commits
    # (before STT/agent), so the user hears acknowledgment within ~0.5s of
    # stopping speaking instead of waiting for the response pipeline.
    instant_ack: bool = True
    # "smart": wait for the transcript (bounded by smart_ack_budget_ms, then
    # fall back to blind) and pick a context-appropriate ack pool;
    # "blind": fire a generic ack at commit, before STT.
    instant_ack_mode: Literal["smart", "blind", "delayed"] = "smart"
    # Same 1.2 s as the opening-line timer (900 tripped on every turn with a
    # service that takes about a second to transcribe).
    smart_ack_budget_ms: int = 1200
    turn_debounce_ms: int = 2000
    min_turn_chars: int = 2
    duplicate_turn_window_ms: int = 10000
    post_bot_cooldown_ms: int = 1200
    reply_max_sentences: int = 8
    reply_max_chars: int = 1200
    discord_reply_max_sentences: int = 8
    discord_reply_max_chars: int = 900
    preamble_pools: dict[str, list[str]] = {}
    reassure_tool_pools: dict[str, list[str]] = {}
    narration_phrases: dict[str, str] = {}
    # After the required spoken cue when tool work starts:
    #   "narrated" - speak a line per tool ("running git status")
    #   "silence"  - no additional per-tool narration
    #   "keyboard" - play a typing clip instead of speaking
    # Long work the agent hands off runs in a second session of the same
    # agent while the call carries on (sleeptalk/jobs.py). Cascade only.
    background_jobs: bool = True
    # Background jobs that run at once; more queue, none are refused.
    # Unset: 3. Lower it if local speech stutters while jobs build or test.
    max_background_jobs: int | None = Field(default=None, ge=1)
    narration_style: Literal["narrated", "silence", "keyboard"] = "silence"
    # How much of a long answer is spoken aloud:
    #   "full"       - speak every sentence the agent produces
    #   "summarized" - speak the opening, then one line for the rest
    # The call page shows what was said, with the whole answer folded under it.
    reply_length: Literal["full", "summarized", "concise"] = "full"
    # Seconds of no audible progress before a keepalive line fires; 0 = off.
    keepalive_interval_s: float = 10.0
    endpointing_overrides: EndpointingOverrides = EndpointingOverrides()
    turns: TurnPolicyConfig = TurnPolicyConfig()
    capture: CapturePolicyConfig = CapturePolicyConfig()
    interrupt: InterruptPolicyConfig = InterruptPolicyConfig()

    @property
    def barge_in(self) -> str:
        return self.interrupt.mode


class LogConfig(_Strict):
    """[log] — logging knobs. Console stays primary in foreground."""

    level: str = "WARNING"
    #: Detailed performance traces are opt-in. Basic error logs stay available.
    latency: bool = False
    #: Where those traces land. Empty means the run's own logs directory,
    #: so every surface of one profile writes to the same file.
    latency_path: str = ""


CredentialReference = Annotated[str, Field(pattern=r"^credential:[0-9a-f]{32}$")]
CredentialVariable = Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]{0,127}$")]


class CredentialsConfig(_Strict):
    refs: dict[CredentialVariable, CredentialReference] = Field(default_factory=dict)


_CREDENTIAL_PROVIDERS = {
    "DEEPGRAM_API_KEY": "deepgram", "CARTESIA_API_KEY": "cartesia",
    "ELEVENLABS_API_KEY": "elevenlabs", "PLAYHT_API_KEY": "playht",
    "PLAYHT_USER_ID": "playht", "RIME_API_KEY": "rime", "HUME_API_KEY": "hume",
    "OPENAI_API_KEY": "openai",
    # OpenAI-compatible audio services: one key listens and speaks.
    "OPENROUTER_API_KEY": "openrouter", "GROQ_API_KEY": "groq",
    "TOGETHER_API_KEY": "together", "VENICE_API_KEY": "venice",
}

# Tokens an agent adapter needs (a gateway session, a bridge's bearer token).
# They live in the private store like speech keys; the running session puts
# them back into the agent's environment, where the adapters read them.
_AGENT_TOKEN_PROVIDERS = {
    "HERMES_GATEWAY_SESSION_TOKEN": "hermes", "HERMES_GATEWAY_API_KEY": "hermes",
    "OPENCLAW_GATEWAY_TOKEN": "openclaw",
}


def agent_token_fields(agent: object) -> dict[str, str]:
    """Environment names of agent tokens a profile may reference, with each one's store provider."""
    fields = dict(_AGENT_TOKEN_PROVIDERS)
    options = getattr(agent, "options", None) or {}
    token_env = str(options.get("token_env") or "").strip() if isinstance(options, dict) else ""
    if token_env and token_env not in _CREDENTIAL_PROVIDERS:
        fields[token_env] = "agent-token"
    return fields


class SleeptalkConfig(_Strict):
    credentials: CredentialsConfig = Field(default_factory=CredentialsConfig)
    binding: RuntimeBinding | None = None
    route: RouteConfig = Field(default_factory=CascadeRouteConfig)
    transport: TransportConfig = TransportConfig()
    agent: AgentConfig = AgentConfig()
    stt: STTConfig = STTConfig()
    tts: TTSConfig = TTSConfig()
    policy: PolicyConfig = PolicyConfig()
    log: LogConfig = LogConfig()

    # Set by the loader; not part of the TOML schema.
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    source_path: str = ""  # excluded from TOML; injected post-parse


# ``source_path`` must not be a TOML-settable key; drop it from the strict set.
_TOML_SECTIONS = ("route", "transport", "agent", "stt", "tts", "policy", "log", "binding", "credentials")


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def _parse_toml_file(path: Path) -> dict:
    """Stage 1: file parses, sections known. Raises ConfigError."""
    if not path.is_file():
        raise ConfigError(
            [
                f"{path} not found. Run sleeptalk to set up (it writes this file), "
                "or pass --config PATH."
            ]
        )
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError([f"{path} does not parse as TOML: {exc}"]) from exc

    unknown_sections = sorted(set(raw) - set(_TOML_SECTIONS))
    if unknown_sections:
        raise ConfigError(
            [
                f"{path}: unknown section(s) {unknown_sections} — valid sections: "
                + ", ".join(f"[{s}]" for s in _TOML_SECTIONS)
            ]
        )
    return raw


def _validate_config(
    raw: dict,
    *,
    source_path: Path,
    env: dict[str, str] | None,
    check_env: bool,
    check_binaries: bool,
    verify_binding: bool = True,
) -> SleeptalkConfig:
    """Stages 2-4: schema valid -> discriminators resolve -> env vars present."""
    from sleeptalk.defaults import register_defaults

    register_defaults()

    # Stage 2: schema valid (strict: unknown keys hard-fail with valid options).
    try:
        config = SleeptalkConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(_format_validation_errors(exc)) from None
    config.source_path = str(source_path)
    config.agent.source_path = config.source_path

    if verify_binding and config.binding is not None:
        from sleeptalk.runtime_binding import BindingError, verify
        try:
            verify(config)
        except BindingError as exc:
            raise ConfigError([str(exc)]) from None

    # Stages 3+4 collected together: discriminators resolve against the
    # registries, only applicable options are set, env vars (secrets) are
    # present. One pass, all problems.
    env = env if env is not None else _load_env(source_path)
    if check_env:
        env = resolve_credential_env(config, env)
    problems = _validate_component_choices(config, env=env)
    if check_env:
        problems += collect_missing_env(config, env)
    if check_binaries:
        problems += collect_missing_binaries(config, env=env)
        problems += collect_missing_runtime_extras(config, env=env)
    if problems:
        raise ConfigError(problems)
    return config


def _merge_toml(base: dict, overlay: dict) -> dict:
    """Deep-merge overlay onto base: mappings merge recursively; lists and
    scalars replace wholesale (never element-merge).

    A new [transport] type replaces that table wholesale. Discord keys on a
    web or terminal overlay are leftover, not inherited settings.
    """
    merged = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            if (key == "policy" and value.get("endpointing") in _ENDPOINTING_MODES
                    and value["endpointing"] != merged[key].get("endpointing", "balanced")):
                # A profile selecting a different preset replaces the base's
                # timing choices. Keep non-timing controls and any overrides
                # explicitly supplied by the profile itself.
                from sleeptalk.policy import ENDPOINTING_PRESETS

                timing_keys = set().union(*(p.keys() for p in ENDPOINTING_PRESETS.values()))
                inherited = dict(merged[key])
                inherited["endpointing_overrides"] = {
                    name: setting for name, setting in inherited.get("endpointing_overrides", {}).items()
                    if name not in timing_keys
                }
                merged[key] = _merge_toml(inherited, value)
                continue
            if (
                key in {"transport", "route"}
                and value.get("type")
                and value.get("type") != merged[key].get("type")
            ):
                merged[key] = dict(value)
            else:
                merged[key] = _merge_toml(merged[key], value)
        else:
            merged[key] = value
    return merged


def _root_for_profile_path(profile_path: Path) -> Path | None:
    """Return the shared root for supported ``profiles/<slug>.toml`` layouts."""
    parent = profile_path.parent
    if parent.name != "profiles":
        return None
    project_root = parent.parent
    if project_root.name == "config":
        project_root = project_root.parent
    return shared_root_for_project(project_root)


def resolve_config_document(
    path: str | Path,
    *,
    root_path: str | Path | None = None,
) -> ResolvedConfigDocument:
    """Resolve a full config or a profile overlay through one public boundary.

    Files in either supported ``profiles/`` layout are always overlays on the
    project ``sleeptalk.toml``. The shared root is required so a profile launch
    can never silently fall back to schema defaults. Paths outside a profiles
    directory remain explicit full-document library inputs.
    """
    selected_path = Path(path)
    resolved_root = (
        Path(root_path)
        if root_path is not None
        else _root_for_profile_path(selected_path)
    )
    if resolved_root is None:
        return ResolvedConfigDocument(
            raw=_parse_toml_file(selected_path),
            source_path=selected_path,
            root_path=selected_path,
            profile_path=None,
        )
    if not resolved_root.is_file():
        raise ConfigError(
            [
                (
                    f"{resolved_root} not found. Profile {selected_path} requires the shared "
                    "sleeptalk.toml base; run sleeptalk to set up and it is written again."
                )
            ]
        )
    base = _parse_toml_file(resolved_root)
    overlay = _parse_toml_file(selected_path)
    return ResolvedConfigDocument(
        raw=_merge_toml(base, overlay),
        source_path=selected_path,
        root_path=resolved_root,
        profile_path=selected_path,
    )


def load_resolved_config(
    path: str | Path = DEFAULT_CONFIG_PATH,
    *,
    root_path: str | Path | None = None,
    env: dict[str, str] | None = None,
    check_env: bool = True,
    check_binaries: bool = True,
    verify_binding: bool = True,
) -> SleeptalkConfig:
    """Load a full config or canonical root-plus-profile effective config.

    ``verify_binding=False`` reads a profile whose runtime binding is stale,
    for callers that only list or edit it. Starting a call always verifies.
    """
    document = resolve_config_document(path, root_path=root_path)
    return _validate_config(
        document.raw,
        source_path=document.source_path,
        env=env,
        check_env=check_env,
        check_binaries=check_binaries,
        verify_binding=verify_binding,
    )


def load_runtime_config(
    path: str | Path,
    *,
    surface: str,
    env: dict[str, str] | None = None,
) -> SleeptalkConfig:
    """Load a profile for one invocation surface without rewriting it.

    A saved profile describes the agent and voice stack. The caller decides
    whether this invocation is Terminal, Web, or Discord, so transport-specific
    credentials and extras are checked only after applying that decision.
    """
    environment = env if env is not None else _load_env(Path(path))
    config = load_resolved_config(
        path,
        env=environment,
        check_env=False,
        check_binaries=False,
    )
    config = config.model_copy(
        update={
            "transport": transport_config_for_surface(config.transport, surface)
        }
    )
    environment = resolve_credential_env(config, environment)
    problems = _validate_component_choices(config, env=environment)
    problems.extend(collect_missing_env(config, environment))
    problems.extend(collect_missing_binaries(config, env=environment))
    problems.extend(collect_missing_runtime_extras(config, env=environment))
    problems.extend(collect_missing_system_dependencies(config))
    if problems:
        raise ConfigError(problems)
    return config


def transport_config_for_surface(transport: TransportConfig, surface: str) -> TransportConfig:
    """Keep common settings when switching surfaces; private options belong to their component."""
    from sleeptalk.defaults import register_defaults
    from sleeptalk.registries import TRANSPORTS

    register_defaults()
    target = TRANSPORTS.get(surface)
    if TRANSPORTS.get(transport.type).name == target.name:
        return transport.model_copy(update={"type": surface})
    values = transport.model_dump(include=set(target.config_fields) - {"options"})
    return TransportConfig.model_validate({**values, "type": surface})


def default_profiles_dir() -> Path:
    """<repo>/config/profiles — config.py lives at <repo>/sleeptalk/."""
    return Path(__file__).resolve().parents[1] / "config" / "profiles"


def load_profile_config(
    slug: str,
    *,
    root_path: str | Path | None = None,
    profiles_dir: str | Path | None = None,
    env: dict[str, str] | None = None,
    check_env: bool = True,
    check_binaries: bool = True,
) -> SleeptalkConfig:
    """Load the layered config for a live bot profile.

    Root ``sleeptalk.toml`` provides shared defaults; ``config/profiles/<slug>.toml``
    overlays it (mappings deep-merge, lists/scalars replace — see module
    docstring). A missing profile TOML is a hard error: no silent fallback.
    """
    profiles_dir = Path(profiles_dir) if profiles_dir is not None else default_profiles_dir()
    if root_path is None:
        root_path = next(
            (c for c in default_config_candidates() if c.is_file()),
            default_config_candidates()[-1],
        )
    profile_path = profiles_dir / f"{slug}.toml"
    if not profile_path.is_file():
        raise ConfigError(
            [
                f"{profile_path} not found — profile {slug!r} cannot boot without its "
                "TOML (no env/agent-store fallback for composition)."
            ]
        )
    return load_resolved_config(
        profile_path,
        root_path=root_path,
        env=env,
        check_env=check_env,
        check_binaries=check_binaries,
    )


def load_config(
    path: str | Path = DEFAULT_CONFIG_PATH,
    *,
    env: dict[str, str] | None = None,
    check_env: bool = True,
    check_binaries: bool = True,
) -> SleeptalkConfig:
    """Load + validate sleeptalk.toml.

    Order: file parses -> schema valid -> discriminators known -> env vars
    present. All problems within a stage are collected and reported together,
    each with the fix. Raises ConfigError on any failure.
    """
    path = Path(path)
    raw = _parse_toml_file(path)
    return _validate_config(
        raw,
        source_path=path,
        env=env,
        check_env=check_env,
        check_binaries=check_binaries,
    )


def collect_missing_env(
    config: SleeptalkConfig, env: dict[str, str] | None = None
) -> list[str]:
    """Every required env var that's missing, each with the fix."""
    from sleeptalk.defaults import register_defaults
    from sleeptalk.registries import AGENTS, STT_PROVIDERS, TRANSPORTS, TTS_PROVIDERS

    register_defaults()
    env = env if env is not None else dict(os.environ)
    required: dict[str, str] = {}
    for registration, section in _selected_registrations(
        config, TRANSPORTS, AGENTS, STT_PROVIDERS, TTS_PROVIDERS, env=env
    ):
        required.update(registration.required_env_for(section))
    if config.route.type == "hybrid":
        required["OPENAI_API_KEY"] = (
            "set it to an OpenAI API key with access to the configured Realtime "
            "and Speech models"
        )
    missing = []
    for var in sorted(required):
        if not str(env.get(var, "")).strip():
            missing.append(f"{var} missing — {required[var]}")
    return missing


def collect_missing_binaries(config: SleeptalkConfig, *, env: dict[str, str] | None = None) -> list[str]:
    """Every declared component binary not on PATH, with its install hint."""
    import shutil

    from sleeptalk.defaults import register_defaults
    from sleeptalk.registries import AGENTS, STT_PROVIDERS, TRANSPORTS, TTS_PROVIDERS

    register_defaults()
    problems: list[str] = []
    for registration, section in _selected_registrations(
        config, TRANSPORTS, AGENTS, STT_PROVIDERS, TTS_PROVIDERS, env=env
    ):
        binary = registration.binary_for(section)
        if binary and registration.find_binary(section) is None:
            hint = registration.install_hint or f"install '{binary}' and ensure it is on PATH"
            problems.append(f"{binary!r} binary not found on PATH — {hint}.")
    return problems


def collect_missing_agent_modules(config: SleeptalkConfig, *, env: dict[str, str] | None = None) -> list[str]:
    from sleeptalk.agents.readiness import probe
    from sleeptalk.defaults import register_defaults
    from sleeptalk.registries import AGENTS

    register_defaults()
    if AGENTS.get_optional(config.agent.type) is None:
        return []
    report = probe(config.agent.type, config.agent, env=env)
    return [
        f"{need.key} is not importable — {need.hint}."
        for need in report.missing
        if need.kind == "python_module"
    ]


def collect_missing_runtime_extras(config: SleeptalkConfig, *, env: dict[str, str] | None = None) -> list[str]:
    problems = collect_missing_agent_modules(config, env=env)
    from sleeptalk import sysdeps

    if config.transport.type == "terminal":
        extra = sysdeps.missing_terminal_extra()
        if extra:
            problems.append(extra)
    elif config.transport.type == "discord":
        extra = sysdeps.missing_discord_extra()
        if extra:
            problems.append(extra)
    return problems


def collect_missing_system_dependencies(config: SleeptalkConfig) -> list[str]:
    """Required native dependencies for the effective transport."""
    from sleeptalk import sysdeps

    return sysdeps.ensure_required_for_surface(config.transport.type, repair=False)


def _selected_registrations(config, TRANSPORTS_, AGENTS_, STT_, TTS_, *, env=None) -> list:
    from sleeptalk.agents.construction import resolve_agent
    from sleeptalk.registries import Registration

    selected: list[tuple[Registration, object]] = []
    components = [
        (TRANSPORTS_, config.transport.type, config),
    ]
    try:
        registration = resolve_agent(config.agent, env if env is not None else dict(os.environ)).registration
        selected.append((registration, config.agent))
    except ValueError:
        pass  # reported by _validate_component_choices
    if config.route.type == "cascade":
        components.extend(
            [
                (STT_, config.stt.provider, config.stt),
                (TTS_, config.tts.provider, config.tts),
            ]
        )
    for registry, name, section in components:
        try:
            selected.append((registry.get(name), section))
        except ValueError:
            pass  # already reported by _validate_component_choices
    if config.route.type == "cascade":
        for spec in config.tts.fallback:
            try:
                selected.append((TTS_.get(spec.provider), spec))
            except ValueError:
                pass
    return selected


def _validate_component_choices(config: SleeptalkConfig, *, env: dict[str, str] | None = None) -> list[str]:
    from sleeptalk.registries import AGENTS, STT_PROVIDERS, TRANSPORTS, TTS_PROVIDERS

    problems: list[str] = []
    components = [
        (TRANSPORTS, config.transport, "type"),
        (AGENTS, config.agent, "type"),
    ]
    if config.route.type == "cascade":
        components.extend(
            [
                (STT_PROVIDERS, config.stt, "provider"),
                (TTS_PROVIDERS, config.tts, "provider"),
            ]
        )
        components.extend((TTS_PROVIDERS, spec, "provider") for spec in config.tts.fallback)
    from sleeptalk.agents.construction import resolve_agent

    try:
        resolve_agent(config.agent, env if env is not None else dict(os.environ))
    except ValueError as exc:
        problems.append(str(exc))
    for registry, section, discriminator in components:
        name = getattr(section, discriminator)
        try:
            registration = registry.get(name)
        except ValueError as exc:
            problems.append(str(exc))
            continue
        options_path = f"[{_section_name(section)}.options]"
        common_fields = type(section).model_fields.keys() - {"options"}
        schema_fields = registration.options_schema.model_fields.keys() if registration.options_schema else set()
        duplicate_fields = (set(section.options) | schema_fields) & common_fields
        if duplicate_fields:
            problems.append(
                f"{options_path} contains common fields {sorted(duplicate_fields)}; "
                "set them directly in the component section."
            )
        else:
            try:
                section.options = registration.validate_options(section.options)
            except ValueError as exc:
                problems.append(f"{options_path} for {name!r}: {exc}")
        if registration.config_fields:
            allowed = {discriminator, "options"} | set(registration.config_fields)
            # Loader-injected fields (never TOML-settable by convention) are
            # excluded from the applicability check. Room fields apply to
            # every agent type.
            invalid = sorted(
                section.model_fields_set
                - allowed
                - {"source_path"}
                - _ROOM_AGENT_FIELDS
            )
            if invalid:
                problems.append(
                    f"[{_section_name(section)}] {invalid} do not apply to "
                    f"{discriminator}={name!r} — remove them or pick a {registry.kind} "
                    f"that supports them. Valid options for {name!r}: "
                    + ", ".join(sorted(allowed))
                )
    mode = (config.agent.participation_mode or "conversationalist").strip()
    if mode not in _PARTICIPATION_MODES:
        problems.append(
            f"[agent] unknown participation_mode {mode!r} — valid: "
            + ", ".join(_PARTICIPATION_MODES)
        )
    elif mode == "proactive" and not any(
        item.strip() for item in config.agent.proactive_triggers
    ):
        problems.append(
            "[agent] proactive_triggers is required when participation_mode is "
            "proactive"
        )
    return problems


def _section_name(section: BaseModel) -> str:
    return {
        CascadeRouteConfig: "route",
        HybridRouteConfig: "route",
        TransportConfig: "transport",
        AgentConfig: "agent",
        STTConfig: "stt",
        TTSConfig: "tts",
        TTSSpecConfig: "tts.fallback",
    }[type(section)]


def _private_env_file(path: Path) -> bool:
    """Whether a .env may be read: a regular file this user owns that nobody
    else can write. One only others can't write but can read is made 0600."""
    import stat

    try:
        info = path.lstat()
    except OSError:
        return False
    if not stat.S_ISREG(info.st_mode):
        return False
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        logger.warning("Ignoring %s: it belongs to another user.", path)
        return False
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        logger.warning("Ignoring %s: other users can write it. Run: chmod 600 %s", path, path)
        return False
    if info.st_mode & 0o077:
        try:
            path.chmod(0o600)
        except OSError:
            pass
    return True


def _load_env(config_path: Path) -> dict[str, str]:
    """Load project/profile .env, then $SLEEPTALK_HOME.

    A self-contained ``<project>/profiles/<slug>.toml`` anchors both its
    relative workspace and its secrets at ``<project>``. The tier-3
    ``<project>/config/profiles/<slug>.toml`` layout follows the same rule.
    The working directory's .env is never read: whatever repo the command
    happens to run in must not fill the agent's environment.
    """
    resolved = config_path.resolve()
    candidates = [resolved.parent / ".env"]
    if resolved.parent.name == "profiles":
        project_root = resolved.parent.parent
        if project_root.name == "config":
            project_root = project_root.parent
        candidates.append(project_root / ".env")
    home = os.environ.get("SLEEPTALK_HOME", "").strip()
    if home:
        candidates.append(Path(home).expanduser() / ".env")
    environment = dict(os.environ)
    for candidate in candidates:
        if not _private_env_file(candidate):
            continue
        expanded: dict[str, str | None] = {}
        for name, value in dotenv_values(candidate, interpolate=False).items():
            if value is None:
                expanded[name] = None
                continue
            scope = {**expanded, **environment}
            expanded[name] = "".join(part.resolve(scope) for part in parse_variables(value))
        for name, value in expanded.items():
            if value is not None:
                environment.setdefault(name, value)
    return environment


def resolve_credential_env(
    config: SleeptalkConfig, environment: dict[str, str], *,
    store: CredentialStore | None = None,
) -> dict[str, str]:
    """Resolve explicit references for runtime use without changing os.environ.

    A missing or inaccessible reference never falls back to an ambient key.
    Provider identity is checked before returning its value to a component.
    Read-only config discovery must not call this function.
    """
    from sleeptalk.credential_store import CredentialError, CredentialStore

    resolved = dict(environment)
    if not config.credentials.refs:
        return resolved
    resolved = resolve_agent_token_env(config, resolved, store=store)
    providers = {**_CREDENTIAL_PROVIDERS, config.transport.token_env: "discord"}
    from sleeptalk.defaults import register_defaults
    from sleeptalk.registries import STT_PROVIDERS, TRANSPORTS, TTS_PROVIDERS

    register_defaults()
    sections = [(TRANSPORTS, config.transport.type, config)]
    required = {"OPENAI_API_KEY"} if config.route.type == "hybrid" else set()
    if config.route.type == "cascade":
        sections.extend([
            (STT_PROVIDERS, config.stt.provider, config.stt),
            (TTS_PROVIDERS, config.tts.provider, config.tts),
            *((TTS_PROVIDERS, spec.provider, spec) for spec in config.tts.fallback),
        ])
    for registry, name, section in sections:
        registration = registry.get_optional(name)
        if registration is not None:
            required.update(registration.required_env_for(section))
    selected = {name: ref for name, ref in config.credentials.refs.items() if name in required}
    if not selected:
        return resolved
    if set(selected) - providers.keys():
        raise ConfigError(["[credentials.refs] contains an unsupported credential environment field."])
    store = store if store is not None else CredentialStore()
    for name, reference in selected.items():
        try:
            status = store.status(reference)
            if status.provider != providers[name]:
                raise ConfigError([f"[credentials.refs] {name} references the wrong provider."])
            resolved[name] = store.get(reference)
        except CredentialError:
            raise ConfigError([f"[credentials.refs] {name} is unavailable; unlock the private store or replace it privately."]) from None
    return resolved


def resolve_agent_token_env(
    config: SleeptalkConfig, environment: dict[str, str], *,
    store: CredentialStore | None = None,
) -> dict[str, str]:
    """Put the agent tokens the profile references back into an environment.

    Same rules as speech keys: an explicit reference only, the provider is
    checked first, and a missing one never falls back to an ambient value.
    """
    from sleeptalk.credential_store import CredentialError, CredentialStore

    fields = agent_token_fields(config.agent)
    selected = {name: ref for name, ref in config.credentials.refs.items() if name in fields}
    if not selected:
        return dict(environment)
    resolved = dict(environment)
    store = store if store is not None else CredentialStore()
    for name, reference in selected.items():
        try:
            if store.status(reference).provider != fields[name]:
                raise ConfigError([f"[credentials.refs] {name} references the wrong provider."])
            resolved[name] = store.get(reference)
        except CredentialError:
            raise ConfigError([f"[credentials.refs] {name} is unavailable; unlock the private store or set the agent up again."]) from None
    return resolved


def _format_validation_errors(exc: ValidationError) -> list[str]:
    problems: list[str] = []
    for error in exc.errors():
        if error["loc"] and error["loc"][0] == "credentials":
            problems.append("[credentials] must contain refs mapping environment names to opaque credential references.")
            continue
        loc = ".".join(str(part) for part in error["loc"])
        if error["type"] == "extra_forbidden":
            model = _model_for_location(exc, error["loc"])
            valid = ""
            if model is not None:
                valid = " Valid keys: " + ", ".join(sorted(model.model_fields))
            key = error["loc"][-1]
            parent = ".".join(str(part) for part in error["loc"][:-1]) or "(root)"
            problems.append(f"Unknown key '{key}' in [{parent}].{valid}")
        elif error["type"] == "literal_error":
            problems.append(f"{loc}: {error['msg']}")
        else:
            problems.append(f"{loc}: {error['msg']}")
    return problems


def _model_for_location(exc: ValidationError, loc: tuple) -> type[BaseModel] | None:
    model: type[BaseModel] | None = SleeptalkConfig
    for part in loc[:-1]:
        if model is None:
            return None
        field_info = model.model_fields.get(str(part))
        annotation = getattr(field_info, "annotation", None)
        model = annotation if isinstance(annotation, type) and issubclass(annotation, BaseModel) else None
    return model
