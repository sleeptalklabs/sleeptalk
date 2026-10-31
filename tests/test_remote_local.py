import hashlib

import pytest
from aiohttp.test_utils import TestClient, TestServer

pytest.importorskip('fido2')
pytest.importorskip('rfc8785')
pytest.importorskip('cryptography.hazmat.primitives.hpke')


from sleeptalk.remote_local import PairingSession


class FakeStore:
    def __init__(self):
        self.confirmed = []
        self.phase = "open"

    def enrollment_status(self, request_id):
        payload = {"operation": "pairing_status", "request_id": request_id, "status": self.phase}
        if self.phase == "awaiting_confirmation":
            payload["fingerprint"] = "0123456789abcdef0123456789abcdef"
        return {"payload": payload, "signature": "signed"}

    def confirm_enrollment(self, request_id, fingerprint, *, current_binding):
        self.confirmed.append((request_id, fingerprint, current_binding))
        self.phase = "confirmed"
        return {"operator_id": "operator", "workspace_id": "workspace", "host_id": "host"}


@pytest.fixture
def invitation():
    return {
        "kind": "pairing",
        "descriptor": {
            "enrollment_id": "ZW5yb2xsbWVudC1pZA",
            "setup_origin": "https://setup.example.com",
            "expires_at": 4_000_000_000,
        },
        "signature": "signature",
        "capability": "private-capability",
    }


@pytest.mark.asyncio
async def test_loopback_invitation_exchange_and_fresh_binding_confirmation(invitation):
    store = FakeStore()
    bindings = []

    def current_binding():
        bindings.append(True)
        return "a" * 64

    session = PairingSession(store, invitation, current_binding=current_binding)
    server = TestServer(session.app(), host="127.0.0.1")
    client = TestClient(server)
    await client.start_server()
    try:
        session.origin = str(client.make_url("/")).rstrip("/")
        page = await client.get("/", headers={"Host": "wrong.example"})
        assert page.status == 403

        page = await client.get("/")
        assert page.status == 200
        assert b"private-capability" not in await page.read()

        denied = await client.post("/session", json={"invitation": "wrong"},
                                   headers={"Origin": session.origin})
        assert denied.status == 401

        exchanged = await client.post("/session", json={"invitation": session.local_invitation},
                                      headers={"Origin": session.origin})
        assert exchanged.status == 200
        csrf = (await exchanged.json())["csrf"]
        headers = {"Origin": session.origin, "X-Setup-CSRF": csrf}

        status = await client.get("/status", headers={"X-Setup-CSRF": csrf})
        body = await status.json()
        assert body["status"] == "open"
        assert body["remote_url"].startswith("https://setup.example.com/entry#")
        assert "private-capability" not in body["remote_url"]

        store.phase = "awaiting_confirmation"
        status = await client.get("/status", headers={"X-Setup-CSRF": csrf})
        fingerprint = (await status.json())["fingerprint"]
        rejected = await client.post("/confirm", json={"fingerprint": "f" * 32, "confirmed": True}, headers=headers)
        assert rejected.status == 409

        confirmed = await client.post("/confirm", json={"fingerprint": fingerprint, "confirmed": True}, headers=headers)
        assert confirmed.status == 200
        assert (await confirmed.json())["operator_id"] == "operator"
        assert bindings == [True]
        assert store.confirmed == [(invitation["descriptor"]["enrollment_id"], fingerprint, "a" * 64)]
        assert session.finished.is_set()
        assert session.remote_url == ""
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_loopback_mutations_require_exact_origin_cookie_and_csrf(invitation):
    session = PairingSession(FakeStore(), invitation, current_binding=lambda: "a" * 64)
    server = TestServer(session.app(), host="127.0.0.1")
    client = TestClient(server)
    await client.start_server()
    try:
        session.origin = str(client.make_url("/")).rstrip("/")
        exchanged = await client.post("/session", json={"invitation": session.local_invitation},
                                      headers={"Origin": session.origin})
        csrf = (await exchanged.json())["csrf"]
        bad_origin = await client.post("/confirm", json={}, headers={"Origin": "http://attacker.invalid", "X-Setup-CSRF": csrf})
        assert bad_origin.status == 403
        bad_csrf = await client.post("/confirm", json={}, headers={"Origin": session.origin, "X-Setup-CSRF": "wrong"})
        assert bad_csrf.status == 403
    finally:
        await client.close()
