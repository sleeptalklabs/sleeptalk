"""Local phrase playback, bounded prefetch, and interruption without native overlap."""

from __future__ import annotations

import asyncio
import json
import threading
from contextlib import aclosing
from types import SimpleNamespace

import numpy as np
import pytest

from sleeptalk.metering import UsageMeter
from sleeptalk.providers import ProviderSpec
from sleeptalk.speech.common import SpeechError
from sleeptalk.speech.local_tts import LocalTTS, _phrases


TEXT = (
    "We can start speaking as soon as the first phrase is ready, "
    "while the next phrase is generated in the background, "
    "so a long answer feels more responsive."
)


class Engine:
    def __init__(self, *, block_call=0, fail_call=0):
        self.calls = []
        self.block_call = block_call
        self.fail_call = fail_call
        self.entered = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()

    def generate(self, text, *, sid, speed):
        self.calls.append((text, sid, speed))
        number = len(self.calls)
        if number == self.block_call:
            self.entered.set()
            assert self.release.wait(5), "test failed to release native inference"
            self.finished.set()
        if number == self.fail_call:
            raise RuntimeError("inference failed")
        return SimpleNamespace(samples=np.full(3000, number / 10, dtype=np.float32), sample_rate=48_000)


def _speaker(voice):
    """Kokoro's speaker number for a voice name, whichever Kokoro bundle is pinned."""
    from sleeptalk.speech.local_models import get_model, voice_index

    return voice_index(get_model("kokoro-en"), voice)


def provider(engine, *, voice="lessac"):
    tts = LocalTTS(ProviderSpec(provider="local", voice_id=voice))
    tts._tts = engine
    return tts


async def entered(engine):
    assert await asyncio.to_thread(engine.entered.wait, 2)


@pytest.mark.parametrize("text", [
    "Hey, what's up!", TEXT,
    "Keep 1,234.56 and https://example.com/path?q=value together while explaining "
    "how this long unpunctuated response can begin playing well before all of "
    "the remaining words have been synthesized by the local voice model",
    "The release includes a sufficiently long opening phrase; followed by a short tail.",
])
def test_phrases_preserve_words_numbers_urls_and_short_replies(text):
    phrases = _phrases(text)
    assert " ".join(phrases) == text
    if len(text) < 45:
        assert phrases == [text]
    else:
        assert len(phrases) > 1
        assert all(len(phrase.split()) >= 3 for phrase in phrases)


def test_a_long_clause_without_commas_splits_before_a_word_that_starts_a_clause():
    text = ("The function reads the configuration file from the home directory and then validates every "
            "field against the schema before it starts the server on the port you chose.")
    phrases = _phrases(text)
    assert " ".join(phrases) == text
    assert len(phrases) == 2 and phrases[1].startswith("before it starts")
    # No clause word in reach: no cut until well past the join length.
    plain = ("Keep reading the configuration file from the home directory of the current user account "
             "with every field checked against its schema version number")
    assert _phrases(plain) == [plain]


async def test_first_audio_arrives_while_next_phrase_is_still_generating():
    engine = Engine(block_call=2)
    tts = provider(engine)
    try:
        async with aclosing(tts.stream_pcm(TEXT)) as stream:
            first = await asyncio.wait_for(anext(stream), 2)
            await entered(engine)
            assert len(first) == 1920 and not engine.finished.is_set()
            # Consumer has paused. Only one future phrase may be generated.
            engine.release.set()
            assert await asyncio.to_thread(engine.finished.wait, 2)
            await asyncio.sleep(0)
            assert len(engine.calls) == 2
            chunks = [first, *[chunk async for chunk in stream]]
        assert all(0 < len(chunk) <= 1920 and len(chunk) % 2 == 0 for chunk in chunks)
        assert " ".join(call[0] for call in engine.calls) == TEXT
        expected = b"".join(
            np.full(3000, int(number / 10 * 32767), dtype=np.int16).tobytes()
            for number in range(1, len(engine.calls) + 1)
        )
        assert b"".join(chunks) == expected
    finally:
        engine.release.set()
        await tts.close()


@pytest.mark.parametrize("action", ["iterator_close", "abort", "cancel_waiter", "close"])
async def test_interruption_discards_old_audio_and_serializes_the_next_turn(action, tmp_path):
    engine = Engine(block_call=2)
    tts = provider(engine)
    log = tmp_path / "usage.jsonl"
    tts.set_usage_meter(UsageMeter(log_path=log))
    stream = tts.stream_pcm(TEXT)
    next_turn = None
    try:
        first = await anext(stream)
        await entered(engine)
        delivered = len(first)
        if action == "cancel_waiter":
            # Finish consuming the first phrase, then wait on the blocked one.
            for _ in range(3):
                delivered += len(await anext(stream))
            pending = asyncio.create_task(anext(stream))
            await asyncio.sleep(0)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
        elif action == "iterator_close":
            await asyncio.wait_for(stream.aclose(), 1)
        else:
            await asyncio.wait_for(getattr(tts, action)(), 1)
            with pytest.raises(asyncio.CancelledError):
                await anext(stream)
        await stream.aclose()
        if action == "close":
            with pytest.raises(SpeechError, match="closed"):
                await tts.synthesize_pcm("New turn.")
        else:
            next_turn = asyncio.create_task(tts.synthesize_pcm("New turn."))
            await asyncio.sleep(0.02)
            assert len(engine.calls) == 2  # old native call still holds the lock
        engine.release.set()
        if next_turn is not None:
            pcm = await asyncio.wait_for(next_turn, 2)
            assert len(pcm) == 6000
            assert [call[0] for call in engine.calls][2:] == ["New turn."]
        events = [json.loads(line) for line in log.read_text().splitlines()]
        finished = [event for event in events if event["stage"] == "attempt_finished"]
        assert finished[0]["status"] == "cancelled"
        assert finished[0]["units"]["outputAudioBytes"] == delivered
        assert not tts._requests
    finally:
        engine.release.set()
        if next_turn is not None:
            await asyncio.gather(next_turn, return_exceptions=True)
        await stream.aclose()
        await tts.close()


