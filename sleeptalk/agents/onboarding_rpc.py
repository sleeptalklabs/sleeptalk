"""Read-only protocol evidence; a handshake does not validate model credentials."""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import signal
import secrets
from pathlib import Path

from sleeptalk.agent_environment import agent_environment
from sleeptalk.config import AgentConfig

PROBE_TIMEOUT = 20.0
_RESULT = {
    "status": "needs_input", "protocol": "unknown", "login": "unknown",
    "workspace": "unknown", "permissions": "unknown", "next_action": "",
}


async def probe(agent: AgentConfig, registration_name: str, env: dict[str, str]) -> dict[str, str]:
    """Inspect the selected registration without changing sessions or permissions.

    `login` means the model provider accepts the runtime's credentials. Pi's
    state and ACP's initialization cannot establish this. Gateway admission is
    reported separately and never promoted to model authentication.
    """
    result = dict(_RESULT)
    if registration_name == "acp" or (registration_name == "hermes_gateway" and not agent.gateway_url):
        from sleeptalk.agents.hermes_bridge import isolated, probe as hermes_probe
        if isolated(agent):
            try:
                async with asyncio.timeout(PROBE_TIMEOUT):
                    return await hermes_probe(agent, env, "acp" if registration_name == "acp" else "gateway")
            except ValueError as exc:
                return {**result, "next_action": str(exc)}
            except Exception:
                return {**result, "next_action": "The isolated Hermes bridge failed or timed out; verify its pinned installation and selected provider privately."}
        if agent.permission_mode not in {"pending", "deny"}:
            return {**result, "permissions": "unavailable", "next_action": "Select pending or deny tool permissions before verifying this runtime. No process was launched."}
        if registration_name == "hermes_gateway":
            return {**result, "next_action": "Pass --gateway-url for a running Hermes dashboard, or use --agent hermes to run the installed Hermes over ACP."}
        return await _installed_hermes(agent, env, result)

    if registration_name in {"pi", "omp"} and agent.permission_mode not in {"pending", "deny"}:
        return {**result, "permissions": "unavailable", "next_action": "Select pending or deny tool permissions before verifying this runtime. No process was launched."}
    environment = agent_environment(env)
    try:
        async with asyncio.timeout(PROBE_TIMEOUT):
            if registration_name in {"pi", "omp"}:
                binary = _binary(agent.pi_bin or registration_name, environment)
                args = [binary, "--mode", "rpc"]
                if registration_name == "pi":
                    args.extend(["--no-approve" if agent.permission_mode in {"pending", "deny"} else "--approve", "-n", agent.slug])
                args.append("--no-session")
                if agent.model:
                    args += ["--model", agent.model]
                if agent.provider and agent.provider != "pi":
                    args += ["--provider", agent.provider]
                nonce = None
                if agent.permission_mode in {"pending", "deny"}:
                    nonce = secrets.token_hex(16)
                    args.extend(["--no-extensions", "--extension", str(Path(__file__).with_name("pi_permission_extension.js"))])
                    environment.update({"SLEEPTALK_PERMISSION_MODE": agent.permission_mode,
                                        "SLEEPTALK_PERMISSION_NONCE": nonce,
                                        "SLEEPTALK_ONBOARDING_PROBE": "1"})
                request = {"id": "onboarding", "type": "get_state"}
                if registration_name == "omp":
                    from sleeptalk.agents.omp_sdk import prepare
                    try:
                        launch = prepare(binary, agent.workspace_dir, agent.provider, agent.model, environment, agent.permission_mode, nonce, probe=True)
                    except ValueError as exc:
                        return {**result, "permissions": "unavailable", "next_action": str(exc)}
                    try:
                        response = await _stdio(launch.args, agent.workspace_dir, launch.env, request, nonce=nonce)
                    finally:
                        launch.close()
                else:
                    response = await _stdio(args, agent.workspace_dir, environment, request, nonce=nonce)
                if response.get("type") != "response" or response.get("success") is not True or not isinstance(response.get("data"), dict):
                    raise ValueError("invalid state response")
                result.update(protocol="available")
                proof = response.get("_onboarding", {})
                if proof.get("version") == 1 and proof.get("mode") == agent.permission_mode and nonce:
                    result["permissions"] = "available"
                    workspace = proof.get("workspace")
                    if isinstance(workspace, str) and workspace and Path(workspace).resolve() == Path(agent.workspace_dir).resolve():
                        result["workspace"] = "available"
                    selected = isinstance(proof.get("model"), str) and bool(proof["model"]) and proof.get("provider") in {"openai", "anthropic"} and (not agent.model or proof.get("model") == agent.model) and (
                        agent.provider in {"", "pi"} or proof.get("provider") == agent.provider
                    )
                    if selected and proof.get("login") in {"available", "unavailable"}:
                        result["login"] = proof["login"]
                else:
                    result["permissions"] = "unavailable"
                if all(result[field] == "available" for field in ("workspace", "permissions", "login")):
                    return {**result, "status": "ready", "next_action": ""}
                return {**result, "next_action": "Select pending or deny tool permissions and an authenticated native OpenAI or Anthropic API-key model. OAuth and custom-provider verification remain unimplemented. Model-list permissions, quota errors, or unavailable models leave login unverified; check the selected provider privately."}
            if registration_name == "openclaw_acp":
                response = await _stdio([_binary("openclaw", environment), "acp"], agent.workspace_dir, environment, {
                    "jsonrpc": "2.0", "id": "onboarding", "method": "initialize", "params": {
                        "protocolVersion": 1, "clientInfo": {"name": "sleeptalk-onboarding", "version": "0.1.0"},
                        "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False},
                    },
                })
                initialized = response.get("result")
                if (
                    response.get("jsonrpc") != "2.0"
                    or "error" in response
                    or not isinstance(initialized, dict)
                    or type(initialized.get("protocolVersion")) is not int
                    or initialized["protocolVersion"] != 1
                ):
                    raise ValueError("invalid ACP initialization")
                return {**result, "protocol": "available", "permissions": _permissions(agent), "next_action": (
                    "ACP initialization succeeded. An isolated session and login verifier "
                    "has not been implemented, so login and effective workspace remain "
                    "unverified. Select pending or deny permissions and complete this "
                    "adapter's human login and workspace verification before private onboarding."
                )}
            if registration_name == "hermes_gateway":
                return await _hermes(agent, environment, result)
            if registration_name == "openclaw":
                return await _openclaw(agent, environment, result)
            return {**result, "next_action": "No RPC onboarding probe is registered for this adapter."}
    except FileNotFoundError:
        return {**result, "protocol": "unavailable", "next_action": "Install the selected adapter on the runtime host, then repeat its protocol probe."}
    except Exception:
        return {**result, "next_action": "The selected protocol probe failed or timed out. Check its installation, gateway access, and protocol version privately, then retry. No conversation was opened."}


