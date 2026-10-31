"""Provider-agnostic, metadata-honest voice catalog discovery.

Catalog adapters normalize provider-owned metadata and preview URLs.  Search
is deliberately local: public catalogs are fetched in full (and cached),
factual conflicts are removed, and the remaining entries are ranked against
the user's words.  No phrase is ever mapped to a voice ID.
"""

from __future__ import annotations

import asyncio
import csv
import io
import math
import re
import time
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

import aiohttp

_TIMEOUT = aiohttp.ClientTimeout(total=15)
ELEVENLABS_CATALOG_URL = "https://api.elevenlabs.io/v1/shared-voices"
PLAYHT_CATALOG_URL = (
    "https://docs.google.com/spreadsheets/d/"
    "13YQFPYiwsQSQOfiuW_BB1rixzTHDwApwCUqg_45-vOw/export?format=csv&gid=0"
)
PLAYHT_SOURCE_URL = "https://docs.play.ht/reference/list-of-prebuilt-voices"
RIME_CATALOG_URL = "https://users.rime.ai/data/voices/voice_details.json"
RIME_SOURCE_URL = "https://docs.rime.ai/api-reference/data/voice-details"
HUME_CATALOG_URL = "https://api.hume.ai/v0/tts/voices"
HUME_SOURCE_URL = "https://dev.hume.ai/reference/voices/list"
CARTESIA_CATALOG_URL = "https://api.cartesia.ai/voices"

JsonFetcher = Callable[..., Awaitable[Any]]
TextFetcher = Callable[..., Awaitable[str]]


@dataclass(slots=True)
class DiscoveredVoice:
    provider: str
    voice_id: str
    name: str
    description: str | None = None
    languages: list[str] = field(default_factory=list)
    gender: str | None = None
    age: str | None = None
    accent: str | None = None
    country: str | None = None
    styles: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    preview_url: str | None = None
    preview_requires_auth: bool = False
    synthesis_requires_auth: bool = True
    source_url: str | None = None
    provider_metadata: dict[str, Any] = field(default_factory=dict, repr=False)

    def facts(self) -> list[str]:
        """Provider-supplied facts suitable for display (never inference)."""
        facts: list[str] = []
        facts.extend(self.languages)
        facts.extend(value for value in (self.gender, self.age, self.accent, self.country) if value)
        facts.extend(self.styles)
        facts.extend(tag.split(":", 1)[1] if ":" in tag else tag for tag in self.tags)
        return _unique(facts)


@dataclass(frozen=True, slots=True)
class PreviewResult:
    url: str | None
    requires_auth: bool = False
    message: str = ""


@dataclass(frozen=True, slots=True)
class VoiceMatch:
    voice: DiscoveredVoice
    score: float
    factual_matches: tuple[str, ...] = ()
    inferred_matches: tuple[str, ...] = ()

    @property
    def credible(self) -> bool:
        return self.score > 0


@dataclass(frozen=True, slots=True)
class ProviderIssue:
    provider: str
    message: str
    requires_auth: bool = False


@dataclass(slots=True)
class DiscoveryReport:
    query: str
    matches: list[VoiceMatch]
    issues: list[ProviderIssue] = field(default_factory=list)


class CatalogAuthenticationRequired(RuntimeError):
    """A provider cannot expose its catalog before credentials are supplied."""


class TTLVoiceCache:
    """Small in-memory normalized-catalog cache with an explicit TTL."""

    def __init__(self, ttl_seconds: float = 3600) -> None:
        self.ttl_seconds = ttl_seconds
        self._values: dict[str, tuple[float, list[DiscoveredVoice]]] = {}

    def get(self, key: str) -> list[DiscoveredVoice] | None:
        cached = self._values.get(key)
        if not cached:
            return None
        expires_at, voices = cached
        if expires_at <= time.monotonic():
            self._values.pop(key, None)
            return None
        return list(voices)

    def put(self, key: str, voices: list[DiscoveredVoice]) -> None:
        self._values[key] = (time.monotonic() + self.ttl_seconds, list(voices))

    def clear(self) -> None:
        self._values.clear()


