from pathlib import Path

import pytest

pytest.importorskip("discord")

from sleeptalk.discord.smoke import smoke_discord_startup


def test_discord_smoke_loads_example_and_registers_commands() -> None:
    root = Path(__file__).resolve().parents[1]
    commands = smoke_discord_startup(
        root / "config" / "profiles" / "example.toml",
        root_path=root / "sleeptalk.example.toml",
        env={"MY_BOT_DISCORD_BOT_TOKEN": "wheel-smoke-token"},
    )
    assert {"ask", "speak", "join", "leave", "status", "settings"}.issubset(commands)


def test_example_profile_is_closed_and_refuses_to_start_without_ids() -> None:
    from sleeptalk.config import load_resolved_config
    from sleeptalk.discord.access import DiscordAccess

    root = Path(__file__).resolve().parents[1]
    config = load_resolved_config(
        root / "config" / "profiles" / "example.toml",
        root_path=root / "sleeptalk.example.toml",
        env={}, check_env=False, check_binaries=False,
    )
    access = DiscordAccess.from_surface(config.transport)
    assert access.required
    with pytest.raises(ValueError, match="authorization_required = false"):
        access.validate()
