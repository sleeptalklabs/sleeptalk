"""Barge-in: one decision, many microphones.

A transport knows what its microphone heard. It does not know whether that
should interrupt the assistant. That is one product decision, and it lives
here.

The split:

* **Detection is environmental.** A laptop speaker bleeds the assistant's own
  voice back into the mic, so terminal listens to an AEC residual and trusts
  energy. A browser hands over an isolated, echo-cancelled stream, so the web
  transport can afford a speech model. Discord decodes Opus per speaker. None
  of that is policy, and the kernel does not try to own it.
* **The decision is policy.** How long speech must last, whether a dip inside
  a word ends the attempt, whether the floor is even contested right now, and
  what a fired barge does to playback — those must not differ by surface, and
  they all live in :class:`BargeGate`.

A transport therefore reports :class:`BargeEvidence` and executes the returned
:class:`BargeDecision`. Implementing a new surface means answering two
questions per audio window ("was that speech?", "how loud?") and honouring
four actions. It does not mean reimplementing turn-taking.

Numbers that legitimately differ per environment live in
:class:`BargeCalibration`, declared by the transport and visible in config.
Numbers that must not differ live in
:class:`~sleeptalk.interrupt.InterruptPolicy`.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from sleeptalk.interrupt import AcousticKind, Floor, InterruptPolicy, SpeakingState

__all__ = [
    "BargeAction",
    "BargeCalibration",
    "BargeDecision",
    "BargeEvidence",
    "BargeGate",
    "BargeSurface",
    "execute",
]


class BargeAction(str, Enum):
    """What the transport must do about the audio it just reported."""

    none = "none"
    #: Early speech, not yet an interruption. Pause output; reversible.
    duck = "duck"
    #: The floor was taken under ``mode="soft"``. Stop output, keep the turn
    #: alive until a transcript says whether this was a real interruption.
    pause = "pause"
    #: The floor was taken under ``mode="immediate"``. Stop output, cancel
    #: the turn.
    stop = "stop"
    #: A soft barge resolved as a backchannel. Put the assistant back on.
    resume = "resume"


@dataclass(frozen=True)
class BargeCalibration:
    """How one environment's audio maps onto shared policy bars.

    These exist because microphones differ, not because products differ. A
    surface that changes one of these is saying "my signal is shaped like
    this"; a surface that wants different *behaviour* is changing
    :class:`~sleeptalk.interrupt.InterruptPolicy`, which is shared.
    """

    #: Scales the level scalar before it meets ``InterruptPolicy.min_volume``.
    #: Discord capture of a spoken "stop" lands near 0.02 against a 0.03 bar.
    level_scale: float = 1.0
    #: Speech dips mid-word. A gap shorter than this keeps the attempt alive.
    #: 0 means any non-speech window ends it.
    dip_tolerance_ms: float = 0.0
    #: Raw RMS that counts as energy for ducking alone (0 disables). Ducking
    #: is reversible, so it may run on looser evidence than an interruption.
    duck_rms_threshold: int = 0
    #: Whether this surface can pause and resume output. A surface that
    #: cannot must not be handed ``pause``; the gate downgrades it to
    #: ``stop`` and says so rather than silently ignoring soft mode.
    supports_resume: bool = False
    #: Whether this surface implements ducking at all.
    supports_duck: bool = False


@dataclass(frozen=True)
class BargeEvidence:
    """One analysis window of conditioned audio from one speaker.

    ``level`` is normalised loudness in 0..1 *before* calibration. ``voiced``
    is the detector's speech opinion; ``None`` means this window carries no
    opinion (a suppressed frame, or a detector that only measures energy).
    """

    channel: str = ""
    duration_ms: float = 20.0
    level: float = 0.0
    voiced: bool | None = None
    confidence: float | None = None
    raw_rms: int = 0
    #: Non-empty when the transport withheld this window (echo gate, hush,
    #: muted mic). Recorded, never admitted.
    suppressed: str = ""


@dataclass(frozen=True)
class BargeDecision:
    """What the gate concluded, and everything needed to explain it later."""

    action: BargeAction
    reason: str
    floor_state: SpeakingState
    barge_id: str = ""
    channel: str = ""
    sustain_ms: float = 0.0
    dip_ms: float = 0.0
    level: float = 0.0
    raw_level: float = 0.0
    mode: str = ""
    detector: str = ""
    downgraded: bool = False

    @property
    def fired(self) -> bool:
        """True when the floor changed hands (as opposed to ducking)."""
        return self.action in (BargeAction.pause, BargeAction.stop)

    def as_event(self) -> dict[str, Any]:
        """Flat telemetry record. Identical shape on every surface."""
        return {
            "bargeId": self.barge_id,
            "action": self.action.value,
            "reason": self.reason,
            "floorState": self.floor_state.value,
            "channel": self.channel,
            "detector": self.detector,
            "mode": self.mode,
            "sustainMs": round(self.sustain_ms),
            "dipMs": round(self.dip_ms),
            "level": round(self.level, 4),
            "rawLevel": round(self.raw_level, 4),
            "downgraded": self.downgraded,
        }


class BargeSurface:
    """What a transport must be able to do about a decision.

    Defaults are no-ops so a minimal transport can implement only ``halt``
    and ``cancel_turn`` and still get correct interruption behaviour.
    """

    async def duck(self, reason: str) -> bool:
        return False

    async def resume(self, reason: str) -> bool:
        return False

    async def halt(self, reason: str, *, resumable: bool = False) -> bool:
        """Stop audible output now.

        ``resumable`` is a soft barge: the assistant may be put back on if
        the transcript turns out to be a backchannel. A surface that cannot
        do that never sees it — the gate downgrades soft to stop first.
        """
        return False

    async def hold_turn(self, reason: str) -> None:
        """A soft barge fired: output stopped, the turn is still alive.

        The transport should start capturing the interrupting speech — the
        transcript is what decides whether this becomes a cancellation.
        """

    async def cancel_turn(self, reason: str) -> None:
        """Interrupt the presentation and cancel the work behind it."""


@dataclass
class _Attempt:
    """One speaker's run at taking the floor."""

    channel: str = ""
    sustain_ms: float = 0.0
    dip_ms: float = 0.0
    barge_id: str = ""