async def test_failed_prefetch_surfaces_after_delivered_audio_and_next_request_works(tmp_path):
    engine = Engine(fail_call=2)
    tts = provider(engine)
    log = tmp_path / "usage.jsonl"
    tts.set_usage_meter(UsageMeter(log_path=log))
    chunks = []
    with pytest.raises(SpeechError, match="inference failed"):
        async for chunk in tts.synthesize(TEXT):
            chunks.append(chunk)
    assert sum(map(len, chunks)) == 6000
    assert len(engine.calls) == 2
    assert await tts.synthesize_pcm("Recover.")
    events = [json.loads(line) for line in log.read_text().splitlines()]
    finished = [event for event in events if event["stage"] == "attempt_finished"]
    assert [event["status"] for event in finished] == ["error", "ok"]
    assert finished[0]["units"]["outputAudioBytes"] == 6000
    await tts.close()


async def test_voice_and_speed_are_stable_through_a_request():
    from dataclasses import replace

    engine = Engine(block_call=2)
    tts = provider(engine, voice="af_sarah")
    try:
        async with aclosing(tts.stream_pcm(TEXT)) as stream:
            await anext(stream)
            await entered(engine)
            tts.set_voice("af_sky")
            tts.spec = replace(tts.spec, speed=1.25)
            engine.release.set()
            async for _ in stream:
                pass
        assert all(sid == _speaker("af_sarah") and speed == 1.0 for _, sid, speed in engine.calls)
        await tts.synthesize_pcm("New voice.")
        assert engine.calls[-1][1] == _speaker("af_sky") and engine.calls[-1][2] != 1.0
    finally:
        engine.release.set()
        await tts.close()


async def test_abort_before_first_audio_does_not_start_later_phrases():
    engine = Engine(block_call=1)
    tts = provider(engine)
    pending = asyncio.create_task(tts.synthesize_pcm(TEXT))
    try:
        await entered(engine)
        await asyncio.wait_for(tts.abort(), 1)
        with pytest.raises(asyncio.CancelledError):
            await pending
        engine.release.set()
        assert await asyncio.to_thread(engine.finished.wait, 2)
        assert len(engine.calls) == 1
        assert not tts._requests
    finally:
        engine.release.set()
        await asyncio.gather(pending, return_exceptions=True)
        await tts.close()


@pytest.mark.parametrize("action", ["switch_voice", "close"])
async def test_model_load_cannot_restore_a_stale_or_closed_voice(monkeypatch, action):
    entered_load = threading.Event()
    release_load = threading.Event()
    engines = {"piper-lessac-medium": Engine(), "kokoro-en": Engine()}

    def load(model, *args, **kwargs):
        if model.id == "piper-lessac-medium":
            entered_load.set()
            assert release_load.wait(5)
        return engines[model.id]

    monkeypatch.setattr("sleeptalk.speech.local_tts.load_synthesizer", load)
    # Piper's voice named: the test needs the model that loads slowly.
    tts = LocalTTS(ProviderSpec(provider="local", voice_id="lessac"))
    pending = asyncio.create_task(tts.synthesize_pcm("Hello there."))
    try:
        assert await asyncio.to_thread(entered_load.wait, 2)
        if action == "switch_voice":
            tts.set_voice("af_sarah")
        else:
            await tts.close()
        release_load.set()
        if action == "close":
            with pytest.raises(SpeechError, match="closed"):
                await pending
            assert tts._tts is None
        else:
            assert await pending
            assert tts._tts is engines["kokoro-en"]
            assert engines["kokoro-en"].calls[0][1] == _speaker("af_sarah")
        assert not engines["piper-lessac-medium"].calls
        assert not tts._requests
    finally:
        release_load.set()
        await asyncio.gather(pending, return_exceptions=True)
        await tts.close()


async def test_switching_to_a_british_voice_during_a_load_loads_british_english(monkeypatch):
    entered_load = threading.Event()
    release_load = threading.Event()
    loads = []

    def load(model, *args, accent="en-us", **kwargs):
        loads.append(accent)
        if len(loads) == 1:
            entered_load.set()
            assert release_load.wait(5)
        return Engine()

    monkeypatch.setattr("sleeptalk.speech.local_tts.load_synthesizer", load)
    tts = LocalTTS(ProviderSpec(provider="local", model="kokoro-en", voice_id="af_heart"))
    pending = asyncio.create_task(tts.synthesize_pcm("Hello there."))
    try:
        assert await asyncio.to_thread(entered_load.wait, 2)
        tts.set_voice("bf_emma")  # same model, other accent
        release_load.set()
        assert await pending
        assert loads == ["en-us", "en"] and tts.accent == "en"
    finally:
        release_load.set()
        await asyncio.gather(pending, return_exceptions=True)
        await tts.close()
