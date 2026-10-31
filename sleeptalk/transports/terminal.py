"""Terminal transport lifecycle and cleanup (r2-terminal-lifecycle).

Connects the audio device I/O (:mod:`sleeptalk.transports.terminal_audio`)
to a transport lifecycle: devices are opened on run, released on normal
exit, Ctrl-C (SIGINT/SIGTERM), and exceptions — and cleanup is idempotent
so a second ``close()`` (or a close after a failed open) is always safe.
After close, the transport can be started again in the same process
(devices re-open cleanly; PortAudio handles the re-open).

Permission errors get concrete recovery steps (macOS microphone permission
is the common case — the terminal needs Input Monitoring/Microphone
approval in System Settings).

Start a conversation from a profile (documented command)::

    sleeptalk run --config <profile>            # [transport] type = "terminal"

Add ``[transport] type = "terminal"`` to any sleeptalk.toml profile; the
canonical ``RealtimeSession`` runs with local microphone/speaker I/O. Ctrl-C
ends the session and releases the devices.

Interactive TTYs also show the animated setup-wizard voice being, driven by
normalized session events and capture activity. Set ``SLEEPTALK_NO_ASCII=1``
to keep the log-only presentation; non-TTY runs disable it automatically.
Raw terminal microphones default to speaker-safe capture: local TTS echo
cannot start a recursive turn, but a loud sustained interrupt still cancels
playback (the Discord/Web "say stop" path). Headphone/AEC users can opt in
to full barge-in capture with ``SLEEPTALK_TERMINAL_FULL_DUPLEX=1``.

Supported platforms: macOS/Windows/Linux with PortAudio (via the
sounddevice package); macOS requires Microphone (and Input Monitoring for
TTYs) permission — the transport prints the exact System Settings step
when denied.

The transport is transport-registry shaped (``__init__(config, env)`` +
``async run(session)``) like NullTransport/DiscordTransport. It adapts device
I/O to the public ``RealtimeSession`` methods only: captured microphone PCM
enters ``run_turn`` and synthesized PCM reaches the speaker through a
late-bound AudioOutput seam. A second utterance during an active response
calls the session's cancellation boundary, flushes queued audio, and begins a
clean turn. Explicit
input/output device selection comes from ``[transport]`` (``input_device`` /
``output_device``); an invalid selection raises an actionable error at
startup rather than silently falling back. Testable without audio hardware
via the injectable sounddevice seam from r2-terminal-device-io.
"""

from __future__ import annotations

import asyncio
import audioop
import contextlib
import logging
import math
import os
import select
import signal
import sys
import threading
import time
from array import array
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from sleeptalk.media_audio import AudioDecodeError, decode_audio_bytes
from sleeptalk.barge import BargeCalibration, BargeEvidence, BargeGate
from sleeptalk.interrupt import AcousticKind, InterruptPolicy
from sleeptalk.input_transcripts import input_endpointing_config, input_partial, input_source
from sleeptalk.pause_ahead import StreamAhead, configured_pause_ms, start_from_pause, warm_on_speech
from sleeptalk.policy import (
    narration_style_or_default,
    reply_length_or_default,
)
from sleeptalk.presentation import Line, PresentationState, apply_playback_ms, reduce
from sleeptalk.session_events import SessionEvent, SessionEventType, Speaker, transcript
from sleeptalk.speech_speed import snap_speed
from sleeptalk.transports.terminal_aec import AecStatus, TerminalAec
from sleeptalk.transports.terminal_audio import (
    AudioDeviceError,
    TerminalMicrophone,
    TerminalSpeaker,
    audio_recovery,
)
from sleeptalk.transports.terminal_controls import (
    ControlEffect,
    GlobalControl,
    SetNarrationStyle,
    SetReplyLength,
    SetSpeechSpeed,
    TerminalControls,
    TerminalKey,
)
from sleeptalk.transports.terminal_visual import TerminalBeing, agent_display_name, stack_from_config
from sleeptalk.voice_loop.smart_turn import SmartTurnScorer
from sleeptalk.voice_loop.telemetry import VoiceLatencyTrace, cascade_turn_identity
from sleeptalk.voice_loop.utterance import UtteranceAssembler

__all__ = ["TerminalTransport", "calibrate_capture_threshold"]

logger = logging.getLogger("sleeptalk.terminal_transport")

_ESC_CSI_TIMEOUT = 0.12


def _wait_stdin(fileno: int | None) -> bool:
    if fileno is None:
        return True
    ready, _, _ = select.select([fileno], [], [], _ESC_CSI_TIMEOUT)
    return bool(ready)


def _read_tty(fd: int, n: int) -> str:
    chunks: list[str] = []
    for _ in range(max(n, 1)):
        chunk = os.read(fd, 1)
        if not chunk:
            break
        chunks.append(chunk.decode("latin-1"))
    return "".join(chunks)


def _read_terminal_key(
    read: Callable[[int], str], *, fileno: int | None = None
) -> TerminalKey | None:
    char = read(1)
    if char in {"q", "Q", "\x03"}:
        return TerminalKey.QUIT
    if char in {"s", "S"}:
        return TerminalKey.SETTINGS
    if char == " ":
        return TerminalKey.STOP
    if char in {"m", "M"}:
        return TerminalKey.MUTE
    if char in {"\r", "\n"}:
        return TerminalKey.ENTER
    if char != "\x1b":
        return None
    # Lone Escape must not block the key thread waiting for CSI.
    if not _wait_stdin(fileno):
        return TerminalKey.ESCAPE
    intro = read(1)
    if not intro:
        return TerminalKey.ESCAPE
    if intro not in {"[", "O"}:
        return None
    csi = intro
    while len(csi) < 16:
        if not _wait_stdin(fileno):
            return None
        piece = read(1)
        if not piece:
            return None
        csi += piece
        if piece.isalpha() or piece == "~":
            break
    if csi in {"[A", "OA"}:
        return TerminalKey.UP
    if csi in {"[B", "OB"}:
        return TerminalKey.DOWN
    return None


_SAMPLE_RATE = 48_000
_SAMPLE_WIDTH = 2
_THINK_CUT_IN_MS = 250
# 650 ms cut the last word. 1800 ms (uncertain) made STT and smart-ack drag.
# 1000 ms is the Deepgram hang that already fixed mid-thought cuts.
TERMINAL_COMMIT_SILENCE_MS = 1000
# Keep this much trailing hush on the wire so the last phoneme reaches STT.
TERMINAL_STREAM_TAIL_MS = 200
_TERMINAL_LISTEN_RMS = 120
# Match web_voice.session._WEB_BARGE_INPUT_GAIN and
# discord_native.turns._DISCORD_BARGE_VOLUME_GAIN. Spoken "stop" lands at
# 0.016-0.021, under min_volume 0.03. Scale the volume check, not the PCM.
_TERMINAL_BARGE_VOLUME_GAIN = 2.5
# 40 ms of mono s16 @ 48 kHz — the analysis window web hands Silero
# (web_voice.session buffers CORE_FRAME_BYTES * 2 of stereo for the same
# span). Smaller windows starve the model; larger ones cost barge latency.
_WARMING_LINE = "warming up · not listening yet"
#: A denied macOS mic delivers exact zeros; real rooms never do.
_MIC_SILENT_SECONDS = 3.0
_MIC_SILENT_LINE = (
    "the microphone is sending only silence · on macOS, allow your terminal app in "
    "System Settings → Privacy & Security → Microphone, then restart it"
)
_BARGE_ANALYSIS_BYTES = 3_840
_CAPTURE_FRAME_BYTES = 4_800
_TERMINAL_NOISE_MARGIN_FLOOR = 40


