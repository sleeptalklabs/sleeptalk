"""Tests for sleeptalk.transports.terminal_audio (r2-terminal-device-io).

Fake-device driven: no audio hardware or sounddevice install required.
Covers device enumeration/default/explicit selection, unavailable-device
errors, canonical-format boundaries, drop-oldest capture backpressure,
keep-every-chunk playback, and a local loopback diagnostic (fake in→out
round trip).
"""

from __future__ import annotations

import pytest
from sleeptalk.transports.terminal_audio import (
    CANONICAL_CHANNELS,
    CANONICAL_DTYPE,
    CANONICAL_SAMPLE_RATE,
    AudioDeviceError,
    DeviceListing,
    TerminalMicrophone,
    TerminalSpeaker,
    default_devices,
    list_devices,
    prefer_low_latency_hostapi,
)


class FakeStream:
    def __init__(self, samplerate, channels, dtype, device=None, blocksize=None, callback=None):
        self.samplerate = samplerate
        self.channels = channels
        self.dtype = dtype
        self.device = device
        self.blocksize = blocksize
        self.callback = callback
        self.started = False
        self.closed = False

    def start(self):
        self.started = True

    def stop(self):
        pass

    def close(self):
        self.closed = True

    # test drivers ---------------------------------------------------------
    def push_audio(self, pcm: bytes) -> None:
        """Simulate the sounddevice capture callback."""
        frames = len(pcm) // 2
        self.callback(pcm, frames, None, None)

    def pull_audio(self, frames: int) -> bytes:
        """Simulate the playback callback filling outdata."""
        outdata = bytearray(frames * 2)
        self.callback(outdata, frames, None, None)
        return bytes(outdata)


class FakeSoundDevice:
    """sounddevice-shaped fake with a configurable device list."""

    def __init__(self, devices=None, default=(0, 1)):
        self._devices = (
            devices
            if devices is not None
            else [
                {
                    "name": "MacBook Pro Microphone",
                    "max_input_channels": 1,
                    "max_output_channels": 0,
                },
                {"name": "MacBook Pro Speakers", "max_input_channels": 0, "max_output_channels": 2},
                {"name": "USB Headset", "max_input_channels": 1, "max_output_channels": 1},
            ]
        )
        self.default = type("Default", (), {"device": default})()
        self.opened_streams: list[FakeStream] = []

    def query_devices(self):
        return self._devices

    def RawInputStream(self, **kwargs):
        stream = FakeStream(**kwargs)
        self.opened_streams.append(stream)
        return stream

    def RawOutputStream(self, **kwargs):
        stream = FakeStream(**kwargs)
        self.opened_streams.append(stream)
        return stream


def test_prefer_hostapi_is_noop_without_query() -> None:
    assert prefer_low_latency_hostapi(FakeSoundDevice()) is None


def test_prefer_wasapi_on_windows() -> None:
    class _Host:
        def __init__(self) -> None:
            self.default = type("Default", (), {"hostapi": 0})()

        def query_hostapis(self):
            return [{"name": "MME"}, {"name": "Windows WASAPI"}]

    host = _Host()
    assert prefer_low_latency_hostapi(host, platform="win32") == 1
    assert host.default.hostapi == 1


def test_prefer_missing_api_leaves_default() -> None:
    class _Host:
        def __init__(self) -> None:
            self.default = type("Default", (), {"hostapi": 0})()

        def query_hostapis(self):
            return [{"name": "ALSA"}]

    host = _Host()
    assert prefer_low_latency_hostapi(host, platform="linux") is None
    assert host.default.hostapi == 0


# -- enumeration -----------------------------------------------------------


def test_list_devices_marks_defaults() -> None:
    listing = list_devices(FakeSoundDevice(default=(0, 1)))
    by_name = {d["name"]: d for d in listing}
    assert by_name["MacBook Pro Microphone"]["is_default_input"]
    assert by_name["MacBook Pro Speakers"]["is_default_output"]
    assert by_name["USB Headset"]["inputs"] == 1
    assert by_name["USB Headset"]["outputs"] == 1


def test_default_devices_resolved() -> None:
    defaults = default_devices(FakeSoundDevice(default=(0, 1)))
    assert defaults == {"input": 0, "output": 1}


def test_missing_default_input_is_clear_error() -> None:
    sd = FakeSoundDevice(default=(-1, 1))
    with pytest.raises(AudioDeviceError, match="default input"):
        default_devices(sd)


# -- device selection --------------------------------------------------------


def test_explicit_device_by_name_and_index() -> None:
    listing = DeviceListing(FakeSoundDevice())
    assert listing.resolve_input("USB Headset") == 2
    assert listing.resolve_output("USB Headset") == 2
    assert listing.resolve_input(2) == 2


def test_default_selection_when_none_given() -> None:
    listing = DeviceListing(FakeSoundDevice(default=(0, 1)))
    assert listing.resolve_input(None) == 0
    assert listing.resolve_output(None) == 1


