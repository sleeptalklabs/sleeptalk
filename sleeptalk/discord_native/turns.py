from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Any

import discord

from sleeptalk.discord_native.playback import DiscordPlaybackController
from sleeptalk.discord_native.timeline import VoiceTimeline
from sleeptalk.voice_loop.telemetry import log_latency_event
from sleeptalk.voice_loop.vad import (
    SileroVadConfig,
    SileroVadGate,
    SpeechGate,
    pcm_duration_ms,
    pcm_rms,
)
from sleeptalk.barge import (
    BargeAction,
    BargeCalibration,
    BargeEvidence,
    BargeGate,
    BargeSurface,
    execute,
)
from sleeptalk.interrupt import AcousticKind, InterruptPolicy, SpeakingState
from sleeptalk.voice.configs import DiscordTurnConfig

# Match web_voice.session._WEB_BARGE_INPUT_GAIN. Discord capture of a spoken
# "stop" often lands at normalized volume 0.016-0.021, under min_volume 0.03.
# Scale the barge volume check (not the PCM) so RMS telemetry and duck stay
# honest while a single word clears the bar.
_DISCORD_BARGE_VOLUME_GAIN = 2.5
_INTERRUPT_MODES = {"off", "soft", "immediate"}
_BARGE_DIP_TOLERANCE_MS = 120.0


def interrupt_from_voice_policy(policy: Any) -> InterruptPolicy:
    values = getattr(policy, "interrupt", None)
    if values is None:
        return InterruptPolicy()
    mode = getattr(values, "mode", "immediate") or "immediate"
    if mode not in _INTERRUPT_MODES:
        mode = "immediate"
    return InterruptPolicy(
        enabled=bool(getattr(values, "enabled", True)),
        local_responsiveness=bool(getattr(policy, "local_responsiveness", False)),
        snappy_responsiveness=bool(getattr(policy, "snappy_responsiveness", False)),
        mode=mode,
        min_duration_ms=int(getattr(values, "min_duration_ms", 150) or 150),
        grace_ms=int(getattr(values, "grace_ms", 400) or 400),
        gap_hold_ms=int(getattr(values, "gap_hold_ms", 600) or 600),
        cooldown_ms=int(getattr(values, "cooldown_ms", 1200) or 1200),
        min_volume=float(getattr(values, "min_volume", 0.03) or 0.03),
    )


@dataclass
class DiscordTurnStats:
    pcm_frames: int = 0
    speech_frames: int = 0
    interruptions: int = 0
    last_rms: int = 0
    last_vad_mode: str = "silero"
    last_vad_state: str = ""
    last_vad_confidence: float = 0.0
    last_vad_volume: float = 0.0
    max_rms: int = 0
    max_vad_confidence: float = 0.0
    max_vad_volume: float = 0.0
    playback_pcm_frames: int = 0
    playback_speech_frames: int = 0
    vad_failures: int = 0
    last_interrupt_user: str = ""
    rms_speech_frames: int = 0
    silero_speech_frames: int = 0
    both_speech_frames: int = 0
    agree_frames: int = 0
    disagree_frames: int = 0
    rms_only_triggers: int = 0
    silero_only_triggers: int = 0
    both_triggers: int = 0
    last_rms_speech: bool = False
    last_silero_started: bool = False


