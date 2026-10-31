"""Live voice-list fetching for the wizard's audition step.

Voices are fetched from the provider's own API (never hardcoded IDs, which
rot) and filtered/annotated by the language chosen in the wizard's language
step. Paste-any-voice-ID is always available as an escape hatch, so an empty
or wrong filter never blocks setup.
"""

from __future__ import annotations

from dataclasses import dataclass

import aiohttp

_TIMEOUT = aiohttp.ClientTimeout(total=8)


@dataclass(frozen=True)
class Voice:
    id: str
    name: str
    language: str = ""  # primary subtag when the provider declares one
    description: str = ""

    def matches(self, language: str) -> bool:
        if not self.language:
            return False
        return self.language.lower() == _primary(language)


def _primary(language: str) -> str:
    return (language or "").strip().lower().replace("_", "-").split("-", 1)[0]


async def list_cartesia_voices(api_key: str) -> list[Voice]:
    """Cartesia GET /voices — each voice declares a `language` subtag."""
    voices: list[Voice] = []
    params: dict[str, str] = {"limit": "100"}
    async with aiohttp.ClientSession() as session:
        for _page in range(5):  # paginated; 500 voices is plenty to curate 3
            async with session.get(
                "https://api.cartesia.ai/voices",
                headers={
                    "Cartesia-Version": "2026-03-01",
                    "Authorization": f"Bearer {api_key}",
                },
                params=params,
                timeout=_TIMEOUT,
            ) as response:
                if response.status != 200:
                    raise RuntimeError(
                        f"Cartesia voice list failed (HTTP {response.status})."
                    )
                payload = await response.json()
            data = payload.get("data", payload if isinstance(payload, list) else [])
            for item in data:
                voices.append(
                    Voice(
                        id=str(item.get("id", "")),
                        name=str(item.get("name", "")),
                        language=str(item.get("language", "")),
                        description=str(item.get("description", "")),
                    )
                )
            cursor = payload.get("next_page") if isinstance(payload, dict) else None
            if not cursor:
                break
            params["starting_after"] = str(cursor)
    return [voice for voice in voices if voice.id]


async def list_elevenlabs_voices(api_key: str) -> list[Voice]:
    """ElevenLabs GET /v1/voices — language inferred from labels when present.

    ElevenLabs voices don't declare a single language (models are
    multilingual), so matches() is advisory here: we filter on the
    `language`/`accent` label when one exists and otherwise keep the voice
    with an honest annotation in the wizard.
    """
    async with aiohttp.ClientSession() as session, session.get(
        "https://api.elevenlabs.io/v1/voices",
        headers={"xi-api-key": api_key},
        timeout=_TIMEOUT,
    ) as response:
        if response.status != 200:
            raise RuntimeError(
                f"ElevenLabs voice list failed (HTTP {response.status})."
            )
        payload = await response.json()
    voices: list[Voice] = []
    for item in payload.get("voices", []):
        labels = item.get("labels") or {}
        language = str(labels.get("language") or labels.get("accent") or "")
        description = ", ".join(
            str(v) for k, v in sorted(labels.items()) if k in ("accent", "age", "gender", "use_case")
        )
        voices.append(
            Voice(
                id=str(item.get("voice_id", "")),
                name=str(item.get("name", "")),
                language=_primary(language),
                description=description,
            )
        )
    return [voice for voice in voices if voice.id]


# Deepgram Flux voices are fixed model names, not a fetched catalog
# (https://developers.deepgram.com/docs/flux-tts/voices). Featured voices first.
DEEPGRAM_FLUX_VOICES: tuple[tuple[str, str], ...] = (
    ("haley", "American, featured"), ("hannah", "American, featured"), ("kit", "American, featured"),
    ("alexis", "American, featured"), ("cliff", "American, featured"), ("sienna", "American, featured"),
    ("cole", "American, featured"), ("brooke", "American, featured"), ("colin", "British, featured"),
    ("gemma", "British, featured"), ("heather", "American, featured"), ("miles", "American, featured"),
    ("sean", "Irish, featured"), ("bree", "American"), ("brittany", "American"), ("bruce", "American"),
    ("conor", "Irish"), ("donovan", "American"), ("drew", "American"), ("elise", "American"),
    ("jack", "American"), ("kai", "American"), ("kelsey", "American"), ("maeve", "Irish"),
    ("marcelo", "American"), ("marcus", "American"), ("meena", "Indian"), ("meghan", "American"),
    ("naveen", "Indian"), ("paige", "American"), ("priya", "Indian"), ("rufus", "British"),
    ("sharon", "Australian"), ("tanner", "American"), ("wade", "Australian"), ("wes", "American"),
)


