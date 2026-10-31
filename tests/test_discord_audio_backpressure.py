import asyncio
import io
import wave
from types import SimpleNamespace

import pytest

pytest.importorskip("discord")

from sleeptalk.discord_voice.route_audio import DiscordRouteAudioOutput
from sleeptalk.routes.audio_output import TurnPlaybackError
from sleeptalk.stream_tts import StreamPCMAudioSource


MONO_FRAME = b"\x01\x00" * 960


class ManualPlayback:
    def __init__(self):
        self.started = asyncio.Event()
        self.stopped = asyncio.Event()
        self.source = None

    async def play_source(self, source, **_kwargs):
        self.source = source
        self.stopped = asyncio.Event()
        self.started.set()
        await self.stopped.wait()
        return SimpleNamespace(cancelled=True, completed=False)

    async def stop(self, _reason):
        self.stopped.set()


def wav_audio(pcm):
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(48000)
        wav.writeframes(pcm)
    return output.getvalue()


@pytest.mark.parametrize("as_file", [False, True])
async def test_slow_player_bounds_raw_and_file_audio(as_file, tmp_path):
    playback = ManualPlayback()
    consumed = []
    output = DiscordRouteAudioOutput(playback, on_read=consumed.append)
    pcm = MONO_FRAME * 60
    clip = tmp_path / "clip.wav"
    clip.write_bytes(wav_audio(pcm))
    writer = asyncio.create_task(output.play_file(clip, turn_id=1) if as_file else output.write(pcm, 1))
    await asyncio.wait_for(playback.started.wait(), 1)
    source = playback.source
    assert output.buffered_ms == 500
    assert not writer.done()
    assert output.accounting().bytes_written == 0
    assert output.accounting().bytes_generated == len(pcm)

    frames = []
    for _ in range(60):
        frames.append(await asyncio.to_thread(source.read))
        assert 0 <= output.buffered_ms <= 500
    await asyncio.wait_for(writer, 1)
    assert output.buffered_ms == 0
    assert b"".join(frames) == b"\x01\x00" * (len(pcm))
    assert output.accounting().bytes_written == len(pcm)
    assert consumed == [1] * 60
    await output.discard_turn(1)


@pytest.mark.parametrize("operation", ["discard", "close"])
async def test_blocked_writer_wakes_on_discard_or_close(operation):
    playback = ManualPlayback()
    output = DiscordRouteAudioOutput(playback)
    writer = asyncio.create_task(output.write(MONO_FRAME * 30, 3))
    await asyncio.wait_for(playback.started.wait(), 1)
    old_source = playback.source
    assert output.buffered_ms == 500
    assert not writer.done()

    if operation == "discard":
        await asyncio.wait_for(output.discard_turn(3), 1)
    else:
        await asyncio.wait_for(output.close(), 1)
    await asyncio.wait_for(writer, 1)
    assert old_source.read() == b""
    assert output.buffered_ms == 0
    assert output.accounting().bytes_written == 0
    assert output.accounting().bytes_dropped_stale == len(MONO_FRAME) * 5
    await output.write(MONO_FRAME, 3)
    assert output.accounting().bytes_dropped_stale == len(MONO_FRAME) * 6

    if operation == "discard":
        playback.started.clear()
        await output.write(MONO_FRAME, 4)
        await asyncio.wait_for(playback.started.wait(), 1)
        assert playback.source is not old_source
        assert len(await asyncio.to_thread(playback.source.read)) == 3840
        assert output.accounting().turn_id == 4
        assert output.accounting().bytes_written == len(MONO_FRAME)
        await output.discard_turn(4)


@pytest.mark.parametrize("error", [RuntimeError("playback failed"), None])
async def test_playback_completion_wakes_blocked_writer_and_fences_failed_turn(error):
    class FailingPlayback(ManualPlayback):
        async def play_source(self, source, **_kwargs):
            self.source = source
            self.started.set()
            await self.stopped.wait()
            if error is not None:
                raise error

    playback = FailingPlayback()
    output = DiscordRouteAudioOutput(playback)
    writer = asyncio.create_task(output.write(MONO_FRAME * 30, 8))
    await asyncio.wait_for(playback.started.wait(), 1)
    assert not writer.done()
    playback.stopped.set()
    expected = RuntimeError if error is not None else TurnPlaybackError
    with pytest.raises(expected):
        await asyncio.wait_for(writer, 1)
    assert output.buffered_ms == 0
    assert playback.source.read() == b""
    await output.write(MONO_FRAME, 8)
    assert output.buffered_ms == 0
    await output.close()


async def test_writer_task_cancellation_releases_write_lock():
    playback = ManualPlayback()
    output = DiscordRouteAudioOutput(playback)
    writer = asyncio.create_task(output.write(MONO_FRAME * 30, 1))
    await asyncio.wait_for(playback.started.wait(), 1)
    writer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await writer
    await output.discard_turn(1)
    playback.started.clear()
    await asyncio.wait_for(output.write(MONO_FRAME, 2), 1)
    await asyncio.wait_for(playback.started.wait(), 1)
    await output.discard_turn(2)


async def test_source_cleanup_wakes_blocked_writer():
    playback = ManualPlayback()
    output = DiscordRouteAudioOutput(playback)
    writer = asyncio.create_task(output.write(MONO_FRAME * 30, 1))
    await asyncio.wait_for(playback.started.wait(), 1)
    await asyncio.to_thread(playback.source.cleanup)
    await asyncio.wait_for(writer, 1)
    assert output.buffered_ms == 0
    await output.discard_turn(1)


async def test_finishing_source_remains_measurable_and_can_be_discarded():
    playback = ManualPlayback()
    output = DiscordRouteAudioOutput(playback)
    await output.write(MONO_FRAME, 1)
    await asyncio.wait_for(playback.started.wait(), 1)
    finishing = asyncio.create_task(output.finish_turn(1))
    await asyncio.sleep(0)
    assert not finishing.done()
    assert output.buffered_ms == 20
    await asyncio.wait_for(output.discard_turn(1), 1)
    await asyncio.wait_for(finishing, 1)
    assert playback.source.read() == b""
    assert output.buffered_ms == 0


def test_source_reports_duration_and_callbacks_outside_read_lock():
    consumed = []
    changed = []
    source = StreamPCMAudioSource(
        on_read=lambda count: consumed.append((count, source.buffered_ms)),
        on_buffer_change=lambda: changed.append(source.buffered_ms),
    )
    source.feed(b"\x01\x00" * 5)
    assert source.buffered_ms == 10 / 192
    assert source.read() == bytes(source.FRAME_BYTES)
    assert consumed == []
    source.finish()
    assert source.read()[:10] == b"\x01\x00" * 5
    assert consumed == [(10, 0)]
    assert changed[-1] == 0
    source.cleanup()
    assert not source.feed(b"\x01\x00")
    assert source.read() == b""
