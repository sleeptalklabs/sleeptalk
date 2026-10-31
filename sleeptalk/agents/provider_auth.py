"""Explicit provider credentials and bounded metadata authentication.

The caller resolves a private reference. This module never reads a login store,
refreshes a token, follows a redirect, or starts a model turn.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import ipaddress
from collections.abc import Mapping
from urllib.parse import quote, urlsplit, urlunsplit

_METADATA = "SLEEPTALK_PROVIDER_AUTH"
_CREDENTIAL = "SLEEPTALK_PROVIDER_CREDENTIAL"
_PROTOCOLS = {"openai-completions", "openai-responses", "anthropic-messages", "cursor", "openai-codex-responses"}
OPENROUTER_ENDPOINT = "https://openrouter.ai/api/v1"


def is_loopback_host(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def is_private_endpoint(base_url: str) -> bool:
    """A loopback endpoint: the credential never leaves this machine."""

    host = urlsplit(base_url).hostname or ""
    return is_loopback_host(host.lower())


def canonical_endpoint(value: str) -> str:
    """HTTPS anywhere; plain http only on loopback (Ollama, LM Studio, a local proxy)."""

    if not isinstance(value, str) or len(value) > 2048 or any(ord(c) <= 32 or ord(c) > 126 for c in value) or "\\" in value:
        raise ValueError("Select a canonical HTTPS provider endpoint")
    url = urlsplit(value)
    if url.scheme not in {"https", "http"} or not url.hostname or url.username is not None or url.password is not None or url.query or url.fragment:
        raise ValueError("Select an HTTPS provider endpoint without credentials, query or fragment")
    if "%" in url.netloc or url.port == 0 or not url.hostname.isascii() or "%" in url.path or any(p in {".", ".."} for p in url.path.split("/")):
        raise ValueError("Select a canonical HTTPS provider endpoint")
    host = url.hostname.lower()
    if url.scheme == "http" and not is_loopback_host(host):
        raise ValueError("Plain http:// is only allowed on loopback (127.0.0.1, localhost); use https://")
    if ":" in host:
        host = f"[{host}]"
    port = url.port
    default_port = 443 if url.scheme == "https" else 80
    return urlunsplit((url.scheme, host + (f":{port}" if port and port != default_port else ""), url.path.rstrip("/"), "", ""))


SUBSCRIPTION_REFUSAL = ("Subscription logins (Claude, ChatGPT, Cursor) work only in the vendor's own app. "
                        "Use an API key from the provider's console instead.")


@dataclass(frozen=True)
class ProviderSelection:
    mode: str
    protocol: str
    base_url: str
    credential: str = field(repr=False)

    def __post_init__(self) -> None:
        if self.mode == "oauth":
            # A Claude, ChatGPT, or Cursor subscription login belongs to that
            # vendor's own app. Sleeptalk never carries one to another runtime
            # or calls the vendor's private endpoints with it.
            raise ValueError(SUBSCRIPTION_REFUSAL)
        if self.mode != "api_key" or self.protocol not in _PROTOCOLS:
            raise ValueError("Unsupported explicit provider authentication")
        endpoint = canonical_endpoint(self.base_url)
        object.__setattr__(self, "base_url", endpoint)
        if not isinstance(self.credential, str) or not self.credential or len(self.credential) > 32768 or any(ord(c) < 32 for c in self.credential):
            raise ValueError("The selected private provider credential is missing or malformed")
        if self.protocol == "openai-codex-responses":
            raise ValueError(SUBSCRIPTION_REFUSAL)
        if self.protocol == "cursor" and endpoint != "https://api2.cursor.sh":
            raise ValueError("Cursor authentication requires its native endpoint")

    @classmethod
    def from_environment(cls, env: Mapping[str, str]) -> ProviderSelection | None:
        raw = env.get(_METADATA)
        if not raw:
            return None
        data = json.loads(raw)
        if not isinstance(data, dict) or set(data) != {"mode", "protocol", "base_url"}:
            raise ValueError("Invalid explicit provider selection")
        return cls(data["mode"], data["protocol"], data["base_url"], env.get(_CREDENTIAL, ""))


async def verify(selection: ProviderSelection, model: str) -> str:
    """Prove remote acceptance of the selected credential without inference."""
    import aiohttp

    if not model or len(model) > 512:
        return "unknown"
    if selection.protocol == "cursor":
        if selection.mode == "api_key":
            from sleeptalk.agents.cursor_isolation import verify_api_key
            return await verify_api_key(selection.credential)
        return "unknown"
    openrouter = (selection.protocol == "openai-completions"
                  and selection.base_url == OPENROUTER_ENDPOINT)
    if openrouter:
        # OpenRouter has no per-model route and serves /models publicly; the
        # per-key listing rejects anonymous callers, so it is the right probe.
        url = selection.base_url + "/models/user"
        params = None
        headers = {"Authorization": f"Bearer {selection.credential}"}
    else:
        url = selection.base_url + ("/v1" if selection.protocol == "anthropic-messages" else "") + "/models/" + quote(model, safe="")
        params = None
        headers = ({"x-api-key": selection.credential, "anthropic-version": "2023-06-01"}
                   if selection.protocol == "anthropic-messages" else {"Authorization": f"Bearer {selection.credential}"})
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8), trust_env=False, cookie_jar=aiohttp.DummyCookieJar()) as client:
            # A key is only sent to an unknown public endpoint after that endpoint
            # proves it wants one. A loopback endpoint keeps the key on this machine.
            if selection.base_url not in {"https://api.openai.com/v1", "https://api.anthropic.com"} and not is_private_endpoint(selection.base_url):
                async with client.get(url, allow_redirects=False) as control:
                    if control.status not in {401, 403}:
                        return "unknown"
            async with client.get(url, params=params, headers=headers, allow_redirects=False) as response:
                if response.status in {401, 403}:
                    return "unavailable"
                if response.status != 200:
                    return "unknown"
                raw = bytearray()
                async for chunk in response.content.iter_chunked(4096):
                    raw.extend(chunk)
                    if len(raw) > (2097152 if openrouter else 65536):
                        return "unknown"
                data = json.loads(raw)
                if not isinstance(data, dict):
                    return "unknown"
                if openrouter:
                    models = data.get("data")
                    return "available" if isinstance(models, list) and any(isinstance(item, dict) and item.get("id") == model for item in models) else "unknown"
                discriminator = "type" if selection.protocol == "anthropic-messages" else "object"
                return "available" if data.get(discriminator) == "model" and data.get("id") == model else "unknown"
    except (aiohttp.ClientError, TimeoutError, ValueError, TypeError):
        return "unknown"