class VoiceCatalogProvider(ABC):
    name: str
    catalog_requires_auth: bool = False

    def __init__(self, *, cache: TTLVoiceCache | None = None) -> None:
        self.cache = cache or TTLVoiceCache()

    @abstractmethod
    async def list_voices(self) -> list[DiscoveredVoice]:
        raise NotImplementedError

    async def search(self, query: str) -> list[VoiceMatch]:
        return rank_voices(await self.list_voices(), query)

    async def get_preview(self, voice: DiscoveredVoice) -> PreviewResult:
        if voice.preview_url:
            return PreviewResult(voice.preview_url)
        if voice.preview_requires_auth:
            return PreviewResult(
                None,
                requires_auth=True,
                message=f"{voice.provider} requires a key before previewing this voice.",
            )
        return PreviewResult(None, message=f"{voice.provider} does not publish a preview for this voice.")


async def _fetch_json(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
) -> Any:
    async with aiohttp.ClientSession() as session, session.get(
        url, headers=headers, params=params, timeout=_TIMEOUT
    ) as response:
        if response.status in (401, 403):
            raise CatalogAuthenticationRequired(f"catalog rejected credentials (HTTP {response.status})")
        if response.status == 429:
            raise RuntimeError("catalog rate limited the request (HTTP 429)")
        if response.status != 200:
            raise RuntimeError(f"catalog request failed (HTTP {response.status})")
        return await response.json(content_type=None)


async def _fetch_text(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
) -> str:
    async with aiohttp.ClientSession() as session, session.get(
        url, headers=headers, params=params, timeout=_TIMEOUT
    ) as response:
        if response.status == 429:
            raise RuntimeError("catalog rate limited the request (HTTP 429)")
        if response.status != 200:
            raise RuntimeError(f"catalog request failed (HTTP {response.status})")
        return await response.text()


class ElevenLabsCatalog(VoiceCatalogProvider):
    name = "elevenlabs"

    def __init__(self, api_key: str = "", *, fetch_json: JsonFetcher = _fetch_json,
                 cache: TTLVoiceCache | None = None, max_pages: int | None = None) -> None:
        super().__init__(cache=cache)
        self.api_key = api_key
        self.fetch_json = fetch_json
        # ElevenLabs currently permits one anonymous three-voice page, but
        # rejects pagination and filters without a key. Authenticated callers
        # can still traverse the whole catalog in 100-item pages.
        self.max_pages = max_pages if max_pages is not None else (250 if api_key else 1)

    async def list_voices(self) -> list[DiscoveredVoice]:
        if cached := self.cache.get(self.name):
            return cached
        voices: list[DiscoveredVoice] = []
        headers = {"xi-api-key": self.api_key} if self.api_key else None
        page_size = 100 if self.api_key else 3

        async def fetch_page(page: int):
            return await self.fetch_json(
                ELEVENLABS_CATALOG_URL,
                headers=headers,
                params={"page_size": page_size, "page": page},
            )

        first = await fetch_page(0)
        pages: list[Any] = [first]
        total_count = int(first.get("total_count") or 0)
        if first.get("has_more") and total_count:
            page_count = min(self.max_pages, math.ceil(total_count / page_size))
            semaphore = asyncio.Semaphore(8)

            async def limited_fetch(page: int):
                async with semaphore:
                    return await fetch_page(page)

            fetched = await asyncio.gather(
                *(limited_fetch(page) for page in range(1, page_count)),
                return_exceptions=True,
            )
            # One removed page or transient rate limit must not discard the
            # usable catalog pages already fetched from this provider.
            pages.extend(page for page in fetched if not isinstance(page, Exception))
        elif first.get("has_more"):
            # Test doubles and older responses may omit total_count.
            previous = first
            for page in range(1, self.max_pages):
                if not previous.get("has_more"):
                    break
                previous = await fetch_page(page)
                pages.append(previous)

        for payload in pages:
            for item in payload.get("voices", []):
                voice_id = _text(item.get("voice_id"))
                if not voice_id:
                    continue
                verified = item.get("verified_languages") or []
                languages = _unique(
                    [_text(item.get("language"))]
                    + [_text(entry.get("locale") or entry.get("language")) for entry in verified]
                )
                styles = _unique([_text(item.get("descriptive")), _text(item.get("use_case"))])
                voices.append(DiscoveredVoice(
                    provider=self.name,
                    voice_id=voice_id,
                    name=_text(item.get("name")) or voice_id,
                    description=_optional_text(item.get("description")),
                    languages=languages,
                    gender=_optional_text(item.get("gender")),
                    age=_optional_text(item.get("age")),
                    accent=_optional_text(item.get("accent")),
                    styles=styles,
                    tags=_unique([_text(item.get("category"))]),
                    preview_url=_optional_text(item.get("preview_url")),
                    source_url="https://elevenlabs.io/voice-library",
                    provider_metadata=dict(item),
                ))
        self.cache.put(self.name, voices)
        return voices


