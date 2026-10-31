from dataclasses import replace
from types import SimpleNamespace

import pytest

from sleeptalk.opening_preamble_gate import OpeningPreambleGate
from sleeptalk.providers import ProviderSpec
from sleeptalk.routes.base import TurnContext
from sleeptalk.routes.cascade import CascadeRoute


class Speech:
    def __init__(self):
        self.spec = ProviderSpec(provider="fake", voice_id="voice", speed=1)
        self.calls = 0
        self.fail = False
        self.chunk = b"\x01\x00" * 960

    async def stream_pcm(self, text):
        self.calls += 1
        yield self.chunk
        if self.fail:
            raise RuntimeError("truncated stream")
        yield self.chunk


class Output:
    def __init__(self):
        self.writes = []

    async def write(self, pcm, turn_id):
        self.writes.append((turn_id, pcm))


def context(turn_id=1):
    return TurnContext(session_id="cache", turn_id=turn_id, emit=lambda _: None,
                       extras={"_opening_gate": OpeningPreambleGate()})


@pytest.mark.asyncio
async def test_only_complete_canned_audio_is_reused_and_voice_changes_miss():
    speech, output = Speech(), Output()
    route = CascadeRoute(agent=SimpleNamespace(), tts=speech, audio_output=output)
    await route._speak_audio("One moment.", context(), category="acknowledgement")
    await route._speak_audio("One moment.", context(2), category="acknowledgement")
    assert speech.calls == 1
    assert output.writes[:2] == [(1, speech.chunk)] * 2
    assert output.writes[2:] == [(2, speech.chunk)] * 2
    speech.spec = replace(speech.spec, speed=0.5)
    await route._speak_audio("One moment.", context(3), category="acknowledgement")
    assert speech.calls == 2
    await route._speak_audio("One moment.", context(4), category="answer")
    assert speech.calls == 3
    await route.close()
    assert route._clip_cache_bytes == 0


@pytest.mark.asyncio
async def test_partial_and_oversized_clips_never_enter_cache():
    speech = Speech()
    route = CascadeRoute(agent=SimpleNamespace(), tts=speech, audio_output=Output())
    speech.fail = True
    with pytest.raises(RuntimeError, match="truncated"):
        await route._speak_audio("One moment.", context(), category="acknowledgement")
    assert not route._clip_cache
    speech.fail = False
    speech.chunk = b"\x01\x00" * 96_001
    await route._speak_audio("One moment.", context(2), category="acknowledgement")
    assert speech.calls == 2
    assert not route._clip_cache
    await route.close()


@pytest.mark.asyncio
async def test_cache_evicts_oldest_clip_within_total_byte_budget():
    speech = Speech()
    speech.chunk = b"\x01\x00" * 48_000
    route = CascadeRoute(agent=SimpleNamespace(), tts=speech, audio_output=Output())
    for number in range(10):
        await route._speak_audio(f"Cue {number}.", context(number), category="narration")
    assert len(route._clip_cache) == 8
    assert route._clip_cache_bytes == 1_536_000
    await route._speak_audio("Cue 0.", context(11), category="narration")
    assert speech.calls == 11
    await route.close()