async def _installed_hermes(agent: AgentConfig, env: dict[str, str], result: dict[str, str]) -> dict[str, str]:
    """Probe the Hermes on PATH over ACP: any version, its own model login.

    Sleeptalk holds no credential here; Hermes authenticates with whatever it
    is configured with, and approvals stay with the caller under pending or
    deny. The pinned isolated runtime is the explicit alternative.
    """
    if agent.permission_mode not in {"pending", "deny"}:
        return {**result, "permissions": "unavailable", "next_action": "Select pending or deny tool permissions before verifying this runtime. No process was launched."}
    environment = agent_environment(env)
    try:
        async with asyncio.timeout(PROBE_TIMEOUT):
            binary = _binary(agent.pi_bin or "hermes", environment)
            response = await _stdio([binary, "acp"], agent.workspace_dir, environment, {
                "jsonrpc": "2.0", "id": "onboarding", "method": "initialize", "params": {
                    "protocolVersion": 1, "clientInfo": {"name": "sleeptalk-onboarding", "version": "0.1.0"},
                    "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False},
                },
            })
    except FileNotFoundError:
        return {**result, "protocol": "unavailable", "next_action": "Install Hermes on the runtime host (it provides `hermes acp`), or select the pinned isolated runtime with options.hermes_root."}
    except Exception:
        return {**result, "next_action": "The installed Hermes did not answer an ACP initialize; check `hermes acp` on this host, then retry."}
    initialized = response.get("result")
    if response.get("jsonrpc") != "2.0" or "error" in response or not isinstance(initialized, dict) or initialized.get("protocolVersion") != 1:
        return {**result, "protocol": "unavailable", "next_action": "The installed Hermes answered an unsupported ACP version; update Hermes or select the pinned isolated runtime."}
    return {**result, "status": "ready", "protocol": "available", "permissions": "inherited",
            "workspace": "inherited", "login": "inherited",
            "next_action": "The installed Hermes will run in the workspace with its own model login; approvals stay with the caller."}


def _permissions(agent: AgentConfig) -> str:
    return "inherited" if agent.permission_mode in {"pending", "deny"} else "unavailable"


def _binary(name: str, env: dict[str, str]) -> str:
    found = shutil.which(name, path=env.get("PATH", os.defpath))
    fallback = Path.home() / ".local" / "bin" / name
    if found:
        return found
    if fallback.is_file() and os.access(fallback, os.X_OK):
        return str(fallback)
    raise FileNotFoundError


