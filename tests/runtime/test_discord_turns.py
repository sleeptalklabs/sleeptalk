import asyncio
from types import SimpleNamespace

import pytest

from sleeptalk.voice_loop.endpointing import TurnCompletionSignal, decide_endpoint
from sleeptalk.discord_native.timeline import VoiceTimeline
from sleeptalk.discord_native.turns import (
    DiscordTurnConfig,
    DiscordTurnCoordinator,
    _DISCORD_BARGE_VOLUME_GAIN,
    interrupt_from_voice_policy,
)
from sleeptalk.voice_loop.vad import (
    SileroVadConfig,
    SpeechGate,
    VadObservation,
    pcm_duration_ms,
    pcm_rms,
)
from sleeptalk.interrupt import InterruptPolicy, SpeakingState


def test_pcm_rms_and_duration_for_discord_frame():
    pcm = (1000).to_bytes(2, "little", signed=True) * 2 * 960

    assert pcm_rms(pcm) == 1000
    assert pcm_duration_ms(pcm) == 20


def test_speech_gate_requires_consecutive_speech_ms():
    gate = SpeechGate(threshold=500, min_speech_ms=40)
    loud_frame = (1000).to_bytes(2, "little", signed=True) * 2 * 960

    assert gate.observe(loud_frame) is False
    assert gate.observe(loud_frame) is True
    gate.reset()
    assert gate.observe(b"\x00\x00" * 2 * 960) is False


def _speech_silero(monkeypatch, *, volume=0.5):
    class Gate:
        speaking = True

        def __init__(self, _config: SileroVadConfig):
            pass

        def reset(self):
            return None

        async def observe(self, _pcm):
            self.speaking = True
            return VadObservation(
                is_speech=True,
                started=True,
                state="SPEAKING",
                confidence=0.95,
                volume=volume,
            )

    monkeypatch.setattr("sleeptalk.discord_native.turns.SileroVadGate", Gate)


def _quiet_silero(monkeypatch):
    class Gate:
        speaking = False

        def __init__(self, _config: SileroVadConfig):
            pass

        def reset(self):
            return None

        async def observe(self, _pcm):
            return VadObservation(is_speech=False, started=False, state="QUIET")

    monkeypatch.setattr("sleeptalk.discord_native.turns.SileroVadGate", Gate)


def _open_floor(interrupt=None, **config_kw):
    class FakePlayback:
        active = True

        async def pause(self, _reason):
            return True

        async def resume(self, _reason):
            return True

        async def stop(self, _reason):
            self.active = False
            return True

    return DiscordTurnCoordinator(
        config=DiscordTurnConfig(**config_kw),
        playback=FakePlayback(),
        timeline=VoiceTimeline(),
        interrupt=interrupt,
    )


def test_interrupt_from_voice_policy_reads_gap_hold():
    policy = SimpleNamespace(
        interrupt=SimpleNamespace(
            enabled=True,
            mode="immediate",
            min_duration_ms=150,
            grace_ms=400,
            gap_hold_ms=900,
            cooldown_ms=1200,
            min_volume=0.03,
        )
    )
    assert interrupt_from_voice_policy(policy).gap_hold_ms == 900


@pytest.mark.asyncio
async def test_soft_barge_pauses_until_transcript_classifies_it(monkeypatch):
    calls = []
    _speech_silero(monkeypatch)

    class Playback:
        active = True

        async def pause(self, reason):
            calls.append(("pause", reason))
            return True

        async def resume(self, reason):
            calls.append(("resume", reason))
            return True

        async def stop(self, reason):
            calls.append(("stop", reason))
            return True

    async def interrupted(_user):
        calls.append(("interrupt", "pending"))

    coordinator = DiscordTurnCoordinator(
        config=DiscordTurnConfig(duck_min_ms=20),
        playback=Playback(),
        timeline=VoiceTimeline(),
        on_interrupt=interrupted,
        interrupt=InterruptPolicy(mode="soft", grace_ms=0, min_duration_ms=20),
    )
    coordinator.bot_playback_started()
    loud = (1000).to_bytes(2, "little", signed=True) * 2 * 960

    assert await coordinator.observe_pcm(SimpleNamespace(id=123), loud) is True
    assert not any(kind == "stop" for kind, _ in calls)
    assert ("interrupt", "pending") in calls

    assert await coordinator.resume_after_soft_barge("backchannel") is True
    assert ("resume", "backchannel") in calls


def test_adaptive_endpointing_waits_on_trailing_pronouns():
    # "What should we" must not flush quickly — subject pronouns trail off.
    decision = decide_endpoint(
        signal=TurnCompletionSignal(transcript="What should we", is_final=True, source="deepgram"),
        speech_duration_ms=900,
        silence_ms=700,
        base_silence_ms=120,
    )
    assert decision.should_endpoint is False
    assert decision.threshold_ms >= 1800


def test_adaptive_endpointing_allows_fast_complete_question():
    decision = decide_endpoint(
        signal=TurnCompletionSignal(transcript="How's it going?"),
        speech_duration_ms=900,
        silence_ms=500,
        base_silence_ms=120,
    )

    assert decision.should_endpoint is True
    assert decision.mode == "responsive"
    assert decision.reason == "complete"
    assert decision.threshold_ms == 450


def test_adaptive_endpointing_does_not_promote_unpunctuated_stt_final():
    early = decide_endpoint(
        signal=TurnCompletionSignal(
            transcript="Fix the wording file",
            is_final=True,
            source="deepgram",
        ),
        speech_duration_ms=900,
        silence_ms=500,
        base_silence_ms=120,
    )
    ready = decide_endpoint(
        signal=TurnCompletionSignal(
            transcript="Fix the wording file",
            is_final=True,
            source="deepgram",
        ),
        speech_duration_ms=900,
        silence_ms=700,
        base_silence_ms=120,
    )

    assert early.should_endpoint is False
    assert early.reason == "uncertain"
    assert early.threshold_ms == 1_800
    assert ready.should_endpoint is False


