"""Local/Cartesia browser responsiveness: early audio and interruption safeguards."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sleeptalk.config import PolicyConfig, SleeptalkConfig
from sleeptalk.interrupt import AcousticKind, Floor, InterruptPolicy
from sleeptalk.pause_ahead import configured_pause_ms
from sleeptalk.policy import VoicePolicy, local_responsiveness_enabled
from sleeptalk.streaming import LOCAL_PHRASE_STREAMING, SentenceStreamer
from sleeptalk.voice.configs import AdaptiveEndpointingConfig, TurnCompletionSignal
from sleeptalk.voice_loop.utterance import UtteranceAssembler
from tests.test_utterance_gate_hold import Clock, FRAME


def local_config(**updates):
    values = {
        "policy": {},
        "transport": {"type": "web"},
        "stt": {"provider": "local", "model": "moonshine-tiny-en"},
        "tts": {"provider": "local", "model": "kokoro-en"},
    }
    values.update(updates)
    return SleeptalkConfig.model_validate(values)


@pytest.mark.parametrize("updates", [
    {"transport": {"type": "null"}}, {"route": {"type": "hybrid"}},
])
def test_responsiveness_is_scoped_to_live_cascade(updates):
    assert local_responsiveness_enabled(local_config())
    assert not local_responsiveness_enabled(local_config(**updates))
    assert not VoicePolicy.from_config(PolicyConfig()).local_responsiveness


@pytest.mark.parametrize("stt", ["local", "cartesia", "elevenlabs", "deepgram", "openrouter", "venice", "groq", "together"])
@pytest.mark.parametrize("tts", ["local", "cartesia", "elevenlabs", "deepgram", "openrouter", "venice",
                                "groq", "together", "playht", "playai", "rime", "hume", "hume-octave"])
@pytest.mark.parametrize("fallback", [[], [{"provider": "local"}], [{"provider": "cartesia"}],
                                      [{"provider": "elevenlabs"}], [{"provider": "deepgram"}],
                                      [{"provider": "openrouter"}], [{"provider": "venice"}]])
def test_responsiveness_supports_local_and_hosted_combinations(stt, tts, fallback):
    config = local_config(stt={"provider": stt}, tts={"provider": tts, "fallback": fallback})
    assert local_responsiveness_enabled(config)


def test_speaking_only_fallbacks_do_not_disable_browser_responsiveness():
    assert local_responsiveness_enabled(local_config(
        stt={"provider": "groq"},
        tts={"provider": "hume", "fallback": [{"provider": "playai"}, {"provider": "rime"}]},
    ))


@pytest.mark.parametrize("updates", [
    {"transport": {"type": "null"}},
    {"route": {"type": "hybrid"}},
])
def test_cartesia_preserves_transport_and_route_gates(updates):
    values = {"stt": {"provider": "cartesia"}, "tts": {"provider": "cartesia"}}
    values.update(updates)
    assert not local_responsiveness_enabled(local_config(**values))


@pytest.mark.parametrize("updates", [
    {"transport": {"type": "null"}},
    {"route": {"type": "hybrid"}},
])
def test_deepgram_preserves_transport_and_route_gates(updates):
    values = {"stt": {"provider": "deepgram"}, "tts": {"provider": "deepgram"}}
    values.update(updates)
    assert not local_responsiveness_enabled(local_config(**values))


@pytest.mark.parametrize("old_flag", [True, False])
def test_retired_toggle_is_accepted_but_cannot_disable_standard_behavior(old_flag):
    config = local_config(policy={"experimental_responsiveness": old_flag})
    assert local_responsiveness_enabled(config)
    assert "experimental_responsiveness" not in config.policy.model_dump()
    assert "local_responsiveness" not in config.policy.model_dump()


def test_settings_no_longer_exposes_or_writes_the_retired_toggle(tmp_path):
    from sleeptalk.settings_page import apply_setting, describe_profile
    path = tmp_path / "profile.toml"
    path.write_text('[transport]\ntype = "web"\n[stt]\nprovider = "local"\n[tts]\nprovider = "local"\n[policy]\nexperimental_responsiveness = false\n')
    state = describe_profile(path)
    assert "experimental_responsiveness" not in state["behaviour"]
    with pytest.raises(ValueError):
        apply_setting(path, "policy.experimental_responsiveness", True)


@pytest.mark.parametrize("enabled,isolated,expected", [
    (True, True, True), (False, True, False), (True, False, False),
])
def test_only_local_isolated_audio_removes_recurring_grace(enabled, isolated, expected):
    floor = Floor(InterruptPolicy(local_responsiveness=enabled),
                  AcousticKind.isolated if isolated else AcousticKind.mixed)
    floor.observe(0, "turn_start")
    floor.observe(0, "audio_start")
    floor.observe(1000, "drain")
    floor.observe(1100, "audio_start")
    assert floor.observe(1150, "mic_speech").fire is expected


def test_earlier_pause_preserves_explicit_disable_and_baseline():
    for enabled, configured, expected in ((False, 300, 300), (True, 300, 220), (True, 0, 0)):
        session = SimpleNamespace(policy=VoicePolicy(local_responsiveness=enabled),
                                  config=SimpleNamespace(stt=SimpleNamespace(pause_ahead_ms=configured)))
        assert configured_pause_ms(session) == expected


def phrase_streamer(enabled):
    token = LOCAL_PHRASE_STREAMING.set(enabled)
    try:
        return SentenceStreamer()
    finally:
        LOCAL_PHRASE_STREAMING.reset(token)


def test_phrase_streaming_is_task_local_and_preserves_all_words():
    text = ('The first thing to check is whether the browser can hear your voice clearly '
            'and whether the response starts quickly enough for a natural conversation')
    old, local = SentenceStreamer(), phrase_streamer(True)
    assert old.feed(text) == []
    early = local.feed(text)
    assert early and len(early[0]) <= 64
    assert ' '.join([*early, *local.feed('.'), local.flush()]).strip() == text + '.'
    assert old.feed('.') == [text + '.']
    assert not LOCAL_PHRASE_STREAMING.get()


def test_phrases_keep_code_fences_hidden_across_deltas():
    stream = phrase_streamer(True)
    pieces = []
    for delta in ('This explanation will start speaking before the entire answer is ready ',
                  '```python\nsecret = 42\n', '```\nThe rest stays readable.'):
        pieces.extend(stream.feed(delta))
    pieces.append(stream.flush())
    assert 'secret' not in ' '.join(pieces)
    assert 'The rest stays readable.' in ' '.join(pieces)


def assembler(clock, scorer, *, enabled=True, signal=None):
    events, delivered = [], []
    async def receive(_participant, pcm):
        delivered.append(pcm)
    result = UtteranceAssembler(
        loop=clock, min_bytes=1, silence_seconds=0.4,
        should_ignore_audio=lambda: False, on_utterance=receive,
        speech_rms_threshold=100, smart_turn_scorer=scorer,
        endpointing_config=AdaptiveEndpointingConfig(local_responsiveness=enabled),
        turn_completion_provider=lambda _: signal or TurnCompletionSignal(transcript="Can you explain this"),
        on_smart_turn_event=lambda _, event: events.append(event),
    )
    return result, events, delivered


async def test_high_confidence_early_semantics_commits_before_the_old_timeout():
    clock = Clock(asyncio.get_running_loop())
    score = SimpleNamespace(threshold=0.6, score=AsyncMock(return_value=0.95))
    capture, events, delivered = assembler(clock, score)
    capture.observe(1, object(), FRAME)
    clock.now += 0.26
    capture._start_semantic_check(1)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert delivered == [FRAME]
    assert events[0]["flushed"] and events[0]["bucketReason"] == "local_early_pause"
    capture._cleanup_in_loop()


@pytest.mark.parametrize("replacement", [False, True])
async def test_late_semantic_result_cannot_cut_resumed_or_replaced_speech(replacement):
    clock = Clock(asyncio.get_running_loop())
    entered, release = asyncio.Event(), asyncio.Event()
    async def score(_):
        entered.set()
        await release.wait()
        return 0.99
    capture, events, delivered = assembler(clock, SimpleNamespace(threshold=0.6, score=score))
    capture.observe(1, object(), FRAME)
    clock.now += 0.26
    capture._start_semantic_check(1)
    await entered.wait()
    if replacement:
        # Simulate replacement while an uncooperative native inference completes.
        capture._open_buffer(1)
    clock.now += 0.1
    capture.observe(1, object(), FRAME)
    release.set()
    await asyncio.sleep(0)
    assert not delivered and 1 in capture.buffers
    assert events[-1]["stale"] and not events[-1]["flushed"]
    capture._cleanup_in_loop()


async def test_floor_holder_and_low_confidence_keep_normal_endpoint_fallback():
    clock = Clock(asyncio.get_running_loop())
    scorer = SimpleNamespace(threshold=0.6, score=AsyncMock(return_value=0.7))
    for signal in (TurnCompletionSignal(transcript="and um"), None):
        capture, events, delivered = assembler(clock, scorer, signal=signal)
        capture.observe(1, object(), FRAME)
        clock.now += 0.26
        capture._start_semantic_check(1)
        await asyncio.sleep(0)
        assert not delivered
        if signal is not None:
            scorer.score.assert_not_awaited()
        else:
            assert not events[-1]["flushed"]
        capture._cleanup_in_loop()


async def test_baseline_schedules_no_early_semantics_and_cleanup_cancels_local_scoring():
    clock = Clock(asyncio.get_running_loop())
    scorer = SimpleNamespace(threshold=0.6, score=AsyncMock(return_value=0.99))
    capture, _, _ = assembler(clock, scorer, enabled=False)
    capture.observe(1, object(), FRAME)
    assert not capture._semantic_handles and not capture._semantic_tasks
    capture._cleanup_in_loop()
    capture, _, _ = assembler(clock, scorer)
    capture.observe(1, object(), FRAME)
    assert capture._semantic_handles
    capture._cleanup_in_loop()
    assert not capture._semantic_handles and not capture._semantic_tasks


@pytest.mark.parametrize("stt,tts,enabled", [
    ("groq", "cartesia", True),
    ("local", "local", True),
    ("local", "cartesia", True),
    ("cartesia", "cartesia", True),
    ("elevenlabs", "elevenlabs", True),
    ("local", "elevenlabs", True),
    ("deepgram", "deepgram", True),
    ("deepgram", "cartesia", True),
    ("openrouter", "openrouter", True),
    ("venice", "venice", True),
    ("together", "hume", True),
    ("groq", "rime", True),
    ("cartesia", "playht", True),
])
async def test_cascade_can_speak_before_the_agent_finishes_the_first_sentence(stt, tts, enabled):
    from sleeptalk.routes.base import RouteInput
    from sleeptalk.routes.cascade import CascadeRoute
    from tests.test_cascade_streaming import context, ImmediateTTS, RecordingOutput
    prefix = 'The first thing we should check is whether the local microphone captures every word '
    waiting, release, heard = asyncio.Event(), asyncio.Event(), asyncio.Event()
    class Agent:
        async def respond_streaming(self, text):
            stream = SentenceStreamer()
            for part in stream.feed(prefix):
                yield part
            waiting.set()
            await release.wait()
            for part in stream.feed('correctly.'):
                yield part
        async def abort(self):
            pass
    class Output(RecordingOutput):
        async def write(self, pcm, turn_id):
            await super().write(pcm, turn_id)
            heard.set()
    config = local_config(stt={"provider": stt}, tts={"provider": tts})
    policy = VoicePolicy(local_responsiveness=local_responsiveness_enabled(config), instant_ack=False,
                         fillers_enabled=False, keepalive_interval_s=0)
    route = CascadeRoute(agent=Agent(), tts=ImmediateTTS(), audio_output=Output())
    await route.prepare(SimpleNamespace(policy=policy, config=None))
    ctx = context([])
    ctx.extras['suppress_preamble'] = True
    turn = asyncio.create_task(route.run_turn(RouteInput(text='Explain the test'), ctx))
    try:
        await asyncio.wait_for(waiting.wait(), 2)
        if enabled:
            await asyncio.wait_for(heard.wait(), 2)
        else:
            assert not heard.is_set()
        release.set()
        outcome = await asyncio.wait_for(turn, 2)
        assert outcome.status == 'completed'
        assert ' '.join(outcome.generated) == prefix + 'correctly.'
        assert not LOCAL_PHRASE_STREAMING.get()
    finally:
        release.set()
        if not turn.done():
            turn.cancel()
        await asyncio.gather(turn, return_exceptions=True)
        await route.close()


@pytest.mark.parametrize('mode', ['immediate', 'off'])
@pytest.mark.parametrize('stt,tts', [('local', 'local'), ('local', 'cartesia'), ('cartesia', 'cartesia'),
                                   ('elevenlabs', 'elevenlabs'), ('deepgram', 'deepgram'), ('openrouter', 'openrouter'), ('venice', 'venice')])
async def test_browser_ducks_then_resumes_a_blip_without_cancelling(mode, stt, tts, monkeypatch):
    from sleeptalk.policy import InterruptPolicyValues
    from sleeptalk.web_voice.session import WebCallSession
    from tests.runtime.test_web_call import FakeStream, FakeKernelSession
    monkeypatch.setattr('sleeptalk.web_voice.session.log_latency_event', lambda **_: None)
    config = local_config(stt={"provider": stt}, tts={"provider": tts})
    kernel = FakeKernelSession(VoicePolicy(local_responsiveness=local_responsiveness_enabled(config),
                               interrupt=InterruptPolicyValues(mode=mode)))
    web = WebCallSession(session=kernel, stream=FakeStream(), call_sid='local')
    try:
        web._clock_ms = 0
        web.playback.active = True
        web._on_playout_audio_start()
        web._clock_ms = 500
        for _ in range(2):
            await web._offer_detector_speech(True, 40, source='test', rms=3000,
                                             confidence=0.9, volume=0.1)
        assert web._gate.ducked is (mode != 'off')
        assert web.playback._resume_event.is_set() is (mode == 'off')
        assert kernel.aborts == 0
        for _ in range(15):
            await web._offer_detector_speech(False, 40, source='test', rms=0,
                                             confidence=0.0, volume=0.0)
        assert web.playback._resume_event.is_set()
        assert kernel.aborts == 0
    finally:
        await web.close()


async def test_browser_counts_fresh_speech_not_vad_hangover(monkeypatch):
    from sleeptalk.web_voice.session import WebCallSession
    from sleeptalk.voice_loop.vad import VadObservation
    from tests.runtime.test_web_call import FakeStream, FakeKernelSession, ScriptedBargeVad
    monkeypatch.setattr('sleeptalk.web_voice.session.log_latency_event', lambda **_: None)
    vad = ScriptedBargeVad(VadObservation(True, state='SPEAKING', confidence=0.9, volume=0.1))
    monkeypatch.setattr('sleeptalk.web_voice.session.SileroVadGate', lambda _: vad)
    kernel = FakeKernelSession(VoicePolicy(local_responsiveness=True))
    web = WebCallSession(session=kernel, stream=FakeStream(), call_sid='local')
    try:
        web._clock_ms = 0
        web.playback.active = True
        web._on_playout_audio_start()
        web._clock_ms = 500
        for _ in range(4):
            await web._watch_web_barge_in(bytes(3840))
        vad.observation = VadObservation(True, state='STOPPING', confidence=0.01, volume=0.1)
        for _ in range(10):
            await web._watch_web_barge_in(bytes(3840))
        assert kernel.aborts == 0
    finally:
        await web.close()
