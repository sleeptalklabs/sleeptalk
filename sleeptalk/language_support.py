"""Language -> provider capability matrix for the wizard and doctor.

Static data, keyed by BCP-47 primary subtag. Tiers:

- ``full``        provider documents the language for the default launch model.
- ``degraded``    usable via a non-default model or with documented caveats;
                  the wizard warns honestly and adjusts (e.g. Deepgram nova-2).
- ``unsupported`` no documented support; wizard warns, never silently fails.

LAST_VERIFIED stamps when this table was last checked against provider docs.
Warnings cite it, and doctor nudges when the data is more than
STALENESS_NUDGE_MONTHS old — provider language lists grow, so stale tables
under-promise.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

# Last checked against:
#   Deepgram  https://developers.deepgram.com/docs/models-languages-overview
#   Deepgram TTS https://developers.deepgram.com/docs/flux-tts/voices (flux, English)
#                https://developers.deepgram.com/docs/tts-models (aura-2)
#   Cartesia  https://docs.cartesia.ai/2024-06-10/api-reference/tts/sse (sonic-3)
#   ElevenLabs https://elevenlabs.io/docs/models (eleven_multilingual_v2)
LAST_VERIFIED = "2026-09"
STALENESS_NUDGE_MONTHS = 6

TIER_FULL = "full"
TIER_DEGRADED = "degraded"
TIER_UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class Support:
    tier: str
    note: str = ""

    @property
    def ok(self) -> bool:
        return self.tier == TIER_FULL


# Deepgram nova-3: en, es, fr, de, hi, ru, pt, ja, it, nl (+ "multi" code-switching),
# and Chinese (zh, zh-CN, zh-TW, zh-HK stream on nova-3-general per the public
# GET /v1/models, checked 2026-09-23).
# Korean joined nova-3 in Deepgram's model list (checked 2026-09-24; it beat nova-2).
_DEEPGRAM_NOVA3 = {"en", "es", "fr", "de", "hi", "ru", "pt", "ja", "it", "nl", "zh", "ko"}
# nova-2 (and nova) widen coverage; quality varies by language.
_DEEPGRAM_NOVA2 = {
    "bg", "ca", "cs", "da", "el", "et", "fi", "hu", "id", "lt", "lv",
    "ms", "no", "pl", "ro", "sk", "sv", "th", "tr", "uk", "vi",
}
# Cartesia STT: ink-2 and ink-preview cover five languages (docs.cartesia.ai,
# 2026-10-04; checked by a TTS round trip that day); ink-whisper
# (Whisper-based) covers the rest.
_CARTESIA_INK2 = {"en", "es", "fr", "hi", "ja"}
_CARTESIA_INK_WHISPER = {
    "en", "es", "fr", "de", "it", "pt", "pl", "nl", "sv", "da", "no", "fi",
    "ja", "ko", "zh", "hi", "ru", "ar", "tr", "uk", "vi", "th", "id", "ms",
    "tl", "el", "he", "cs", "ro", "hu", "bg", "hr", "sk", "sl", "lt", "lv", "et",
    "ca", "ta", "ur", "fa", "bn", "sw",
}
# Deepgram TTS: flux-* voices are English only; aura-2-* voices cover these.
_DEEPGRAM_AURA2 = {"en", "es", "de", "fr", "nl", "it", "ja"}
# Cartesia sonic-3: 42 languages (broad coverage; verify additions over time).
_CARTESIA_SONIC3 = {
    "en", "es", "fr", "de", "it", "pt", "pl", "nl", "sv", "da", "no", "fi",
    "ja", "ko", "zh", "hi", "ru", "ar", "tr", "uk", "vi", "th", "id", "ms",
    "tl", "el", "he", "cs", "ro", "hu", "bg", "hr", "sk", "sl", "lt", "lv", "et",
}
# Cartesia sonic-3.6: the 44 languages its model page lists (2026-10-04).
_CARTESIA_SONIC36 = {
    "en", "de", "es", "fr", "ja", "pt", "zh", "hi", "ko", "it", "nl", "pl", "ru", "sv", "tr", "tl",
    "bg", "ro", "ar", "cs", "el", "fi", "hr", "ms", "sk", "da", "ta", "uk", "hu", "no", "vi", "bn",
    "th", "he", "ka", "id", "te", "gu", "kn", "ml", "mr", "pa", "or", "ur",
}
# Cartesia sonic-2 and sonic-turbo: 15 languages.
_CARTESIA_SONIC2 = {"en", "fr", "de", "es", "pt", "zh", "ja", "hi", "it", "ko", "nl", "pl", "ru", "sv", "tr"}
# ElevenLabs eleven_multilingual_v2: 29 languages.
_ELEVENLABS_MULTILINGUAL_V2 = {
    "en", "ja", "zh", "de", "hi", "fr", "ko", "pt", "it", "es", "id", "nl",
    "tr", "tl", "pl", "sv", "bg", "ro", "ar", "cs", "el", "fi", "hr", "ms",
    "sk", "da", "ta", "uk", "ru",
}
# ElevenLabs scribe_v2: the multilingual list plus languages a hand test
# transcribed correctly (ur: 2026-09-17, spoken Urdu with an en-US hint).
_ELEVENLABS_SCRIBE_V2 = _ELEVENLABS_MULTILINGUAL_V2 | {"ur"}


def _primary(language: str) -> str:
    return (language or "").strip().lower().replace("_", "-").split("-", 1)[0]


def lookup(language: str, provider: str) -> Support:
    """Capability tier for one provider's default launch model."""
    tag = _primary(language)
    provider = provider.strip().lower()
    if not tag:
        return Support(TIER_UNSUPPORTED, "no language set")
    if provider == "deepgram":
        if tag in _DEEPGRAM_NOVA3:
            return Support(TIER_FULL)
        if tag in _DEEPGRAM_NOVA2:
            return Support(
                TIER_DEGRADED,
                "Deepgram nova-3 does not cover this language; "
                "stt.model = \"nova-2\" is required and accuracy varies",
            )
        return Support(TIER_UNSUPPORTED, "no documented Deepgram support")
    if provider == "cartesia-stt":
        if tag in _CARTESIA_INK2:
            return Support(TIER_FULL)
        if tag in _CARTESIA_INK_WHISPER:
            return Support(
                TIER_DEGRADED,
                "Cartesia ink-2 covers en, es, fr, hi, and ja; "
                "stt.model = \"ink-whisper\" is required and accuracy varies",
            )
        return Support(TIER_UNSUPPORTED, "no documented Cartesia STT support")
    if provider == "deepgram-tts":
        if tag == "en":
            return Support(TIER_FULL)
        if tag in _DEEPGRAM_AURA2:
            return Support(
                TIER_DEGRADED,
                "Deepgram flux voices are English only; "
                "tts.model must be an aura-2-* voice for this language",
            )
        return Support(TIER_UNSUPPORTED, "no Deepgram TTS voice for this language")
    if provider == "cartesia":
        # Sonic 3.6 is the default; Sonic 3 speaks four languages it doesn't
        # list, and resolve_tts_model picks it for them.
        if tag in _CARTESIA_SONIC36 or tag in _CARTESIA_SONIC3:
            return Support(TIER_FULL)
        return Support(TIER_UNSUPPORTED, "neither sonic-3.6 nor sonic-3 lists this language")
    if provider == "elevenlabs":
        if tag in _ELEVENLABS_MULTILINGUAL_V2:
            return Support(TIER_FULL)
        return Support(
            TIER_UNSUPPORTED, "eleven_multilingual_v2 does not list this language"
        )
    if provider in {"local", "local-stt"}:
        if tag == "en":
            return Support(TIER_FULL)
        return Support(TIER_UNSUPPORTED, "the supported local speech models are English only")
    if provider == "elevenlabs-stt":
        # Scribe v2 documents 90+ languages; the multilingual voice list is the
        # conservative subset we can promise end to end, plus what was measured.
        if tag in _ELEVENLABS_SCRIBE_V2:
            return Support(TIER_FULL)
        return Support(TIER_DEGRADED, "ElevenLabs Scribe may cover this language; accuracy is undocumented here")
    aggregator = _aggregator_support(tag, provider)
    if aggregator is not None:
        return aggregator
    return Support(TIER_FULL, "no capability data for this provider — assuming OK")


