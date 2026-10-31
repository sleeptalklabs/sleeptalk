"""Voice catalog normalization, ranking, resilience, and navigation."""

from __future__ import annotations

import tomllib

import pytest
from sleeptalk import wizard
from sleeptalk.voice_discovery import (
    CatalogAuthenticationRequired,
    DiscoveredVoice,
    ElevenLabsCatalog,
    HumeCatalog,
    PlayHTCatalog,
    PreviewResult,
    RimeCatalog,
    TTLVoiceCache,
    VoiceCatalogProvider,
    VoiceDiscoveryService,
    VoiceDiscoverySession,
    rank_voices,
)


@pytest.mark.asyncio
async def test_elevenlabs_public_catalog_paginates_and_normalizes():
    calls = []

    async def fetch(_url, **kwargs):
        page = kwargs["params"]["page"]
        calls.append(page)
        return {
            "voices": [{
                "voice_id": f"v-{page}",
                "name": f"Voice {page}",
                "gender": "Female",
                "age": "old",
                "accent": "indian",
                "language": "en",
                "descriptive": "warm",
                "use_case": "conversational",
                "preview_url": f"https://audio.example/v-{page}.mp3",
                "description": "A provider description",
            }],
            "has_more": page == 0,
        }

    voices = await ElevenLabsCatalog(fetch_json=fetch, max_pages=2).list_voices()
    assert calls == [0, 1]
    assert [voice.voice_id for voice in voices] == ["v-0", "v-1"]
    assert voices[0].preview_url == "https://audio.example/v-0.mp3"
    assert voices[0].gender == "Female"


@pytest.mark.asyncio
async def test_playht_official_sheet_normalization_preserves_real_id_and_sample():
    csv_text = """name,accent,sample,age,S3 bucket,gender,language,language_code,loudness,style,tempo,texture
Navya,indian,https://samples.example/navya.wav,old,s3://catalog/navya.json,female,English (IN),en-IN,high,conversational,fast,smooth
"""

    async def fetch(_url, **_kwargs):
        return csv_text

    voice = (await PlayHTCatalog(fetch_text=fetch).list_voices())[0]
    assert voice.voice_id == "s3://catalog/navya.json"
    assert voice.preview_url == "https://samples.example/navya.wav"
    assert voice.languages == ["English (IN)", "en-IN"]
    assert "loudness:high" in voice.tags


@pytest.mark.asyncio
async def test_rime_normalizes_missing_metadata_without_invention():
    async def fetch(_url, **_kwargs):
        return [{
            "speaker": "cove",
            "gender": "Male",
            "age": None,
            "country": "US",
            "dialect": "Generic",
            "genre": ["Conversational"],
            "modelId": "mistv3",
            "language": "English",
            "lang": "eng",
        }]

    voice = (await RimeCatalog(fetch_json=fetch).list_voices())[0]
    assert voice.voice_id == "cove"
    assert voice.age is None
    assert voice.preview_url is None and voice.preview_requires_auth
    assert voice.provider_metadata["modelId"] == "mistv3"


def _voice(**overrides):
    values = dict(provider="test", voice_id="id", name="Voice")
    values.update(overrides)
    return DiscoveredVoice(**values)


def test_ranking_applies_factual_constraints_before_semantic_fit():
    voices = [
        _voice(voice_id="match", name="Priya", gender="female", age="old", accent="indian",
               styles=["conversational"], tags=["loudness:high"]),
        _voice(voice_id="wrong-gender", name="Raj", gender="male", age="old", accent="indian"),
        _voice(voice_id="wrong-region", name="Alice", gender="female", age="old", accent="american"),
    ]
    matches = rank_voices(voices, "an older Indian woman")
    assert [match.voice.voice_id for match in matches] == ["match"]
    assert set(matches[0].factual_matches) == {
        "gender: female", "age: old", "accent/country: indian"
    }


def test_loud_man_uses_metadata_semantics_without_claiming_an_inferred_fact():
    loud = _voice(voice_id="loud", gender="male", tags=["loudness:high"])
    quiet = _voice(voice_id="quiet", gender="male", tags=["loudness:low"])
    matches = rank_voices([quiet, loud], "a loud man")
    assert matches[0].voice.voice_id == "loud"
    assert matches[0].factual_matches == ("gender: male",)
    assert "high" in matches[0].inferred_matches


def test_cute_voice_expands_to_provider_supplied_playful_and_youthful_metadata():
    playful = _voice(voice_id="playful", styles=["playful", "bright"])
    serious = _voice(voice_id="serious", styles=["formal", "authoritative"])

    matches = rank_voices([serious, playful], "cute voice")

    assert [match.voice.voice_id for match in matches] == ["playful"]
    assert set(matches[0].inferred_matches) >= {"bright", "playful"}