class BargeGate:
    """Owns sustain, dip tolerance, floor admission and post-fire action.

    The gate is a synchronous state machine: detection already happened in
    the transport, so nothing here awaits. That makes the whole product
    decision testable from a list of evidence windows, which is how the
    cross-surface parity tests work.
    """

    def __init__(
        self,
        policy: InterruptPolicy,
        kind: AcousticKind,
        *,
        calibration: BargeCalibration | None = None,
        detector: str = "none",
        clock: Callable[[], int] = lambda: int(time.monotonic() * 1000),
        emit: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        policy = policy.for_acoustics(kind)
        self.policy = policy
        self.kind = kind
        self.calibration = calibration or BargeCalibration()
        self.detector = detector
        self._floor = Floor(policy, kind, detector=detector)
        # Milliseconds on whatever timebase the transport already uses. The
        # floor only ever compares them, so the epoch does not matter — but
        # rebasing here would corrupt a transport whose clock is injectable.
        self._clock = clock
        self._emit = emit
        self._attempt = _Attempt()
        self._duck_streak_ms = 0.0
        self._duck_quiet_ms = 0.0
        self._ducked = False
        self._fired_this_turn = False
        self._last_floor_state = self._floor.state

    def retune(self, policy: InterruptPolicy) -> None:
        """Adopt new policy values mid-session (the live /vad commands)."""
        policy = policy.for_acoustics(self.kind)
        self.policy = policy
        self._floor.policy = policy

    # -- turn lifecycle ---------------------------------------------------

    @property
    def floor_state(self) -> SpeakingState:
        return self._floor.state

    @property
    def idle(self) -> bool:
        return self._floor.state is SpeakingState.idle

    @property
    def ducked(self) -> bool:
        return self._ducked

    @property
    def attempt_channel(self) -> str:
        """Who currently owns the barge attempt, if anyone."""
        return self._attempt.channel

    def turn_started(self) -> None:
        self._floor.observe(self._now_ms(), "turn_start")

    def audio_started(self) -> None:
        self._floor.observe(self._now_ms(), "audio_start")
        self._fired_this_turn = False

    def audio_drained(self) -> None:
        self._floor.observe(self._now_ms(), "drain")

    def reset_attempt(self) -> None:
        """Drop accumulated evidence without touching the floor.

        An interruption that did not come from the microphone — a hotkey, a
        committed utterance — still ends whatever the detector was building
        towards. Carrying that sustain into the next turn would let the next
        barge fire on less fresh speech than the policy asks for.
        """
        self._reset_attempt()

    def turn_ended(self) -> None:
        self._floor.observe(self._now_ms(), "turn_end")
        self._reset_attempt()
        self._reset_duck()
        self._fired_this_turn = False

    # -- the decision -----------------------------------------------------

    def observe(self, evidence: BargeEvidence) -> BargeDecision:
        """Fold one window of evidence into the floor decision."""
        if self.idle:
            return self._decide(BargeAction.none, "no turn in flight", evidence)

        level = evidence.level * self.calibration.level_scale
        loud = level >= self.policy.min_volume
        voiced = bool(evidence.voiced) if evidence.voiced is not None else loud
        held = bool(evidence.suppressed)

        duck = BargeAction.none if held else self._update_duck(evidence, voiced=voiced)

        if not self._own_attempt(evidence.channel):
            return self._decide(
                BargeAction.none, "another speaker owns this attempt", evidence, level=level
            )

        # A withheld window is not speech, so it decays the attempt like any
        # other silence — it simply never reaches the floor.
        self._accumulate(evidence, qualified=not held and voiced and loud)
        if held:
            return self._decide(
                BargeAction.none, f"suppressed. {evidence.suppressed}", evidence, level=level
            )
        if self._attempt.sustain_ms < self.policy.min_duration_ms:
            return self._decide(duck, "building sustain", evidence, level=level)

        hit = self._floor.observe(self._now_ms(), "mic_speech", volume=level)
        if not hit.fire:
            return self._decide(duck, hit.reason, evidence, level=level)
        return self._fire(evidence, level)

    def resolve(self, *, backchannel: bool) -> BargeDecision:
        """Second half of a soft barge: the transcript arrived.

        A backchannel ("mhm", "right") puts the assistant back on. Anything
        else is a real interruption and cancels the turn.
        """
        action = BargeAction.resume if backchannel else BargeAction.stop
        reason = "backchannel. resuming" if backchannel else "transcript confirms interrupt"
        decision = BargeDecision(
            action=action,
            reason=reason,
            floor_state=self._floor.state,
            barge_id=self._attempt.barge_id,
            channel=self._attempt.channel,
            mode=self.policy.mode,
            detector=self.detector,
        )
        if backchannel:
            self._ducked = False
        self._reset_attempt()
        self._record("barge_resolved", decision)
        return decision

    # -- internals --------------------------------------------------------

    def _now_ms(self) -> int:
        return int(self._clock())

    def _own_attempt(self, channel: str) -> bool:
        """One interruption belongs to one speaker.

        Two voices folded into a single streak can take the floor on evidence
        neither of them produced, so a second speaker is held out until the
        incumbent has been quiet for the dip tolerance.
        """
        incumbent = self._attempt.channel
        if incumbent == channel:
            return True
        if incumbent and self._attempt.dip_ms < self._dip_tolerance():
            return False
        self._attempt = _Attempt(channel=channel)
        return True

    def _dip_tolerance(self) -> float:
        return max(self.calibration.dip_tolerance_ms, 0.0)

    def _accumulate(self, evidence: BargeEvidence, *, qualified: bool) -> None:
        if qualified:
            if not self._attempt.sustain_ms:
                # A run of speech is starting: this is the attempt telemetry
                # will be reconstructed from, so it gets its own id.
                self._attempt.barge_id = uuid.uuid4().hex[:12]
            self._attempt.sustain_ms += evidence.duration_ms
            self._attempt.dip_ms = 0.0
            return
        self._attempt.dip_ms += evidence.duration_ms
        if self._attempt.dip_ms >= self._dip_tolerance():
            # The attempt is dead. The dip keeps growing, because it is also
            # the evidence that lets another speaker take over.
            self._attempt.sustain_ms = 0.0
            self._attempt.barge_id = ""

    def _update_duck(self, evidence: BargeEvidence, *, voiced: bool) -> BargeAction:
        cal = self.calibration
        if not cal.supports_duck or self._fired_this_turn:
            return BargeAction.none
        energetic = bool(cal.duck_rms_threshold) and evidence.raw_rms >= cal.duck_rms_threshold
        confident = (
            evidence.confidence is None
            or evidence.confidence >= self.policy.duck_confidence
        )
        speaking = energetic or (
            voiced and confident and evidence.level >= self.policy.duck_min_volume
        )
        if speaking:
            self._duck_quiet_ms = 0.0
            if self._ducked:
                return BargeAction.none
            self._duck_streak_ms += evidence.duration_ms
            if self._duck_streak_ms >= self.policy.duck_min_ms:
                self._ducked = True
                return BargeAction.duck
            return BargeAction.none
        self._duck_streak_ms = 0.0
        if not self._ducked:
            return BargeAction.none
        self._duck_quiet_ms += evidence.duration_ms
        if self._duck_quiet_ms >= self.policy.duck_release_ms:
            self._ducked = False
            return BargeAction.resume
        return BargeAction.none

    def _fire(self, evidence: BargeEvidence, level: float) -> BargeDecision:
        self._fired_this_turn = True
        downgraded = False
        if self.policy.mode == "soft":
            if self.calibration.supports_resume:
                action = BargeAction.pause
            else:
                # Soft means "pause and let the transcript decide". A surface
                # that cannot resume cannot honour that, so it stops instead
                # — the same thing it did before, now on the record.
                action = BargeAction.stop
                downgraded = True
        else:
            action = BargeAction.stop
        decision = self._decide(
            action, "interrupt taken", evidence, level=level, downgraded=downgraded
        )
        self._reset_duck()
        if action is BargeAction.stop:
            self._reset_attempt()
        return decision

    def _decide(
        self,
        action: BargeAction,
        reason: str,
        evidence: BargeEvidence,
        *,
        level: float | None = None,
        downgraded: bool = False,
    ) -> BargeDecision:
        decision = BargeDecision(
            action=action,
            reason=reason,
            floor_state=self._floor.state,
            barge_id=self._attempt.barge_id,
            channel=evidence.channel,
            sustain_ms=self._attempt.sustain_ms,
            dip_ms=self._attempt.dip_ms,
            level=level if level is not None else evidence.level,
            raw_level=evidence.level,
            mode=self.policy.mode,
            detector=self.detector,
            downgraded=downgraded,
        )
        self._maybe_record(decision)
        return decision

    def _maybe_record(self, decision: BargeDecision) -> None:
        """Emit on anything that happened, never per window.

        Telemetry writes are synchronous and this runs in the audio path, so
        a per-frame record would cost frames. A transition or an action is
        enough to reconstruct the episode.
        """
        moved = decision.floor_state is not self._last_floor_state
        self._last_floor_state = decision.floor_state
        if decision.action is not BargeAction.none or moved:
            self._record("barge_decision", decision)

    def _record(self, stage: str, decision: BargeDecision) -> None:
        if self._emit is not None:
            self._emit(stage, decision.as_event())

    def _reset_attempt(self) -> None:
        self._attempt = _Attempt()

    def _reset_duck(self) -> None:
        self._duck_streak_ms = 0.0
        self._duck_quiet_ms = 0.0
        self._ducked = False


async def execute(decision: BargeDecision, surface: BargeSurface) -> None:
    """Carry out a decision. One definition of what each action means."""
    action = decision.action
    if action is BargeAction.none:
        return
    if action is BargeAction.duck:
        await surface.duck("duck-on-vad")
        return
    if action is BargeAction.resume:
        await surface.resume("duck-release")
        return
    if action is BargeAction.pause:
        await surface.halt("barge-in-pending", resumable=True)
        await surface.hold_turn(decision.reason)
        return
    await surface.halt("barge-in", resumable=False)
    await surface.cancel_turn(decision.reason)


# What a listener says while the assistant talks without asking for the floor:
# "mm-hmm", "right", "ajá", "嗯". A soft barge whose transcript is only these
# puts the assistant back on. Per language, as a listener actually says them.
_BACKCHANNELS = frozenset({
    # English
    "mhm", "mm", "mmm", "mm hmm", "mmhmm", "mm-hmm", "hmm", "hm", "uh huh", "uh-huh", "uhhuh",
    "yeah", "yep", "yup", "yes", "ok", "okay", "right", "sure", "got it", "i see", "cool", "nice",
    "ah", "oh", "aha", "ahh", "ohh",
    # Spanish
    "ajá", "aja", "ajam", "sí", "si", "vale", "claro", "ya", "ya ya", "bueno", "entiendo",
    # French
    "oui", "ouais", "d'accord", "dac", "je vois",
    # German
    "ja", "genau", "achso", "ach so", "stimmt", "alles klar",
    # Italian
    "sì", "certo", "già", "capito", "va bene",
    # Portuguese
    "sim", "tá", "ta", "aham", "uhum", "entendi", "certo",
    # Dutch
    "oké", "precies", "klopt",
    # Japanese
    "はい", "うん", "ええ", "なるほど", "そうですね", "そっか", "へえ",
    # Korean
    "네", "응", "아", "그렇군요", "그래요", "맞아요",
    # Chinese
    "嗯", "嗯嗯", "对", "对对", "好", "好的", "是的", "哦", "明白",
    # Hindi
    "हाँ", "हां", "अच्छा", "ठीक है", "जी", "जी हाँ",
})


def is_backchannel(transcript: str) -> bool:
    """Whether a barge's transcript is only a backchannel (or nothing at all).

    Case, punctuation and repetition don't matter: "Mm-hmm.", "嗯嗯。", "Sí, sí"
    all count; "Wait, stop." and "What's the capital?" don't.
    """
    import re
    import unicodedata

    text = unicodedata.normalize("NFC", str(transcript or "")).lower().strip()
    text = re.sub(r"[\s.,!?¡¿…。，！？、।~\-–—\"“”]+", " ", text).strip()
    if not text:
        return True
    if text in _BACKCHANNELS or text.replace(" ", "") in _BACKCHANNELS:
        return True
    words = text.split()
    # A repeated backchannel ("yeah yeah", "sí sí", "对 对").
    if len(words) <= 3 and all(word in _BACKCHANNELS for word in words):
        return True
    # The same repeated with no spaces, as CJK is written ("うんうん", "对对对", "네네").
    compact = text.replace(" ", "")
    return any(len(compact) % len(unit) == 0 and compact == unit * (len(compact) // len(unit))
               for unit in _BACKCHANNELS if len(unit) <= 2 and len(compact) <= 6)