def _aggregator_support(tag: str, provider: str) -> Support | None:
    """OpenRouter, Groq, Together, Venice: judged by the speech catalog.

    The launch model covers it: full. Another model the catalog recommends
    does: degraded, and the build swaps to it (``resolve_*_model``). No
    documented model does: unsupported. Undocumented coverage stays full.
    """
    from sleeptalk import speech_catalog as catalog

    role = catalog.STT if provider.endswith("-stt") else catalog.TTS
    name = provider.removesuffix("-stt")
    if name not in ("openrouter", "groq", "together", "venice"):
        return None
    launch = catalog.recommended(name, role, "en-US")
    best = catalog.recommended(name, role, tag)
    if launch is None or best is None:
        return None
    if best.id != launch.id and best.covers(tag) is not False:
        kind = "stt" if role == catalog.STT else "tts"
        return Support(TIER_DEGRADED, f"{launch.id} is not the pick for this language; {kind}.model = \"{best.id}\" is used")
    if launch.covers(tag) is not False:
        return Support(TIER_FULL) if launch.covers(tag) else Support(
            TIER_FULL, f"{launch.id} does not document its languages; assuming OK")
    return Support(TIER_UNSUPPORTED, f"no {name} model in the catalog documents this language")


def summary(language: str, providers: list[str]) -> list[tuple[str, Support]]:
    return [(provider, lookup(language, provider)) for provider in providers]


