from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import sleeptalk.call_provider as call_provider
from sleeptalk.call_provider import CallContext, CallProviderError, P2PCallProvider
from sleeptalk.service_grant import ServiceGrantStore


class FakeResponse:
    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self._body = body

    async def __aenter__(self) -> FakeResponse:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def text(self) -> str:
        return self._body


class FakeSession:
    def __init__(self, responses: list[FakeResponse], calls: list[dict[str, Any]], **_kwargs: Any) -> None:
        self.responses = responses
        self.calls = calls

    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"url": url, **kwargs})
        return self.responses.pop(0)


def ticket_body(origin: str) -> str:
    token = "t" * 43
    routing_id = "r" * 24
    return json.dumps(
        {
            "ticket": token,
            "routingId": routing_id,
            "joinUrl": f"{origin}/#/call/{routing_id}/{token}",
            "agentSignalUrl": f"wss://call.example.test/api/tickets/{routing_id}/signal",
            "agentAccessKey": "agent-secret",
            "iceServers": [{"urls": ["stun:stun.cloudflare.com:3478"]}],
            "expiresAt": "2026-09-15T21:00:00.000Z",
            "sessionCapSeconds": 1800,
            "inactivitySeconds": 90,
            "signalTypes": [],
        }
    )


@pytest.mark.asyncio
async def test_first_mint_enrolls_once_and_sends_no_caller_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []
    responses = [
        FakeResponse(201, '{"enrolled":true}'),
        FakeResponse(201, ticket_body("https://call.example.test")),
        FakeResponse(201, ticket_body("https://call.example.test")),
    ]

    def session_factory(**kwargs: Any) -> FakeSession:
        return FakeSession(responses, calls, **kwargs)

    monkeypatch.setattr(call_provider.aiohttp, "ClientSession", session_factory)
    store = ServiceGrantStore(tmp_path / "credentials")
    provider = P2PCallProvider("https://call.example.test", grant_store=store)
    context = CallContext("agent", "private-chat", "private-user")

    await provider.mint(context)
    await provider.mint(context)

    assert [call["url"] for call in calls] == [
        "https://call.example.test/api/installations/enroll",
        "https://call.example.test/api/tickets",
        "https://call.example.test/api/tickets",
    ]
    enrollment, first_mint, second_mint = calls
    assert set(enrollment["json"]) == {"installationId", "secretHash"}
    assert len(enrollment["json"]["secretHash"]) == 64
    assert first_mint["json"] == {"ttlSeconds": 300}
    assert second_mint["json"] == {"ttlSeconds": 300}
    assert "private-chat" not in json.dumps(calls)
    assert "private-user" not in json.dumps(calls)
    assert first_mint["headers"]["Authorization"].startswith("SleeptalkGrant v1.")
    assert first_mint["allow_redirects"] is False
    assert store.load("https://call.example.test").enrolled is True  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_service_error_uses_fixed_local_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []
    responses = [
        FakeResponse(201, '{"enrolled":true}'),
        FakeResponse(
            403,
            '{"error":{"code":"grant_revoked","message":"secret attacker text"}}',
        ),
    ]

    monkeypatch.setattr(
        call_provider.aiohttp,
        "ClientSession",
        lambda **kwargs: FakeSession(responses, calls, **kwargs),
    )
    provider = P2PCallProvider(
        "https://call.example.test",
        grant_store=ServiceGrantStore(tmp_path / "credentials"),
    )

    with pytest.raises(CallProviderError, match="credential was revoked") as failure:
        await provider.mint(CallContext("agent", "chat", "user"))
    assert "attacker" not in str(failure.value)
