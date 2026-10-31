"""Publish the loopback call server on the host's tailnet with `tailscale serve`.

A tailnet call needs no hosted relay and no TURN: the browser reaches this
host by its tailnet name over HTTPS, and audio flows directly between the
two tailnet addresses. Only members of the tailnet can reach the name, and
the single-use link keeps its secret in the URL fragment as always.

`tailscale serve` (never `funnel`, which is public) terminates TLS with a
certificate Tailscale issues for the machine's name; HTTPS certificates must
be enabled once for the tailnet in its admin console.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import subprocess
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from sleeptalk.call_provider import CallProviderError

DEFAULT_HTTPS_PORT = 8443
ADMIN_DNS_URL = "https://login.tailscale.com/admin/dns"
_BINARY_FALLBACKS = ("/Applications/Tailscale.app/Contents/MacOS/Tailscale", "/usr/bin/tailscale", "/usr/local/bin/tailscale")
_COMMAND_TIMEOUT = 30.0


class TailnetError(CallProviderError):
    """A fixed-string, value-free reason; `code` is stable for receipts."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class TailnetStatus:
    dns_name: str
    https_enabled: bool
    running: bool
    addresses: tuple[str, ...] = ()

    def origin(self, https_port: int = DEFAULT_HTTPS_PORT) -> str:
        suffix = "" if https_port == 443 else f":{https_port}"
        return f"https://{self.dns_name}{suffix}"


def locate(environ: dict[str, str] | None = None) -> str | None:
    env = os.environ if environ is None else environ
    found = shutil.which("tailscale", path=env.get("PATH"))
    if found:
        return found
    for candidate in _BINARY_FALLBACKS:
        if os.access(candidate, os.X_OK) and not Path(candidate).is_dir():
            return candidate
    return None


def require(environ: dict[str, str] | None = None) -> str:
    binary = locate(environ)
    if binary is None:
        raise TailnetError("tailscale_missing", "Tailscale is not installed on this host. Install it and sign in, then retry.")
    return binary