def warnings_for(
    language: str, providers: list[str], *, today: date | None = None
) -> list[str]:
    """Honest capability warnings for non-full tiers, citing LAST_VERIFIED."""
    problems: list[str] = []
    for provider, support in summary(language, providers):
        if support.tier == TIER_FULL:
            continue
        problems.append(
            f"{provider}: {support.tier} support for '{language}' — {support.note} "
            f"(capability data last verified {LAST_VERIFIED})"
        )
    nudge = staleness_nudge(today)
    if nudge and problems:
        problems.append(nudge)
    return problems


def _parse_verified() -> date:
    year, month = (int(part) for part in LAST_VERIFIED.split("-"))
    return date(year, month, 1)


def staleness_months(today: date | None = None) -> int:
    today = today or date.today()
    verified = _parse_verified()
    return max(0, (today.year - verified.year) * 12 + today.month - verified.month)


def staleness_nudge(today: date | None = None) -> str | None:
    """Doctor nudge when the capability table may be out of date."""
    months = staleness_months(today)
    if months < STALENESS_NUDGE_MONTHS:
        return None
    return (
        f"language capability data is {months} months old (last verified "
        f"{LAST_VERIFIED}) — re-check provider docs and bump LAST_VERIFIED in "
        "sleeptalk/language_support.py"
    )


# ---------------------------------------------------------------------------
# Model resolution: the language picks the model, the user never has to.
# ---------------------------------------------------------------------------

# Launch model per provider and the wider model it falls back to when the
# language is a degraded tier. Deepgram TTS is absent on purpose: there the
# model *is* the voice, so the voice pick (voice_list) carries the language.
_STT_LAUNCH_MODELS = {"deepgram": "nova-3", "cartesia": "ink-2", "elevenlabs": "scribe_v2", "local": "moonshine-small-streaming-en"}
_STT_WIDER_MODELS = {"deepgram": "nova-2", "cartesia": "ink-whisper"}
_TTS_LAUNCH_MODELS = {"cartesia": "sonic-3.6", "elevenlabs": "eleven_multilingual_v2", "deepgram": "flux-hannah-en", "local": "kokoro-en"}
# OpenAI-compatible audio services route many vendors' models by slug; their
# language swap comes from the speech catalog (speech_catalog.recommended).
_AGGREGATORS = ("openrouter", "groq", "together", "venice")

# Every model the streaming path can use, with the coverage a user reads in
# the advanced "Speech models" setting. Batch-only models (Deepgram
# whisper-cloud) are left out: the call path cannot use them.
STT_MODEL_CHOICES: dict[str, tuple[tuple[str, str], ...]] = {
    "deepgram": (
        ("nova-3", "en, es, fr, de, hi, ru, pt, ja, it, nl; \"multi\" code-switches between them"),
        ("nova-2", "30+ languages, lower accuracy"),
    ),
    "cartesia": (
        ("ink-2", "en, es, fr, hi, ja, streaming; detects which"),
        ("ink-preview", "Cartesia's preview of the next Ink; same languages as ink-2"),
        ("ink-whisper", "40+ languages, REST only"),
    ),
    "elevenlabs": (
        ("scribe_v2", "90+ languages, auto-detects"),
    ),
    "local": (
        ("moonshine-small-streaming-en", "English only, streaming on this machine"),
        ("moonshine-tiny-en", "English only, on this machine"),
    ),
}
TTS_MODEL_CHOICES: dict[str, tuple[tuple[str, str], ...]] = {
    "cartesia": (
        ("sonic-3.6", "Cartesia's recommended model, 44 languages; follows the latest 3.6 snapshot"),
        ("sonic-3.6-2026-08-27", "Sonic 3.6 pinned to its 2026-08-27 snapshot"),
        ("sonic-3", "40+ languages; pronunciation follows tts.language"),
        ("sonic-preview", "Cartesia's beta; can change without notice"),
        ("sonic-2", "15 languages; Cartesia retires it on 2026-10-20"),
        ("sonic-turbo", "15 languages; Cartesia retires it on 2026-10-20"),
    ),
    "elevenlabs": (
        ("eleven_multilingual_v2", "29 languages, best quality, detects language from text"),
        ("eleven_flash_v2_5", "32 languages, lowest latency, takes tts.language"),
        ("eleven_turbo_v2_5", "32 languages, balanced, takes tts.language"),
    ),
}


