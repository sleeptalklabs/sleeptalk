"""P1/P2: hybrid & compare capture VAD, and grace-period abort.

Hybrid/compare tests drive UtteranceAssembler directly with scripted Silero
gates; the grace-abort test drives PiDiscordLiveSession.handle_utterance on a
namespace, the same shape as the other characterization tests.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from sleeptalk.voice_loop.utterance import UtteranceAssembler
from sleeptalk.voice_loop.vad import VadObservation

LOUD = (3000).to_bytes(2, "little", signed=True) * 960
QUIET = b"\x00\x00" * 960


class _ScriptedGate:
    def __init__(self, observations):
        self._observations = observations

    async def observe(self, _pcm):
        return self._observations.pop(0) if len(self._observations) > 1 else self._observations[0]


def _assembler(*, mode, gate, on_utterance, compare_primary="silero", on_compare=None):
    loop = asyncio.get_running_loop()
    return UtteranceAssembler(
        loop=loop,
        min_bytes=1,
        silence_seconds=30,
        on_utterance=on_utterance,
        should_ignore_audio=lambda: False,
        speech_rms_threshold=500,
        vad_mode=mode,
        compare_primary=compare_primary,
        silero_vad_factory=lambda _config: gate,
        on_vad_compare_event=on_compare,
    )


async def _settle(steps: int = 4) -> None:
    for _ in range(steps):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_hybrid_capture_buffers_rms_only_speech():
    """Quiet/rambly speech the RMS gate would drop is Silero's job; the
    reverse — energetic speech Silero is unsure about — must still be
    captured: hybrid is the UNION of both legs."""
    utterances: list[bytes] = []

    async def on_utterance(_p, pcm):
        utterances.append(pcm)

    gate = _ScriptedGate([VadObservation(is_speech=False, state="QUIET")])
    asm = _assembler(mode="hybrid", gate=gate, on_utterance=on_utterance)

    asm.observe("u", "participant", LOUD)
    await _settle()
    asm.flush("u")
    await _settle()

    assert utterances == [LOUD]


@pytest.mark.asyncio
async def test_hybrid_capture_buffers_silero_only_speech():
    """Low-energy speech below the RMS floor must still open a buffer when
    Silero hears it — the original cut-off bug hybrid exists to fix."""
    utterances: list[bytes] = []

    async def on_utterance(_p, pcm):
        utterances.append(pcm)

    gate = _ScriptedGate([VadObservation(is_speech=True, started=True, state="SPEAKING")])
    asm = _assembler(mode="hybrid", gate=gate, on_utterance=on_utterance)

    asm.observe("u", "participant", QUIET)
    await _settle()
    asm.flush("u")
    await _settle()

    assert utterances == [QUIET]


@pytest.mark.asyncio
async def test_hybrid_silero_stop_does_not_flush_while_rms_still_loud():
    """In hybrid, Silero's stop only ends the utterance when the RMS leg
    also went quiet — energetic trailing speech survives a model STOP."""
    utterances: list[bytes] = []

    async def on_utterance(_p, pcm):
        utterances.append(pcm)

    gate = _ScriptedGate(
        [
            VadObservation(is_speech=True, started=True, state="SPEAKING"),
            VadObservation(is_speech=False, stopped=True, state="QUIET"),
        ]
    )
    asm = _assembler(mode="hybrid", gate=gate, on_utterance=on_utterance)

    asm.observe("u", "participant", QUIET)  # silero starts
    await _settle()
    asm.observe("u", "participant", LOUD)  # silero stops, RMS still loud
    await _settle()
    assert utterances == []  # no premature flush
    asm.flush("u")
    await _settle()
    assert utterances == [QUIET + LOUD]


@pytest.mark.asyncio
async def test_open_capture_preserves_vad_rejected_tail_for_fallback():
    """VAD controls the speech clock, not which decoded audio survives."""
    utterances: list[bytes] = []

    async def on_utterance(_p, pcm):
        utterances.append(pcm)

    rejected_tail = (100).to_bytes(2, "little", signed=True) * 960
    gate = _ScriptedGate(
        [
            VadObservation(is_speech=True, started=True, state="SPEAKING"),
            VadObservation(is_speech=False, state="QUIET"),
        ]
    )
    asm = _assembler(mode="hybrid", gate=gate, on_utterance=on_utterance)

    asm.observe("u", "participant", LOUD)
    await _settle()
    asm.observe("u", "participant", rejected_tail)
    await _settle()
    asm.flush("u")
    await _settle()

    assert utterances == [LOUD + rejected_tail]


@pytest.mark.asyncio
async def test_compare_mode_acts_on_primary_and_logs_agreement():
    """Compare mode runs both legs, logs transitions, but only the primary
    leg drives capture — here primary=rms, so Silero-only speech is logged
    but NOT captured."""
    utterances: list[bytes] = []
    events: list[dict] = []

    async def on_utterance(_p, pcm):
        utterances.append(pcm)

    gate = _ScriptedGate([VadObservation(is_speech=True, started=True, state="SPEAKING")])
    asm = _assembler(
        mode="compare",
        gate=gate,
        on_utterance=on_utterance,
        compare_primary="rms",
        on_compare=lambda pid, event: events.append(event),
    )

    asm.observe("u", "participant", QUIET)  # silero-only speech: logged, dropped
    await _settle()
    asm.flush("u")
    await _settle()
    assert utterances == []
    assert asm.compare_stats["silero_only_frames"] == 1
    assert events and events[0]["sileroSpeech"] is True
    assert events[0]["rmsSpeech"] is False

    asm.observe("u", "participant", LOUD)  # both legs agree: captured
    await _settle()
    asm.flush("u")
    await _settle()
    assert utterances == [LOUD]
    assert asm.compare_stats["agree_frames"] == 1


@pytest.mark.asyncio
async def test_endpoint_floor_is_not_padded_by_silero_stop_ms():
    """The endpoint floor is the configured silence window in every VAD
    mode. vad_stop_ms is Silero's speech hangover; folding it into the
    floor forced every endpoint decision to wait >=900ms once the Silero
    leg drove (the post-hybrid-default lag regression)."""
    from sleeptalk.voice_loop.vad import SileroVadConfig

    gate = _ScriptedGate([VadObservation(is_speech=True, started=True, state="SPEAKING")])
    loop = asyncio.get_running_loop()
    asm = UtteranceAssembler(
        loop=loop,
        min_bytes=1,
        silence_seconds=0.45,
        on_utterance=lambda p, pcm: asyncio.sleep(0),
        should_ignore_audio=lambda: False,
        vad_mode="hybrid",
        silero_vad_config=SileroVadConfig(stop_ms=900),
        silero_vad_factory=lambda _config: gate,
    )
    assert asm._base_flush_delay_seconds() == 0.45


# --- P5 smart-turn accelerator ------------------------------------------------


def _uncertain_assembler(*, scorer, on_utterance, events=None):
    """RMS-mode assembler whose endpoint decision lands in the 'uncertain'
    bucket (heuristic score 0.66 < completion_threshold 0.72)."""
    from sleeptalk.voice_loop.endpointing import AdaptiveEndpointingConfig, TurnCompletionSignal

    loop = asyncio.get_running_loop()
    return UtteranceAssembler(
        loop=loop,
        min_bytes=1,
        silence_seconds=30,
        on_utterance=on_utterance,
        should_ignore_audio=lambda: False,
        speech_rms_threshold=500,
        vad_mode="rms",
        endpointing_config=AdaptiveEndpointingConfig(enabled=True),
        turn_completion_provider=lambda pid: TurnCompletionSignal(
            transcript="let me think about that one", source="deepgram"
        ),
        smart_turn_scorer=scorer,
        on_smart_turn_event=(lambda pid, event: events.append(event)) if events is not None else None,
    )


@pytest.mark.asyncio
async def test_smart_turn_accelerates_uncertain_bucket():
    """Heuristic says 'uncertain' (1.8s wait); Smart Turn hears a completed
    thought (0.9) -> flush NOW instead of after the full bucket wait."""
    utterances: list[bytes] = []
    events: list[dict] = []

    async def on_utterance(_p, pcm):
        utterances.append(pcm)

    scorer = SimpleNamespace(threshold=0.6, score=lambda pcm: asyncio.sleep(0, result=0.9))
    asm = _uncertain_assembler(scorer=scorer, on_utterance=on_utterance, events=events)

    asm.observe("u", "participant", LOUD)
    asm._maybe_flush("u")  # heuristic: uncertain, no endpoint
    assert utterances == []
    await _settle()
    assert utterances == [LOUD]  # smart turn flushed it
    assert events and events[0]["flushed"] is True
    assert events[0]["bucketReason"] == "uncertain"


@pytest.mark.asyncio
async def test_smart_turn_no_does_not_flush():
    """Model hears an unfinished thought (0.2): utterance stays open, the
    normal bucket timer continues — no interruption."""
    utterances: list[bytes] = []
    events: list[dict] = []

    async def on_utterance(_p, pcm):
        utterances.append(pcm)

    scorer = SimpleNamespace(threshold=0.6, score=lambda pcm: asyncio.sleep(0, result=0.2))
    asm = _uncertain_assembler(scorer=scorer, on_utterance=on_utterance, events=events)

    asm.observe("u", "participant", LOUD)
    asm._maybe_flush("u")
    await _settle()
    assert utterances == []
    assert events and events[0]["flushed"] is False


@pytest.mark.asyncio
async def test_smart_turn_never_overrides_floor_holder_veto():
    """Trailing floor-holder ('and...') is certain knowledge from the text
    side — the model is not even consulted."""
    from sleeptalk.voice_loop.endpointing import AdaptiveEndpointingConfig, TurnCompletionSignal

    calls: list[bytes] = []

    async def fake_score(pcm):
        calls.append(pcm)
        return 0.99

    loop = asyncio.get_running_loop()
    asm = UtteranceAssembler(
        loop=loop,
        min_bytes=1,
        silence_seconds=30,
        on_utterance=lambda p, pcm: asyncio.sleep(0),
        should_ignore_audio=lambda: False,
        speech_rms_threshold=500,
        vad_mode="rms",
        endpointing_config=AdaptiveEndpointingConfig(enabled=True),
        turn_completion_provider=lambda pid: TurnCompletionSignal(
            transcript="I was thinking we could, and", source="deepgram"
        ),
        smart_turn_scorer=SimpleNamespace(threshold=0.6, score=fake_score),
    )

    asm.observe("u", "participant", LOUD)
    asm._maybe_flush("u")
    await _settle()
    assert calls == []  # veto held: scorer never ran


# --- P2.3 grace-period abort -------------------------------------------------

from sleeptalk.session import RealtimeSession
from sleeptalk.voice_policy_runtime import TranscriptionResult
from sleeptalk.voice_loop.lifecycle import LifecycleSupervisor  # noqa: E402
from sleeptalk.voice_loop.telemetry import VoiceLatencyTrace  # noqa: E402
from sleeptalk.discord_voice import PiDiscordLiveSession  # noqa: E402
from sleeptalk.discord.access import DiscordAccess

OPEN_ACCESS = DiscordAccess(required=False)


@pytest.mark.asyncio
async def test_grace_abort_cancels_fragment_turn_and_merges_transcript():
    """Speech resuming within the grace window of a commit means the
    endpoint fired mid-thought: the in-flight turn (answering the fragment)
    is cancelled and the merged transcript is re-asked as one turn."""
    user = SimpleNamespace(id=7, display_name="U", bot=False)
    lock = asyncio.Lock()
    await lock.acquire()

    async def release_lock():
        await asyncio.sleep(0.05)
        lock.release()

    releaser = asyncio.create_task(release_lock())

    class AgentRec:
        def __init__(self):
            self.abort_calls = 0

        async def abort(self):
            self.abort_calls += 1

    agent = AgentRec()
    speak_task = asyncio.create_task(asyncio.sleep(60))
    spoken_prompts: list[str] = []
    committed_at = time.monotonic() - 2.0

    async def fake_stream_and_speak(prompt, trace, *, suppress_preamble=False, ack_already_played=False):
        spoken_prompts.append(prompt)
        return "answer"

    settings = SimpleNamespace(
        discord_transcript_debug=False,
        discord_voice_text_echo=False,
        voice_chat_log_enabled=False,
        voice_chat_log_path="",
    )
    session = SimpleNamespace(
        _lifecycle=LifecycleSupervisor(),
        _barge_listen=set(),
        _resume_offer_task=None,
        turn_lock=lock,
        _pending_fragments={},
        _transcribe_for_status=lambda pcm: asyncio.sleep(0, result="and the rest"),
        _in_flight_prompts={7: "tell me about the"},
        _last_utterance_committed_at={7: committed_at},
        # Speech began 500ms after commit but took longer than the 750ms grace
        # to finish. The onset, not flush time, decides whether it is a merge.
        _grace_continuation_started_at={7: committed_at + 0.5},
        _live_turn={"answer_queued": asyncio.Event()},  # still working, no answer
        _soft_barge_pending=False,
        _speak_tasks=[speak_task],
        _interrupted_context=None,
        _resume_offered=False,
        _pending_cron_offer=False,
        _turn_generation=0,
        bot=SimpleNamespace(
            access=OPEN_ACCESS,
            realtime_session=RealtimeSession(SimpleNamespace()),
            agent=agent,
            settings=settings,
            surface="test",
            persona="test",
            profile=SimpleNamespace(slug="test", name="Test"),
            policy=SimpleNamespace(
                resume_grace_ms=750,
                fragment_hold_ms=0,
                instant_ack=False,
                capture=SimpleNamespace(silence_ms=450),
            ),
        ),
        text_channel=None,
        voice=None,
        bot_speaking=False,
        _new_trace=lambda u, pcm: VoiceLatencyTrace(
            surface="test", transport="test", persona="test"
        ),
        _maybe_hold_fragment=lambda u, t, tr, ack=None: asyncio.sleep(0, result=t),
        _stream_and_speak=fake_stream_and_speak,
        _maybe_offer_resume=lambda tr: asyncio.sleep(0),
    )

    session._handle_utterance = (
        lambda utterance_user, utterance_pcm: PiDiscordLiveSession._handle_utterance(
            session, utterance_user, utterance_pcm
        )
    )

    async def cancel_active_turn(*, owner=None):
        speak_task.cancel()
        await agent.abort()

    session.bot.cancel_active_turn = cancel_active_turn
    await PiDiscordLiveSession.handle_utterance(session, user, LOUD)
    await releaser

    assert speak_task.cancelled()
    assert agent.abort_calls == 1
    assert spoken_prompts == ["tell me about the and the rest"]
    assert session._turn_generation == 1


def test_collect_during_turn_keeps_grace_buffer_open_until_flush():
    now = time.monotonic()
    session = SimpleNamespace(
        _barge_listen=set(),
        _pending_fragments={},
        _last_utterance_committed_at={7: now},
        _live_turn={"answer_queued": asyncio.Event()},
        bot=SimpleNamespace(policy=SimpleNamespace(resume_grace_ms=750)),
        sink=SimpleNamespace(buffers={}),
    )

    assert PiDiscordLiveSession._collect_during_turn(session, 7) is True

    # Once VAD opened the continuation buffer, expiration of the initial
    # grace window must not chop the remainder of that same sentence.
    session._last_utterance_committed_at[7] = now - 5
    session.sink.buffers[7] = bytearray(b"speech")
    assert PiDiscordLiveSession._collect_during_turn(session, 7) is True


@pytest.mark.asyncio
async def test_discord_does_not_start_parallel_blind_ack(monkeypatch):
    """Discord delegates blind acknowledgements to the shared policy."""
    from pathlib import Path

    user = SimpleNamespace(id=7, display_name="U", bot=False)
    played: list[object] = []
    speak_kwargs: list[dict] = []

    async def fake_play_file(clip):
        played.append(clip)
        return SimpleNamespace(cancelled=False)

    async def fake_stream_and_speak(prompt, trace, *, suppress_preamble=False, ack_already_played=False):
        speak_kwargs.append({"ack_already_played": ack_already_played})
        return "answer"

    monkeypatch.setattr(
        "sleeptalk.session.RealtimeSession.transcribe",
        lambda *args, **kw: asyncio.sleep(0, result=TranscriptionResult("hello there", "streaming", {})),
    )

    settings = SimpleNamespace(
        discord_transcript_debug=False,
        discord_voice_text_echo=False,
        voice_chat_log_enabled=False,
        voice_chat_log_path="",
    )
    session = SimpleNamespace(
        _lifecycle=LifecycleSupervisor(),
        _barge_listen=set(),
        _resume_offer_task=None,
        turn_lock=asyncio.Lock(),
        _pending_fragments={},
        _in_flight_prompts={},
        _last_utterance_committed_at={},
        _live_turn=None,
        _soft_barge_pending=False,
        _speak_tasks=[],
        _interrupted_context=None,
        _resume_offered=False,
        _pending_cron_offer=False,
        _turn_generation=0,
        playback=SimpleNamespace(active=False, play_file=fake_play_file),
        streaming_stt=None,
        bot=SimpleNamespace(
            access=OPEN_ACCESS,
            realtime_session=RealtimeSession(SimpleNamespace()),
            stt=None,
            stt_rest_timeout_seconds=8.0,
            settings=settings,
            surface="test",
            persona="test",
            profile=SimpleNamespace(slug="test", name="Test"),
            preamble_clips={"ack": [Path("ack-1.wav")]},
            _preamble_rotation={},
            policy=SimpleNamespace(
                resume_grace_ms=750,
                fragment_hold_ms=0,
                instant_ack=True,
                instant_ack_mode="blind",
                capture=SimpleNamespace(silence_ms=450),
            ),
        ),
        text_channel=None,
        voice=None,
        bot_speaking=False,
        _new_trace=lambda u, pcm: VoiceLatencyTrace(
            surface="test", transport="test", persona="test"
        ),
        _maybe_hold_fragment=lambda u, t, tr, ack=None: asyncio.sleep(0, result=t),
        _stream_and_speak=fake_stream_and_speak,
        _maybe_offer_resume=lambda tr: asyncio.sleep(0),
    )

    session._handle_utterance = (
        lambda utterance_user, utterance_pcm: PiDiscordLiveSession._handle_utterance(
            session, utterance_user, utterance_pcm
        )
    )
    await PiDiscordLiveSession.handle_utterance(session, user, LOUD)
    await asyncio.sleep(0)

    assert played == []  # ack fired at commit, pre-agent
    assert speak_kwargs == [{"ack_already_played": False}]  # runner skips tier-1


@pytest.mark.asyncio
async def test_grace_abort_does_not_fire_once_answer_is_playing():
    """Once answer audio is queued, a new utterance is an interruption for
    the barge-in path — not a mid-thought continuation. No merge."""
    user = SimpleNamespace(id=7, display_name="U", bot=False)
    lock = asyncio.Lock()
    await lock.acquire()

    async def release_lock():
        await asyncio.sleep(0.05)
        lock.release()

    releaser = asyncio.create_task(release_lock())

    class AgentRec:
        def __init__(self):
            self.abort_calls = 0

        async def abort(self):
            self.abort_calls += 1

    answer_queued = asyncio.Event()
    answer_queued.set()  # answer already playing
    agent = AgentRec()
    session = SimpleNamespace(
        _lifecycle=LifecycleSupervisor(),
        _barge_listen=set(),
        _resume_offer_task=None,
        turn_lock=lock,
        _pending_fragments={},
        _transcribe_for_status=lambda pcm: asyncio.sleep(0, result="a brand new topic"),
        _in_flight_prompts={7: "tell me about the"},
        _last_utterance_committed_at={7: time.monotonic()},
        _live_turn={"answer_queued": answer_queued},
        _soft_barge_pending=False,
        _speak_tasks=[],
        voice=None,
        bot=SimpleNamespace(
            access=OPEN_ACCESS,
            realtime_session=RealtimeSession(SimpleNamespace()),
            agent=agent,
            policy=SimpleNamespace(resume_grace_ms=750),
        ),
    )

    # Answer playing + no soft-barge pending: utterance drops, nothing merged.
    session._handle_utterance = (
        lambda utterance_user, utterance_pcm: PiDiscordLiveSession._handle_utterance(
            session, utterance_user, utterance_pcm
        )
    )
    await PiDiscordLiveSession.handle_utterance(session, user, LOUD)
    await releaser

    assert agent.abort_calls == 0
