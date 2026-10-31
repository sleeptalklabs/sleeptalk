"""Browser WebRTC call session (room feed, barge-in, grace)."""
from __future__ import annotations

import asyncio
import contextlib
import inspect
import time
from collections import deque
from dataclasses import dataclass, field, replace

from av import AudioFrame, AudioResampler
from sleeptalk.call_provider import BidirectionalAudioStream
from sleeptalk.barge import BargeAction, BargeCalibration, BargeEvidence, BargeGate, is_backchannel
from sleeptalk.interrupt import AcousticKind, InterruptPolicy
from sleeptalk.session_events import InterruptionSource
from sleeptalk.policy import narration_style_or_default, reply_length_or_default
from sleeptalk.presentation import PresentationState, reduce, start_hearing
from sleeptalk.session_events import SessionEvent, SessionEventType, Speaker, transcript

from sleeptalk.media_core import CORE_FRAME_BYTES, CORE_RATE, pcm_duration_ms
from sleeptalk.voice_loop.smart_turn import SmartTurnScorer
from sleeptalk.voice_loop.utterance import UtteranceAssembler
from sleeptalk.voice_loop.vad import SileroVadGate, SpeechGate, pcm_rms
from sleeptalk.voice_loop.telemetry import (
    VoiceLatencyTrace,
    cascade_turn_identity,
    log_latency_event,
)
from sleeptalk.pause_ahead import StreamAhead, configured_pause_ms, start_from_pause, warm_on_speech
from sleeptalk.web_voice.playback import (
    WebRTCPlaybackController,
    _gain_pcm_s16,
    _pcm_bytes,
    _stereo_to_mono_s16,
)

_WEB_BARGE_INPUT_GAIN = 2.5

@dataclass(slots=True)
class BrowserTransportState:
    """Latest browser getStats snapshot, shared with per-turn traces."""

    latest: dict[str, object] = field(default_factory=dict)

    def update(self, event: dict[str, object]) -> None:
        if event.get("stage") != "transport_stats":
            return
        self.latest = {key: value for key, value in event.items() if key != "stage"}

    @property
    def transport(self) -> str:
        relay = self.latest.get("usesRelay")
        if relay is True:
            return "web-turn"
        if relay is False:
            return "web-direct"
        return "web-webrtc"

    def fields(self) -> dict[str, object]:
        fields = dict(self.latest)
        rtt = fields.get("currentRttMs")
        jitter = fields.get("jitterBufferMs")
        if isinstance(rtt, (int, float)) or isinstance(jitter, (int, float)):
            fields["estimatedDownstreamMs"] = round(
                (float(rtt) / 2 if isinstance(rtt, (int, float)) else 0)
                + (float(jitter) if isinstance(jitter, (int, float)) else 0),
                1,
            )
        return fields


