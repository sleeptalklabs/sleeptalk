"""Runtime-standalone audit: the Discord transport is package-owned.

Covers: bare package imports (no server on sys.path); constructing and
running the transport against a fake session without app.*; extras declared.
"""

from __future__ import annotations

import asyncio
import types

from sleeptalk.config import AgentConfig, SleeptalkConfig, STTConfig, TransportConfig, TTSConfig
from sleeptalk.discord import PiDiscordBot
from sleeptalk.transports.discord import DiscordTransport, transport_summary


class FakeConfig:
    class _Section:
        def __init__(self, **kwargs):
            for key, value in kwargs.items():
                setattr(self, key, value)

    def __init__(self):
        self.transport = self._Section(
            token_env="DISCORD_BOT_TOKEN",
            guild_id="1001",
            client_id="c",
            allowed_channel_ids="",
            native_voice_enabled=True,
            voice_only=False,
            presence_auto_join=False,
            opus_library="",
        )
        self.agent = self._Section(
            type="pi", name="n", slug="s", provider="pi", model="",
            gateway_url="", gateway_session_id="", workspace_dir="",
            pi_bin="", persona_instructions="", provider_slug="",
            participation_mode="conversationalist",
            addressable_name="",
            proactive_triggers=(),
        )
        self.stt = self._Section(model="", language="", keyterms="", base_url="")
        self.stt.rest_timeout_seconds = 8.0
        self.tts = self._Section(
            provider="cartesia", model="", voice_id="v", speed=1.0,
            output_format="", base_url="",
        )


def _config() -> SleeptalkConfig:
    return SleeptalkConfig(
        transport=TransportConfig(authorization_required=False, token_env="DISCORD_BOT_TOKEN", guild_id="1001", opus_library=""),
        agent=AgentConfig(type="pi", name="n", slug="s"),
        stt=STTConfig(),
        tts=TTSConfig(voice_id="v"),
    )


def _without_server(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name.startswith("app.") or name == "app":
            raise ImportError(f"No module named {name!r} (simulated wheel install)")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", blocked)


def test_transport_importable_without_server() -> None:
    transport = DiscordTransport(FakeConfig(), {})
    assert transport.config is not None
    assert transport_summary(FakeConfig())["guild_id"] == "1001"


def test_transport_runs_without_server(monkeypatch) -> None:
    _without_server(monkeypatch)

    async def fake_start(self, token):
        self.token = token

    monkeypatch.setattr("sleeptalk.transports.discord.load_opus", lambda _lib: None)
    monkeypatch.setattr(PiDiscordBot, "start", fake_start)
    class IdleRoute:
        closes = 0

        def active_turn(self):
            return None

        async def close(self):
            self.closes += 1

    route = IdleRoute()
    session = types.SimpleNamespace(
        policy=None,
        agent=None,
        stt=None,
        streaming_stt=None,
        tts=None,
        tts_fallbacks=[],
        usage_meter=None,
        route=route,
    )
    asyncio.run(DiscordTransport(_config(), {"DISCORD_BOT_TOKEN": "tok"}).run(session))
    assert route.closes == 1


def test_extras_declared_for_optional_integration() -> None:
    from pathlib import Path

    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    text = pyproject.read_text(encoding="utf-8")
    assert "[project.optional-dependencies]" in text
    assert "discord = [" in text
    base = text.split("[project.optional-dependencies]")[0]
    assert "discord.py" not in base


def test_no_server_imports_at_module_scope() -> None:
    import sleeptalk.transports.discord as mod
    import inspect
    import ast

    source = inspect.getsource(mod)
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith("app."), (
                f"module-scope server import: {node.module}"
            )
        if isinstance(node, ast.Import):
            assert not any(a.name.startswith("app.") for a in node.names), (
                "module-scope server import"
            )


def test_legacy_invert_symbols_are_gone() -> None:
    import sleeptalk.transports.discord as mod

    assert not hasattr(mod, "DiscordServerIntegrationRequired")
    assert not hasattr(mod, "resolve_discord_runtime")
