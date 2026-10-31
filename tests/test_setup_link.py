"""Phone links: the sealed channel, and an agent's request answered on the phone's settings page."""
import asyncio
import base64
import json
import time

from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestServer
from cryptography.hazmat.primitives.asymmetric import ec
import pytest

from sleeptalk.call_provider import CallTicket, P2PCallProvider, _parse_mint_response
from sleeptalk.credential_store import CredentialStore
from sleeptalk import private_input, setup_link
from sleeptalk.private_input import InputSession, InputSpec, atomic_json

SENTINEL = "phone-sentinel-never-in-messages"
ROUTING = "r" * 24
REQUEST = "request-link-1"


def test_link_carries_the_public_key_in_the_fragment():
    key = ec.generate_private_key(ec.SECP256R1())
    raw = json.dumps({"kind": "setup", "ticket": "t" * 43, "routingId": ROUTING,
        "joinUrl": f"https://call.sleep.talk/#/setup/{ROUTING}/{'t' * 43}",
        "agentSignalUrl": f"wss://call.sleep.talk/api/tickets/{ROUTING}/signal", "agentAccessKey": "k" * 43,
        "expiresAt": "2026-09-16T12:10:00Z", "sessionCapSeconds": 900, "inactivitySeconds": 300})
    ticket = _parse_mint_response(raw, "https://call.sleep.talk", route="setup")
    url = setup_link.link_url(ticket, key)
    fragment = url.split("#", 1)[1]
    assert fragment.startswith(f"/setup/{ROUTING}/{'t' * 43}/")
    assert len(setup_link.unb64url(fragment.rsplit("/", 1)[1], limit=128)) == 65
    call_shaped = json.loads(raw) | {"kind": "call", "joinUrl": f"https://call.sleep.talk/#/call/{ROUTING}/{'t' * 43}"}
    with pytest.raises(Exception, match="doesn't support setup links"):
        _parse_mint_response(json.dumps(call_shaped), "https://call.sleep.talk", route="setup")


async def test_legacy_token_cannot_mint_setup_links():
    with pytest.raises(Exception, match="hosted call service"):
        await P2PCallProvider("https://call.example", mint_token="legacy").mint_setup()


@pytest.fixture
def session(tmp_path, monkeypatch):
    monkeypatch.setattr("sleeptalk.setup.ensure_dependencies", lambda *args, **kwargs: ())
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.setenv("SLEEPTALK_HOME", str(tmp_path / ".sleeptalk"))

    async def ready(*args):
        return {"status": "ready"}

    monkeypatch.setattr("sleeptalk.runtime_probe.probe_runtime", ready)
    spec = InputSpec(request_id=REQUEST, profile="voice", workspace=str(tmp_path), agent="codex", surface="web",
                     tts_provider="cartesia", profile_path=str(tmp_path / ".sleeptalk/profiles/voice.toml"))
    directory = tmp_path / "setup" / spec.request_id
    atomic_json(directory / "request.json", spec.model_dump())
    atomic_json(directory / "state.json", {"status": "waiting", "created_at": time.time(),
                                           "expires_at": time.time() + 900, "refs": {}})

    async def validate(provider, value, spec, voice):
        if value != SENTINEL:
            raise RuntimeError("401 rejected " + value)
        return {"stt_model": "nova-3"} if provider == "deepgram" else {"voice_id": "test-voice"}

    return InputSession(directory, store=CredentialStore(tmp_path / "store", backend="file"), validator=validate)


@pytest.fixture
def session(tmp_path, monkeypatch):
    monkeypatch.setattr("sleeptalk.setup.ensure_dependencies", lambda *args, **kwargs: ())
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.setenv("SLEEPTALK_HOME", str(tmp_path / ".sleeptalk"))

    async def ready(*args):
        return {"status": "ready"}

    monkeypatch.setattr("sleeptalk.runtime_probe.probe_runtime", ready)
    spec = InputSpec(request_id=REQUEST, profile="voice", workspace=str(tmp_path), agent="codex", surface="web",
                     tts_provider="cartesia", profile_path=str(tmp_path / ".sleeptalk/profiles/voice.toml"))
    directory = tmp_path / "setup" / spec.request_id
    atomic_json(directory / "request.json", spec.model_dump())
    atomic_json(directory / "state.json", {"status": "waiting", "created_at": time.time(),
                                           "expires_at": time.time() + 900, "refs": {}})

    async def validate(provider, value, spec, voice):
        if value != SENTINEL:
            raise RuntimeError("401 rejected " + value)
        return {"stt_model": "nova-3"} if provider == "deepgram" else {"voice_id": "test-voice"}

    return InputSession(directory, store=CredentialStore(tmp_path / "store", backend="file"), validator=validate)