@pytest.mark.parametrize(
    "transcript",
    (
        "Can you make a little Pokemon game for me and then open it so I could",
        "I'm I'm not",
        "Yeah. Make a simple one and make it like a.",
    ),
)
def test_adaptive_endpointing_holds_observed_cutoff_prefixes(transcript):
    decision = decide_endpoint(
        signal=TurnCompletionSignal(
            transcript=transcript,
            is_final=True,
            source="deepgram",
        ),
        speech_duration_ms=8_000,
        silence_ms=955,
        base_silence_ms=450,
    )

    assert decision.should_endpoint is False
    assert decision.reason in {"uncertain", "incomplete"}
    assert decision.threshold_ms >= 1_800


def test_adaptive_endpointing_waits_for_acoustic_fallback():
    decision = decide_endpoint(
        signal=TurnCompletionSignal(transcript="", source="deepgram"),
        speech_duration_ms=900,
        silence_ms=120,
        base_silence_ms=120,
    )

    # A transcript source exists, so the transcript is EXPECTED — its
    # absence means STT lag; wait the uncertain budget instead of flushing
    # at the bare acoustic threshold.
    assert decision.should_endpoint is False
    assert decision.reason == "acoustic_fallback"
    assert decision.threshold_ms == 1_800


def test_adaptive_endpointing_stretches_incomplete_clause():
    decision = decide_endpoint(
        signal=TurnCompletionSignal(transcript="So what we need is basically a way to"),
        speech_duration_ms=4_000,
        silence_ms=1_000,
        base_silence_ms=120,
    )

    assert decision.should_endpoint is False
    assert decision.reason == "incomplete"
    assert decision.threshold_ms == 3_500


def test_adaptive_endpointing_does_not_treat_dangling_final_as_complete():
    decision = decide_endpoint(
        signal=TurnCompletionSignal(transcript="What we need is", is_final=True),
        speech_duration_ms=2_000,
        silence_ms=1_000,
        base_silence_ms=120,
    )

    assert decision.should_endpoint is False
    assert decision.reason == "incomplete"


def test_trailing_adverbs_are_floor_holders():
    # "And, also," / "And also." must never endpoint quickly, even when the
    # STT punctuates the fragment as a final sentence.
    for transcript in ("And, also,", "And also.", "but anyway", "plus"):
        decision = decide_endpoint(
            signal=TurnCompletionSignal(
                transcript=transcript, is_final=True, source="deepgram"
            ),
            speech_duration_ms=1_500,
            silence_ms=900,
            base_silence_ms=900,
        )
        assert decision.should_endpoint is False, transcript
        assert decision.reason == "floor_holder", transcript
        assert decision.threshold_ms >= 3_500, transcript


def test_short_final_not_promoted_to_complete():
    # STTs mark mid-thought fragments as final; a short final or one ending
    # on a dangling word must not get the is_final score bump.
    decision = decide_endpoint(
        signal=TurnCompletionSignal(
            transcript="I was trying to say that", is_final=True, source="deepgram"
        ),
        speech_duration_ms=2_500,
        silence_ms=900,
        base_silence_ms=900,
    )
    assert decision.should_endpoint is False
    assert decision.reason == "incomplete"
    assert decision.threshold_ms == 3_500


def test_acoustic_fallback_stretches_for_sustained_speech():
    # No transcript signal yet, but the user spoke for a while: a bare VAD
    # stop must not flush after base silence — wait the uncertain budget so
    # an interim transcript has time to land.
    decision = decide_endpoint(
        signal=TurnCompletionSignal(transcript="", source="deepgram"),
        speech_duration_ms=4_000,
        silence_ms=900,
        base_silence_ms=900,
    )
    assert decision.should_endpoint is False
    assert decision.reason == "acoustic_fallback"
    assert decision.threshold_ms == 1_800


def test_acoustic_fallback_stays_snappy_for_blips_without_source():
    # No transcript source at all (acoustic-only pipeline): a brief blip
    # still flushes at the bare base threshold.
    decision = decide_endpoint(
        signal=None,
        speech_duration_ms=400,
        silence_ms=900,
        base_silence_ms=900,
    )
    assert decision.should_endpoint is True
    assert decision.reason == "acoustic_fallback"


def test_adaptive_endpointing_resets_patience_for_fillers():
    decision = decide_endpoint(
        signal=TurnCompletionSignal(transcript="I think we should, um"),
        speech_duration_ms=18_000,
        silence_ms=3_600,
        base_silence_ms=120,
    )

    assert decision.should_endpoint is False
    assert decision.mode == "monologue"
    assert decision.filler_detected is True
    assert decision.threshold_ms == 5_000


@pytest.mark.asyncio
async def test_turn_coordinator_stops_playback_and_cancels_backend_on_barge_in(
    monkeypatch,
):
    cancelled = 0
    _speech_silero(monkeypatch)

    class FakePlayback:
        active = True

        async def stop(self, reason):
            assert reason == "barge-in"
            self.active = False
            return True

    async def cancel_backend(*_args):
        nonlocal cancelled
        cancelled += 1

    timeline = VoiceTimeline()
    coordinator = DiscordTurnCoordinator(
        config=DiscordTurnConfig(),
        playback=FakePlayback(),
        timeline=timeline,
        on_interrupt=cancel_backend,
        interrupt=InterruptPolicy(grace_ms=0, min_duration_ms=20),
    )
    coordinator.bot_playback_started()
    loud_frame = (1000).to_bytes(2, "little", signed=True) * 2 * 960
    user = SimpleNamespace(id=123, name="Alice")

    assert await coordinator.observe_pcm(user, loud_frame) is True
    assert cancelled == 1
    assert coordinator.stats.interruptions == 1
    assert "barge_in_detected" in timeline.compact()


