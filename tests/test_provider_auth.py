import json
from unittest.mock import patch

import pytest
from sleeptalk.agents.provider_auth import ProviderSelection, canonical_endpoint, verify


def selected(**changes):
    data = dict(mode="api_key", protocol="openai-responses", base_url="https://models.example/v1")
    data.update(changes)
    return ProviderSelection.from_environment({"SLEEPTALK_PROVIDER_AUTH": json.dumps(data), "SLEEPTALK_PROVIDER_CREDENTIAL": "private-fixture"})


@pytest.mark.parametrize("url", ["http://host/v1", "https://user@host", "https://host/?key=a", "https://host/#key", "https://host/../api", "https://host/%2e%2e", "https://host/\\other", "https://host/\n"])
def test_endpoint_rejects_ambiguous_or_untrusted_transport(url):
    with pytest.raises(ValueError): canonical_endpoint(url)


def test_endpoint_canonicalizes_scope():
    assert canonical_endpoint("https://EXAMPLE.com:443/prefix/") == "https://example.com/prefix"


@pytest.mark.parametrize("protocol,url", [("openai-responses", "https://api.openai.com/v1"), ("anthropic-messages", "https://custom.example"), ("cursor", "https://custom.example")])
def test_oauth_never_forwarded_to_custom_or_unsupported_endpoint(protocol, url):
    with pytest.raises(ValueError): selected(mode="oauth", protocol=protocol, base_url=url)


class Response:
    def __init__(self, body, status):
        self.body, self.status, self.content = body, status, self
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    async def iter_chunked(self, size):
        yield self.body


class Client:
    def __init__(self, body, status=200): self.body, self.status, self.calls = body, status, []
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return Response(b"{}", 401) if "headers" not in kwargs else Response(self.body, self.status)


@pytest.mark.asyncio
@pytest.mark.parametrize("body,status,result", [(b'{"object":"model","id":"chosen"}',200,"available"), (b'{"object":"model","id":"other"}',200,"unknown"), (b'{}',302,"unknown"), (b'{}',401,"unavailable"), (b'{}',403,"unavailable"), (b'x'*65537,200,"unknown")])
async def test_selected_model_is_checked_without_redirect_or_model_turn(body, status, result):
    client = Client(body, status)
    with patch("aiohttp.ClientSession", return_value=client) as factory:
        assert await verify(selected(), "chosen") == result
    assert factory.call_args.kwargs["trust_env"] is False
    assert client.calls[1:] == [("https://models.example/v1/models/chosen", {"params":None, "headers":{"Authorization":"Bearer private-fixture"},"allow_redirects":False})]


@pytest.mark.parametrize("protocol,url", [
    ("anthropic-messages", "https://api.anthropic.com"),
    ("openai-codex-responses", "https://chatgpt.com/backend-api"),
    ("cursor", "https://api2.cursor.sh"),
])
def test_subscription_logins_are_refused_even_at_the_vendor(protocol, url):
    """A Claude, ChatGPT, or Cursor subscription login stays in the vendor's
    own app: Sleeptalk neither hands it to another runtime nor probes the
    vendor's private endpoints with it."""
    with pytest.raises(ValueError, match="vendor's own app"):
        selected(mode="oauth", protocol=protocol, base_url=url)


def test_codex_protocol_needs_a_subscription_so_it_is_refused():
    with pytest.raises(ValueError, match="vendor's own app"):
        ProviderSelection("api_key", "openai-codex-responses", "https://chatgpt.com/backend-api", "sk-fixture")


@pytest.mark.asyncio
async def test_probes_send_no_borrowed_client_identity():
    client = Client(b'{"type":"model","id":"claude-sonnet-4-6"}')
    with patch("aiohttp.ClientSession", return_value=client):
        assert await verify(selected(protocol="anthropic-messages", base_url="https://api.anthropic.com"), "claude-sonnet-4-6") == "available"
    headers = client.calls[-1][1]["headers"]
    assert "User-Agent" not in headers and "originator" not in headers
    assert client.calls[-1][0] == "https://api.anthropic.com/v1/models/claude-sonnet-4-6"


@pytest.mark.parametrize("url", ["https://@host", "https://host:0", "https://%65xample.com", "https://host/é"])
def test_endpoint_rejects_scope_aliases(url):
    with pytest.raises(ValueError): canonical_endpoint(url)


def test_selection_repr_omits_credential():
    assert "private-fixture" not in repr(selected())


@pytest.mark.asyncio
async def test_public_catalog_does_not_prove_authentication():
    client = Client(b'{"object":"model","id":"chosen"}')
    client.get = lambda *args, **kwargs: Response(client.body, 200)
    with patch("aiohttp.ClientSession", return_value=client):
        assert await verify(selected(), "chosen") == "unknown"


@pytest.mark.parametrize("protocol", ["cursor", "anthropic-messages", "openai-codex-responses"])
def test_direct_selection_also_enforces_native_oauth_origin(protocol):
    with pytest.raises(ValueError):
        ProviderSelection("oauth", protocol, "https://custom.example", "private-fixture")
