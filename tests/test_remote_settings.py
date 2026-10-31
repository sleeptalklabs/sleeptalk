"""The settings page on a phone: requests through the sealed channel to a loopback settings server."""
from __future__ import annotations

import asyncio
import base64
import json

from aiohttp import web
from aiohttp.test_utils import TestServer
from cryptography.hazmat.primitives.asymmetric import ec

from sleeptalk import remote_settings, setup_link
from sleeptalk.call_provider import CallTicket
from sleeptalk.settings_page import SettingsPage
from tests.test_setup_link import ROUTING, FakeRoom, PageChannel
from tests.test_settings_page import PROFILE


async def _room(tmp_path):
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    (profiles / "default.toml").write_text(PROFILE)
    page = SettingsPage(profiles / "default.toml", profiles_dir=profiles, environ={}, state_dir=tmp_path / "state")
    fake = FakeRoom()
    app = web.Application()
    app.router.add_get(f"/api/tickets/{ROUTING}/signal", fake.handler)
    server = TestServer(app)
    await server.start_server()
    ticket = CallTicket(token="t" * 43, routing_id=ROUTING, join_url=f"http://x/#/setup/{ROUTING}/{'t' * 43}",
                        agent_signal_url=str(server.make_url(f"/api/tickets/{ROUTING}/signal")).replace("http", "ws"),
                        agent_access_key="agent-key", expires_at="", session_cap_seconds=900,
                        inactivity_seconds=300, ice_servers=())
    key = ec.generate_private_key(ec.SECP256R1())
    fake.host_key = key.public_key()
    return fake, server, remote_settings.SettingsRoom(page, ticket, key), key


async def _call(fake, call_id, method, path, body=None):
    await fake.send_raw("setup.sealed", fake.page.seal("settings.call", {"id": call_id, "method": method, "path": path, "body": body}))
    parts, head = {}, {}
    while True:
        envelope = await asyncio.wait_for(fake.from_host.get(), 10)
        assert envelope["type"] == "setup.sealed", envelope
        inner, reply = fake.page.open(envelope["payload"])
        assert inner == "settings.reply" and reply["id"] == call_id
        if reply["part"] == 0:
            head = reply
        parts[reply["part"]] = reply["data"]
        if len(parts) == reply["parts"]:
            break
    data = base64.b64decode("".join(parts[i] for i in range(len(parts))))
    return head["status"], head["content_type"], data


async def test_the_phone_page_reads_and_changes_settings_through_the_channel(tmp_path, monkeypatch):
    async def quiet(*, environ=None):
        return {"available": False, "code": "tailscale_missing"}

    monkeypatch.setattr("sleeptalk.tailnet.detect", quiet)
    fake, server, room, key = await _room(tmp_path)
    task = asyncio.create_task(room.run())
    try:
        assert (await fake.expect("call.auth"))["role"] == "agent"
        await fake.send("peer.ready")  # opens the page's channel
        status, kind, data = await _call(fake, 1, "GET", "/api/state")
        state = json.loads(data)
        assert status == 200 and kind == "application/json" and state["profile"] == "default"
        assert len(data) > remote_settings.CHUNK  # a big reply really was split and put back together
        status, _, data = await _call(fake, 2, "POST", "/api/set", {"setting": "policy.reply_length", "value": "summarized"})
        assert status == 200, data
        assert 'reply_length = "summarized"' in (tmp_path / "profiles" / "default.toml").read_text()
        status, kind, data = await _call(fake, 3, "GET", "/i18n/ja.json")
        assert status == 200 and json.loads(data)
        # The page itself, the session exchange, and anything off the list stay out of reach.
        for path in ("/", "/session", "/settings.js", "/../etc/passwd", "/api/state/../../x"):
            status, _, _ = await _call(fake, 4, "GET", path)
            assert status == 404, path
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await server.close()
    # The relay saw only the channel: no profile, setting, or voice in the clear.
    assert {json.loads(raw)["type"] for raw in fake.raw} <= {"call.auth", "setup.sealed"}
    assert "default" not in "".join(fake.raw) and "reply_length" not in "".join(fake.raw)


def test_the_phone_link_opens_the_settings_route():
    key = ec.generate_private_key(ec.SECP256R1())
    ticket = CallTicket(token="t" * 43, routing_id=ROUTING, join_url=f"https://call.sleep.talk/#/setup/{ROUTING}/{'t' * 43}",
                        agent_signal_url="wss://x", agent_access_key="k", expires_at="", session_cap_seconds=900,
                        inactivity_seconds=300, ice_servers=())
    url = remote_settings.settings_url(ticket, key)
    assert url.startswith(f"https://call.sleep.talk/settings#/settings/{ROUTING}/{'t' * 43}/")
    assert setup_link.unb64url(url.rsplit("/", 1)[1], limit=128)[0] == 4