@pytest.mark.asyncio
async def test_turn_coordinator_respects_bot_grace_period(monkeypatch):
    _speech_silero(monkeypatch)

    class FakePlayback:
        active = True

        async def stop(self, _reason):
            raise AssertionError("Should not stop during grace period")

    coordinator = DiscordTurnCoordinator(
        config=DiscordTurnConfig(),
        playback=FakePlayback(),
        timeline=VoiceTimeline(),
        interrupt=InterruptPolicy(grace_ms=10_000, min_duration_ms=20),
    )
    coordinator.bot_playback_started()
    loud_frame = (1000).to_bytes(2, "little", signed=True) * 2 * 960

    assert await coordinator.observe_pcm(SimpleNamespace(id=123), loud_frame) is False
    await asyncio.sleep(0)
    assert coordinator.stats.interruptions == 0


@pytest.mark.asyncio
async def test_turn_coordinator_interrupts_between_playback_files(monkeypatch):
    cancelled = 0
    _speech_silero(monkeypatch)

    class FakePlayback:
        active = False

        async def stop(self, reason):
            assert reason == "barge-in"
            return False

    async def cancel_backend(*_args):
        nonlocal cancelled
        cancelled += 1

    coordinator = DiscordTurnCoordinator(
        config=DiscordTurnConfig(),
        playback=FakePlayback(),
        timeline=VoiceTimeline(),
        on_interrupt=cancel_backend,
        interrupt=InterruptPolicy(grace_ms=0, min_duration_ms=20),
    )
    coordinator.bot_playback_started()
    loud_frame = (1000).to_bytes(2, "little", signed=True) * 2 * 960

    assert await coordinator.observe_pcm(SimpleNamespace(id=123), loud_frame) is True
    assert cancelled == 1
    assert coordinator.stats.interruptions == 1


@pytest.mark.asyncio
async def test_turn_coordinator_can_use_silero_for_barge_in(monkeypatch):
    cancelled = 0
    logged = []
    _speech_silero(monkeypatch)

    class FakePlayback:
        active = True

        async def stop(self, reason):
            assert reason == "barge-in"
            self.active = False
            return True

    async def cancel_backend(*_args):
        nonlocal cancelled
        cancelled += 1

    monkeypatch.setattr(
        "sleeptalk.discord_native.turns.log_latency_event",
        lambda **event: logged.append(event),
    )
    coordinator = DiscordTurnCoordinator(
        config=DiscordTurnConfig(
            vad_threshold=10_000,
            log_surface="bob-discord",
            log_persona="bob",
        ),
        playback=FakePlayback(),
        timeline=VoiceTimeline(),
        on_interrupt=cancel_backend,
        interrupt=InterruptPolicy(grace_ms=0, min_duration_ms=20),
    )
    coordinator.bot_playback_started()

    assert await coordinator.observe_pcm(SimpleNamespace(id=123), b"\x00\x00" * 2 * 960) is True
    assert cancelled == 1
    assert coordinator.stats.last_vad_mode == "silero"
    assert coordinator.stats.interruptions == 1
    detected = [event for event in logged if event["stage"] == "barge_in_detected"]
    assert detected[0]["source"] == "silero"
    assert detected[0]["surface"] == "bob-discord"
    # The gate's own record carries the same join key as the transport's.
    decisions = [event for event in logged if event["stage"] == "barge_decision"]
    assert decisions and decisions[-1]["bargeId"] == detected[0]["bargeId"]
    assert decisions[-1]["trace_id"] == detected[0]["bargeId"]


@pytest.mark.asyncio
async def test_silero_failure_is_logged_once_with_the_energy_it_fell_back_to(
    monkeypatch,
):
    """The failure itself must stay visible in telemetry.

    This asserted `is False` while the fallback did not exist: a lost model
    silently cost the session its barge-in. It now cuts (min_duration_ms is
    20 here, one frame), and what this test still guards is that the
    fallback is announced exactly once and the energy behind it is recorded.
    """
    logged = []

    class BrokenSileroGate:
        def __init__(self, _config: SileroVadConfig):
            pass

        def reset(self):
            return None

        async def observe(self, _pcm):
            raise RuntimeError("vad unavailable")

    class FakePlayback:
        active = True

        async def stop(self, _reason):
            self.active = False
            return True

    monkeypatch.setattr("sleeptalk.discord_native.turns.SileroVadGate", BrokenSileroGate)
    monkeypatch.setattr(
        "sleeptalk.discord_native.turns.log_latency_event",
        lambda **event: logged.append(event),
    )
    coordinator = DiscordTurnCoordinator(
        config=DiscordTurnConfig(
            log_surface="bob-discord",
            log_persona="bob",
        ),
        playback=FakePlayback(),
        timeline=VoiceTimeline(),
        interrupt=InterruptPolicy(grace_ms=0, min_duration_ms=20),
    )
    coordinator.bot_playback_started()
    loud_frame = (1000).to_bytes(2, "little", signed=True) * 2 * 960

    assert await coordinator.observe_pcm(SimpleNamespace(id=123), loud_frame) is True
    assert coordinator.stats.vad_failures == 1
    assert coordinator.stats.last_rms == 1000
    stages = [event["stage"] for event in logged]
    assert stages.count("barge_in_vad_fallback") == 1
    assert "barge_in_detected" in stages