class WebCallSession:
    """WebRTC capture/playout adapter using only public RealtimeSession APIs."""

    PARTICIPANT_ID = 0
    # Speech that starts this soon after the answer's first audio is the
    # caller still talking, not a reply to what they heard.
    REACT_S = 1.0

    def __init__(
        self,
        *,
        session,
        stream: BidirectionalAudioStream,
        call_sid: str,
        surface: str = "web-link",
        persona: str = "agent",
        transport_state: BrowserTransportState | None = None,
        on_transcript=None,
        on_progress=None,
        stack: tuple[str, ...] = (),
        speaker: str = "agent",
        profile_path: str = "",
    ) -> None:
        self.session = session
        continuous = getattr(session, "continuous_input", None)
        self._continuous_input = bool(continuous()) if callable(continuous) else False
        self._live_task_running = False
        self.profile_path = profile_path
        from sleeptalk.voice_loop.telemetry import latency_logging_enabled

        self._diagnostics_baseline = latency_logging_enabled()
        self._diagnostics_changed = False
        self.call_sid = call_sid
        self.surface = surface
        self.persona = persona
        self.policy = session.policy
        self.on_progress = on_progress
        self.on_transcript = on_transcript
        self.transport_state = transport_state or BrowserTransportState()
        self._clock_ms: int | None = None
        values = self.policy.interrupt
        self._gate = BargeGate(
            InterruptPolicy(
                local_responsiveness=bool(getattr(self.policy, "local_responsiveness", False)),
                snappy_responsiveness=bool(getattr(self.policy, "snappy_responsiveness", False)),
                enabled=values.enabled,
                mode=values.mode,
                min_duration_ms=values.min_duration_ms,
                grace_ms=values.grace_ms,
                gap_hold_ms=values.gap_hold_ms,
                cooldown_ms=values.cooldown_ms,
                min_volume=values.min_volume,
                duck_min_ms=80 if getattr(self.policy, "local_responsiveness", False) else 200,
            ),
            AcousticKind.isolated,
            calibration=BargeCalibration(
                # The browser's stream is already gained before the detector
                # sees it (_WEB_BARGE_INPUT_GAIN), so the volume Silero
                # reports is on the same scale the volume bar expects.
                level_scale=1.0,
                # Web has always reset on the first non-speech window. It is
                # tuned that way and feels right; it is not Discord's number
                # by accident.
                dip_tolerance_ms=0.0,
                # Soft barge: the answer pauses while the caller talks, and
                # their words decide: a backchannel ("mm-hmm", "ajá", "嗯")
                # puts it back on, anything else stops it and becomes the turn.
                supports_resume=True,
                supports_duck=bool(getattr(self.policy, "local_responsiveness", False))
                    and values.enabled and values.mode != "off",
            ),
            detector="silero",
            clock=lambda: self._now_ms(),
            emit=self._log_barge_event,
        )
        self.playback = WebRTCPlaybackController(
            stream,
            on_audio_start=self._on_playout_audio_start,
            on_drain=self._on_playout_drain,
        )
        self.session.set_audio_output(self.playback)
        self._signal_stream = stream
        self._participant = {"address": call_sid}
        self._speech_started_at: float | None = None
        self._last_endpoint_decision: dict[str, object] = {}
        self._agent_state_tasks: set[asyncio.Task] = set()
        current_speed = getattr(session, "current_tts_speed", None)
        current_voice = getattr(session, "current_tts_voice", None)
        provider, voice_id = current_voice() if callable(current_voice) else ("", "")
        current_language = getattr(session, "current_tts_language", None)
        self._presentation = PresentationState(
            stack=stack,
            speaker=speaker,
            tts_provider=provider,
            tts_voice_id=voice_id,
            tts_language=(current_language() if callable(current_language) else ""),
            settings_available=bool(profile_path),
            tts_speed=(current_speed() if callable(current_speed) else 1.0),
            narration_style=narration_style_or_default(
                getattr(self.policy, "narration_style", "")
            ),
            reply_length=reply_length_or_default(
                getattr(self.policy, "reply_length", "")
            ),
        )
        bind = getattr(stream, "set_interrupt_handler", None)
        if callable(bind):
            bind(self.interrupt_presentation)
        bind_settings = getattr(stream, "set_settings_handler", None)
        if callable(bind_settings):
            bind_settings(self.apply_settings)
        self._turn_in_flight = False
        self._response_has_output = False
        self._response_generation = 0
        self._closed = False
        self._pending_endpoint_timings: dict[object, deque[dict[str, int]]] = {}
        self._unsubscribe = self.session.subscribe(self._on_session_event)
        self._web_barge_vad: SileroVadGate | None = None
        self._web_barge_vad_failed = False
        self._web_barge_pcm = bytearray()
        self._web_barge_streak_ms = 0.0
        self._barge_fired_for_response = False
        self._web_playback_was_active = False
        self._web_barge_capture_active = False
        # Resume grace: speech that starts within resume_grace_ms of a turn
        # ending, before the agent is heard, is the rest of that turn ("What's
        # two plus two? ... Just the number, please."). It re-asks with both.
        self._last_commit_at: float | None = None
        self._last_turn_pcm = b""
        self._grace_continuation = False
        # Speech while the agent works in silence (after the grace, nothing
        # playing): it used to be dropped, so "stop" couldn't reach it. Now
        # it's heard and settled like a soft barge.
        self._working_speech = False
        self._turn_token: object | None = None
        # A soft barge waiting for the caller's words: the answer is paused.
        self._soft_barge_pending = False
        self._soft_barge_watchdog: asyncio.TimerHandle | None = None
        self._soft_barge_settling = False
        self._barge_at: float | None = None
        # When the answer's first audio went out: a caller who starts
        # speaking within REACT_S of it hasn't heard enough to be answering it.
        self._first_output_at: float | None = None
        self._compare_user = ""
        self._compare_source = ""
        self._compare_spoken = ""
        self._web_barge_preroll: deque[tuple[bytes, bool]] = deque(maxlen=50)
        turns = self.policy.turns
        self._web_barge_rms = SpeechGate(
            threshold=turns.vad_threshold,
            min_speech_ms=turns.interrupt_min_ms,
        )
        capture = self.policy.capture
        stt_config = getattr(getattr(session, "config", None), "stt", None)
        self._local_streaming_input = False
        if getattr(stt_config, "provider", "") == "local":
            from sleeptalk.builder import stt_model_for_provider

            # Use the same default resolution as the recognizer factory, including
            # profiles that specify only provider = "local".
            self._local_streaming_input = (
                stt_model_for_provider("local", getattr(stt_config, "model", ""))
                == "moonshine-small-streaming-en"
            )
        self._input_partial_source = (
            "moonshine_streaming" if self._local_streaming_input
            else getattr(stt_config, "provider", "")
        )
        self._input_partial = ""
        self._input_signal_key = None
        endpoint_factory = getattr(self.policy, "endpointing_config", None)
        endpoint_config = endpoint_factory() if callable(endpoint_factory) else None
        if self._local_streaming_input and endpoint_config is not None:
            endpoint_config = replace(endpoint_config, local_streaming_input=True)
        if endpoint_config is not None and getattr(self.policy, "local_responsiveness", False):
            endpoint_config = replace(endpoint_config, streaming_pause_timing=True)
        smart_turn = (
            SmartTurnScorer(threshold=capture.smart_turn_threshold) if capture.smart_turn else None
        )
        self.assembler = UtteranceAssembler(
            loop=asyncio.get_running_loop(),
            min_bytes=_pcm_bytes(capture.min_utterance_ms),
            silence_seconds=capture.silence_ms / 1000,
            on_utterance=self._handle_utterance,
            should_ignore_audio=lambda: self._turn_in_flight,
            collect_during_ignore=self._collect_during_turn,
            on_speech_started=self._on_speech_started,
            speech_rms_threshold=capture.rms_threshold,
            vad_mode=capture.vad_mode,
            compare_primary=capture.compare_primary,
            silero_vad_config=(
                self.policy.silero_config()
                if callable(getattr(self.policy, "silero_config", None))
                else None
            ),
            smart_turn_scorer=smart_turn,
            endpointing_config=endpoint_config,
            on_smart_turn_event=lambda user_id, event: self._capture_event(
                "smart_turn", user_id, event
            ),
            on_vad_compare_event=lambda user_id, event: self._capture_event(
                "capture_vad_compare", user_id, event
            ),
            on_endpoint_decision=self._on_endpoint_decision,
            on_capture_event=lambda user_id, event: self._capture_event(
                "capture_vad_dropout", user_id, event
            ),
            on_pause=self._on_speech_pause,
            pause_ms=configured_pause_ms(self.session),
            preroll_ms=500,
            # Judge "is the caller done?" by words, as the terminal and Discord
            # do: a streaming listener's live transcript, or a batch
            # listener's early transcript at the pause. Without them every
            # turn ended on silence alone (120 ms for short lines, 1.8 s else).
            turn_completion_provider=self._turn_completion_signal,
            early_transcripts=self._listens_ahead,
        )
        self.assembler.resume_grace_ms = int(getattr(self.policy, "resume_grace_ms", 750) or 0)
        self._stream_ahead = StreamAhead(self.session, self.assembler, self.PARTICIPANT_ID, _stereo_to_mono_s16)
        self._resampler = AudioResampler(format="s16", layout="stereo", rate=CORE_RATE)
        self._incoming = bytearray()
        self._publish_snapshot()

    async def feed_audio_frame(self, frame: AudioFrame) -> None:
        for converted in self._resampler.resample(frame):
            self._incoming.extend(bytes(converted.planes[0])[: converted.samples * 4])
        while len(self._incoming) >= CORE_FRAME_BYTES:
            pcm = bytes(self._incoming[:CORE_FRAME_BYTES])
            del self._incoming[:CORE_FRAME_BYTES]
            if self._continuous_input:
                await self.session.append_audio(
                    _stereo_to_mono_s16(pcm), participant_id=self.PARTICIPANT_ID
                )
                continue
            # A paused answer (soft barge) still counts as active playback;
            # the caller's words must reach a streaming listener all the same.
            soft = getattr(self, "_soft_barge_pending", False)
            forward = (
                (not self.playback.active or soft)
                and (not self._turn_in_flight or self._web_barge_capture_active
                     or getattr(self, "_grace_continuation", False)
                     or getattr(self, "_working_speech", False))
            )
            await self._watch_web_barge_in(pcm, forwarded_to_streaming=forward)
            if forward:
                await self.session.append_audio(
                    _stereo_to_mono_s16(pcm), participant_id=self.PARTICIPANT_ID
                )
            self.assembler.observe(self.PARTICIPANT_ID, self._participant, pcm)
            self._stream_ahead.sync()
            if self._input_partial_source and forward:
                self._sync_streaming_partial()

    def _sync_streaming_partial(self) -> None:
        signal = self._turn_completion_signal(self.PARTICIPANT_ID)
        source = self._input_partial_source
        if signal is not None and getattr(signal, "source", "") == source:
            text = str(getattr(signal, "transcript", "") or "")
        elif signal is None or not getattr(signal, "source", ""):
            preview = getattr(self.session, "input_transcript_preview", None)
            text = preview(self.PARTICIPANT_ID) if callable(preview) else ""
        else:
            return
        if not self._input_partial_source or source != self._input_partial_source:
            return
        if text != self._input_partial:
            self._input_partial = text
            # Partials are revisable display state; only run_turn commits user text.
            self.session.emit(transcript(
                self.session.session_id, 0, speaker=Speaker.USER, text=text, final=False,
                source=source,
            ))
            self._capture_event("input_partial", self.PARTICIPANT_ID, {
                "textChars": len(text), "source": source,
            })
        key = (text, getattr(signal, "is_final", False))
        if self._local_streaming_input and key != self._input_signal_key:
            self._input_signal_key = key
            self.assembler.recheck_streaming_endpoint(self.PARTICIPANT_ID)

    def _now_ms(self) -> int:
        if self._clock_ms is not None:
            return self._clock_ms
        return int(time.monotonic() * 1000)

    def _on_playout_audio_start(self) -> None:
        if self._gate.idle:
            self._gate.turn_started()
        self._gate.audio_started()
        self._web_playback_was_active = True
        self._reset_web_barge_detector()

    def _on_playout_drain(self) -> None:
        if self._gate.idle:
            self._web_playback_was_active = False
            return
        self._gate.audio_drained()
        self._web_playback_was_active = False
        if not self._turn_in_flight:
            self._gate.turn_ended()

    async def _watch_web_barge_in(
        self, pcm: bytes, *, forwarded_to_streaming: bool = False
    ) -> None:
        playing = self.playback.active
        if playing and not self._web_playback_was_active:
            self._on_playout_audio_start()
        elif not playing and self._web_playback_was_active:
            self._on_playout_drain()
        if self._turn_in_flight and self._gate.idle:
            self._gate.turn_started()
        if self._gate.idle:
            return
        if self._barge_fired_for_response:
            return
        self._web_barge_preroll.append((pcm, forwarded_to_streaming))
        detector_pcm = _gain_pcm_s16(pcm, _WEB_BARGE_INPUT_GAIN)
        rms_speech = self._web_barge_rms.observe(detector_pcm)
        if self._web_barge_vad_failed:
            await self._offer_detector_speech(
                rms_speech,
                pcm_duration_ms(detector_pcm),
                source="rms_fallback",
                rms=self._web_barge_rms.last_rms,
                volume=1.0,
            )
            return
        self._web_barge_pcm.extend(detector_pcm)
        if len(self._web_barge_pcm) < CORE_FRAME_BYTES * 2:
            return
        analysis_pcm = bytes(self._web_barge_pcm)
        self._web_barge_pcm.clear()
        try:
            if self._web_barge_vad is None:
                config_factory = getattr(self.policy, "silero_config", None)
                config = config_factory() if callable(config_factory) else None
                if config is not None and getattr(self.policy, "local_responsiveness", False):
                    # The barge gate already requires sustained evidence. Avoid
                    # paying the listening detector's attack/hangover as well.
                    config = replace(config, start_ms=32, stop_ms=120)
                self._web_barge_vad = SileroVadGate(config)
            observation = await self._web_barge_vad.observe(analysis_pcm)
        except (ImportError, RuntimeError, ValueError, OSError, AttributeError) as exc:
            self._web_barge_vad_failed = True
            self._capture_event(
                "barge_in_vad_fallback",
                self.PARTICIPANT_ID,
                {"error": f"{type(exc).__name__}: {str(exc)[:200]}"},
            )
            await self._offer_detector_speech(
                rms_speech,
                pcm_duration_ms(analysis_pcm),
                source="rms_fallback",
                rms=self._web_barge_rms.last_rms,
                volume=1.0,
            )
            return
        await self._offer_detector_speech(
            observation.is_speech and (
                not getattr(self.policy, "local_responsiveness", False)
                or observation.state != "STOPPING"
            ),
            pcm_duration_ms(analysis_pcm),
            source="silero",
            rms=pcm_rms(analysis_pcm),
            confidence=observation.confidence,
            volume=observation.volume,
        )

    async def _offer_detector_speech(
        self,
        speech: bool,
        frame_ms: float,
        *,
        source: str,
        rms: int,
        confidence: float | None = None,
        volume: float = 1.0,
    ) -> None:
        decision = self._gate.observe(
            BargeEvidence(
                channel=str(self.PARTICIPANT_ID),
                duration_ms=frame_ms,
                level=volume,
                voiced=speech,
                confidence=confidence,
                raw_rms=rms,
            )
        )
        self._web_barge_streak_ms = decision.sustain_ms
        if decision.action is BargeAction.duck:
            await self.playback.pause("local-duck")
        elif decision.action is BargeAction.resume:
            await self.playback.resume("local-duck-release")
        if decision.fired:
            await self._fire_web_barge_in(
                source=source,
                rms=rms,
                confidence=confidence,
                volume=volume,
                decision=decision,
            )

    def _reset_web_barge_detector(self) -> None:
        self._web_barge_pcm.clear()
        self._web_barge_streak_ms = 0.0
        self._web_barge_preroll.clear()
        self._web_barge_rms.reset()
        if self._web_barge_vad is not None:
            self._web_barge_vad.reset()

    async def _fire_web_barge_in(
        self,
        *,
        source: str,
        rms: int,
        confidence: float | None = None,
        volume: float | None = None,
        decision: object | None = None,
    ) -> None:
        if self._barge_fired_for_response:
            return
        self._barge_fired_for_response = True
        # When the caller started talking, not when the detector was sure.
        onset_ms = float(getattr(self, "_web_barge_streak_ms", 0) or 0)
        self._barge_at = time.monotonic() - onset_ms / 1000
        self._capture_event(
            "barge_in_detected",
            self.PARTICIPANT_ID,
            {
                "source": source,
                "rms": rms,
                "confidence": confidence,
                "volume": volume,
                "volumeThreshold": self.policy.interrupt.min_volume,
                "sustainMs": round(self._web_barge_streak_ms),
                "inputGain": _WEB_BARGE_INPUT_GAIN,
                "bargeId": getattr(decision, "barge_id", ""),
                "floorState": getattr(getattr(decision, "floor_state", None), "value", ""),
                "floorReason": getattr(decision, "reason", ""),
                "downgraded": getattr(decision, "downgraded", False),
            },
        )
        soft = getattr(decision, "action", None) is BargeAction.pause
        if soft and await self.playback.pause("barge-in"):
            # Hold the answer; the caller's words decide (see _settle_soft_barge).
            self._soft_barge_pending = True
            self._arm_soft_barge_watchdog()
        else:
            await self.playback.stop("barge-in")
            if getattr(self.policy, "local_responsiveness", False):
                self._capture_event("barge_playback_stopped", self.PARTICIPANT_ID, {
                    "bargeId": getattr(decision, "barge_id", ""),
                    "estimatedSpeechToHostStopMs": round((time.monotonic() - self._barge_at) * 1000),
                    "measurement": "host_output_cleared",
                })
            await self.session.interrupt_presentation()
        self._web_barge_capture_active = True
        preroll = b"".join(pcm for pcm, _forwarded in self._web_barge_preroll)
        missing = b"".join(pcm for pcm, forwarded in self._web_barge_preroll if not forwarded)
        if missing:
            await self.session.append_audio(
                _stereo_to_mono_s16(missing), participant_id=self.PARTICIPANT_ID
            )
        if preroll:
            self.assembler.inject_preroll(self.PARTICIPANT_ID, self._participant, preroll)

    # Words that never came (a cough, a door): put the answer back on.
    SOFT_BARGE_TIMEOUT_SECONDS = 4.0

    def _arm_soft_barge_watchdog(self) -> None:
        if self._soft_barge_watchdog is not None:
            self._soft_barge_watchdog.cancel()
        loop = asyncio.get_running_loop()
        self._soft_barge_watchdog = loop.call_later(self.SOFT_BARGE_TIMEOUT_SECONDS, self._soft_barge_timed_out)

    def _soft_barge_timed_out(self) -> None:
        """No words settled the barge: resume, unless the caller is still talking or being heard."""
        if not getattr(self, "_soft_barge_pending", False):
            return
        still_talking = self.PARTICIPANT_ID in self.assembler.buffers
        if still_talking or getattr(self, "_soft_barge_settling", False):
            self._arm_soft_barge_watchdog()
            return
        asyncio.ensure_future(self._resume_after_soft_barge("no words"))

    def _disarm_soft_barge(self) -> None:
        self._soft_barge_pending = False
        if getattr(self, "_soft_barge_watchdog", None) is not None:
            self._soft_barge_watchdog.cancel()
            self._soft_barge_watchdog = None

    async def _resume_after_soft_barge(self, reason: str, transcript: str = "") -> None:
        if not getattr(self, "_soft_barge_pending", False) or self._closed:
            return
        self._disarm_soft_barge()
        self._gate.resolve(backchannel=True)
        self._barge_fired_for_response = False
        self._web_barge_capture_active = False
        self._reset_web_barge_detector()
        await self.playback.resume("backchannel")
        self._capture_event("barge_in_resolved", self.PARTICIPANT_ID,
                            {"disposition": "resumed", "reason": reason, "transcriptChars": len(transcript)})
        self._clear_hearing()

    async def _settle_soft_barge(self, pcm: bytes) -> str | None:
        """The caller's words after a soft barge: resume, or stop and return them.

        Returns the transcript the new turn should use (so it isn't
        transcribed twice), or None when the answer was put back on.
        """
        transcript = ""
        failed = False
        self._soft_barge_settling = True
        try:
            result = await self.session.transcribe(_stereo_to_mono_s16(pcm), participant_id=self.PARTICIPANT_ID)
            transcript = str(getattr(result, "text", result) or "").strip()
        except Exception as exc:  # noqa: BLE001 - unheard words count as a real interruption
            failed = True
            self._capture_event("barge_in_transcribe_failed", self.PARTICIPANT_ID,
                                {"error": f"{type(exc).__name__}: {str(exc)[:160]}"})
        finally:
            self._soft_barge_settling = False
        if not getattr(self, "_soft_barge_pending", False):
            # The watchdog already put the answer back on; these words are a new turn.
            if not transcript or is_backchannel(transcript) or await self._answered_pending(transcript):
                return None
            return transcript
        if not failed and await self._answered_pending(transcript):
            # "Yes" over the question, or "what are you doing?": answered; what was playing goes on.
            await self._resume_after_soft_barge("answered", transcript)
            return None
        if not failed and is_backchannel(transcript):
            await self._resume_after_soft_barge("backchannel", transcript)
            return None
        self._disarm_soft_barge()
        self._gate.resolve(backchannel=False)
        await self.playback.stop("barge-in")
        await self.session.interrupt_presentation()
        self._capture_event("barge_in_resolved", self.PARTICIPANT_ID,
                            {"disposition": "cancelled", "reason": "transcript", "transcriptChars": len(transcript)})
        return transcript

    def _collect_during_turn(self, user_id) -> bool:
        """While a turn is in flight: the barge-in capture, or a continuation in the resume grace."""
        if user_id != self.PARTICIPANT_ID:
            return False
        if self._web_barge_capture_active:
            return True
        if getattr(self, "_grace_continuation", False) and user_id in self.assembler.buffers:
            return True  # a continuation under way is kept to its end
        if self._in_resume_grace():
            self._grace_continuation = True
            return True
        if getattr(self, "_working_speech", False) and user_id in self.assembler.buffers:
            return True
        if self._turn_in_flight and not self.playback.active and not getattr(self, "_grace_continuation", False):
            self._working_speech = True
            return True
        return False

    def _in_resume_grace(self) -> bool:
        grace_ms = int(getattr(self.assembler, "resume_grace_ms", 0) or 0)
        committed = getattr(self, "_last_commit_at", None)
        if not grace_ms or committed is None or not self._turn_in_flight:
            return False
        if self.playback.active or self._response_has_output:
            return False  # the agent is being heard: that's a barge-in, not a continuation
        return (time.monotonic() - committed) * 1000 <= grace_ms

    async def _settle_working_speech(self, pcm: bytes) -> str | None:
        """Words said while the agent works in silence: a new turn, or nothing.

        A backchannel or noise leaves the work alone; anything else ("stop",
        a new question) interrupts it and becomes the turn. Returns the text
        the turn should use, or None when the work goes on.
        """
        try:
            result = await self.session.transcribe(_stereo_to_mono_s16(pcm), participant_id=self.PARTICIPANT_ID)
            transcript = str(getattr(result, "text", result) or "").strip()
        except Exception as exc:  # noqa: BLE001 - unheard words are still a turn: run it on the audio
            self._capture_event("working_speech_transcribe_failed", self.PARTICIPANT_ID,
                                {"error": f"{type(exc).__name__}: {str(exc)[:160]}"})
            await self.session.interrupt_presentation()
            return ""
        if await self._answered_pending(transcript):
            self._clear_hearing()
            return None
        if is_backchannel(transcript):
            self._capture_event("working_speech", self.PARTICIPANT_ID,
                                {"disposition": "ignored", "transcriptChars": len(transcript)})
            self._clear_hearing()
            return None
        self._capture_event("working_speech", self.PARTICIPANT_ID,
                            {"disposition": "interrupted", "transcriptChars": len(transcript)})
        await self.session.interrupt_presentation()
        return transcript

    async def _answered_pending(self, transcript: str) -> bool:
        """Words the call answers itself while the agent works, leaving the
        work running: a yes or no to its permission question, or "what are
        you doing?"."""
        for name, disposition in (("answer_pending", "permission_answered"), ("answer_status", "status_answered")):
            answer = getattr(self.session, name, None)
            if callable(answer) and await answer(transcript):
                self._capture_event("working_speech", self.PARTICIPANT_ID,
                                    {"disposition": disposition, "transcriptChars": len(transcript)})
                return True
        return False

    def _log_barge_event(self, stage: str, fields: dict) -> None:
        self._capture_event(
            stage,
            self.PARTICIPANT_ID,
            {**fields, "trace_id": fields.get("bargeId") or None},
        )

    def _capture_event(self, stage: str, user_id, event: dict) -> None:
        if event.get("kind") == "utterance_committed":
            if self._closed:
                return
            fields = {
                "endpointSilenceMs": event["endpointSilenceMs"]
            } if "endpointSilenceMs" in event else {}
            self._pending_endpoint_timings.setdefault(user_id, deque()).append(fields)
            stage = "utterance_committed"
        elif event.get("kind") == "early_end_shadow":
            stage = "early_end_shadow"
        log_latency_event(
            surface=self.surface,
            transport=self.transport_state.transport,
            persona=self.persona,
            stage=stage,
            userId=user_id,
            localResponsiveness=bool(getattr(getattr(self, "policy", None), "local_responsiveness", False)),
            **event,
        )

    def _turn_completion_signal(self, user_id) -> object:
        signal = getattr(self.session, "turn_completion_signal", None)
        if not callable(signal) or user_id != self.PARTICIPANT_ID:
            return None
        try:
            return signal(self.PARTICIPANT_ID)
        except Exception:  # noqa: BLE001 - endpointing falls back to silence
            return None

    def _listens_ahead(self) -> bool:
        listens = getattr(self.session, "listens_ahead", None)
        return bool(listens()) if callable(listens) else False

    def _on_speech_pause(self, user_id, pcm: bytes) -> None:
        # Same conversion as the committed turn, so the early transcript can be
        # matched byte for byte against what endpointing finally sends.
        if not self._closed and user_id == self.PARTICIPANT_ID:
            task = start_from_pause(self.session, _stereo_to_mono_s16(pcm), self.PARTICIPANT_ID)
            if task is not None:
                self.assembler.watch_early_transcript(user_id, task)

    def _on_endpoint_decision(self, user_id, decision) -> None:
        event = {
            "reason": decision.reason,
            "silenceMs": decision.silence_ms,
            "thresholdMs": decision.threshold_ms,
            "shouldEndpoint": decision.should_endpoint,
            # What the decision was judged on, to tell a lagging transcript
            # from a misjudged one.
            "score": round(decision.semantic_score, 2),
            "signalSource": decision.source,
            "transcriptChars": len(decision.transcript or ""),
        }
        self._last_endpoint_decision = event
        self._capture_event("endpoint_decision", user_id, event)

    def _on_speech_started(self, _participant) -> None:
        if self.on_progress is not None:
            self.on_progress("speech_received")
        heard = getattr(self.session, "note_caller_speech", None)
        if callable(heard):
            heard()  # a background job's announcement waits for the caller
        if self._speech_started_at is None:
            self._speech_started_at = time.perf_counter()
            warm_on_speech(self.session)
        nxt = start_hearing(self._presentation)
        if nxt is self._presentation:
            return
        self._presentation = nxt
        self._publish_snapshot()

    def _clear_hearing(self) -> None:
        if not self._presentation.hearing:
            return
        self._presentation = replace(self._presentation, hearing=False)
        self._publish_snapshot()

    async def _handle_utterance(self, participant, pcm: bytes) -> None:
        del participant
        pending = self._pending_endpoint_timings.get(self.PARTICIPANT_ID)
        endpoint_fields = pending.popleft() if pending else {}
        if not pending:
            self._pending_endpoint_timings.pop(self.PARTICIPANT_ID, None)
        if self._closed:
            return
        self._web_barge_capture_active = False
        if pcm_rms(pcm) < 1:
            self._speech_started_at = None
            self._clear_hearing()
            if getattr(self, "_soft_barge_pending", False):
                await self._resume_after_soft_barge("silence")
            return
        barge_text: str | None = None
        if getattr(self, "_soft_barge_pending", False):
            barge_text = await self._settle_soft_barge(pcm)
            if barge_text is None:
                self._speech_started_at = None
                return
        if getattr(self, "_working_speech", False) and barge_text is None:
            self._working_speech = False
            if self._turn_in_flight and not getattr(self, "_grace_continuation", False):
                barge_text = await self._settle_working_speech(pcm)
                if barge_text is None:
                    self._speech_started_at = None
                    return
        barge_at = getattr(self, "_barge_at", None)
        self._barge_at = None
        committed = getattr(self, "_last_commit_at", None)
        grace_ms = int(getattr(getattr(self, "assembler", None), "resume_grace_ms", 0) or 0)
        first_output = getattr(self, "_first_output_at", None)
        if (barge_at is not None and committed is not None and grace_ms and self._last_turn_pcm
                and ((barge_at - committed) * 1000 <= grace_ms * 2
                     or (first_output is not None and first_output >= committed
                         and barge_at - first_output <= self.REACT_S
                         and barge_at - committed <= self.REACT_S + 3))):
            # The caller kept talking and the agent had only just started
            # answering the first half ("我想了解一下" … "茶的历史"): the rest of
            # the same turn, not a new one. Ask again with both halves.
            self._capture_event("grace_continuation", self.PARTICIPANT_ID,
                                {"afterBarge": True, "firstBytes": len(self._last_turn_pcm), "restBytes": len(pcm)})
            pcm = self._last_turn_pcm + pcm
            barge_text = None
            self._grace_continuation = False
        if getattr(self, "_grace_continuation", False):
            self._grace_continuation = False
            if self._turn_in_flight and barge_text is None:
                # The rest of the last turn: cancel the answer to the first
                # half and ask again with both halves.
                self._capture_event("grace_continuation", self.PARTICIPANT_ID,
                                    {"firstBytes": len(self._last_turn_pcm), "restBytes": len(pcm)})
                await self.session.interrupt_presentation()
                pcm = self._last_turn_pcm + pcm
        self._last_commit_at = time.monotonic()
        self._last_turn_pcm = pcm
        transport_fields = self.transport_state.fields()
        identity = cascade_turn_identity(
            getattr(self.session, "config", None),
            session_id=str(getattr(self.session, "session_id", "") or ""),
        )
        trace = VoiceLatencyTrace(
            surface=self.surface,
            transport=self.transport_state.transport,
            persona=self.persona,
            metadata={
                **identity,
                "callProvider": "webrtc",
                "userId": "browser",
                **transport_fields,
            },
        )
        trace.mark(
            "utterance_ready",
            audioMs=pcm_duration_ms(pcm),
            audioBytes=len(pcm),
            **endpoint_fields,
            speechToEndpointMs=(
                round((time.perf_counter() - self._speech_started_at) * 1000)
                if self._speech_started_at is not None
                else None
            ),
        )
        self._speech_started_at = None
        before = (
            len(self.session.events)
            if self.on_transcript is not None and hasattr(self.session, "events")
            else 0
        )
        self._turn_in_flight = True
        token = self._turn_token = object()
        try:
            outcome = await self.session.run_turn(
                # A soft barge's words were already transcribed to decide it.
                **({"text": barge_text} if barge_text else {"pcm": _stereo_to_mono_s16(pcm)}),
                extras={
                    "participant_id": self.PARTICIPANT_ID,
                    "latency_trace": trace,
                    **transport_fields,
                },
            )
            if self.on_transcript is not None:
                events = (
                    self.session.events[before:]
                    if hasattr(self.session, "events")
                    else []
                )
                user_text = next(
                    (
                        str(event.payload.get("text", ""))
                        for event in events
                        if event.type is SessionEventType.TRANSCRIPT_FINAL
                        and event.payload.get("speaker") == Speaker.USER.value
                    ),
                    "",
                )
                if user_text and user_text != self._compare_user:
                    self._compare_user = user_text
                    self._compare_source = ""
                    self._compare_spoken = ""
                if (
                    self._compare_user
                    and not self._compare_source
                    and not self._compare_spoken
                    and outcome.generated
                ):
                    await self._publish_compare(
                        self._compare_user,
                        " ".join(outcome.generated),
                        "",
                        "",
                    )
            trace.finish("turn_complete", turnId=outcome.turn_id, status=outcome.status)
        except Exception as exc:  # noqa: BLE001 - call stays alive after a failed turn
            trace.finish("turn_error", error=f"{type(exc).__name__}: {str(exc)[:200]}")
            self._clear_hearing()
        finally:
            # A turn cancelled for a newer one must not mark the newer one done.
            if getattr(self, "_turn_token", token) is token:
                self._turn_in_flight = False

    def _on_session_event(self, event: SessionEvent) -> None:
        self._note_compare(event)
        self._queue_presentation(event)
        state = task = detail = ""
        if self._continuous_input and event.type is SessionEventType.AGENT_EVENT and str(event.payload.get("name", "")).startswith("hybrid.task."):
            self._live_task_running = event.payload["name"] == "hybrid.task.started"
            state = "answering" if self._response_has_output else ("working" if self._live_task_running else "idle")
        elif event.type is SessionEventType.RESPONSE_STARTED:
            self._turn_in_flight = True
            self._barge_fired_for_response = False
            self._response_has_output = False
            self._first_output_at = None
            self._response_generation += 1
            if self._gate.idle:
                self._gate.turn_started()
            state = "working"
        elif event.type is SessionEventType.RESPONSE_FIRST_OUTPUT:
            if self.on_progress is not None:
                self.on_progress("agent_response")
            if not self._response_has_output:
                self._first_output_at = time.monotonic()
            self._response_has_output = True
            state = "answering"
        elif event.type in {
            SessionEventType.RESPONSE_COMPLETED,
            SessionEventType.RESPONSE_CANCELLED,
        }:
            self._turn_in_flight = False
            if not self.playback.active and not self._gate.idle:
                self._gate.turn_ended()
            if self._defer_idle_until_playout(event.turn_id or 0):
                return
            self._response_has_output = False
            state = "working" if self._live_task_running else "idle"
        elif event.type is SessionEventType.PROGRESS:
            state = "answering" if self._response_has_output else "working"
            task = str(event.payload.get("text") or "")
            detail = str(event.payload.get("detail") or "")
        elif event.type is SessionEventType.TOOL_STARTED:
            state = "answering" if self._response_has_output else "working"
            task = str(event.payload.get("name") or "")
            detail = str(event.payload.get("detail") or "")
        elif event.type is SessionEventType.INTERRUPTION:
            state = "listening"
        if not state:
            return
        send = getattr(self._signal_stream, "send_agent_state", None)
        if not callable(send):
            return
        state_task = asyncio.create_task(
            self._push_agent_state(send, state, task, detail),
            name=f"web-agent-state-{event.turn_id or 0}",
        )
        self._agent_state_tasks.add(state_task)
        state_task.add_done_callback(self._agent_state_tasks.discard)

    def _note_compare(self, event: SessionEvent) -> None:
        if event.type is not SessionEventType.TRANSCRIPT_FINAL:
            return
        text = " ".join(str(event.payload.get("text", "")).split())
        if not text:
            return
        speaker = event.payload.get("speaker")
        owner = event.payload.get("owner")
        if speaker == Speaker.USER.value:
            if text != self._compare_user:
                self._compare_user = text
                self._compare_source = ""
                self._compare_spoken = ""
            return
        if speaker != Speaker.ASSISTANT.value:
            return
        if owner == "specialist":
            self._compare_source = text
        else:
            self._compare_spoken = text
        if self.on_transcript is None or not self._compare_user:
            return
        assistant = self._compare_spoken or self._compare_source
        state_task = asyncio.create_task(
            self._publish_compare(
                self._compare_user,
                assistant,
                self._compare_source,
                self._compare_spoken,
            ),
            name=f"web-compare-transcript-{event.turn_id or 0}",
        )
        self._agent_state_tasks.add(state_task)
        state_task.add_done_callback(self._agent_state_tasks.discard)

    async def _publish_compare(
        self,
        user_text: str,
        assistant_text: str,
        source_text: str,
        spoken_text: str,
    ) -> None:
        callback = self.on_transcript
        if callback is None:
            return
        try:
            result = callback(user_text, assistant_text, source_text, spoken_text)
        except TypeError:
            result = callback(user_text, assistant_text)
        if inspect.isawaitable(result):
            await result

    def _queue_presentation(self, event: SessionEvent) -> None:
        if (event.type is SessionEventType.TRANSCRIPT_FINAL
                and event.payload.get("speaker") == Speaker.USER.value):
            self._input_partial = ""
            self._input_signal_key = None
        try:
            self._presentation = reduce(self._presentation, event)
        except Exception as exc:  # noqa: BLE001 - display-only snapshot
            print(f"presentation reduce FAILED: {type(exc).__name__}: {exc}")
            return
        self._publish_snapshot(event.turn_id or 0)

    def _publish_snapshot(self, turn_id: int = 0) -> None:
        from sleeptalk.voice_loop.telemetry import latency_logging_enabled

        self._presentation = replace(self._presentation, diagnostics_enabled=latency_logging_enabled())
        send = getattr(self._signal_stream, "send_presentation", None)
        if not callable(send):
            return
        payload = self._presentation.to_json()
        state_task = asyncio.create_task(
            self._push_presentation(send, payload),
            name=f"web-presentation-{turn_id}",
        )
        self._agent_state_tasks.add(state_task)
        state_task.add_done_callback(self._agent_state_tasks.discard)

    async def _push_presentation(self, send, payload: dict) -> None:
        try:
            await send(payload)
        except Exception as exc:  # noqa: BLE001 - display-only signaling
            if not _call_ended(exc):
                print(f"presentation push FAILED: {type(exc).__name__}: {exc}")

    def _defer_idle_until_playout(self, turn_id: int) -> bool:
        if not self.playback.active:
            return False
        send = getattr(self._signal_stream, "send_agent_state", None)
        if not callable(send):
            return False
        generation = self._response_generation
        state_task = asyncio.create_task(
            self._push_idle_after_playout(send, generation),
            name=f"web-agent-idle-{turn_id}",
        )
        self._agent_state_tasks.add(state_task)
        state_task.add_done_callback(self._agent_state_tasks.discard)
        return True

    async def _push_idle_after_playout(self, send, generation: int) -> None:
        await self.playback.wait_until_idle()
        if self._closed or generation != self._response_generation:
            return
        self._response_has_output = False
        await self._push_agent_state(send, "working" if self._live_task_running else "idle", "", "")

    async def _push_agent_state(self, send, state: str, task: str, detail: str) -> None:
        try:
            await send(state=state, task=task, detail=detail)
        except Exception as exc:  # noqa: BLE001 - display-only signaling
            if not _call_ended(exc):
                print(f"agent_state push FAILED: {type(exc).__name__}: {exc}")

    async def close(self, *, reason: str = "agent_closed") -> None:
        if self._closed:
            return
        self._disarm_soft_barge()
        self._closed = True
        if self._diagnostics_changed:
            self._set_diagnostics(self._diagnostics_baseline)
        self._pending_endpoint_timings.clear()
        self._stream_ahead.close()
        close_with_reason = getattr(self._signal_stream, "close", None)
        if callable(close_with_reason):
            with contextlib.suppress(TypeError, ValueError):
                if "reason" in inspect.signature(close_with_reason).parameters:
                    self._signal_stream.pending_close_reason = reason
        self.assembler.cleanup()
        self._unsubscribe()
        for task in tuple(self._agent_state_tasks):
            task.cancel()
        if self._agent_state_tasks:
            await asyncio.gather(*self._agent_state_tasks, return_exceptions=True)
        await self.session.aclose()
        await self.playback.close()

    def apply_settings(self, payload: dict) -> None:
        """Apply an in-call setting change from the browser.

        Unknown keys are ignored rather than rejected: the settings menu ships
        controls before every one of them is wired, and an unrecognised key
        must never break a live call.
        """
        if self._closed:
            return
        if type(payload.get("diagnostics")) is bool:
            self._set_diagnostics(payload["diagnostics"])
        if payload.get("openSettings") is True:
            task = asyncio.create_task(self._open_settings(), name="web-open-settings")
            self._agent_state_tasks.add(task)
            task.add_done_callback(self._agent_state_tasks.discard)
        select_job = payload.get("selectJob")
        if isinstance(select_job, str) and select_job:
            # A tab in the worker panel: the caller's bare words now go to it.
            jobs = getattr(self.session, "jobs", None)
            if jobs is not None:
                jobs.select(select_job)
        cancel_job = payload.get("cancelJob")
        if cancel_job is True or isinstance(cancel_job, str) and cancel_job:
            # The worker panel's Cancel button: no words to misread. It names
            # its job; a bare true (an older page) means the live one shown.
            jobs = getattr(self.session, "jobs", None)
            target = None
            if jobs is not None:
                target = (next((job for job in jobs.jobs if job.id == cancel_job), None)
                          if isinstance(cancel_job, str) else jobs.active)
            if target is not None and (target.active or target.queued):
                task = asyncio.create_task(jobs.cancel(target), name="web-cancel-job")
                self._agent_state_tasks.add(task)
                task.add_done_callback(self._agent_state_tasks.discard)
        narration_key = (
            "narrationStyle"
            if "narrationStyle" in payload
            else "narration"
            if "narration" in payload
            else None
        )
        if narration_key is not None:
            requested = payload.get(narration_key)
            setter = getattr(self.session, "set_narration_style", None)
            if callable(setter):
                self.policy.narration_style = setter(requested)
            else:
                self.policy.narration_style = narration_style_or_default(requested)
        if "replyLength" in payload:
            requested = payload.get("replyLength")
            setter = getattr(self.session, "set_reply_length", None)
            if callable(setter):
                self.policy.reply_length = setter(requested)
            else:
                self.policy.reply_length = reply_length_or_default(requested)
        self._presentation = replace(
            self._presentation,
            narration_style=narration_style_or_default(
                getattr(self.policy, "narration_style", "")
            ),
            reply_length=reply_length_or_default(
                getattr(self.policy, "reply_length", "")
            ),
        )
        voice = payload.get("ttsVoice")
        if isinstance(voice, str) and voice.strip():
            task = asyncio.create_task(self._apply_voice(voice.strip()), name="web-voice-switch")
            self._agent_state_tasks.add(task)
            task.add_done_callback(self._agent_state_tasks.discard)
        raw = payload.get("ttsSpeed")
        if raw is None:
            self._publish_snapshot()
            return
        try:
            requested = float(raw)
        except (TypeError, ValueError):
            self._publish_snapshot()
            return
        setter = getattr(self.session, "set_tts_speed", None)
        if not callable(setter):
            self._publish_snapshot()
            return
        applied = setter(requested)
        self._presentation = replace(self._presentation, tts_speed=applied)
        self._publish_snapshot()

    def _set_diagnostics(self, enabled: bool) -> None:
        from sleeptalk.home import resolve_layout
        from sleeptalk.logging_setup import profile_logs_dir, setup_latency_log

        directory = profile_logs_dir(self.profile_path) if self.profile_path else resolve_layout().logs_dir
        setup_latency_log(logs_dir=directory, enabled=enabled)
        self._diagnostics_changed = True

    async def _open_settings(self) -> None:
        """Give the caller the full settings page: a single-use link to it
        through the sealed channel, which the call page offers to open."""
        notice, url = await open_host_settings(self.profile_path)
        self._presentation = replace(self._presentation, settings_notice=notice, settings_url=url)
        self._publish_snapshot()

    async def _apply_voice(self, voice_id: str) -> None:
        """Prove, switch, save. A failure keeps the current voice and tells the page why."""
        from sleeptalk.session import VoiceUnavailableError

        setter = getattr(self.session, "set_tts_voice", None)
        from sleeptalk.session import VOICE_ID_PATTERN

        if not callable(setter) or not VOICE_ID_PATTERN.fullmatch(voice_id.strip()):
            self._presentation = replace(self._presentation, voice_notice="That voice isn't available.")
            self._publish_snapshot()
            return
        try:
            applied = await setter(voice_id)
        except VoiceUnavailableError as exc:
            notice = {
                "invalid_credential": "The provider rejected the saved key.",
                "permission_denied": "The saved key can't use that voice.",
                "quota_or_rate_limit": "The provider is rate limiting or out of credit.",
                "network_timeout": "The provider didn't answer in time.",
                "voice_unsupported": "This voice runs on the computer itself and can't be changed from here.",
            }.get(exc.code, "That voice isn't available on your account.")
            self._presentation = replace(self._presentation, voice_notice=notice)
            self._publish_snapshot()
            return
        saved = ""
        if self.profile_path:
            try:
                from sleeptalk.profile_edit import set_tts_voice

                set_tts_voice(self.profile_path, applied)
            except (OSError, ValueError):
                saved = " (not saved to the profile)"
        self._presentation = replace(self._presentation, tts_voice_id=applied, voice_notice=f"Voice changed{saved}.")
        self._publish_snapshot()

    async def interrupt_presentation(self, *, clear_output: bool = True) -> None:
        """Public interruption boundary for peer cancel and WebRTC barge-in.

        `clear_output` marks the deliberate press of Stop, as opposed to the
        barge path below, which interrupts without it. A deliberate stop
        gives up pending specialist findings; talking over an answer leaves
        them to be resumed.
        """
        if clear_output:
            await self.playback.stop("user-cancel")
        await self.session.interrupt_presentation(
            source=(
                InterruptionSource.CANCEL
                if clear_output
                else InterruptionSource.USER_SPEECH
            )
        )


