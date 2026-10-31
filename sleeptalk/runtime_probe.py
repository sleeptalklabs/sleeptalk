"""Bounded adapter inspection without submitting a prompt or starting user work."""

from __future__ import annotations

import asyncio

from sleeptalk.agent_environment import agent_environment
from sleeptalk.agents.construction import resolve_agent
from sleeptalk.config import AgentConfig, SleeptalkConfig, _validate_component_choices
from sleeptalk.runtime_binding import BindingError, canonical_workspace


# Canonical registrations, not aliases or the name of the installing agent.
PROBE_GROUPS = {
    "codex": "codex", "claude_code": "cli", "cursor": "cli", "cursor_acp": "cli",
    "pi": "rpc", "omp": "rpc", "acp": "rpc", "hermes_gateway": "rpc",
    "openclaw": "rpc", "openclaw_acp": "rpc",
    "command": "registration", "http": "registration", "bridge": "registration",
    "model": "registration",
}


async def probe_runtime(agent: AgentConfig, *, env: dict[str, str] | None = None) -> dict:
    result = {
        "schema_version": 1, "operation": "probe_runtime", "status": "needs_input",
        "protocol": "unknown", "login": "unknown", "workspace": "unknown",
        "permissions": "unknown", "continuity": "new",
        "next_action": "This adapter has no non-work authentication probe. Complete adapter verification before collecting speech credentials.",
    }
    try:
        workspace = canonical_workspace(agent.workspace_dir)
    except BindingError:
        return {**result, "status": "failed", "workspace": "unavailable", "next_action": "Select an accessible workspace on the intended runtime host."}
    agent = agent.model_copy(update={"workspace_dir": str(workspace)})
    # Auto-approval is never introduced by a readiness probe or private setup.
    if agent.permission_mode in {"auto", "skip", "never"}:
        return {**result, "permissions": "unavailable", "next_action": "Select permission behavior that retains approval prompts."}
    from sleeptalk.agent_auth import AgentAuthError, resolve_agent_auth_env
    try:
        environment = resolve_agent_auth_env(agent, agent_environment(env))
    except AgentAuthError as exc:
        return {**result, "login": "unavailable", "next_action": str(exc)}
    try:
        construction = resolve_agent(agent, environment)
    except (ValueError, KeyError):
        return {**result, "next_action": "Select an installed, registered adapter and a compatible runtime policy."}
    name = construction.registration.name
    if _validate_component_choices(SleeptalkConfig(agent=agent), env=environment):
        return {**result, "next_action": "One or more settings do not apply to the selected adapter. Correct its runtime configuration before setup."}
    result["adapter"] = name
    if construction.selection is not None:
        result["runtime"] = construction.selection.kind.value
        if construction.selection.fallback_from is not None:
            return {**result, "next_action": "The requested runtime would fall back. Choose and pin the intended runtime explicitly before setup."}
    group = PROBE_GROUPS.get(name)
    if group is None or group == "registration":
        # Installed extensions and bring-your-own types supply their own probe.
        probe = construction.registration.probe
        if probe is None:
            return result
        try:
            async with asyncio.timeout(30):
                return {**result, **await probe(agent, environment)}
        except Exception:
            return {**result, "next_action": "The adapter probe failed or timed out. Check its configuration privately, then retry."}
    if group != "codex":
        from sleeptalk.agents import onboarding_cli, onboarding_rpc
        implementation = onboarding_cli if group == "cli" else onboarding_rpc
        try:
            async with asyncio.timeout(30):
                return {**result, **await implementation.probe(agent, name, environment)}
        except Exception:
            return {**result, "next_action": "The adapter probe failed or timed out. Check its installation and login privately, then retry."}
    return await _probe_codex(agent, workspace, environment, result)


async def _probe_codex(agent, workspace, environment, result):
    from sleeptalk.agents.codex_app_server import CodexAppServerClient, CodexSchemaError
    client = CodexAppServerClient(cwd=str(workspace), permission_mode=agent.permission_mode, env=environment)
    try:
        async with asyncio.timeout(30):
            await client._spawn(client._launch_args())
            await client._handshake()
            result["protocol"] = "available"
            account = await client._rpc("account/read", {"refreshToken": True})
            if not isinstance(account, dict) or not (
                isinstance(account.get("account"), dict) or account.get("requiresOpenaiAuth") is False
            ):
                return {**result, "login": "unavailable", "next_action": "Complete Codex login privately on the runtime host, then retry the probe."}
            result["login"] = "available"
            effective = await client._rpc("config/read", {"cwd": str(workspace), "includeLayers": False})
            if not isinstance(effective, dict) or not isinstance(effective.get("config"), dict):
                return {**result, "next_action": "The adapter did not return effective workspace settings."}
            if effective["config"].get("approval_policy") not in {"untrusted", "on-failure", "on-request"}:
                return {**result, "permissions": "unavailable", "next_action": "The effective Codex approval policy does not establish retained approval prompts. Configure an approval-capable policy privately, then retry."}
            return {**result, "status": "ready", "workspace": "available", "permissions": "inherited", "next_action": ""}
    except CodexSchemaError:
        return {**result, "protocol": "unavailable", "next_action": "Use the Codex version matching the installed Sleeptalk protocol schema."}
    except Exception:
        return {**result, "next_action": "The adapter probe failed or timed out. Check runtime installation and login privately, then retry."}
    finally:
        await client.close()