@pytest.mark.asyncio
async def test_silero_failure_falls_back_to_energy_barge_in(monkeypatch):
    """Losing the model must not silently cost the session its barge-in.

    A Silero load/inference failure used to disable interruption for the rest
    of the call: the detector returned "no speech" forever and the user could
    talk over the bot indefinitely with no way to stop it. Energy alone is a
    worse detector, but it is not nothing, and the volume bar plus Floor grace
    and cooldown are what hold the bot's own echo out either way.
    """
    logged = []

    class BrokenSileroGate:
        def __init__(self, _config: SileroVadConfig):
            pass

        def reset(self):
            return None

        async def observe(self, _pcm):
            raise RuntimeError("vad unavailable")

    monkeypatch.setattr("sleeptalk.discord_native.turns.SileroVadGate", BrokenSileroGate)
    monkeypatch.setattr(
        "sleeptalk.discord_native.turns.log_latency_event",
        lambda **event: logged.append(event),
    )
    coordinator = DiscordTurnCoordinator(
        config=DiscordTurnConfig(
            duck_on_vad=False, log_surface="t", log_persona="t"
        ),
        playback=_FakePlayback(),
        timeline=VoiceTimeline(),
        interrupt=InterruptPolicy(grace_ms=0, min_duration_ms=150, min_volume=0.03),
    )
    coordinator.bot_playback_started()
    user = SimpleNamespace(id=123)
    loud = (1000).to_bytes(2, "little", signed=True) * 2 * 960

    fired = False
    for _ in range(40):  # 800ms of sustained speech
        fired = await coordinator.observe_pcm(user, loud) or fired
    assert fired is True
    assert coordinator.stats.interruptions == 1
    assert coordinator.stats.vad_failures == 1
    detected = [e for e in logged if e["stage"] == "barge_in_detected"]
    assert detected and detected[0]["source"] == "rms_fallback"


@pytest.mark.asyncio
async def test_energy_fallback_still_rejects_quiet_echo(monkeypatch):
    """The fallback keeps the volume bar. Bot echo is quiet; it must not cut."""

    class BrokenSileroGate:
        def __init__(self, _config: SileroVadConfig):
            pass

        def reset(self):
            return None

        async def observe(self, _pcm):
            raise RuntimeError("vad unavailable")

    monkeypatch.setattr("sleeptalk.discord_native.turns.SileroVadGate", BrokenSileroGate)
    coordinator = DiscordTurnCoordinator(
        # RMS 200 clears a lowered speech gate but not min_volume:
        # (200 / 32768) * 2.5 = 0.015, under 0.03.
        config=DiscordTurnConfig(vad_threshold=100, duck_on_vad=False),
        playback=_FakePlayback(),
        timeline=VoiceTimeline(),
        interrupt=InterruptPolicy(grace_ms=0, min_duration_ms=150, min_volume=0.03),
    )
    coordinator.bot_playback_started()
    user = SimpleNamespace(id=123)
    quiet_echo = (200).to_bytes(2, "little", signed=True) * 2 * 960

    for _ in range(40):
        assert await coordinator.observe_pcm(user, quiet_echo) is False
    assert coordinator.stats.interruptions == 0


@pytest.mark.asyncio
async def test_two_speakers_do_not_sum_into_one_barge_streak(monkeypatch):
    """One interruption belongs to one person.

    The streak and the Silero gate were session-wide, so alternating frames
    from two people accumulated into a single streak and fired on evidence
    neither of them produced. Each speaker alone is below the bar here, so
    nothing may fire.
    """
    speech = VadObservation(
        is_speech=True, started=True, state="SPEAKING", confidence=0.9, volume=0.5
    )
    _scripted_silero_gate(monkeypatch, [speech])
    coordinator = DiscordTurnCoordinator(
        config=DiscordTurnConfig(vad_threshold=10_000),
        playback=_FakePlayback(),
        timeline=VoiceTimeline(),
        interrupt=InterruptPolicy(grace_ms=0, min_duration_ms=100, min_volume=0.03),
    )
    coordinator.bot_playback_started()
    alice = SimpleNamespace(id=1)
    bob = SimpleNamespace(id=2)
    frame = b"\x00\x00" * 2 * 960

    # 8 frames, alternating: 80ms each, neither reaches the 100ms bar.
    for index in range(8):
        speaker = alice if index % 2 == 0 else bob
        assert await coordinator.observe_pcm(speaker, frame) is False
    assert coordinator.stats.interruptions == 0


@pytest.mark.asyncio
async def test_barge_owner_releases_after_a_dip_so_the_next_speaker_can_cut(
    monkeypatch,
):
    """Owning the attempt is not owning the floor. Going quiet hands it back."""
    quiet = VadObservation(is_speech=False, state="QUIET", confidence=0.1, volume=0.0)
    loud = VadObservation(
        is_speech=True, started=True, state="SPEAKING", confidence=0.9, volume=0.5
    )
    script = [loud, loud] + [quiet] * 8 + [loud] * 10
    _scripted_silero_gate(monkeypatch, script)
    coordinator = DiscordTurnCoordinator(
        config=DiscordTurnConfig(vad_threshold=10_000),
        playback=_FakePlayback(),
        timeline=VoiceTimeline(),
        interrupt=InterruptPolicy(grace_ms=0, min_duration_ms=100, min_volume=0.03),
    )
    coordinator.bot_playback_started()
    alice = SimpleNamespace(id=1)
    bob = SimpleNamespace(id=2)
    frame = b"\x00\x00" * 2 * 960

    for _ in range(2):  # alice starts
        assert await coordinator.observe_pcm(alice, frame) is False
    for _ in range(8):  # alice goes quiet past the dip tolerance
        assert await coordinator.observe_pcm(alice, frame) is False
    assert coordinator._gate.attempt_channel == "1"

    fired = False
    for _ in range(10):  # bob takes the attempt over from the quiet incumbent
        fired = await coordinator.observe_pcm(bob, frame) or fired
    assert fired is True
    assert coordinator._gate.attempt_channel == "2"