class PlayHTCatalog(VoiceCatalogProvider):
    name = "playht"

    def __init__(self, *, fetch_text: TextFetcher = _fetch_text,
                 cache: TTLVoiceCache | None = None) -> None:
        super().__init__(cache=cache)
        self.fetch_text = fetch_text

    async def list_voices(self) -> list[DiscoveredVoice]:
        if cached := self.cache.get(self.name):
            return cached
        text = await self.fetch_text(PLAYHT_CATALOG_URL)
        voices: list[DiscoveredVoice] = []
        for raw in csv.DictReader(io.StringIO(text)):
            row = {_normalize_header(key): _text(value) for key, value in raw.items() if key}
            voice_id = row.get("id") or row.get("s3_bucket") or row.get("voice") or ""
            if not voice_id:
                continue
            style_values = [row.get("style", "")]
            tags = [
                f"loudness:{row['loudness']}" if row.get("loudness") else "",
                f"tempo:{row['tempo']}" if row.get("tempo") else "",
                f"texture:{row['texture']}" if row.get("texture") else "",
            ]
            voices.append(DiscoveredVoice(
                provider=self.name,
                voice_id=voice_id,
                name=row.get("name") or voice_id,
                languages=_unique([row.get("language", ""), row.get("language_code", "")]),
                gender=_optional_text(row.get("gender")),
                age=_optional_text(row.get("age")),
                accent=_optional_text(row.get("accent")),
                styles=_unique(style_values),
                tags=_unique(tags),
                preview_url=_optional_text(row.get("sample")),
                source_url=PLAYHT_SOURCE_URL,
                provider_metadata=dict(row),
            ))
        self.cache.put(self.name, voices)
        return voices


class RimeCatalog(VoiceCatalogProvider):
    name = "rime"

    def __init__(self, *, fetch_json: JsonFetcher = _fetch_json,
                 cache: TTLVoiceCache | None = None) -> None:
        super().__init__(cache=cache)
        self.fetch_json = fetch_json

    async def list_voices(self) -> list[DiscoveredVoice]:
        if cached := self.cache.get(self.name):
            return cached
        payload = await self.fetch_json(RIME_CATALOG_URL)
        voices: list[DiscoveredVoice] = []
        for item in payload if isinstance(payload, list) else []:
            voice_id = _text(item.get("speaker"))
            if not voice_id:
                continue
            genres = item.get("genre") or []
            if isinstance(genres, str):
                genres = [genres]
            voices.append(DiscoveredVoice(
                provider=self.name,
                voice_id=voice_id,
                name=voice_id.replace("_", " ").title(),
                description=_optional_text(item.get("description")),
                languages=_unique([_text(item.get("language")), _text(item.get("lang"))]),
                gender=_optional_text(item.get("gender")),
                age=_optional_text(item.get("age")),
                accent=_optional_text(item.get("dialect")),
                country=_optional_text(item.get("country")),
                styles=_unique(
                    [_text(value) for value in genres]
                    + [_text(value) for value in (item.get("styles") or [])]
                ),
                tags=_unique([_text(item.get("demographic"))]),
                preview_requires_auth=True,
                source_url=RIME_SOURCE_URL,
                provider_metadata=dict(item),
            ))
        self.cache.put(self.name, voices)
        return voices


