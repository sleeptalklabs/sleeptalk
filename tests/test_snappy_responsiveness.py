"""Snappy spends less time waiting without committing speculative user text."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sleeptalk.policy import VoicePolicy
from sleeptalk.streaming import LOCAL_PHRASE_STREAMING, SNAPPY_PHRASE_STREAMING, SentenceStreamer
from sleeptalk.voice.configs import TurnCompletionSignal
from sleeptalk.voice_loop.endpointing import decide_endpoint
from tests.test_local_endpoint_deadlines import Clock, PCM, capture


@pytest.mark.parametrize("preset,expected", [("aggressive", 180), ("relaxed", 850), ("balanced", 650)])
@pytest.mark.parametrize("custom", [False, True])
def test_profile_preset_replaces_inherited_timing_but_keeps_explicit_controls(tmp_path, preset, expected, custom):
    from sleeptalk.config import load_resolved_config

    root = tmp_path / "sleeptalk.toml"
    root.write_text('[policy.endpointing_overrides]\nsemantic_complete_silence_ms = 650\n'
                    'incomplete_silence_ms = 3500\nenabled = false\ncompletion_threshold = 0.9\n')
    profile = tmp_path / "profiles" / "test.toml"
    profile.parent.mkdir()
    profile.write_text(f'[policy]\nendpointing = "{preset}"\n' + (
        '[policy.endpointing_overrides]\nsemantic_complete_silence_ms = 420\n' if custom else ''))
    before = root.read_bytes(), profile.read_bytes()
    config = load_resolved_config(profile, check_env=False, check_binaries=False, verify_binding=False)
    effective = VoicePolicy.from_config(config.policy).endpointing_config()
    assert effective.semantic_complete_silence_ms == (420 if custom else expected)
    assert not effective.enabled and effective.completion_threshold == 0.9
    assert before == (root.read_bytes(), profile.read_bytes())


@pytest.mark.parametrize("preset", ["aggressive", "balanced"])
async def test_snappy_keeps_canned_openings_out_of_the_answer_lane(preset):
    from sleeptalk.routes.base import RouteInput
    from sleeptalk.routes.cascade import CascadeRoute
    from sleeptalk.voice_policy_runtime import VoicePolicyTimings
    from tests.test_cascade_streaming import context, ImmediateTTS, RecordingOutput

    waiting, release = asyncio.Event(), asyncio.Event()
    class Agent:
        async def respond_streaming(self, text):
            waiting.set()
            await release.wait()
            yield "Here is the answer."
        async def abort(self):
            pass
    selected = policy(preset)
    selected.instant_ack = True
    selected.preamble_pools = {"ack_action": ("Okay, let me check that.",)}
    output = RecordingOutput()
    route = CascadeRoute(agent=Agent(), tts=ImmediateTTS(), audio_output=output,
                         policy_timings=VoicePolicyTimings(ack_delay_seconds=0, sentence_grace_seconds=0))
    await route.prepare(SimpleNamespace(policy=selected, config=None))
    task = asyncio.create_task(route.run_turn(RouteInput(text="What is this repository about?"), context([])))
    try:
        await asyncio.wait_for(waiting.wait(), 2)
        # All controller delays are zero: let their ready callbacks run.
        for _ in range(20):
            await asyncio.sleep(0)
        if preset == "aggressive":
            assert output.writes == []
            assert route._policy_runtime.instant_ack_mode == "off"
            assert route._policy_runtime.next_opening_lines() == ()
        else:
            assert output.writes
        release.set()
        outcome = await asyncio.wait_for(task, 2)
        assert outcome.status == "completed"
        assert output.writes[-1][0] == b"Here is the answer."
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await route.close()


@pytest.mark.parametrize("preset,duck_ms,pause_ms", [("aggressive", 60, 100), ("balanced", 200, 160)])
def test_snappy_yields_early_and_soft_backchannels_still_resume(preset, duck_ms, pause_ms):
    from sleeptalk.barge import BargeAction, BargeCalibration, BargeEvidence, BargeGate
    from sleeptalk.discord_native.turns import interrupt_from_voice_policy
    from sleeptalk.interrupt import AcousticKind
    from sleeptalk.policy import InterruptPolicyValues

    selected = policy(preset)
    selected.interrupt = InterruptPolicyValues(mode="soft")
    configured = interrupt_from_voice_policy(selected)
    now = [500]
    gate = BargeGate(configured, AcousticKind.isolated, clock=lambda: now[0],
                     calibration=BargeCalibration(supports_resume=True, supports_duck=True))
    gate.turn_started()
    gate.audio_started()
    now[0] = 1000
    actions = []
    for n in range(1, pause_ms // 20 + 1):
        actions.append(gate.observe(BargeEvidence(channel="user", duration_ms=20, level=0.1,
                                                  voiced=True, confidence=0.9)).action)
    assert actions[-1] == BargeAction.pause
    assert BargeAction.pause not in actions[:-1]
    if preset == "aggressive":
        assert actions[duck_ms // 20 - 1] == BargeAction.duck
    assert gate.resolve(backchannel=True).action == BargeAction.resume
    # Retuning cannot accidentally reset Snappy's caps or enable a disabled mic.
    gate.retune(replace(configured, mode="off"))
    assert gate.policy.mode == "off"
    assert gate.observe(BargeEvidence(duration_ms=1000, level=1, voiced=True)).action == BargeAction.none
    mixed = BargeGate(configured, AcousticKind.mixed)
    assert mixed.policy == configured  # speaker echo retains the original protection


def test_snappy_duck_blip_releases_without_cancelling():
    from sleeptalk.barge import BargeAction, BargeCalibration, BargeEvidence, BargeGate
    from sleeptalk.interrupt import AcousticKind, InterruptPolicy

    gate = BargeGate(InterruptPolicy(local_responsiveness=True, snappy_responsiveness=True),
                     AcousticKind.isolated, clock=lambda: 500,
                     calibration=BargeCalibration(supports_duck=True))
    gate.turn_started()
    gate.audio_started()
    assert gate.observe(BargeEvidence(duration_ms=60, level=0.1, voiced=True)).action == BargeAction.duck
    assert gate.observe(BargeEvidence(duration_ms=240, level=0, voiced=False)).action == BargeAction.none
    assert gate.observe(BargeEvidence(duration_ms=20, level=0, voiced=False)).action == BargeAction.resume


@pytest.mark.parametrize("preset,expected_ms", [("aggressive", 100), ("balanced", 150)])
async def test_web_terminal_and_discord_pass_the_selected_interrupt_policy(preset, expected_ms, monkeypatch):
    from sleeptalk.discord_native.turns import interrupt_from_voice_policy
    from sleeptalk.interrupt import AcousticKind
    from sleeptalk.transports.terminal import TerminalTransport
    from sleeptalk.web_voice.session import WebCallSession
    from tests.runtime.test_web_call import FakeStream, FakeKernelSession
    from tests.test_surface_transcripts import session_for

    monkeypatch.setattr('sleeptalk.web_voice.session.log_latency_event', lambda **_: None)
    selected = policy(preset)
    web = WebCallSession(session=FakeKernelSession(selected), stream=FakeStream(), call_sid='snappy')
    session = session_for()
    session.policy = selected
    terminal = TerminalTransport(session.config, {})
    try:
        for configured in (web._gate.policy, terminal._interrupt_policy(session),
                           interrupt_from_voice_policy(selected)):
            assert configured.for_acoustics(AcousticKind.isolated).min_duration_ms == expected_ms
    finally:
        await web.close()


def policy(preset):
    return VoicePolicy(endpointing=preset, local_responsiveness=True,
                       instant_ack=False, fillers_enabled=False, keepalive_interval_s=0)


@pytest.mark.parametrize("preset,first_ms,retry_ms", [
    ("aggressive", 100, 200), ("balanced", 250, 500), ("relaxed", 250, 500),
    ("batch", 250, 500),
])
async def test_semantic_checks_follow_preset_and_keep_confidence(preset, first_ms, retry_ms):
    clock = Clock()
    scorer = SimpleNamespace(threshold=0.6, score=AsyncMock(side_effect=[0.7, 0.95]))
    assembler, _, delivered, _, scores = capture(clock, scorer=scorer, cartesia=True)
    assembler.endpointing_config = policy(preset).endpointing_config()
    assembler.turn_completion_provider = lambda _: TurnCompletionSignal(
        transcript="Tell me more", source="cartesia",
    )
    try:
        await assembler._observe_gated(0, object(), PCM)
        await clock.advance(first_ms / 1000 - 0.01)
        assert not scorer.score.await_count
        await clock.advance(0.02)
        assert scorer.score.await_count == 1 and not delivered
        await clock.advance((retry_ms - first_ms) / 1000)
        assert scorer.score.await_count == 2 and len(delivered) == 1
        assert scores[-1]["silenceMs"] == retry_ms
        assert scores[-1]["threshold"] == 0.8
    finally:
        assembler._cleanup_in_loop()


@pytest.mark.parametrize("preset,expected", [("aggressive", 220), ("balanced", 600), ("relaxed", 600)])
def test_moonshine_completion_cap_is_snappier_only_when_selected(preset, expected):
    config = replace(policy(preset).endpointing_config(), local_streaming_input=True)
    decision = decide_endpoint(
        signal=TurnCompletionSignal(transcript="Tell me more", source="moonshine_streaming", is_final=True),
        speech_duration_ms=2000, silence_ms=expected, base_silence_ms=120, config=config,
    )
    assert decision.threshold_ms == expected and decision.should_endpoint
    held = decide_endpoint(
        signal=TurnCompletionSignal(transcript="and um", source="moonshine_streaming", is_final=True),
        speech_duration_ms=2000, silence_ms=expected, base_silence_ms=120, config=config,
    )
    assert not held.should_endpoint and held.reason == "floor_holder"


async def test_snappy_resumed_speech_invalidates_the_first_score():
    clock = Clock()
    entered, release = asyncio.Event(), asyncio.Event()
    async def score(_):
        entered.set()
        await release.wait()
        return 0.99
    assembler, _, delivered, _, scores = capture(
        clock, scorer=SimpleNamespace(threshold=0.6, score=score), cartesia=True,
    )
    assembler.endpointing_config = policy("aggressive").endpointing_config()
    try:
        await assembler._observe_gated(0, object(), PCM)
        await clock.advance(0.16)
        assert entered.is_set()
        await assembler._observe_gated(0, object(), PCM)
        release.set()
        for _ in range(4):
            await asyncio.sleep(0)
        assert not delivered
        assert scores[-1]["stale"] and not scores[-1]["flushed"]
    finally:
        assembler._cleanup_in_loop()


def test_balanced_batch_tuning_does_not_select_snappy_output_or_new_timing():
    selected = policy("balanced")
    assert selected.tune_for_batch_listener()
    assert selected.endpointing == "batch" and not selected.snappy_responsiveness
    assert selected.endpointing_config().semantic_complete_silence_ms == 450
    assert selected.endpointing_config().semantic_check_ms == 250


@pytest.mark.parametrize("preset,expected_ms", [("aggressive", 100), ("balanced", 250)])
async def test_confident_pause_commits_at_the_selected_early_deadline(preset, expected_ms):
    clock = Clock()
    scorer = SimpleNamespace(threshold=0.6, score=AsyncMock(return_value=0.95))
    assembler, _, delivered, _, _ = capture(clock, scorer=scorer, cartesia=True)
    assembler.endpointing_config = policy(preset).endpointing_config()
    try:
        await assembler._observe_gated(0, object(), PCM)
        await clock.advance(0.27)
        assert len(delivered) == 1
        assert (delivered[0][0] - 100) * 1000 == pytest.approx(expected_ms)
    finally:
        assembler._cleanup_in_loop()


def test_small_snappy_phrases_keep_words_urls_and_hidden_code_intact():
    local_token = LOCAL_PHRASE_STREAMING.set(True)
    snappy_token = SNAPPY_PHRASE_STREAMING.set(True)
    try:
        streamer = SentenceStreamer()
        text = ("We can start by checking the microphone settings and then visiting "
                "https://example.com/a/very/long/path/for/troubleshooting to find the guide. "
                "```python\nsecret_function()\n```\nThen we can test the sound again.")
        chunks = []
        for char in text:
            chunks.extend(streamer.feed(char))
        chunks.append(streamer.flush())
        spoken = " ".join(chunks).strip()
        assert "secret_function" not in spoken and "```" not in spoken
        assert any("https://example.com/a/very/long/path/for/troubleshooting" in chunk for chunk in chunks)
        assert "checking the microphone settings" in spoken
        assert "Then we can test the sound again." in spoken
        assert len(chunks) >= 3
    finally:
        SNAPPY_PHRASE_STREAMING.reset(snappy_token)
        LOCAL_PHRASE_STREAMING.reset(local_token)


@pytest.mark.parametrize("preset", ["aggressive", "balanced", "relaxed", "batch"])
async def test_snappy_audio_starts_with_a_smaller_opening_and_context_does_not_leak(preset):
    from sleeptalk.routes.base import RouteInput
    from sleeptalk.routes.cascade import CascadeRoute
    from tests.test_cascade_streaming import context, ImmediateTTS, RecordingOutput

    prefix = "We can start by checking the microphone "
    waiting, release, heard = asyncio.Event(), asyncio.Event(), asyncio.Event()
    class Agent:
        async def respond_streaming(self, text):
            streamer = SentenceStreamer()
            for part in streamer.feed(prefix):
                yield part
            waiting.set()
            await release.wait()
            for part in streamer.feed("settings."):
                yield part
            tail = streamer.flush()
            if tail:
                yield tail
        async def abort(self):
            pass
    class Output(RecordingOutput):
        async def write(self, pcm, turn_id):
            await super().write(pcm, turn_id)
            heard.set()
    route = CascadeRoute(agent=Agent(), tts=ImmediateTTS(), audio_output=Output())
    await route.prepare(SimpleNamespace(policy=policy(preset), config=None))
    ctx = context([])
    ctx.extras["suppress_preamble"] = True
    task = asyncio.create_task(route.run_turn(RouteInput(text="Help me"), ctx))
    try:
        await asyncio.wait_for(waiting.wait(), 2)
        if preset == "aggressive":
            await asyncio.wait_for(heard.wait(), 2)
        else:
            assert not heard.is_set()
        assert not LOCAL_PHRASE_STREAMING.get() and not SNAPPY_PHRASE_STREAMING.get()
        release.set()
        outcome = await asyncio.wait_for(task, 2)
        assert outcome.status == "completed"
        assert " ".join(outcome.generated) == prefix + "settings."
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await route.close()