async def test_a_remote_page_can_change_settings_but_never_run_programs(tmp_path, monkeypatch):
    """A phone or call link reaches settings only: no program as the agent,
    no installs, no Keychain move, no opening a Discord bot to everyone.
    Nothing is spawned when such a request is refused."""
    async def quiet(*, environ=None):
        return {"available": False, "code": "tailscale_missing"}

    monkeypatch.setattr("sleeptalk.tailnet.detect", quiet)

    async def must_not_run(*args, **kwargs):
        raise AssertionError("a remote request started an adapter check")
    monkeypatch.setattr("sleeptalk.conformance.runner.run_adapter_test", must_not_run)
    fake, server, room, key = await _room(tmp_path)
    task = asyncio.create_task(room.run())
    try:
        await fake.expect("call.auth")
        await fake.send("peer.ready")
        own = {"agent": "own", "own": {"kind": "command", "command": "sh -c 'touch /tmp/pwned'", "keep": True}}
        for n, (path, body) in enumerate([
                ("/api/agent", own),
                ("/api/new", {"name": "x", "workspace": str(tmp_path), "agent": "own", "own": own["own"], "provider": "cartesia", "value": "k"}),
                ("/api/keys/move", {"confirm": True}),
                ("/api/set", {"setting": "transport.authorization_required", "value": False}),
                ("/api/set", {"setting": "transport.operator_ids", "value": "1"})], start=10):
            status, _, data = await _call(fake, n, "POST", path, body)
            assert status == 403 and b"computer running your agent" in data, (path, status, data)
        # Ordinary settings still work.
        status, _, data = await _call(fake, 30, "POST", "/api/set", {"setting": "policy.reply_length", "value": "summarized"})
        assert status == 200, data
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await server.close()


async def test_a_recorded_hello_cannot_reopen_the_channel(tmp_path, monkeypatch):
    async def quiet(*, environ=None):
        return {"available": False, "code": "tailscale_missing"}

    monkeypatch.setattr("sleeptalk.tailnet.detect", quiet)
    fake, server, room, key = await _room(tmp_path)
    task = asyncio.create_task(room.run())
    try:
        await fake.expect("call.auth")
        await fake.send("peer.ready")
        first = fake.page
        recorded = first.seal("settings.call", {"id": 1, "method": "POST", "path": "/api/set",
                                                "body": {"setting": "policy.reply_length", "value": "summarized"}})
        await fake.send_raw("setup.sealed", recorded)
        await fake.from_host.get()
        (tmp_path / "profiles" / "default.toml").write_text(
            (tmp_path / "profiles" / "default.toml").read_text().replace('reply_length = "summarized"', 'reply_length = "full"'))
        # A new page connection, then the relay replays the first hello and its request.
        await fake.send("peer.ready")
        await fake.send_raw("setup.hello", first.hello)
        await fake.send_raw("setup.sealed", recorded)
        await asyncio.sleep(0.3)
        assert fake.from_host.empty()
        assert 'reply_length = "full"' in (tmp_path / "profiles" / "default.toml").read_text()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await server.close()


async def test_a_remote_page_can_install_a_listed_agent_but_not_choose_the_command(tmp_path, monkeypatch):
    """The page picks which agent; the command is Sleeptalk's own."""
    async def quiet(*, environ=None):
        return {"available": False, "code": "tailscale_missing"}

    monkeypatch.setattr("sleeptalk.tailnet.detect", quiet)
    ran = []
    monkeypatch.setattr("sleeptalk.settings_page.install_agent_pieces",
                        lambda name: ran.append(name) or {"lines": [f"installed {name}"], "ready": True, "missing": []})
    fake, server, room, key = await _room(tmp_path)
    task = asyncio.create_task(room.run())
    try:
        await fake.expect("call.auth")
        await fake.send("peer.ready")
        status, _, data = await _call(fake, 10, "POST", "/api/agent/install", {"agent": "pi"})
        assert status == 200 and ran == ["pi"], data
        for n, body in enumerate(({"agent": "gremlin"}, {"agent": "pi", "command": "sh -c 'touch /tmp/pwned'"}), start=11):
            status, _, _ = await _call(fake, n, "POST", "/api/agent/install", body)
            assert status == 400
        assert ran == ["pi"]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await server.close()
