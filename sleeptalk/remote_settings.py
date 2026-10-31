"""The settings page on a phone: the same page, driven over the sealed setup channel.

A host with no browser of its own (a VPS reached through chat) can't open
``sleeptalk settings --open`` for its user. Instead it mints a single-use
setup link. The phone loads the settings page from the hosted call service
and opens the sealed channel to this process (see ``setup_link``); every
request the page makes, and every icon, word list, and voice sample it
loads, then travels through that channel to an ordinary settings server
running on this host's loopback. That server keeps all of its own rules:
the token exchange, CSRF, Host and Origin checks, and the idle timeout. The
relay only ever carries ciphertext.

Trust: whoever opens the link first can change this host's Sleeptalk
settings until the room ends. The page is marked remote, so beyond that it
can only install one of the listed agents by its built-in command and set
up, start, or stop a Discord bot (a service that outlives the room). It
can't set a program to run as the agent, move keys into the Keychain, or
open a Discord bot to everyone (see ``settings_page.REMOTE_REFUSED``).
Never open the link from an agent tool.
"""
from __future__ import annotations

import asyncio
import base64
import json
import re
from typing import Any

import aiohttp
from cryptography.hazmat.primitives.asymmetric import ec

from sleeptalk.call_provider import CallTicket
from sleeptalk.setup_link import LinkError, SetupChannel, _envelope, link_url

# The page may ask the host for its API and for the page's own static files;
# the page itself, and anything else on the host, is not reachable.
_TUNNELLED = re.compile(r"^/(api/[a-z/-]+(\?[A-Za-z0-9=&_.-]*)?|i18n/[a-z]{2}\.json|icons/[a-z_]+\.svg|"
                        r"samples/[a-z]+/(?:[a-z]{2}/)?[A-Za-z0-9_.-]+\.mp3)$")
# Base64 characters per sealed reply part; keeps each relay message well under its 64 KB cap.
CHUNK = 30_000
MAX_BODY = 16 * 1024
# Channels one room may open (each page load or reconnect opens one).
MAX_HELLOS = 20
# Requests the page may have in flight at once.
MAX_IN_FLIGHT = 8


def settings_url(ticket: CallTicket, private_key: ec.EllipticCurvePrivateKey) -> str:
    """The phone link: a setup room, opened by the phone's settings page."""
    return link_url(ticket, private_key).replace("/#/setup/", "/settings#/settings/", 1)


class SettingsBridge:
    """A settings server on this host's loopback, with one authenticated browser session held here."""

    def __init__(self, page) -> None:
        self.page = page
        self._runner: Any = None
        self._client: aiohttp.ClientSession | None = None
        self._csrf = ""

    async def start(self) -> None:
        from aiohttp import web

        self._runner = web.AppRunner(self.page.app(), access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        self.page.origin = f"http://127.0.0.1:{self._runner.addresses[0][1]}"
        self._client = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True),
                                             timeout=aiohttp.ClientTimeout(total=120))
        async with self._client.post(self.page.origin + "/session", json={"token": self.page.token},
                                     headers={"Origin": self.page.origin}) as response:
            response.raise_for_status()
            self._csrf = (await response.json())["csrf"]

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
        if self._runner is not None:
            await self._runner.cleanup()

    async def call(self, method: str, path: str, body: Any) -> tuple[int, str, bytes]:
        """(status, content type, body) of one request the page made."""
        if method not in {"GET", "POST"} or not isinstance(path, str) or not _TUNNELLED.fullmatch(path):
            return 404, "application/json", b'{"status":"refused","code":"not_found"}'
        data = json.dumps(body).encode() if method == "POST" else None
        if data is not None and len(data) > MAX_BODY:
            return 413, "application/json", b'{"status":"refused","code":"too_large"}'
        assert self._client is not None
        # Installing an agent runs up to ten minutes; everything else answers in seconds.
        timeout = aiohttp.ClientTimeout(total=660) if path == "/api/agent/install" else None
        async with self._client.request(method, self.page.origin + path, data=data, allow_redirects=False, timeout=timeout, headers={
                "Origin": self.page.origin, "X-Settings-CSRF": self._csrf, "Content-Type": "application/json"}) as response:
            return response.status, response.content_type, await response.read()


