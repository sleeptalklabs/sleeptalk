from __future__ import annotations

import asyncio
import json
import re
from importlib import resources
from urllib.parse import quote, urlsplit

import aiohttp
import pytest
from yarl import URL

from sleeptalk.call_provider import CallContext
from sleeptalk.local_rendezvous import LocalCallProvider


@pytest.mark.asyncio
async def test_local_provider_rejects_unrelated_paths(tmp_path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("not a browser asset")
    paths = [
        "/" + quote(str(outside), safe=""),
        "/assets/" + quote(str(outside), safe=""),
        "/assets/%2E%2E/index.html",
        "/missing.js",
        "/assets/missing.js",
    ]
    async with LocalCallProvider() as provider:
        async with aiohttp.ClientSession() as session:
            for path in paths:
                async with session.get(URL(provider.origin + path, encoded=True)) as response:
                    assert response.status == 404, path


@pytest.mark.asyncio
async def test_local_provider_serves_every_browser_page_asset() -> None:
    root = resources.files("sleeptalk").joinpath("_web")
    async with LocalCallProvider() as provider:
        async with aiohttp.ClientSession() as session:
            async with session.get(provider.origin + "/index.html") as response:
                assert response.status == 200
                html = await response.text()
            paths = re.findall(r'(?:src|href)="(/[^\"]+)"', html)
            assert any(path.endswith(".js") for path in paths)
            assert any(path.endswith(".css") for path in paths)
            assert "/sleep.mp4" in paths
            for path in paths:
                async with session.get(provider.origin + path) as response:
                    assert response.status == 200, path
                    assert await response.read() == root.joinpath(path[1:]).read_bytes()


@pytest.mark.asyncio
async def test_local_provider_serves_packaged_browser_and_redeems_once() -> None:
    async with LocalCallProvider() as provider:
        ticket = await provider.mint(
            CallContext("maxwell", "conversation", "terminal-user"),
            ttl_seconds=60,
        )
        parsed = urlsplit(ticket.join_url)
        assert parsed.scheme == "http"
        assert parsed.hostname == "127.0.0.1"
        assert parsed.port is not None
        assert ticket.ice_servers == ()

        async with aiohttp.ClientSession() as session:
            async with session.get(provider.origin) as response:
                assert response.status == 200
                assert "SLEEP.TALK" in await response.text()
                assert response.headers["Referrer-Policy"] == "no-referrer"
                assert "frame-ancestors 'none'" in response.headers[
                    "Content-Security-Policy"
                ]

            endpoint = f"{provider.origin}/api/tickets/{ticket.routing_id}/redeem"
            headers = {"Origin": provider.origin}
            async with session.post(
                endpoint,
                json={"ticket": ticket.token},
                headers={"Origin": "http://attacker.invalid"},
            ) as response:
                assert response.status == 403

            async def redeem() -> tuple[int, dict[str, object]]:
                async with session.post(
                    endpoint, json={"ticket": ticket.token}, headers=headers
                ) as response:
                    return response.status, await response.json()

            attempts = await asyncio.gather(redeem(), redeem())
            assert sorted(status for status, _body in attempts) == [200, 409]
            redemption = next(body for status, body in attempts if status == 200)
            assert str(redemption["browserSignalUrl"]).startswith("ws://127.0.0.1:")
            assert redemption["iceServers"] == []


@pytest.mark.asyncio
async def test_local_provider_authenticates_and_relays_signaling() -> None:
    async with LocalCallProvider() as provider:
        ticket = await provider.mint(
            CallContext("maxwell", "conversation", "terminal-user"),
            ttl_seconds=60,
        )
        endpoint = f"{provider.origin}/api/tickets/{ticket.routing_id}/redeem"
        async with aiohttp.ClientSession() as session:
            async with session.post(
                endpoint,
                json={"ticket": ticket.token},
                headers={"Origin": provider.origin},
            ) as response:
                redemption = await response.json()

            async with (
                session.ws_connect(ticket.agent_signal_url) as agent,
                session.ws_connect(
                    redemption["browserSignalUrl"],
                    origin=provider.origin,
                ) as browser,
            ):
                await agent.send_json(
                    _signal(
                        "call.auth",
                        {"role": "agent", "key": ticket.agent_access_key},
                    )
                )
                await browser.send_json(
                    _signal(
                        "call.auth",
                        {"role": "browser", "key": redemption["browserAccessKey"]},
                    )
                )
                assert json.loads((await agent.receive()).data)["type"] == "ticket.redeemed"
                assert json.loads((await agent.receive()).data)["type"] == "peer.ready"
                assert json.loads((await browser.receive()).data)["type"] == "peer.ready"

                offer = _signal("webrtc.offer", {"sdp": "offer", "type": "offer"})
                await browser.send_json(offer)
                assert json.loads((await agent.receive()).data) == offer


def _signal(kind: str, payload: dict[str, str]) -> dict[str, object]:
    return {"v": 1, "id": f"id-{kind}", "type": kind, "payload": payload}


@pytest.mark.asyncio
async def test_local_redemption_notifies_host_before_browser_connects() -> None:
    async with LocalCallProvider() as provider:
        ticket = await provider.mint(CallContext("agent", "conversation", "user"))
        assert "ticket.redeemed" in ticket.signal_types
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(ticket.agent_signal_url) as agent:
                await agent.send_json(_signal("call.auth", {"role": "agent", "key": ticket.agent_access_key}))
                await agent.send_json(_signal("ping", {}))
                assert json.loads((await agent.receive()).data)["type"] == "pong"
                async with session.post(
                    f"{provider.origin}/api/tickets/{ticket.routing_id}/redeem",
                    json={"ticket": ticket.token}, headers={"Origin": provider.origin},
                ) as response:
                    assert response.status == 200
                notification = json.loads((await asyncio.wait_for(agent.receive(), timeout=2)).data)
                assert notification["type"] == "ticket.redeemed"
                assert notification["payload"] == {}


@pytest.mark.asyncio
async def test_public_origin_publishes_links_for_a_trusted_proxy() -> None:
    """Behind `tailscale serve`, the browser sees the tailnet origin while the
    host's own connection stays on loopback."""
    from sleeptalk.local_rendezvous import LocalCallProvider

    public = "https://host.example.ts.net:8443"
    async with LocalCallProvider(public_origin=public) as provider:
        ticket = await provider.mint(CallContext("maxwell", "conversation", "terminal-user"), ttl_seconds=60)
        assert ticket.join_url.startswith(public + "/#/call/")
        assert ticket.agent_signal_url.startswith("ws://127.0.0.1:")
        assert ticket.ice_servers == ()
        loopback = f"http://127.0.0.1:{provider.port}"
        endpoint = f"{loopback}/api/tickets/{ticket.routing_id}/redeem"
        async with aiohttp.ClientSession() as session:
            # The page is served under the public host.
            async with session.get(loopback, headers={"Host": "host.example.ts.net:8443"}) as response:
                assert response.status == 200
            # A browser that reached loopback directly is not the published origin.
            async with session.post(endpoint, json={"ticket": ticket.token}, headers={"Origin": loopback}) as response:
                assert response.status == 403
            # An unknown Host is refused even with the right Origin.
            async with session.post(endpoint, json={"ticket": ticket.token},
                                    headers={"Host": "other.example:8443", "Origin": public}) as response:
                assert response.status == 403
            # The proxy either preserves Host or rewrites it and forwards the original.
            async with session.post(endpoint, json={"ticket": ticket.token},
                                    headers={"Host": f"127.0.0.1:{provider.port}", "X-Forwarded-Host": "host.example.ts.net:8443", "Origin": public}) as response:
                assert response.status == 200
                body = await response.json()
                assert body["browserSignalUrl"] == f"wss://host.example.ts.net:8443/api/tickets/{ticket.routing_id}/signal"
                assert body["iceServers"] == []


def test_public_origin_must_be_a_bare_origin() -> None:
    from sleeptalk.call_provider import CallProviderError
    from sleeptalk.local_rendezvous import LocalCallProvider

    assert LocalCallProvider(public_origin=" https://a.example:8443/ ").public_origin == "https://a.example:8443"
    for bad in ("a.example", "https://a.example/call", "https://a.example?x=1", "ftp://a.example"):
        with pytest.raises(CallProviderError):
            LocalCallProvider(public_origin=bad)


@pytest.mark.asyncio
async def test_local_calls_have_no_session_cap_only_an_inactivity_deadline() -> None:
    """Nothing metered runs a local or tailnet call, so only silence ends it."""
    import math
    from sleeptalk.local_rendezvous import LocalCallProvider, _INACTIVITY_SECONDS, _SESSION_CAP_SECONDS

    assert _SESSION_CAP_SECONDS == 0 and _INACTIVITY_SECONDS == 90
    async with LocalCallProvider() as provider:
        ticket = await provider.mint(CallContext("maxwell", "conversation", "terminal-user"), ttl_seconds=60)
        assert ticket.session_cap_seconds == 0
        async with aiohttp.ClientSession() as session:
            async with session.post(f"{provider.origin}/api/tickets/{ticket.routing_id}/redeem",
                                    json={"ticket": ticket.token}, headers={"Origin": provider.origin}) as response:
                assert response.status == 200
                body = await response.json()
        assert body["sessionCapSeconds"] == 0 and body["sessionExpiresAt"] == ""
        assert body["inactivitySeconds"] == 90
        assert provider._ticket is not None and provider._ticket.session_expires_at == math.inf


@pytest.mark.asyncio
async def test_the_local_page_may_connect_only_to_its_own_server() -> None:
    async with LocalCallProvider() as provider:
        async with aiohttp.ClientSession() as session:
            async with session.get(provider.origin + "/index.html") as response:
                csp = response.headers["Content-Security-Policy"]
    assert f"connect-src 'self' ws://{provider.origin.removeprefix('http://')};" in csp
    assert " ws: " not in csp and "wss:;" not in csp
    published = LocalCallProvider(public_origin="https://box.tail1234.ts.net:8443")
    published._host_header = "127.0.0.1:1"
    published.origin = published.public_origin
    assert published._browser_signal_url().startswith("wss://box.tail1234.ts.net:8443/")