def test_vad_silero_barge_knobs_reach_the_live_floor(monkeypatch):
    """/vad silero wrote DiscordTurnConfig fields the barge gate never read,
    so three of its knobs were silent no-ops for the operator."""
    _quiet_silero(monkeypatch)
    coordinator = DiscordTurnCoordinator(
        config=DiscordTurnConfig(),
        playback=_FakePlayback(),
        timeline=VoiceTimeline(),
        interrupt=InterruptPolicy(min_duration_ms=150, min_volume=0.03, cooldown_ms=1200),
    )

    snap = coordinator.reconfigure(
        silero_barge_min_ms=400,
        silero_barge_min_volume=0.08,
        post_interrupt_cooldown_ms=2000,
    )

    assert coordinator._interrupt.min_duration_ms == 400
    assert coordinator._interrupt.min_volume == 0.08
    assert coordinator._interrupt.cooldown_ms == 2000
    assert coordinator._gate.policy is coordinator._interrupt
    assert snap["sileroBargeMinMs"] == 400
    assert snap["sileroBargeMinVolume"] == 0.08
    assert snap["postInterruptCooldownMs"] == 2000


@pytest.mark.asyncio
async def test_turn_coordinator_logs_playback_summary(monkeypatch):
    logged = []
    _quiet_silero(monkeypatch)

    class FakePlayback:
        active = True

        async def stop(self, _reason):
            return True

    monkeypatch.setattr(
        "sleeptalk.discord_native.turns.log_latency_event",
        lambda **event: logged.append(event),
    )
    coordinator = DiscordTurnCoordinator(
        config=DiscordTurnConfig(
            log_surface="bob-discord",
            log_persona="bob",
        ),
        playback=FakePlayback(),
        timeline=VoiceTimeline(),
        interrupt=InterruptPolicy(grace_ms=0, gap_hold_ms=600),
    )
    coordinator.bot_playback_started()
    loud_frame = (1000).to_bytes(2, "little", signed=True) * 2 * 960

    assert await coordinator.observe_pcm(SimpleNamespace(id=123), loud_frame) is False
    coordinator.bot_playback_stopped()
    if coordinator._turn_end_handle is not None:
        coordinator._turn_end_handle.cancel()
    coordinator._end_floor_turn()

    assert logged[-1]["stage"] == "barge_in_playback_summary"
    assert logged[-1]["pcmFrames"] == 1
    assert logged[-1]["maxRms"] == 1000



@pytest.mark.asyncio
async def test_floor_holds_leftover_speech_in_gap_hold(monkeypatch):
    _speech_silero(monkeypatch)
    clock = [0]
    coordinator = _open_floor(
        InterruptPolicy(grace_ms=0, gap_hold_ms=600, min_duration_ms=20),
    )
    coordinator._now_ms = lambda: clock[0]
    coordinator.bot_playback_started()
    clock[0] = 1500
    coordinator.bot_playback_stopped()
    clock[0] = 1800
    loud = (1000).to_bytes(2, "little", signed=True) * 2 * 960
    assert await coordinator.observe_pcm(SimpleNamespace(id=123), loud) is False
    assert coordinator.stats.interruptions == 0
    assert coordinator._gate.floor_state is SpeakingState.hold


@pytest.mark.asyncio
async def test_floor_fires_stop_after_grace_while_clip_plays(monkeypatch):
    _speech_silero(monkeypatch)
    clock = [0]
    coordinator = _open_floor(
        InterruptPolicy(grace_ms=400, min_duration_ms=20),
    )
    coordinator._now_ms = lambda: clock[0]
    coordinator.bot_playback_started()
    clock[0] = 800
    loud = (1000).to_bytes(2, "little", signed=True) * 2 * 960
    assert await coordinator.observe_pcm(SimpleNamespace(id=123), loud) is True
    assert coordinator.stats.interruptions == 1


@pytest.mark.asyncio
async def test_rms_does_not_cut_when_silero_is_live(monkeypatch):
    _quiet_silero(monkeypatch)
    coordinator = _open_floor(
        InterruptPolicy(grace_ms=0, min_duration_ms=20),
        vad_threshold=500,
    )
    coordinator.bot_playback_started()
    loud = (1000).to_bytes(2, "little", signed=True) * 2 * 960
    for _ in range(8):
        assert await coordinator.observe_pcm(SimpleNamespace(id=123), loud) is False
    assert coordinator.stats.last_rms == 1000
    assert coordinator.stats.interruptions == 0


def test_episode_gap_constant_is_gone():
    assert not hasattr(DiscordTurnCoordinator, "_EPISODE_GAP_SECONDS")
    assert _DISCORD_BARGE_VOLUME_GAIN == 2.5


def test_reconfigure_keeps_isolated_silero(monkeypatch):
    _quiet_silero(monkeypatch)

    class FakePlayback:
        active = False

        async def stop(self, _reason):
            return True

    coordinator = DiscordTurnCoordinator(
        config=DiscordTurnConfig(log_surface="test", log_persona="test"),
        playback=FakePlayback(),
        timeline=VoiceTimeline(),
    )
    assert coordinator._silero_enabled is True
    assert coordinator._silero_gate is not None

    snap = coordinator.reconfigure(vad_mode="rms")
    assert snap["vadMode"] == "silero"
    assert coordinator._silero_enabled is True
    assert coordinator._silero_gate is not None