class SettingsRoom:
    """The host side of one phone settings room."""

    def __init__(self, page, ticket: CallTicket, private_key: ec.EllipticCurvePrivateKey) -> None:
        self.page = page
        self.ticket = ticket
        self.private_key = private_key
        # Reached from somewhere else: settings, and installing a listed agent;
        # never a program of the page's choosing.
        page.remote = True
        self.bridge = SettingsBridge(page)
        self.channel: SetupChannel | None = None
        # Every hello this room has accepted: a recorded one can't be replayed
        # to reopen an old channel and re-run its requests.
        self._hellos: set[str] = set()
        self._send_lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()
        self._socket: Any = None

    async def run(self, *, until: asyncio.Event | None = None) -> None:
        await self.bridge.start()
        try:
            async with aiohttp.ClientSession() as http, http.ws_connect(
                    self.ticket.agent_signal_url, heartbeat=20, max_msg_size=128 * 1024) as socket:
                self._socket = socket
                await socket.send_str(_envelope("call.auth", {"role": "agent", "key": self.ticket.agent_access_key}))
                stopper = asyncio.create_task((until or asyncio.Event()).wait())
                reader = asyncio.create_task(self._read(socket))
                done, _ = await asyncio.wait({stopper, reader}, return_when=asyncio.FIRST_COMPLETED)
                for task in (stopper, reader, *self._tasks):
                    task.cancel()
                if stopper in done:
                    await socket.send_str(_envelope("call.cancel", {"reason": "setup_ended"}))
        finally:
            await self.bridge.close()

    async def _read(self, socket) -> None:
        async for message in socket:
            if message.type != aiohttp.WSMsgType.TEXT:
                return
            try:
                envelope = json.loads(message.data)
            except json.JSONDecodeError:
                continue
            kind = envelope.get("type") if isinstance(envelope, dict) else None
            payload = envelope.get("payload") if isinstance(envelope, dict) else None
            if kind == "peer.ready":
                self.channel = None
            elif kind == "setup.hello":
                enc = payload.get("enc") if isinstance(payload, dict) else None
                if not isinstance(enc, str) or enc in self._hellos or len(self._hellos) >= MAX_HELLOS:
                    continue
                try:
                    self.channel = SetupChannel.accept(self.private_key, payload, routing_id=self.ticket.routing_id)
                except LinkError:
                    continue
                self._hellos.add(enc)
            elif kind == "setup.sealed" and self.channel is not None:
                try:
                    inner, body = self.channel.open(payload)
                except LinkError:
                    continue
                if inner == "settings.call" and len(self._tasks) < MAX_IN_FLIGHT:
                    task = asyncio.create_task(self._answer(self.channel, body))
                    self._tasks.add(task)
                    task.add_done_callback(self._tasks.discard)
            elif kind in {"call.ended", "call.cancel"}:
                return

    async def _answer(self, channel: SetupChannel, call: dict) -> None:
        call_id = call.get("id")
        if not isinstance(call_id, int) or isinstance(call_id, bool) or not 0 < call_id < 2 ** 31:
            return
        try:
            status, content_type, data = await self.bridge.call(call.get("method"), call.get("path"), call.get("body"))
        except (aiohttp.ClientError, asyncio.TimeoutError):
            status, content_type, data = 502, "application/json", b'{"status":"refused","code":"host_unavailable"}'
        encoded = base64.b64encode(data).decode()
        parts = [encoded[i:i + CHUNK] for i in range(0, len(encoded), CHUNK)] or [""]
        async with self._send_lock:
            if channel is not self.channel:
                return  # the page reconnected; this answer belongs to the old channel
            for index, part in enumerate(parts):
                reply = {"id": call_id, "part": index, "parts": len(parts), "data": part}
                if index == 0:
                    reply.update(status=status, content_type=content_type)
                await self._socket.send_str(_envelope("setup.sealed", channel.seal("settings.reply", reply)))


async def serve_link(page, *, provider=None, ready=None, ttl_seconds: int = 600) -> None:
    """Mint a phone settings link for ``page``, report it through ``ready``, and serve until the room ends."""
    from sleeptalk.call_provider import P2PCallProvider
    from sleeptalk.setup_core import resolved_rendezvous_url

    private_key = ec.generate_private_key(ec.SECP256R1())
    provider = provider or P2PCallProvider(resolved_rendezvous_url())
    ticket = await provider.mint_setup(ttl_seconds=ttl_seconds)
    if ready is not None:
        ready(settings_url(ticket, private_key), ticket)
    try:
        await SettingsRoom(page, ticket, private_key).run()
    finally:
        del private_key


