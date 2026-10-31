import asyncio

import pytest

from sleeptalk.discord_native.playback import (
    DiscordPlaybackController,
    DiscordPlaybackError,
    _audio_duration_ms,
)
from sleeptalk.discord_voice import DiscordRouteAudioOutput


class FakeOpusAudio:
    def __init__(self, path):
        self.path = path


@pytest.mark.asyncio
async def test_playback_stop_prefers_stop_playing_over_stop():
    calls = []

    class FakeVoice:
        def is_playing(self):
            return True

        def stop_playing(self):
            calls.append("stop_playing")

        def stop(self):
            raise AssertionError("stop would also stop receive on VoiceRecvClient")

    stopped = await DiscordPlaybackController(FakeVoice()).stop("barge-in")

    assert stopped is True
    assert calls == ["stop_playing"]


@pytest.mark.asyncio
async def test_playback_stop_falls_back_for_plain_voice_client():
    calls = []

    class FakeVoice:
        def is_playing(self):
            return True

        def stop(self):
            calls.append("stop")

    stopped = await DiscordPlaybackController(FakeVoice()).stop("barge-in")

    assert stopped is True
    assert calls == ["stop"]


@pytest.mark.asyncio
async def test_route_output_discard_fences_late_same_turn_audio():
    released = asyncio.Event()
    stops = []

    class Playback:
        async def stop(self, reason):
            assert reason == "barge-in"
            stops.append(reason)
            released.set()
            return True

    class Source:
        def __init__(self):
            self.discarded = 0

        def discard(self):
            self.discarded += 1

    async def playing():
        await released.wait()

    output = DiscordRouteAudioOutput(Playback())
    source = Source()
    output._latest_turn = 7
    output._source = source
    output._play_task = asyncio.create_task(playing())

    await output.discard_turn(7, "barge-in")
    await output.discard_turn(7, "barge-in")
    await output.write(b"late-pcm", 7)

    assert source.discarded == 1
    assert stops == ["barge-in"]
    assert output.accounting().bytes_dropped_stale == len(b"late-pcm")


@pytest.mark.asyncio
async def test_playback_hooks_fire_after_voice_accepts_file(monkeypatch, tmp_path):
    events = []
    path = tmp_path / "reply.wav"
    path.write_bytes(b"not really a wav")

    class FakeVoice:
        def is_playing(self):
            return False

        def is_connected(self):
            return True

        def play(self, _source, *, after):
            events.append("play")
            after(None)

    monkeypatch.setattr("sleeptalk.discord_native.playback.discord.FFmpegOpusAudio", FakeOpusAudio)
    controller = DiscordPlaybackController(
        FakeVoice(),
        on_started=lambda: events.append("started"),
        on_stopped=lambda: events.append("stopped"),
    )

    result = await controller.play_file(path, on_started=lambda: events.append("trace_started"))

    assert result.completed is True
    assert events == ["play", "started", "trace_started", "stopped"]


@pytest.mark.asyncio
async def test_playback_rejects_disconnected_voice_before_starting(tmp_path):
    events = []
    path = tmp_path / "reply.wav"
    path.write_bytes(b"not really a wav")

    class FakeVoice:
        def is_playing(self):
            return False

        def is_connected(self):
            return False

        def play(self, _source, *, after):
            raise AssertionError("play should not be called")

    controller = DiscordPlaybackController(FakeVoice(), on_started=lambda: events.append("started"))

    with pytest.raises(DiscordPlaybackError, match="voice client is not connected"):
        await controller.play_file(path)

    assert events == []


def test_audio_duration_reads_wav_length(tmp_path):
    import wave

    path = tmp_path / "tone.wav"
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(24000)
        wav.writeframes(b"\x00\x00" * 12000)

    assert _audio_duration_ms(path) == 500
