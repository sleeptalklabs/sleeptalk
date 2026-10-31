"""Black-box terminal proof for the RealtimeSession cutover.

The design of this acceptance boundary was independently approved by
codex-gpt5-terminal-scope-20260822 before implementation was claimed.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import types

from tests.runtime.test_terminal_audio import FakeSoundDevice
from sleeptalk import cli
from sleeptalk.config import LogConfig
from sleeptalk.transports.terminal import TerminalTransport
from sleeptalk.voice_loop.vad import VadObservation, pcm_rms


SPEECH_FRAME = b"\x31\x42" * 2_400
SILENCE_FRAME = b"\x00\x00" * 2_400
REPLY_PCM = b"\x53\x64" * 600


class _RmsListenVad:
    def __init__(self, config=None) -> None:
        self.config = config

    async def observe(self, pcm: bytes):
        return VadObservation(is_speech=pcm_rms(pcm) >= 500)

    def reset(self) -> None:
        return None


class StrictKernel:
    """Only the public session API exists; legacy component access explodes."""

    def __init__(self, transport: TerminalTransport, sounddevice: FakeSoundDevice) -> None:
        self.transport = transport
        self.sounddevice = sounddevice
        self.calls: list[tuple[str, object]] = []
        self.output = None
        self.closed = 0
        self.turn_ids: list[int] = []

    @property
    def route(self):  # pragma: no cover - failure sentinel
        raise AssertionError("terminal accessed the private route")

    @property
    def agent(self):  # pragma: no cover - failure sentinel
        raise AssertionError("terminal accessed the agent")

    @property
    def stt(self):  # pragma: no cover - failure sentinel
        raise AssertionError("terminal accessed STT")

    @property
    def tts(self):  # pragma: no cover - failure sentinel
        raise AssertionError("terminal accessed TTS")

    async def run(self) -> None:
        self.calls.append(("run", None))
        task = asyncio.create_task(self.transport.run(self))
        await _until(lambda: len(self.sounddevice.opened_streams) == 2)
        input_stream, output_stream = self.sounddevice.opened_streams
        # Acoustic-only kernel has no turn_completion_signal. A short
        # utterance commits on the assembler floor (~80-120 ms). Extra
        # silence frames keep the wait honest if Smart Turn or a longer
        # bucket holds the floor.
        for frame in [SPEECH_FRAME] * 5 + [SILENCE_FRAME] * 24:
            input_stream.push_audio(frame)
            await asyncio.sleep(0.055)
        await _until(lambda: any(name == "run_turn" for name, _ in self.calls), timeout=3.0)
        await _until(
            lambda: self.transport.audio_output is not None
            and self.transport.audio_output.accounting().bytes_generated == len(REPLY_PCM)
        )
        output_stream.pull_audio(frames=len(REPLY_PCM) // 2)
        await self.transport.close()
        await task
        await self.aclose()

    def set_audio_output(self, output) -> None:
        self.output = output
        self.calls.append(("set_audio_output", output))

    async def prepare(self) -> None:
        self.calls.append(("prepare", None))

    async def append_audio(self, pcm: bytes, *, participant_id: int = 0) -> None:
        self.calls.append(("append_audio", pcm))

    async def run_turn(self, *, pcm: bytes, extras=None):
        self.calls.append(("run_turn", pcm))
        turn_id = len(self.turn_ids) + 1
        self.turn_ids.append(turn_id)
        await self.output.write(REPLY_PCM, turn_id)

    async def interrupt_presentation(self) -> None:
        self.calls.append(("interrupt_presentation", None))

    async def aclose(self) -> None:
        self.closed += 1
        self.calls.append(("aclose", None))


async def _until(predicate, timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met within timeout")


def _config():
    return types.SimpleNamespace(
        transport=types.SimpleNamespace(type="terminal"),
        agent=types.SimpleNamespace(type="pi", slug="pi-terminal"),
        stt=types.SimpleNamespace(provider="fake-stt"),
        tts=types.SimpleNamespace(provider="fake-tts"),
        policy=types.SimpleNamespace(
            endpointing="server_vad", barge_in=True, fillers=False
        ),
        log=LogConfig(),
    )


def test_cli_terminal_crosses_only_public_kernel_boundary(tmp_path, monkeypatch) -> None:
    """The documented CLI path moves exact mic/output bytes through the kernel."""
    transport = TerminalTransport(config=_config(), env={})
    sounddevice = FakeSoundDevice()
    transport.use_sounddevice(sounddevice)
    transport.use_listen_vad(_RmsListenVad)
    kernel = StrictKernel(transport, sounddevice)
    monkeypatch.setattr(cli, "load_resolved_config", lambda path: _config())
    monkeypatch.setattr(cli, "build_session", lambda config: kernel)

    import sleeptalk.logging_setup as logging_setup

    monkeypatch.setattr(
        logging_setup, "setup_file_logging", lambda **kwargs: tmp_path / "terminal.log"
    )
    profile = tmp_path / "config" / "profiles" / "pi.toml"
    result = cli.main(["run", "--config", str(profile), "--skip-checks"])

    assert result == cli.EXIT_OK
    turn_pcms = [pcm for name, pcm in kernel.calls if name == "run_turn"]
    assert len(turn_pcms) == 1
    # Trailing hush is trimmed to TERMINAL_STREAM_TAIL_MS; require the spoken
    # prefix rather than a brittle full-buffer equality.
    assert turn_pcms[0].startswith(SPEECH_FRAME * 5)
    assert [name for name, _ in kernel.calls[:3]] == [
        "run",
        "set_audio_output",
        "prepare",
    ]
    assert kernel.turn_ids == [1]
    assert kernel.closed == 1
    accounting = transport.audio_output.accounting()
    assert accounting.bytes_generated == len(REPLY_PCM)
    assert accounting.bytes_written == len(REPLY_PCM)
    assert all(stream.closed for stream in sounddevice.opened_streams)


def test_terminal_source_cannot_revive_direct_route_runtime() -> None:
    """Static tripwire for the exact legacy calls removed by this migration."""
    import sleeptalk.transports.terminal as terminal_module

    tree = ast.parse(inspect.getsource(terminal_module))
    forbidden_methods = {"bind", "active_turn", "cancel_turn"}
    accessed = {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    }
    assert not (forbidden_methods & accessed)

    imports = {
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    assert "sleeptalk.routes.base" not in imports
    assert "sleeptalk.routes.cascade" not in imports