def test_unknown_device_is_clear_error() -> None:
    with pytest.raises(AudioDeviceError, match="Unknown input device"):
        DeviceListing(FakeSoundDevice()).resolve_input("No Such Mic")


def test_output_device_requested_as_input_is_error() -> None:
    # speakers have no input channels
    with pytest.raises(AudioDeviceError, match="no input channels"):
        DeviceListing(FakeSoundDevice()).resolve_input("MacBook Pro Speakers")


def test_no_usable_device_is_clear_error() -> None:
    empty = FakeSoundDevice(devices=[], default=(-1, -1))
    with pytest.raises(AudioDeviceError, match="No usable input device"):
        DeviceListing(empty).resolve_input(None)


def test_missing_sounddevice_package_is_clear_error() -> None:
    class BrokenImport:
        def __getattr__(self, name):
            raise AudioDeviceError("requires the 'sounddevice' package")

    # simulate ImportError path: pass None and monkeypatch the import
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "sounddevice":
            raise ImportError("No module named 'sounddevice'")
        return real_import(name, *args, **kwargs)

    builtins.__import__ = fake_import
    try:
        with pytest.raises(AudioDeviceError, match="sounddevice"):
            DeviceListing(None)
    finally:
        builtins.__import__ = real_import


# -- capture -----------------------------------------------------------------


def test_microphone_opens_canonical_format() -> None:
    sd = FakeSoundDevice()
    mic = TerminalMicrophone(sounddevice=sd)
    stream = sd.opened_streams[0]
    assert stream.samplerate == CANONICAL_SAMPLE_RATE
    assert stream.dtype == CANONICAL_DTYPE
    assert stream.channels == CANONICAL_CHANNELS
    assert stream.device == 0  # default input
    mic.close()
    assert stream.closed


def test_microphone_reads_oldest_first() -> None:
    sd = FakeSoundDevice()
    mic = TerminalMicrophone(sounddevice=sd)
    stream = sd.opened_streams[0]
    stream.push_audio(b"\x01\x00")
    stream.push_audio(b"\x02\x00")
    assert mic.read() == b"\x01\x00\x02\x00"
    assert mic.read() == b""  # drained
    mic.close()


def test_microphone_backpressure_is_bounded_drop_oldest() -> None:
    sd = FakeSoundDevice()
    mic = TerminalMicrophone(sounddevice=sd, buffer_chunks=4)
    stream = sd.opened_streams[0]
    for i in range(10):
        stream.push_audio(bytes([i]) * 2)
    # ring kept the newest 4; memory never exceeded the bound
    data = mic.read()
    assert data == bytes([6]) * 2 + bytes([7]) * 2 + bytes([8]) * 2 + bytes([9]) * 2
    assert mic.dropped_chunks == 6
    mic.close()


def test_microphone_close_stops_capture() -> None:
    sd = FakeSoundDevice()
    mic = TerminalMicrophone(sounddevice=sd)
    stream = sd.opened_streams[0]
    mic.close()
    stream.push_audio(b"\x01\x00")  # late callback after close
    assert mic.read() == b""
    mic.close()  # idempotent



def test_microphone_counts_exact_silence_until_it_hears_anything() -> None:
    # A macOS terminal denied the mic gets exact zeros, never an error.
    sd = FakeSoundDevice()
    mic = TerminalMicrophone(sounddevice=sd)
    stream = sd.opened_streams[0]
    second = CANONICAL_SAMPLE_RATE * CANONICAL_CHANNELS * 2
    for _ in range(4):
        stream.push_audio(b"\0" * second)
    assert mic.silent_seconds == 4.0
    stream.push_audio(b"\0\0\x01\x00")  # a real room is never exactly zero
    stream.push_audio(b"\0" * second)
    assert mic.silent_seconds == 0.0
    mic.close()

# -- playback -----------------------------------------------------------------


def test_speaker_opens_canonical_format() -> None:
    sd = FakeSoundDevice()
    speaker = TerminalSpeaker(sounddevice=sd)
    stream = sd.opened_streams[0]
    assert stream.samplerate == CANONICAL_SAMPLE_RATE
    assert stream.dtype == CANONICAL_DTYPE
    speaker.close()
    assert stream.closed


def test_speaker_plays_queued_audio_in_order() -> None:
    sd = FakeSoundDevice()
    speaker = TerminalSpeaker(sounddevice=sd)
    stream = sd.opened_streams[0]
    speaker.write(b"\x01\x00\x02\x00")
    speaker.write(b"\x03\x00\x04\x00")
    played = stream.pull_audio(frames=2)  # 2 frames = 4 bytes int16
    assert played == b"\x01\x00\x02\x00"
    played = stream.pull_audio(frames=2)
    assert played == b"\x03\x00\x04\x00"
    assert speaker.bytes_played == 8  # both chunks fully played
    speaker.close()


