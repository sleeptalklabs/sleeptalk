import asyncio

import pytest
from av import AudioFrame
from sleeptalk.call_provider import QueuedAudioTrack, _WebRTCAudioStream
from sleeptalk.web_voice.playback import WebRTCPlaybackController


def stream_with_track():
    stream = object.__new__(_WebRTCAudioStream)
    stream._closed = asyncio.Event()
    stream._authorized = asyncio.Event()
    stream._authorized.set()
    stream.outgoing = QueuedAudioTrack()
    return stream


async def wait_for_frame(track):
    for _ in range(10):
        if not track._frames.empty():
            return
        await asyncio.sleep(0)
    raise AssertionError("No audio reached the output track")


@pytest.mark.asyncio
async def test_web_accounts_only_frames_consumed_by_real_track_and_finish_waits():
    stream = stream_with_track()
    playback = WebRTCPlaybackController(stream)
    await playback.write(b"\x01\x00" * 1920, turn_id=1)
    finished = asyncio.create_task(playback.finish_turn(1))
    await wait_for_frame(stream.outgoing)
    assert playback.accounting().bytes_written == 0
    assert not finished.done()

    await stream.outgoing.recv()
    await wait_for_frame(stream.outgoing)
    assert playback.accounting().bytes_written == 1920
    assert not finished.done()
    await stream.outgoing.recv()
    await asyncio.wait_for(finished, 0.2)
    assert playback.accounting().bytes_written == 3840
    assert playback.accounting().bytes_generated == 3840
    await playback.close()
    stream.outgoing.stop()


@pytest.mark.asyncio
async def test_web_discard_wakes_pending_consumption_without_counting_unheard_frames():
    stream = stream_with_track()
    playback = WebRTCPlaybackController(stream)
    await playback.write(b"\x01\x00" * 2880, turn_id=1)
    finished = asyncio.create_task(playback.finish_turn(1))
    await wait_for_frame(stream.outgoing)
    await stream.outgoing.recv()
    await wait_for_frame(stream.outgoing)
    await playback.discard_turn(1)
    await asyncio.wait_for(finished, 0.2)
    assert playback.accounting().bytes_written == 1920
    assert playback.accounting().bytes_generated == 5760
    silence = await stream.outgoing.recv()
    assert bytes(silence.planes[0]) == bytes(silence.planes[0].buffer_size)
    await playback.close()
    stream.outgoing.stop()


@pytest.mark.asyncio
async def test_web_close_does_not_wait_for_an_absent_webrtc_consumer():
    stream = stream_with_track()
    playback = WebRTCPlaybackController(stream)
    await playback.write(b"\x01\x00" * 960, turn_id=1)
    await wait_for_frame(stream.outgoing)
    await asyncio.wait_for(playback.close(), 0.2)
    assert playback.accounting().bytes_written == 0
    silence = await stream.outgoing.recv()
    assert bytes(silence.planes[0]) == bytes(silence.planes[0].buffer_size)
    stream.outgoing.stop()


@pytest.mark.asyncio
async def test_track_hangup_wakes_both_queued_and_blocked_receipts():
    track = QueuedAudioTrack(max_frames=1)
    frame = AudioFrame(format="s16", layout="stereo", samples=960)
    first = asyncio.create_task(track.send_played(frame))
    await wait_for_frame(track)
    second = asyncio.create_task(track.send_played(frame))
    third = asyncio.create_task(track.send_played(frame))
    await asyncio.sleep(0)
    assert not first.done()
    assert not second.done()
    track.stop()
    assert await asyncio.wait_for(first, 0.2) is False
    assert await asyncio.wait_for(second, 0.2) is False
    assert await asyncio.wait_for(third, 0.2) is False


@pytest.mark.asyncio
async def test_track_clear_rejects_frames_waiting_for_queue_space():
    track = QueuedAudioTrack(max_frames=1)
    frame = AudioFrame(format="s16", layout="stereo", samples=960)
    frame.planes[0].update(b"\x01" * frame.planes[0].buffer_size)
    first = asyncio.create_task(track.send_played(frame))
    await wait_for_frame(track)
    second = asyncio.create_task(track.send_played(frame))
    await asyncio.sleep(0)
    track.clear()
    assert await asyncio.wait_for(first, 0.2) is False
    assert await asyncio.wait_for(second, 0.2) is False
    silence = await track.recv()
    assert bytes(silence.planes[0]) == bytes(silence.planes[0].buffer_size)
    track.stop()


@pytest.mark.asyncio
async def test_a_dropped_call_ends_playout_quietly():
    """The connection dropping mid-answer used to leave the playout task with
    an exception nobody retrieved, printed as a traceback in the host log."""
    import gc

    seen = []
    loop = asyncio.get_running_loop()
    loop.set_exception_handler(lambda _loop, context: seen.append(context.get("exception")))
    try:
        stream = stream_with_track()
        stream._end_reason = "connection_lost"
        stream._closed.set()
        playback = WebRTCPlaybackController(stream)
        await playback.write(b"\x01\x00" * 1920, turn_id=1)
        for _ in range(5):
            await asyncio.sleep(0)
        gc.collect()
        await asyncio.sleep(0)
        assert seen == [] and not playback.active
        await playback.close()
    finally:
        loop.set_exception_handler(None)


class _RecordingStream:
    """Records which audio reached the track, yielding between frames as the
    real paced track does."""

    def __init__(self):
        self.sent: list[str] = []

    async def send_played(self, frame):
        loud = max(abs(s) for s in frame.to_ndarray().ravel())
        self.sent.append("clip" if loud > 100 else "speech")
        await asyncio.sleep(0)
        return True

    def clear_output(self):
        pass


@pytest.mark.asyncio
async def test_web_clip_waits_behind_queued_speech_instead_of_splicing_into_it(tmp_path):
    import wave

    clip = tmp_path / "keyboard.wav"
    with wave.open(str(clip), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(48000)
        out.writeframes((10000).to_bytes(2, "little", signed=True) * 4800)
    stream = _RecordingStream()
    playback = WebRTCPlaybackController(stream)
    for _ in range(4):
        await playback.write(b"\x01\x00" * 1920, turn_id=1)

    await asyncio.wait_for(playback.play_file(clip), 1.0)

    assert "clip" in stream.sent
    first_clip = stream.sent.index("clip")
    assert "speech" not in stream.sent[first_clip:], stream.sent
    await playback.close()
