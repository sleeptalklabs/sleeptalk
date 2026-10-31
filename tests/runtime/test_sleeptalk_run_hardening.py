"""Item 1: sleeptalk run hardening — signals, exit codes, FATAL logging,
and the no-prompt-imports regression guard."""

from __future__ import annotations

import ast
import asyncio
import logging
import os
import signal
from pathlib import Path

import pytest

from sleeptalk.cli import EXIT_CONFIG, EXIT_CRASH, EXIT_OK, _run_session, main

PACKAGE_DIR = Path(__file__).resolve().parents[2] / "sleeptalk"

# Modules allowed to touch prompt libraries (never imported by `run`).
INTERACTIVE_MODULES = {"wizard.py"}


class FakeSession:
    def __init__(self, *, crash: bool = False) -> None:
        self.crash = crash
        self.closed = False

    async def run(self) -> None:
        if self.crash:
            raise RuntimeError("boom")
        try:
            await asyncio.Event().wait()  # runs forever, like a real session
        except asyncio.CancelledError:
            self.closed = True  # transport context managers unwound
            raise


# ---------------------------------------------------------------------------
# exit-code contract
# ---------------------------------------------------------------------------


def test_missing_config_exits_2(tmp_path, capsys):
    assert main(["run", "--config", str(tmp_path / "nope.toml")]) == EXIT_CONFIG
    assert "not found" in capsys.readouterr().err


def test_invalid_config_exits_2(tmp_path, capsys):
    path = tmp_path / "sleeptalk.toml"
    path.write_text("[agent]\ntype = \"nope\"\n")
    assert main(["run", "--config", str(path)]) == EXIT_CONFIG
    assert "nope" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_crash_exits_1_with_fatal_log(caplog):
    with caplog.at_level(logging.CRITICAL, logger="sleeptalk"):
        code = await _run_session(FakeSession(crash=True))
    assert code == EXIT_CRASH
    fatal = [r for r in caplog.records if r.levelno >= logging.CRITICAL]
    assert fatal and "FATAL" in fatal[0].message
    assert fatal[0].exc_info is not None  # traceback attached


@pytest.mark.asyncio
async def test_crash_is_said_on_screen_not_only_in_the_log(capsys):
    code = await _run_session(FakeSession(crash=True), log_path="/tmp/me/sleeptalk.log")
    assert code == EXIT_CRASH
    err = capsys.readouterr().err
    assert "RuntimeError: boom" in err and "/tmp/me/sleeptalk.log" in err


@pytest.mark.asyncio
async def test_audio_failure_says_what_to_do(capsys):
    from sleeptalk.transports.terminal_audio import AudioDeviceError

    class NoMic:
        async def run(self) -> None:
            raise AudioDeviceError("Could not open input device 'USB': PortAudio error")

    assert await _run_session(NoMic()) == EXIT_CONFIG
    err = capsys.readouterr().err
    assert "audio unavailable" in err and "libportaudio2" in err


@pytest.mark.skipif(os.name == "nt", reason="loop signal handlers are unix-only")
def test_sigterm_exits_0_and_closes():
    async def drive() -> tuple[int, FakeSession]:
        session = FakeSession()
        task = asyncio.ensure_future(_run_session(session))
        await asyncio.sleep(0.1)
        os.kill(os.getpid(), signal.SIGTERM)
        return await task, session

    code, session = asyncio.run(drive())
    assert code == EXIT_OK
    assert session.closed  # close() path ran: no orphaned agents/ghost voice


@pytest.mark.asyncio
async def test_normal_completion_exits_0():
    class DoneSession:
        async def run(self) -> None:
            return

    assert await _run_session(DoneSession()) == EXIT_OK


# ---------------------------------------------------------------------------
# regression: the run path must never grow a TTY dependency
# ---------------------------------------------------------------------------


def test_run_path_has_no_prompt_imports():
    violations = []
    for source in PACKAGE_DIR.rglob("*.py"):
        if source.name in INTERACTIVE_MODULES or "assets" in source.parts:
            continue
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [a.name for a in node.names]
                root = getattr(node, "module", None) or ""
                if any(n.split(".")[0] in ("questionary", "rich") for n in names) or \
                        root.split(".")[0] in ("questionary", "rich"):
                    violations.append(f"{source.name}:{node.lineno} imports a prompt library")
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id == "input":
                    violations.append(f"{source.name}:{node.lineno} calls input()")
    assert not violations, "\n".join(violations)
