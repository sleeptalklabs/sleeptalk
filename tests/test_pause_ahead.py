"""Early batch transcription at a pause.

The early transcript may only stand in for the committed utterance when it
heard the same audio plus nothing but quiet. It never ends a turn.
"""

from __future__ import annotations

import asyncio
import math
import struct
from types import SimpleNamespace
from typing import Any

import pytest

from sleeptalk.pause_ahead import PauseAheadTranscriber
from sleeptalk.voice_loop.utterance import UtteranceAssembler
from sleeptalk.voice_policy_runtime import VoicePolicyRuntime

RATE = 48_000


def tone(ms: int, amplitude: int = 6000) -> bytes:
    count = RATE * ms // 1000
    return struct.pack(
        f"<{count}h",
        *(int(amplitude * math.sin(2 * math.pi * 220 * index / RATE)) for index in range(count)),
    )


def hush(ms: int, amplitude: int = 0) -> bytes:
    return tone(ms, amplitude) if amplitude else b"\x00\x00" * (RATE * ms // 1000)


SENTENCE = tone(800) + hush(300)


class Batch:
    """A batch STT that records every upload."""

    def __init__(self) -> None:
        self.uploads: list[bytes] = []
        self.purposes: list[str] = []

    async def transcribe_pcm(self, pcm: bytes, *, purpose: str = "") -> str:
        self.uploads.append(pcm)
        self.purposes.append(purpose)
        return f"heard {len(pcm)}"


def runtime(stt: Batch, *, pause_ms: int = 300, streaming: Any = None) -> VoicePolicyRuntime:
    config = SimpleNamespace(stt=SimpleNamespace(pause_ahead_ms=pause_ms, rest_timeout_seconds=8.0))
    return VoicePolicyRuntime(agent=object(), stt=stt, streaming_stt=streaming, config=config)


@pytest.mark.asyncio
async def test_endpoint_silence_after_the_pause_uses_the_early_transcript() -> None:
    stt = Batch()
    voice = runtime(stt)
    assert voice.transcribe_ahead(SENTENCE)
    await asyncio.sleep(0)

    result = await voice.transcribe(SENTENCE + hush(600))

    assert result.source == "pause_ahead"
    assert result.text == f"heard {len(SENTENCE)}"
    assert stt.uploads == [SENTENCE]
    assert stt.purposes == ["pause_ahead"]
    assert result.statistics["pauseAhead"] == "hit"
    await voice.close()


@pytest.mark.asyncio
async def test_speech_after_the_pause_transcribes_the_whole_utterance() -> None:
    stt = Batch()
    voice = runtime(stt)
    voice.transcribe_ahead(SENTENCE)
    committed = SENTENCE + tone(400) + hush(600)

    result = await voice.transcribe(committed)

    assert result.source == "rest"
    assert result.text == f"heard {len(committed)}"
    assert result.statistics["pauseAhead"] == "sound_after_pause"
    await voice.close()


@pytest.mark.asyncio
async def test_a_soft_trailing_word_is_not_lost() -> None:
    """A quiet word after the pause, far below speech level, still counts."""
    stt = Batch()
    voice = runtime(stt)
    voice.transcribe_ahead(SENTENCE)
    committed = SENTENCE + tone(250, amplitude=400) + hush(600)

    result = await voice.transcribe(committed)

    assert result.source == "rest"
    assert stt.uploads[-1] == committed
    await voice.close()


@pytest.mark.asyncio
async def test_room_noise_matching_the_pause_still_counts_as_quiet() -> None:
    noisy_pause = tone(800) + hush(300, amplitude=60)
    stt = Batch()
    voice = runtime(stt)
    voice.transcribe_ahead(noisy_pause)

    result = await voice.transcribe(noisy_pause + hush(600, amplitude=60))

    assert result.source == "pause_ahead"
    await voice.close()


@pytest.mark.asyncio
async def test_a_different_utterance_never_borrows_the_early_transcript() -> None:
    stt = Batch()
    voice = runtime(stt)
    voice.transcribe_ahead(SENTENCE)
    other = tone(800, amplitude=3000) + hush(900)

    result = await voice.transcribe(other)

    assert result.source == "rest"
    assert result.statistics["pauseAhead"] == "different_audio"
    await voice.close()


def test_trimmed_commit_shorter_than_the_snapshot_still_matches() -> None:
    async def scenario() -> None:
        stt = Batch()
        transcriber = PauseAheadTranscriber(stt.transcribe_pcm)
        transcriber.start(0, SENTENCE)
        take = transcriber.take(0, SENTENCE[: len(SENTENCE) - len(hush(150))])
        assert take.outcome == "hit"
        assert await take.task == f"heard {len(SENTENCE)}"

    asyncio.run(scenario())


@pytest.mark.asyncio
async def test_a_failed_early_call_falls_back_to_the_normal_transcription() -> None:
    class Flaky(Batch):
        async def transcribe_pcm(self, pcm: bytes, *, purpose: str = "") -> str:
            if purpose == "pause_ahead":
                raise RuntimeError("provider hiccup")
            return await super().transcribe_pcm(pcm, purpose=purpose)

    stt = Flaky()
    voice = runtime(stt)
    voice.transcribe_ahead(SENTENCE)

    result = await voice.transcribe(SENTENCE + hush(600))

    assert result.source == "rest"
    assert result.statistics["pauseAhead"] == "failed"
    await voice.close()


def test_streaming_stt_or_zero_turns_it_off() -> None:
    assert not runtime(Batch(), streaming=object()).transcribe_ahead(SENTENCE)
    assert not runtime(Batch(), pause_ms=0).transcribe_ahead(SENTENCE)


FRAME = b"\x40\x10" * 480


class Timers:
    """The assembler's loop with timers the test fires by hand."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self.now = loop.time()
        self.pending: list[tuple[float, Any, tuple[Any, ...], asyncio.TimerHandle]] = []

    def time(self) -> float:
        return self.now

    def call_later(self, delay: float, callback: Any, *args: Any) -> asyncio.TimerHandle:
        handle = self._loop.call_later(3600, lambda: None)
        self.pending.append((delay, callback, args, handle))
        return handle

    def fire(self, name: str) -> None:
        for _delay, callback, args, handle in list(self.pending):
            if getattr(callback, "__name__", "") == name and not handle.cancelled():
                handle.cancel()
                callback(*args)

    def is_closed(self) -> bool:
        return False


@pytest.mark.asyncio
async def test_the_pause_report_never_ends_the_utterance() -> None:
    timers = Timers(asyncio.get_running_loop())
    delivered: list[bytes] = []
    paused: list[bytes] = []

    async def on_utterance(_participant: object, pcm: bytes) -> None:
        delivered.append(pcm)

    assembler = UtteranceAssembler(
        loop=timers,
        min_bytes=1,
        silence_seconds=0.4,
        on_utterance=on_utterance,
        should_ignore_audio=lambda: False,
        on_pause=lambda _pid, pcm: paused.append(pcm),
        pause_ms=300,
    )
    assembler.observe(0, object(), FRAME)
    assert [delay for delay, callback, *_ in timers.pending if callback.__name__ == "_report_pause"] == [0.3]

    timers.fire("_report_pause")
    assert paused == [FRAME]
    assert delivered == [] and 0 in assembler.buffers

    # Re-armed with no new speech: nothing new to report.
    assembler._schedule_endpoint_check(0)
    timers.fire("_report_pause")
    assert paused == [FRAME]

    assembler.observe(0, object(), FRAME)
    timers.fire("_report_pause")
    assert paused == [FRAME, FRAME + FRAME]
    assembler._cleanup_in_loop()
    assert not assembler.pause_handles


@pytest.mark.parametrize("provider", ["openrouter", "groq", "together", "venice", "elevenlabs", "deepgram", "cartesia", "local"])
def test_every_listening_provider_accepts_the_setting(provider) -> None:
    """[stt] keys survive a provider switch on the settings page, so every
    provider takes pause_ahead_ms; streaming ones simply don't use it."""
    from sleeptalk.config import SleeptalkConfig, _validate_component_choices
    from sleeptalk.defaults import register_defaults

    register_defaults()
    config = SleeptalkConfig.model_validate({
        "agent": {"type": "pi"}, "transport": {"type": "web"},
        "stt": {"provider": provider, "pause_ahead_ms": 0},
        "tts": {"provider": "cartesia"},
    })
    assert config.stt.pause_ahead_ms == 0
    problems = [p for p in _validate_component_choices(config, env={}) if "pause_ahead_ms" in p]
    assert problems == []


@pytest.mark.asyncio
async def test_silero_wind_down_frames_do_not_hold_the_pause_off() -> None:
    """Silero keeps calling its wind-down "speech" and the turn ends when it
    stops; a 20 ms frame that completes no Silero window repeats the old state
    with no confidence. Neither may restart the pause, or it never fires first."""
    from sleeptalk.voice_loop.vad import VadObservation

    observations = iter([
        VadObservation(is_speech=True, started=True, state="SPEAKING", confidence=0.9),
        VadObservation(is_speech=True, state="SPEAKING", confidence=0.0),   # no window completed
        VadObservation(is_speech=True, state="STOPPING", confidence=0.1),   # wind-down
        VadObservation(is_speech=True, state="SPEAKING", confidence=0.8),   # voice again
    ])

    class Gate:
        async def observe(self, pcm):
            return next(observations)

    timers = Timers(asyncio.get_running_loop())

    async def on_utterance(_participant, _pcm):
        return None

    assembler = UtteranceAssembler(
        loop=timers, min_bytes=1, silence_seconds=0.4, on_utterance=on_utterance,
        should_ignore_audio=lambda: False, vad_mode="silero", silero_vad_factory=lambda _config: Gate(),
        on_pause=lambda _pid, _pcm: None, pause_ms=300,
    )

    def armed() -> int:
        return sum(1 for _d, callback, _a, _h in timers.pending if callback.__name__ == "_report_pause")

    for expected in (1, 1, 1, 2):
        await assembler._observe_gated(0, object(), FRAME)
        assert armed() == expected
    assembler._cleanup_in_loop()


def _shadow_assembler(timers, events):
    async def on_utterance(_participant, _pcm):
        return None

    return UtteranceAssembler(
        loop=timers, min_bytes=1, silence_seconds=0.4, on_utterance=on_utterance,
        should_ignore_audio=lambda: False, on_capture_event=lambda _pid, event: events.append(event),
    )


@pytest.mark.asyncio
async def test_early_end_shadow_logs_how_much_sooner_a_finished_question_could_end() -> None:
    timers = Timers(asyncio.get_running_loop())
    events: list[dict] = []
    assembler = _shadow_assembler(timers, events)
    assembler.observe(0, object(), FRAME)
    text = asyncio.get_running_loop().create_future()
    assembler.watch_early_transcript(0, text)
    timers.now += 0.6
    text.set_result("What's the status of the deploy?")
    await asyncio.sleep(0)
    timers.now += 0.4  # endpointing ends the turn a full second after the voice
    assembler.flush(0)
    shadow = next(e for e in events if e["kind"] == "early_end_shadow")
    assert shadow["reason"] == "complete" and shadow["wouldCut"] == 0
    assert shadow["textReadyMs"] == 600 and shadow["actualEndMs"] == 1000
    assert shadow["savedMs"] == 1000 - shadow["wouldEndMs"] > 0
    assembler._cleanup_in_loop()


@pytest.mark.asyncio
async def test_early_end_shadow_logs_a_voice_that_came_back_after_the_would_be_end() -> None:
    timers = Timers(asyncio.get_running_loop())
    events: list[dict] = []
    assembler = _shadow_assembler(timers, events)
    assembler.observe(0, object(), FRAME)
    text = asyncio.get_running_loop().create_future()
    assembler.watch_early_transcript(0, text)
    timers.now += 0.6
    text.set_result("Open the settings page.")
    await asyncio.sleep(0)
    timers.now += 1.5  # well past any complete-bucket threshold: then they carry on
    assembler.observe(0, object(), FRAME)
    timers.now += 1.0
    assembler.flush(0)
    shadow = next(e for e in events if e["kind"] == "early_end_shadow")
    assert shadow["wouldCut"] == 1 and shadow["wouldCutPastGrace"] == 1
    assert "savedMs" not in shadow  # nothing arrived after the resumed speech
    assembler._cleanup_in_loop()


@pytest.mark.asyncio
async def test_text_that_lands_after_new_speech_is_not_judged() -> None:
    timers = Timers(asyncio.get_running_loop())
    events: list[dict] = []
    assembler = _shadow_assembler(timers, events)
    assembler.observe(0, object(), FRAME)
    text = asyncio.get_running_loop().create_future()
    assembler.watch_early_transcript(0, text)
    timers.now += 0.3
    assembler.observe(0, object(), FRAME)  # they spoke again first
    text.set_result("So I was thinking")
    await asyncio.sleep(0)
    assembler.flush(0)
    assert not [e for e in events if e["kind"] == "early_end_shadow"]
    assembler._cleanup_in_loop()


@pytest.mark.asyncio
async def test_speech_start_reopens_idle_connections() -> None:
    from sleeptalk.pause_ahead import warm_on_speech

    warmed = asyncio.Event()

    class Session:
        async def keep_warm(self):
            warmed.set()

    warm_on_speech(Session())
    await asyncio.wait_for(warmed.wait(), 1)
    warm_on_speech(object())  # a session without it is left alone


def test_preroll_keeps_the_moment_before_the_vad_opened_a_turn() -> None:
    async def scenario():
        timers = Timers(asyncio.get_running_loop())

        async def on_utterance(_p, _pcm):
            return None

        quiet = b"\x01\x00" * 1920  # 20 ms stereo at 48 kHz, below the floor
        assembler = UtteranceAssembler(loop=timers, min_bytes=1, silence_seconds=0.4, on_utterance=on_utterance,
                                       should_ignore_audio=lambda: gate["shut"], speech_rms_threshold=500,
                                       preroll_ms=40)
        for _ in range(5):
            assembler.observe(0, object(), quiet)
        assembler.observe(0, object(), FRAME)
        # The last 40 ms of quiet, then the voice.
        assert bytes(assembler.buffers[0]) == quiet * 2 + FRAME
        assembler._cleanup_in_loop()

        # Audio from before an ignored stretch is never used as pre-roll.
        assembler.observe(0, object(), quiet)
        gate["shut"] = True
        assembler.observe(0, object(), quiet)
        gate["shut"] = False
        assembler.observe(0, object(), FRAME)
        assert bytes(assembler.buffers[0]) == FRAME
        assembler._cleanup_in_loop()

    gate = {"shut": False}
    asyncio.run(scenario())


@pytest.mark.asyncio
async def test_a_false_start_becomes_preroll_for_the_turn_that_follows() -> None:
    """"Can you | check the logs": the VAD drops "Can you" as a false start at the
    dip, and the turn it opens next began at "check". The dropped audio is
    kept, so a batch transcriber still gets the first words."""
    from sleeptalk.voice_loop.vad import VadObservation

    observations = iter([
        VadObservation(is_speech=True, state="STARTING", confidence=0.9),   # "Can"
        VadObservation(is_speech=True, state="STARTING", confidence=0.9),   # "you"
        VadObservation(is_speech=False, state="QUIET", confidence=0.1),     # the dip: false start
        VadObservation(is_speech=True, started=True, state="SPEAKING", confidence=0.9),  # "check"
    ])

    class Gate:
        async def observe(self, pcm):
            return next(observations)

    timers = Timers(asyncio.get_running_loop())

    async def on_utterance(_p, _pcm):
        return None

    assembler = UtteranceAssembler(loop=timers, min_bytes=1, silence_seconds=0.4, on_utterance=on_utterance,
                                   should_ignore_audio=lambda: False, vad_mode="silero",
                                   silero_vad_factory=lambda _c: Gate(), preroll_ms=500)
    can, you, dip, check = (bytes([n, 16]) * 1920 for n in (1, 2, 3, 4))
    for frame in (can, you, dip, check):
        await assembler._observe_gated(0, object(), frame)
    assert bytes(assembler.buffers[0]) == can + you + dip + check
    assembler._cleanup_in_loop()


def test_a_too_short_utterance_becomes_preroll_for_the_next() -> None:
    async def scenario():
        timers = Timers(asyncio.get_running_loop())

        async def on_utterance(_p, _pcm):
            delivered.append(_pcm)

        assembler = UtteranceAssembler(loop=timers, min_bytes=len(FRAME) * 3, silence_seconds=0.4,
                                       on_utterance=on_utterance, should_ignore_audio=lambda: False,
                                       speech_rms_threshold=500, preroll_ms=500)
        first = FRAME  # "Can you": one frame, below the minimum
        assembler.observe(0, object(), first)
        assembler.flush(0)
        assert delivered == []
        later = bytes(reversed(FRAME))
        for _ in range(3):
            assembler.observe(0, object(), later)
        assert bytes(assembler.buffers[0]).startswith(first)
        assembler._cleanup_in_loop()

    delivered: list[bytes] = []
    asyncio.run(scenario())


def _early_text_assembler(timers, committed, *, early=True, enabled=True):
    from sleeptalk.voice.configs import AdaptiveEndpointingConfig

    async def on_utterance(_participant, pcm):
        committed.append(pcm)

    return UtteranceAssembler(
        loop=timers, min_bytes=1, silence_seconds=0.12, on_utterance=on_utterance,
        should_ignore_audio=lambda: False, on_pause=lambda _pid, _pcm: None, pause_ms=300,
        early_transcripts=(lambda: early),
        endpointing_config=AdaptiveEndpointingConfig(early_transcript=enabled),
    )


def _speak(timers, assembler, seconds: float) -> None:
    for _ in range(round(seconds / 0.02)):
        assembler.observe(0, object(), FRAME)
        timers.now += 0.02


@pytest.mark.asyncio
async def test_a_short_line_waits_for_its_early_text_then_ends_on_it() -> None:
    """A batch listener's short line used to end after 120 ms of silence, before
    any text existed; now the pause's text judges it: a finished question ends
    at the complete-bucket silence, well before the 1.8 s uncertain wait."""
    timers = Timers(asyncio.get_running_loop())
    committed: list[bytes] = []
    assembler = _early_text_assembler(timers, committed)
    _speak(timers, assembler, 1.0)
    timers.now += 0.15
    timers.fire("_maybe_flush")
    await asyncio.sleep(0)
    assert not committed  # 150 ms of silence no longer ends it: text is coming
    timers.fire("_report_pause")
    text = asyncio.get_running_loop().create_future()
    assembler.watch_early_transcript(0, text)
    timers.now += 0.35  # the text lands 500 ms after the voice stopped
    text.set_result("What's two plus two?")
    await asyncio.sleep(0)
    assert not committed  # judged "complete": ends at 650 ms of silence
    timers.now += 0.2
    timers.fire("_maybe_flush")
    await asyncio.sleep(0)
    assert len(committed) == 1  # 700 ms after the voice, not 1.8 s
    assembler._cleanup_in_loop()


@pytest.mark.asyncio
async def test_a_half_sentence_is_not_ended_on_its_pause() -> None:
    """ "I'd like to know about" then a breath: the text says unfinished, so the
    turn waits for the rest instead of splitting it in two."""
    timers = Timers(asyncio.get_running_loop())
    committed: list[bytes] = []
    assembler = _early_text_assembler(timers, committed)
    _speak(timers, assembler, 1.0)
    timers.now += 0.3
    timers.fire("_report_pause")
    text = asyncio.get_running_loop().create_future()
    assembler.watch_early_transcript(0, text)
    timers.now += 0.3
    text.set_result("I'd like to know about")
    await asyncio.sleep(0)
    timers.fire("_maybe_flush")
    await asyncio.sleep(0)
    assert not committed
    _speak(timers, assembler, 1.0)  # "the history of tea"
    await asyncio.sleep(0)
    assert not committed
    assembler._cleanup_in_loop()


@pytest.mark.asyncio
async def test_without_early_text_a_short_line_ends_on_silence_as_before() -> None:
    for early, enabled in ((False, True), (True, False)):
        timers = Timers(asyncio.get_running_loop())
        committed: list[bytes] = []
        assembler = _early_text_assembler(timers, committed, early=early, enabled=enabled)
        _speak(timers, assembler, 1.0)
        timers.now += 0.15
        timers.fire("_maybe_flush")
        await asyncio.sleep(0)
        assert len(committed) == 1, (early, enabled)
        assembler._cleanup_in_loop()


@pytest.mark.asyncio
async def test_speech_that_barged_in_is_never_dropped_as_too_short() -> None:
    """The words that interrupted the agent ("Wait, stop.") proved themselves
    to the barge-in detector; the turn they start keeps them even when the
    gate counted too little speech, instead of dropping the utterance."""
    timers = Timers(asyncio.get_running_loop())
    committed: list[bytes] = []

    async def on_utterance(_participant, pcm):
        committed.append(pcm)

    assembler = UtteranceAssembler(
        loop=timers, min_bytes=len(FRAME) * 100, silence_seconds=0.12, on_utterance=on_utterance,
        should_ignore_audio=lambda: False,
    )
    assembler.inject_preroll(0, object(), FRAME * 10)
    timers.now += 0.2
    assembler.flush(0)
    await asyncio.sleep(0)
    assert committed == [FRAME * 10]
    # An ordinary too-short blip is still dropped.
    assembler.observe(0, object(), FRAME)
    timers.now += 0.2
    assembler.flush(0)
    await asyncio.sleep(0)
    assert len(committed) == 1
    assembler._cleanup_in_loop()


@pytest.mark.asyncio
async def test_a_streaming_listeners_text_is_looked_at_again_soon() -> None:
    """With a streaming transcript still settling, the next endpoint check comes
    within 150 ms rather than when the 1.8 s uncertain budget runs out."""
    from sleeptalk.voice_loop.endpointing import TurnCompletionSignal

    timers = Timers(asyncio.get_running_loop())

    async def on_utterance(_participant, _pcm):
        return None

    text = {"now": ""}
    assembler = UtteranceAssembler(
        loop=timers, min_bytes=1, silence_seconds=0.12, on_utterance=on_utterance,
        should_ignore_audio=lambda: False,
        turn_completion_provider=lambda _pid: TurnCompletionSignal(transcript=text["now"], source="deepgram"),
    )
    _speak(timers, assembler, 2.0)
    timers.now += 0.15
    timers.pending.clear()
    assembler._maybe_flush(0)
    delays = [delay for delay, callback, _a, _h in timers.pending if callback.__name__ == "_maybe_flush"]
    assert delays and max(delays) <= 0.15
    assembler._cleanup_in_loop()


@pytest.mark.asyncio
async def test_a_barge_buffer_is_not_dropped_as_a_false_start() -> None:
    """Discord, synthetic: "Mm-hmm" barged in, its pre-roll opened the buffer,
    and the next frame Silero called quiet dropped it as a false start (never
    reported as speech). The rest was too short, so no utterance came, and the
    soft-barged answer stayed paused for good."""
    from sleeptalk.voice_loop.vad import VadObservation

    class Gate:
        async def observe(self, pcm):
            return VadObservation(is_speech=False, state="SILENT", confidence=0.0)

    timers = Timers(asyncio.get_running_loop())
    committed: list[bytes] = []

    async def on_utterance(_participant, pcm):
        committed.append(pcm)

    assembler = UtteranceAssembler(
        loop=timers, min_bytes=len(FRAME) * 100, silence_seconds=0.12, on_utterance=on_utterance,
        should_ignore_audio=lambda: False, vad_mode="silero", silero_vad_factory=lambda _config: Gate(),
    )
    assembler.inject_preroll(0, object(), FRAME * 10)
    await assembler._observe_gated(0, object(), FRAME)
    assert 0 in assembler.buffers  # kept, not dropped
    assembler.flush(0)
    await asyncio.sleep(0)
    assert committed and committed[0].startswith(FRAME * 10)
    assembler._cleanup_in_loop()