class HumeCatalog(VoiceCatalogProvider):
    """Authenticated Hume Voice Library catalog (not voice design).

    Hume's natural-language voice design is intentionally a separate product
    capability and is never mixed into catalog results by this adapter.
    """

    name = "hume"
    catalog_requires_auth = True
    supports_voice_design = True

    def __init__(self, api_key: str = "", *, fetch_json: JsonFetcher = _fetch_json,
                 cache: TTLVoiceCache | None = None, max_pages: int = 100) -> None:
        super().__init__(cache=cache)
        self.api_key = api_key
        self.fetch_json = fetch_json
        self.max_pages = max_pages

    async def list_voices(self) -> list[DiscoveredVoice]:
        if not self.api_key.strip():
            raise CatalogAuthenticationRequired("Hume requires a key to search its Voice Library")
        if cached := self.cache.get(self.name):
            return cached
        voices: list[DiscoveredVoice] = []
        for page in range(self.max_pages):
            payload = await self.fetch_json(
                HUME_CATALOG_URL,
                headers={"X-Hume-Api-Key": self.api_key},
                params={"provider": "HUME_AI", "page_number": page, "page_size": 100},
            )
            for item in payload.get("voices_page", []):
                voice_id = _text(item.get("id"))
                if not voice_id:
                    continue
                tags = item.get("tags") or []
                if isinstance(tags, dict):
                    tags = [f"{key}:{value}" for key, value in tags.items()]
                voices.append(DiscoveredVoice(
                    provider=self.name,
                    voice_id=voice_id,
                    name=_text(item.get("name")) or voice_id,
                    tags=_unique([_text(tag) for tag in tags]),
                    preview_requires_auth=True,
                    source_url=HUME_SOURCE_URL,
                    provider_metadata=dict(item),
                ))
            if page + 1 >= int(payload.get("total_pages") or 1):
                break
        self.cache.put(self.name, voices)
        return voices


class CartesiaCatalog(VoiceCatalogProvider):
    """Authenticated compatibility adapter for existing Cartesia users."""

    name = "cartesia"
    catalog_requires_auth = True

    def __init__(self, api_key: str = "", *, fetch_json: JsonFetcher = _fetch_json,
                 cache: TTLVoiceCache | None = None, max_pages: int = 20) -> None:
        super().__init__(cache=cache)
        self.api_key = api_key
        self.fetch_json = fetch_json
        self.max_pages = max_pages

    async def list_voices(self) -> list[DiscoveredVoice]:
        if not self.api_key.strip():
            raise CatalogAuthenticationRequired("Cartesia requires a key to search its catalog")
        if cached := self.cache.get(self.name):
            return cached
        params: dict[str, Any] = {"limit": 100}
        voices: list[DiscoveredVoice] = []
        for _page in range(self.max_pages):
            payload = await self.fetch_json(
                CARTESIA_CATALOG_URL,
                headers={
                    "Cartesia-Version": "2026-03-01",
                    "Authorization": f"Bearer {self.api_key}",
                },
                params=params,
            )
            data = payload.get("data", payload if isinstance(payload, list) else [])
            for item in data:
                voice_id = _text(item.get("id"))
                if voice_id:
                    voices.append(DiscoveredVoice(
                        provider=self.name,
                        voice_id=voice_id,
                        name=_text(item.get("name")) or voice_id,
                        description=_optional_text(item.get("description")),
                        languages=_unique([_text(item.get("language"))]),
                        preview_requires_auth=True,
                        source_url="https://play.cartesia.ai",
                        provider_metadata=dict(item),
                    ))
            cursor = payload.get("next_page") if isinstance(payload, dict) else None
            if not cursor:
                break
            params["starting_after"] = cursor
        self.cache.put(self.name, voices)
        return voices


class DeepgramCatalog(VoiceCatalogProvider):
    """Deepgram voices: Flux (English) and Aura-2 (seven languages).

    Without a key the documented seed list serves; with one, GET /v1/models
    supersedes it so new Aura-2 languages appear without a code change.
    """

    name = "deepgram"
    catalog_requires_auth = False

    def __init__(self, api_key: str = "") -> None:
        self.api_key = api_key

    async def list_voices(self) -> list[DiscoveredVoice]:
        from sleeptalk.voice_list import fetch_deepgram_voices

        return [
            DiscoveredVoice(
                provider=self.name,
                voice_id=voice.id,
                name=voice.name,
                description=voice.description,
                languages=[voice.language or "en"],
                accent=voice.description.replace(" English", "").replace(", Aura-2", ""),
                preview_requires_auth=True,
                source_url=(
                    "https://developers.deepgram.com/docs/flux-tts/voices"
                    if voice.id.startswith("flux-")
                    else "https://developers.deepgram.com/docs/tts-models"
                ),
            )
            for voice in await fetch_deepgram_voices(self.api_key)
        ]


