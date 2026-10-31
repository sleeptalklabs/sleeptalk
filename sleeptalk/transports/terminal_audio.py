"""Terminal audio device I/O (r2-terminal-device-io).

The audio transport for the terminal surface: microphone capture and
speaker playback at the session kernel's canonical format, with device
enumeration, explicit/default device selection, and clear errors when no
device is available.

Canonical format — matches the session kernel's audio boundary (the
Deepgram STT seam): **48 kHz, int16, mono** PCM. Capture and playback both
run at this rate so a terminal session feeds the cascade route the same
bytes Discord does.

Capture and playback use different queues:

- capture uses a drop-oldest ring (``collections.deque(maxlen=…)``). A
  slow consumer loses the oldest mic audio and never grows memory.
- playback keeps every queued chunk. Realtime can generate several
  times faster than the speaker. Drop-oldest there chops the middle of
  a long answer into 400 ms slices.

``sounddevice`` is imported lazily and injectable for tests: pass a fake
module as ``sounddevice=`` (the fakes in ``test_terminal_audio.py`` drive
callbacks without any audio hardware).
"""

from __future__ import annotations

import sys
import threading
from collections import deque
from typing import Any, Protocol

__all__ = [
    "AudioDeviceError",
    "CANONICAL_CHANNELS",
    "CANONICAL_DTYPE",
    "CANONICAL_SAMPLE_RATE",
    "DeviceListing",
    "TerminalMicrophone",
    "TerminalSpeaker",
    "default_devices",
    "list_devices",
    "prefer_low_latency_hostapi",
]

#: Kernel audio boundary: int16 mono 48 kHz (Deepgram STT seam).
CANONICAL_SAMPLE_RATE = 48_000
CANONICAL_CHANNELS = 1
CANONICAL_DTYPE = "int16"

#: Capture ring size (chunks). ~10 s of 50 ms chunks; drop-oldest.
_CAPTURE_BUFFER_CHUNKS = 200


class AudioDeviceError(RuntimeError):
    """No usable audio device, or the selected device is unavailable."""

    def __init__(self, message: str, *, device: Any = None) -> None:
        super().__init__(message)
        self.device = device


MIC_ACCESS_HINT = (
    "Grant microphone access: System Settings → Privacy & Security → "
    "Microphone → enable your terminal app (and Input Monitoring if "
    "using a TTY), then restart the terminal."
)


def audio_recovery(exc: AudioDeviceError) -> str:
    """One line telling the caller how to get their audio back."""
    message = str(exc).lower()
    if (
        "permission" in message
        or "not authorized" in message
        or "audio input" in message
    ):
        return MIC_ACCESS_HINT
    if "portaudio" in message:
        return (
            "Install the PortAudio library: sudo apt install libportaudio2 "
            "(Debian/Ubuntu), or brew install portaudio (macOS)."
        )
    if "sounddevice" in message:
        return (
            "Install audio support: pip install sounddevice (requires PortAudio)."
        )
    return "Check that a microphone and speaker are connected and not held by another app."


class _InputStream(Protocol):
    def start(self) -> None: ...

    def stop(self) -> None: ...

    def close(self) -> None: ...


class _OutputStream(Protocol):
    def start(self) -> None: ...

    def stop(self) -> None: ...

    def close(self) -> None: ...


def _preferred_hostapi_needles(platform: str = "") -> tuple[str, ...]:
    system = platform or sys.platform
    if system == "darwin":
        return ("core audio",)
    if system == "win32":
        return ("windows wasapi", "wasapi")
    return ("pipewire", "pulseaudio", "pulse")


def prefer_low_latency_hostapi(sd: Any, *, platform: str = "") -> int | None:
    """Pin PortAudio to WASAPI / Core Audio / Pulse when the host offers it."""
    query = getattr(sd, "query_hostapis", None)
    default = getattr(sd, "default", None)
    if not callable(query) or default is None:
        return None
    try:
        apis = list(query())
    except Exception:
        return None
    named: list[tuple[int, str]] = []
    for index, api in enumerate(apis):
        if isinstance(api, dict):
            name = str(api.get("name", ""))
        else:
            name = str(getattr(api, "name", ""))
        named.append((index, name.lower()))
    for needle in _preferred_hostapi_needles(platform):
        for index, name in named:
            if needle in name:
                try:
                    default.hostapi = index
                except Exception:
                    return None
                return index
    return None


