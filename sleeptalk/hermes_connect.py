"""Detect a Hermes dashboard and write a hermes_gateway profile.

The dashboard door is /api/ws with ?token=. A messaging gateway being up
is a different process. sleeptalk never SSHs and never scrapes Telegram.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import re
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

import aiohttp

SESSION_TOKEN_ENV = "HERMES_GATEWAY_SESSION_TOKEN"
DEFAULT_ORIGIN = "http://127.0.0.1:9119"
DASHBOARD_COMMAND = "hermes dashboard --host 127.0.0.1 --port 9119 --no-open"
_CLOSE_UNAUTHORIZED = 4401
# A loopback dashboard injects its per-start session token into its own page,
# which is how the local browser authenticates. Gated dashboards don't.
_PAGE_TOKEN = re.compile(r'window\.__HERMES_SESSION_TOKEN__="([A-Za-z0-9_\-]{16,200})"')
_MAX_PAGE_BYTES = 2_000_000


_DASHBOARD_PROCESS = re.compile(r"\bhermes\b.*\b(?:dashboard|web)\b.*--port[ =](\d{2,5})\b")


def running_dashboard_origins(process_lines: list[str] | None = None) -> list[str]:
    """Loopback dashboard origins to try: the default port, then ports of running
    `hermes dashboard --port N` processes on this machine."""
    if process_lines is None:
        import subprocess

        try:
            process_lines = subprocess.run(
                ["ps", "-axo", "command="], capture_output=True, text=True, timeout=5, check=False
            ).stdout.splitlines()
        except (OSError, subprocess.SubprocessError):
            process_lines = []
    origins = [DEFAULT_ORIGIN]
    for line in process_lines:
        match = _DASHBOARD_PROCESS.search(line)
        if match:
            origin = f"http://127.0.0.1:{int(match.group(1))}"
            if origin not in origins:
                origins.append(origin)
    return origins


def is_loopback_origin(url: str) -> bool:
    host = urlsplit(url).hostname or ""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def origin_for(url: str) -> str:
    """http(s) origin for a dashboard or socket URL."""
    parts = urlsplit(url)
    scheme = {"ws": "http", "wss": "https"}.get(parts.scheme, parts.scheme or "http")
    return urlunsplit((scheme, parts.netloc, "", "", ""))


async def read_local_session_token(url: str, *, timeout: float = 4.0) -> str:
    """The current session token of a dashboard on this machine, or ""."""
    if not is_loopback_origin(url):
        return ""
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
            async with session.get(origin_for(url) + "/", allow_redirects=False) as response:
                if response.status != 200:
                    return ""
                page = (await response.content.read(_MAX_PAGE_BYTES)).decode("utf-8", errors="replace")
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
        return ""
    match = _PAGE_TOKEN.search(page)
    return match.group(1) if match else ""


@dataclass(frozen=True)
class GatewayTarget:
    origin: str
    ws_url: str
    token: str


@dataclass(frozen=True)
class Unreachable:
    kind: Literal["unreachable"] = "unreachable"
    tried: tuple[str, ...] = ()


@dataclass(frozen=True)
class NotDashboard:
    kind: Literal["not_dashboard"] = "not_dashboard"
    origin: str = ""
    hint: str = ""


@dataclass(frozen=True)
class AuthRequired:
    kind: Literal["auth_required"] = "auth_required"
    origin: str = ""
    ws_url: str = ""


@dataclass(frozen=True)
class TokenDead:
    kind: Literal["token_dead"] = "token_dead"
    origin: str = ""
    ws_url: str = ""


@dataclass(frozen=True)
class Ready:
    kind: Literal["ready"] = "ready"
    origin: str = ""
    ws_url: str = ""


DashboardProbe = Unreachable | NotDashboard | AuthRequired | TokenDead | Ready


def parse_gateway_target(raw: str) -> GatewayTarget:
    text = (raw or "").strip() or DEFAULT_ORIGIN
    if "://" not in text:
        text = f"http://{text}"
    parts = urlsplit(text)
    scheme = parts.scheme.lower()
    http_scheme = "https" if scheme in {"https", "wss"} else "http"
    ws_scheme = "wss" if http_scheme == "https" else "ws"
    netloc = parts.netloc or "127.0.0.1:9119"
    origin = urlunsplit((http_scheme, netloc, "", "", ""))
    token = ""
    if parts.query:
        token = (parse_qs(parts.query).get("token") or [""])[0]
    path = parts.path.rstrip("/")
    if not path.endswith("/api/ws"):
        path = "/api/ws"
    ws_url = urlunsplit((ws_scheme, netloc, path, "", ""))
    return GatewayTarget(origin=origin, ws_url=ws_url, token=token)


def socket_url(ws_url: str, token: str = "") -> str:
    parts = urlsplit(ws_url)
    query = dict(
        (key, values[-1])
        for key, values in parse_qs(parts.query).items()
        if values
    )
    if token:
        query["token"] = token
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def resolve_session_token(raw_url: str, env: dict[str, str] | None = None) -> str:
    source = env if env is not None else os.environ
    return (source.get(SESSION_TOKEN_ENV, "") or parse_gateway_target(raw_url).token).strip()


def probe_message(probe: DashboardProbe) -> str:
    if isinstance(probe, Unreachable):
        tried = ", ".join(probe.tried) or DEFAULT_ORIGIN
        return (
            f"Hermes dashboard not listening at {tried}. "
            f"Run `{DASHBOARD_COMMAND}`. A messaging gateway being up is a different door."
        )
    if isinstance(probe, NotDashboard):
        hint = probe.hint or "the socket did not send gateway.ready"
        return (
            f"{probe.origin or DEFAULT_ORIGIN} is not a Hermes dashboard ({hint}). "
            f"Bind the dashboard with `{DASHBOARD_COMMAND}`."
        )
    if isinstance(probe, AuthRequired):
        return (
            f"Dashboard at {probe.origin} wants a session token. "
            f"Paste the dashboard URL that includes ?token= or set {SESSION_TOKEN_ENV}."
        )
    if isinstance(probe, TokenDead):
        return (
            f"Dashboard at {probe.origin} closed the socket (4401). "
            f"The session token died, usually because the dashboard restarted. "
            f"Run `sleeptalk connect hermes` again."
        )
    return f"Hermes dashboard ready at {probe.ws_url}"


async def probe_dashboard(
    raw_url: str = DEFAULT_ORIGIN,
    *,
    token: str = "",
    timeout: float = 4.0,
    handshake: Any | None = None,
) -> DashboardProbe:
    target = parse_gateway_target(raw_url)
    secret = token.strip() or target.token
    url = socket_url(target.ws_url, secret)
    opener = handshake or _handshake
    status = await opener(url, timeout=timeout)
    if status == "ready":
        return Ready(origin=target.origin, ws_url=target.ws_url)
    if status == "auth_required":
        return AuthRequired(origin=target.origin, ws_url=target.ws_url)
    if status == "token_dead":
        return TokenDead(origin=target.origin, ws_url=target.ws_url)
    if status == "unreachable":
        return Unreachable(tried=(target.origin,))
    return NotDashboard(origin=target.origin, hint="no gateway.ready event")


async def _handshake(ws_url: str, *, timeout: float = 4.0) -> str:
    sent_token = "token=" in urlsplit(ws_url).query
    try:
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(
                ws_url, timeout=aiohttp.ClientTimeout(total=timeout)
            ) as ws:
                message = await asyncio.wait_for(ws.receive(), timeout=timeout)
                if message.type in {aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED}:
                    code = ws.close_code if ws.close_code is not None else message.data
                    if code == _CLOSE_UNAUTHORIZED:
                        return "token_dead" if sent_token else "auth_required"
                    return "not_dashboard"
                if message.type == aiohttp.WSMsgType.TEXT:
                    try:
                        payload = json.loads(message.data)
                    except json.JSONDecodeError:
                        return "not_dashboard"
                    if isinstance(payload, dict) and payload.get("method") == "event":
                        # Dashboards wrap events in JSON-RPC: {"method": "event", "params": {...}}.
                        payload = payload.get("params")
                    if isinstance(payload, dict) and payload.get("type") == "gateway.ready":
                        return "ready"
                    return "not_dashboard"
                return "not_dashboard"
    except aiohttp.ClientConnectionError:
        return "unreachable"
    except aiohttp.WSServerHandshakeError as exc:
        # Newer dashboards refuse an unauthenticated socket at the HTTP upgrade.
        if exc.status in {401, 403}:
            return "token_dead" if sent_token else "auth_required"
        return "not_dashboard"
    except (aiohttp.ClientError, asyncio.TimeoutError):
        # Something else answers on this port (an HTTP 404 on /api/ws, say).
        return "not_dashboard"


def apply_dashboard(answers: Any, probe: Ready, token: str) -> None:
    answers.agent_type = "hermes_gateway"
    answers.gateway_url = probe.ws_url
    answers.gateway_session_token = token.strip()


async def attach_hermes(
    answers: Any,
    *,
    raw_url: str = "",
    token: str = "",
    env: dict[str, str] | None = None,
    handshake: Any | None = None,
) -> DashboardProbe:
    target = parse_gateway_target(raw_url)
    # The session token saved in the environment belongs to the local
    # dashboard: it is never sent to an address somewhere else.
    secret = token.strip() or (resolve_session_token(raw_url, env) if is_loopback_origin(target.origin) else target.token)
    probe = await probe_dashboard(
        target.origin, token=secret, handshake=handshake
    )
    if isinstance(probe, (AuthRequired, TokenDead)):
        discovered = await read_local_session_token(target.origin)
        if discovered:
            probe = await probe_dashboard(target.origin, token=discovered, handshake=handshake)
            if isinstance(probe, Ready):
                # The token changes whenever the dashboard restarts, so it is
                # read again at call time instead of being saved.
                apply_dashboard(answers, probe, "")
                return probe
    if isinstance(probe, Ready):
        apply_dashboard(answers, probe, secret)
    return probe
