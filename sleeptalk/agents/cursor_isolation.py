"""Native-key Cursor launches with private configuration and no ambient MCP root."""
from __future__ import annotations

import base64
import json
import os
import sys
from pathlib import Path
from collections.abc import Mapping

NATIVE_ENDPOINT = "https://api2.cursor.sh"


def environment(source: Mapping[str, str], home: str) -> dict[str, str]:
    home = str(Path(home).resolve())
    result = {key: source[key] for key in ("PATH", "SYSTEMROOT", "WINDIR", "TMPDIR", "TMP", "TEMP", "CURSOR_API_KEY") if key in source}
    result.update(HOME=home, USERPROFILE=home, XDG_CONFIG_HOME=str(Path(home) / ".config"),
                  XDG_CACHE_HOME=str(Path(home) / ".cache"), CURSOR_CONFIG_DIR=str(Path(home) / ".cursor"),
                  CURSOR_DATA_DIR=str(Path(home) / ".cursor"),
                  GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_COUNT="2",
                  GIT_CONFIG_KEY_0="core.fsmonitor", GIT_CONFIG_VALUE_0="false",
                  GIT_CONFIG_KEY_1="core.hooksPath", GIT_CONFIG_VALUE_1=os.devnull)
    return result


def command(binary: str) -> list[str]:
    return [binary, "--endpoint", NATIVE_ENDPOINT, "acp"]


async def verify_api_key(key: str) -> str:
    import aiohttp

    try:
        authorization = "Basic " + base64.b64encode(f"{key}:".encode()).decode("ascii")
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8), trust_env=False) as client:
            async with client.get("https://api.cursor.com/v1/me", headers={"Authorization": authorization}, allow_redirects=False) as response:
                if response.status == 401:
                    return "unavailable"
                if response.status != 200:
                    return "unknown"
                body = bytearray()
                async for chunk in response.content.iter_chunked(4096):
                    body.extend(chunk)
                    if len(body) > 65536:
                        return "unknown"
                data = json.loads(body)
                if isinstance(data, dict) and isinstance(data.get("apiKeyName"), str) and isinstance(data.get("createdAt"), str):
                    return "available"
    except (aiohttp.ClientError, TimeoutError, ValueError, TypeError):
        pass
    return "unknown"


def require_hook_free_workspace(workspace: str) -> None:
    """The CLI has no switch to disable project or machine hook execution."""
    enterprise = (Path("/Library/Application Support/Cursor/hooks.json") if sys.platform == "darwin"
                  else Path("C:/ProgramData/Cursor/hooks.json") if sys.platform == "win32"
                  else Path("/etc/cursor/hooks.json"))
    paths = [enterprise, *(Path(workspace) / relative for relative in (
        ".cursor/hooks.json", ".claude/settings.json", ".claude/settings.local.json",
    ))]
    if any(path.exists() or path.is_symlink() for path in paths):
        raise ValueError("Protected Cursor sessions require a workspace and host without Cursor or Claude hook configuration files; the installed CLI cannot disable those hooks.")
