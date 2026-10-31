"""VoicePolicy: every runtime voice decision, extracted from the session classes.

Filler clips, endpointing thresholds, resume-after-interrupt, and barge-in
mode used to live scattered across ``voice_processor.py``,
``pi_discord_bot.py``, and ``discord_native/``. They are now owned here,
constructed from the ``[policy]`` section of ``sleeptalk.toml``.

Latency logging stays in ``app.latency`` — it is instrumentation, not policy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Default spoken-line pools. These used to be module constants in
# app/pi_discord_bot.py; policy now owns them.
# ---------------------------------------------------------------------------

DEFAULT_PREAMBLE_POOLS: dict[str, tuple[str, ...]] = {
    # Expanded pools (VOICE_PREAMBLES_PLAN.md): 3-clip pools read as a
    # pattern within one session — repetition fatigue.
    # Real words only: "Mm-hmm." and "Uh-huh." are vocalizations that TTS
    # engines render inconsistently (local Piper and Kokoro make a noise, not
    # an acknowledgement), so they are out of the pool.
    "ack": (
        "Got it.", "Okay.", "Yep.",
        "Right.", "Sure.", "Alright.",
    ),
    "action": (
        "Let me check that.", "I'll look into it.", "On it, checking now.",
        "Checking that now.", "Let me look that up.", "Give me a second to check.",
    ),
    # Combined ack+action as ONE clip: natural comma pause, no stitching
    # gap — the common lookup turn ("Okay... let me check that.").
    "ack_action": (
        "Okay, let me check that.",
        "Got it, checking now.",
        "Alright, let me look into it.",
        "Sure, let me check.",
    ),
    # Imperative task turns ("Fix the test") — smart ack picks these.
    "action:task": (
        "On it.", "Got it, on it.", "Doing that now.", "Working on it now.",
    ),
    # Emotional utterances: neutral-empathetic, never cheerful.
    "ack:empathy": ("Oh, I see.", "Ah, okay.", "Hmm, I see."),
    "working": ("Still on it.", "Working through it."),
    "control": ("Stopped.", "Okay, stopped."),
    "greeting": ("Hey, I'm here.", "Hey hey.", "Hello, hello."),
    # Canned join offer: no LLM round-trip on entry. 'Yes' is intercepted and
    # becomes a self-contained agent prompt.
    "cron_offer": (
        "Welcome back. Want me to run through the latest from your scheduled jobs?",
    ),
    "reassure": (
        "Still working on it.",
        "Hang tight, still going.",
        "Not done yet, almost there.",
        "Almost there, give me a moment.",
        "It's taking a bit, but I'm on it.",
        "Haven't forgotten you, still at it.",
        "Still checking on that.",
        "Bear with me a moment.",
        "Give me a little longer on this one.",
        "One more moment, still checking.",
    ),
}

# Task-specific reassure lines, picked when we know which tool the agent is
# running during a long knock loop. Keys match narration_phrases so the
# current tool kind maps straight onto a pool ("reassure:bash", etc.).
DEFAULT_REASSURE_TOOL_POOLS: dict[str, tuple[str, ...]] = {
    "read": (
        "Still reading through it.",
        "Still going through the file.",
        "Still digging through that file.",
    ),
    "edit": (
        "Still making the edits.",
        "Edits are still going through.",
        "Still working through the changes.",
    ),
    "write": (
        "Still writing it out.",
        "Still writing the file.",
        "The file is still being written.",
    ),
    "bash": (
        "That command is still running.",
        "Still running the command.",
        "The command's still going.",
        "That command is taking a while.",
    ),
    "search": (
        "Still searching.",
        "Still looking that up.",
        "The search is taking a bit.",
        "Still pulling results.",
    ),
    "other": (
        "Still working on it.",
        "Hang tight, still going.",
        "Still on it, give me a moment.",
    ),
}

DEFAULT_NARRATION_PHRASES: dict[str, str] = {
    "read": "Reading a file.",
    "edit": "Editing code.",
    "write": "Writing a file.",
    "bash": "Running a command.",
    "search": "Searching.",
    "task": "Starting a sub-task.",
    "mcp": "Using one of my tools.",
    "other": "Working on it.",
}

DEFAULT_FILLER_GENERAL: tuple[str, ...] = (
    "Let me check that.",
    "One second, I am looking.",
    "I am checking now.",
    "Let me pull that together.",
)

DEFAULT_FILLER_WEB_SEARCH: tuple[str, ...] = (
    "Let me check that.",
    "Checking now.",
    "I'll look that up.",
    "One second, checking.",
    "Let me verify that.",
)

# Endpointing presets scale the silence thresholds of AdaptiveEndpointingConfig.
# relaxed = patient (monologue-friendly), aggressive = snappy.
ENDPOINTING_PRESETS: dict[str, dict[str, int]] = {
    "relaxed": {
        "complete_silence_ms": 650,
        "balanced_complete_silence_ms": 850,
        "monologue_complete_silence_ms": 1100,
        "uncertain_silence_ms": 2400,
        "incomplete_silence_ms": 4500,
        "monologue_incomplete_silence_ms": 6500,
        "semantic_complete_silence_ms": 850,
        "filler_reset_ms": 4000,
    },
    "balanced": {},
    # A batch listener (an OpenAI-compatible audio service, or Local speech)
    # hears nothing until the turn is over and then pays an upload and a
    # decode, so its silence windows are shorter: the wait a
    # person feels comes out about where "balanced" lands with a streaming
    # listener. Applied automatically; "relaxed" opts out.
    "batch": {
        "complete_silence_ms": 300,
        "balanced_complete_silence_ms": 450,
        "monologue_complete_silence_ms": 600,
        "uncertain_silence_ms": 1200,
        "incomplete_silence_ms": 2500,
        "monologue_incomplete_silence_ms": 3500,
        "semantic_complete_silence_ms": 450,
        "filler_reset_ms": 2500,
    },
    "aggressive": {
        "complete_silence_ms": 180,
        "balanced_complete_silence_ms": 220,
        "monologue_complete_silence_ms": 350,
        "uncertain_silence_ms": 650,
        "incomplete_silence_ms": 1400,
        "monologue_incomplete_silence_ms": 2000,
        "semantic_complete_silence_ms": 180,
        "filler_reset_ms": 2500,
        "semantic_check_ms": 100,
        "semantic_recheck_ms": 200,
        "streaming_recheck_ms": 50,
        "local_complete_cap_ms": 220,
        "local_incomplete_cap_ms": 650,
    },
}

BARGE_IN_MODES = ("off", "soft", "immediate")


@dataclass(frozen=True)
class TurnPolicyValues:
    """Barge-in / turn-taking VAD thresholds (discord_native.turns.DiscordTurnConfig)."""

    vad_threshold: int = 500
    interrupt_min_ms: int = 250
    bot_grace_ms: int = 400
    vad_mode: str = "compare"
    silero_confidence: float = 0.70
    silero_start_ms: int = 220
    silero_stop_ms: int = 900
    silero_min_volume: float = 0.005
    post_interrupt_cooldown_ms: int = 1200
    duck_on_vad: bool = True
    duck_min_ms: int = 200
    duck_release_ms: int = 600
    duck_min_volume: float = 0.03
    duck_confidence: float = 0.70
    vad_profile: str = "default"
    compare_primary: str = "silero"
    debug: bool = False
    max_wait_seconds: float = 120.0
    # Parse-only legacy values; the runtime never pauses/restarts long tasks.
    long_task_check_in_seconds: float = 180.0
    long_task_auto_continue_seconds: float = 30.0


@dataclass(frozen=True)
class CapturePolicyValues:
    """Utterance capture / endpointing gates (discord_native sink + VAD)."""

    silence_ms: int = 450
    min_utterance_ms: int = 450
    min_transcript_words: int = 4
    # 2026-08-14: floors lowered 500 -> 300 / 0.01 -> 0.005. DeepRe live-call
    # probes (capture_vad_dropout events) showed real quiet-speech segments at
    # RMS 115-466 being rejected by both capture legs mid-utterance, which
    # froze the buffer and truncated the committed transcript at the cut
    # point. 300 stays well above the measured room-noise floor (~RMS 10-30)
    # while admitting soft mid-sentence speech; endpointing buckets absorb
    # any extra noise tails.
    rms_threshold: int = 300
    vad_mode: str = "hybrid"  # Silero ∪ RMS floor; loud RMS fallback on failure
    compare_primary: str = "silero"
    smart_turn: bool = True
    smart_turn_threshold: float = 0.6
    vad_confidence: float = 0.78
    vad_start_ms: int = 250
    vad_stop_ms: int = 900
    vad_min_volume: float = 0.005
    # Minimum utterance length for the legacy (non-native) listen path.
    min_listen_utterance_ms: int = 450


@dataclass(frozen=True)
class InterruptPolicyValues:
    """Floor grace, hold, cooldown, and mode ([policy.interrupt])."""

    enabled: bool = True
    mode: str = "immediate"
    min_duration_ms: int = 150
    grace_ms: int = 400
    gap_hold_ms: int = 600
    cooldown_ms: int = 1200
    min_volume: float = 0.03


NARRATION_STYLES: tuple[str, ...] = ("narrated", "silence", "keyboard")


def narration_style_or_default(value: object) -> str:
    """Unknown styles fall back to silence, the default.

    A profile written by a newer build, or a hand-edited typo, gets the same
    quiet working time as a new one; "Still working on it" lines still play.
    """
    style = str(value or "").strip().lower()
    return style if style in NARRATION_STYLES else "silence"


REPLY_LENGTHS: tuple[str, ...] = ("full", "summarized", "concise")


def local_responsiveness_enabled(config: Any) -> bool:
    """Live cascade responsiveness, independent of speech provider.

    The historical local-only name is retained for policy and telemetry callers.
    """
    return bool(
        getattr(getattr(config, "transport", None), "type", "") in {"web", "terminal", "discord"}
        and getattr(getattr(config, "route", None), "type", "cascade") == "cascade"
    )


def max_background_jobs_for(config: Any) -> int:
    """Jobs that run at once: the config's number, else 3. Local speech gets
    3 as well (decided 2026-10-15, after the planned default of 1 surprised
    a caller); a profile whose voice stutters under load can set fewer."""
    configured = getattr(getattr(config, "policy", None), "max_background_jobs", None)
    if configured is not None:
        return max(1, int(configured))
    return 3


def reply_length_or_default(value: object) -> str:
    """Unknown lengths fall back to full.

    The mirror of narration's rule rather than a copy of it: there the risk
    was speaking louder than asked, here it is dropping words nobody chose
    to drop. A typo must cost the listener time, never information.
    """
    length = str(value or "").strip().lower()
    return length if length in REPLY_LENGTHS else "full"


def wants_brief_speech(value: object) -> bool:
    """Is this session asking to be told less?

    Accepts the setting or a reader for it, because the setting is live:
    the terminal hotkey and the web control both mutate the shared policy
    mid-session, and an answer already being composed should honour the
    flip rather than the value read at boot.
    """
    if callable(value):
        value = value()
    return reply_length_or_default(value) in {"summarized", "concise"}


@dataclass
class VoicePolicy:
    """All voice-behavior decisions for a session, built from [policy]."""

    # Fillers
    fillers_enabled: bool = True
    filler_delay_ms: int = 800
    first_token_budget_ms: int = 800
    filler_general: tuple[str, ...] = DEFAULT_FILLER_GENERAL
    filler_web_search: tuple[str, ...] = DEFAULT_FILLER_WEB_SEARCH
    barge_in_min_chars: int = 4
    early_barge_in_ms: int = 900
    # Resume-after-interrupt: no proactive "want me to continue?" offer
    # (removed 2026-08-14). _interrupted_context is still tracked and a
    # user ask ("continue", "go on") resumes it — see pi_discord_bot.
    # Fragment holding: an incomplete utterance ("can you tell me about
    # the...") is held for fragment_hold_ms waiting for its continuation,
    # then merged into one turn — instead of answering the fragment.
    # 0 disables. Production traces (2026-08-14) showed 33/33 holds timing
    # out with zero merges at 4000ms — pure dead air — so the default stays
    # short and the smart ack masks the hold (see pi_discord_bot).
    fragment_hold_ms: int = 1500
    fragment_barge_window_ms: int = 10000
    # Grace-period abort window (see PolicyConfig.resume_grace_ms).
    resume_grace_ms: int = 750
    # Instant micro-ack at utterance commit (see PolicyConfig.instant_ack).
    instant_ack: bool = True
    # "smart": wait for the transcript (bounded by smart_ack_budget_ms, then
    # fall back to blind) and pick a context-appropriate ack pool — a
    # question goes straight to "Okay, let me check that." instead of a
    # blind "Okay." + gap + the agent's own preamble. "blind": fire a
    # generic ack at commit, before STT (VOICE_PREAMBLES_PLAN.md).
    instant_ack_mode: str = "smart"
    # 1200 (was 900): the same 1.2 s of silence as the opening line's timer;
    # services that take about a second to transcribe tripped 900 every turn.
    smart_ack_budget_ms: int = 1200
    # Turn buffering (voice_processor)
    turn_debounce_ms: int = 2000
    min_turn_chars: int = 2
    duplicate_turn_window_ms: int = 10000
    post_bot_cooldown_ms: int = 1200
    # Reply shaping
    reply_max_sentences: int = 8
    reply_max_chars: int = 1200
    discord_reply_max_sentences: int = 8
    discord_reply_max_chars: int = 900
    # Spoken clip pools
    preamble_pools: dict[str, tuple[str, ...]] = field(
        default_factory=lambda: dict(DEFAULT_PREAMBLE_POOLS)
    )
    reassure_tool_pools: dict[str, tuple[str, ...]] = field(
        default_factory=lambda: dict(DEFAULT_REASSURE_TOOL_POOLS)
    )
    narration_phrases: dict[str, str] = field(
        default_factory=lambda: dict(DEFAULT_NARRATION_PHRASES)
    )
    # After the required cue when tool work begins: "narrated" speaks a line
    # per tool, "silence" adds no chatter, "keyboard" plays a typing clip.
    narration_style: str = "silence"
    # Hand long work to a background job (sleeptalk/jobs.py).
    background_jobs: bool = True
    # Jobs that run at once; more queue. The builder sets it from the config
    # (see max_background_jobs_for).
    max_background_jobs: int = 3
    # How much of an answer is spoken: "full" speaks every sentence,
    # "summarized" speaks the opening and condenses the rest (see
    # sleeptalk.summarize). The page shows what was said, with the whole
    # answer folded under it.
    reply_length: str = "full"
    # Long-turn keepalive narration: seconds of no audible progress (no
    # yielded sentence, no tool narration) before the streaming layer emits
    # a keepalive tick. 0 disables. Supersedes _LOOP_REASSURE_SECONDS.
    keepalive_interval_s: float = 10.0
    # Endpointing
    endpointing: str = "balanced"
    endpointing_overrides: dict[str, Any] = field(default_factory=dict)
    # Discord capture + turn thresholds
    turns: TurnPolicyValues = field(default_factory=TurnPolicyValues)
    capture: CapturePolicyValues = field(default_factory=CapturePolicyValues)
    interrupt: InterruptPolicyValues = field(default_factory=InterruptPolicyValues)
    local_responsiveness: bool = False
    # Derived at build time: Discord needs visible captions to justify extra
    # batch uploads. This does not disable native streaming or pause-ahead STT.
    input_previews: bool = True

    # ---- derived views -------------------------------------------------

    @property
    def snappy_responsiveness(self) -> bool:
        # Automatic batch tuning of Balanced must not opt into Snappy.
        return self.local_responsiveness and self.endpointing == "aggressive"

    @property
    def barge_in(self) -> str:
        return self.interrupt.mode

    @property
    def allow_barge_in(self) -> bool:
        return self.interrupt.enabled and self.interrupt.mode != "off"

    @property
    def barge_in_mode(self) -> str:
        if self.interrupt.mode == "immediate":
            return "immediate"
        return "transcript"

    @property
    def effective_filler_delay_ms(self) -> int:
        if self.first_token_budget_ms > 0:
            return self.first_token_budget_ms
        return self.filler_delay_ms

    def filler_catalog(self):
        """FillerCatalog built from the configured pools (lazy import)."""
        from sleeptalk.voice.fillers import FillerCatalog

        return FillerCatalog.from_strings(
            general="|".join(self.filler_general),
            web_search="|".join(self.filler_web_search),
        )

    def tune_for_batch_listener(self) -> bool:
        """Tighten the silence windows when the listener only hears whole turns.

        Only the default preset moves: a profile that chose "relaxed" or
        "aggressive", or set explicit overrides, said what it wants.
        """
        if self.endpointing != "balanced" or self.endpointing_overrides:
            return False
        self.endpointing = "batch"
        return True

    def endpointing_config(self):
        """AdaptiveEndpointingConfig for the configured preset + overrides."""
        from sleeptalk.voice.configs import AdaptiveEndpointingConfig

        values: dict[str, Any] = dict(ENDPOINTING_PRESETS.get(self.endpointing, {}))
        values.update(self.endpointing_overrides)
        values["local_responsiveness"] = self.local_responsiveness
        values["streaming_pause_timing"] = self.local_responsiveness
        return AdaptiveEndpointingConfig(**values)

    def turn_config(
        self, *, surface: str = "", persona: str = "", agent: str = "", route: str = ""
    ):
        """DiscordTurnConfig from the configured turn/VAD thresholds."""
        from sleeptalk.voice.configs import DiscordTurnConfig

        t = self.turns
        return DiscordTurnConfig(
            barge_in_mode=self.barge_in,
            vad_threshold=t.vad_threshold,
            interrupt_min_ms=t.interrupt_min_ms,
            bot_grace_ms=t.bot_grace_ms,
            vad_mode=t.vad_mode,
            silero_confidence=t.silero_confidence,
            silero_start_ms=t.silero_start_ms,
            silero_stop_ms=t.silero_stop_ms,
            silero_min_volume=t.silero_min_volume,
            post_interrupt_cooldown_ms=t.post_interrupt_cooldown_ms,
            duck_on_vad=t.duck_on_vad,
            duck_min_ms=min(t.duck_min_ms, 80) if self.local_responsiveness else t.duck_min_ms,
            duck_release_ms=t.duck_release_ms,
            duck_min_volume=t.duck_min_volume,
            duck_confidence=t.duck_confidence,
            vad_profile=t.vad_profile,
            compare_primary=t.compare_primary,
            log_surface=surface,
            log_persona=persona,
            log_agent=agent,
            log_route=route,
            log_debug=t.debug,
        )

    @property
    def turn_max_wait_seconds(self) -> float:
        return self.turns.max_wait_seconds

    def silero_config(self):
        """SileroVadConfig for the native capture path."""
        from sleeptalk.voice.configs import SileroVadConfig

        c = self.capture
        return SileroVadConfig(
            confidence=c.vad_confidence,
            start_ms=c.vad_start_ms,
            stop_ms=c.vad_stop_ms,
            min_volume=c.vad_min_volume,
        )

    # ---- construction ---------------------------------------------------

    @classmethod
    def from_config(cls, policy: "Any") -> "VoicePolicy":
        """Build from a sleeptalk.config.PolicyConfig."""
        from sleeptalk.config import PolicyConfig

        if not isinstance(policy, PolicyConfig):
            raise TypeError(f"from_config expects PolicyConfig, got {type(policy).__name__}")
        return cls(
            fillers_enabled=policy.fillers,
            filler_delay_ms=policy.filler_delay_ms,
            first_token_budget_ms=policy.first_token_budget_ms,
            filler_general=tuple(policy.filler_general),
            filler_web_search=tuple(policy.filler_web_search),
            barge_in_min_chars=policy.barge_in_min_chars,
            early_barge_in_ms=policy.early_barge_in_ms,
            fragment_hold_ms=policy.fragment_hold_ms,
            fragment_barge_window_ms=policy.fragment_barge_window_ms,
            resume_grace_ms=policy.resume_grace_ms,
            instant_ack=policy.instant_ack,
            instant_ack_mode=policy.instant_ack_mode,
            smart_ack_budget_ms=policy.smart_ack_budget_ms,
            turn_debounce_ms=policy.turn_debounce_ms,
            min_turn_chars=policy.min_turn_chars,
            duplicate_turn_window_ms=policy.duplicate_turn_window_ms,
            post_bot_cooldown_ms=policy.post_bot_cooldown_ms,
            reply_max_sentences=policy.reply_max_sentences,
            reply_max_chars=policy.reply_max_chars,
            discord_reply_max_sentences=policy.discord_reply_max_sentences,
            discord_reply_max_chars=policy.discord_reply_max_chars,
            preamble_pools=(
                {k: tuple(v) for k, v in policy.preamble_pools.items()}
                if policy.preamble_pools
                else dict(DEFAULT_PREAMBLE_POOLS)
            ),
            reassure_tool_pools=(
                {k: tuple(v) for k, v in policy.reassure_tool_pools.items()}
                if policy.reassure_tool_pools
                else dict(DEFAULT_REASSURE_TOOL_POOLS)
            ),
            narration_style=narration_style_or_default(policy.narration_style),
            reply_length=reply_length_or_default(policy.reply_length),
            background_jobs=bool(policy.background_jobs),
            narration_phrases=(
                dict(policy.narration_phrases)
                if policy.narration_phrases
                else dict(DEFAULT_NARRATION_PHRASES)
            ),
            keepalive_interval_s=policy.keepalive_interval_s,
            endpointing=policy.endpointing,
            endpointing_overrides=policy.endpointing_overrides.model_dump(exclude_unset=True),
            turns=TurnPolicyValues(**policy.turns.model_dump()),
            capture=CapturePolicyValues(**policy.capture.model_dump()),
            interrupt=InterruptPolicyValues(**policy.interrupt.model_dump()),
        )
