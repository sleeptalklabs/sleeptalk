"""Sleeptalk: config-driven voice session composition.

Configuration selects the transport, agent, speech providers, and voice policy.
Component factories import their implementations lazily. The package does not
require a server checkout.

Library entry points:
    load_config(path)     -> SleeptalkConfig (validated, env checked)
    build_session(config) -> RealtimeSession (assembled via registries)

Exports are lazy (PEP 562) so importing the schema never pulls the builder.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from sleeptalk.builder import build_session
    from sleeptalk.config import (
        AgentConfig,
        ConfigError,
        PolicyConfig,
        SleeptalkConfig,
        STTConfig,
        TransportConfig,
        TTSConfig,
        load_config,
    )
    from sleeptalk.policy import VoicePolicy
    from sleeptalk.registries import (
        AGENTS,
        STT_PROVIDERS,
        TRANSPORTS,
        TTS_PROVIDERS,
        Registration,
        Registry,
    )
    from sleeptalk.session import RealtimeSession

_LAZY = {
    "build_session": "sleeptalk.builder",
    "RealtimeSession": "sleeptalk.session",
    "AgentConfig": "sleeptalk.config",
    "ConfigError": "sleeptalk.config",
    "PolicyConfig": "sleeptalk.config",
    "SleeptalkConfig": "sleeptalk.config",
    "STTConfig": "sleeptalk.config",
    "TransportConfig": "sleeptalk.config",
    "TTSConfig": "sleeptalk.config",
    "load_config": "sleeptalk.config",
    "VoicePolicy": "sleeptalk.policy",
    "AGENTS": "sleeptalk.registries",
    "STT_PROVIDERS": "sleeptalk.registries",
    "TRANSPORTS": "sleeptalk.registries",
    "TTS_PROVIDERS": "sleeptalk.registries",
    "Registration": "sleeptalk.registries",
    "Registry": "sleeptalk.registries",
}

__all__ = sorted(_LAZY)


def __getattr__(name: str):
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    value = getattr(importlib.import_module(module), name)
    globals()[name] = value
    return value