async def _run(binary: str, *args: str, timeout: float = _COMMAND_TIMEOUT) -> tuple[int, str, str]:
    process = await asyncio.create_subprocess_exec(
        binary, *args, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        async with asyncio.timeout(timeout):
            out, err = await process.communicate()
    except TimeoutError:
        process.kill()
        await process.wait()
        raise TailnetError("tailscale_timeout", "Tailscale did not answer in time on this host.") from None
    return process.returncode or 0, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


async def status(binary: str) -> TailnetStatus:
    code, out, _err = await _run(binary, "status", "--json")
    try:
        payload = json.loads(out) if code == 0 else {}
    except ValueError:
        payload = {}
    self_node = payload.get("Self") or {}
    dns_name = str(self_node.get("DNSName") or "").rstrip(".")
    running = payload.get("BackendState") == "Running" and bool(dns_name)
    if not running:
        raise TailnetError("tailscale_not_running", "Tailscale is installed but not connected on this host. Run `tailscale up` and sign in, then retry.")
    cert_domains = payload.get("CertDomains") or []
    return TailnetStatus(
        dns_name=dns_name,
        https_enabled=bool(cert_domains),
        running=True,
        addresses=tuple(str(ip) for ip in (self_node.get("TailscaleIPs") or [])),
    )


def require_https(state: TailnetStatus) -> None:
    if not state.https_enabled:
        raise TailnetError(
            "tailscale_https_disabled",
            "HTTPS certificates are not enabled for this tailnet. Enable them once under DNS at "
            f"{ADMIN_DNS_URL}, then retry.",
        )


async def publish(binary: str, port: int, *, https_port: int = DEFAULT_HTTPS_PORT) -> None:
    """Serve loopback `port` to the tailnet at https://<name>:<https_port>/."""
    code, _out, err = await _run(binary, "serve", "--bg", "--yes", f"--https={https_port}", f"http://127.0.0.1:{port}")
    if code != 0:
        lowered = err.lower()
        if "operator" in lowered or "permission" in lowered or "access denied" in lowered:
            raise TailnetError("tailscale_operator_required",
                               "This user may not configure Tailscale serve. Run `sudo tailscale set --operator=$USER` once, then retry.")
        raise TailnetError("tailscale_serve_failed", "Tailscale could not publish the call server on the tailnet. Check `tailscale serve status` on this host.")


async def unpublish(binary: str, *, https_port: int = DEFAULT_HTTPS_PORT) -> None:
    """Remove this port's serve entry; other serve configuration is untouched."""
    await _run(binary, "serve", f"--https={https_port}", "off")


def _record_path() -> Path:
    from sleeptalk.home import DEFAULT_HOME, ENV_VAR

    root = os.environ.get(ENV_VAR, "").strip()
    return (Path(root).expanduser() if root else DEFAULT_HOME) / "tailnet-serve.json"


def _write_record(https_port: int, port: int) -> None:
    """Remember the serve entry this process published, privately."""
    path = _record_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump({"https_port": https_port, "port": port, "pid": os.getpid()}, stream)


def _read_record() -> dict[str, int] | None:
    try:
        fd = os.open(_record_path(), os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        with os.fdopen(fd, encoding="utf-8") as stream:
            value = json.load(stream)
    except (OSError, ValueError):
        return None
    if not isinstance(value, dict) or not all(isinstance(value.get(k), int) for k in ("https_port", "port", "pid")):
        return None
    return value


def _clear_record() -> None:
    with contextlib.suppress(OSError):
        _record_path().unlink()


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _serves_our_port(serve_status: str, https_port: int, port: int) -> bool:
    """Whether `tailscale serve status --json` still proxies to our loopback port."""
    try:
        payload = json.loads(serve_status)
    except ValueError:
        return False
    target = f"http://127.0.0.1:{port}"
    for site, config in ((payload or {}).get("Web") or {}).items():
        if not str(site).endswith(f":{https_port}"):
            continue
        for handler in ((config or {}).get("Handlers") or {}).values():
            if str((handler or {}).get("Proxy") or "").rstrip("/") == target:
                return True
    return False


def _stale_record() -> dict[str, int] | None:
    record = _read_record()
    if record is None or _pid_alive(record["pid"]):
        return None
    return record


async def clear_stale(binary: str) -> None:
    """Remove a serve entry a dead Sleeptalk call left, and nothing else."""
    record = _stale_record()
    if record is None:
        return
    code, out, _err = await _run(binary, "serve", "status", "--json")
    if code == 0 and _serves_our_port(out, record["https_port"], record["port"]):
        await unpublish(binary, https_port=record["https_port"])
    _clear_record()


def clear_stale_after_kill(environ: dict[str, str] | None = None) -> None:
    """Synchronous `clear_stale` for `host stop` after it had to SIGKILL a call."""
    record = _stale_record()
    binary = locate(environ)
    if record is None or binary is None:
        return
    try:
        shown = subprocess.run([binary, "serve", "status", "--json"], capture_output=True,
                               text=True, timeout=_COMMAND_TIMEOUT, stdin=subprocess.DEVNULL)
        if shown.returncode == 0 and _serves_our_port(shown.stdout, record["https_port"], record["port"]):
            subprocess.run([binary, "serve", f"--https={record['https_port']}", "off"], capture_output=True,
                           timeout=_COMMAND_TIMEOUT, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return
    _clear_record()


async def prepare(*, https_port: int = DEFAULT_HTTPS_PORT, environ: dict[str, str] | None = None) -> tuple[str, str]:
    """Return (binary, public origin) once the host can publish on its tailnet."""
    binary = require(environ)
    state = await status(binary)
    require_https(state)
    return binary, state.origin(https_port)


@asynccontextmanager
async def published_provider(*, https_port: int = DEFAULT_HTTPS_PORT, environ: dict[str, str] | None = None):
    """A loopback call server published on the tailnet for the duration of a call."""
    from sleeptalk.local_rendezvous import LocalCallProvider

    binary, origin = await prepare(https_port=https_port, environ=environ)
    async with LocalCallProvider(public_origin=origin) as provider:
        # A call that died without cleanup leaves its serve entry behind;
        # clear it so this call's port is what the name points at.
        await clear_stale(binary)
        await unpublish(binary, https_port=https_port)
        await publish(binary, provider.port, https_port=https_port)
        with contextlib.suppress(OSError):
            _write_record(https_port, provider.port)
        try:
            yield provider
        finally:
            with contextlib.suppress(Exception):
                await unpublish(binary, https_port=https_port)
            _clear_record()


async def detect(*, environ: dict[str, str] | None = None) -> dict[str, object]:
    return await _detect(environ=environ)


async def _detect(*, environ: dict[str, str] | None = None) -> dict[str, object]:
    """A value-free summary for prompts and receipts; never raises.

    ``available`` is true only when a call could be published right now. The
    host cannot know whether the user's other device is on the same tailnet,
    so callers ask that question rather than switching silently.
    """
    try:
        binary = require(environ)
        state = await status(binary)
        require_https(state)
    except TailnetError as exc:
        return {"available": False, "code": exc.code, "message": str(exc)}
    except Exception:
        return {"available": False, "code": "tailscale_unavailable", "message": "Tailscale could not be inspected on this host."}
    return {"available": True, "code": "", "name": state.dns_name, "origin": state.origin()}