def run_link(*, profile: str = "") -> int:
    """`sleeptalk settings --phone`: print a single-use phone link to this host's settings."""
    from sleeptalk.home import resolve_layout
    from sleeptalk.settings_page import SettingsPage, editable_profiles

    layout = resolve_layout()
    profiles = editable_profiles(layout.profiles_dir) if layout.profiles_dir.is_dir() else []
    slug = profile or ("default" if "default" in profiles or not profiles else profiles[0])
    if profile and slug not in profiles:
        print(f"No profile named {slug!r}. Profiles here: {', '.join(profiles) or 'none'}")
        return 2
    page = SettingsPage(layout.profiles_dir / f"{slug}.toml", profiles_dir=layout.profiles_dir,
                        env_path=layout.env_path)

    def announce(url: str, ticket: CallTicket) -> None:
        # Flushed: under nohup, a service or an agent's pipe, output is block
        # buffered and the link would sit unseen until the page closed.
        print(f"Settings on your phone: {url}", flush=True)
        print("Single use: it opens once, then works until you close it or 15 minutes pass. Ctrl-C to stop.",
              flush=True)

    try:
        asyncio.run(serve_link(page, ready=announce))
    except KeyboardInterrupt:
        pass
    return 0




def start_detached(*, profile: str = "", timeout: float = 20.0) -> dict[str, Any]:
    """`sleeptalk host settings-link`: serve a phone settings link from a background
    process and return a receipt with the link, for an agent to send."""
    import os
    import subprocess
    import sys
    import tempfile
    import time
    from pathlib import Path

    directory = Path(tempfile.mkdtemp(prefix="sleeptalk-settings-"))
    os.chmod(directory, 0o700)
    out = directory / "link.json"
    subprocess.Popen([sys.executable, "-m", "sleeptalk.remote_settings", "--out", str(out), *(["--profile", profile] if profile else [])],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     close_fds=True, start_new_session=True)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if out.exists():
            result = json.loads(out.read_text(encoding="utf-8"))
            out.unlink(missing_ok=True)
            directory.rmdir()
            if result.get("url"):
                return {"schema_version": 1, "operation": "settings_link", "status": "ready", "settings_url": result["url"],
                        "say": "Here's a private link to your Sleeptalk settings. It opens once, on any device, "
                               "and closes after 15 minutes or when you're done.",
                        "remediation": "Send settings_url to the user privately (a DM). Never open it yourself."}
            return {"schema_version": 1, "operation": "settings_link", "status": "failed",
                    "next_action": str(result.get("error") or "The settings link couldn't start.")}
        time.sleep(0.05)
    return {"schema_version": 1, "operation": "settings_link", "status": "failed",
            "next_action": "The settings link didn't start in time. Check the network, then try again."}


def main() -> int:
    import argparse
    import logging
    from pathlib import Path

    from sleeptalk.home import resolve_layout
    from sleeptalk.private_input import atomic_json
    from sleeptalk.settings_page import SettingsPage, editable_profiles

    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--profile", default="")
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    layout = resolve_layout()
    profiles = editable_profiles(layout.profiles_dir) if layout.profiles_dir.is_dir() else []
    if args.profile and args.profile not in profiles:
        atomic_json(args.out, {"error": f"No profile named {args.profile!r}."})
        return 2
    slug = args.profile or ("default" if "default" in profiles or not profiles else profiles[0])
    page = SettingsPage(layout.profiles_dir / f"{slug}.toml", profiles_dir=layout.profiles_dir, env_path=layout.env_path)
    try:
        asyncio.run(serve_link(page, ready=lambda url, ticket: atomic_json(args.out, {"url": url})))
    except Exception as exc:  # noqa: BLE001 — the waiting command reports it
        from sleeptalk.call_provider import CallProviderError

        atomic_json(args.out, {"error": str(exc) if isinstance(exc, CallProviderError) else "The settings link couldn't start."})
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
