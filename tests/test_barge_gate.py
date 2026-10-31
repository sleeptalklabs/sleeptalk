"""The barge decision, driven from evidence tapes.

No audio, no transports, no async. Everything a surface can ask the kernel
is expressible as a list of BargeEvidence, which is what makes the
cross-surface parity test at the bottom possible.
"""

from __future__ import annotations

import pytest

from sleeptalk.barge import (
    BargeAction,
    BargeCalibration,
    BargeEvidence,
    BargeGate,
    BargeSurface,
    execute,
)
from sleeptalk.interrupt import AcousticKind, InterruptPolicy, SpeakingState


def _gate(policy=None, calibration=None, kind=AcousticKind.isolated, detector="silero"):
    clock = {"t": 0}
    gate = BargeGate(
        policy or InterruptPolicy(grace_ms=0, min_duration_ms=100, min_volume=0.03),
        kind,
        calibration=calibration,
        detector=detector,
        clock=lambda: clock["t"],
    )
    gate.turn_started()
    gate.audio_started()
    return gate


def _speech(channel="u1", level=0.5, ms=20.0, voiced=True, rms=0):
    return BargeEvidence(
        channel=channel, level=level, duration_ms=ms, voiced=voiced, raw_rms=rms
    )


def _quiet(channel="u1", ms=20.0):
    return BargeEvidence(channel=channel, level=0.0, duration_ms=ms, voiced=False)


def _run(gate, evidence):
    return [gate.observe(item) for item in evidence]


# -- sustain -----------------------------------------------------------------


def test_sustain_must_reach_min_duration_before_the_floor_is_asked():
    gate = _gate()
    decisions = _run(gate, [_speech()] * 4)
    assert [d.action for d in decisions[:4]] == [BargeAction.none] * 4
    assert gate.observe(_speech()).action is BargeAction.stop


def test_sustain_counts_windows_of_unequal_length():
    gate = _gate()
    assert gate.observe(_speech(ms=60)).action is BargeAction.none
    assert gate.observe(_speech(ms=60)).action is BargeAction.stop


def test_quiet_windows_never_build_sustain():
    gate = _gate()
    for decision in _run(gate, [_quiet()] * 20):
        assert decision.action is BargeAction.none


def test_speech_below_min_volume_does_not_count():
    """A confident detector on quiet audio is echo, not an interruption."""
    gate = _gate()
    for _ in range(20):
        assert gate.observe(_speech(level=0.01)).action is BargeAction.none


# -- dip tolerance -----------------------------------------------------------


def test_dip_shorter_than_tolerance_keeps_the_attempt_alive():
    """Real speech dips frame to frame; one low window is not a stop."""
    gate = _gate(calibration=BargeCalibration(dip_tolerance_ms=120))
    _run(gate, [_speech()] * 4)  # 80ms
    _run(gate, [_quiet()] * 2)  # 40ms dip, under tolerance
    assert gate.observe(_speech()).action is BargeAction.stop


def test_dip_time_is_not_credited_to_the_streak():
    gate = _gate(calibration=BargeCalibration(dip_tolerance_ms=120))
    _run(gate, [_speech()] * 4)  # 80ms of speech
    _run(gate, [_quiet()] * 5)  # 100ms of dip, still under tolerance
    # If dip counted, 180ms would already be past the 100ms bar.
    assert gate.observe(_speech()).action is BargeAction.stop
    # ...and it fired on the 100th ms of *speech*, not of elapsed time.
    assert gate.floor_state is SpeakingState.cooldown


def test_repeated_short_dips_still_reach_the_bar():
    """A whole word survives several dips; that is the point of tolerance."""
    gate = _gate(calibration=BargeCalibration(dip_tolerance_ms=120))
    fired = False
    for _ in range(6):
        fired = gate.observe(_speech()).fired or fired
        fired = gate.observe(_quiet()).fired or fired
    assert fired is True


def test_dip_at_tolerance_kills_the_streak():
    gate = _gate(calibration=BargeCalibration(dip_tolerance_ms=120))
    _run(gate, [_speech()] * 4)  # 80ms, short of 100
    _run(gate, [_quiet()] * 6)  # 120ms dip: attempt is dead
    _run(gate, [_speech()] * 4)  # 80ms again, must not add to the old 80
    assert gate.floor_state is not SpeakingState.cooldown