# One settings server per host process: a second request replaces the first.
# It outlives the call on purpose, so edits can finish after hanging up.
_settings_server: asyncio.Task | None = None


async def open_host_settings(profile_path: str) -> tuple[str, str]:
    """A single-use link to this profile's settings page, for the device the
    call is on: (the sentence the call page shows, the link or "").

    The page is the host's own settings page reached through the sealed
    setup channel (``remote_settings``), so it works on a phone. Without the
    hosted call service it opens in this machine's browser instead.
    """
    global _settings_server
    from pathlib import Path

    from sleeptalk.settings_page import SettingsPage

    path = Path(profile_path) if profile_path else None
    if path is None or not path.is_file():
        return "Settings aren't available for this call.", ""
    if _settings_server is not None and not _settings_server.done():
        _settings_server.cancel()
    loop = asyncio.get_running_loop()
    ready: asyncio.Future[str] = loop.create_future()
    page = SettingsPage(path, profiles_dir=path.parent)
    from sleeptalk import remote_settings

    _settings_server = asyncio.create_task(
        remote_settings.serve_link(page, ready=lambda url, ticket: ready.done() or ready.set_result(url)),
        name="phone-settings-link",
    )
    await asyncio.wait({ready, _settings_server}, timeout=15, return_when=asyncio.FIRST_COMPLETED)
    if not ready.done():
        # No hosted service (or it failed): fall back, never break the call.
        _settings_server.cancel()
        return await _open_on_host(path)
    url = ready.result()
    return "Settings are ready. They open once, through an encrypted link; changes apply to your next call.", url