def calibrate_capture_threshold(base: int, warmup_pcm: bytes) -> int:
    """Raise the RMS gate above warmup room tone so a pause can commit."""
    frames: list[int] = []
    for offset in range(0, len(warmup_pcm) - _CAPTURE_FRAME_BYTES + 1, _CAPTURE_FRAME_BYTES):
        frames.append(_pcm_rms(warmup_pcm[offset : offset + _CAPTURE_FRAME_BYTES]))
    if not frames and warmup_pcm:
        frames.append(_pcm_rms(warmup_pcm))
    if not frames:
        return max(int(base), 1)
    frames.sort()
    p90 = frames[min(len(frames) - 1, int(len(frames) * 0.9))]
    margin = max(_TERMINAL_NOISE_MARGIN_FLOOR, p90 // 4)
    return max(int(base), p90 + margin)


def _policy_ms(values: Any, name: str, default: int) -> int:
    raw = getattr(values, name, default)
    if raw is None:
        return default
    return max(int(raw), 0)


def _policy_volume(values: Any, name: str, default: float) -> float:
    raw = getattr(values, name, default)
    if raw is None:
        return default
    return max(float(raw), 0.0)


def _pcm_duration_ms(pcm: bytes) -> int:
    return int(len(pcm) * 1000 / (_SAMPLE_RATE * _SAMPLE_WIDTH))


def _pcm_rms(pcm: bytes) -> int:
    """Return the energy of canonical mono PCM without treating bytes as turns."""
    usable = len(pcm) - (len(pcm) % _SAMPLE_WIDTH)
    if usable <= 0:
        return 0
    samples = memoryview(pcm[:usable]).cast("h")
    return int(math.sqrt(sum(sample * sample for sample in samples) / len(samples)))


def _wav_mono_pcm(path: Path) -> bytes:
    """Decode a file clip once before sending it through canonical playout."""
    try:
        return decode_audio_bytes(path.read_bytes())
    except (OSError, AudioDecodeError):
        return b""


def _mono_to_stereo_s16(pcm: bytes) -> bytes:
    """Duplicate mono s16le into the assembler's 48 kHz stereo contract."""
    samples = array("h")
    samples.frombytes(pcm[: len(pcm) - len(pcm) % _SAMPLE_WIDTH])
    stereo = array("h")
    for sample in samples:
        stereo.extend((sample, sample))
    return stereo.tobytes()


def _stereo_to_mono_s16(pcm: bytes) -> bytes:
    """Average assembler stereo s16le back to the session's mono contract."""
    samples = array("h")
    samples.frombytes(pcm[: len(pcm) - len(pcm) % 4])
    mono = array("h")
    mono.extend(
        (int(samples[index]) + int(samples[index + 1])) // 2
        for index in range(0, len(samples), 2)
    )
    return mono.tobytes()


def _stereo_bytes_for_ms(duration_ms: int) -> int:
    return int(_SAMPLE_RATE * 2 * _SAMPLE_WIDTH * max(duration_ms, 0) / 1000)


class _HeldSmartTurn:
    """Ignore Smart Turn on blips the terminal mic can emit between reads."""

    def __init__(self, scorer: SmartTurnScorer, *, min_bytes: int) -> None:
        self._scorer = scorer
        self.threshold = scorer.threshold
        self._min_bytes = min_bytes

    async def score(self, pcm: bytes, **kwargs: Any) -> float:
        if len(pcm) < self._min_bytes:
            return 0.0
        return await self._scorer.score(pcm, **kwargs)


def _commit_silence_ms(policy: Any) -> int:
    """Terminal RMS commit wait. Discord complete/uncertain buckets stay put."""
    capture = getattr(policy, "capture", None)
    raw = getattr(capture, "terminal_commit_silence_ms", None)
    if raw is not None:
        return max(int(raw), 50)
    return max(TERMINAL_COMMIT_SILENCE_MS, 50)


def _stream_chunk(capture: _CaptureState, pcm: bytes, *, energy: bytes | None = None) -> bool:
    """Send speech plus a short hush pad. The rest of the commit hang stays local."""
    probe = energy if energy is not None else pcm
    if _pcm_rms(probe) >= capture.threshold:
        return True
    return capture.quiet_ms <= TERMINAL_STREAM_TAIL_MS


def _trim_trailing_hush(pcm: bytes, *, threshold: int, keep_ms: int) -> bytes:
    """Keep speech and ``keep_ms`` of tail. Drop the rest of the commit hang."""
    if not pcm:
        return pcm
    frame = max(int(_SAMPLE_RATE * _SAMPLE_WIDTH * 10 / 1000), _SAMPLE_WIDTH)
    frame -= frame % _SAMPLE_WIDTH
    keep_bytes = int(_SAMPLE_RATE * _SAMPLE_WIDTH * max(keep_ms, 0) / 1000)
    keep_bytes -= keep_bytes % _SAMPLE_WIDTH
    end = len(pcm) - (len(pcm) % _SAMPLE_WIDTH)
    last_speech = 0
    for offset in range(0, end, frame):
        if _pcm_rms(pcm[offset : offset + frame]) >= threshold:
            last_speech = offset + min(frame, end - offset)
    trimmed = pcm[: min(end, last_speech + keep_bytes)]
    return trimmed or pcm




@dataclass
class _CaptureState:
    """Device-side utterance framing; semantic turn policy stays in the kernel."""

    threshold: int
    start_ms: int
    silence_ms: int
    interrupt_ms: int
    bot_grace_ms: int
    hush_threshold: int = 0
    candidate: bytearray = field(default_factory=bytearray)
    speech_streak_ms: int = 0
    voiced_ms: int = 0
    quiet_ms: int = 0
    open: bool = False
    streaming: bool = False
    began_before_play: bool = False

    def observe(
        self,
        pcm: bytes,
        *,
        energy: bytes | None = None,
        playing: bool = False,
    ) -> tuple[bool, bool]:
        """Return ``(opened_now, endpoint_now)`` for one captured chunk.

        ``energy`` is the barge detector input (echo-cancelled residual
        during playback). The candidate still stores the original ``pcm``.
        """
        duration_ms = max(_pcm_duration_ms(pcm), 1)
        speech = _pcm_rms(energy if energy is not None else pcm) >= self.threshold
        if not self.open:
            if not speech:
                self.candidate.clear()
                self.speech_streak_ms = 0
                self.voiced_ms = 0
                self.began_before_play = False
                return False, False
            if not self.candidate:
                self.began_before_play = not playing
            self.candidate.extend(pcm)
            self.speech_streak_ms += duration_ms
            self.voiced_ms += duration_ms
            if self.speech_streak_ms < self.start_ms:
                return False, False
            self.open = True
            return True, False

        self.candidate.extend(pcm)
        if speech:
            self.voiced_ms += duration_ms
            self.speech_streak_ms += duration_ms
            self.quiet_ms = 0
        else:
            self.speech_streak_ms = 0
            self.quiet_ms += duration_ms
        return False, self.quiet_ms >= self.silence_ms

    def take(self) -> bytes:
        pcm = bytes(self.candidate)
        self.reset()
        return pcm

    def accept_interrupt(self) -> None:
        if self.candidate:
            self.open = True

    def leftover_before_first_audio(self, *, playing: bool) -> bool:
        return self.began_before_play and not playing

    def reset(self) -> None:
        self.candidate.clear()
        self.speech_streak_ms = 0
        self.voiced_ms = 0
        self.quiet_ms = 0
        self.open = False
        self.streaming = False
        self.began_before_play = False


class _SpeakerOutput:
    """AudioOutput boundary wrapping :class:`TerminalSpeaker` (the session
    late-binding seam). The kernel delivers synthesized PCM here; this drops
    stale-turn audio (barge-in safety), delegates playback to the speaker,
    and accounts **callback-consumed** bytes (what the device callback
    pulled, via :attr:`TerminalSpeaker.bytes_played`) rather than merely
    queued bytes.

    No provider is constructed or imported here — the kernel owns assembly;
    this is pure device-I/O bookkeeping. Implements the AudioOutput protocol
    by structural typing (``write``/``accounting``/``close``)."""

    def __init__(
        self,
        speaker: TerminalSpeaker,
        *,
        on_audio: Any = None,
        on_drop: Any = None,
        on_finish: Any = None,
    ) -> None:
        self._speaker = speaker
        self._on_audio = on_audio
        self._on_drop = on_drop
        self._on_finish = on_finish
        self._epoch = -1  # highest turn that has delivered audio
        self._bytes_generated = 0  # bytes handed to the speaker (queued)
        self._bytes_dropped_stale = 0

    async def write(self, pcm: bytes, turn_id: int) -> None:
        if turn_id < self._epoch or not pcm:
            self._bytes_dropped_stale += len(pcm)
            return
        self._epoch = turn_id
        self._speaker.write(pcm)
        self._bytes_generated += len(pcm)
        if callable(self._on_audio):
            self._on_audio(turn_id, len(pcm), pcm)

    async def play_file(self, path: Path) -> object:
        """Play a narration clip through the normal audio path.

        Routed through write() rather than straight to the device so the clip
        is visible to the echo canceller and the barge-in gate. Handed to the
        speaker unmixed, the agent's own typing would come back through an
        open mic and read as the user interrupting.
        """
        pcm = _wav_mono_pcm(path)
        if not pcm:
            return None
        await self.write(pcm, max(self._epoch, 0))
        return None

    async def finish_turn(self, turn_id: int) -> None:
        """The route has no more audio for this presentation.

        Not "playback is over" — the device queue still holds whatever was
        handed over. This is the fact the transport cannot infer: an empty
        queue mid-stream looks exactly like the end of a sentence, and
        guessing wrong is what reopened the microphone onto the assistant's
        own voice. A route that never calls this is never treated any
        differently than before.
        """
        if turn_id < self._epoch:
            return
        if callable(self._on_finish):
            self._on_finish(turn_id)

    async def discard_turn(self, turn_id: int, reason: str = "interrupted") -> None:
        del reason
        self._epoch = max(self._epoch, turn_id + 1)
        self.flush()

    def accounting(self):
        from sleeptalk.routes.audio_output import AudioOutputAccounting

        return AudioOutputAccounting(
            turn_id=self._epoch,
            bytes_generated=self._bytes_generated,
            bytes_written=self._speaker.bytes_played,  # callback-consumed
            bytes_dropped_stale=self._bytes_dropped_stale,
        )

    def flush(self) -> None:
        """Drop queued audio (barge-in): the route stops the cancelled turn,
        the transport flushes whatever it already queued."""
        self._speaker.flush()

    @property
    def flushed_chunks(self) -> int:
        """Chunks dropped by :meth:`flush` (barge-in proof)."""
        return self._speaker.flushed_chunks

    async def close(self) -> None:
        self._speaker.close()


class TerminalTransport:
    """Registry-shaped transport for the terminal surface."""

    def __init__(self, config, env: dict[str, str]) -> None:
        self.config = config
        self.env = env
        self.events: list[dict[str, Any]] = []
        self._closed = False
        self._microphone: TerminalMicrophone | None = None
        self._speaker: TerminalSpeaker | None = None
        self._audio_output: _SpeakerOutput | None = None
        self._sounddevice: Any = None  # injectable; None = real package
        self._input_device: Any = None
        self._output_device: Any = None
        # Explicit device selection from [transport] (name or index; "" = default).
        transport_cfg = getattr(config, "transport", None)
        in_dev = getattr(transport_cfg, "input_device", "")
        out_dev = getattr(transport_cfg, "output_device", "")
        self._input_device = in_dev if in_dev != "" else None
        self._output_device = out_dev if out_dev != "" else None
        self._previous_handlers: dict[int, Any] = {}
        self._shutdown = asyncio.Event()
        self._run_task: asyncio.Task[Any] | None = None
        self._being: TerminalBeing | Any | None = None
        self._being_factory: Any = TerminalBeing
        self._unsubscribe_events: Any = None
        self._playback_watch_task: asyncio.Task[Any] | None = None
        self._terminal_full_duplex = self._resolve_full_duplex()
        self._echo_gate_active = False
        self._awaiting_hush_after_bot = False
        #: A route handed over audio and has not said it is finished.
        #: The shared session finishes synchronous turns; detached routes
        #: signal the completion of each later presentation themselves.
        self._presentation_open = False
        self._capture_resume_at = 0.0
        self._echo_grace_ms = 400
        self._echo_canceller = TerminalAec()
        self._aec_silent_reported = False
        self._mic_silence_reported = False
        self._warming = False
        self._warming_at = 0.0
        self._gate: BargeGate | None = None
        self._barge_vad: Any = None
        self._barge_vad_failed = False
        self._barge_pcm = bytearray()
        self._floor_origin = 0.0
        self._floor_turn = False
        self._floor_playing = False
        self._presentation = PresentationState()
        self._controls = TerminalControls()
        self._muted = False
        self._cut_in = threading.Event()
        self._keys_stop: threading.Event | None = None
        self._keys_thread: threading.Thread | None = None
        self._session: Any = None
        self._output: _SpeakerOutput | None = None
        self.assembler: UtteranceAssembler | None = None
        self._capture: _CaptureState | None = None
        self._turn_task: asyncio.Task[Any] | None = None
        self._utterance = 0
        self._listen_threshold = _TERMINAL_LISTEN_RMS
        self._hush_threshold = _TERMINAL_LISTEN_RMS
        self._listen_vad_factory: Any = None
        self._assembler_collect = False
        self._pending_early_end: dict | None = None
        self._stream_ahead: StreamAhead | None = None
        self._vad_start_ms = 250
        self._latency: VoiceLatencyTrace | None = None
        self._latency_stages: set[str] = set()
        self._pending_endpoint_timings: deque[dict[str, int]] = deque()

    # -- configuration -----------------------------------------------------

    def _resolve_full_duplex(self) -> bool:
        """Headphones or speakers, from the profile with an env override.

        Answered once in `sleeptalk init` and stored as
        ``[transport] capture_mode``; SLEEPTALK_TERMINAL_FULL_DUPLEX still wins
        so a one-off run can flip it without editing the profile. Anything
        unrecognised means speakers, because guessing headphones wrongly makes
        the agent interrupt itself on every sentence.
        """
        raw = str(self.env.get("SLEEPTALK_TERMINAL_FULL_DUPLEX", "")).strip().lower()
        if raw:
            return raw in {"1", "true", "yes", "on"}
        transport = getattr(self.config, "transport", None)
        mode = str(getattr(transport, "capture_mode", "") or "").strip().lower()
        return mode in {"headphones", "headset", "full_duplex"}

    async def _prompt_capture_mode(self) -> None:
        """Ask headphones-or-speakers at boot, when nothing has answered yet.

        Headphones come and go between runs, so the question belongs at the
        moment the answer is actually knowable rather than only at setup. A
        profile answer or an explicit env override skips it, and a non-TTY
        never blocks. Enter means speakers: answering "headphones" wrongly
        makes the agent interrupt itself on every sentence, so the blind
        default has to be the safe one.
        """
        if str(self.env.get("SLEEPTALK_TERMINAL_FULL_DUPLEX", "")).strip():
            return
        transport = getattr(self.config, "transport", None)
        if str(getattr(transport, "capture_mode", "") or "").strip():
            return
        if not sys.stdin.isatty():
            return
        try:
            answer = await asyncio.to_thread(
                input, "Are you on headphones or speakers? [h = headphones, Enter = speakers] "
            )
        except (EOFError, OSError, RuntimeError):
            return
        self._terminal_full_duplex = answer.strip().lower() in {
            "h",
            "headphones",
            "headphone",
            "headset",
        }
        self._record(
            "capture_mode_prompted",
            capture_mode="full_duplex" if self._terminal_full_duplex else "speaker_safe",
        )

    def use_sounddevice(self, sounddevice: Any) -> None:
        """Inject a fake sounddevice module (tests)."""
        self._sounddevice = sounddevice

    def use_terminal_being(self, factory: Any) -> None:
        """Inject the terminal presentation factory (tests/embedders)."""
        self._being_factory = factory

    def use_echo_canceller(self, canceller: TerminalAec) -> None:
        """Inject the barge residual engine (tests)."""
        self._echo_canceller = canceller

    def use_listen_vad(self, factory: Any) -> None:
        """Inject the listen Silero factory (tests)."""
        self._listen_vad_factory = factory

    def request_cut_in(self) -> None:
        """Stop the current turn without ending the session."""
        self._cut_in.set()

    @property
    def microphone(self) -> TerminalMicrophone | None:
        """The open capture device (None before run / after close)."""
        return self._microphone

    @property
    def audio_output(self) -> _SpeakerOutput | None:
        """The session-bound playback boundary (None until run starts)."""
        return self._audio_output

    def _record(self, event: str, **fields: Any) -> None:
        self.events.append({"event": event, **fields})
        logger.info("terminal-transport %s: %s", event, fields)

    # -- latency traces ------------------------------------------------------

    def _latency_enabled(self) -> bool:
        return bool(getattr(getattr(self.config, "log", None), "latency", True))

    def _slug(self) -> str:
        return str(getattr(getattr(self.config, "agent", None), "slug", "") or "sleeptalk")

    def _start_latency_turn(self, **fields: Any) -> None:
        """Open a trace at the endpoint, the one instant every surface shares.

        Terminal used to keep its timings in the in-memory transport event
        stream, which made it the one surface that could not be compared
        with web or Discord. Same envelope, same stage names, same file.
        """
        if not self._latency_enabled():
            return
        slug = self._slug()
        identity = cascade_turn_identity(
            self.config,
            session_id=str(getattr(self._session, "session_id", "") or ""),
        )
        self._latency = VoiceLatencyTrace(
            surface=f"{slug}-terminal",
            transport="terminal",
            persona=slug,
            metadata={**identity, "utterance": self._utterance},
        )
        self._latency_stages = set()
        self._mark_latency("utterance_ready", **fields)

    def _mark_latency(self, stage: str, *, once: bool = False, **fields: Any) -> None:
        trace = self._latency
        if trace is None or (once and stage in self._latency_stages):
            return
        self._latency_stages.add(stage)
        try:
            trace.mark(stage, **fields)
        except Exception:  # noqa: BLE001 — telemetry never breaks a turn
            logger.debug("latency trace failed at %s", stage, exc_info=True)

    def _finish_latency(self, stage: str = "turn_complete", **fields: Any) -> None:
        trace = self._latency
        if trace is None:
            return
        self._latency = None
        self._latency_stages = set()
        try:
            trace.finish(stage, **fields)
        except Exception:  # noqa: BLE001 — telemetry never breaks a turn
            logger.debug("latency trace failed at %s", stage, exc_info=True)

    def _mark_latency_event(self, event: SessionEvent) -> None:
        """Translate the normalized session events into trace stages."""
        kind = event.type
        if kind is SessionEventType.TRANSCRIPT_FINAL:
            text = str(event.payload.get("text") or "")
            self._mark_latency("stt_completed", once=True, textChars=len(text))
        elif kind is SessionEventType.RESPONSE_STARTED:
            self._mark_latency("agent_started", once=True)
        elif kind is SessionEventType.RESPONSE_COMPLETED:
            self._finish_latency("turn_complete")
        elif kind is SessionEventType.RESPONSE_CANCELLED:
            self._finish_latency("turn_cancelled")

    # -- transport lifecycle -------------------------------------------------

    async def run(self, session: Any) -> None:
        """Open devices, drive the session, guarantee release.

        Restartable through the public API: ``run()`` may be called again
        after ``close()`` in the same process — it resets the lifecycle
        state itself (audit rework: no caller ever touches private fields).
        """
        self._closed = False
        self._shutdown = asyncio.Event()  # fresh per run: restart is clean
        self._echo_gate_active = False
        self._awaiting_hush_after_bot = False
        self._presentation_open = False
        self._capture_resume_at = 0.0
        self._aec_silent_reported = False
        self._mic_silence_reported = False
        self._cut_in.clear()
        self._echo_canceller.reset()
        self._gate = None
        self._barge_vad = None
        self._barge_vad_failed = False
        self._barge_pcm.clear()
        self._floor_origin = 0.0
        self._floor_turn = False
        self._floor_playing = False
        self._session = None
        self._output = None
        if self._stream_ahead is not None:
            self._stream_ahead.close()
        self._stream_ahead = None
        self.assembler = None
        self._capture = None
        self._turn_task = None
        self._utterance = 0
        self._assembler_collect = False
        self._controls = TerminalControls()
        self._session = session
        self._sync_setting_values(session)
        await self._prompt_capture_mode()
        self._start_visual(session)
        try:
            self._microphone = TerminalMicrophone(
                device=self._input_device, sounddevice=self._sounddevice
            )
            self._speaker = TerminalSpeaker(
                device=self._output_device, sounddevice=self._sounddevice
            )
        except AudioDeviceError as exc:
            self._record(
                "devices_unavailable", error=str(exc), recovery=self._recovery_for(exc)
            )
            self._set_being("idle", detail="ERROR · audio unavailable")
            await self.close()
            raise
        self._install_signal_handlers()
        self._run_task = asyncio.current_task()
        self._record(
            "session_started",
            input_device=self._microphone.device_index,
            output_device=self._speaker.device_index,
            capture_mode=(
                "full_duplex" if self._terminal_full_duplex else "speaker_safe"
            ),
        )
        # The mic is not read until prepare() finishes and the capture loop
        # starts, so anything said before then is dropped. Say so rather than
        # showing "listening" over a window that is not listening.
        self._show_warming()
        try:
            await self._drive(session)
        except asyncio.CancelledError:
            self._record("session_cancelled")
            raise
        except KeyboardInterrupt:
            self._record("session_interrupted")
        except BaseException as exc:
            self._record("session_error", error=f"{type(exc).__name__}: {exc}")
            self._set_being("idle", detail="ERROR · see logs")
            raise
        finally:
            await self.close()

    async def _drive(self, session: Any) -> None:
        """Adapt microphone and speaker devices to ``RealtimeSession``.

        Terminal deliberately knows nothing about routes, agents, STT, TTS,
        turn contexts, or cancellation epochs. Those all stay behind the
        canonical session boundary.
        """
        speaker = self._speaker
        output: _SpeakerOutput | None = None
        if speaker is not None:
            output = _SpeakerOutput(
                speaker,
                on_audio=self._on_audio_queued,
                on_drop=self._on_audio_dropped,
                on_finish=self._on_audio_finished,
            )
            self._audio_output = output
            session.set_audio_output(output)
        subscribe = getattr(session, "subscribe", None)
        if callable(subscribe):
            self._unsubscribe_events = subscribe(self._on_session_event)
        loop = asyncio.get_running_loop()
        self._floor_origin = loop.time()
        # Speakers put the bot's own voice into the mic, so the floor must run
        # on energy: Silero would hear the bot as speech and interrupt every
        # sentence. SLEEPTALK_TERMINAL_FULL_DUPLEX is the user asserting there
        # is no acoustic loop (headphones/AEC), which is exactly the condition
        # web and Discord get for free — so it earns their detector too.
        self._gate = BargeGate(
            self._interrupt_policy(session),
            AcousticKind.isolated if self._terminal_full_duplex else AcousticKind.mixed,
            calibration=BargeCalibration(
                # A spoken "stop" over a laptop speaker is quiet by the time
                # the residual reaches us; scale the check, not the audio.
                level_scale=_TERMINAL_BARGE_VOLUME_GAIN,
                # Terminal has always reset on the first quiet frame. It is
                # tuned that way and it works; leave it alone.
                dip_tolerance_ms=0.0,
                # _SpeakerOutput can flush but not pause, so soft cannot mean
                # "pause and reconsider" here. The gate downgrades it to stop
                # — which is what terminal already did — and records it.
                supports_resume=False,
                supports_duck=False,
            ),
            detector="silero" if self._terminal_full_duplex else "residual_rms",
            clock=lambda: self._floor_now(),
            # Terminal has no latency log; its records land in the transport
            # event stream, carrying the same bargeId as the other surfaces.
            emit=lambda stage, fields: self._record(stage, **fields),
        )
        await session.prepare()

        capture = self._capture_state(session)
        self._echo_grace_ms = capture.bot_grace_ms
        self._listen_threshold = capture.threshold
        self._hush_threshold = capture.hush_threshold or capture.threshold
        self._vad_start_ms = capture.start_ms
        self._session = session
        self._output = output
        self._capture = capture
        self._publish(
            tts_speed=self._tts_speed_now(),
            narration_style=narration_style_or_default(
                getattr(getattr(session, "policy", None), "narration_style", "")
            ),
            reply_length=reply_length_or_default(
                getattr(getattr(session, "policy", None), "reply_length", "")
            ),
        )
        self.assembler = self._build_assembler(session, capture)
        self._stream_ahead = StreamAhead(session, self.assembler, 0, _stereo_to_mono_s16)
        self._clear_warming()
        try:
            while not self._closed and not self._shutdown.is_set():
                try:
                    await asyncio.wait_for(self._shutdown.wait(), timeout=0.05)
                    break
                except asyncio.TimeoutError:
                    pass
                if self._turn_task is not None and self._turn_task.done():
                    await self._settle_turn(self._turn_task)
                    self._turn_task = None
                if self._cut_in.is_set():
                    self._cut_in.clear()
                    active = self._turn_task is not None and not self._turn_task.done()
                    if active or self._playback_pending():
                        await self._apply_interrupt(
                            session,
                            output,
                            self._turn_task,
                            self._utterance,
                            detector="hotkey",
                            reason="hotkey",
                        )
                        self._turn_task = None
                        continue
                pcm = self._microphone.read() if self._microphone else b""
                if not self._mic_silence_reported and self._microphone is not None:
                    self._check_mic_silence()
                if self._muted:
                    pcm = b""
                if not pcm:
                    if (
                        self.assembler is not None
                        and 0 in self.assembler.buffers
                        and capture.quiet_ms == 0
                    ):
                        self.assembler.last_audio_at[0] = loop.time()
                    continue
                now = loop.time()
                if not self._terminal_full_duplex and self._echo_gate_active:
                    if self._playback_pending():
                        # Keep the decay window anchored to audible playback,
                        # but still observe the mic. Discord/Web listen during
                        # TTS so a spoken "stop" can cancel. Draining every
                        # frame made Terminal barge-in impossible.
                        self._capture_resume_at = max(
                            self._capture_resume_at,
                            now + self._echo_grace_ms / 1000,
                        )
                    elif now >= self._capture_resume_at:
                        self._end_echo_gate(capture)
                speaker_busy = self._speaker_busy(now)
                # Laptop AEC with no engine returns silent residual after any
                # far-end push. That must mute barge during playback, not hide
                # the first real sentence once the speaker has drained.
                mute_echo = (
                    not self._terminal_full_duplex
                    and self._echo_gate_active
                    and speaker_busy
                )
                if self._hush_check_due(now):
                    if _pcm_rms(pcm) < self._hush_threshold:
                        self._release_hush(capture)
                    else:
                        self._sync_playback_floor()
                        continue
                detector = pcm
                if mute_echo:
                    frame = self._echo_canceller.process(pcm)
                    detector = frame.residual
                    if frame.status is AecStatus.SILENT and not self._aec_silent_reported:
                        self._record("barge_unavailable", reason="aec_silent")
                        self._aec_silent_reported = True
                capture.observe(
                    pcm,
                    energy=detector,
                    playing=self._floor_playing,
                )
                if self.assembler is not None:
                    self.assembler.observe(0, self, _mono_to_stereo_s16(pcm))
                    if self._stream_ahead is not None:
                        self._stream_ahead.sync()
                self._sync_playback_floor()
                evidence = (
                    await self._observe_silero(detector, pcm)
                    if self._barge_uses_silero()
                    else self._observe_residual(detector, pcm)
                )
                if evidence is not None and capture.leftover_before_first_audio(
                    playing=self._floor_playing
                ):
                    # The last word of the previous utterance, not a new
                    # barge: withhold it rather than let it take the floor.
                    evidence = replace(evidence, suppressed="leftover_before_first_audio")
                decision = (
                    self._gate.observe(evidence)
                    if evidence is not None and self._gate is not None
                    else None
                )
                if decision is not None and decision.fired:
                    await self._apply_interrupt(
                        session,
                        output,
                        self._turn_task,
                        self._utterance,
                        detector=self._gate.detector if self._gate else "residual_rms",
                        reason=decision.reason,
                    )
                    self._turn_task = None
                    self._assembler_collect = True
                    capture.accept_interrupt()
                    if self.assembler is not None and pcm:
                        self.assembler.inject_preroll(0, self, _mono_to_stereo_s16(pcm))

                if not capture.open:
                    continue

                active = self._turn_task is not None and not self._turn_task.done()

                if mute_echo:
                    continue

                append_audio = getattr(session, "append_audio", None)
                if not active and callable(append_audio):
                    stream_pcm = bytes(capture.candidate) if not capture.streaming else pcm
                    if stream_pcm:
                        await append_audio(stream_pcm, participant_id=0)
                        capture.streaming = True
                        self._sync_input_partial(session)
        finally:
            if self.assembler is not None:
                self.assembler.cleanup()
            if self._turn_task is not None and not self._turn_task.done():
                try:
                    await session.interrupt_presentation()
                except (asyncio.CancelledError, StopAsyncIteration):
                    pass
                if output is not None:
                    output.flush()
            if self._turn_task is not None:
                await self._settle_turn(self._turn_task)

    def _interrupt_policy(self, session: Any) -> InterruptPolicy:
        policy = getattr(session, "policy", None)
        if policy is None:
            policy = getattr(self.config, "policy", None)
        values = getattr(policy, "interrupt", None)
        if values is None:
            return InterruptPolicy()
        mode = getattr(values, "mode", "immediate") or "immediate"
        if mode not in ("off", "soft", "immediate"):
            mode = "immediate"
        return InterruptPolicy(
            enabled=bool(getattr(values, "enabled", True)),
            local_responsiveness=bool(getattr(policy, "local_responsiveness", False)),
            snappy_responsiveness=bool(getattr(policy, "snappy_responsiveness", False)),
            mode=mode,
            min_duration_ms=_policy_ms(values, "min_duration_ms", 150),
            grace_ms=_policy_ms(values, "grace_ms", 400),
            gap_hold_ms=_policy_ms(values, "gap_hold_ms", 600),
            cooldown_ms=_policy_ms(values, "cooldown_ms", 1200),
            min_volume=_policy_volume(values, "min_volume", 0.03),
        )

    def _sync_input_partial(self, session: Any) -> None:
        text = input_partial(session)
        if text == self._presentation.partial:
            return
        self._on_session_event(transcript(
            getattr(session, "session_id", ""), 0,
            speaker=Speaker.USER, text=text, final=False, source=input_source(session),
        ))
        if self.assembler is not None and input_source(session) == "moonshine_streaming":
            self.assembler.recheck_streaming_endpoint(0)

    def _floor_now(self) -> int:
        return int((asyncio.get_running_loop().time() - self._floor_origin) * 1000)

    def _mark_turn_start(self) -> None:
        if self._gate is None or self._floor_turn:
            return
        self._gate.turn_started()
        self._floor_turn = True

    def _mark_turn_end(self) -> None:
        if self._gate is None or not self._floor_turn:
            return
        self._gate.turn_ended()
        self._floor_turn = False
        self._floor_playing = False
        self._barge_pcm.clear()

    def _sync_playback_floor(self) -> None:
        if self._gate is None:
            return
        pending = self._playback_pending()
        if pending and not self._floor_playing:
            self._mark_turn_start()
            self._gate.audio_started()
            self._floor_playing = True
        elif not pending and self._floor_playing:
            self._gate.audio_drained()
            self._floor_playing = False

    def _barge_uses_silero(self) -> bool:
        """Speaker mode must keep the synchronous residual path.

        Awaiting on every captured frame reorders the capture loop against
        playback and turn tasks, so the opt-in detector must not add an await
        for users who never asked for it.
        """
        gate = self._gate
        return gate is not None and gate.detector == "silero" and not self._barge_vad_failed

    async def _observe_silero(self, detector: bytes, pcm: bytes) -> BargeEvidence | None:
        """Full-duplex barge-in: energy says how loud, Silero says whether it
        is speech. A microwave beep clears the volume bar the same way a
        spoken "stop" does; only the model separates them."""
        del pcm
        self._barge_pcm.extend(detector)
        if len(self._barge_pcm) < _BARGE_ANALYSIS_BYTES:
            return None
        analysis = bytes(self._barge_pcm)
        self._barge_pcm.clear()
        try:
            if self._barge_vad is None:
                self._barge_vad = self._new_barge_gate()
            if self._barge_vad is None:
                return self._observe_residual(analysis, analysis)
            observation = await self._barge_vad.observe(_mono_to_stereo_s16(analysis))
        except (ImportError, RuntimeError, ValueError, OSError, AttributeError) as exc:
            # Never leave the user without barge-in because a model failed to
            # load. Fall back to energy for the rest of the session and say so.
            self._barge_vad_failed = True
            self._barge_vad = None
            self._record(
                "barge_vad_fallback",
                error=f"{type(exc).__name__}: {str(exc)[:200]}",
            )
            return self._observe_residual(analysis, analysis)
        return BargeEvidence(
            duration_ms=max(_pcm_duration_ms(analysis), 1),
            level=_pcm_rms(analysis) / 32768.0,
            voiced=observation.is_speech,
            confidence=getattr(observation, "confidence", None),
            raw_rms=_pcm_rms(analysis),
        )

    def _new_barge_gate(self) -> Any:
        """Borrow the listen path's gate factory.

        Terminal code must not construct providers (the assembler owns that
        seam, and test_no_provider_constructed_in_terminal_code enforces it),
        so the barge detector reuses the factory and config the assembler
        already resolved rather than importing the model itself.
        """
        assembler = self.assembler
        if assembler is None:
            return None
        factory = self._listen_vad_factory or getattr(assembler, "silero_vad_factory", None)
        if not callable(factory):
            return None
        return factory(getattr(assembler, "silero_vad_config", None))

    def _observe_residual(self, detector: bytes, pcm: bytes) -> BargeEvidence:
        """Mixed audio: the AEC residual is all we can trust, so energy alone
        decides. voiced is left unset — the gate reads that as "loud enough
        counts as speech", which is exactly what residual RMS means."""
        rms = _pcm_rms(detector)
        return BargeEvidence(
            duration_ms=max(_pcm_duration_ms(pcm), 1),
            level=rms / 32768.0,
            voiced=None,
            raw_rms=rms,
        )

    def _capture_state(self, session: Any) -> _CaptureState:
        """Resolve existing profile capture values for terminal device framing."""
        policy = getattr(session, "policy", None)
        if policy is None:
            policy = getattr(self.config, "policy", None)
        capture = getattr(policy, "capture", None)
        turns = getattr(policy, "turns", None)
        threshold = int(getattr(capture, "rms_threshold", 0) or 0)
        if threshold <= 0:
            threshold = _TERMINAL_LISTEN_RMS
        configured_grace_ms = getattr(turns, "bot_grace_ms", 400)
        dumped = b""
        if self._microphone is not None:
            dumped = self._microphone.read() or b""
        hush_threshold = calibrate_capture_threshold(max(threshold, 1), dumped)
        return _CaptureState(
            threshold=threshold,
            start_ms=max(int(getattr(capture, "vad_start_ms", 250) or 250), 50),
            silence_ms=_commit_silence_ms(policy),
            interrupt_ms=max(int(getattr(turns, "interrupt_min_ms", 250) or 250), 50),
            bot_grace_ms=max(
                int(400 if configured_grace_ms is None else configured_grace_ms),
                0,
            ),
            hush_threshold=hush_threshold,
        )

    def _build_assembler(self, session: Any, capture: _CaptureState) -> UtteranceAssembler:
        policy = getattr(session, "policy", None)
        if policy is None:
            policy = getattr(self.config, "policy", None)
        values = getattr(policy, "capture", None)
        smart_turn = None
        if values is None or bool(getattr(values, "smart_turn", True)):
            smart_turn = _HeldSmartTurn(
                SmartTurnScorer(
                    threshold=float(getattr(values, "smart_turn_threshold", 0.6) or 0.6)
                ),
                min_bytes=_stereo_bytes_for_ms(300),
            )
        endpointing = None
        if policy is not None and callable(getattr(policy, "endpointing_config", None)):
            endpointing = policy.endpointing_config()
        endpointing = input_endpointing_config(session, endpointing)
        signal = getattr(session, "turn_completion_signal", None)

        def provider(participant_id: Any) -> Any:
            if not callable(signal):
                return None
            try:
                return signal(int(participant_id))
            except (TypeError, ValueError):
                return signal(0)

        return UtteranceAssembler(
            loop=asyncio.get_running_loop(),
            min_bytes=_stereo_bytes_for_ms(50),
            silence_seconds=max(
                int(getattr(values, "silence_ms", 120) or 120) / 1000 if values is not None else 0.12,
                0.08,
            ),
            on_utterance=self._on_assembled_utterance,
            should_ignore_audio=self._assembler_should_ignore,
            collect_during_ignore=lambda _pid: self._assembler_collect,
            on_speech_started=self._on_speech_started,
            speech_rms_threshold=0,
            vad_mode="silero",
            silero_vad_config=(
                policy.silero_config()
                if policy is not None and callable(getattr(policy, "silero_config", None))
                else None
            ),
            silero_vad_factory=self._listen_vad_factory,
            smart_turn_scorer=smart_turn,
            turn_completion_provider=provider if callable(signal) else None,
            endpointing_config=endpointing,
            on_capture_event=self._capture_endpoint_timing,
            on_pause=self._on_speech_pause,
            pause_ms=configured_pause_ms(session),
            # A batch listener's early transcript judges the turn, as on web.
            early_transcripts=lambda: bool(callable(getattr(session, "listens_ahead", None))
                                           and session.listens_ahead()),
        )

    def _on_speech_pause(self, participant_id: Any, stereo_pcm: bytes) -> None:
        session = self._session
        if session is None or self._closed:
            return
        task = start_from_pause(session, _stereo_to_mono_s16(stereo_pcm))
        if task is not None and self.assembler is not None:
            self.assembler.watch_early_transcript(participant_id, task)

    def _capture_endpoint_timing(self, participant_id: Any, event: dict) -> None:
        del participant_id
        if not self._closed and event.get("kind") == "early_end_shadow":
            # Log-only verdicts; written on the turn's trace once it starts.
            self._pending_early_end = {k: v for k, v in event.items() if k != "kind"}
            return
        if self._closed or event.get("kind") != "utterance_committed":
            return
        fields = {
            "endpointSilenceMs": event["endpointSilenceMs"]
        } if "endpointSilenceMs" in event else {}
        self._pending_endpoint_timings.append(fields)

    def _assembler_should_ignore(self) -> bool:
        if self._muted:
            return True
        if self._terminal_full_duplex:
            return False
        if self._echo_gate_active and self._playback_pending():
            return True
        return self._awaiting_hush_after_bot

    def _on_speech_started(self, participant: object) -> None:
        del participant
        if self._session is not None:
            warm_on_speech(self._session)
            heard = getattr(self._session, "note_caller_speech", None)
            if callable(heard):
                heard()  # a background job's announcement waits for the caller
        self._set_hearing(True)
        self._set_being(
            "listening",
            active=True,
            detail="hearing you · pause to send",
        )

    async def _on_assembled_utterance(self, participant: object, stereo_pcm: bytes) -> None:
        del participant
        endpoint_fields = (
            self._pending_endpoint_timings.popleft() if self._pending_endpoint_timings else {}
        )
        session = self._session
        if session is None or self._closed:
            return
        utterance_pcm = _trim_trailing_hush(
            _stereo_to_mono_s16(stereo_pcm),
            threshold=self._listen_threshold,
            keep_ms=TERMINAL_STREAM_TAIL_MS,
        )
        capture = self._capture
        leftover = False
        voiced_ms = 0
        start_ms = self._vad_start_ms
        if capture is not None:
            leftover = capture.leftover_before_first_audio(playing=self._floor_playing)
            voiced_ms = capture.voiced_ms
            start_ms = capture.start_ms
            capture.take()
            self._set_hearing(capture.open)
        active = self._turn_task is not None and not self._turn_task.done()
        think_cut = leftover and voiced_ms < max(start_ms, _THINK_CUT_IN_MS)
        if self._playback_pending() and not self._assembler_collect:
            return
        if active and (self._floor_playing or think_cut):
            return
        if active:
            await self._apply_interrupt(
                session,
                self._output,
                self._turn_task,
                self._utterance,
                detector="committed_utterance",
                reason="user_speech",
            )
            self._turn_task = None
        if not utterance_pcm:
            return
        self._set_hearing(False)
        self._presentation = replace(self._presentation, partial="")
        self._set_being("thinking", detail="transcribing your voice")
        self._utterance += 1
        self._assembler_collect = False
        self._start_latency_turn(
            audioMs=_pcm_duration_ms(utterance_pcm),
            voicedMs=voiced_ms,
            **endpoint_fields,
        )
        early_end, self._pending_early_end = self._pending_early_end, None
        if early_end:
            self._mark_latency("early_end_shadow", **early_end)
        self._turn_task = asyncio.create_task(
            session.run_turn(
                pcm=utterance_pcm,
                extras={"participant_id": 0, "latency_trace": self._latency},
            )
        )

    async def _apply_interrupt(
        self,
        session: Any,
        output: _SpeakerOutput | None,
        turn_task: asyncio.Task[Any] | None,
        utterance: int,
        *,
        detector: str,
        reason: str,
    ) -> None:
        self._record(
            "interruption",
            cancelled_utterance=utterance,
            detector=detector,
            reason=reason,
        )
        self._finish_latency("turn_cancelled", detector=detector, reason=reason)
        self._set_hearing(True)
        self._set_being("listening", active=True, detail="hearing you")
        await session.interrupt_presentation()
        if output is not None:
            output.flush()
        self._sync_playback_floor()
        if turn_task is not None:
            await self._settle_turn(turn_task)
        self._mark_turn_end()
        self._echo_gate_active = False
        self._awaiting_hush_after_bot = False
        self._presentation_open = False
        self._aec_silent_reported = False
        self._echo_canceller.reset()
        self._barge_pcm.clear()
        # Hotkey and committed-utterance interrupts never reach the floor, so
        # _mark_turn_end may not have cleared the streak. Whatever the
        # detector had built towards is over either way.
        if self._gate is not None:
            self._gate.reset_attempt()

    async def _settle_turn(self, turn_task: asyncio.Task[Any]) -> None:
        """Await a terminal turn and surface its exception without killing I/O."""
        try:
            await turn_task
        except asyncio.CancelledError:
            return
        except Exception as exc:  # one failed utterance does not kill the device loop
            self._record("turn_error", error=f"{type(exc).__name__}: {exc}")
            self._finish_latency("turn_error", error=type(exc).__name__)
            self._set_being("idle", detail="ERROR · turn failed")
            logger.warning("terminal turn failed: %s: %s", type(exc).__name__, exc)
        if not self._playback_pending():
            self._mark_turn_end()

    async def close(self) -> None:
        """Idempotent teardown — safe from any exit path, twice.

        The transport owns devices and signal handlers only. The enclosing
        ``RealtimeSession.run`` finally block owns route/provider/meter close.
        """
        if self._closed:
            return
        self._closed = True
        self._pending_endpoint_timings.clear()
        self._shutdown.set()
        unsubscribe = self._unsubscribe_events
        self._unsubscribe_events = None
        if callable(unsubscribe):
            unsubscribe()
        playback_watch = self._playback_watch_task
        self._playback_watch_task = None
        if playback_watch is not None and not playback_watch.done():
            playback_watch.cancel()
            await asyncio.gather(playback_watch, return_exceptions=True)
        self._restore_signal_handlers()
        for name, device in (
            ("microphone", self._microphone),
            ("speaker", self._speaker),
        ):
            if device is not None:
                try:
                    device.close()
                except Exception as exc:  # release never fails cleanup
                    self._record("device_release_error", device=name, error=str(exc))
        self._microphone = None
        self._speaker = None
        self._record("session_closed")
        being = self._being
        self._being = None
        self._stop_keys()
        if being is not None:
            being.close()

    # -- terminal presentation ---------------------------------------------

    def _start_visual(self, session: Any | None = None) -> None:
        """Start the old wizard being for an interactive production run."""
        disabled = str(self.env.get("SLEEPTALK_NO_ASCII", "")).strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        try:
            self._being = self._being_factory(enabled=False if disabled else None)
            agent = getattr(self.config, "agent", None)
            agent_name = (
                (agent_display_name(agent) if agent is not None else "")
                or getattr(agent, "type", "")
                or "voice agent"
            )
            self._being.set_identity(
                agent=str(agent_name),
                surface="terminal",
                stack=stack_from_config(self.config),
            )
            self._being.pin_top()
            self._quiet_stdout_logs()
            if session is not None:
                self._start_keys(session)
            self._publish()
        except Exception:  # visual failure must never take down voice I/O
            logger.exception("terminal visual failed to start")
            self._being = None

    def _quiet_stdout_logs(self) -> None:
        root = logging.getLogger()
        for handler in list(root.handlers):
            if not isinstance(handler, logging.StreamHandler):
                continue
            if isinstance(handler, logging.FileHandler):
                continue
            stream = getattr(handler, "stream", None)
            if stream in {sys.stdout, sys.stderr}:
                root.removeHandler(handler)

    def _start_keys(self, session: Any | None = None) -> None:
        if not sys.stdin.isatty():
            return
        if session is None:
            return
        try:
            import termios
            import tty
        except ImportError:
            return
        self._keys_stop = threading.Event()
        event_loop = asyncio.get_running_loop()
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)

        def read(n: int) -> str:
            return _read_tty(fd, n)

        def read_loop() -> None:
            try:
                tty.setcbreak(fd)
                while self._keys_stop is not None and not self._keys_stop.is_set():
                    ready, _, _ = select.select([fd], [], [], 0.1)
                    if not ready:
                        continue
                    # os.read, not sys.stdin.read: TextIOWrapper may prefetch
                    # the tail of an escape sequence before select sees it.
                    action = _read_terminal_key(read, fileno=fd)
                    if action is not None:
                        event_loop.call_soon_threadsafe(
                            self._handle_terminal_key, session, action
                        )
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)

        self._keys_thread = threading.Thread(
            target=read_loop, name="terminal-keys", daemon=True
        )
        self._keys_thread.start()

    def _stop_keys(self) -> None:
        if self._keys_stop is not None:
            self._keys_stop.set()
        self._keys_thread = None
        self._keys_stop = None

    def _handle_terminal_key(self, session: Any, key: TerminalKey) -> None:
        """Apply one decoded key on the event loop that owns live state."""
        if self._closed:
            return
        effect = self._controls.handle(key, self._presentation)
        if effect is not None:
            self._apply_control_effect(session, effect)
        self._publish()

    def _apply_control_effect(self, session: Any, effect: ControlEffect) -> None:
        if effect is GlobalControl.QUIT:
            self._request_shutdown(signal.SIGINT)
            return
        if effect is GlobalControl.STOP:
            self.request_cut_in()
            return
        if effect is GlobalControl.TOGGLE_MUTE:
            self._muted = not self._muted
            return
        if isinstance(effect, SetNarrationStyle):
            style = session.set_narration_style(effect.value)
            self._presentation = replace(self._presentation, narration_style=style)
            self._record("narration_style", style=style)
            return
        if isinstance(effect, SetReplyLength):
            length = session.set_reply_length(effect.value)
            self._presentation = replace(self._presentation, reply_length=length)
            self._record("reply_length", length=length)
            return
        if isinstance(effect, SetSpeechSpeed):
            speed = snap_speed(session.set_tts_speed(effect.value))
            self._presentation = replace(self._presentation, tts_speed=speed)
            self._record("tts_speed", speed=speed)
            return
        raise AssertionError(f"unknown terminal control effect: {effect!r}")

    def _sync_setting_values(self, session: Any) -> None:
        policy = getattr(session, "policy", None)
        if policy is None:
            policy = getattr(self.config, "policy", None)
        read_speed = getattr(session, "current_tts_speed", None)
        speed = read_speed() if callable(read_speed) else self._presentation.tts_speed
        self._presentation = replace(
            self._presentation,
            tts_speed=snap_speed(speed),
            narration_style=narration_style_or_default(
                getattr(policy, "narration_style", "")
            ),
            reply_length=reply_length_or_default(
                getattr(policy, "reply_length", "")
            ),
        )

    def _tts_speed_now(self) -> float:
        session = self._session
        reader = getattr(session, "current_tts_speed", None)
        if callable(reader):
            return snap_speed(reader())
        return snap_speed(self._presentation.tts_speed)

    def _publish(self, **fields: Any) -> None:
        if fields:
            self._presentation = replace(self._presentation, **fields)
        being = self._being
        if being is None:
            return
        try:
            set_settings_menu = getattr(being, "set_settings_menu", None)
            if callable(set_settings_menu):
                set_settings_menu(self._controls.view(self._presentation))
            being.set_presentation(self._presentation, muted=self._muted)
        except Exception:  # presentation observers are isolated from the kernel
            logger.exception("terminal visual state update failed")

    def _show_warming(self) -> None:
        """Hold presence at idle and name the window the mic is closed."""
        self._warming = True
        self._warming_at = time.monotonic()
        self._presentation = replace(
            self._presentation,
            presence="idle",
            lines=(
                *self._presentation.lines,
                Line(kind="progress", text=_WARMING_LINE),
            ),
        )
        self._publish()

    def _clear_warming(self) -> None:
        """Drop the warming line the moment the capture loop can hear.

        The measured window is also recorded: the deaf gap was reported as
        "about 3 seconds" and nobody had a number for it.
        """
        if not self._warming:
            return
        self._warming = False
        self._record(
            "capture_ready",
            warmup_ms=int(max(time.monotonic() - self._warming_at, 0.0) * 1000),
        )
        self._presentation = replace(
            self._presentation,
            lines=tuple(
                line for line in self._presentation.lines if line.text != _WARMING_LINE
            ),
        )
        self._publish()
        self._set_being("listening", detail="waiting for your voice")

    def _end_echo_gate(self, capture: _CaptureState) -> None:
        """The bot's audio is off the wire. Stop holding capture shut.

        The hush guard exists so the speaker's decaying tail is not taken
        for the user. Once the gate has decayed there is no tail left, and
        waiting on silence past that point is waiting for the *user* to
        stop talking — which on a noisy desk, or mid-sentence, never comes.

        That wait was unbounded, and the frames it dropped never reached
        the barge detector either, so nothing could rescue it: the terminal
        heard everything and captured none of it, for the rest of the
        session. Grace has already passed here; trust the microphone.
        """
        self._echo_gate_active = False
        if self._awaiting_hush_after_bot:
            self._awaiting_hush_after_bot = False
            self._record("hush_expired", grace_ms=self._echo_grace_ms)
        if not capture.open:
            capture.reset()
            self._set_hearing(False)
        self._record("echo_gate_ended", grace_ms=self._echo_grace_ms)

    def _release_hush(self, capture: _CaptureState) -> None:
        self._awaiting_hush_after_bot = False
        if capture.open:
            capture.reset()
            self._set_hearing(False)

    def _set_hearing(self, hearing: bool) -> None:
        being = self._being
        if being is None:
            return
        try:
            being.set_hearing(hearing)
        except Exception:
            logger.exception("terminal visual hearing update failed")

    def _set_being(self, state: str, *, active: bool = False, detail: str = "") -> None:
        del active, detail
        self._publish(presence=state)

    def _on_audio_queued(self, turn_id: int, byte_count: int, pcm: bytes = b"") -> None:
        """Enter SPEAKING only after canonical PCM reaches the device queue."""
        self._echo_canceller.push_farend(pcm)
        accounting = self._audio_output.accounting() if self._audio_output else None
        speaker = self._speaker
        self._mark_latency("playback_started", once=True, turn_id=turn_id)
        self._record(
            "audio_queued",
            turn_id=turn_id,
            bytes=byte_count,
            generated_bytes=(accounting.bytes_generated if accounting else byte_count),
            consumed_bytes=(accounting.bytes_written if accounting else 0),
            queued_bytes=(speaker.queued_bytes if speaker is not None else 0),
            dropped_chunks=(speaker.dropped_chunks if speaker is not None else 0),
        )
        if not self._terminal_full_duplex:
            if not self._echo_gate_active:
                self._aec_silent_reported = False
                self._record(
                    "echo_gate_started",
                    turn_id=turn_id,
                    grace_ms=self._echo_grace_ms,
                )
            self._echo_gate_active = True
            self._awaiting_hush_after_bot = True
            self._presentation_open = True
            self._capture_resume_at = max(
                self._capture_resume_at,
                asyncio.get_running_loop().time() + self._echo_grace_ms / 1000,
            )
        self._set_being("speaking", detail="speaking")
        self._sync_playback_floor()
        if self._playback_watch_task is None or self._playback_watch_task.done():
            self._playback_watch_task = asyncio.create_task(
                self._watch_playback(),
                name="terminal-playback-state",
            )

    def _on_audio_finished(self, turn_id: int) -> None:
        """The route says it has handed over the whole presentation.

        Whatever is still queued will now play out to its end, so the
        speaker is busy exactly as long as the queue is — no grace window
        standing in for a fact nobody had.
        """
        if not self._presentation_open:
            return
        self._presentation_open = False
        self._record("presentation_finished", turn_id=turn_id)

    def _on_audio_dropped(self, turn_id: int, byte_count: int) -> None:
        self._record(
            "audio_payload_dropped",
            turn_id=turn_id,
            bytes=byte_count,
            reason="empty_or_unsupported",
        )
        self._set_being("thinking", detail="voice output unavailable")

    def _playback_pending(self) -> bool:
        speaker = self._speaker
        return bool(speaker is not None and speaker.queued_bytes > 0)

    def _speaker_busy(self, now: float) -> bool:
        """Is the assistant still talking?

        An empty device queue is not the end of an utterance. Hybrid streams
        the answer from the provider in chunks, and a few hundred
        milliseconds of jitter drains the speaker between two words. Reading
        that as "the bot stopped" released the hush guard mid-sentence,
        which put the raw microphone — carrying the assistant's own voice —
        in front of the barge detector and the utterance assembler. The
        assistant interrupted itself, and the slivers of the user caught in
        those gaps were assembled into fragment turns:

            you    Can you Tell me The latest Well The
            you    What Yes States

        Bridge the gap with the same grace the echo gate already decays
        over, so only a real end of speech opens the microphone.
        """
        if self._playback_pending():
            return True
        if not self._presentation_open:
            # The route said it was done and the queue has drained. Nothing
            # left to bridge, and no reason to make the user wait out a
            # window that was only ever a guess.
            return False
        return now < self._capture_resume_at

    def _hush_check_due(self, now: float) -> bool:
        """May this frame be measured for the hush that reopens the mic?

        Only where the assistant has genuinely stopped. Asking during a gap
        between two chunks let one quiet frame — the pause between words —
        stand in for the end of the answer.
        """
        return (
            not self._terminal_full_duplex
            and not self._speaker_busy(now)
            and self._awaiting_hush_after_bot
        )

    async def _watch_playback(self) -> None:
        """Keep SPEAKING visible until callbacks consume all queued PCM."""
        last_consumed = -1
        stalled_polls = 0
        try:
            while not self._closed and self._playback_pending():
                await asyncio.sleep(0.05)
                accounting = self._audio_output.accounting() if self._audio_output else None
                consumed = accounting.bytes_written if accounting else 0
                if consumed > last_consumed:
                    last_consumed = consumed
                    stalled_polls = 0
                    elapsed_ms = int(consumed * 1000 / (_SAMPLE_RATE * _SAMPLE_WIDTH))
                    self._presentation = apply_playback_ms(self._presentation, elapsed_ms)
                    self._publish()
                else:
                    stalled_polls += 1
                if stalled_polls >= 40:
                    self._record(
                        "audio_playback_stalled",
                        generated_bytes=(accounting.bytes_generated if accounting else 0),
                        consumed_bytes=consumed,
                    )
                    self._presentation = replace(
                        self._presentation,
                        presence="idle",
                        lines=(
                            *self._presentation.lines,
                            Line(kind="progress", text="ERROR · speaker stalled"),
                        ),
                    )
                    self._publish()
                    return
            if not self._closed:
                self._sync_playback_floor()
                self._set_being("listening", detail="waiting for your voice")
        except asyncio.CancelledError:
            return

    def _on_session_event(self, event: SessionEvent) -> None:
        kind = event.type
        self._mark_latency_event(event)
        if kind in {SessionEventType.INTERRUPTION, SessionEventType.RESPONSE_CANCELLED}:
            if self._playback_watch_task is not None:
                self._playback_watch_task.cancel()
                self._playback_watch_task = None
        try:
            self._presentation = reduce(self._presentation, event)
        except Exception:
            logger.exception("presentation reduce failed")
            return
        self._publish()

    # -- signals ------------------------------------------------------------

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                self._previous_handlers[sig] = signal.getsignal(sig)
                loop.add_signal_handler(sig, self._request_shutdown, sig)
            except (NotImplementedError, ValueError, OSError):
                # Windows/non-main-thread: KeyboardInterrupt still lands in run()
                pass

    def _restore_signal_handlers(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        for sig in list(self._previous_handlers):
            try:
                loop.remove_signal_handler(sig)
            except (NotImplementedError, ValueError, RuntimeError):
                pass
        self._previous_handlers.clear()

    def _request_shutdown(self, sig: int) -> None:
        """Real signal handling (audit rework): set the shutdown event the
        drive loop polls — no loop.stop() shortcuts that strand tasks."""
        self._record("shutdown_requested", signal=signal.Signals(sig).name)
        self._shutdown.set()
        if self._run_task is not None and not self._run_task.done():
            self._run_task.cancel()

    # -- permission errors ----------------------------------------------------

    @staticmethod
    def _recovery_for(exc: AudioDeviceError) -> str:
        return audio_recovery(exc)

    def _check_mic_silence(self) -> None:
        """Say so once when the mic has given nothing but exact zeros."""
        silent = getattr(self._microphone, "silent_seconds", 0.0)
        if not isinstance(silent, (int, float)) or silent < _MIC_SILENT_SECONDS:
            return
        self._mic_silence_reported = True
        self._record("microphone_silent", seconds=round(silent, 1))
        self._presentation = replace(
            self._presentation,
            lines=(*self._presentation.lines, Line(kind="progress", text=_MIC_SILENT_LINE)),
        )
        self._publish()