# Deepgram Aura-2 voices: the model is the voice, and the language is in the
# name. Flux is English only, so a non-English profile lands on one of these.
# Seed list from https://developers.deepgram.com/docs/tts-models; the live
# list (GET /v1/models, needs a key) supersedes it when a key is at hand.
DEEPGRAM_AURA2_VOICES: tuple[tuple[str, str, str], ...] = (
    ("thalia", "en", "American, featured"), ("andromeda", "en", "American"),
    ("helena", "en", "American"), ("apollo", "en", "American"),
    ("arcas", "en", "American"), ("aries", "en", "American"),
    ("asteria", "en", "American"), ("athena", "en", "American"),
    ("atlas", "en", "American"), ("aurora", "en", "American"),
    ("celeste", "es", "Colombian"), ("estrella", "es", "Mexican"),
    ("nestor", "es", "Peninsular"), ("sirio", "es", "Mexican"),
    ("carina", "es", "Peninsular"), ("alvaro", "es", "Peninsular"),
    ("diana", "es", "Peninsular"), ("aquila", "es", "Latin American"),
    ("selena", "es", "Latin American"), ("javier", "es", "Mexican"),
)
DEEPGRAM_MODELS_URL = "https://api.deepgram.com/v1/models"


def list_deepgram_voices() -> list[Voice]:
    """Deepgram voices without a key: Flux (English) plus the Aura-2 seed list.

    The voice id is the model name. Flux comes first so an English profile
    keeps the streaming voice; Aura-2 follows, tagged by language, so the
    language-ranked pick can land on a Spanish voice.
    """
    flux = [
        Voice(id=f"flux-{name}-en", name=name.title(), language="en", description=f"{accent} English")
        for name, accent in DEEPGRAM_FLUX_VOICES
    ]
    aura = [
        Voice(id=f"aura-2-{name}-{language}", name=name.title(), language=language,
              description=f"{accent}, Aura-2")
        for name, language, accent in DEEPGRAM_AURA2_VOICES
    ]
    return flux + aura


async def fetch_deepgram_voices(api_key: str) -> list[Voice]:
    """Deepgram GET /v1/models: every TTS model with its language list.

    Aura-2 covers seven languages and the list grows, so the live catalog
    beats the seed. Falls back to the seed on any failure so a key that
    cannot list models never blocks setup.
    """
    if not api_key.strip():
        return list_deepgram_voices()
    try:
        async with aiohttp.ClientSession() as session, session.get(
            DEEPGRAM_MODELS_URL,
            headers={"Authorization": f"Token {api_key}"},
            timeout=_TIMEOUT,
        ) as response:
            if response.status != 200:
                return list_deepgram_voices()
            payload = await response.json()
    except Exception:
        return list_deepgram_voices()
    return deepgram_voices_from_models(payload) or list_deepgram_voices()


def deepgram_voices_from_models(payload: object) -> list[Voice]:
    """Voices from a /v1/models document; empty when the shape is unknown."""
    if not isinstance(payload, dict):
        return []
    voices: list[Voice] = []
    for item in payload.get("tts", []) or []:
        if not isinstance(item, dict):
            continue
        model = str(item.get("canonical_name") or item.get("name") or "").strip()
        if not model or not model.startswith(("flux-", "aura-")):
            continue
        languages = [str(tag) for tag in (item.get("languages") or []) if str(tag).strip()]
        language = _primary(languages[0]) if languages else model.rsplit("-", 1)[-1]
        metadata = item.get("metadata") or {}
        accent = str(metadata.get("accent") or "") if isinstance(metadata, dict) else ""
        family = "Flux" if model.startswith("flux-") else "Aura-2" if model.startswith("aura-2-") else "Aura"
        name = model.split("-")[-2] if model.count("-") >= 2 else model
        voices.append(Voice(
            id=model, name=name.title(), language=language,
            description=", ".join(part for part in (accent, family) if part),
        ))
    # Flux first: the streaming voice stays the English default.
    return sorted(voices, key=lambda voice: not voice.id.startswith("flux-"))


def list_local_voices() -> list[Voice]:
    """Shipped local voices and this host's saved Pocket references."""
    from sleeptalk.speech.local_models import MODELS
    from sleeptalk.speech.local_voices import list_voices as saved_voices

    voices: list[Voice] = []
    for model in MODELS.values():
        if model.kind != "tts":
            continue
        for name in model.voices:
            voices.append(Voice(id=name, name=name.replace("_", " ").title(), language="en",
                                description=f"{model.label}, {model.size_mb} MB, {model.license}"))
    voices.extend(Voice(id=v["voice_id"], name=v["name"], language="en",
                        description="Kyutai Pocket TTS, uploaded voice") for v in saved_voices())
    return voices


async def list_voices(provider: str, api_key: str) -> list[Voice]:
    provider = provider.strip().lower()
    if provider == "local":
        return list_local_voices()
    if provider == "deepgram":
        return await fetch_deepgram_voices(api_key)
    if provider == "cartesia":
        return await list_cartesia_voices(api_key)
    if provider == "elevenlabs":
        return await list_elevenlabs_voices(api_key)
    raise ValueError(f"no voice listing for provider {provider!r}")


def curate(voices: list[Voice], language: str, *, limit: int = 3) -> tuple[list[Voice], str | None]:
    """Pick up to ``limit`` audition voices for the chosen language.

    Returns (voices, warning) — warning is set when the language filter
    matched nothing and we fell back to unfiltered picks.
    """
    matches = [voice for voice in voices if voice.matches(language)]
    if matches:
        return matches[:limit], None
    if not voices:
        return [], "no voices returned by the provider — paste a voice ID instead"
    return voices[:limit], (
        f"no voices explicitly tagged '{_primary(language)}' — showing "
        "untagged voices; multilingual models may still cover your language, "
        "listen and judge, or paste any voice ID"
    )