class VoiceDiscoveryService:
    """Search providers concurrently while isolating every provider failure."""

    def __init__(self, providers: Iterable[VoiceCatalogProvider] | None = None) -> None:
        self.providers = list(providers or default_catalogs())
        self._by_name = {provider.name: provider for provider in self.providers}

    def provider(self, name: str) -> VoiceCatalogProvider:
        return self._by_name[name.strip().lower()]

    async def search(self, query: str, *, limit: int = 30) -> DiscoveryReport:
        results = await asyncio.gather(
            *(provider.list_voices() for provider in self.providers),
            return_exceptions=True,
        )
        voices: list[DiscoveredVoice] = []
        issues: list[ProviderIssue] = []
        for provider, result in zip(self.providers, results):
            if isinstance(result, CatalogAuthenticationRequired):
                issues.append(ProviderIssue(provider.name, str(result), requires_auth=True))
            elif isinstance(result, Exception):
                issues.append(ProviderIssue(provider.name, str(result)))
            else:
                voices.extend(result)
                if isinstance(provider, ElevenLabsCatalog) and not provider.api_key:
                    issues.append(ProviderIssue(
                        provider.name,
                        "first public page searched; ElevenLabs currently requires a key "
                        "to paginate or filter additional shared voices",
                        requires_auth=True,
                    ))
        voices = _deduplicate_voices(voices)
        matches = rank_voices(voices, query)
        if query.strip() and any(match.score > 0 for match in matches):
            matches = [match for match in matches if match.score > 0]
        return DiscoveryReport(query=query, matches=matches[:limit], issues=issues)


def default_catalogs(credentials: dict[str, str] | None = None) -> list[VoiceCatalogProvider]:
    credentials = credentials or {}
    return [
        ElevenLabsCatalog(credentials.get("elevenlabs", "")),
        PlayHTCatalog(),
        RimeCatalog(),
        HumeCatalog(credentials.get("hume", "")),
        CartesiaCatalog(credentials.get("cartesia", "")),
        DeepgramCatalog(credentials.get("deepgram", "")),
    ]


def _primary_tag(language: str) -> str:
    return (language or "").strip().lower().replace("_", "-").split("-", 1)[0]


def voices_for_language(voices: Iterable[DiscoveredVoice], language: str) -> list[DiscoveredVoice]:
    """Voices whose declared languages include this one, by primary subtag.

    ``rank_voices`` is a free-text search and tokenises ``"fr-FR"`` as one
    token, so a BCP-47 tag never constrained it. This is the deterministic
    filter the wizard's auto-pick uses before ranking. Voices that declare
    no language at all are kept: multilingual models (ElevenLabs) leave the
    field empty and may still cover the language.
    """
    wanted = _primary_tag(language)
    if not wanted:
        return list(voices)
    matched: list[DiscoveredVoice] = []
    untagged: list[DiscoveredVoice] = []
    for voice in voices:
        tags = {_primary_tag(tag) for tag in voice.languages if str(tag).strip()}
        if not tags:
            untagged.append(voice)
        elif wanted in tags:
            matched.append(voice)
    return matched or untagged


def _deduplicate_voices(voices: Iterable[DiscoveredVoice]) -> list[DiscoveredVoice]:
    """Keep the richest record when a catalog repeats the same provider ID."""

    selected: dict[tuple[str, str], DiscoveredVoice] = {}
    order: list[tuple[str, str]] = []
    for voice in voices:
        key = (voice.provider.casefold(), voice.voice_id)
        existing = selected.get(key)
        if existing is None:
            order.append(key)
        if existing is None or _voice_richness(voice) > _voice_richness(existing):
            selected[key] = voice
    return [selected[key] for key in order]


def _voice_richness(voice: DiscoveredVoice) -> tuple[int, int]:
    facts = voice.facts()
    descriptive_length = len(voice.description or "") + sum(map(len, facts))
    return len(facts), descriptive_length


@dataclass(frozen=True, slots=True)
class _Constraints:
    genders: frozenset[str] = frozenset()
    ages: frozenset[str] = frozenset()
    regions: frozenset[str] = frozenset()
    languages: frozenset[str] = frozenset()