def test_zero_tolerance_resets_on_any_quiet_window():
    """Terminal and web reset instantly today and must keep doing so."""
    gate = _gate(calibration=BargeCalibration(dip_tolerance_ms=0))
    for _ in range(10):
        assert gate.observe(_speech()).action is BargeAction.none
        assert gate.observe(_quiet()).action is BargeAction.none


# -- calibration -------------------------------------------------------------


def test_level_scale_lifts_a_quiet_stop_over_the_bar():
    """Discord capture of a spoken "stop" lands near 0.02 against a 0.03 bar."""
    quiet_stop = 0.02
    unscaled = _gate()
    for _ in range(20):
        assert unscaled.observe(_speech(level=quiet_stop)).action is BargeAction.none

    scaled = _gate(calibration=BargeCalibration(level_scale=2.5))
    fired = any(scaled.observe(_speech(level=quiet_stop)).fired for _ in range(20))
    assert fired is True


def test_scaled_level_is_reported_next_to_the_raw_one():
    gate = _gate(calibration=BargeCalibration(level_scale=2.5))
    decision = gate.observe(_speech(level=0.2))
    assert decision.raw_level == pytest.approx(0.2)
    assert decision.level == pytest.approx(0.5)


def test_evidence_without_a_speech_opinion_falls_back_to_loudness():
    """Energy-only detectors (terminal's AEC residual) report voiced=None."""
    gate = _gate(kind=AcousticKind.mixed, detector="residual_rms")
    loud = BargeEvidence(channel="u1", level=0.5, duration_ms=20, voiced=None)
    fired = any(gate.observe(loud).fired for _ in range(10))
    assert fired is True


# -- one attempt belongs to one speaker --------------------------------------


def test_two_speakers_do_not_pool_their_evidence():
    gate = _gate()
    for index in range(10):
        decision = gate.observe(_speech(channel="alice" if index % 2 else "bob"))
        assert decision.action is BargeAction.none


def test_the_incumbent_yields_after_going_quiet():
    gate = _gate(calibration=BargeCalibration(dip_tolerance_ms=60))
    _run(gate, [_speech(channel="alice")] * 2)
    _run(gate, [_quiet(channel="alice")] * 4)
    fired = any(gate.observe(_speech(channel="bob")).fired for _ in range(10))
    assert fired is True


def test_a_barge_id_is_stable_across_one_attempt_and_changes_on_the_next():
    gate = _gate(calibration=BargeCalibration(dip_tolerance_ms=0))
    first = gate.observe(_speech()).barge_id
    assert gate.observe(_speech()).barge_id == first
    gate.observe(_quiet())
    gate.observe(_quiet())
    assert gate.observe(_speech()).barge_id != first


# -- floor admission ---------------------------------------------------------


def test_suppressed_evidence_never_reaches_the_floor():
    """Echo gate, hush, muted mic: recorded, never admitted."""
    gate = _gate()
    for _ in range(20):
        decision = gate.observe(
            BargeEvidence(channel="u1", level=0.9, voiced=True, suppressed="echo_gate")
        )
        assert decision.action is BargeAction.none
        assert "echo_gate" in decision.reason


def test_nothing_fires_without_a_turn():
    gate = BargeGate(InterruptPolicy(), AcousticKind.isolated, detector="silero")
    for _ in range(20):
        assert gate.observe(_speech()).action is BargeAction.none


def test_start_grace_holds_the_floor():
    clock = {"t": 0}
    gate = BargeGate(
        InterruptPolicy(grace_ms=400, min_duration_ms=100, min_volume=0.03),
        AcousticKind.isolated,
        detector="silero",
        clock=lambda: clock["t"],
    )
    gate.turn_started()
    gate.audio_started()
    for _ in range(20):
        decision = gate.observe(_speech())
        assert decision.action is BargeAction.none
    assert "grace" in decision.reason
    clock["t"] = 500
    assert gate.observe(_speech()).fired is True


def test_mode_off_never_fires():
    gate = _gate(InterruptPolicy(mode="off", grace_ms=0, min_duration_ms=20))
    for _ in range(20):
        assert gate.observe(_speech()).action is BargeAction.none


