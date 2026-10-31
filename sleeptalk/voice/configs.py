"""Config dataclasses for capture/endpointing, extracted from discord_native.

These are policy-shaped values (VoicePolicy derives them); the capture
machinery that consumes them lives in discord_native and imports them back.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class AdaptiveEndpointingConfig:
    enabled: bool = True
    responsive_mode_ms: int = 5_000
    monologue_mode_ms: int = 15_000
    completion_threshold: float = 0.72
    complete_silence_ms: int = 450
    balanced_complete_silence_ms: int = 650
    monologue_complete_silence_ms: int = 850
    uncertain_silence_ms: int = 1_800
    incomplete_silence_ms: int = 3_500
    monologue_incomplete_silence_ms: int = 5_000
    filler_reset_ms: int = 3_500
    semantic_complete_silence_ms: int = 650
    # Judge "is the speaker done?" by the pause's early transcript when the
    # listener only transcribes whole utterances (batch STT): wait for the
    # text instead of ending on silence alone.
    early_transcript: bool = True
    local_responsiveness: bool = False
    # Derived from the selected local streaming model.
    local_streaming_input: bool = False
    # Live cascade listeners share the fresh-voice clock and bounded semantic
    # retry without inheriting Moonshine's completed-segment endpoint thresholds.
    streaming_pause_timing: bool = False
    semantic_check_ms: int = 250
    semantic_recheck_ms: int = 500
    streaming_recheck_ms: int = 150
    local_complete_cap_ms: int = 600
    local_incomplete_cap_ms: int = 1200

    @property
    def fresh_voice_timing(self) -> bool:
        return self.local_streaming_input or self.streaming_pause_timing


@dataclass
class DiscordTurnConfig:
    # Preserve the coordinator's historic standalone behavior. VoicePolicy
    # passes the configured live value explicitly (normally ``soft``).
    barge_in_mode: str = "immediate"  # off | soft | immediate
    vad_threshold: int = 500
    interrupt_min_ms: int = 250
    bot_grace_ms: int = 400
    vad_mode: str = "rms"
    silero_confidence: float = 0.70
    silero_start_ms: int = 220
    silero_stop_ms: int = 900
    silero_min_volume: float = 0.005
    # After a barge-in fires, suppress re-triggers for this long. Kills the
    # double-fire pattern where the echo tail re-interrupts the next segment.
    post_interrupt_cooldown_ms: int = 1200
    # Duck-on-VAD: the reversible barge-in cue. The moment frame-level speech
    # appears while the bot is playing (looser bars than the barge-in gate —
    # pausing is reversible, cancelling is not), pause playback after
    # duck_min_ms of sustained speech. If the speech matures into a real
    # barge-in the normal stop path takes over; if it was a blip (cough,
    # "mhmm", echo), playback resumes after duck_release_ms of quiet. The
    # volume bar is the echo guard: the bot's own TTS bleeding into the mic
    # is quiet, a real interruption is loud.
    duck_on_vad: bool = True
    duck_min_ms: int = 200
    duck_release_ms: int = 600
    duck_min_volume: float = 0.03
    duck_confidence: float = 0.70
    vad_profile: str = "default"
    compare_primary: str = "rms"
    log_surface: str = ""
    log_transport: str = "discord-native"
    # Which brain and which route produced these events. The coordinator logs
    # directly rather than through a VoiceLatencyTrace, so without these its
    # records cannot be attributed to a run.
    log_agent: str = ""
    log_route: str = ""
    log_persona: str = ""
    log_debug: bool = False


@dataclass(frozen=True)
class SileroVadConfig:
    confidence: float = 0.78
    start_ms: int = 250
    stop_ms: int = 900
    min_volume: float = 0.01
    # While already speaking, frames only need to clear this lower confidence
    # to keep the session alive (hysteresis). 0 derives it from `confidence`.
    exit_confidence: float = 0.0
    input_sample_rate: int = 48000
    input_channels: int = 2
    sample_width: int = 2
    vad_sample_rate: int = 16000


@dataclass(frozen=True)
class TurnCompletionSignal:
    transcript: str = ""
    score: float | None = None
    is_final: bool = False
    source: str = ""