async def _open_on_host(path) -> tuple[str, str]:
    """Without the hosted service: serve the page here and open this machine's browser."""
    global _settings_server
    import webbrowser

    from sleeptalk.settings_page import SettingsPage

    loop = asyncio.get_running_loop()
    ready: asyncio.Future[str] = loop.create_future()
    page = SettingsPage(path, profiles_dir=path.parent)
    _settings_server = asyncio.create_task(
        page.serve(open_browser=False, ready=lambda url: ready.done() or ready.set_result(url)),
        name="host-settings-page",
    )
    try:
        url = await asyncio.wait_for(ready, 10)
    except (asyncio.TimeoutError, Exception):  # noqa: BLE001 — report, never break the call
        _settings_server.cancel()
        return "Settings couldn't start. Run `sleeptalk settings` on the computer running your agent.", ""
    opened = await asyncio.to_thread(webbrowser.open, url)
    if not opened:
        _settings_server.cancel()
        return "No browser on the computer running your agent. Run `sleeptalk settings` there.", ""
    return "Settings opened in the browser on the computer running your agent. Changes apply to your next call.", ""


def _call_ended(exc: BaseException) -> bool:
    """A push that failed only because the call is over: nothing to report."""
    from sleeptalk.call_provider import CallEnded

    return isinstance(exc, CallEnded)