def test_reconfigure_updates_silero_params(monkeypatch):
    _quiet_silero(monkeypatch)

    class FakePlayback:
        active = False

        async def stop(self, _reason):
            return True

    coordinator = DiscordTurnCoordinator(
        config=DiscordTurnConfig(log_surface="t", log_persona="t"),
        playback=FakePlayback(),
        timeline=VoiceTimeline(),
    )

    snap = coordinator.reconfigure(
        silero_confidence=0.85,
        silero_start_ms=300,
        silero_stop_ms=1200,
        silero_min_volume=0.02,
        vad_profile="aggressive",
        compare_primary="silero",
    )
    assert snap["sileroConfidence"] == 0.85
    assert snap["sileroStartMs"] == 300
    assert snap["sileroStopMs"] == 1200
    assert snap["sileroMinVolume"] == 0.02
    assert snap["vadProfile"] == "aggressive"
    assert coordinator.config.silero_confidence == 0.85

# ---------------------------------------------------------------------------
# Echo gate + cooldown tests
# ---------------------------------------------------------------------------


def _scripted_silero_gate(monkeypatch, observations):
    """Fake silero gate returning a scripted VadObservation per call."""

    class ScriptedGate:
        speaking = True

        def __init__(self, _config: SileroVadConfig):
            self._calls = 0

        def reset(self):
            return None

        async def observe(self, _pcm):
            obs = observations[min(self._calls, len(observations) - 1)]
            self._calls += 1
            return obs

    monkeypatch.setattr("sleeptalk.discord_native.turns.SileroVadGate", ScriptedGate)


class _FakePlayback:
    active = True

    async def stop(self, _reason):
        self.active = False
        return True


@pytest.mark.asyncio
async def test_silero_barge_in_rejects_quiet_confident_echo(monkeypatch):
    """Echo-like frames (high confidence, low volume) never interrupt."""
    logged = []
    echo = VadObservation(
        is_speech=True, started=True, state="SPEAKING", confidence=0.95, volume=0.01
    )
    _scripted_silero_gate(monkeypatch, [echo])
    monkeypatch.setattr(
        "sleeptalk.discord_native.turns.log_latency_event",
        lambda **event: logged.append(event),
    )
    coordinator = DiscordTurnCoordinator(
        config=DiscordTurnConfig(
            vad_threshold=10_000,
            log_surface="t",
            log_persona="t",
        ),
        playback=_FakePlayback(),
        timeline=VoiceTimeline(),
        interrupt=InterruptPolicy(grace_ms=0, min_duration_ms=100),
    )
    coordinator.bot_playback_started()
    frame = b"\x00\x00" * 2 * 960

    for _ in range(20):
        assert await coordinator.observe_pcm(SimpleNamespace(id=123), frame) is False
    assert coordinator.stats.interruptions == 0
    assert not any(e["stage"] == "barge_in_detected" for e in logged)


@pytest.mark.asyncio
async def test_isolated_gain_admits_borderline_discord_stop(monkeypatch):
    logged = []

    class VolumeFromPcmGate:
        speaking = True

        def __init__(self, _config: SileroVadConfig):
            self._calls = 0

        def reset(self):
            return None

        async def observe(self, pcm: bytes):
            volume = min(pcm_rms(pcm) / 32768.0, 1.0)
            self._calls += 1
            return VadObservation(
                is_speech=True,
                started=self._calls == 1,
                state="SPEAKING",
                confidence=0.95,
                volume=volume,
            )

    monkeypatch.setattr("sleeptalk.discord_native.turns.SileroVadGate", VolumeFromPcmGate)
    monkeypatch.setattr(
        "sleeptalk.discord_native.turns.log_latency_event",
        lambda **event: logged.append(event),
    )
    coordinator = DiscordTurnCoordinator(
        config=DiscordTurnConfig(
            vad_threshold=10_000,
            log_surface="t",
            log_persona="t",
        ),
        playback=_FakePlayback(),
        timeline=VoiceTimeline(),
        interrupt=InterruptPolicy(grace_ms=0, min_duration_ms=150, min_volume=0.03),
    )
    coordinator.bot_playback_started()
    frame = (700).to_bytes(2, "little", signed=True) * 2 * 960

    for _ in range(7):
        assert await coordinator.observe_pcm(SimpleNamespace(id=123), frame) is False
    assert await coordinator.observe_pcm(SimpleNamespace(id=123), frame) is True
    assert coordinator.stats.interruptions == 1
    assert any(e["stage"] == "barge_in_detected" for e in logged)
    assert _DISCORD_BARGE_VOLUME_GAIN == 2.5


@pytest.mark.asyncio
async def test_silero_barge_in_requires_sustained_speech(monkeypatch):
    """A single loud confident blip isn't enough; the streak must build up."""
    blip_then_quiet = [
        VadObservation(
            is_speech=True, started=True, state="SPEAKING", confidence=0.95, volume=0.5
        ),
        VadObservation(
            is_speech=False, started=False, state="QUIET", confidence=0.1, volume=0.0
        ),
    ]
    _scripted_silero_gate(monkeypatch, blip_then_quiet)
    coordinator = DiscordTurnCoordinator(
        config=DiscordTurnConfig(
            vad_threshold=10_000,
            log_surface="t",
            log_persona="t",
        ),
        playback=_FakePlayback(),
        timeline=VoiceTimeline(),
        interrupt=InterruptPolicy(grace_ms=0, min_duration_ms=100),
    )
    coordinator.bot_playback_started()
    frame = b"\x00\x00" * 2 * 960

    for _ in range(10):
        assert await coordinator.observe_pcm(SimpleNamespace(id=123), frame) is False
    assert coordinator.stats.interruptions == 0