def _load_sounddevice(injected: Any = None) -> Any:
    if injected is not None:
        return injected
    try:
        import sounddevice  # type: ignore[import-not-found]
    except ImportError as exc:
        raise AudioDeviceError(
            "Terminal audio requires the 'sounddevice' package. "
            "Install it (pip install sounddevice) or use an audio-free surface."
        ) from exc
    except OSError as exc:
        # sounddevice is installed but the PortAudio library it loads isn't.
        raise AudioDeviceError(f"Terminal audio needs the PortAudio library ({exc}).") from exc
    prefer_low_latency_hostapi(sounddevice)
    return sounddevice


def list_devices(sounddevice: Any = None) -> list[dict[str, Any]]:
    """Enumerate audio devices. ``is_default`` marks host defaults."""
    sd = _load_sounddevice(sounddevice)
    try:
        devices = list(sd.query_devices())
    except Exception as exc:  # PortAudio failures vary by platform.
        raise AudioDeviceError(f"Could not enumerate audio devices: {exc}") from exc
    default_input = getattr(sd.default, "device", (None, None))[0]
    default_output = getattr(sd.default, "device", (None, None))[1]
    listing: list[dict[str, Any]] = []
    for index, device in enumerate(devices):
        max_inputs = int(device.get("max_input_channels", 0) or 0)
        max_outputs = int(device.get("max_output_channels", 0) or 0)
        listing.append(
            {
                "index": index,
                "name": device.get("name", ""),
                "inputs": max_inputs,
                "outputs": max_outputs,
                "is_default_input": index == default_input,
                "is_default_output": index == default_output,
            }
        )
    return listing


def default_devices(sounddevice: Any = None) -> dict[str, Any]:
    """The host default input/output devices, with clear errors if absent."""
    sd = _load_sounddevice(sounddevice)
    try:
        default_input, default_output = sd.default.device
    except Exception as exc:
        raise AudioDeviceError(f"Could not read default audio devices: {exc}") from exc
    if default_input is None or default_input < 0:
        raise AudioDeviceError("No default input (microphone) device available.")
    if default_output is None or default_output < 0:
        raise AudioDeviceError("No default output (speaker) device available.")
    return {"input": default_input, "output": default_output}


class DeviceListing:
    """Device selection helper: default or explicit, with usable checks."""

    def __init__(self, sounddevice: Any = None) -> None:
        self._sd = _load_sounddevice(sounddevice)

    def resolve_input(self, name_or_index: Any = None) -> Any:
        return self._resolve(name_or_index, kind="input")

    def resolve_output(self, name_or_index: Any = None) -> Any:
        return self._resolve(name_or_index, kind="output")

    def _resolve(self, name_or_index: Any, *, kind: str) -> Any:
        devices = list_devices(self._sd)
        if name_or_index is None:
            for device in devices:
                if kind == "input" and device["is_default_input"]:
                    return device["index"]
                if kind == "output" and device["is_default_output"]:
                    return device["index"]
            usable = [d for d in devices if d[f"{kind}s"] > 0]
            if not usable:
                raise AudioDeviceError(f"No usable {kind} device found.")
            return usable[0]["index"]
        for device in devices:
            if device["index"] == name_or_index or (
                isinstance(name_or_index, str)
                and device["name"].lower() == name_or_index.lower()
            ):
                if device[f"{kind}s"] <= 0:
                    raise AudioDeviceError(
                        f"Device '{device['name']}' has no {kind} channels.",
                        device=device,
                    )
                return device["index"]
        raise AudioDeviceError(f"Unknown {kind} device: {name_or_index!r}")