_GENDER_TERMS = {
    "woman": "female", "women": "female", "female": "female", "feminine": "female",
    "man": "male", "men": "male", "male": "male", "masculine": "male",
    "nonbinary": "nonbinary", "non-binary": "nonbinary", "androgynous": "nonbinary",
}
_AGE_TERMS = {
    "old": "old", "older": "old", "elderly": "old", "senior": "old", "mature": "old",
    "young": "young", "youth": "young", "teen": "young", "child": "child",
    "adult": "adult", "middle-aged": "middle", "middle": "middle",
}
_REGION_GROUPS = {
    "indian": {"indian", "india", "in", "en-in"},
    "british": {"british", "uk", "gb", "en-gb", "england", "english"},
    "american": {"american", "us", "usa", "en-us"},
    "australian": {"australian", "australia", "au", "en-au"},
    "canadian": {"canadian", "canada", "ca", "en-ca"},
    "irish": {"irish", "ireland", "ie", "en-ie"},
    "scottish": {"scottish", "scotland", "scots"},
}
_LANGUAGE_GROUPS = {
    "english": {"english", "en", "eng"}, "spanish": {"spanish", "es", "spa"},
    "hindi": {"hindi", "hi", "hin"}, "french": {"french", "fr", "fra"},
    "german": {"german", "de", "deu", "ger"}, "japanese": {"japanese", "ja", "jpn"},
    "portuguese": {"portuguese", "pt", "por"}, "arabic": {"arabic", "ar", "ara"},
    "italian": {"italian", "it", "ita"}, "dutch": {"dutch", "nl", "nld", "dut"},
    "polish": {"polish", "pl", "pol"}, "urdu": {"urdu", "ur", "urd"},
    "korean": {"korean", "ko", "kor"}, "chinese": {"chinese", "mandarin", "zh", "zho", "chi"},
    "russian": {"russian", "ru", "rus"}, "turkish": {"turkish", "tr", "tur"},
}
_SEMANTIC_GROUPS = (
    {"loud", "energetic", "powerful", "high", "bold", "excited", "advertising"},
    {"calm", "reassuring", "soothing", "warm", "gentle", "meditation", "low", "slow"},
    {"funny", "cute", "adorable", "comedic", "humorous", "playful", "quirky"},
    {"anime", "animated", "animation", "character", "characters", "gaming", "expressive"},
    {"conversational", "conversation", "natural", "casual", "agent"},
    {"narrative", "narrator", "story", "storytelling", "audiobook"},
    {"cute", "adorable", "high-pitched", "highpitched", "bright", "youthful"},
    {"gravelly", "rough", "raspy", "thick"},
)
_STOPWORDS = {
    "a", "an", "and", "the", "voice", "voices", "some", "something", "few", "me",
    "show", "please", "with", "that", "sounds", "sound", "like", "delivery", "want",
}


def rank_voices(voices: Iterable[DiscoveredVoice], query: str) -> list[VoiceMatch]:
    query_tokens = _tokens(query)
    constraints = _parse_constraints(query_tokens)
    expanded_query = _expand_semantics(query_tokens)
    matches: list[VoiceMatch] = []
    examples_query = not (query_tokens - _STOPWORDS) or "example" in query.lower()
    for voice in voices:
        factual, conflict, missing_count = _factual_fit(voice, constraints)
        if conflict:
            continue
        document = _voice_document(voice)
        doc_tokens = _tokens(document)
        expanded_doc = _expand_semantics(doc_tokens)
        exact = (query_tokens - _STOPWORDS) & doc_tokens
        semantic = (expanded_query - query_tokens) & doc_tokens
        reverse_semantic = (expanded_doc - doc_tokens) & query_tokens
        score = float(len(exact) * 2 + len(semantic | reverse_semantic) + len(factual) * 5)
        score -= missing_count * 0.4
        if examples_query:
            score = max(score, 0.1)
        inferred = tuple(sorted(semantic | reverse_semantic))
        matches.append(VoiceMatch(voice, score, tuple(factual), inferred))
    ranked = sorted(
        matches,
        key=lambda match: (-match.score, match.voice.provider, match.voice.name.lower()),
    )
    if query.strip() and not examples_query:
        return [match for match in ranked if match.credible]
    return ranked


def _parse_constraints(tokens: set[str]) -> _Constraints:
    genders = {_GENDER_TERMS[token] for token in tokens if token in _GENDER_TERMS}
    ages = {_AGE_TERMS[token] for token in tokens if token in _AGE_TERMS}
    regions = {name for name, aliases in _REGION_GROUPS.items() if tokens & aliases}
    languages = {name for name, aliases in _LANGUAGE_GROUPS.items() if tokens & aliases}
    return _Constraints(frozenset(genders), frozenset(ages), frozenset(regions), frozenset(languages))


