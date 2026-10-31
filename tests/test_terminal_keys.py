import os
import threading
import termios
import tty
from types import SimpleNamespace

import sleeptalk.transports.terminal as terminal_module
from sleeptalk.policy import VoicePolicy
from sleeptalk.transports.terminal import TerminalTransport, _read_terminal_key, _read_tty
from sleeptalk.transports.terminal_controls import TerminalKey


def _read_from(data: str):
    buf = [data]

    def read(n: int) -> str:
        chunk = buf[0][:n]
        buf[0] = buf[0][n:]
        return chunk

    return read


def test_read_terminal_key_quit() -> None:
    assert _read_terminal_key(_read_from("q")) is TerminalKey.QUIT


def test_read_terminal_key_settings_and_stop_are_distinct() -> None:
    assert _read_terminal_key(_read_from("s")) is TerminalKey.SETTINGS
    assert _read_terminal_key(_read_from(" ")) is TerminalKey.STOP


def test_read_terminal_key_mute() -> None:
    assert _read_terminal_key(_read_from("m")) is TerminalKey.MUTE


def test_read_terminal_key_decodes_menu_navigation() -> None:
    assert _read_terminal_key(_read_from("\x1b[A")) is TerminalKey.UP
    assert _read_terminal_key(_read_from("\x1b[B")) is TerminalKey.DOWN
    assert _read_terminal_key(_read_from("\x1bOA")) is TerminalKey.UP
    assert _read_terminal_key(_read_from("\x1bOB")) is TerminalKey.DOWN
    assert _read_terminal_key(_read_from("\r")) is TerminalKey.ENTER
    assert _read_terminal_key(_read_from("\n")) is TerminalKey.ENTER


def test_read_terminal_key_removes_scattered_setting_shortcuts() -> None:
    for encoded in ("n", "b", "\x1b[Z", "\x1b[1;2Z"):
        assert _read_terminal_key(_read_from(encoded)) is None


def test_read_terminal_key_other_csi_is_ignored() -> None:
    assert _read_terminal_key(_read_from("\x1b[C")) is None
    assert _read_terminal_key(_read_from("\x1b[9;1u")) is None


def test_read_terminal_key_lone_esc_times_out() -> None:
    reader, writer = os.pipe()
    try:
        os.write(writer, b"\x1b")

        def read(n: int) -> str:
            return os.read(reader, n).decode()

        assert _read_terminal_key(read, fileno=reader) is TerminalKey.ESCAPE
    finally:
        os.close(reader)
        os.close(writer)


def test_read_tty_reads_one_byte() -> None:
    reader, writer = os.pipe()
    try:
        os.write(writer, b"s")
        assert _read_tty(reader, 1) == "s"
    finally:
        os.close(reader)
        os.close(writer)


def test_arrow_burst_through_os_read() -> None:
    reader, writer = os.pipe()
    try:
        os.write(writer, b"\x1b[A")

        def read(n: int) -> str:
            return os.read(reader, n).decode("latin-1")

        assert _read_terminal_key(read, fileno=reader) is TerminalKey.UP
    finally:
        os.close(reader)
        os.close(writer)


def test_start_keys_schedules_the_first_decoded_key_on_the_event_loop(
    monkeypatch,
) -> None:
    scheduled = threading.Event()
    calls: list[tuple[object, tuple[object, ...]]] = []

    class FakeLoop:
        def call_soon_threadsafe(self, callback: object, *args: object) -> None:
            calls.append((callback, args))
            assert transport._keys_stop is not None
            transport._keys_stop.set()
            scheduled.set()

    class FakeStdin:
        @staticmethod
        def isatty() -> bool:
            return True

        @staticmethod
        def fileno() -> int:
            return 91

    policy = VoicePolicy()
    config = SimpleNamespace(
        transport=SimpleNamespace(input_device="", output_device="", capture_mode=""),
        policy=policy,
    )
    transport = TerminalTransport(config, {})
    session = object()
    monkeypatch.setattr(terminal_module.sys, "stdin", FakeStdin())
    monkeypatch.setattr(
        terminal_module.asyncio, "get_running_loop", lambda: FakeLoop()
    )
    monkeypatch.setattr(termios, "tcgetattr", lambda _fd: ())
    monkeypatch.setattr(termios, "tcsetattr", lambda _fd, _when, _old: None)
    monkeypatch.setattr(tty, "setcbreak", lambda _fd: None)
    monkeypatch.setattr(
        terminal_module.select,
        "select",
        lambda _read, _write, _error, _timeout: ([91], [], []),
    )
    monkeypatch.setattr(
        terminal_module,
        "_read_terminal_key",
        lambda _read, *, fileno: TerminalKey.SETTINGS,
    )

    transport._start_keys(session)
    thread = transport._keys_thread
    try:
        assert scheduled.wait(1.0)
    finally:
        transport._stop_keys()
        if thread is not None:
            thread.join(timeout=1.0)

    assert calls == [
        (transport._handle_terminal_key, (session, TerminalKey.SETTINGS))
    ]
