"""Acceptance tests for the sleeptalk composition contract (brief section 7).

* Swap agent.type between two backends: no code changes, both build.
* Swap tts.provider: same.
* Remove a required env var: startup fails naming that exact var.
* The null transport (logs instead of connecting) exists via only a new
  class + registration — it is the permanent test harness.
* A hand-written minimal config (transport + agent only) runs.
"""

from __future__ import annotations

import asyncio

import pytest

from sleeptalk.builder import build_session, run_connectivity_checks
from sleeptalk.config import ConfigError, load_config


def _write(tmp_path, text):
    path = tmp_path / "sleeptalk.toml"
    path.write_text(text)
    return path


MINIMAL = """\
[transport]
type = "null"

[agent]
type = "pi"
"""


def test_minimal_handwritten_config_runs(tmp_path):
    config = load_config(_write(tmp_path, MINIMAL), check_env=False, check_binaries=False)
    session = build_session(config, env={})
    asyncio.run(session.run())  # null transport: logs, exits
    assert session.transport.events[0]["event"] == "session_started"


def test_swap_agent_type_no_code_changes(tmp_path):
    for agent_type, cls in (("pi", "PiRPCClient"), ("claude_code", "ClaudeCodeClient")):
        text = MINIMAL.replace('type = "pi"', f'type = "{agent_type}"')
        # claude_code rejects pi-only fields; rewrite cleanly.
        text = MINIMAL.replace(
            '[agent]\ntype = "pi"', f'[agent]\ntype = "{agent_type}"'
        )
        config = load_config(
            _write(tmp_path, text), check_env=False, check_binaries=False
        )
        session = build_session(config, env={})
        assert type(session.agent).__name__ == cls


def test_swap_tts_provider_no_code_changes(tmp_path):
    for provider, cls in (
        ("cartesia", "CartesiaTTS"),
        ("elevenlabs", "ElevenLabsTTS"),
    ):
        text = MINIMAL + f'\n[tts]\nprovider = "{provider}"\nvoice_id = "v"\n'
        config = load_config(
            _write(tmp_path, text), check_env=False, check_binaries=False
        )
        session = build_session(config, env={})
        assert type(session.tts).__name__ == cls


def test_missing_env_var_fails_naming_exact_var(tmp_path):
    text = MINIMAL.replace('type = "null"', 'type = "discord"')
    path = _write(tmp_path, text)
    with pytest.raises(ConfigError) as excinfo:
        load_config(path, env={}, check_binaries=False)
    message = str(excinfo.value)
    assert "DISCORD_BOT_TOKEN" in message
    assert "DEEPGRAM_API_KEY" in message  # all missing vars in one pass
    assert "CARTESIA_API_KEY" in message


def test_null_transport_is_just_a_registration():
    """The null transport was added as: new class + one register() call.
    Prove the pattern: register another fake at runtime and build it."""
    from sleeptalk.registries import Registry

    calls = []

    class LogTransport:
        def __init__(self, config, env):
            pass

        async def run(self, session):
            calls.append("ran")

    registry = Registry("transport")
    registry.register("log", lambda config, env: LogTransport(config, env))
    transport = registry.build("log", None, {})
    asyncio.run(transport.run(None))
    assert calls == ["ran"]


def test_config_driven_tts_fallback(tmp_path):
    text = MINIMAL + (
        '\n[tts]\nprovider = "cartesia"\nvoice_id = "v1"\n'
        '\n[[tts.fallback]]\nprovider = "elevenlabs"\nvoice_id = "v2"\n'
    )
    config = load_config(_write(tmp_path, text), check_env=False, check_binaries=False)
    session = build_session(config, env={})
    assert [type(t).__name__ for t in [session.tts, *session.tts_fallbacks]] == [
        "CartesiaTTS",
        "ElevenLabsTTS",
    ]


def test_connectivity_checks_aggregate_failures(tmp_path):
    from sleeptalk.registries import STT_PROVIDERS

    text = MINIMAL
    config = load_config(_write(tmp_path, text), check_env=False, check_binaries=False)

    async def failing_check(section, env):
        raise RuntimeError("HTTP 401")

    registration = STT_PROVIDERS.get("deepgram")
    STT_PROVIDERS._registrations["deepgram"] = type(registration)(
        **{**registration.__dict__, "check": failing_check}
    )
    try:
        with pytest.raises(ConfigError, match="deepgram connectivity check failed"):
            asyncio.run(run_connectivity_checks(config, env={}))
    finally:
        STT_PROVIDERS._registrations["deepgram"] = registration


def test_committed_configs_load(tmp_path):
    """The tracked examples parse, validate, and resolve discriminators."""
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    paths = [
        repo_root / "sleeptalk.example.toml",
        repo_root / "config" / "profiles" / "example.toml",
    ]
    for path in paths:
        config = load_config(path, check_env=False, check_binaries=False)
        assert config.transport.type and config.agent.type