class TerminalMicrophone:
    """Canonical-format capture with a bounded, drop-oldest ring."""

    def __init__(
        self,
        *,
        device: Any = None,
        blocksize: int = 2_400,  # 50 ms at 48 kHz
        buffer_chunks: int = _CAPTURE_BUFFER_CHUNKS,
        sounddevice: Any = None,
    ) -> None:
        self.sample_rate = CANONICAL_SAMPLE_RATE
        self.channels = CANONICAL_CHANNELS
        self.blocksize = blocksize
        self._dropped_chunks = 0
        self._closed = False
        self._lock = threading.Lock()
        self._ring: deque[bytes] = deque(maxlen=buffer_chunks)
        # Audio captured before the first non-zero sample. macOS gives a
        # terminal that was denied the microphone exact zeros, not an error.
        self._zero_bytes = 0
        self._heard = False
        sd = _load_sounddevice(sounddevice)
        index = DeviceListing(sd).resolve_input(device)
        try:
            self._stream: _InputStream = sd.RawInputStream(
                samplerate=CANONICAL_SAMPLE_RATE,
                channels=CANONICAL_CHANNELS,
                dtype=CANONICAL_DTYPE,
                blocksize=blocksize,
                device=index,
                callback=self._on_audio,
            )
            self._stream.start()
        except Exception as exc:
            raise AudioDeviceError(
                f"Could not open input device {device!r}: {exc}", device=device
            ) from exc
        self.device_index = index

    def _on_audio(self, data, frames, time, status) -> None:  # noqa: ANN001
        if self._closed:
            return
        chunk = bytes(data)
        if not self._heard:
            if chunk.strip(b"\0"):
                self._heard = True
            else:
                self._zero_bytes += len(chunk)
        with self._lock:
            ring: deque[bytes] = self._ring
            if len(ring) == ring.maxlen:
                self._dropped_chunks += 1  # drop oldest under backpressure
            ring.append(chunk)

    def read(self) -> bytes:
        """Drain all captured audio (oldest first)."""
        with self._lock:
            chunks = list(self._ring)
            self._ring.clear()
        return b"".join(chunks)

    @property
    def dropped_chunks(self) -> int:
        """Chunks dropped because the consumer was too slow (bounded)."""
        return self._dropped_chunks

    @property
    def silent_seconds(self) -> float:
        """Seconds of exact digital silence since the stream opened; 0 once anything was heard."""
        if self._heard:
            return 0.0
        return self._zero_bytes / (self.sample_rate * self.channels * 2)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._stream.stop()
        self._stream.close()


class TerminalSpeaker:
    """Canonical-format playback. The queue keeps every chunk until play or flush."""

    def __init__(
        self,
        *,
        device: Any = None,
        buffer_chunks: int = 200,
        sounddevice: Any = None,
    ) -> None:
        del buffer_chunks
        self.sample_rate = CANONICAL_SAMPLE_RATE
        self.channels = CANONICAL_CHANNELS
        self._dropped_chunks = 0
        self._flushed_chunks = 0
        self._closed = False
        self._lock = threading.Lock()
        self._queue: deque[bytes] = deque()
        self._played = 0
        sd = _load_sounddevice(sounddevice)
        index = DeviceListing(sd).resolve_output(device)
        try:
            self._stream: _OutputStream = sd.RawOutputStream(
                samplerate=CANONICAL_SAMPLE_RATE,
                channels=CANONICAL_CHANNELS,
                dtype=CANONICAL_DTYPE,
                device=index,
                callback=self._on_play,
            )
            self._stream.start()
        except Exception as exc:
            raise AudioDeviceError(
                f"Could not open output device {device!r}: {exc}", device=device
            ) from exc
        self.device_index = index

    def _on_play(self, outdata, frames, time, status) -> None:  # noqa: ANN001
        needed = frames * 2  # int16 mono → bytes
        data = bytearray()
        with self._lock:
            while self._queue and len(data) < needed:
                chunk = self._queue[0]
                take = min(len(chunk), needed - len(data))
                data.extend(chunk[:take])
                self._played += take
                if take == len(chunk):
                    self._queue.popleft()
                else:
                    # A provider may deliver a whole sentence as one WAV/PCM
                    # chunk. Preserve the unconsumed tail for later device
                    # callbacks instead of dropping nearly the entire answer.
                    self._queue[0] = chunk[take:]
        if len(data) < needed:
            data.extend(b"\x00" * (needed - len(data)))  # underrun → silence
        outdata[:needed] = data[:needed]

    def write(self, pcm: bytes) -> None:
        """Queue PCM for playback. Older chunks stay until the device plays them or ``flush``."""
        if self._closed or not pcm:
            return
        with self._lock:
            self._queue.append(pcm)

    def flush(self) -> int:
        """Drop ALL queued audio (barge-in interruption). Returns the number
        of queued chunks discarded; already-played bytes stay accounted in
        :attr:`bytes_played`."""
        with self._lock:
            flushed = len(self._queue)
            self._queue.clear()
            self._flushed_chunks += flushed
        return flushed

    @property
    def dropped_chunks(self) -> int:
        return self._dropped_chunks

    @property
    def flushed_chunks(self) -> int:
        """Chunks dropped by :meth:`flush` (barge-in)."""
        return self._flushed_chunks

    @property
    def bytes_played(self) -> int:
        with self._lock:
            return self._played

    @property
    def queued_bytes(self) -> int:
        with self._lock:
            return sum(len(chunk) for chunk in self._queue)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._stream.stop()
        self._stream.close()