def test_speaker_drains_large_sentence_across_device_callbacks() -> None:
    """Regression: a REST TTS sentence is one large chunk, while PortAudio
    requests many small blocks. No unconsumed tail may be discarded."""
    sd = FakeSoundDevice()
    speaker = TerminalSpeaker(sounddevice=sd)
    stream = sd.opened_streams[0]
    sentence = b"".join(sample.to_bytes(2, "little") for sample in range(1, 13))
    speaker.write(sentence)

    blocks = [stream.pull_audio(frames=3) for _ in range(4)]

    assert b"".join(blocks) == sentence
    assert speaker.bytes_played == len(sentence)
    assert stream.pull_audio(frames=2) == b"\x00" * 4
    assert speaker.bytes_played == len(sentence)
    speaker.close()


def test_speaker_flush_discards_only_unplayed_sentence_tail() -> None:
    sd = FakeSoundDevice()
    speaker = TerminalSpeaker(sounddevice=sd)
    stream = sd.opened_streams[0]
    sentence = b"\x01\x00\x02\x00\x03\x00\x04\x00"
    speaker.write(sentence)

    assert stream.pull_audio(frames=2) == sentence[:4]
    assert speaker.bytes_played == 4
    assert speaker.flush() == 1
    assert stream.pull_audio(frames=2) == b"\x00" * 4
    assert speaker.bytes_played == 4
    speaker.close()


def test_speaker_underrun_fills_silence() -> None:
    sd = FakeSoundDevice()
    speaker = TerminalSpeaker(sounddevice=sd)
    stream = sd.opened_streams[0]
    speaker.write(b"\x01\x00")  # half a frame worth
    played = stream.pull_audio(frames=2)
    assert played == b"\x01\x00\x00\x00"  # silence-padded
    speaker.close()


def test_speaker_keeps_every_chunk_when_generation_outruns_playback() -> None:
    """Realtime 2.1 can queue ~5x realtime. Drop-oldest chops the middle."""
    sd = FakeSoundDevice()
    speaker = TerminalSpeaker(sounddevice=sd, buffer_chunks=3)
    for i in range(5):
        speaker.write(bytes([i]) * 2)
    assert speaker.dropped_chunks == 0
    assert speaker.queued_bytes == 10
    stream = sd.opened_streams[0]
    played = stream.pull_audio(frames=5)
    assert played == b"".join(bytes([i]) * 2 for i in range(5))
    speaker.close()


def test_speaker_keeps_a_realtime_length_burst_past_the_old_chunk_cap() -> None:
    chunk = b"\x01\x00" * 19200
    sd = FakeSoundDevice()
    speaker = TerminalSpeaker(sounddevice=sd, buffer_chunks=200)
    for _ in range(250):
        speaker.write(chunk)
    assert speaker.dropped_chunks == 0
    assert speaker.queued_bytes == 250 * len(chunk)
    speaker.close()


def test_speaker_flush_drops_queued_audio() -> None:
    """Barge-in flush: queued audio is discarded without affecting bytes
    already pulled by the playback callback."""
    sd = FakeSoundDevice()
    speaker = TerminalSpeaker(sounddevice=sd)
    stream = sd.opened_streams[0]
    speaker.write(b"\x01\x00\x02\x00")  # queued, not yet played
    assert speaker.flush() == 1  # one chunk discarded
    assert speaker.flushed_chunks == 1
    assert speaker.queued_bytes == 0
    # nothing left to play
    played = stream.pull_audio(frames=2)
    assert played == b"\x00\x00\x00\x00"  # underrun → silence
    assert speaker.bytes_played == 0  # nothing was consumed before flush
    speaker.close()


def test_speaker_write_after_close_is_noop() -> None:
    sd = FakeSoundDevice()
    speaker = TerminalSpeaker(sounddevice=sd)
    speaker.close()
    speaker.write(b"\x01\x00")  # ignored
    assert speaker.bytes_played == 0


def test_speaker_unavailable_device_is_clear_error() -> None:
    # device list where index 1 (default output) has no output channels
    broken = FakeSoundDevice(
        devices=[{"name": "Mic Only", "max_input_channels": 1, "max_output_channels": 0}],
        default=(0, -1),
    )
    with pytest.raises(AudioDeviceError, match="No usable output device"):
        TerminalSpeaker(sounddevice=broken)


# -- loopback diagnostic -------------------------------------------------------


def test_fake_loopback_diagnostic() -> None:
    """In→out round trip at the canonical boundary: capture bytes replay
    through the speaker queue come out identical (bar silence padding)."""
    sd = FakeSoundDevice()
    mic = TerminalMicrophone(sounddevice=sd)
    speaker = TerminalSpeaker(sounddevice=sd)
    in_stream = sd.opened_streams[0]
    out_stream = sd.opened_streams[1]

    pcm = bytes(range(256)) * 8  # 2048 bytes = 1024 frames
    in_stream.push_audio(pcm)
    captured = mic.read()
    assert captured == pcm

    speaker.write(captured)
    played = out_stream.pull_audio(frames=1024)
    assert played == pcm

    mic.close()
    speaker.close()
