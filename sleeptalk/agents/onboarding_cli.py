"""Bounded, value-free setup observations for Claude and Cursor runtimes.

No prompt or model turn is sent. The first-party Claude API-key path initializes
an isolated SDK client with session persistence disabled. A local login
configuration is distinct from an authenticated response.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import signal
import tempfile

from sleeptalk.agent_environment import agent_environment, sdk_environment
from sleeptalk.config import AgentConfig
from sleeptalk.runtime_binding import BindingError, canonical_workspace

_TIMEOUT = 15.0
_MAX_OUTPUT = 64 * 1024
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


class _ProbeError(Exception):
    pass


def _result() -> dict[str, str]:
    return {
        "status": "needs_input", "protocol": "unknown", "login": "unknown",
        "login_configuration": "unknown", "workspace": "unknown",
        "permissions": "unknown", "next_action": "",
    }


@asynccontextmanager
async def _process(argv: list[str], workspace: Path, env: dict[str, str]):
    proc = await asyncio.create_subprocess_exec(
        *argv, cwd=str(workspace), env=agent_environment(env),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, limit=_MAX_OUTPUT,
        start_new_session=os.name == "posix",
    )
    try:
        yield proc
    finally:
        # Own process group includes children launched by the inspected CLI.
        if os.name == "posix":
            with suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
        elif proc.returncode is None:
            with suppress(ProcessLookupError):
                proc.kill()
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(proc.communicate(), timeout=2.0)


async def _capture(argv: list[str], workspace: Path, env: dict[str, str]) -> tuple[int, str]:
    async with _process(argv, workspace, env) as proc:
        proc.stdin.close()
        chunks = []
        size = 0
        while chunk := await proc.stdout.read(4096):
            size += len(chunk)
            if size > _MAX_OUTPUT:
                raise _ProbeError()
            chunks.append(chunk)
        code = await proc.wait()
    return code, b"".join(chunks).decode("utf-8", errors="replace")


def _claude_binary() -> str:
    # Use the SDK's own resolver, including its bundled executable. A PATH
    # Claude installation may differ from the executable this adapter uses.
    from claude_agent_sdk import ClaudeAgentOptions
    from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport

    return SubprocessCLITransport(prompt="", options=ClaudeAgentOptions())._find_cli()


async def _claude(agent: AgentConfig, workspace: Path, env: dict[str, str]) -> dict[str, str]:
    result = _result()
    if agent.permission_mode.strip().lower() not in {"pending", "default"}:
        return {**result, "permissions": "unavailable", "next_action": "Select Claude permission_mode=pending or default to retain tool approval callbacks."}
    result["permissions"] = "available"
    if importlib.util.find_spec("claude_agent_sdk") is None:
        return {**result, "protocol": "unavailable", "next_action": "Install the Claude SDK extra in the selected Sleeptalk runtime."}
    binary = _claude_binary()
    # Older CLIs can treat unknown command words as a user prompt. Establish
    # that these are actual subcommands before passing them to the binary.
    code, help_text = await _capture([binary, "--help"], workspace, env)
    if code != 0 or not re.search(r"(?m)^\s+auth(?:\s|\[)", help_text):
        return {**result, "next_action": "The selected Claude SDK executable does not advertise an auth command. Install a compatible SDK version; no prompt was sent."}
    code, help_text = await _capture([binary, "auth", "--help"], workspace, env)
    if code != 0 or not re.search(r"(?m)^\s+status(?:\s|\[)", help_text):
        return {**result, "next_action": "The selected Claude SDK executable does not advertise auth status. Install a compatible SDK version; no prompt was sent."}
    code, output = await _capture([binary, "auth", "status"], workspace, env)
    payload = json.loads(output)
    if not isinstance(payload, dict) or type(payload.get("loggedIn")) is not bool:
        raise _ProbeError()
    if code == 1 and payload["loggedIn"] is False:
        return {**result, "login_configuration": "unavailable", "login": "unavailable", "next_action": "Complete Claude login privately on the runtime host, then retry."}
    if code != 0 or payload["loggedIn"] is not True:
        raise _ProbeError()
    if payload.get("apiProvider") == "firstParty" and env.get("ANTHROPIC_API_KEY") and not any(
        env.get(name) for name in ("ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
            "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY")
    ) and env.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com").rstrip("/") == "https://api.anthropic.com":
        login = await _anthropic_api_login(env["ANTHROPIC_API_KEY"])
        if login != "available":
            return {**result, "login_configuration": "available", "login": login,
                    "next_action": "Verify the selected Anthropic API key and model access privately, then retry."}
        await _claude_workspace(agent, workspace, env)
        return {**result, "status": "ready", "protocol": "available", "login": "available",
                "login_configuration": "available", "workspace": "available", "next_action": ""}
    return {
        **result, "login_configuration": "available",
        "next_action": "Claude reports a configured login. Sleeptalk still needs a bounded SDK authentication and workspace probe; this status does not prove live provider access. Use the existing operator setup path until that probe is implemented.",
    }


async def _anthropic_api_login(key: str) -> str:
    import aiohttp
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8), trust_env=False) as session:
        async with session.get("https://api.anthropic.com/v1/models", params={"limit": "1"},
            headers={"x-api-key": key, "anthropic-version": "2023-06-01"}, allow_redirects=False) as response:
            if response.status in {401, 403}:
                return "unavailable"
            if response.status != 200:
                return "unknown"
            raw = bytearray()
            async for chunk in response.content.iter_chunked(4096):
                raw.extend(chunk)
                if len(raw) > _MAX_OUTPUT:
                    return "unknown"
            payload = json.loads(raw)
            models = payload.get("data") if isinstance(payload, dict) else None
            return "available" if isinstance(models, list) and any(
                isinstance(item, dict) and item.get("type") == "model" and isinstance(item.get("id"), str) and item["id"] for item in models
            ) else "unknown"


async def _claude_workspace(agent: AgentConfig, workspace: Path, env: dict[str, str]) -> None:
    from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, PermissionResultDeny
    async def deny(*args, **kwargs):
        return PermissionResultDeny(message="Onboarding inspection cannot execute tools.")
    options = ClaudeAgentOptions(cwd=str(workspace), permission_mode="default", can_use_tool=deny,
        setting_sources=[], strict_mcp_config=True, mcp_servers={}, tools=[], skills=[], plugins=[],
        settings=json.dumps({"disableAllHooks": True}), env=sdk_environment(env),
        extra_args={"no-session-persistence": None}, max_buffer_size=_MAX_OUTPUT)
    async with ClaudeSDKClient(options=options) as client:
        info = await client.get_server_info()
        if not isinstance(info, dict):
            raise _ProbeError()


def _cursor_binary(env: dict[str, str]) -> str:
    from sleeptalk.agents.profiles.cursor import CURSOR_CLI_BINARY_FALLBACKS

    found = shutil.which("agent", path=env.get("PATH", ""))
    if found:
        return found
    for candidate in CURSOR_CLI_BINARY_FALLBACKS:
        expanded = os.path.expanduser(candidate)
        if os.access(expanded, os.X_OK):
            return expanded
    raise FileNotFoundError()


async def _rpc(proc, request_id: int, method: str, params: dict) -> dict:
    request = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
    proc.stdin.write(json.dumps(request).encode() + b"\n")
    await proc.stdin.drain()
    total = 0
    while True:
        line = await proc.stdout.readline()
        total += len(line)
        if not line or total > _MAX_OUTPUT:
            raise _ProbeError()
        payload = json.loads(line)
        if not isinstance(payload, dict) or payload.get("jsonrpc") != "2.0":
            raise _ProbeError()
        if "method" in payload and "id" in payload:
            # No permission, filesystem, or other server request is executed.
            raise _ProbeError()
        if payload.get("id") != request_id:
            continue
        if "error" in payload or not isinstance(payload.get("result"), dict):
            raise _ProbeError()
        return payload["result"]


async def _cursor_native_session(agent: AgentConfig, workspace: Path, env: dict[str, str]) -> dict[str, str]:
    from sleeptalk.agents import cursor_isolation

    result = {**_result(), "permissions": "available"}
    if agent.gateway_session_id:
        return {**result, "next_action": "Isolated Cursor onboarding creates a new local session. Existing local-session handoff requires its original runtime configuration."}
    try:
        cursor_isolation.require_hook_free_workspace(str(workspace))
    except ValueError as exc:
        return {**result, "permissions": "unavailable", "next_action": str(exc)}
    login = await cursor_isolation.verify_api_key(env["CURSOR_API_KEY"])
    result["login"] = login
    if login != "available":
        return {**result, "next_action": "Cursor did not validate this native API key. Check the key and API metadata access privately; no authentication browser or agent session was started."}
    result["login_configuration"] = "available"
    binary = _cursor_binary(env)
    with tempfile.TemporaryDirectory(prefix="sleeptalk-cursor-") as home:
        isolated = cursor_isolation.environment(env, home)
        async with _process(cursor_isolation.command(binary), Path(home), isolated) as proc:
            init = await _rpc(proc, 1, "initialize", {
                "protocolVersion": 1, "clientInfo": {"name": "sleeptalk-onboarding", "version": "1"},
                "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False},
            })
            if type(init.get("protocolVersion")) is not int or init["protocolVersion"] != 1:
                raise _ProbeError()
            result["protocol"] = "available"
            created = await _rpc(proc, 2, "session/new", {"cwd": str(workspace), "mcpServers": []})
            session_id = created.get("sessionId")
            if not isinstance(session_id, str) or not session_id or len(session_id) > 512:
                raise _ProbeError()
            if agent.model:
                selection = await _rpc(proc, 3, "session/set_config_option", {"sessionId": session_id, "configId": "model", "value": agent.model})
                options = selection.get("configOptions")
                if not isinstance(options, list) or not any(isinstance(item, dict) and item.get("id") == "model" and item.get("currentValue") == agent.model for item in options):
                    raise _ProbeError()
            else:
                models = created.get("models")
                if not isinstance(models, dict) or not isinstance(models.get("currentModelId"), str) or not models["currentModelId"]:
                    raise _ProbeError()
            listing = await _rpc(proc, 4, "session/list", {"cwd": str(workspace)})
            sessions = listing.get("sessions")
            if not isinstance(sessions, list) or not any(isinstance(item, dict) and item.get("sessionId") == session_id and isinstance(item.get("cwd"), str) and Path(item["cwd"]).resolve() == workspace for item in sessions):
                raise _ProbeError()
            result["workspace"] = "available"
    return {**result, "status": "ready", "next_action": ""}


async def _cursor_acp(agent: AgentConfig, workspace: Path, env: dict[str, str]) -> dict[str, str]:
    result = _result()
    if agent.permission_mode.strip().lower() not in {"pending", "deny"}:
        return {**result, "permissions": "unavailable", "next_action": "Select Cursor ACP permission_mode=pending or deny before setup."}
    result["permissions"] = "available"
    if env.get("CURSOR_API_KEY"):
        return await _cursor_native_session(agent, workspace, env)
    binary = _cursor_binary(env)
    code, output = await _capture([binary, "status"], workspace, env)
    output = _ANSI.sub("", output).lower()
    if "not logged in" in output:
        return {**result, "login_configuration": "unavailable", "login": "unavailable", "next_action": "Complete Cursor CLI login privately on the runtime host, then retry."}
    # Exit zero, key presence, and an unknown text format are not login proof.
    if code != 0 or not re.search(r"\blogged in as\b", output):
        return {**result, "next_action": "Cursor did not report a recognized configured login. Check CLI login privately; no authentication browser was started."}
    result["login_configuration"] = "available"
    async with _process([binary, "acp"], workspace, env) as proc:
        init = await _rpc(proc, 1, "initialize", {
            "protocolVersion": 1,
            "clientInfo": {"name": "sleeptalk-onboarding", "version": "1"},
            "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}},
        })
        if type(init.get("protocolVersion")) is not int or init["protocolVersion"] != 1:
            raise _ProbeError()
        result["protocol"] = "available"
        methods = init.get("authMethods")
        if not isinstance(methods, list) or not any(isinstance(item, dict) and item.get("id") == "cursor_login" for item in methods):
            return {**result, "next_action": "Cursor ACP did not advertise the supported authentication method. Check the installed CLI version."}
        await _rpc(proc, 2, "authenticate", {"methodId": "cursor_login"})
        result["login"] = "available"
    return {
        **result,
        "next_action": "Cursor ACP authenticated without a prompt. Sleeptalk still needs an isolated workspace-session check before private setup can report ready. Use the existing operator setup path until that check is implemented.",
    }


async def probe(agent: AgentConfig, registration_name: str, env: dict[str, str]) -> dict[str, str]:
    """Inspect exactly the resolved registration; never select another runtime."""
    result = _result()
    try:
        workspace = canonical_workspace(agent.workspace_dir)
    except BindingError:
        return {**result, "workspace": "unavailable", "next_action": "Select an accessible workspace on the intended runtime host."}
    if registration_name not in {"claude_code", "cursor_acp", "cursor"}:
        return {**result, "next_action": "No CLI onboarding probe is registered for this selected runtime."}
    try:
        async with asyncio.timeout(_TIMEOUT):
            if registration_name == "cursor":
                from sleeptalk.agents.cursor_sdk_isolation import probe as sdk_probe
                return await sdk_probe(agent, agent_environment(env))
            handler = _claude if registration_name == "claude_code" else _cursor_acp
            return await handler(agent, workspace, agent_environment(env))
    except FileNotFoundError:
        return {**result, "protocol": "unavailable", "next_action": "Install the selected adapter executable in this runtime, then retry."}
    except Exception:
        # CLI output can contain account identifiers, tokens, and local paths.
        return {**result, "next_action": "The selected runtime probe failed, returned an unsupported response, or timed out. Check installation and login privately, then retry."}