class DiscordTurnCoordinator(BargeSurface):
    def __init__(
        self,
        *,
        config: DiscordTurnConfig,
        playback: DiscordPlaybackController,
        timeline: VoiceTimeline,
        on_interrupt: Callable[[discord.User], Awaitable[None]] | None = None,
        interrupt: InterruptPolicy | None = None,
    ) -> None:
        self.config = config
        self.playback = playback
        self.timeline = timeline
        self.on_interrupt = on_interrupt
        self.stats = DiscordTurnStats()
        self._speech_gate = SpeechGate(
            threshold=config.vad_threshold,
            min_speech_ms=config.interrupt_min_ms,
        )
        self._origin = time.monotonic()
        self._interrupt = replace(
            interrupt or InterruptPolicy(mode=_interrupt_mode(config)),
            duck_min_ms=config.duck_min_ms,
            duck_release_ms=config.duck_release_ms,
            duck_min_volume=config.duck_min_volume,
            duck_confidence=config.duck_confidence,
        )
        self._gate = BargeGate(
            self._interrupt,
            AcousticKind.isolated,
            calibration=BargeCalibration(
                # Discord capture of a spoken "stop" lands at 0.016-0.021,
                # under min_volume 0.03. Scale the check, not the PCM, so RMS
                # telemetry and ducking stay honest.
                level_scale=_DISCORD_BARGE_VOLUME_GAIN,
                # One dip inside a word is not the end of an interruption.
                dip_tolerance_ms=_BARGE_DIP_TOLERANCE_MS,
                duck_rms_threshold=config.vad_threshold,
                supports_resume=True,
                supports_duck=config.duck_on_vad,
            ),
            detector="silero",
            # Tests and the timeline drive this clock; keep one source.
            clock=lambda: self._now_ms(),
            emit=self._log_barge_event,
        )
        self._silero_gate = _build_silero_gate(config)
        self._silero_enabled = self._silero_gate is not None
        self._compare_primary = config.compare_primary.strip().lower()
        if self._compare_primary not in {"rms", "silero"}:
            self._compare_primary = "rms"
        self._interrupted_current_playback = False
        self._logged_debug_observations = 0
        self._turn_end_handle: asyncio.TimerHandle | None = None
        self._barge_detector = "silero"
        self._barging_user: discord.User | None = None
        self._last_halt_result = False
        self._cooldown_suppressed = 0
        self._cooldown_logged = False

    def bot_playback_started(self) -> None:
        if self._turn_end_handle is not None:
            self._turn_end_handle.cancel()
            self._turn_end_handle = None
        if self._gate.idle:
            self._gate.turn_started()
            self._reset_playback_stats()
        self._gate.audio_started()
        self._interrupted_current_playback = False

    def bot_playback_stopped(self) -> None:
        if self._gate.idle:
            return
        self._gate.audio_drained()
        delay_s = self._interrupt.gap_hold_ms / 1000
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._end_floor_turn()
            return
        if self._turn_end_handle is not None:
            self._turn_end_handle.cancel()
        self._turn_end_handle = loop.call_later(delay_s, self._end_floor_turn)

    def _end_floor_turn(self) -> None:
        self._turn_end_handle = None
        if self._gate.idle:
            return
        self._gate.turn_ended()
        self._log_event(
            "barge_in_playback_summary",
            interrupted=self._interrupted_current_playback,
            mode="silero",
            sileroEnabled=self._silero_enabled,
            pcmFrames=self.stats.playback_pcm_frames,
            speechFrames=self.stats.playback_speech_frames,
            maxRms=self.stats.max_rms,
            lastRms=self.stats.last_rms,
            lastVadState=self.stats.last_vad_state,
            lastVadConfidence=round(self.stats.last_vad_confidence, 4),
            maxVadConfidence=round(self.stats.max_vad_confidence, 4),
            lastVadVolume=round(self.stats.last_vad_volume, 4),
            maxVadVolume=round(self.stats.max_vad_volume, 4),
            vadFailures=self.stats.vad_failures,
            vadProfile=self.config.vad_profile,
            rmsSpeechFrames=self.stats.rms_speech_frames,
            sileroSpeechFrames=self.stats.silero_speech_frames,
        )
        self._interrupted_current_playback = False
        self._speech_gate.reset()
        if self._silero_gate:
            self._silero_gate.reset()

    def _reset_playback_stats(self) -> None:
        self.stats.max_rms = 0
        self.stats.max_vad_confidence = 0.0
        self.stats.max_vad_volume = 0.0
        self.stats.playback_pcm_frames = 0
        self.stats.playback_speech_frames = 0
        self.stats.last_vad_state = ""
        self.stats.last_vad_confidence = 0.0
        self.stats.last_vad_volume = 0.0
        self.stats.rms_speech_frames = 0
        self.stats.silero_speech_frames = 0
        self.stats.both_speech_frames = 0
        self.stats.agree_frames = 0
        self.stats.disagree_frames = 0
        self.stats.rms_only_triggers = 0
        self.stats.silero_only_triggers = 0
        self.stats.both_triggers = 0
        self.stats.last_rms_speech = False
        self.stats.last_silero_started = False
        self._logged_debug_observations = 0
        self._speech_gate.reset()
        if self._silero_gate:
            self._silero_gate.reset()

    async def observe_pcm(
        self, user: discord.User, pcm: bytes, *, during_turn: bool = False
    ) -> bool:
        self.stats.pcm_frames += 1
        rms_speech = self._speech_gate.observe(pcm)
        self.stats.last_rms = self._speech_gate.last_rms
        self.stats.max_rms = max(self.stats.max_rms, self.stats.last_rms)
        self.stats.last_vad_mode = "silero"
        if rms_speech:
            self.stats.rms_speech_frames += 1

        if self._gate.idle and not self.playback.active and not during_turn:
            self._speech_gate.reset()
            self._interrupted_current_playback = False
            return False

        self.stats.playback_pcm_frames += 1
        evidence = await self._evidence(user, pcm)
        decision = self._gate.observe(evidence)
        self._close_cooldown_window(decision)
        self._maybe_log_debug_observation(
            rmsSpeech=rms_speech,
            sileroSpeech=bool(evidence.voiced),
            source="silero" if evidence.voiced else "rms",
        )
        if evidence.voiced:
            self.stats.speech_frames += 1
            self.stats.playback_speech_frames += 1

        if decision.action in (BargeAction.duck, BargeAction.resume):
            self._barging_user = user
            await execute(decision, self)
            return False
        if not decision.fired:
            if decision.reason == "held. cooldown":
                self._cooldown_suppressed += 1
                if not self._cooldown_logged:
                    self._cooldown_logged = True
                    self._log_event(
                        "barge_in_suppressed",
                        reason="cooldown",
                        source="silero",
                        vadConfidence=round(self.stats.last_vad_confidence, 4),
                        vadVolume=round(self.stats.last_vad_volume, 4),
                        rms=self.stats.last_rms,
                    )
                return True
            return bool(self._interrupted_current_playback and evidence.voiced)
        if self._interrupted_current_playback:
            return True

        self._interrupted_current_playback = True
        self.stats.interruptions += 1
        self.stats.last_interrupt_user = _display_name(user)
        self._barging_user = user
        self._last_halt_result = False
        soft = decision.action is BargeAction.pause
        await execute(decision, self)
        self._log_event(
            "barge_in_detected",
            userId=getattr(user, "id", ""),
            userName=self.stats.last_interrupt_user,
            source=self._barge_detector,
            mode="silero",
            vadProfile=self.config.vad_profile,
            stopResult=self._last_halt_result if not soft else False,
            disposition="pending_transcript" if soft else "cancelled",
            backendCancelRequested=bool(self.on_interrupt) and not soft,
            rms=self.stats.last_rms,
            maxRms=self.stats.max_rms,
            vadState=self.stats.last_vad_state,
            vadConfidence=round(self.stats.last_vad_confidence, 4),
            maxVadConfidence=round(self.stats.max_vad_confidence, 4),
            vadVolume=round(self.stats.last_vad_volume, 4),
            maxVadVolume=round(self.stats.max_vad_volume, 4),
            vadFailures=self.stats.vad_failures,
            pcmFrames=self.stats.playback_pcm_frames,
            speechFrames=self.stats.playback_speech_frames,
            rmsSpeechFrames=self.stats.rms_speech_frames,
            sileroSpeechFrames=self.stats.silero_speech_frames,
            sileroBargeStreakMs=round(decision.sustain_ms),
            volumeGain=_DISCORD_BARGE_VOLUME_GAIN,
            floorState=decision.floor_state.value,
            floorReason=decision.reason,
            bargeId=decision.barge_id,
            downgraded=decision.downgraded,
        )
        self.timeline.record(
            "barge_in_detected",
            f"{self.stats.last_interrupt_user} mode=silero rms={self.stats.last_rms}",
        )
        return True

    async def resume_after_soft_barge(self, reason: str = "barge-in-backchannel") -> bool:
        self._gate.resolve(backchannel=True)
        resumed = await self.playback.resume(reason)
        self._interrupted_current_playback = False
        self.timeline.record("barge_in_resolved", "resumed" if resumed else "idle")
        self._log_event("barge_in_resolved", disposition="resumed", reason=reason)
        return resumed

    def commit_soft_barge(self, reason: str = "barge-in") -> None:
        self._gate.resolve(backchannel=False)
        self.timeline.record("barge_in_resolved", f"cancelled {reason}")
        self._log_event("barge_in_resolved", disposition="cancelled", reason=reason)

    # -- BargeSurface: what the gate's decisions mean on Discord -----------

    async def duck(self, reason: str) -> bool:
        if not await self.playback.pause(reason):
            return False
        self.timeline.record("playback_ducked", reason)
        self._log_event(
            "playback_ducked",
            mode="silero",
            rms=self.stats.last_rms,
            vadConfidence=round(self.stats.last_vad_confidence, 4),
            vadVolume=round(self.stats.last_vad_volume, 4),
        )
        return True

    async def resume(self, reason: str) -> bool:
        if not await self.playback.resume(reason):
            return False
        self.timeline.record("playback_resumed", reason)
        self._log_event("playback_resumed", reason=reason, mode="silero")
        return True

    async def halt(self, reason: str, *, resumable: bool = False) -> bool:
        # A soft barge pauses: the transcript still gets to say whether this
        # was an interruption or a "mhm".
        if resumable:
            self._last_halt_result = self._gate.ducked or await self.playback.pause(reason)
        else:
            self._last_halt_result = await self.playback.stop(reason)
        return self._last_halt_result

    def _now_ms(self) -> int:
        return int((time.monotonic() - self._origin) * 1000)

    async def hold_turn(self, reason: str) -> None:
        # Soft: live_session captures the interrupting speech and waits for a
        # transcript before deciding. It must still hear about the barge.
        await self.cancel_turn(reason)

    async def cancel_turn(self, reason: str) -> None:
        self.timeline.record(
            "barge_in_detected",
            f"{self.stats.last_interrupt_user} mode=silero rms={self.stats.last_rms}",
        )
        if self.on_interrupt and self._barging_user is not None:
            await self.on_interrupt(self._barging_user)
            self.timeline.record("backend_cancel_requested")

    # -- evidence ----------------------------------------------------------

    async def _evidence(self, user: discord.User, pcm: bytes) -> BargeEvidence:
        """Report what this frame sounded like. The gate decides what it means."""
        channel = str(getattr(user, "id", "") or "")
        duration_ms = pcm_duration_ms(pcm)
        rms = self.stats.last_rms
        if not self._silero_enabled or self._silero_gate is None:
            return self._energy_evidence(channel, duration_ms, rms)
        try:
            observation = await self._silero_gate.observe(pcm)
        except (ImportError, RuntimeError, ValueError, OSError, AttributeError):
            # Losing the model must not cost the session its barge-in for the
            # rest of the call. Energy is a worse detector, not no detector,
            # and the volume bar plus floor grace still hold echo out.
            self.stats.vad_failures += 1
            self._silero_enabled = False
            self.timeline.record("barge_in_vad_fallback", "silero failed; energy barge-in")
            self._log_event(
                "barge_in_vad_fallback",
                mode="silero",
                rms=rms,
                maxRms=self.stats.max_rms,
                vadFailures=self.stats.vad_failures,
            )
            return self._energy_evidence(channel, duration_ms, rms)

        self.stats.last_vad_state = observation.state
        self.stats.last_vad_confidence = observation.confidence
        self.stats.last_vad_volume = observation.volume
        self.stats.max_vad_confidence = max(
            self.stats.max_vad_confidence, observation.confidence
        )
        self.stats.max_vad_volume = max(self.stats.max_vad_volume, observation.volume)
        if observation.is_speech:
            self.stats.silero_speech_frames += 1
        return BargeEvidence(
            channel=channel,
            duration_ms=duration_ms,
            level=observation.volume,
            voiced=observation.is_speech,
            confidence=observation.confidence,
            raw_rms=rms,
        )

    def _energy_evidence(self, channel: str, duration_ms: float, rms: int) -> BargeEvidence:
        self._barge_detector = "rms_fallback"
        return BargeEvidence(
            channel=channel,
            duration_ms=duration_ms,
            level=rms / 32768.0,
            voiced=rms >= self.config.vad_threshold,
            raw_rms=rms,
        )

    def _close_cooldown_window(self, decision) -> None:
        """One record per cooldown window, not one per frame.

        log_latency_event opens, writes and closes synchronously on the calling
        thread, and this is the audio path. A four-minute session wrote 261 of
        these. The first frame still reports immediately, with live VAD values;
        the rest are counted and summarised when the floor leaves cooldown.
        """
        # A fire re-enters cooldown immediately, so "state is cooldown" alone
        # never closes the window. Firing means the old window ended.
        if not decision.fired and decision.floor_state is SpeakingState.cooldown:
            return
        if self._cooldown_suppressed:
            self._log_event(
                "barge_in_suppressed",
                reason="cooldown_ended",
                source="silero",
                frames=self._cooldown_suppressed,
            )
        self._cooldown_suppressed = 0
        self._cooldown_logged = False

    def _log_barge_event(self, stage: str, fields: dict) -> None:
        # bargeId doubles as the trace id, so one interruption is a single
        # join key across every event it produced — on any surface.
        self._log_event(stage, trace_id=fields.get("bargeId") or None, **fields)

    def _log_event(self, stage: str, **fields: object) -> None:
        if not self.config.log_surface or not self.config.log_persona:
            return
        log_latency_event(
            surface=self.config.log_surface,
            transport=self.config.log_transport,
            persona=self.config.log_persona,
            stage=stage,
            agent=self.config.log_agent or None,
            route=self.config.log_route or None,
            **fields,
        )

    def _maybe_log_debug_observation(self, **fields: object) -> None:
        if not self.config.log_debug:
            return
        self._logged_debug_observations += 1
        if self._logged_debug_observations % 10:
            return
        self._log_event(
            "barge_in_vad_observed",
            mode="silero",
            sileroEnabled=self._silero_enabled,
            rms=self.stats.last_rms,
            maxRms=self.stats.max_rms,
            vadState=self.stats.last_vad_state,
            vadConfidence=round(self.stats.last_vad_confidence, 4),
            maxVadConfidence=round(self.stats.max_vad_confidence, 4),
            vadVolume=round(self.stats.last_vad_volume, 4),
            maxVadVolume=round(self.stats.max_vad_volume, 4),
            pcmFrames=self.stats.playback_pcm_frames,
            **fields,
        )

    def reconfigure(
        self,
        *,
        vad_mode: str | None = None,
        compare_primary: str | None = None,
        vad_profile: str | None = None,
        silero_confidence: float | None = None,
        silero_start_ms: int | None = None,
        silero_stop_ms: int | None = None,
        silero_min_volume: float | None = None,
        silero_barge_min_ms: int | None = None,
        silero_barge_min_volume: float | None = None,
        post_interrupt_cooldown_ms: int | None = None,
        log_debug: bool | None = None,
    ) -> dict[str, object]:
        if vad_mode is not None:
            self.config.vad_mode = vad_mode
        if compare_primary is not None:
            primary = compare_primary.strip().lower()
            self.config.compare_primary = primary if primary in {"rms", "silero"} else "rms"
            self._compare_primary = self.config.compare_primary
        if vad_profile is not None:
            self.config.vad_profile = vad_profile
        if silero_confidence is not None:
            self.config.silero_confidence = silero_confidence
        if silero_start_ms is not None:
            self.config.silero_start_ms = silero_start_ms
        if silero_stop_ms is not None:
            self.config.silero_stop_ms = silero_stop_ms
        if silero_min_volume is not None:
            self.config.silero_min_volume = silero_min_volume
        # The live barge gate reads InterruptPolicy, not DiscordTurnConfig.
        # Writing only the config fields made these three knobs silent no-ops.
        changes: dict[str, object] = {}
        if silero_barge_min_ms is not None:
            changes["min_duration_ms"] = int(silero_barge_min_ms)
        if silero_barge_min_volume is not None:
            changes["min_volume"] = float(silero_barge_min_volume)
        if post_interrupt_cooldown_ms is not None:
            self.config.post_interrupt_cooldown_ms = post_interrupt_cooldown_ms
            changes["cooldown_ms"] = int(post_interrupt_cooldown_ms)
        if changes:
            self._interrupt = replace(self._interrupt, **changes)
            self._gate.retune(self._interrupt)
        if log_debug is not None:
            self.config.log_debug = log_debug

        self._silero_gate = _build_silero_gate(self.config)
        self._silero_enabled = self._silero_gate is not None
        self.stats.last_vad_mode = "silero"
        self._speech_gate.reset()
        if self._silero_gate:
            self._silero_gate.reset()
        return self.config_snapshot()

    def config_snapshot(self) -> dict[str, object]:
        return {
            "vadMode": "silero",
            "vadProfile": self.config.vad_profile,
            "comparePrimary": self._compare_primary,
            "vadThreshold": self.config.vad_threshold,
            "interruptMinMs": self.config.interrupt_min_ms,
            "botGraceMs": self._interrupt.grace_ms,
            "sileroConfidence": self.config.silero_confidence,
            "sileroStartMs": self.config.silero_start_ms,
            "sileroStopMs": self.config.silero_stop_ms,
            "sileroMinVolume": self.config.silero_min_volume,
            "sileroBargeMinMs": self._interrupt.min_duration_ms,
            "sileroBargeMinVolume": self._interrupt.min_volume,
            "postInterruptCooldownMs": self._interrupt.cooldown_ms,
            "gapHoldMs": self._interrupt.gap_hold_ms,
            "logDebug": self.config.log_debug,
            "sileroEnabled": self._silero_enabled,
        }


def _display_name(user: discord.User) -> str:
    return str(
        getattr(user, "display_name", None)
        or getattr(user, "name", None)
        or getattr(user, "id", "unknown")
    )


def _interrupt_mode(config: DiscordTurnConfig) -> str:
    mode = config.barge_in_mode.strip().lower()
    return mode if mode in _INTERRUPT_MODES else "immediate"


def _build_silero_gate(config: DiscordTurnConfig) -> SileroVadGate | None:
    try:
        return SileroVadGate(
            SileroVadConfig(
                confidence=config.silero_confidence,
                start_ms=config.silero_start_ms,
                stop_ms=max(config.silero_stop_ms, config.silero_start_ms),
                min_volume=config.silero_min_volume,
            )
        )
    except (ImportError, RuntimeError, ValueError, OSError, AttributeError, FileNotFoundError):
        return None