# -- soft vs immediate -------------------------------------------------------


def test_soft_pauses_where_the_surface_can_resume():
    gate = _gate(
        InterruptPolicy(mode="soft", grace_ms=0, min_duration_ms=20, min_volume=0.03),
        calibration=BargeCalibration(supports_resume=True),
    )
    decision = gate.observe(_speech())
    assert decision.action is BargeAction.pause
    assert decision.downgraded is False


def test_soft_downgrades_to_stop_where_it_cannot_and_says_so():
    """Terminal has no pause. It stopped before and stops now — on the record."""
    gate = _gate(
        InterruptPolicy(mode="soft", grace_ms=0, min_duration_ms=20, min_volume=0.03),
        calibration=BargeCalibration(supports_resume=False),
    )
    decision = gate.observe(_speech())
    assert decision.action is BargeAction.stop
    assert decision.downgraded is True


def test_soft_resolves_to_resume_on_a_backchannel():
    gate = _gate(
        InterruptPolicy(mode="soft", grace_ms=0, min_duration_ms=20, min_volume=0.03),
        calibration=BargeCalibration(supports_resume=True),
    )
    gate.observe(_speech())
    assert gate.resolve(backchannel=True).action is BargeAction.resume
    assert gate.resolve(backchannel=False).action is BargeAction.stop


def test_immediate_stops():
    gate = _gate(InterruptPolicy(mode="immediate", grace_ms=0, min_duration_ms=20))
    assert gate.observe(_speech()).action is BargeAction.stop


# -- ducking -----------------------------------------------------------------


def test_ducking_is_off_where_the_surface_does_not_support_it():
    gate = _gate(InterruptPolicy(grace_ms=0, min_duration_ms=10_000, min_volume=0.03))
    for _ in range(40):
        assert gate.observe(_speech()).action is BargeAction.none


def test_duck_then_release_on_a_blip():
    policy = InterruptPolicy(
        grace_ms=0, min_duration_ms=10_000, min_volume=0.03,
        duck_min_ms=200, duck_release_ms=600,
    )
    gate = _gate(policy, calibration=BargeCalibration(supports_duck=True))
    actions = [gate.observe(_speech()).action for _ in range(10)]
    assert actions.count(BargeAction.duck) == 1
    assert gate.ducked is True
    released = [gate.observe(_quiet()).action for _ in range(30)]
    assert released.count(BargeAction.resume) == 1
    assert gate.ducked is False


def test_duck_yields_to_a_real_barge_in():
    policy = InterruptPolicy(
        grace_ms=0, min_duration_ms=300, min_volume=0.03, duck_min_ms=100
    )
    gate = _gate(policy, calibration=BargeCalibration(supports_duck=True))
    actions = [gate.observe(_speech()).action for _ in range(20)]
    assert BargeAction.duck in actions
    assert actions[-1] is BargeAction.stop or BargeAction.stop in actions


# -- telemetry ---------------------------------------------------------------


def test_records_actions_and_transitions_but_not_every_window():
    events = []
    clock = {"t": 0}
    gate = BargeGate(
        InterruptPolicy(grace_ms=0, min_duration_ms=200, min_volume=0.03),
        AcousticKind.isolated,
        detector="silero",
        clock=lambda: clock["t"],
        emit=lambda stage, fields: events.append((stage, fields)),
    )
    gate.turn_started()
    gate.audio_started()
    _run(gate, [_speech()] * 40)
    assert len(events) < 5, f"per-window logging would cost frames: {len(events)}"
    assert events[-1][1]["action"] == "stop"


def test_every_event_of_one_attempt_shares_a_barge_id():
    events = []
    gate = BargeGate(
        InterruptPolicy(grace_ms=0, min_duration_ms=60, min_volume=0.03),
        AcousticKind.isolated,
        detector="silero",
        calibration=BargeCalibration(supports_duck=True),
        clock=lambda: 0,
        emit=lambda stage, fields: events.append(fields),
    )
    gate.turn_started()
    gate.audio_started()
    _run(gate, [_speech()] * 5)
    ids = {event["bargeId"] for event in events}
    assert len(ids) == 1 and "" not in ids


# -- executing a decision ----------------------------------------------------