@pytest.mark.asyncio
async def test_silero_barge_in_fires_on_sustained_loud_speech(monkeypatch):
    speech = VadObservation(
        is_speech=True, started=True, state="SPEAKING", confidence=0.95, volume=0.5
    )
    _scripted_silero_gate(monkeypatch, [speech])
    coordinator = DiscordTurnCoordinator(
        config=DiscordTurnConfig(
            vad_threshold=10_000,
            log_surface="t",
            log_persona="t",
        ),
        playback=_FakePlayback(),
        timeline=VoiceTimeline(),
        interrupt=InterruptPolicy(grace_ms=0, min_duration_ms=100),
    )
    coordinator.bot_playback_started()
    frame = b"\x00\x00" * 2 * 960

    for _ in range(4):
        assert await coordinator.observe_pcm(SimpleNamespace(id=123), frame) is False
    assert await coordinator.observe_pcm(SimpleNamespace(id=123), frame) is True
    assert coordinator.stats.interruptions == 1


@pytest.mark.asyncio
async def test_silero_barge_in_survives_confidence_dips_inside_one_utterance(
    monkeypatch,
):
    """Real speech dips in confidence frame to frame and must still interrupt.

    Production traces showed a caller talking over the bot for eight seconds
    without ever interrupting it: Silero reported hundreds of speech frames
    while the barge gate re-thresholded each one at silero_barge_confidence
    and zeroed the streak on every dip, so it never accumulated. Speech that
    Silero's own state machine calls SPEAKING, at interrupting volume, must
    build a streak even when individual frames waver.
    """
    wavering = [
        VadObservation(
            is_speech=True,
            started=index == 0,
            state="SPEAKING",
            confidence=0.95 if index % 2 == 0 else 0.60,
            volume=0.5,
        )
        for index in range(12)
    ]
    _scripted_silero_gate(monkeypatch, wavering)
    coordinator = DiscordTurnCoordinator(
        config=DiscordTurnConfig(
            vad_threshold=10_000,
            log_surface="t",
            log_persona="t",
        ),
        playback=_FakePlayback(),
        timeline=VoiceTimeline(),
        interrupt=InterruptPolicy(grace_ms=0, min_duration_ms=100, min_volume=0.03),
    )
    coordinator.bot_playback_started()
    frame = b"\x00\x00" * 2 * 960

    for _ in range(4):
        assert await coordinator.observe_pcm(SimpleNamespace(id=123), frame) is False
    assert await coordinator.observe_pcm(SimpleNamespace(id=123), frame) is True
    assert coordinator.stats.interruptions == 1


@pytest.mark.asyncio
async def test_barge_in_cooldown_suppresses_second_trigger(monkeypatch):
    """After an interrupt, a re-trigger inside the cooldown window is logged
    as suppressed instead of firing again (the echo-tail double-fire)."""
    logged = []
    speech = VadObservation(
        is_speech=True, started=True, state="SPEAKING", confidence=0.95, volume=0.5
    )
    _scripted_silero_gate(monkeypatch, [speech])
    monkeypatch.setattr(
        "sleeptalk.discord_native.turns.log_latency_event",
        lambda **event: logged.append(event),
    )
    coordinator = DiscordTurnCoordinator(
        config=DiscordTurnConfig(
            vad_threshold=10_000,
            log_surface="t",
            log_persona="t",
        ),
        playback=_FakePlayback(),
        timeline=VoiceTimeline(),
        interrupt=InterruptPolicy(grace_ms=0, min_duration_ms=20, cooldown_ms=60_000),
    )
    frame = b"\x00\x00" * 2 * 960

    coordinator.bot_playback_started()
    assert await coordinator.observe_pcm(SimpleNamespace(id=123), frame) is True
    assert coordinator.stats.interruptions == 1

    coordinator.bot_playback_stopped()
    coordinator.bot_playback_started()
    assert await coordinator.observe_pcm(SimpleNamespace(id=123), frame) is True
    assert coordinator.stats.interruptions == 1

    suppressed = [e for e in logged if e["stage"] == "barge_in_suppressed"]
    assert len(suppressed) == 1
    assert suppressed[0]["reason"] == "cooldown"


@pytest.mark.asyncio
async def test_clip_gap_holds_leftover_until_next_audio(monkeypatch):
    _speech_silero(monkeypatch)
    clock = [0]
    coordinator = _open_floor(
        InterruptPolicy(grace_ms=400, gap_hold_ms=600, min_duration_ms=20),
    )
    coordinator._now_ms = lambda: clock[0]
    loud_frame = (1000).to_bytes(2, "little", signed=True) * 2 * 960
    user = SimpleNamespace(id=123, name="Alice")

    coordinator.bot_playback_started()
    clock[0] = 200
    assert await coordinator.observe_pcm(user, loud_frame) is False
    clock[0] = 500
    coordinator.bot_playback_stopped()
    clock[0] = 800
    assert await coordinator.observe_pcm(user, loud_frame) is False
    assert coordinator.stats.interruptions == 0
    coordinator.bot_playback_started()
    clock[0] = 1300
    assert await coordinator.observe_pcm(user, loud_frame) is True
    assert coordinator.stats.interruptions == 1


@pytest.mark.asyncio
async def test_floor_turn_ends_after_gap_hold(monkeypatch):
    _speech_silero(monkeypatch)
    coordinator = _open_floor(
        InterruptPolicy(grace_ms=10_000, gap_hold_ms=600, min_duration_ms=20),
    )
    loud_frame = (1000).to_bytes(2, "little", signed=True) * 2 * 960
    user = SimpleNamespace(id=123, name="Alice")

    coordinator.bot_playback_started()
    coordinator.bot_playback_stopped()
    assert coordinator._turn_end_handle is not None
    coordinator._turn_end_handle.cancel()
    coordinator._end_floor_turn()
    assert coordinator._turn_end_handle is None
    assert coordinator._gate.floor_state is SpeakingState.idle

    coordinator.bot_playback_started()
    assert await coordinator.observe_pcm(user, loud_frame) is False
    assert coordinator.stats.interruptions == 0