class PageChannel:
    """The phone page's half of the sealed channel, as phone_settings.ts does it."""

    def __init__(self, host_public_key, routing_id=ROUTING):
        import os
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        key = os.urandom(32)
        self.aead, self.routing_id, self.sent, self.received = AESGCM(key), routing_id, 0, 0
        sealed = setup_link.SUITE.encrypt(json.dumps({"key": setup_link.b64url(key)}).encode(), host_public_key,
                                          info=setup_link.CHANNEL_INFO + json.dumps({"routing_id": routing_id}, separators=(",", ":")).encode())
        n = setup_link.KEM.P256.enc_length()
        self.hello = {"enc": setup_link.b64url(sealed[:n]), "ct": setup_link.b64url(sealed[n:])}

    def aad(self, direction, seq):
        return setup_link.CHANNEL_INFO + json.dumps({"direction": direction, "routing_id": self.routing_id, "seq": seq},
                                                    sort_keys=True, separators=(",", ":")).encode()

    def seal(self, kind, payload, *, seq=None):
        import os

        self.sent = seq if seq is not None else self.sent + 1
        iv = os.urandom(12)
        ct = self.aead.encrypt(iv, json.dumps({"type": kind, "payload": payload}).encode(), self.aad("page", self.sent))
        return {"seq": self.sent, "iv": setup_link.b64url(iv), "ct": setup_link.b64url(ct)}

    def open(self, payload):
        assert payload["seq"] > self.received
        self.received = payload["seq"]
        inner = json.loads(self.aead.decrypt(setup_link.unb64url(payload["iv"], limit=32),
                                             setup_link.unb64url(payload["ct"], limit=10 ** 6), self.aad("host", payload["seq"])))
        return inner["type"], inner["payload"]


class FakeRoom:
    """The Worker's view of a setup room, reduced to one host socket. It also
    plays the page's half of the sealed channel, so tests speak in the inner
    message types."""

    def __init__(self):
        self.from_host: asyncio.Queue = asyncio.Queue()
        self.socket = None
        self.raw: list[str] = []
        self.host_key = None
        self.page: PageChannel | None = None

    async def handler(self, request):
        socket = web.WebSocketResponse()
        await socket.prepare(request)
        self.socket = socket
        async for message in socket:
            if message.type == WSMsgType.TEXT:
                self.raw.append(message.data)
                await self.from_host.put(json.loads(message.data))
        return socket

    async def send_raw(self, kind, payload=None):
        await self.socket.send_str(json.dumps({"v": 1, "id": "x", "type": kind, "payload": payload or {}}))

    async def send(self, kind, payload=None):
        if kind == "peer.ready":
            await self.send_raw("peer.ready")
            self.page = PageChannel(self.host_key)
            await self.send_raw("setup.hello", self.page.hello)
        elif kind in setup_link.SEALED_TYPES:
            await self.send_raw("setup.sealed", self.page.seal(kind, payload or {}))
        else:
            await self.send_raw(kind, payload)

    async def expect(self, kind):
        envelope = await asyncio.wait_for(self.from_host.get(), 5)
        if kind in setup_link.SEALED_TYPES:
            assert envelope["type"] == "setup.sealed", envelope
            inner, payload = self.page.open(envelope["payload"])
            assert inner == kind, (inner, payload)
            return payload
        assert envelope["type"] == kind, envelope
        return envelope["payload"]


async def relay():
    """A fake relay room and a provider whose setup tickets point at it."""
    fake = FakeRoom()
    app = web.Application()
    app.router.add_get(f"/api/tickets/{ROUTING}/signal", fake.handler)
    server = TestServer(app)
    await server.start_server()
    ticket = CallTicket(token="t" * 43, routing_id=ROUTING, join_url=f"https://call.example/#/setup/{ROUTING}/{'t' * 43}",
                        agent_signal_url=str(server.make_url(f"/api/tickets/{ROUTING}/signal")).replace("http", "ws"),
                        agent_access_key="agent-key", expires_at="", session_cap_seconds=900,
                        inactivity_seconds=300, ice_servers=())

    class Provider:
        async def mint_setup(self, ttl_seconds):
            return ticket
    return fake, server, Provider()


def host_key_from_link(directory):
    """The public key the phone reads from the link's fragment."""
    from cryptography.hazmat.primitives.asymmetric.ec import EllipticCurvePublicKey, SECP256R1
    url = json.loads((directory / "invitation.json").read_text())["setup_url"]
    assert "/settings#/settings/" in url
    return EllipticCurvePublicKey.from_encoded_point(SECP256R1(), setup_link.unb64url(url.rsplit("/", 1)[1], limit=128))


async def call(fake, call_id, method, path, body=None):
    """One settings request from the phone page, through the channel; returns (status, json)."""
    await fake.send_raw("setup.sealed", fake.page.seal("settings.call", {"id": call_id, "method": method, "path": path, "body": body}))
    parts, head = {}, {}
    while True:
        envelope = await asyncio.wait_for(fake.from_host.get(), 10)
        inner, reply = fake.page.open(envelope["payload"])
        assert inner == "settings.reply" and reply["id"] == call_id
        head = reply if reply["part"] == 0 else head
        parts[reply["part"]] = reply["data"]
        if len(parts) == reply["parts"]:
            break
    return head["status"], json.loads(base64.b64decode("".join(parts[i] for i in range(len(parts)))))