def test_no_credible_match_does_not_return_unrelated_default():
    voices = [_voice(gender="male", age="young", accent="american")]
    assert rank_voices(voices, "older Indian woman") == []


def test_missing_metadata_is_allowed_but_ranked_below_supplied_matching_facts():
    known = _voice(voice_id="known", gender="female", age="old")
    missing = _voice(voice_id="missing", name="older woman")
    matches = rank_voices([missing, known], "older woman")
    assert matches[0].voice.voice_id == "known"


class _StaticProvider(VoiceCatalogProvider):
    def __init__(self, name, result):
        super().__init__()
        self.name = name
        self.result = result

    async def list_voices(self):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


@pytest.mark.asyncio
async def test_provider_failure_and_auth_gate_are_isolated():
    good = _StaticProvider("good", [_voice(provider="good", description="calm")])
    failed = _StaticProvider("failed", RuntimeError("rate limited"))
    gated = _StaticProvider("gated", CatalogAuthenticationRequired("key required"))
    report = await VoiceDiscoveryService([failed, good, gated]).search("calm")
    assert [match.voice.provider for match in report.matches] == ["good"]
    assert {(issue.provider, issue.requires_auth) for issue in report.issues} == {
        ("failed", False), ("gated", True)
    }


@pytest.mark.asyncio
async def test_service_deduplicates_repeated_provider_voice_ids_using_richest_metadata():
    sparse = _voice(provider="rime", voice_id="astra", name="Astra")
    rich = _voice(
        provider="rime",
        voice_id="astra",
        name="Astra",
        gender="female",
        age="young adult",
        styles=["playful"],
    )
    provider = _StaticProvider("rime", [sparse, rich])

    report = await VoiceDiscoveryService([provider]).search("cute woman")

    assert len(report.matches) == 1
    assert report.matches[0].voice is rich


@pytest.mark.asyncio
async def test_hume_catalog_is_credential_gated_before_network_call():
    called = False

    async def fetch(*_args, **_kwargs):
        nonlocal called
        called = True

    with pytest.raises(CatalogAuthenticationRequired):
        await HumeCatalog(fetch_json=fetch).list_voices()
    assert called is False


@pytest.mark.asyncio
async def test_preview_contract_distinguishes_public_and_authenticated():
    provider = _StaticProvider("test", [])
    public = await provider.get_preview(_voice(preview_url="https://audio.example/a.mp3"))
    gated = await provider.get_preview(_voice(provider="rime", preview_requires_auth=True))
    assert public == PreviewResult("https://audio.example/a.mp3")
    assert gated.requires_auth and gated.url is None


@pytest.mark.asyncio
async def test_normalized_catalog_cache_has_ttl():
    now = 100.0
    import sleeptalk.voice_discovery as discovery

    original = discovery.time.monotonic
    discovery.time.monotonic = lambda: now
    try:
        cache = TTLVoiceCache(ttl_seconds=10)
        cache.put("x", [_voice()])
        assert cache.get("x")
        now = 111.0
        assert cache.get("x") is None
    finally:
        discovery.time.monotonic = original


def test_navigation_next_back_refine_and_spoken_confirmation():
    matches = rank_voices(
        [_voice(voice_id=str(index), name=f"Voice {index}") for index in range(7)],
        "show me examples",
    )
    session = VoiceDiscoverySession(matches)
    assert [m.voice.voice_id for m in session.current()] == ["0", "1", "2"]
    assert [m.voice.voice_id for m in session.next()] == ["3", "4", "5"]
    assert [m.voice.voice_id for m in session.back()] == ["0", "1", "2"]
    pending = session.choose("two")
    assert pending and pending.voice.voice_id == "1"
    assert session.confirm("yes").voice_id == "1"
    assert session.choose("I want number three").voice.voice_id == "2"
    session.refine(matches[-2:])
    assert session.page == 0 and session.pending is None


def test_selection_persists_provider_model_and_exact_voice_id():
    answers = wizard.WizardAnswers(
        tts_provider="rime",
        tts_model="mistv3",
        tts_voice_id="cove",
        language="en-US",
    )
    parsed = tomllib.loads(wizard.profile_toml(answers))
    assert parsed["tts"] == {
        "provider": "rime",
        "model": "mistv3",
        "voice_id": "cove",
        "language": "en-US",
        "speed": answers.tts_speed,
    }


def test_playht_credentials_use_secure_env_path_only():
    answers = wizard.WizardAnswers(
        tts_provider="playht",
        tts_voice_id="s3://catalog/voice.json",
        tts_key="secret-key",
        tts_user_id="secret-user",
    )
    assert wizard.env_entries(answers)["PLAYHT_API_KEY"] == "secret-key"
    assert wizard.env_entries(answers)["PLAYHT_USER_ID"] == "secret-user"
    profile = wizard.profile_toml(answers)
    assert "secret-key" not in profile and "secret-user" not in profile