class _Surface(BargeSurface):
    def __init__(self):
        self.calls = []

    async def duck(self, reason):
        self.calls.append(("duck", reason))
        return True

    async def resume(self, reason):
        self.calls.append(("resume", reason))
        return True

    async def halt(self, reason, *, resumable=False):
        self.calls.append(("pause" if resumable else "halt", reason))
        return True

    async def cancel_turn(self, reason):
        self.calls.append(("cancel", reason))


@pytest.mark.asyncio
async def test_stop_halts_output_before_cancelling_the_turn():
    gate = _gate(InterruptPolicy(mode="immediate", grace_ms=0, min_duration_ms=20))
    surface = _Surface()
    await execute(gate.observe(_speech()), surface)
    assert [name for name, _ in surface.calls] == ["halt", "cancel"]


@pytest.mark.asyncio
async def test_pause_halts_output_and_leaves_the_turn_alone():
    gate = _gate(
        InterruptPolicy(mode="soft", grace_ms=0, min_duration_ms=20, min_volume=0.03),
        calibration=BargeCalibration(supports_resume=True),
    )
    surface = _Surface()
    await execute(gate.observe(_speech()), surface)
    assert [name for name, _ in surface.calls] == ["pause"]


@pytest.mark.asyncio
async def test_a_minimal_surface_only_needs_halt_and_cancel():
    """The contributor contract: implement two methods, get barge-in."""

    class Minimal(BargeSurface):
        def __init__(self):
            self.halted = False

        async def halt(self, reason, *, resumable=False):
            self.halted = True
            return True

        async def cancel_turn(self, reason):
            self.cancelled = True

    gate = _gate(InterruptPolicy(mode="immediate", grace_ms=0, min_duration_ms=20))
    surface = Minimal()
    await execute(gate.observe(_speech()), surface)
    assert surface.halted and surface.cancelled


# -- parity ------------------------------------------------------------------


def test_the_same_word_decides_the_same_way_on_every_surface():
    """One evidence tape, three calibrations, one verdict.

    Calibration may change how loud a signal looks. It must not change what
    the kernel concludes once the signal clears the bar. Any future
    per-surface behaviour constant fails here first.
    """
    tape = [_speech(level=0.4)] * 3 + [_quiet()] + [_speech(level=0.4)] * 5

    verdicts = {}
    for name, calibration in {
        "terminal": BargeCalibration(dip_tolerance_ms=0),
        "web": BargeCalibration(dip_tolerance_ms=0),
        "discord": BargeCalibration(dip_tolerance_ms=0, level_scale=1.0),
    }.items():
        gate = _gate(calibration=calibration)
        verdicts[name] = [d.action for d in _run(gate, tape)]

    assert len(set(map(tuple, verdicts.values()))) == 1, verdicts


def test_a_non_microphone_interrupt_drops_accumulated_sustain():
    """A hotkey stop ends the attempt the detector was building.

    Terminal's residual streak used to be zeroed inside _apply_interrupt.
    Without that, sustain survives an interrupt the floor never saw, and the
    next barge fires on less fresh speech than min_duration_ms asks for.
    """
    gate = _gate()
    _run(gate, [_speech()] * 4)  # 80ms banked, short of the 100ms bar

    gate.reset_attempt()

    # One more window must not complete the old streak.
    assert gate.observe(_speech()).action is BargeAction.none
    assert gate.observe(_speech()).action is BargeAction.none
    assert gate.observe(_speech()).action is BargeAction.none
    assert gate.observe(_speech()).action is BargeAction.none
    assert gate.observe(_speech()).fired is True


def test_backchannels_are_recognised_in_every_page_language():
    from sleeptalk.barge import is_backchannel

    for text in ("Mm-hmm.", "Uh-huh!", "yeah yeah", "Ajá.", "Sí, sí.", "d'accord", "genau", "certo",
                 "aham", "はい", "うんうん", "네네", "嗯嗯。", "对对对", "ठीक है।", ""):
        assert is_backchannel(text), text
    for text in ("Wait, stop.", "Stop.", "No.", "What's the capital of France?", "Espera, para.",
                 "等一下，停。", "okay tell me more about tea", "停停"):
        assert not is_backchannel(text), text
