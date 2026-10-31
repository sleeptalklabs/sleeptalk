import asyncio
from types import SimpleNamespace

import pytest
from sleeptalk.discord_voice.route_audio import DiscordRouteAudioOutput


class ManualPlayback:
    def __init__(self):
        self.source = None
        self.started = asyncio.Event()
        self.stopped = asyncio.Event()

    async def play_source(self, source, **kwargs):
        self.source = source
        self.started.set()
        await self.stopped.wait()
        return SimpleNamespace(cancelled=True, completed=False)

    async def stop(self, reason):
        self.stopped.set()


@pytest.mark.asyncio
async def test_discord_counts_source_consumption_and_excludes_queued_cancelled_audio():
    playback = ManualPlayback()
    output = DiscordRouteAudioOutput(playback)
    frame_bytes = 1920  # Canonical 48 kHz mono, one Discord frame.
    await output.write(b"\x01\x00" * (frame_bytes * 3 // 2), 1)
    await playback.started.wait()
    assert output.accounting().bytes_generated == frame_bytes * 3
    assert output.accounting().bytes_written == 0

    assert len(playback.source.read()) == frame_bytes * 2
    assert output.accounting().bytes_written == frame_bytes
    await output.discard_turn(1)
    assert playback.source.read() == b""
    assert output.accounting().bytes_written == frame_bytes
    await output.write(b"\x02\x00" * 960, 1)
    assert output.accounting().bytes_written == frame_bytes
    assert output.accounting().bytes_dropped_stale == frame_bytes


@pytest.mark.asyncio
async def test_discord_never_counts_underrun_or_final_frame_padding():
    playback = ManualPlayback()
    output = DiscordRouteAudioOutput(playback)
    await output.write(b"\x01\x00" * 5, 2)
    await playback.started.wait()
    playback.source.read()
    assert output.accounting().bytes_written == 0
    playback.source.finish()
    playback.source.read()
    assert output.accounting().bytes_written == 10
    assert output.accounting().bytes_generated == 10
    await output.discard_turn(2)
