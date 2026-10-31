"""Shared TTS speed staircase. Spec holds the operator step; clamp is wire-only.

The operator picks one of ``SPEED_STEPS`` and every surface shows that step
("1.25x"). Each provider accepts a different interval, and some read the
number in the opposite direction, so the step is translated at the wire in
exactly one place: :func:`wire_speed`.

Adding a TTS provider
---------------------
Every TTS registration needs a :class:`SpeedProfile` here, keyed by its
registration name, before it can ship. ``tests/test_tts_speed.py`` fails for
any built-in TTS provider without one. A provider whose accepted interval
depends on the model declares one profile per model prefix. A provider
outside this package declares its profile from its ``register()`` callback
with :func:`declare_speed_profile`.

The interval must come from the provider's API reference or from an observed
rejection, and the source is recorded beside the entry. A too-wide interval
is a silent outage: the provider answers 400 on every sentence and the caller
hears nothing (ElevenLabs, 2026-09-17, speed 1.25 against an accepted 1.2).
"""

from __future__ import annotations

from dataclasses import dataclass

# 2x was removed on 2026-09-17: no supported cloud provider plays it (ElevenLabs
# stops at 1.2, Deepgram, Cartesia and OpenAI Realtime at 1.5), so the step
# only ever collapsed onto 1.5x or below.
SPEED_STEPS: tuple[float, ...] = (0.5, 1.0, 1.25, 1.5)


@dataclass(frozen=True)
class SpeedProfile:
    """How one provider reads the operator's speed step on the wire.

    ``low`` and ``high`` bound the value the provider accepts. ``inverted``
    means the provider treats values below 1.0 as faster, so the step is
    reciprocated before clamping. ``source`` names where the bounds came from.
    """

    low: float
    high: float
    inverted: bool = False
    source: str = ""

    def wire(self, step: float) -> float:
        value = 1.0 / step if self.inverted else step
        return round(min(self.high, max(self.low, value)), 4)


# Keyed by registration name, or "name:model" for a model-dependent interval.
# The exact model wins; the bare name covers models this table has not seen.
_SPEED_PROFILES: dict[str, SpeedProfile] = {
    "cartesia": SpeedProfile(
        0.6, 1.5, source="docs.cartesia.ai Sonic-3 generation_config.speed: 0.6 to 1.5",
    ),
    "deepgram": SpeedProfile(
        0.7, 1.5, source="developers.deepgram.com TTS voice controls: 0.7 to 1.5, outside errors",
    ),
    "elevenlabs": SpeedProfile(
        0.7, 1.2, source="api.elevenlabs.io 400 invalid_voice_settings: 'greater or equal to 0.7 and less or equal to 1.2'",
    ),
    "hume": SpeedProfile(
        0.5, 2.0, source="dev.hume.ai utterances[].speed; below 0.75 or above 1.5 may be unstable",
    ),
    "local": SpeedProfile(
        0.5, 2.0, source="sherpa-onnx OfflineTts.generate(speed=); Piper maps to 1/length_scale, Kokoro takes it directly",
    ),
    "local:pocket-tts-en": SpeedProfile(
        1.0, 1.0, source="sherpa-onnx 1.13.8 Pocket Generate ignores GenerationConfig.speed; native speed only",
    ),
    "openai-realtime": SpeedProfile(
        0.25, 1.5, source="platform.openai.com Realtime session audio.output.speed: 0.25 to 1.5",
    ),
    "playht": SpeedProfile(
        0.5, 2.0, source="docs.play.ht v2 speed: 0.5 to 2.0",
    ),
    # Rime documents no bounds for speedAlpha, so the staircase extremes pass
    # through. Mist and Mist v2 read speedAlpha backwards: lower is faster.
    "rime": SpeedProfile(
        0.5, 2.0, source="docs.rime.ai/docs/speed: Mist v3, Arcana and Coda higher is faster; no bounds published",
    ),
    # OpenAI-shaped /audio/speech: OpenAI documents 0.25 to 4.0; the aggregators
    # pass it through where the model supports it and ignore it otherwise, so the
    # operator staircase (0.5 to 1.5) is sent unchanged.
    "openrouter": SpeedProfile(0.25, 4.0, source="platform.openai.com audio/speech speed: 0.25 to 4.0; per-model on OpenRouter"),
    "groq": SpeedProfile(0.25, 4.0, source="console.groq.com audio/speech follows the OpenAI shape"),
    "together": SpeedProfile(0.25, 4.0, source="docs.together.ai audio/speech follows the OpenAI shape"),
    "venice": SpeedProfile(0.25, 4.0, source="docs.venice.ai audio/speech speed: model-specific, OpenAI shape"),
    "rime:mistv2": SpeedProfile(
        0.5, 2.0, inverted=True, source="docs.rime.ai/docs/speed: Mist v2 lower than 1.0 is faster",
    ),
    "rime:mist": SpeedProfile(
        0.5, 2.0, inverted=True, source="docs.rime.ai/docs/speed: Mist lower than 1.0 is faster",
    ),
}


def declare_speed_profile(provider: str, profile: SpeedProfile, *, model: str = "") -> None:
    """Register the interval for a provider added outside this package."""
    _SPEED_PROFILES[_key(provider, model)] = profile


def declared_speed_providers() -> frozenset[str]:
    """Registration names that own a speed profile."""
    return frozenset(key.split(":", 1)[0] for key in _SPEED_PROFILES)


def speed_profile(provider: str, model: str = "") -> SpeedProfile | None:
    """The profile for a provider and model; the exact model entry wins."""
    name = provider.strip().lower()
    if model.strip():
        exact = _SPEED_PROFILES.get(_key(name, model))
        if exact is not None:
            return exact
    return _SPEED_PROFILES.get(name)


def _key(provider: str, model: str) -> str:
    name = provider.strip().lower()
    return f"{name}:{model.strip().lower()}" if model.strip() else name


def snap_speed(value: float | None) -> float:
    """Nearest SPEED_STEPS member. None -> 1.0. Exact ties pick the higher step. Idempotent."""
    if value is None:
        return 1.0
    return min(SPEED_STEPS, key=lambda step: (abs(step - value), -step))


def next_speed(value: float | None) -> float:
    """Snap, then advance on the ring. 1.5 wraps to 0.5. None treated as 1.0 (first cycle -> 1.25)."""
    current = snap_speed(value)
    index = SPEED_STEPS.index(current)
    return SPEED_STEPS[(index + 1) % len(SPEED_STEPS)]


def wire_speed(provider: str, value: float | None, *, model: str = "") -> float:
    """snap_speed, then translate to what this provider accepts.

    Unknown provider: snap only. A model-dependent provider passes ``model``
    so an inverted or narrower interval applies. Not idempotent for an
    inverted profile, by design: the input is always the operator's step.
    """
    stepped = snap_speed(value)
    profile = speed_profile(provider, model)
    if profile is None:
        return stepped
    return profile.wire(stepped)


def format_speed(value: float | None) -> str:
    step = snap_speed(value)
    if step == int(step):
        return f"{int(step)}x"
    return f"{step:g}x"