def _factual_fit(voice: DiscoveredVoice, wanted: _Constraints) -> tuple[list[str], bool, int]:
    matched: list[str] = []
    missing = 0
    checks = (
        (wanted.genders, [_canonical_gender(voice.gender)], "gender"),
        (wanted.ages, _age_categories(voice.age), "age"),
        (wanted.regions, _voice_regions(voice), "accent/country"),
        (wanted.languages, _voice_languages(voice), "language"),
    )
    for requested, actual, label in checks:
        if not requested:
            continue
        actual_set = {value for value in actual if value}
        if not actual_set:
            missing += 1
        elif requested & actual_set:
            matched.append(f"{label}: {', '.join(sorted(requested & actual_set))}")
        else:
            return matched, True, missing
    return matched, False, missing


def _voice_regions(voice: DiscoveredVoice) -> list[str]:
    tokens = _tokens(" ".join(value for value in (voice.accent, voice.country) if value))
    values = []
    for name, aliases in _REGION_GROUPS.items():
        if tokens & aliases:
            values.append(name)
    return values


def _voice_languages(voice: DiscoveredVoice) -> list[str]:
    tokens = _tokens(" ".join(voice.languages))
    values = []
    for name, aliases in _LANGUAGE_GROUPS.items():
        if tokens & aliases:
            values.append(name)
    return values


def _canonical_gender(value: str | None) -> str:
    tokens = _tokens(value or "")
    for token in tokens:
        if token in _GENDER_TERMS:
            return _GENDER_TERMS[token]
    return ""


def _age_categories(value: str | None) -> list[str]:
    tokens = _tokens(value or "")
    values = {_AGE_TERMS[token] for token in tokens if token in _AGE_TERMS}
    if "young" in values and "adult" in values:
        values.add("young")
    if "middle" in values:
        values.update({"adult", "old"})
    return list(values)


def _voice_document(voice: DiscoveredVoice) -> str:
    return " ".join(
        [voice.name, voice.description or "", *voice.languages, voice.gender or "", voice.age or "",
         voice.accent or "", voice.country or "", *voice.styles, *voice.tags]
    ).lower().replace("_", " ")


def _expand_semantics(tokens: set[str]) -> set[str]:
    expanded = set(tokens)
    for group in _SEMANTIC_GROUPS:
        if tokens & group:
            expanded.update(group)
    return expanded


def _tokens(value: str) -> set[str]:
    normalized = value.lower().replace("_", " ")
    return set(re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)?", normalized))


def _normalize_header(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")


def _text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() in {"null", "none", "nan"} else text


def _optional_text(value: Any) -> str | None:
    return _text(value) or None


def _unique(values: Iterable[str]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        clean = _text(value)
        key = clean.casefold()
        if clean and key not in seen:
            output.append(clean)
            seen.add(key)
    return output


_ORDINALS = {"one": 1, "first": 1, "two": 2, "second": 2, "three": 3, "third": 3}


class VoiceDiscoverySession:
    """Pure navigation/confirmation state used by terminal or spoken shells."""

    def __init__(self, matches: Iterable[VoiceMatch] = (), *, page_size: int = 3) -> None:
        self.matches = list(matches)
        self.page_size = max(1, page_size)
        self.page = 0
        self.pending: VoiceMatch | None = None
        self.selected: DiscoveredVoice | None = None

    def refine(self, matches: Iterable[VoiceMatch]) -> None:
        self.matches = list(matches)
        self.page = 0
        self.pending = None

    def current(self) -> list[VoiceMatch]:
        start = self.page * self.page_size
        return self.matches[start : start + self.page_size]

    def next(self) -> list[VoiceMatch]:
        if (self.page + 1) * self.page_size < len(self.matches):
            self.page += 1
        return self.current()

    def back(self) -> list[VoiceMatch]:
        self.page = max(0, self.page - 1)
        self.pending = None
        return self.current()

    def choose(self, value: str | int) -> VoiceMatch | None:
        if isinstance(value, str):
            clean = " ".join(re.findall(r"[a-z0-9]+", value.lower()))
            selected = re.search(
                r"\b(one|first|two|second|three|third|[1-9])\b",
                clean,
            )
            token = selected.group(1) if selected else ""
            number = _ORDINALS.get(token)
            if number is None and token.isdigit():
                number = int(token)
        else:
            number = value
        current = self.current()
        if not number or not 1 <= number <= len(current):
            return None
        self.pending = current[number - 1]
        return self.pending

    def confirm(self, answer: str) -> DiscoveredVoice | None:
        if answer.strip().lower() in {"yes", "y", "use it", "confirm", "okay", "ok"} and self.pending:
            self.selected = self.pending.voice
        return self.selected
