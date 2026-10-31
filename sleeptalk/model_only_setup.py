"""Setup-time choices for ``type = "model"``: vendor presets, the key, and its receipt.

The wizard, the answers file, and headless setup all go through here so a
model key is stored under one scope and never written to a profile or .env.
"""

from __future__ import annotations

from typing import Any

from sleeptalk.agent_auth import AgentAuthChoice, auth_scope
from sleeptalk.agents.model_only import PRESETS
from sleeptalk.agents.provider_auth import is_private_endpoint

OTHER_VENDOR = "other"
OLLAMA_VENDOR = "ollama"
DEFAULT_VENDOR = "openrouter"
#: Ollama and most local servers ignore the bearer token but the wire format
#: still carries one; this is the conventional stand-in.
PLACEHOLDER_KEY = "ollama"

#: Menu order. OpenRouter first: one key reaches every model.
VENDORS: tuple[tuple[str, str], ...] = (
    ("openrouter", "OpenRouter — one key, every model"),
    ("openai", "OpenAI"),
    ("anthropic", "Anthropic"),
    (OLLAMA_VENDOR, "Ollama — models on this machine, no key"),
    (OTHER_VENDOR, "Other OpenAI-compatible endpoint — paste its base URL"),
)
VENDOR_LABELS = dict(VENDORS)


def vendor_name(vendor: str) -> str:
    """The short vendor name for messages: "OpenRouter", not the menu line."""

    return VENDOR_LABELS.get(normalize_vendor(vendor), "The endpoint").split(" — ")[0]


class ModelSetupError(ValueError):
    """A fix-stating setup problem; never carries a credential value."""


def normalize_vendor(vendor: str) -> str:
    value = (vendor or "").strip().lower()
    if not value:
        return DEFAULT_VENDOR
    if value not in VENDOR_LABELS:
        raise ModelSetupError(f"model_vendor must be one of {', '.join(name for name, _ in VENDORS)}")
    return value


def vendor_for_base_url(base_url: str) -> str:
    for name, preset in PRESETS.items():
        if preset["base_url"] == base_url:
            return name
    return OTHER_VENDOR


def default_model(vendor: str) -> str:
    preset = PRESETS.get(normalize_vendor(vendor))
    return preset["model"] if preset else ""


def console_url(vendor: str) -> str:
    preset = PRESETS.get(normalize_vendor(vendor))
    return preset["console"] if preset else ""


def auth_choice(vendor: str, base_url: str = "") -> AgentAuthChoice:
    """The non-secret (mode, protocol, base_url) triple for a vendor or a pasted URL."""

    name = normalize_vendor(vendor)
    if name == OTHER_VENDOR:
        if not base_url.strip():
            raise ModelSetupError("model_base_url is required for another OpenAI-compatible endpoint")
        try:
            return AgentAuthChoice(mode="api_key", protocol="openai-completions", base_url=base_url.strip())
        except ValueError:
            raise ModelSetupError("model_base_url must be a canonical HTTPS endpoint (plain http only on 127.0.0.1 or localhost) without credentials, query, or fragment") from None
    preset = PRESETS[name]
    return AgentAuthChoice(mode="api_key", protocol=preset["protocol"], base_url=preset["base_url"])


def key_optional(choice: AgentAuthChoice) -> bool:
    """Loopback servers (Ollama, LM Studio) usually want no key at all."""

    return is_private_endpoint(choice.base_url)


def key_required(vendor: str, base_url: str = "") -> bool:
    try:
        return not key_optional(auth_choice(vendor, base_url))
    except ModelSetupError:
        return True


async def verify_key(choice: AgentAuthChoice, model: str, key: str) -> str:
    """'available' | 'unavailable' | 'unknown', from the shared credential probe."""

    from sleeptalk.agents.provider_auth import ProviderSelection, verify

    selection = ProviderSelection(choice.mode, choice.protocol, choice.base_url, key or PLACEHOLDER_KEY)
    return await verify(selection, model)


def ollama_origin(base_url: str) -> str:
    return base_url[:-3] if base_url.endswith("/v1") else base_url


