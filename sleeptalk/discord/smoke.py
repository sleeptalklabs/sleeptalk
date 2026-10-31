"""No-connect Discord startup smoke for a wheel-only install."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_FORBIDDEN = ("app", "server")


def imported_server_modules() -> list[str]:
    return sorted(
        name
        for name in sys.modules
        if name in _FORBIDDEN or name.startswith("app.") or name.startswith("server.")
    )


def smoke_discord_startup(
    config_path: Path,
    *,
    root_path: Path | None = None,
    env: dict[str, str] | None = None,
) -> list[str]:
    from sleeptalk.builder import build_session
    from sleeptalk.config import load_resolved_config
    from sleeptalk.discord import DiscordEnv, PiDiscordBot
    from sleeptalk.transports.discord import DiscordTransport

    env = dict(os.environ if env is None else env)
    config = load_resolved_config(
        config_path,
        root_path=root_path,
        env=env,
        check_env=False,
        check_binaries=False,
    )
    env.setdefault(config.transport.token_env, "wheel-smoke-token")
    transport = config.transport
    if transport.authorization_required and not transport.operator_ids:
        # The example profile ships closed with blank IDs; this smoke never
        # connects, so placeholder IDs let startup run to command registration.
        config = config.model_copy(update={"transport": transport.model_copy(update={
            "guild_id": transport.guild_id or "1",
            "allowed_channel_ids": transport.allowed_channel_ids or "1",
            "operator_ids": "1",
        })})
    bot = PiDiscordBot(
        config, DiscordEnv.from_mapping(config, env),
        realtime_session=build_session(config, env=env),
    )
    group = bot.register_voice_commands()
    leaked = imported_server_modules()
    if leaked:
        raise RuntimeError("sleeptalk imported server/ or app.*: " + ", ".join(leaked))
    _ = DiscordTransport
    return [command.name for command in group.commands]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sleeptalk.discord.smoke")
    parser.add_argument("--config", required=True)
    parser.add_argument("--root", default=None)
    args = parser.parse_args(argv)
    commands = smoke_discord_startup(
        Path(args.config),
        root_path=Path(args.root) if args.root else None,
    )
    print("discord-smoke: commands=" + ",".join(commands))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
