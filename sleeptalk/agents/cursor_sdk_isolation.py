"""Pinned Cursor SDK bridge for explicit deny-only local sessions."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from importlib.metadata import PackageNotFoundError, distribution
import json
import os
from pathlib import Path
import signal
import tempfile
from urllib.parse import urlsplit

from sleeptalk.harness_versions import cursor_sdk_policy

SDK_VERSION = cursor_sdk_policy()["recommended"]


def bundled_bridge() -> tuple[str, str]:
    package = distribution("cursor-sdk")
    if package.version not in cursor_sdk_policy()["verified"]:
        raise ValueError(f"Protected Cursor SDK sessions require an exact verified SDK; fresh-install recommendation: cursor-sdk=={SDK_VERSION}.")
    root = Path(package.locate_file("cursor_sdk/_vendor/bridge"))
    node = root / "bin/node"
    entry = root / "dist/bin/cursor-sdk-bridge.js"
    if not node.is_file() or not entry.is_file():
        raise ValueError("Install the platform Cursor SDK wheel containing its bundled bridge and Node runtime.")
    return str(node), str(entry)


async def _drain(stream) -> None:
    while await stream.read(4096):
        pass


def _local_client(**kwargs):
    """Client for our authenticated, disposable child bridge only.

    The SDK treats a manually launched bridge as remote and blocks run-scoped
    cancellation/wait without an API key, even for local runs. These methods
    carry no key field. Keep environment fallback disabled and exempt only
    these two RPCs on the owned bridge; cloud-agent guards remain intact.
    This private SDK override is covered by the exact wheel allowlist.
    """
    from cursor_sdk import AsyncClient

    class LocalClient(AsyncClient):
        async def _agent_unary(self, method, message, *, skip_remote_cloud_guard=False):
            return await super()._agent_unary(
                method, message,
                skip_remote_cloud_guard=skip_remote_cloud_guard or method in {"CancelRun", "WaitLiveRun"},
            )

    return LocalClient(**kwargs, allow_api_key_env_fallback=False)


@asynccontextmanager
async def bridge(workspace: str):
    import httpx

    node, entry = bundled_bridge()
    workspace = str(Path(workspace).resolve())
    with tempfile.TemporaryDirectory(prefix="sleeptalk-cursor-sdk-") as directory:
        home = str(Path(directory).resolve())
        env = {"PATH": os.defpath, "HOME": home, "USERPROFILE": home, "TMPDIR": home,
               "TMP": home, "TEMP": home, "XDG_CONFIG_HOME": home + "/.config",
               "XDG_CACHE_HOME": home + "/.cache", "CURSOR_CONFIG_DIR": home + "/.cursor",
               "CURSOR_DATA_DIR": home + "/.cursor", "CURSOR_SDK_CLIENT_LANGUAGE": "python",
               "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
               "GIT_CONFIG_COUNT": "2", "GIT_CONFIG_KEY_0": "core.fsmonitor",
               "GIT_CONFIG_VALUE_0": "false", "GIT_CONFIG_KEY_1": "core.hooksPath",
               "GIT_CONFIG_VALUE_1": os.devnull}
        process = await asyncio.create_subprocess_exec(
            node, entry, "--workspace", workspace, "--state-root", home,
            "--host", "127.0.0.1", "--port", "0", "--max-message-bytes", "65536",
            "--local-store", json.dumps({"type": "jsonl", "rootDir": home + "/store"}),
            cwd=home, env=env, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
            limit=65536, start_new_session=True,
        )
        client = http = drain = None
        try:
            async with asyncio.timeout(15):
                total = 0
                while True:
                    line = await process.stderr.readline()
                    total += len(line)
                    if not line or total > 65536:
                        raise ValueError("Cursor SDK bridge did not produce a bounded discovery response.")
                    if line.startswith(b"cursor-sdk-bridge ready "):
                        ready = json.loads(line[len(b"cursor-sdk-bridge ready "):])
                        break
            url = urlsplit(ready.get("url", ""))
            token_path = Path(ready.get("authTokenFile", "")).resolve()
            if (ready.get("schemaVersion") != 1 or ready.get("protocol") != "connect"
                    or ready.get("transport") != "tcp" or ready.get("pid") != process.pid
                    or url.scheme != "http" or url.hostname != "127.0.0.1" or not url.port
                    or url.username or url.password or url.path not in {"", "/"} or url.query or url.fragment
                    or Path(ready.get("workspaceRef", "")).resolve() != Path(workspace)
                    or Path(ready.get("stateRoot", "")).resolve() != Path(home)
                    or not token_path.is_relative_to(home) or token_path.stat().st_size > 4096):
                raise ValueError("Cursor SDK bridge discovery did not match the isolated runtime.")
            token = token_path.read_text().strip()
            if not token:
                raise ValueError("Cursor SDK bridge did not provide local authentication.")
            drain = asyncio.create_task(_drain(process.stderr))
            http = httpx.AsyncClient(trust_env=False, follow_redirects=False)
            client = _local_client(base_url=ready["url"], auth_token=token, unary_timeout=8,
                                   max_retries=0, http_client=http)
            yield client
        finally:
            if client is not None:
                with suppress(Exception):
                    async with asyncio.timeout(3):
                        await client.aclose()
            if http is not None:
                await http.aclose()
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            with suppress(TimeoutError):
                await asyncio.wait_for(process.wait(), 2)
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            await process.wait()
            if drain is not None:
                drain.cancel()
                with suppress(asyncio.CancelledError):
                    await drain


async def create_agent(client, workspace: str, model: str, api_key: str):
    from cursor_sdk import AgentOptions, LocalAgentOptions

    options = AgentOptions(model=model, api_key=api_key, tools=[], disallowed_tools=["mcp", "task"],
                           local=LocalAgentOptions(cwd=workspace, setting_sources=[], auto_review=False),
                           mcp_servers={}, agents={})
    async with asyncio.timeout(15):
        agent = await client.agents.create(options)
        info = await client.agents.get(agent.agent_id, cwd=workspace, api_key=api_key)
    if (info.runtime != "local" or Path(info.cwd).resolve() != Path(workspace).resolve()
            or not agent.model or agent.model.id != model):
        raise ValueError("Cursor SDK did not confirm the selected workspace and model.")
    return agent


async def probe(agent, env: dict[str, str]) -> dict[str, str]:
    result = {"status": "needs_input", "protocol": "unknown", "login": "unknown",
              "workspace": "unknown", "permissions": "unavailable", "next_action": ""}
    if agent.permission_mode != "deny":
        return {**result, "next_action": "Cursor SDK supports an explicit deny-only setup with no built-in tools. Pending tool approvals require an SDK approval callback that is not available in the pinned version."}
    if agent.gateway_session_id or not agent.model or not env.get("CURSOR_API_KEY"):
        return {**result, "next_action": "Select a new Cursor SDK session, an explicit model, and a native API key privately. Existing-session handoff is not supported by this isolated path."}
    try:
        bundled_bridge()
    except (PackageNotFoundError, ValueError):
        return {**result, "next_action": f"Install the platform cursor-sdk=={SDK_VERSION} wheel before isolated deny-only setup."}
    async with bridge(agent.workspace_dir) as client:
        result["protocol"] = "available"
        user = await client.me(api_key=env["CURSOR_API_KEY"])
        if not user.api_key_name or not user.created_at:
            return {**result, "next_action": "Cursor SDK did not validate the selected native API key."}
        result["login"] = "available"
        await create_agent(client, agent.workspace_dir, agent.model, env["CURSOR_API_KEY"])
    return {**result, "status": "ready", "workspace": "available", "permissions": "available"}