def _stt_key(provider: str) -> str:
    provider = provider.strip().lower()
    return "deepgram" if provider == "deepgram" else f"{provider}-stt"


def _tts_key(provider: str) -> str:
    provider = provider.strip().lower()
    return "deepgram-tts" if provider == "deepgram" else provider


def resolve_stt_model(provider: str, language: str, model: str = "") -> str:
    """The STT model this language needs on this provider.

    A configured model that is not the provider's launch default is the
    user's explicit choice and is kept. The launch default (or an empty
    model) swaps to the provider's wider model when the language is a
    degraded tier — the same swap the wizard used to apply, now at build
    time so hand-edited profiles and every route get it.
    """
    provider = provider.strip().lower()
    if provider in _AGGREGATORS:
        return _resolve_aggregator_model(provider, "stt", language, model)
    launch = _STT_LAUNCH_MODELS.get(provider, "")
    selected = (model or "").strip()
    if selected and launch:
        # A model from another provider's family (nova-3 left in a profile
        # that moved to Cartesia) is not a choice; it snaps to the launch
        # model first, the same way the builder snaps it.
        from sleeptalk.builder import stt_model_for_provider

        selected = stt_model_for_provider(provider, selected)
    if selected and selected != launch:
        return selected
    wider = _STT_WIDER_MODELS.get(provider)
    if wider and lookup(language, _stt_key(provider)).tier == TIER_DEGRADED:
        return wider
    return selected or launch


def resolve_tts_model(provider: str, language: str, model: str = "") -> str:
    """The TTS model for this language.

    Cartesia's launch model (or none) follows the language: Sonic 3.6, or
    Sonic 3 for the languages only it lists (Slovenian, Lithuanian, Latvian,
    Estonian). ElevenLabs multilingual_v2 covers its whole list with one
    model, and Deepgram's language lives in the voice. The one place a swap
    goes, so builder and doctor never grow their own.
    """
    provider = provider.strip().lower()
    if provider in _AGGREGATORS:
        return _resolve_aggregator_model(provider, "tts", language, model)
    selected = (model or "").strip()
    launch = _TTS_LAUNCH_MODELS.get(provider, "")
    if provider == "cartesia" and selected in ("", launch):
        from sleeptalk import speech_catalog as catalog

        best = catalog.recommended(provider, "tts", language)
        return best.id if best else launch
    return selected or launch


def _resolve_aggregator_model(provider: str, role: str, language: str, model: str) -> str:
    """An empty or launch-default model follows the language; any other model is a choice.

    A direct provider's launch default left in the profile (nova-3 after a
    switch to OpenRouter) counts as empty, as the builder already treats it.
    """
    from sleeptalk import speech_catalog as catalog

    launch = catalog.launch_model(provider, role)
    selected = (model or "").strip()
    if selected in _FOREIGN_LAUNCH_MODELS:
        selected = ""
    if selected and selected != launch:
        return selected
    best = catalog.recommended(provider, role, language)
    return catalog.profile_model(best) if best else (selected or launch)


# Launch defaults of the direct providers: in an aggregator profile they are
# leftovers from a switch, not a choice.
_FOREIGN_LAUNCH_MODELS = frozenset({
    *_STT_LAUNCH_MODELS.values(), *_STT_WIDER_MODELS.values(), *_TTS_LAUNCH_MODELS.values(),
    "sonic-3", "sonic-2", "flux-haley-en", "piper-lessac-medium",
    # Together's launch model before Cartesia retires Sonic 2 on 2026-10-20.
    "cartesia/sonic-2",
})


@dataclass(frozen=True)
class LanguageSupport:
    """What a profile language gets end to end on the configured providers."""

    language: str
    stt: Support
    tts: Support
    spoken_lines: bool

    @property
    def supported(self) -> bool:
        """Full support: hears it, speaks it, and has hand-written acks for it."""
        return self.stt.tier != TIER_UNSUPPORTED and self.tts.tier != TIER_UNSUPPORTED and self.spoken_lines

    @property
    def is_english(self) -> bool:
        return _primary(self.language) == "en"


def support_for(language: str, stt_provider: str, tts_provider: str) -> LanguageSupport:
    from sleeptalk.spoken_lines import for_language

    return LanguageSupport(
        language=language,
        stt=lookup(language, _stt_key(stt_provider)),
        tts=lookup(language, _tts_key(tts_provider)),
        spoken_lines=for_language(language) is not None,
    )


def primary_tag(language: str) -> str:
    """Public form of the primary-subtag helper (``"fr-FR"`` -> ``"fr"``)."""
    return _primary(language)
