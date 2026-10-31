"""Construct STT and TTS providers through the component registries.

Provider specs carry configuration; env carries credentials. This boundary
validates provider options and attaches usage metering to constructed adapters.
"""

from __future__ import annotations

from dataclasses import fields, replace
from typing import Any

from sleeptalk.defaults import STTComponents, register_defaults
from sleeptalk.metering import UsageMeter, attach_usage_meter
from sleeptalk.providers import ProviderSpec
from sleeptalk.registries import STT_PROVIDERS, TTS_PROVIDERS

__all__ = [
    "build_stt_components",
    "build_tts",
    "build_tts_chain",
    "canonical_stt_providers",
    "canonical_tts_providers",
]


def _ensure_registered() -> None:
    register_defaults()


def _configured_spec(spec: ProviderSpec, registry, common_fields: set[str]) -> ProviderSpec:
    registration = registry.get(spec.provider)
    spec_fields = {item.name for item in fields(ProviderSpec)}
    duplicates = set(spec.options) & spec_fields
    if duplicates:
        raise ValueError(f"Provider options duplicate common fields: {', '.join(sorted(duplicates))}")
    if registration.options_schema is not None:
        overlaps = set(registration.options_schema.model_fields) & (common_fields | spec_fields)
        if overlaps:
            raise ValueError(f"{registration.name!r} option schema shadows common fields: {', '.join(sorted(overlaps))}")
    private = {key: value for key, value in spec.options.items() if key not in common_fields}
    options = {**spec.options, **registration.validate_options(private)}
    return replace(spec, options=options)


def build_stt_components(
    spec: ProviderSpec | None,
    env: dict[str, str],
    *,
    meter: UsageMeter | None = None,
) -> STTComponents | None:
    """Construct the STT adapter pair (rest + streaming) for a spec.

    Returns ``None`` when no STT spec is configured. Raises ``ValueError``
    (from the registry) naming valid providers when the spec's provider is
    unknown. The meter, when given, is attached to both components so
    attempts are observable (usage identity).
    """
    if spec is None:
        return None
    _ensure_registered()
    from sleeptalk.config import STTConfig

    spec = _configured_spec(spec, STT_PROVIDERS, set(STTConfig.model_fields))
    components = STT_PROVIDERS.build(spec.provider, spec, env)
    if not isinstance(components, STTComponents):
        raise TypeError(f"STT provider {spec.provider!r} must return STTComponents.")
    if components.rest is not None and not callable(getattr(components.rest, "transcribe_pcm", None)):
        raise TypeError(f"STT provider {spec.provider!r} is missing transcribe_pcm().")
    if components.streaming is not None:
        required = ("append_pcm", "finish_transcription", "close")
        missing = [name for name in required if not callable(getattr(components.streaming, name, None))]
        if missing:
            raise TypeError(f"Streaming STT provider {spec.provider!r} is missing {', '.join(missing)}.")
    if components.rest is None and components.streaming is None:
        raise TypeError(f"STT provider {spec.provider!r} must supply REST or streaming transcription.")
    if meter is not None:
        attach_usage_meter(components.rest, meter)
        attach_usage_meter(components.streaming, meter)
    return components


def build_tts(
    spec: ProviderSpec,
    env: dict[str, str],
    *,
    meter: UsageMeter | None = None,
) -> Any:
    """Construct one TTS adapter for a spec (BYOK env carries the key)."""
    _ensure_registered()
    from sleeptalk.config import TTSSpecConfig

    spec = _configured_spec(spec, TTS_PROVIDERS, set(TTSSpecConfig.model_fields))
    adapter = TTS_PROVIDERS.build(spec.provider, spec, env)
    if meter is not None:
        attach_usage_meter(adapter, meter)
    return adapter


def build_tts_chain(
    specs: list[ProviderSpec] | tuple[ProviderSpec, ...],
    env: dict[str, str],
    *,
    meter: UsageMeter | None = None,
) -> list[Any]:
    """Construct a primary TTS adapter plus its fallbacks, in order."""
    return [build_tts(spec, env, meter=meter) for spec in specs]


def canonical_stt_providers() -> list[str]:
    _ensure_registered()
    return STT_PROVIDERS.names()


def canonical_tts_providers() -> list[str]:
    _ensure_registered()
    return TTS_PROVIDERS.names()
