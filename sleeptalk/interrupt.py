"""Turn-scoped speaking Floor and interrupt policy.

AcousticKind is set by the transport. Isolated audio may use Silero.
Mixed audio may not.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from typing import Literal

InterruptMode = Literal["off", "soft", "immediate"]

_SILERO = "silero"
_MIC = "mic_speech"
_DRAIN_EVENTS = frozenset({"queue_empty", "drain"})


class AcousticKind(str, Enum):
    isolated = "isolated"
    mixed = "mixed"


class SpeakingState(str, Enum):
    idle = "idle"
    grace = "grace"
    speaking = "speaking"
    hold = "hold"
    cooldown = "cooldown"


@dataclass(frozen=True)
class InterruptPolicy:
    enabled: bool = True
    mode: InterruptMode = "immediate"
    min_duration_ms: int = 150
    grace_ms: int = 400
    gap_hold_ms: int = 600
    cooldown_ms: int = 1200
    min_volume: float = 0.03
    # Ducking is the reversible half of barge-in: pause output on early
    # speech, resume if it turns out to be a cough. Whether a surface can do
    # it at all is a transport capability; how long it waits is not.
    duck_min_ms: int = 200
    duck_release_ms: int = 600
    # Ducking judges the raw signal, not the calibrated one: pausing is
    # cheap and reversible, so it does not need the scaling that lifts a
    # quiet word over the interruption bar.
    duck_min_volume: float = 0.03
    duck_confidence: float = 0.70
    local_responsiveness: bool = False
    snappy_responsiveness: bool = False

    def for_acoustics(self, kind: AcousticKind) -> InterruptPolicy:
        """Fast reversible yielding requires an isolated, echo-controlled mic."""
        if not (self.local_responsiveness and self.snappy_responsiveness
                and kind is AcousticKind.isolated):
            return self
        return replace(
            self,
            min_duration_ms=min(self.min_duration_ms, 100),
            grace_ms=min(self.grace_ms, 50),
            gap_hold_ms=min(self.gap_hold_ms, 50),
            cooldown_ms=min(self.cooldown_ms, 300),
            duck_min_ms=min(self.duck_min_ms, 50),
            duck_release_ms=min(self.duck_release_ms, 250),
        )


@dataclass(frozen=True)
class FloorHit:
    fire: bool
    state: SpeakingState
    reason: str


class Floor:
    """Turn owns the floor. Detector is live after grace while audio plays.

    After queue_empty or drain the detector sleeps gap_hold_ms.
    """

    def __init__(
        self,
        policy: InterruptPolicy,
        kind: AcousticKind,
        *,
        detector: str = "none",
    ) -> None:
        if kind is AcousticKind.mixed and detector == _SILERO:
            raise ValueError("Silero cannot run on mixed audio")
        self.policy = policy
        self.kind = kind
        self.detector = detector
        self._turn = False
        self._playing = False
        self._grace_until = -1
        self._hold_until = -1
        self._cooldown_until = -1
        self._audio_seen = False
        self.state = SpeakingState.idle

    def observe(self, t_ms: int, event: str, *, volume: float = 1.0) -> FloorHit:
        if event == "turn_start":
            self._turn = True
            self._sync(t_ms)
            return FloorHit(False, self.state, "turn started")
        if event == "audio_start":
            self._playing = True
            local_isolated = self.policy.local_responsiveness and self.kind is AcousticKind.isolated
            grace = min(self.policy.grace_ms, 100) if local_isolated else self.policy.grace_ms
            self._grace_until = t_ms + (0 if local_isolated and self._audio_seen else grace)
            self._audio_seen = True
            self._hold_until = -1
            self._sync(t_ms)
            return FloorHit(False, self.state, "audio started")
        if event in _DRAIN_EVENTS:
            self._playing = False
            hold = self.policy.gap_hold_ms
            if self.policy.local_responsiveness and self.kind is AcousticKind.isolated:
                hold = min(hold, 100)
            self._hold_until = t_ms + hold
            self._sync(t_ms)
            return FloorHit(False, self.state, "drain hold")
        if event == "turn_end":
            self._turn = False
            self._playing = False
            self._grace_until = -1
            self._hold_until = -1
            self._sync(t_ms)
            return FloorHit(False, self.state, "turn ended")
        if event != _MIC:
            raise ValueError(f"unknown floor event {event!r}")
        return self._on_mic(t_ms, volume)

    def _on_mic(self, t_ms: int, volume: float) -> FloorHit:
        self._sync(t_ms)
        if not self.policy.enabled or self.policy.mode == "off":
            return FloorHit(False, self.state, "held. interrupt disabled")
        if volume < self.policy.min_volume:
            return FloorHit(False, self.state, "held. below min_volume")
        if self.state is SpeakingState.cooldown:
            return FloorHit(False, self.state, "held. cooldown")
        if not self._turn:
            return FloorHit(False, self.state, "held. no turn")
        if self._playing and t_ms < self._grace_until:
            return FloorHit(False, self.state, "held. still in start grace")
        if self._playing:
            return self._fire(t_ms, "interrupt while audio plays")
        if self._hold_until < 0:
            return FloorHit(False, self.state, "held. waiting for first audio")
        if t_ms < self._hold_until:
            return FloorHit(False, self.state, "held. leftover mic in the sentence gap")
        return self._fire(t_ms, "interrupt in a long gap after hold")

    def _fire(self, t_ms: int, reason: str) -> FloorHit:
        self._cooldown_until = t_ms + self.policy.cooldown_ms
        self.state = SpeakingState.cooldown
        return FloorHit(True, self.state, reason)

    def _sync(self, t_ms: int) -> None:
        if self._cooldown_until >= 0 and t_ms < self._cooldown_until:
            self.state = SpeakingState.cooldown
            return
        self._cooldown_until = -1
        if not self._turn:
            self.state = SpeakingState.idle
            return
        if self._playing:
            self.state = (
                SpeakingState.grace if t_ms < self._grace_until else SpeakingState.speaking
            )
            return
        if self._hold_until >= 0 and t_ms < self._hold_until:
            self.state = SpeakingState.hold
            return
        self.state = SpeakingState.speaking