async def ollama_models(base_url: str) -> list[str]:
    """Names of the models pulled into a local Ollama; [] when it is not answering."""

    import aiohttp

    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=3), trust_env=False) as client:
            async with client.get(ollama_origin(base_url) + "/api/tags", allow_redirects=False) as response:
                if response.status != 200:
                    return []
                data = await response.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError, OSError, ValueError):
        return []
    models = data.get("models") if isinstance(data, dict) else None
    if not isinstance(models, list):
        return []
    names = [str(item.get("name")) for item in models if isinstance(item, dict) and item.get("name")]
    return sorted(set(names))


def unreachable_ollama_message(base_url: str) -> str:
    return (f"Ollama is not answering at {ollama_origin(base_url)}. Start it with `ollama serve`, "
            "pull a model (for example `ollama pull llama3.2`), then type its name.")


def missing_ollama_model_message(model: str) -> str:
    return f"Ollama has no model named {model}. Pull it with `ollama pull {model}` before the first call."


def rejected_message(vendor: str) -> str:
    return f"{vendor_name(vendor)} rejected this key. Check it and enter it again."


def unconfirmed_message(model: str) -> str:
    return f"Could not confirm that {model} is available to this key; the first call will tell."


def saved_key_usable(store: Any, reference: str, choice: AgentAuthChoice) -> bool:
    """Is a stored reference still readable and scoped to this exact endpoint choice?"""

    from sleeptalk.credential_store import CredentialError

    if not reference:
        return False
    try:
        status = store.status(reference)
        return status.provider == auth_scope(choice) and bool(status.available)
    except CredentialError:
        return False


def store_key(store: Any, choice: AgentAuthChoice, key: str) -> str:
    """Persist a verified key under its endpoint scope; returns the credential reference.

    A loopback endpoint may have no key; the placeholder is stored so the
    profile still names one reference and the wire format stays uniform.
    """

    value = (key or "").strip()
    if not value:
        if not key_optional(choice):
            raise ModelSetupError("model_key is empty")
        value = PLACEHOLDER_KEY
    return store.put_validated(auth_scope(choice), value)


# Model ids that can't hold a conversation: OpenAI lists them beside the chat
# models under one /models endpoint.
_NOT_CHAT = ("embedding", "whisper", "tts", "dall-e", "image", "moderation", "transcribe",
             "realtime", "audio", "search", "babbage", "davinci", "computer-use", "sora")
_MAX_MODELS = 3000


async def vendor_models(vendor: str, key: str = "") -> list[str]:
    """The chat models a vendor offers, for the model picker; [] when it won't say.

    OpenRouter's list is public. OpenAI and Anthropic list only for a key, which
    goes to that vendor alone, as it does for the key check. Ollama lists what
    is pulled locally. Another endpoint is never asked: the model is typed.
    """
    import aiohttp

    name = normalize_vendor(vendor)
    if name == OLLAMA_VENDOR:
        return await ollama_models(PRESETS[OLLAMA_VENDOR]["base_url"])
    if name == OTHER_VENDOR or name not in PRESETS:
        return []
    headers: dict[str, str] = {}
    url = PRESETS[name]["base_url"] + "/models"
    if name == "anthropic":
        if not key:
            return []
        url = PRESETS[name]["base_url"] + "/v1/models?limit=1000"
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
    elif name != "openrouter":
        if not key:
            return []
        headers = {"Authorization": f"Bearer {key}"}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8), trust_env=False) as client:
            async with client.get(url, headers=headers, allow_redirects=False) as response:
                if response.status != 200:
                    return []
                data = await response.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError, OSError, ValueError):
        return []
    finally:
        headers.clear()
    items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    models: set[str] = set()
    for item in items[:_MAX_MODELS]:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            continue
        model = item["id"].strip()
        if not model or len(model) > 200:
            continue
        outputs = (item.get("architecture") or {}).get("output_modalities") if isinstance(item.get("architecture"), dict) else None
        if isinstance(outputs, list) and "text" not in outputs:
            continue
        if name == "openai" and any(word in model.lower() for word in _NOT_CHAT):
            continue
        models.add(model)
    return sorted(models)
