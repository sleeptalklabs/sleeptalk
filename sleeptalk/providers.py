from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlsplit, urlunsplit

from sleeptalk.speech.contracts import TTS


@dataclass(frozen=True)
class ProviderCapabilities:
    supports_streaming: bool = False
    supports_word_timings: bool = False
    supports_interruption: bool = False


@dataclass(frozen=True)
class ProviderSpec:
    provider: str
    model: str = ""
    voice_id: str = ""
    language: str = ""
    temperature: float | None = None
    speed: float | None = None
    options: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> ProviderSpec | None:
        if not value:
            return None
        known = {
            "provider",
            "model",
            "voice_id",
            "language",
            "temperature",
            "speed",
        }
        provider = str(value.get("provider") or "").strip()
        if not provider:
            raise ValueError("provider spec requires a provider.")
        return cls(
            provider=provider,
            model=str(value.get("model") or ""),
            voice_id=str(value.get("voice_id") or ""),
            language=str(value.get("language") or ""),
            temperature=_optional_float(value.get("temperature")),
            speed=_optional_float(value.get("speed")),
            options={str(key): item for key, item in value.items() if key not in known},
        )


def check_provider_base(url: str) -> str:
    """A speech ``base_url`` a key may be sent to: HTTPS (or WSS) anywhere,
    plain http/ws only on this machine, no credentials, query, or fragment.

    The provider's key rides in every request's headers, so an address typed
    into a profile must not carry it in the clear or somewhere unexpected.
    """
    import ipaddress

    value = str(url).strip()
    parts = urlsplit(value)
    host = (parts.hostname or "").lower()
    if (parts.scheme not in {"https", "wss", "http", "ws"} or not host or parts.username is not None
            or parts.password is not None or parts.query or parts.fragment or any(ord(c) <= 32 for c in value)):
        raise ValueError("base_url must be an https:// address with no credentials, query, or fragment.")
    if parts.scheme in {"http", "ws"}:
        try:
            loopback = host == "localhost" or ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = False
        if not loopback:
            raise ValueError(f"base_url for {host} uses plain {parts.scheme}://, which would send the key unencrypted; "
                             "use https:// (plain http is only for 127.0.0.1 or localhost).")
    return value


def check_gateway_address(url: str) -> str:
    """An agent gateway a session token may travel to: TLS anywhere; plain
    ws/http on this machine or over Tailscale (WireGuard already encrypts
    100.64.0.0/10, fd7a:115c:a1e0::/48, and *.ts.net). A LAN or public
    address needs wss:// or https://."""
    import ipaddress

    value = str(url).strip()
    parts = urlsplit(value)
    host = (parts.hostname or "").lower()
    if (parts.scheme not in {"https", "wss", "http", "ws"} or not host or parts.username is not None
            or parts.password is not None or parts.query or parts.fragment or any(ord(c) <= 32 for c in value)):
        raise ValueError("Use a gateway address without credentials, query, or fragment.")
    if parts.scheme in {"http", "ws"}:
        try:
            address = ipaddress.ip_address(host)
            private_path = address.is_loopback or address in ipaddress.ip_network(
                "100.64.0.0/10" if address.version == 4 else "fd7a:115c:a1e0::/48")
        except ValueError:
            private_path = host == "localhost" or host.endswith(".ts.net")
        if not private_path:
            raise ValueError(f"The gateway {value!r} uses plain {parts.scheme}://, which would send its token "
                             "unencrypted; use wss:// or https:// (plain is only for this machine or a tailnet address).")
    return value


def provider_http_url(spec: ProviderSpec | None, default_base: str, path: str) -> str:
    override = str((spec.options if spec else {}).get("base_url") or "")
    base = check_provider_base(override) if override else default_base
    return f"{base.rstrip('/')}/{path.lstrip('/')}"


def provider_websocket_url(
    spec: ProviderSpec | None,
    default_base: str,
    path: str,
) -> str:
    http_url = provider_http_url(spec, default_base, path)
    parts = urlsplit(http_url)
    scheme = {"https": "wss", "http": "ws"}.get(parts.scheme, parts.scheme)
    return urlunsplit((scheme, parts.netloc, parts.path, parts.query, parts.fragment))


@dataclass(frozen=True)
class TurnDetectionSpec:
    vad: str = ""
    semantic: bool = False
    silence_ms: int = 0

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> TurnDetectionSpec:
        if not value:
            return cls()
        return cls(
            vad=str(value.get("vad") or ""),
            semantic=bool(value.get("semantic", False)),
            silence_ms=int(value.get("silence_ms") or 0),
        )


@dataclass(frozen=True)
class ProviderManifest:
    stt: ProviderSpec | None = None
    llm: ProviderSpec | None = None
    tts: ProviderSpec | None = None
    fused: ProviderSpec | None = None
    fallback_tts: tuple[ProviderSpec, ...] = ()
    turn_detection: TurnDetectionSpec = field(default_factory=TurnDetectionSpec)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> ProviderManifest:
        fallback = value.get("fallback")
        fallback_tts = ()
        if isinstance(fallback, Mapping):
            raw_tts = fallback.get("tts") or []
            if isinstance(raw_tts, list):
                fallback_tts = tuple(
                    spec
                    for item in raw_tts
                    if isinstance(item, Mapping)
                    for spec in [ProviderSpec.from_mapping(item)]
                    if spec is not None
                )
        return cls(
            stt=_provider_spec(value.get("stt")),
            llm=_provider_spec(value.get("llm")),
            tts=_provider_spec(value.get("tts")),
            fused=_provider_spec(value.get("fused")),
            fallback_tts=fallback_tts,
            turn_detection=TurnDetectionSpec.from_mapping(_mapping(value.get("turn_detection"))),
        )


class TTSAdapter(TTS, Protocol):
    capabilities: ProviderCapabilities


async def collect_tts_audio(
    adapter: TTSAdapter,
    text: str,
    *,
    on_first_chunk: Callable[[int], None] | None = None,
) -> bytes:
    """Collect canonical provider PCM into a WAV clip for file-playback APIs."""
    from contextlib import aclosing

    from sleeptalk.media_audio import pcm_to_wav

    chunks: list[bytes] = []
    synthesize = getattr(adapter, "stream_pcm", None) or getattr(adapter, "synthesize", None)
    if synthesize is None:
        # File-only clip producers are consumed here, outside the route contract.
        path = await adapter.synthesize_wav(text)
        try:
            return path.read_bytes()
        finally:
            path.unlink(missing_ok=True)
    async with aclosing(synthesize(text)) as stream:
        async for chunk in stream:
            if not chunk:
                continue
            if not chunks and on_first_chunk is not None:
                on_first_chunk(len(chunk))
            chunks.append(chunk)
    return pcm_to_wav(b"".join(chunks))


async def synthesize_tts_clip(adapter: TTSAdapter, text: str) -> bytes:
    return await collect_tts_audio(adapter, text)


def _provider_spec(value: Any) -> ProviderSpec | None:
    mapping = _mapping(value)
    return ProviderSpec.from_mapping(mapping) if mapping is not None else None


def _mapping(value: Any) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return float(value)
