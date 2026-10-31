"""Factories and probes for bring-your-own-agent registrations.

``command`` runs a local program that speaks Bridge Lite or ACP over stdio.
``http`` posts each turn to a web endpoint and speaks the streamed reply.
``bridge`` connects to a remote Agent Bridge v1 server.

See ``docs/bring-your-own-agent.md``.
"""

from __future__ import annotations

import os
from typing import Any

from sleeptalk.agents.bridge_lite import (
    CommandAgent,
    CommandAgentOptions,
    HttpAgent,
    HttpAgentOptions,
    command_environment,
    command_setup_hint,
    probe_command,
    probe_http,
)

COMMON_FIELDS = frozenset(
    {"options", "name", "slug", "persona_instructions", "workspace_dir", "gateway_session_id"}
)
COMMAND_FIELDS = COMMON_FIELDS | {"permission_mode"}
HTTP_FIELDS = COMMON_FIELDS
BRIDGE_FIELDS = COMMON_FIELDS

ACP_PERMISSION_DEFAULT = "pending"


def _cwd(config: Any) -> str:
    from sleeptalk.defaults import _agent_cwd

    return _agent_cwd(config)


def _required_token(config: Any) -> dict[str, str]:
    options = getattr(config, "options", None) or {}
    name = str(options.get("token_env") or "").strip() if isinstance(options, dict) else ""
    if not name:
        return {}
    return {name: f"export {name}=<token issued by your agent service> before starting Sleeptalk"}


def command_binary(config: Any) -> str:
    """The program a command agent runs, so setup, doctor, and call start can
    report a missing agent before a call link is sent."""

    options = getattr(config, "options", None) or {}
    command = options.get("command") if isinstance(options, dict) else None
    if not isinstance(command, list) or not command or not str(command[0]).strip():
        return ""
    program = os.path.expanduser(str(command[0]))
    if os.sep in program and not os.path.isabs(program):
        try:
            program = os.path.join(_cwd(config), program)
        except Exception:  # noqa: BLE001 - an unresolvable workspace is reported elsewhere
            return program
    return program


def http_required_env(config: Any) -> dict[str, str]:
    return _required_token(config)


def bridge_required_env(config: Any) -> dict[str, str]:
    return _required_token(config)


def build_command_agent(config: Any, env: dict[str, str]) -> Any:
    options = CommandAgentOptions.model_validate(config.options or {})
    cwd = _cwd(config)
    name = str(getattr(config, "slug", "") or "command")
    if options.protocol == "acp":
        return build_command_acp_agent(config, env, options=options, cwd=cwd, name=name)
    return CommandAgent(options, env=env, cwd=cwd, name=name)


def build_command_acp_agent(
    config: Any,
    env: dict[str, str],
    *,
    options: CommandAgentOptions,
    cwd: str,
    name: str,
) -> Any:
    from sleeptalk.agents.acp import AcpClient
    from sleeptalk.agents.profiles import AcpProfile

    program = os.path.expanduser(options.command[0])
    if os.sep in program and not os.path.isabs(program):
        program = os.path.join(cwd, program)
    spawn_env = command_environment(options, env)

    class CommandAcpClient(AcpClient):
        async def _spawn(self, args: list[str], env: dict[str, str] | None = None) -> None:
            await super()._spawn(args, env=spawn_env)

    mode = (getattr(config, "permission_mode", "") or ACP_PERMISSION_DEFAULT).strip().lower()
    client = CommandAcpClient(
        AcpProfile(
            name=name,
            binary=program,
            extra_args=tuple(options.command[1:]),
            install_hint=f"{name}: command not found: {options.command[0]!r} (check [agent.options].command)",
        ),
        cwd=cwd,
        session_id=(getattr(config, "gateway_session_id", "") or None),
        permission_mode=mode,
        env=spawn_env,
    )
    client.name = name
    hint = command_setup_hint(options.command, "acp")
    client.setup_warnings = (hint,) if hint else ()
    return client


def build_http_agent(config: Any, env: dict[str, str]) -> HttpAgent:
    options = HttpAgentOptions.model_validate(config.options or {})
    return HttpAgent(
        options,
        env=env,
        name=str(getattr(config, "slug", "") or "http"),
        session_id=str(getattr(config, "gateway_session_id", "") or ""),
    )


def build_bridge_agent(config: Any, env: dict[str, str]) -> Any:
    from sleeptalk.agents.bridge_remote import BridgeAgent, BridgeAgentOptions

    options = BridgeAgentOptions.model_validate(config.options or {})
    return BridgeAgent(options, env=env, name=str(getattr(config, "slug", "") or "bridge"))


async def probe_command_registration(config: Any, env: dict[str, str]) -> dict[str, Any]:
    agent = build_command_agent(config, env)
    if isinstance(agent, CommandAgent):
        return await probe_command(agent)
    return await probe_acp(agent)


async def probe_acp(agent: Any) -> dict[str, Any]:
    base = {"status": "needs_input", "protocol": "unknown", "login": "unknown"}
    try:
        await agent._ensure_started()
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        return {**base, "protocol": "unavailable", "next_action": f"The ACP command did not complete initialize and session/new: {exc}"}
    finally:
        await agent.close()
    return {
        **base,
        "protocol": "available",
        "permissions": agent.permission_mode,
        "next_action": "The ACP agent initialized a session. Run `sleeptalk adapter test --live` to prove a spoken turn.",
    }


async def probe_http_registration(config: Any, env: dict[str, str]) -> dict[str, Any]:
    return await probe_http(build_http_agent(config, env))


async def probe_bridge_registration(config: Any, env: dict[str, str]) -> dict[str, Any]:
    from sleeptalk.agents.bridge_remote import probe_bridge

    return await probe_bridge(build_bridge_agent(config, env))


def register(agents: Any) -> None:
    from sleeptalk.agents.bridge_remote import BridgeAgentOptions

    agents.register(
        "command",
        build_command_agent,
        options_schema=CommandAgentOptions,
        config_fields=COMMAND_FIELDS,
        binary=command_binary,
        probe=lambda config, env: probe_command_registration(config, env),
        install_hint="set [agent.options].command to the program that runs your agent",
    )
    agents.register(
        "http",
        build_http_agent,
        options_schema=HttpAgentOptions,
        config_fields=HTTP_FIELDS,
        required_env=http_required_env,
        probe=lambda config, env: probe_http_registration(config, env),
        install_hint="set [agent.options].url to your agent's streaming endpoint",
    )
    agents.register(
        "bridge",
        build_bridge_agent,
        options_schema=BridgeAgentOptions,
        config_fields=BRIDGE_FIELDS,
        required_env=bridge_required_env,
        probe=lambda config, env: probe_bridge_registration(config, env),
        install_hint="set [agent.options].url to a wss:// Agent Bridge v1 endpoint",
    )
