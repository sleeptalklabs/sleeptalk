"""One-call, package-owned rendezvous for same-machine browser voice."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import math
import mimetypes
import secrets
import time
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import partial
from importlib import resources
from importlib.resources.abc import Traversable
from types import TracebackType
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import uuid4

from aiortc import MediaStreamError
from aiohttp import WSMsgType, web

from sleeptalk.call_provider import (
    BidirectionalAudioStream,
    CallContext,
    CallEnded,
    CallProviderError,
    CallTicket,
    P2PCallProvider,
)

# Local and tailnet calls use nothing metered, so they have no session cap;
# 0 means none. Silence still ends them. The hosted relay keeps its own cap.
_SESSION_CAP_SECONDS = 0
_INACTIVITY_SECONDS = 90
_MAX_SIGNAL_BYTES = 64 * 1024
_SIGNAL_TYPES = frozenset(
    {
        "call.auth",
        "webrtc.offer",
        "webrtc.answer",
        "webrtc.ice",
        "webrtc.ice_complete",
        "call.hangup",
        "call.cancel",
        "call.interrupt",
        "call.settings",
        "call.error",
        "call.telemetry",
        # Transcript and agent-state mirrors use the peer's data channel.
        "ping",
    }
)
_TERMINAL_TYPES = frozenset({"call.hangup", "call.cancel"})
_SECURITY_HEADERS = {
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "Permissions-Policy": "camera=(), microphone=(self)",
    "Content-Security-Policy": (
        "default-src 'self'; connect-src 'self' ws: wss:; "
        "media-src 'self' blob:; script-src 'self'; style-src 'self'; "
        "img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'"
    ),
}


@dataclass(slots=True)
class _LocalTicket:
    token_hash: bytes
    agent_key: str
    expires_at: float
    state: Literal["issued", "redeemed", "ended"] = "issued"
    browser_key: str = ""
    session_expires_at: float = 0.0
    last_activity_at: float = 0.0
    sockets: dict[str, web.WebSocketResponse] = field(default_factory=dict)
    offer_seen: bool = False
    handled_terminal_ids: set[str] = field(default_factory=set)


def _validated_public_origin(value: str) -> str:
    """An https origin a trusted proxy publishes this loopback server under."""
    origin = value.strip().rstrip("/")
    if not origin:
        return ""
    parts = urlsplit(origin)
    if parts.scheme not in {"http", "https"} or not parts.netloc or parts.path or parts.query or parts.fragment:
        raise CallProviderError("A public origin is a scheme and host, such as https://host.example.ts.net:8443.")
    return f"{parts.scheme}://{parts.netloc}"


class LocalCallProvider:
    """A loopback-only rendezvous and call provider for one browser call.

    With ``public_origin``, a trusted proxy on this host (``tailscale serve``)
    publishes the loopback port under that origin: the join link, the
    browser's signaling URL, and the browser checks use it, while the host's
    own connection stays on loopback.
    """

    def __init__(self, *, public_origin: str = "") -> None:
        self.origin = ""
        self.public_origin = _validated_public_origin(public_origin)
        self._public_host = urlsplit(self.public_origin).netloc if self.public_origin else ""
        self._host_header = ""
        self._runner: web.AppRunner | None = None
        self._ticket: _LocalTicket | None = None
        self._routing_id = ""
        self._lock = asyncio.Lock()
        self._deadline_changed = asyncio.Event()
        self._watchdog: asyncio.Task[None] | None = None

    async def __aenter__(self) -> LocalCallProvider:
        app = web.Application(client_max_size=8 * 1024)
        app.router.add_post("/api/tickets/{routing_id}/redeem", self._redeem)
        app.router.add_get("/api/tickets/{routing_id}/signal", self._signal)
        root = resources.files("sleeptalk").joinpath("_web")
        assets = {
            "": root.joinpath("index.html"),
            "index.html": root.joinpath("index.html"),
            "sleep.mp4": root.joinpath("sleep.mp4"),
        }
        assets.update(
            (f"assets/{asset.name}", asset)
            for asset in root.joinpath("assets").iterdir()
            if asset.is_file()
        )
        for path, asset in assets.items():
            app.router.add_get(f"/{path}", partial(self._asset, asset=asset))
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        addresses = self._runner.addresses
        if len(addresses) != 1:
            await self.close()
            raise CallProviderError("The local browser service could not bind a port.")
        host, port = addresses[0][:2]
        self._host_header = f"{host}:{port}"
        self.origin = self.public_origin or f"http://{self._host_header}"
        return self

    @property
    def port(self) -> int:
        return int(self._host_header.rsplit(":", 1)[1]) if self._host_header else 0

    def _browser_signal_url(self) -> str:
        scheme = "wss" if self.origin.startswith("https://") else "ws"
        return f"{scheme}://{self._public_host or self._host_header}/api/tickets/{self._routing_id}/signal"

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.close()

    async def close(self) -> None:
        watchdog = self._watchdog
        self._watchdog = None
        if watchdog is not None and watchdog is not asyncio.current_task():
            watchdog.cancel()
            try:
                await watchdog
            except asyncio.CancelledError:
                pass
        await self._finish("call_ended", notify=False)
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    async def mint(
        self,
        context: CallContext,
        *,
        ttl_seconds: int = 300,
    ) -> CallTicket:
        if not self.origin:
            raise CallProviderError("The local browser service is not running.")
        if self._ticket is not None:
            raise CallProviderError("This local browser service already owns a call.")
        if not 1 <= ttl_seconds <= 300:
            raise CallProviderError("Call links must expire between 1 and 300 seconds.")
        context.as_payload()
        token = secrets.token_urlsafe(32)
        self._routing_id = secrets.token_urlsafe(18)
        now = time.monotonic()
        self._ticket = _LocalTicket(
            token_hash=hashlib.sha256(token.encode()).digest(),
            agent_key=secrets.token_urlsafe(32),
            expires_at=now + ttl_seconds,
        )
        self._watchdog = asyncio.create_task(
            self._watch_ticket(), name="local-rendezvous-timeout"
        )
        path = f"/api/tickets/{self._routing_id}/signal"
        expires_at = datetime.fromtimestamp(
            time.time() + ttl_seconds, tz=timezone.utc
        ).isoformat().replace("+00:00", "Z")
        return CallTicket(
            token=token,
            routing_id=self._routing_id,
            join_url=f"{self.origin}/#/call/{self._routing_id}/{token}",
            agent_signal_url=f"ws://{self._host_header}{path}",
            agent_access_key=self._ticket.agent_key,
            expires_at=expires_at,
            session_cap_seconds=_SESSION_CAP_SECONDS,
            inactivity_seconds=_INACTIVITY_SECONDS,
            ice_servers=(),
            signal_types=_SIGNAL_TYPES | {"ticket.redeemed"},
        )

    def connect(
        self, ticket: CallTicket
    ) -> AbstractAsyncContextManager[BidirectionalAudioStream]:
        return P2PCallProvider(self.origin).connect(ticket)

    async def _asset(self, request: web.Request, *, asset: Traversable) -> web.Response:
        if not self._trusted_host(request):
            return self._json_error(400, "invalid_host", "Invalid request host.")
        if not asset.is_file():
            return self._json_error(
                503, "assets_missing", "The local browser assets are unavailable."
            )
        content_type, _encoding = mimetypes.guess_type(str(asset))
        # The page may open its signalling socket to this server only (the
        # host was just checked), not to any ws: or wss: address.
        # Published over a tailnet, the browser reaches it as wss://<public host>.
        scheme = "wss" if self.origin.startswith("https://") else "ws"
        socket_origin = f"{scheme}://{self._public_host or self._host_header}"
        headers = {**_SECURITY_HEADERS, "Content-Security-Policy": _SECURITY_HEADERS["Content-Security-Policy"].replace(
            "connect-src 'self' ws: wss:;", f"connect-src 'self' {socket_origin};")}
        return web.Response(
            body=asset.read_bytes(),
            content_type=content_type or "application/octet-stream",
            headers=headers,
        )

    async def _redeem(self, request: web.Request) -> web.Response:
        if not self._trusted_browser_request(request):
            return self._json_error(403, "origin_rejected", "Invalid browser origin.")
        if request.match_info["routing_id"] != self._routing_id:
            return self._json_error(404, "invalid_ticket", "This call link isn't valid.")
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            return self._json_error(400, "invalid_ticket", "Ticket proof must be JSON.")
        supplied = body.get("ticket", "") if isinstance(body, dict) else ""
        supplied_hash = hashlib.sha256(str(supplied).encode()).digest()
        async with self._lock:
            ticket = self._ticket
            now = time.monotonic()
            if ticket is None or not hmac.compare_digest(
                supplied_hash, ticket.token_hash
            ):
                return self._json_error(
                    404, "invalid_ticket", "This call link isn't valid."
                )
            if ticket.state == "ended" or now >= ticket.expires_at:
                return self._json_error(
                    410, "ticket_expired", "This call link has expired."
                )
            if ticket.state != "issued":
                return self._json_error(
                    409, "ticket_used", "This call link has already been used."
                )
            ticket.state = "redeemed"
            ticket.browser_key = secrets.token_urlsafe(32)
            ticket.last_activity_at = now
            ticket.session_expires_at = now + _SESSION_CAP_SECONDS if _SESSION_CAP_SECONDS else math.inf
            self._deadline_changed.set()
            browser_key = ticket.browser_key
            agent_socket = ticket.sockets.get("agent")
            session_expires_at = (
                datetime.fromtimestamp(time.time() + _SESSION_CAP_SECONDS, tz=timezone.utc)
                .isoformat().replace("+00:00", "Z")
                if _SESSION_CAP_SECONDS else ""
            )
        if agent_socket is not None:
            await self._send(agent_socket, self._server_signal("ticket.redeemed"))
        return web.json_response(
            {
                "browserSignalUrl": self._browser_signal_url(),
                "browserAccessKey": browser_key,
                "iceServers": [],
                "sessionExpiresAt": session_expires_at,
                "sessionCapSeconds": _SESSION_CAP_SECONDS,
                "inactivitySeconds": _INACTIVITY_SECONDS,
                "iceTransportPolicy": "all",
            },
            headers={"Cache-Control": "no-store"},
        )

    async def _signal(self, request: web.Request) -> web.StreamResponse:
        if not self._trusted_host(request):
            return self._json_error(400, "invalid_host", "Invalid request host.")
        if request.query_string:
            return self._json_error(
                400, "invalid_signal_url", "Signal URLs cannot contain parameters."
            )
        if request.match_info["routing_id"] != self._routing_id:
            return self._json_error(404, "invalid_ticket", "This call link isn't valid.")
        websocket = web.WebSocketResponse(max_msg_size=_MAX_SIGNAL_BYTES)
        await websocket.prepare(request)
        role = ""
        try:
            async for message in websocket:
                if message.type is not WSMsgType.TEXT:
                    await websocket.close(
                        code=4002, message=b"Text signaling messages only."
                    )
                    break
                envelope = self._parse_signal(message.data)
                if envelope is None:
                    await websocket.close(
                        code=4002, message=b"Invalid signaling message."
                    )
                    break
                if not role:
                    role = await self._authenticate(request, websocket, envelope)
                    if not role:
                        break
                    await self._announce_ready()
                    continue
                await self._relay(role, websocket, envelope)
        finally:
            if role:
                await self._peer_disconnected(role, websocket)
        return websocket

    async def _authenticate(
        self,
        request: web.Request,
        websocket: web.WebSocketResponse,
        envelope: dict[str, Any],
    ) -> str:
        payload = envelope["payload"]
        role = payload.get("role") if envelope["type"] == "call.auth" else ""
        key = payload.get("key", "")
        if role not in {"agent", "browser"} or not isinstance(key, str):
            await websocket.close(code=4003, message=b"Authentication required.")
            return ""
        if role == "browser" and request.headers.get("Origin", "") != self.origin:
            await websocket.close(code=4003, message=b"Invalid browser origin.")
            return ""
        async with self._lock:
            ticket = self._ticket
            expected = ""
            if ticket is not None:
                expected = ticket.agent_key if role == "agent" else ticket.browser_key
            authorized = bool(expected) and hmac.compare_digest(key, expected)
            occupied = ticket is not None and role in ticket.sockets
            if ticket is None or ticket.state == "ended" or not authorized or occupied:
                code = 4009 if occupied else 4003
                await websocket.close(code=code, message=b"Invalid call credential.")
                return ""
            ticket.sockets[role] = websocket
            ticket.last_activity_at = time.monotonic()
            self._deadline_changed.set()
            redeemed = ticket.state == "redeemed"
        if role == "agent" and redeemed:
            await self._send(websocket, self._server_signal("ticket.redeemed"))
        return role

    async def _announce_ready(self) -> None:
        async with self._lock:
            ticket = self._ticket
            sockets = list(ticket.sockets.values()) if ticket else []
            ready = ticket is not None and {"agent", "browser"} <= ticket.sockets.keys()
        if ready:
            envelope = self._server_signal("peer.ready")
            for socket in sockets:
                await self._send(socket, envelope)

    async def _relay(
        self,
        role: str,
        websocket: web.WebSocketResponse,
        envelope: dict[str, Any],
    ) -> None:
        kind = envelope["type"]
        if kind == "call.auth":
            await websocket.close(code=4002, message=b"Already authenticated.")
            return
        if kind == "ping":
            async with self._lock:
                ticket = self._ticket
                if ticket is not None and ticket.state != "ended":
                    ticket.last_activity_at = time.monotonic()
                    self._deadline_changed.set()
            await self._send(websocket, self._server_signal("pong", envelope["payload"]))
            return
        async with self._lock:
            ticket = self._ticket
            if ticket is None or ticket.state == "ended":
                await websocket.close(code=4004, message=b"Call ended.")
                return
            if kind == "webrtc.answer" and not ticket.offer_seen:
                await self._send(
                    websocket,
                    self._server_signal(
                        "call.error",
                        {
                            "code": "answer_before_offer",
                            "detail": "Answer received before offer.",
                        },
                    ),
                )
                return
            if (
                kind in _TERMINAL_TYPES
                and envelope["id"] in ticket.handled_terminal_ids
            ):
                return
            if kind == "webrtc.offer":
                ticket.offer_seen = True
            if kind in _TERMINAL_TYPES:
                ticket.handled_terminal_ids.add(envelope["id"])
            ticket.last_activity_at = time.monotonic()
            self._deadline_changed.set()
            recipient = ticket.sockets.get("browser" if role == "agent" else "agent")
        if recipient is not None:
            await self._send(recipient, envelope)
        if kind in _TERMINAL_TYPES:
            await self._finish("user_cancel")

    async def _peer_disconnected(
        self, role: str, websocket: web.WebSocketResponse
    ) -> None:
        async with self._lock:
            ticket = self._ticket
            if ticket is None or ticket.sockets.get(role) is not websocket:
                return
            ticket.sockets.pop(role, None)
            active = ticket.state != "ended"
        if active:
            await self._finish(f"{role}_disconnected")

    async def _watch_ticket(self) -> None:
        while True:
            async with self._lock:
                ticket = self._ticket
                if ticket is None or ticket.state == "ended":
                    return
                self._deadline_changed.clear()
                now = time.monotonic()
                if ticket.state == "issued":
                    deadline = ticket.expires_at
                    reason = "ticket_expired"
                else:
                    deadline = min(
                        ticket.session_expires_at,
                        ticket.last_activity_at + _INACTIVITY_SECONDS,
                    )
                    reason = (
                        "session_cap"
                        if deadline == ticket.session_expires_at
                        else "inactivity_timeout"
                    )
            delay = deadline - now
            if delay > 0:
                try:
                    await asyncio.wait_for(
                        self._deadline_changed.wait(), timeout=delay
                    )
                except TimeoutError:
                    pass
                if self._deadline_changed.is_set():
                    continue
            async with self._lock:
                ticket = self._ticket
                if ticket is None or ticket.state == "ended":
                    return
                now = time.monotonic()
                if ticket.state == "issued":
                    expired = now >= ticket.expires_at
                else:
                    expired = now >= min(
                        ticket.session_expires_at,
                        ticket.last_activity_at + _INACTIVITY_SECONDS,
                    )
            if not expired:
                continue
            await self._finish(reason)
            return

    async def _finish(self, reason: str, *, notify: bool = True) -> None:
        async with self._lock:
            ticket = self._ticket
            if ticket is None or ticket.state == "ended":
                return
            ticket.state = "ended"
            sockets = list(ticket.sockets.values())
            ticket.sockets.clear()
        if notify:
            envelope = self._server_signal("call.ended", {"reason": reason})
            for socket in sockets:
                await self._send(socket, envelope)
        for socket in sockets:
            if not socket.closed:
                await socket.close(code=1000, message=b"Call ended.")

    def _trusted_host(self, request: web.Request) -> bool:
        host = request.headers.get("Host", "")
        if host == self._host_header:
            # The host's own loopback connection, or a proxy that rewrote Host
            # and forwarded the public one.
            return not self._public_host or request.headers.get("X-Forwarded-Host", self._public_host) == self._public_host
        return bool(self._public_host) and host == self._public_host

    def _trusted_browser_request(self, request: web.Request) -> bool:
        return (
            self._trusted_host(request)
            and request.headers.get("Origin", "") == self.origin
        )

    @staticmethod
    def _parse_signal(raw: str) -> dict[str, Any] | None:
        if len(raw.encode()) > _MAX_SIGNAL_BYTES:
            return None
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if not isinstance(value, dict) or value.get("v") != 1:
            return None
        signal_id = value.get("id")
        kind = value.get("type")
        payload = value.get("payload")
        if (
            not isinstance(signal_id, str)
            or not signal_id
            or len(signal_id) > 128
            or kind not in _SIGNAL_TYPES
            or not isinstance(payload, dict)
        ):
            return None
        return value

    @staticmethod
    def _server_signal(
        kind: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return {"v": 1, "id": str(uuid4()), "type": kind, "payload": payload or {}}

    @staticmethod
    async def _send(socket: web.WebSocketResponse, value: dict[str, Any]) -> None:
        if not socket.closed:
            await socket.send_json(value)

    @staticmethod
    def _json_error(status: int, code: str, message: str) -> web.Response:
        return web.json_response(
            {"error": {"code": code, "message": message}},
            status=status,
            headers={"Cache-Control": "no-store"},
        )


async def run_local_echo_call(
    context: CallContext,
    *,
    ttl_seconds: int,
    on_ready: Callable[[str], None],
    tailnet: bool = False,
) -> None:
    """Run the development echo fixture through the package-owned server."""
    if tailnet:
        from sleeptalk.tailnet import published_provider

        server = published_provider()
    else:
        server = LocalCallProvider()
    async with server as provider:
        ticket = await provider.mint(context, ttl_seconds=ttl_seconds)
        on_ready(ticket.join_url)
        try:
            async with provider.connect(ticket) as stream:
                while True:
                    frame = await stream.recv()
                    await stream.send(frame)
        except (CallEnded, MediaStreamError):
            return