@pytest.mark.asyncio
async def test_duck_on_vad_pauses_then_resumes_on_blip(monkeypatch):
    _quiet_silero(monkeypatch)

    class FakePlayback:
        active = True
        paused = 0
        resumed = 0

        async def pause(self, reason):
            self.paused += 1
            return True

        async def resume(self, reason):
            self.resumed += 1
            return True

        async def stop(self, reason):
            raise AssertionError("blip must not escalate to barge-in stop")

    playback = FakePlayback()
    coordinator = DiscordTurnCoordinator(
        config=DiscordTurnConfig(
            vad_threshold=500,
            duck_min_ms=200,
            duck_release_ms=600,
        ),
        playback=playback,
        timeline=VoiceTimeline(),
        interrupt=InterruptPolicy(mode="off"),
    )
    coordinator.bot_playback_started()
    loud = (1000).to_bytes(2, "little", signed=True) * 2 * 960  # 20ms, rms=1000
    quiet = b"\x00\x00" * 2 * 960
    user = SimpleNamespace(id=123)

    for _ in range(9):  # 180ms of speech: not yet
        await coordinator.observe_pcm(user, loud)
    assert playback.paused == 0
    await coordinator.observe_pcm(user, loud)  # 200ms: duck
    assert playback.paused == 1
    for _ in range(29):  # 580ms quiet: still ducked
        await coordinator.observe_pcm(user, quiet)
    assert playback.resumed == 0
    await coordinator.observe_pcm(user, quiet)  # 600ms: release
    assert playback.resumed == 1
    assert coordinator.stats.interruptions == 0


@pytest.mark.asyncio
async def test_duck_yields_to_real_barge_in(monkeypatch):
    stopped = []

    class FakePlayback:
        active = True

        async def pause(self, reason):
            return True

        async def resume(self, reason):
            return True

        async def stop(self, reason):
            stopped.append(reason)
            self.active = False
            return True

    _speech_silero(monkeypatch)
    coordinator = DiscordTurnCoordinator(
        config=DiscordTurnConfig(
            vad_threshold=500,
            duck_min_ms=200,
        ),
        playback=FakePlayback(),
        timeline=VoiceTimeline(),
        interrupt=InterruptPolicy(grace_ms=0, min_duration_ms=400),
    )
    coordinator.bot_playback_started()
    loud = (1000).to_bytes(2, "little", signed=True) * 2 * 960
    user = SimpleNamespace(id=123)

    fired = False
    for _ in range(25):
        fired = await coordinator.observe_pcm(user, loud) or fired
    assert fired
    assert stopped == ["barge-in"]
    assert coordinator._gate.ducked is False


@pytest.mark.asyncio
async def test_duck_disabled_by_config(monkeypatch):
    _quiet_silero(monkeypatch)

    class FakePlayback:
        active = True

        async def pause(self, reason):
            raise AssertionError("duck_on_vad=False must never pause")

        async def stop(self, reason):
            return True

    coordinator = DiscordTurnCoordinator(
        config=DiscordTurnConfig(
            vad_threshold=500,
            duck_on_vad=False,
        ),
        playback=FakePlayback(),
        timeline=VoiceTimeline(),
        interrupt=InterruptPolicy(mode="off"),
    )
    coordinator.bot_playback_started()
    loud = (1000).to_bytes(2, "little", signed=True) * 2 * 960
    for _ in range(30):
        await coordinator.observe_pcm(SimpleNamespace(id=123), loud)


@pytest.mark.asyncio
async def test_cooldown_suppression_logs_once_per_window_not_once_per_frame(
    monkeypatch,
):
    """log_latency_event writes synchronously on the audio thread.

    A four-minute live session wrote 261 barge_in_suppressed records, one per
    frame held by the cooldown. The first frame still reports immediately with
    live VAD values; the rest are counted and summarised when cooldown ends.
    """
    logged = []
    speech = VadObservation(
        is_speech=True, started=True, state="SPEAKING", confidence=0.95, volume=0.5
    )
    _scripted_silero_gate(monkeypatch, [speech])
    monkeypatch.setattr(
        "sleeptalk.discord_native.turns.log_latency_event",
        lambda **event: logged.append(event),
    )
    coordinator = DiscordTurnCoordinator(
        config=DiscordTurnConfig(vad_threshold=10_000, log_surface="t", log_persona="t"),
        playback=_FakePlayback(),
        timeline=VoiceTimeline(),
        interrupt=InterruptPolicy(grace_ms=0, min_duration_ms=20, cooldown_ms=1000),
    )
    clock = [0]
    coordinator._now_ms = lambda: clock[0]
    frame = b"\x00\x00" * 2 * 960
    user = SimpleNamespace(id=123)

    coordinator.bot_playback_started()
    assert await coordinator.observe_pcm(user, frame) is True
    assert coordinator.stats.interruptions == 1

    for _ in range(10):  # held by cooldown, one frame at a time
        clock[0] += 20
        assert await coordinator.observe_pcm(user, frame) is True

    opened = [e for e in logged if e.get("reason") == "cooldown"]
    assert len(opened) == 1, f"one record per window, got {len(opened)}"

    clock[0] = 5000  # cooldown expires
    await coordinator.observe_pcm(user, frame)

    closed = [e for e in logged if e.get("reason") == "cooldown_ended"]
    assert len(closed) == 1
    assert closed[0]["frames"] == 10