async def _stdio(argv: list[str], cwd: str, env: dict[str, str], request: dict | None, *, nonce: str | None = None) -> dict:
    """One bounded exchange, ignoring logs and refusing server work requests."""
    process = await asyncio.create_subprocess_exec(
        *argv, cwd=cwd, env=agent_environment(env), stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, limit=65536,
        start_new_session=True,
    )
    try:
        assert process.stdin is not None and process.stdout is not None
        if request is not None:
            process.stdin.write((json.dumps(request) + "\n").encode())
            await process.stdin.drain()
        total = 0
        attestation = None
        reply = None
        for _ in range(256):
            line = await process.stdout.readline()
            total += len(line)
            if not line or total > 1024 * 1024:
                raise ValueError("no bounded protocol response")
            try:
                response = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            if not isinstance(response, dict):
                continue
            if nonce and response.get("type") == "extension_ui_request" and response.get("method") == "setStatus" and response.get("statusKey") == "sleeptalk-permissions":
                proof = json.loads(response.get("statusText", ""))
                if not isinstance(proof, dict) or proof.get("nonce") != nonce:
                    raise ValueError("permission extension mismatch")
                attestation = proof
                if reply is not None:
                    return {**reply, "_onboarding": attestation}
                continue
            if response.get("type") == "extension_ui_request" or ("method" in response and "id" in response):
                raise ValueError("probe cannot execute agent requests")
            if request is None and response.get("method") == "event":
                event = response.get("params")
                if isinstance(event, dict) and event.get("type") == "gateway.ready":
                    return event
            if request is not None and response.get("id") == request["id"]:
                if nonce:
                    reply = response
                    if attestation is not None:
                        return {**reply, "_onboarding": attestation}
                else:
                    return response
        raise ValueError("response frame limit exceeded")
    finally:
        # Adapter launchers may exit before their children. Reap the owned
        # process group even after the parent has returned successfully.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), 1.0)
        except TimeoutError:
            pass
        finally:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
        await asyncio.wait_for(process.wait(), 1.0)


def _secure_gateway(url: str) -> bool:
    from sleeptalk.providers import check_gateway_address
    try:
        check_gateway_address(url)
    except ValueError:
        return False
    return True


async def _hermes(agent: AgentConfig, env: dict[str, str], result: dict[str, str]) -> dict[str, str]:
    from sleeptalk.agents.hermes_gateway_adapter import WebsocketTuiLink, _ws_url
    if not _secure_gateway(agent.gateway_url):
        return {**result, "next_action": "Use TLS for a remote Hermes dashboard, or a loopback address on this host."}
    link = WebsocketTuiLink(_ws_url(agent.gateway_url, env.get("HERMES_GATEWAY_SESSION_TOKEN", "")), api_key=env.get("HERMES_GATEWAY_API_KEY", ""))
    try:
        await link.start()
    finally:
        await link.close()
    result["gateway_access"] = "available"
    return {**result, "protocol": "available", "permissions": "unknown", "next_action": "Hermes gateway is reachable. Its ready event does not validate model-provider authentication, workspace, or tool permissions. Verify those through the existing Hermes setup path before private onboarding."}


async def _openclaw(agent: AgentConfig, env: dict[str, str], result: dict[str, str]) -> dict[str, str]:
    from sleeptalk.agents.openclaw_gateway_adapter import AiohttpGatewaySession, DEFAULT_GATEWAY_URL, OpenClawGatewayClient, PROTOCOL
    url = agent.gateway_url or DEFAULT_GATEWAY_URL
    if not _secure_gateway(url):
        return {**result, "next_action": "Use TLS for a remote OpenClaw gateway, or a loopback address on this host."}
    client = OpenClawGatewayClient(url=url, token=env.get("OPENCLAW_GATEWAY_TOKEN", ""), password=env.get("OPENCLAW_GATEWAY_PASSWORD", ""), cwd=agent.workspace_dir, permission_mode="deny")
    if not client._auth_present():
        return {**result, "gateway_access": "unavailable", "next_action": "Configure OpenClaw gateway authentication privately on the runtime host, then retry. Model-provider login remains unverified."}
    session = AiohttpGatewaySession(url, ca_file=env.get("OPENCLAW_GATEWAY_CA_FILE", ""))
    try:
        await session.connect()
        await client._wait_for_challenge(await session.events())
        params = client._connect_params()
        params["scopes"] = ["operator.read"]
        params["caps"] = []
        hello = await session.request("connect", params)
        if hello.get("protocol") != PROTOCOL:
            raise ValueError("protocol mismatch")
    finally:
        await session.close()
    return {**result, "protocol": "available", "gateway_access": "available", "permissions": _permissions(agent), "next_action": "OpenClaw gateway accepted read-only access. This does not validate model-provider login or bind the selected workspace to a conversation. Complete those checks through OpenClaw's existing setup before private onboarding."}