async def serving(session, monkeypatch):
    """setup_link.serve for this request, with the fake relay; returns (fake, server, task, host key)."""
    fake, server, provider = await relay()
    monkeypatch.setattr(setup_link, "InputSession", lambda directory: session)

    async def absent(*, environ=None):
        return {"available": False, "code": "tailscale_missing"}
    monkeypatch.setattr("sleeptalk.tailnet.detect", absent)
    task = asyncio.create_task(setup_link.serve(session.directory, provider=provider))
    await fake.expect("call.auth")
    fake.host_key = host_key_from_link(session.directory)
    return fake, server, task


async def test_the_phone_answers_the_agents_request_on_the_settings_page(session, monkeypatch):
    fake, server, task = await serving(session, monkeypatch)
    try:
        await fake.send("peer.ready")  # the page opens the channel
        status, state = await call(fake, 1, "GET", "/api/state")
        request = state["request"]
        assert status == 200 and state["profile"] is None
        assert request["step"] == "speech" and request["mode"] == "setup" and request["profile"] == "voice"
        assert {sv["id"] for sv in request["options"]["services"]} == {"deepgram", "cartesia"}
        status, view = await call(fake, 2, "POST", "/api/request/key", {"provider": "deepgram", "value": "wrong"})
        assert status == 422 and view["code"] == "invalid_credential"
        status, view = await call(fake, 3, "POST", "/api/request/key", {"provider": "deepgram", "value": SENTINEL})
        assert status == 200 and view["step"] == "speech"
        status, view = await call(fake, 4, "POST", "/api/request/key", {"provider": "cartesia", "value": SENTINEL, "voice_id": "test-voice"})
        assert status == 200 and view["step"] == "done", view
        assert private_input.status(REQUEST, root=session.directory.parent)["profile_ready"]
        # The page now edits the profile it wrote.
        status, state = await call(fake, 5, "GET", "/api/state")
        assert state["profile"] == "voice"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await server.close()
    # The relay saw the channel and nothing else: no key, service, voice, or profile.
    assert {json.loads(raw)["type"] for raw in fake.raw} <= {"call.auth", "setup.sealed", "call.cancel"}
    for word in (SENTINEL, "cartesia", "test-voice", "voice.toml", REQUEST):
        assert word not in "".join(fake.raw), word


async def test_the_room_closes_when_the_agent_cancels(session, monkeypatch):
    fake, server, task = await serving(session, monkeypatch)
    try:
        private_input.cancel(REQUEST, root=session.directory.parent)
        assert (await fake.expect("call.cancel")) == {"reason": "setup_ended"}
        await asyncio.wait_for(task, 5)
        assert not (session.directory / "invitation.json").exists()
    finally:
        task.cancel()
        await server.close()


def test_the_channel_refuses_replays_other_rooms_and_forgeries():
    key = ec.generate_private_key(ec.SECP256R1())
    page = PageChannel(key.public_key())
    host = setup_link.SetupChannel.accept(key, page.hello, routing_id=ROUTING)
    sealed = page.seal("settings.call", {"id": 1})
    assert host.open(sealed) == ("settings.call", {"id": 1})
    for bad in (sealed, {**page.seal("settings.call", {"id": 2}), "iv": sealed["iv"]}, page.seal("setup.request", {}, seq=9)):
        with pytest.raises(setup_link.LinkError):
            host.open(bad)
    with pytest.raises(setup_link.LinkError):
        setup_link.SetupChannel.accept(key, page.hello, routing_id="another-room-000000")
    other = ec.generate_private_key(ec.SECP256R1())
    with pytest.raises(setup_link.LinkError):
        setup_link.SetupChannel.accept(other, page.hello, routing_id=ROUTING)
    # The host's own messages can't be reflected back to it as the page's.
    with pytest.raises(setup_link.LinkError):
        host.open(host.seal("settings.reply", {"id": 1}))


async def test_link_entry_is_for_web_calls_and_discord_bots_only(tmp_path):
    spec = InputSpec(request_id=REQUEST, profile="voice", workspace=str(tmp_path), agent="codex", surface="terminal",
                     capture_mode="headphones", user_terminal_local=True, tts_provider="cartesia")
    with pytest.raises(private_input.InputError, match="Terminal audio"):
        await private_input.start(spec, user_browser_local=False, root=tmp_path, entry="link")
    web_spec = spec.model_copy(update={"surface": "web", "capture_mode": "", "user_terminal_local": False})
    with pytest.raises(private_input.InputError, match="host input link"):
        await private_input.start(web_spec, user_browser_local=False, root=tmp_path)
