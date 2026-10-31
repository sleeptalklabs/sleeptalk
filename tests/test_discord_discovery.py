from unittest.mock import AsyncMock

import pytest

from sleeptalk import discord_check as checks


@pytest.mark.asyncio
async def test_application_uses_authenticated_identity_not_token_text(monkeypatch):
    get = AsyncMock(return_value={"id": "123", "name": "Sleeptalk"})
    monkeypatch.setattr(checks, "_rest_get", get)
    result = await checks.fetch_application("sentinel-token")
    assert result.id == "123"
    assert "client_id=123" in result.invite_url
    assert "sentinel-token" not in repr(result)
    get.assert_awaited_once_with("sentinel-token", "/applications/@me")


@pytest.mark.asyncio
async def test_voice_discovery_respects_role_and_member_overwrites_without_gateway(monkeypatch):
    monkeypatch.setattr(checks, "fetch_bot", AsyncMock(return_value=checks.BotIdentity("bot", "9")))
    bits = checks.INVITE_PERMISSIONS
    channels = [
        {"id":"20", "name":"allowed", "type":2},
        {"id":"21", "name":"denied", "type":2, "permission_overwrites":[{"id":"10", "type":0, "allow":"0", "deny":str(1 << 20)}]},
        {"id":"22", "name":"member allowed", "type":2, "permission_overwrites":[{"id":"11", "type":0, "allow":"0", "deny":str(1 << 21)}, {"id":"9", "type":1, "allow":str(1 << 21), "deny":"0"}]},
        {"id":"23", "name":"text", "type":0},
    ]
    get = AsyncMock(side_effect=[{"roles":["11"]}, [{"id":"10", "permissions":str(bits)}, {"id":"11", "permissions":"0"}], channels])
    monkeypatch.setattr(checks, "_rest_get", get)
    gateway = AsyncMock()
    monkeypatch.setattr(checks, "probe_gateway_intents", gateway)
    result = await checks.list_voice_channels("sentinel-token", "10")
    assert [(item.id, item.eligible) for item in result] == [("20",True), ("21",False), ("22",True)]
    assert result[1].missing_permissions == ("connect",)
    assert "sentinel-token" not in repr(result)
    assert get.await_count == 3
    gateway.assert_not_awaited()


@pytest.mark.asyncio
async def test_voice_discovery_requires_explicit_guild(monkeypatch):
    get = AsyncMock()
    monkeypatch.setattr(checks, "_rest_get", get)
    with pytest.raises(checks.DiscordCheckError, match="Select a Discord server"):
        await checks.list_voice_channels("sentinel", "")
    get.assert_not_awaited()


def test_network_diagnostics_do_not_include_exception_secrets():
    import aiohttp
    error = checks._translate_connect_error(aiohttp.ClientError("sentinel-secret"))
    assert "sentinel-secret" not in str(error)
