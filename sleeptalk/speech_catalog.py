"""Every speech model Sleeptalk can drive, and the one it recommends.

One catalog per provider and role (``stt`` listens, ``tts`` speaks). Each
entry says what a user needs to choose it: the languages it covers, whether
it detects the language itself (and so should get no language hint), how our
call path drives it (streamed or one turn at a time), a normalized price per
hour where the provider publishes one, and the voices that belong to it.

The curated layer below works offline and is the authority on
recommendations, language coverage, and whether our adapters can drive a
model. Providers that publish their list are fetched live on request
(``refresh``) and cached for a day under the private Sleeptalk home; a live
entry adds models and voices the curated layer doesn't know, never
overrides a curated fact. Nothing here reads a key unless the caller passes
one, and a failed fetch falls back to the cache, then to the curated layer.

``recommended(provider, role, language)`` is the single place defaults come
from. English picks are the launch defaults; other languages move to a model
that covers them only when the default does not.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable

from sleeptalk.speech.local_models import KOKORO_EN_VOICES

STT = "stt"
TTS = "tts"
STREAMING = "streaming"   # our adapter streams audio in or out
BATCH = "batch"           # one utterance or one sentence per request
LOCAL = "local"           # runs on this machine

CACHE_TTL_SECONDS = 24 * 60 * 60
FETCH_TIMEOUT_SECONDS = 8.0
# Speech runs at about 15 characters a second, so a per-character TTS price
# becomes a per-hour one at 54,000 characters an hour.
CHARACTERS_PER_HOUR_OF_SPEECH = 15 * 3600


@dataclass(frozen=True)
class ModelOption:
    provider: str
    role: str
    id: str
    label: str = ""
    # Primary language subtags ("zh", "en"). None: not documented.
    languages: frozenset[str] | None = None
    # The model identifies the language itself and handles mixed-language
    # speech; the adapter sends no language hint for it.
    detects_language: bool = False
    transport: str = BATCH
    usd_per_hour: float | None = None
    # Voices that belong to this model. None: any provider voice ID works.
    voices: tuple[str, ...] | None = None
    default_voice: str = ""
    # Deepgram: the profile's tts.model is the voice itself.
    model_is_voice: bool = False
    # Languages whose hint makes the model worse (Whisper told "zh" writes
    # English words in Chinese): for these the adapter sends no hint.
    no_hint_for: frozenset[str] = frozenset()
    # False when our call path cannot drive it (a format we can't decode).
    usable: bool = True
    notes: str = ""
    # Venice labels every model: "private" (zero retention, contract-enforced)
    # or "anonymized" (passed to its maker without your identity; the maker
    # sees the content). Empty for services that don't say.
    privacy: str = ""
    # The response format our call path asks this model for; empty: the
    # service's default. Venice models each accept only some formats.
    audio_format: str = ""
    source: str = "curated"

    def covers(self, language: str) -> bool | None:
        """True or False when documented; None when the provider doesn't say."""
        if self.languages is None:
            return None
        return primary_tag(language) in self.languages

    def owns_voice(self, voice_id: str) -> bool | None:
        if self.voices is None:
            return None
        return voice_id in self.voices


def primary_tag(language: str) -> str:
    return (language or "").strip().lower().replace("_", "-").split("-", 1)[0]


# ---------------------------------------------------------------------------
# Language coverage the providers document (shared with language_support).
# ---------------------------------------------------------------------------

from sleeptalk.language_support import (  # noqa: E402  (plain data module)
    _CARTESIA_INK2,
    _CARTESIA_INK_WHISPER,
    _CARTESIA_SONIC2,
    _CARTESIA_SONIC3,
    _CARTESIA_SONIC36,
    _DEEPGRAM_AURA2,
    _DEEPGRAM_NOVA2,
    _DEEPGRAM_NOVA3,
    _ELEVENLABS_MULTILINGUAL_V2,
    _ELEVENLABS_SCRIBE_V2,
)

# NVIDIA Parakeet TDT 0.6B v3: the 25 European languages it was trained on.
_PARAKEET_V3 = frozenset({
    "bg", "hr", "cs", "da", "nl", "en", "et", "fi", "fr", "de", "el", "hu", "it",
    "lv", "lt", "mt", "pl", "pt", "ro", "sk", "sl", "es", "sv", "ru", "uk",
})
# Whisper large-v3: the widely used subset of its 99 languages.
_WHISPER = frozenset({
    "en", "zh", "de", "es", "ru", "ko", "fr", "ja", "pt", "tr", "pl", "ca", "nl",
    "ar", "sv", "it", "id", "hi", "fi", "vi", "he", "uk", "el", "ms", "cs", "ro",
    "da", "hu", "ta", "no", "th", "ur", "hr", "bg", "lt", "la", "cy", "sk", "fa",
    "lv", "bn", "sr", "sl", "et", "sw", "tl", "yue",
})
# Qwen3-ASR Flash: Mandarin and Chinese dialects plus English, with language
# identification for mixed Chinese and English. Only these are relied on.
_QWEN3_ASR = frozenset({"zh", "yue", "en"})
_ENGLISH = frozenset({"en"})
_F = frozenset

# Grok Voice on OpenRouter and Venice.
_GROK_VOICES = ("eve", "ara", "rex", "sal", "leo")
_VENICE_GROK_VOICES = (
    "altair", "ara", "atlas", "carina", "castor", "celeste", "cosmo", "eve", "helios",
    "helix", "iris", "kepler", "leo", "lumen", "luna", "lux", "naksh", "orion", "perseus",
    "rex", "rigel", "sal", "sirius", "ursa", "zagan", "zenith",
)
_VENICE_KOKORO_VOICES = (
    "af_alloy", "af_aoede", "af_bella", "af_heart", "af_jadzia", "af_jessica", "af_kore", "af_nicole",
    "af_nova", "af_river", "af_sarah", "af_sky", "am_adam", "am_echo", "am_eric", "am_fenrir", "am_liam",
    "am_michael", "am_onyx", "am_puck", "am_santa", "bf_alice", "bf_emma", "bf_lily", "bm_daniel",
    "bm_fable", "bm_george", "bm_lewis", "ef_dora", "em_alex", "em_santa", "ff_siwis", "hf_alpha",
    "hf_beta", "hm_omega", "hm_psi", "if_sara", "im_nicola", "jf_alpha", "jf_gongitsune", "jf_nezumi",
    "jf_tebukuro", "jm_kumo", "pf_dora", "pm_alex", "pm_santa", "zf_xiaobei", "zf_xiaoni", "zf_xiaoxiao",
    "zf_xiaoyi", "zm_yunjian", "zm_yunxi", "zm_yunxia", "zm_yunyang",
)
_VENICE_GRADIUM_VOICES = ("Alice", "Davi", "Elise", "Emma", "Eva", "Jack", "Kent", "Leo", "Maximilian",
                          "Mia", "Sergio", "Valentina")
_VENICE_INWORLD_VOICES = ("Alex", "Ashley", "Craig", "Edward", "Elizabeth", "Hades", "Luna", "Mark",
                          "Olivia", "Pixie", "Priya", "Ronald", "Sarah", "Theodore")
_VENICE_ELEVENLABS_VOICES = (
    "Alice", "Aria", "Bill", "Brian", "Callum", "Charlie", "Charlotte", "Chris", "Daniel", "Eric",
    "George", "Jessica", "Laura", "Liam", "Lily", "Matilda", "Rachel", "River", "Roger", "Sarah", "Will",
)
_VENICE_GEMINI_VOICES = (
    "Achernar", "Achird", "Algenib", "Algieba", "Alnilam", "Aoede", "Autonoe", "Callirrhoe", "Charon",
    "Despina", "Enceladus", "Erinome", "Fenrir", "Gacrux", "Iapetus", "Kore", "Laomedeia", "Leda", "Orus",
    "Puck", "Pulcherrima", "Rasalgethi", "Sadachbia", "Sadaltager", "Schedar", "Sulafat", "Umbriel",
    "Vindemiatrix", "Zephyr", "Zubenelgenubi",
)
_VENICE_MINIMAX_VOICES = (
    "CalmWoman", "CasualGuy", "DeepVoiceMan", "DeterminedMan", "ElegantMan", "ExuberantGirl",
    "FriendlyPerson", "ImposingManner", "InspirationalGirl", "LivelyGirl", "LovelyGirl", "PatientMan",
    "SweetGirl", "WiseWoman", "YoungKnight",
)
_VENICE_QWEN3_VOICES = ("Aiden", "Dylan", "Eric", "Ono_Anna", "Ryan", "Serena", "Sohee", "Uncle_Fu", "Vivian")
_VENICE_CHATTERBOX_VOICES = ("Aurora", "Blade", "Britney", "Carl", "Cliff", "Richard", "Rico", "Siobhan", "Vicky")
# Qwen3-TTS: the ten languages Qwen documents.
_QWEN3_TTS = frozenset({"zh", "en", "ja", "ko", "de", "fr", "ru", "pt", "es", "it"})
_ORPHEUS_VOICES = ("tara", "leah", "jess", "leo", "dan", "mia", "zac")
_TOGETHER_ORPHEUS_VOICES = ("dan", "jess", "leah", "leo", "mia", "tara", "zac", "zoe")
_FLUX_VOICES = (
    "flux-hannah-en", "flux-kit-en", "flux-alexis-en", "flux-cliff-en", "flux-sienna-en",
    "flux-bree-en", "flux-brittany-en", "flux-brooke-en",
)
_QWEN_TTS_FLASH_VOICES = ("loongjohn", "longanhuan_v3.6")
# ElevenLabs' premade voices, as OpenRouter names them for every ElevenLabs model.
_OPENROUTER_ELEVENLABS_VOICES = (
    "jessica", "eric", "sarah", "george", "adam", "alice", "bella", "bill", "brian", "callum", "charlie",
    "chris", "daniel", "harry", "laura", "liam", "lily", "matilda", "river", "roger", "will",
)
# ElevenLabs Flash and Turbo v2.5: Multilingual v2's 29 plus Hungarian, Norwegian, and Vietnamese.
_ELEVENLABS_V2_5 = _F(_ELEVENLABS_MULTILINGUAL_V2 | {"hu", "no", "vi"})


def _stt(provider: str, model: str, label: str, languages, transport: str = BATCH, **extra) -> ModelOption:
    return ModelOption(provider, STT, model, label, _F(languages) if languages is not None else None,
                       transport=transport, **extra)


def _tts(provider: str, model: str, label: str, languages, transport: str = BATCH, **extra) -> ModelOption:
    return ModelOption(provider, TTS, model, label, _F(languages) if languages is not None else None,
                       transport=transport, **extra)


# Order is the fallback order: when the recommended model does not cover a
# language, the first curated model that does is picked.
_CURATED: dict[tuple[str, str], tuple[ModelOption, ...]] = {
    ("deepgram", STT): (
        _stt("deepgram", "nova-3", "Nova-3", _DEEPGRAM_NOVA3, STREAMING,
             notes='Deepgram\'s most accurate model; language "multi" code-switches.'),
        _stt("deepgram", "nova-2", "Nova-2", _DEEPGRAM_NOVA2 | _DEEPGRAM_NOVA3, STREAMING,
             notes="30+ languages, lower accuracy."),
    ),
    ("cartesia", STT): (
        _stt("cartesia", "ink-2", "Ink 2", _CARTESIA_INK2, STREAMING, detects_language=True,
             notes="Cartesia's fastest; English, Spanish, French, Hindi, and Japanese, detected."),
        _stt("cartesia", "ink-preview", "Ink Preview", _CARTESIA_INK2, STREAMING, detects_language=True,
             notes="Cartesia's preview of the next Ink; can change without notice."),
        _stt("cartesia", "ink-whisper", "Ink Whisper", _CARTESIA_INK_WHISPER, STREAMING,
             notes="40+ languages; accuracy varies."),
    ),
    ("elevenlabs", STT): (
        _stt("elevenlabs", "scribe_v2", "Scribe v2", _ELEVENLABS_SCRIBE_V2, STREAMING,
             notes="Live transcripts via Scribe v2 Realtime; Scribe v2 batch recovery."),
    ),
    ("local", STT): (
        _stt("local", "moonshine-tiny-en", "Moonshine tiny", _ENGLISH, LOCAL, usd_per_hour=0.0,
             notes="English, on this machine."),
        _stt("local", "moonshine-small-streaming-en", "Moonshine Small Streaming",
             _ENGLISH, LOCAL, usd_per_hour=0.0,
             notes="Live English transcription on CPU. 143 MB download."),
    ),
    # Measured 2026-09-24 on mixed Chinese and English coding speech (TTS to
    # STT round trips): Parakeet writes romanized nonsense for Chinese;
    # MAI-Transcribe-2 with no language hint scored CER 0.008 and handles
    # English; Qwen3-ASR Flash CER 0.039. Whisper told "zh" writes English
    # words in Chinese and Nemotron told "zh" drops English entirely.
    ("openrouter", STT): (
        _stt("openrouter", "nvidia/parakeet-tdt-0.6b-v3", "Parakeet TDT v3", _PARAKEET_V3,
             usd_per_hour=0.09, notes="Fastest measured; 25 European languages, no Chinese."),
        _stt("openrouter", "microsoft/mai-transcribe-2", "MAI-Transcribe 2", _F({"zh", "yue", "en"}),
             detects_language=True, usd_per_hour=0.1,
             notes="60 languages with code switching; best measured on mixed Chinese and English."),
        _stt("openrouter", "qwen/qwen3-asr-flash-2026-02-10", "Qwen3-ASR Flash", _QWEN3_ASR,
             detects_language=True, usd_per_hour=0.126,
             notes="Mandarin, Chinese dialects, and English mixed in one sentence."),
        _stt("openrouter", "openai/whisper-large-v3-turbo", "Whisper large-v3 turbo", _WHISPER,
             usd_per_hour=0.012, no_hint_for=_F({"zh", "yue"})),
        _stt("openrouter", "nvidia/nemotron-3.5-asr-streaming-multilingual-0.6b", "Nemotron 3.5 ASR",
             None, usd_per_hour=0.012, no_hint_for=_F({"zh", "yue"}),
             notes="Told Chinese, it drops the English words."),
        _stt("openrouter", "qwen/qwen3-asr-1.7b", "Qwen3-ASR 1.7B", _QWEN3_ASR, detects_language=True,
             usd_per_hour=0.027, notes="Cold starts of 25 s measured; not recommended for calls."),
        # ElevenLabs on OpenRouter since 2026-10-07.
        _stt("openrouter", "elevenlabs/scribe-v2", "ElevenLabs Scribe v2", _ELEVENLABS_SCRIBE_V2,
             detects_language=True, usd_per_hour=0.11, notes="90+ languages, detected."),
        _stt("openrouter", "elevenlabs/scribe-v2-medical", "ElevenLabs Scribe v2 Medical", _ELEVENLABS_SCRIBE_V2,
             detects_language=True, usd_per_hour=0.11, notes="Scribe v2 tuned for clinical terms."),
    ),
    ("groq", STT): (
        _stt("groq", "whisper-large-v3-turbo", "Whisper large-v3 turbo", _WHISPER, no_hint_for=_F({"zh", "yue"})),
        _stt("groq", "whisper-large-v3", "Whisper large-v3", _WHISPER, no_hint_for=_F({"zh", "yue"})),
    ),
    ("together", STT): (
        _stt("together", "nvidia/parakeet-tdt-0.6b-v3", "Parakeet TDT v3", _PARAKEET_V3),
        _stt("together", "openai/whisper-large-v3", "Whisper large-v3", _WHISPER, no_hint_for=_F({"zh", "yue"})),
    ),
    # Venice's list and privacy labels as published 2026-10-04; tested that
    # day with a short developer line (STT: one 5 s clip, two rounds).
    ("venice", STT): (
        _stt("venice", "nvidia/parakeet-tdt-0.6b-v3", "Parakeet TDT v3", _PARAKEET_V3, usd_per_hour=0.36,
             privacy="private", notes="About 1 s a turn; 25 European languages, no Chinese."),
        _stt("venice", "openai/whisper-large-v3", "Whisper large-v3", _WHISPER, usd_per_hour=0.36,
             no_hint_for=_F({"zh", "yue"}), privacy="private", notes="About 1.3 to 2.5 s a turn; 99 languages."),
        _stt("venice", "fal-ai/wizper", "Wizper (Whisper v3)", _WHISPER, usd_per_hour=0.36,
             no_hint_for=_F({"zh", "yue"}), privacy="private",
             notes="Whisper v3 on fal; about 1 s a turn. Takes WAV uploads only."),
        _stt("venice", "stt-xai-v1", "Grok Speech to Text", None, usd_per_hour=0.11,
             privacy="anonymized", notes="Quickest measured, about 0.8 s a turn, with punctuation."),
        _stt("venice", "elevenlabs/scribe-v2", "ElevenLabs Scribe v2", _ELEVENLABS_SCRIBE_V2, usd_per_hour=0.6,
             detects_language=True, privacy="anonymized", notes="90+ languages, detected; about 1.6 to 2.3 s a turn."),
    ),
    # Cartesia retires every Sonic 2 and Sonic Turbo snapshot on 2026-10-20
    # and points their users to Sonic 3.6 (docs.cartesia.ai, older models).
    ("cartesia", TTS): (
        _tts("cartesia", "sonic-3.6", "Sonic 3.6", _CARTESIA_SONIC36,
             notes="Cartesia's recommended model, 44 languages; moves to each new 3.6 snapshot."),
        _tts("cartesia", "sonic-3.6-2026-08-27", "Sonic 3.6 (2026-08-27)", _CARTESIA_SONIC36,
             notes="Sonic 3.6 pinned to one snapshot, so it never changes."),
        _tts("cartesia", "sonic-3", "Sonic 3", _CARTESIA_SONIC3, notes="Pronunciation follows tts.language."),
        _tts("cartesia", "sonic-preview", "Sonic Preview", None,
             notes="Cartesia's beta; can change without notice."),
        _tts("cartesia", "sonic-2", "Sonic 2", _CARTESIA_SONIC2,
             notes="Cartesia retires it on 20 October 2026; switch to Sonic 3.6."),
        _tts("cartesia", "sonic-turbo", "Sonic Turbo", _CARTESIA_SONIC2,
             notes="Cartesia retires it on 20 October 2026; switch to Sonic 3.6."),
    ),
    ("elevenlabs", TTS): (
        _tts("elevenlabs", "eleven_multilingual_v2", "Multilingual v2", _ELEVENLABS_MULTILINGUAL_V2,
             notes="Best quality; detects the language from the text."),
        _tts("elevenlabs", "eleven_flash_v2_5", "Flash v2.5", _ELEVENLABS_MULTILINGUAL_V2,
             notes="Lowest latency; takes tts.language."),
        _tts("elevenlabs", "eleven_turbo_v2_5", "Turbo v2.5", _ELEVENLABS_MULTILINGUAL_V2,
             notes="Balanced; takes tts.language."),
    ),
    ("deepgram", TTS): (
        _tts("deepgram", "flux", "Flux", _ENGLISH, STREAMING, voices=_FLUX_VOICES,
             default_voice="flux-hannah-en", model_is_voice=True, notes="Streamed, English."),
        _tts("deepgram", "aura-2", "Aura-2", _DEEPGRAM_AURA2, voices=None,
             default_voice="aura-2-thalia-en", model_is_voice=True,
             notes="Many languages; the voice name carries the language."),
    ),
    ("local", TTS): (
        _tts("local", "kokoro-en", "Kokoro v1.0 82M", _ENGLISH, LOCAL, usd_per_hour=0.0,
             voices=KOKORO_EN_VOICES,
             default_voice="af_heart", notes="The more natural voice, 28 English voices; a 350 MB download."),
        _tts("local", "pocket-tts-en", "Kyutai Pocket TTS", _ENGLISH, LOCAL, usd_per_hour=0.0,
             voices=("pocket-bria", "pocket-loona"), default_voice="pocket-bria",
             notes="English on CPU, chunked audio; a 98 MB download. Bria and Loona voices."),
        _tts("local", "piper-lessac-medium", "Piper Lessac", _ENGLISH, LOCAL, usd_per_hour=0.0,
             voices=("lessac",), default_voice="lessac", notes="Smallest (21 MB) and quickest; plainer voice."),
    ),
    ("openrouter", TTS): (
        _tts("openrouter", "x-ai/grok-voice-tts-1.0", "Grok Voice", None, voices=_GROK_VOICES,
             default_voice="eve", usd_per_hour=0.81,
             notes="Quickest to speak; 20+ languages, detected from the text."),
        _tts("openrouter", "deepgram/flux-tts:free", "Deepgram Flux (free)", _ENGLISH,
             voices=_FLUX_VOICES, default_voice="flux-hannah-en", usd_per_hour=0.0),
        _tts("openrouter", "deepgram/aura-2", "Deepgram Aura-2", _DEEPGRAM_AURA2,
             default_voice="aura-2-thalia-en", usd_per_hour=1.62),
        # Measured 2026-09-24: perfect Chinese round trip with English terms,
        # 24 kHz PCM on our path, first audio in about 0.54 s, about $0.35 an hour.
        _tts("openrouter", "qwen/qwen-audio-3.0-tts-flash", "Qwen TTS Flash", _F({"zh", "en"}),
             voices=_QWEN_TTS_FLASH_VOICES, default_voice="longanhuan_v3.6", usd_per_hour=0.35,
             notes="Best measured for Chinese with English terms."),
        # ElevenLabs on OpenRouter since 2026-10-07: every model takes the same
        # 21 premade voices. Prices are list (launch discount aside). OpenRouter
        # sends each sentence once it is whole, so first audio grows with the
        # sentence: measured that day, a 3 s and an 8 s sentence.
        _tts("openrouter", "elevenlabs/eleven-turbo-v2.5", "ElevenLabs Turbo v2.5", _ELEVENLABS_V2_5,
             voices=_OPENROUTER_ELEVENLABS_VOICES, default_voice="jessica", usd_per_hour=1.08,
             notes="The quickest ElevenLabs measured here; 32 languages."),
        _tts("openrouter", "elevenlabs/eleven-flash-v2.5", "ElevenLabs Flash v2.5", _ELEVENLABS_V2_5,
             voices=_OPENROUTER_ELEVENLABS_VOICES, default_voice="jessica", usd_per_hour=1.08,
             notes="32 languages; about as quick as Turbo v2.5."),
        _tts("openrouter", "elevenlabs/eleven-v4-turbo", "ElevenLabs v4 Turbo", None,
             voices=_OPENROUTER_ELEVENLABS_VOICES, default_voice="jessica", usd_per_hour=1.08,
             notes="90+ languages and audio tags; about 1 s before a short sentence, 2 s before a long one."),
        _tts("openrouter", "elevenlabs/eleven-v4", "ElevenLabs v4", None,
             voices=_OPENROUTER_ELEVENLABS_VOICES, default_voice="jessica", usd_per_hour=2.16,
             notes="The most expressive, 90+ languages; slower than v4 Turbo."),
        _tts("openrouter", "elevenlabs/eleven-multilingual-v2", "ElevenLabs Multilingual v2",
             _ELEVENLABS_MULTILINGUAL_V2, voices=_OPENROUTER_ELEVENLABS_VOICES, default_voice="jessica",
             usd_per_hour=2.16, notes="Made for narration; 29 languages."),
        _tts("openrouter", "elevenlabs/eleven-v3-conversational", "ElevenLabs v3 Conversational", None,
             voices=_OPENROUTER_ELEVENLABS_VOICES, default_voice="jessica", usd_per_hour=1.08,
             notes="70+ languages."),
        _tts("openrouter", "elevenlabs/eleven-v3", "ElevenLabs v3", None,
             voices=_OPENROUTER_ELEVENLABS_VOICES, default_voice="jessica", usd_per_hour=2.16,
             notes="70+ languages."),
        _tts("openrouter", "elevenlabs/eleven-flash-v2", "ElevenLabs Flash v2", _ENGLISH,
             voices=_OPENROUTER_ELEVENLABS_VOICES, default_voice="jessica", usd_per_hour=1.08),
        _tts("openrouter", "elevenlabs/eleven-turbo-v2", "ElevenLabs Turbo v2", _ENGLISH,
             voices=_OPENROUTER_ELEVENLABS_VOICES, default_voice="jessica", usd_per_hour=1.08),
        _tts("openrouter", "hexgrad/kokoro-82m", "Kokoro 82M", None, default_voice="af_sky",
             usd_per_hour=0.22,
             notes="Budget pick; its Chinese voices (zf_, zm_) mangle English words."),
        _tts("openrouter", "canopylabs/orpheus-3b-0.1-ft", "Orpheus 3B", _ENGLISH,
             voices=_ORPHEUS_VOICES, default_voice="tara", usd_per_hour=0.38),
        _tts("openrouter", "minimax/speech-2.8-turbo", "MiniMax Speech 2.8 Turbo", None, usable=False,
             notes="Returns MP3 only; the call path needs PCM."),
        _tts("openrouter", "minimax/speech-2.8-hd", "MiniMax Speech 2.8 HD", None, usable=False,
             notes="Returns MP3 only; the call path needs PCM."),
    ),
    ("groq", TTS): (
        _tts("groq", "canopylabs/orpheus-v1-english", "Orpheus (English)", _ENGLISH, default_voice="troy"),
    ),
    ("together", TTS): (
        # Together lists Sonic 3, not 3.6; its Sonic 2 retires with Cartesia's.
        _tts("together", "cartesia/sonic-3", "Cartesia Sonic 3", _CARTESIA_SONIC3,
             default_voice="db6b0ed5-d5d3-463d-ae85-518a07d3c2b4"),
        _tts("together", "canopylabs/orpheus-3b-0.1-ft", "Orpheus 3B", _ENGLISH,
             voices=_TOGETHER_ORPHEUS_VOICES, default_voice="tara"),
        _tts("together", "hexgrad/Kokoro-82M", "Kokoro 82M", None, default_voice="af_sky"),
    ),
    # First audio is the median of three requests on 2026-10-04. The call
    # path waits 8 s for first audio, so slower models can't hold a call.
    ("venice", TTS): (
        _tts("venice", "tts-xai-v1", "Grok Voice", None, voices=_VENICE_GROK_VOICES,
             default_voice="eve", usd_per_hour=1.01, privacy="anonymized",
             notes="Quickest here; 20+ languages, detected from the text."),
        _tts("venice", "tts-kokoro", "Kokoro", None, voices=_VENICE_KOKORO_VOICES, default_voice="af_sky",
             usd_per_hour=0.19, privacy="private",
             notes="The cheapest, and private. Voice prefixes set the language (af_ American, bf_ British, zf_ Chinese)."),
        _tts("venice", "tts-gradium-v1", "Gradium", None, voices=_VENICE_GRADIUM_VOICES, default_voice="Emma",
             usd_per_hour=2.57, privacy="anonymized", audio_format="wav"),
        _tts("venice", "tts-inworld-1-5-max", "Inworld TTS-1.5 Max", None, voices=_VENICE_INWORLD_VOICES,
             default_voice="Ashley", usd_per_hour=0.68, privacy="anonymized", audio_format="wav"),
        _tts("venice", "tts-elevenlabs-turbo-v2-5", "ElevenLabs Turbo v2.5", _ELEVENLABS_MULTILINGUAL_V2,
             voices=_VENICE_ELEVENLABS_VOICES, default_voice="Rachel", usd_per_hour=3.38,
             privacy="anonymized", audio_format="mp3",
             notes="MP3 only, so each sentence plays once it has fully arrived."),
        _tts("venice", "tts-gemini-3-1-flash", "Gemini 3.1 Flash TTS", None, voices=_VENICE_GEMINI_VOICES,
             default_voice="Kore", usd_per_hour=10.13, privacy="anonymized", audio_format="wav"),
        _tts("venice", "tts-minimax-speech-02-hd", "MiniMax Speech-02 HD", None, voices=_VENICE_MINIMAX_VOICES,
             default_voice="CalmWoman", usd_per_hour=6.75, privacy="anonymized", audio_format="flac", usable=False,
             notes="First audio took about 5.7 s and sometimes over 8 s, when a call stops waiting."),
        _tts("venice", "tts-qwen3-0-6b", "Qwen 3 TTS 0.6B", _QWEN3_TTS, voices=_VENICE_QWEN3_VOICES,
             default_voice="Ryan", usd_per_hour=4.73, privacy="private", audio_format="mp3", usable=False,
             notes="First audio took about 21 s; a call stops waiting after 8 s."),
        _tts("venice", "tts-qwen3-1-7b", "Qwen 3 TTS 1.7B", _QWEN3_TTS, voices=_VENICE_QWEN3_VOICES,
             default_voice="Ryan", usd_per_hour=6.08, privacy="private", audio_format="mp3", usable=False,
             notes="First audio took about 8 s, once over 45 s; a call stops waiting after 8 s."),
        _tts("venice", "tts-orpheus", "Orpheus", _ENGLISH, voices=_TOGETHER_ORPHEUS_VOICES,
             default_voice="tara", usd_per_hour=3.38, privacy="private", audio_format="wav", usable=False,
             notes="Sent no audio in four tries of 45 to 90 s each on 2026-10-04."),
        _tts("venice", "tts-chatterbox-hd", "Chatterbox HD", _ENGLISH, voices=_VENICE_CHATTERBOX_VOICES,
             default_voice="Aurora", usd_per_hour=2.7, privacy="private", audio_format="wav", usable=False,
             notes="First audio took about 9.4 s; a call stops waiting after 8 s."),
    ),
}

# The launch defaults: what every surface picks for English.
_RECOMMENDED: dict[tuple[str, str], str] = {
    ("deepgram", STT): "nova-3",
    ("cartesia", STT): "ink-2",
    ("elevenlabs", STT): "scribe_v2",
    ("local", STT): "moonshine-small-streaming-en",
    ("openrouter", STT): "nvidia/parakeet-tdt-0.6b-v3",
    ("groq", STT): "whisper-large-v3-turbo",
    ("together", STT): "nvidia/parakeet-tdt-0.6b-v3",
    ("venice", STT): "nvidia/parakeet-tdt-0.6b-v3",
    ("cartesia", TTS): "sonic-3.6",
    ("elevenlabs", TTS): "eleven_multilingual_v2",
    ("deepgram", TTS): "flux",
    ("local", TTS): "kokoro-en",
    ("openrouter", TTS): "x-ai/grok-voice-tts-1.0",
    ("groq", TTS): "canopylabs/orpheus-v1-english",
    ("together", TTS): "cartesia/sonic-3",
    ("venice", TTS): "tts-xai-v1",
}

# Chinese on OpenRouter, measured 2026-09-24 on mixed Chinese and English
# coding speech. Runners-up: Qwen3-ASR Flash for listening, Grok Voice "eve"
# for speaking (quickest, small errors).
ZH_OPENROUTER_STT = "microsoft/mai-transcribe-2"
ZH_OPENROUTER_TTS = "qwen/qwen-audio-3.0-tts-flash"

# Picks for a language where the launch default isn't the best choice.
_LANGUAGE_PICKS: dict[tuple[str, str, str], str] = {
    ("openrouter", STT, "zh"): ZH_OPENROUTER_STT,
    ("openrouter", STT, "yue"): ZH_OPENROUTER_STT,
    ("openrouter", TTS, "zh"): ZH_OPENROUTER_TTS,
    # Measured 2026-09-24 on developer speech: MAI-Transcribe-2 beat Whisper
    # large-v3 turbo in Japanese, Korean, and Hindi (CER .011, 0, .046).
    ("openrouter", STT, "ja"): ZH_OPENROUTER_STT,
    ("openrouter", STT, "ko"): ZH_OPENROUTER_STT,
    ("openrouter", STT, "hi"): ZH_OPENROUTER_STT,
}


def providers(role: str) -> tuple[str, ...]:
    return tuple(provider for provider, kind in _CURATED if kind == role)


def curated(provider: str, role: str) -> tuple[ModelOption, ...]:
    return _CURATED.get((provider.strip().lower(), role), ())


def options(provider: str, role: str, *, cache_dir: Path | None = None) -> tuple[ModelOption, ...]:
    """Curated models plus any cached live ones, curated first. No network."""
    provider = provider.strip().lower()
    choices = _merge(curated(provider, role), _read_cache(provider, role, cache_dir))
    if provider == "local" and role == TTS:
        from sleeptalk.speech.local_voices import list_voices

        ids = tuple(v["voice_id"] for v in list_voices())
        choices = tuple(replace(m, voices=(*m.voices, *ids)) if m.id == "pocket-tts-en" else m for m in choices)
    return choices


def find(
    provider: str, role: str, model: str, *, cached: bool = True, cache_dir: Path | None = None,
) -> ModelOption | None:
    """One model by ID. The call path passes ``cached=False``: curated facts only, no disk."""
    wanted = (model or "").strip()
    pool = options(provider, role, cache_dir=cache_dir) if cached else curated(provider, role)
    for option in pool:
        if option.id == wanted:
            return option
    return None


def recommended(provider: str, role: str, language: str = "en-US") -> ModelOption | None:
    """The model a new profile gets for this language; None for an unknown provider."""
    provider = provider.strip().lower()
    base_id = _RECOMMENDED.get((provider, role))
    if base_id is None:
        return None
    by_id = {option.id: option for option in curated(provider, role)}
    base = by_id[base_id]
    tag = primary_tag(language) or "en"
    pick = _LANGUAGE_PICKS.get((provider, role, tag))
    if pick and pick in by_id:
        return by_id[pick]
    if base.covers(tag) is not False:
        return base
    for option in curated(provider, role):
        if option.usable and option.covers(tag):
            return option
    return base


def language_voices(provider: str) -> set[str]:
    """Every per-language starting or alternate voice for this provider."""
    from sleeptalk.voice_catalog import native_ids

    return native_ids(provider)


def recommended_voice(provider: str, language: str = "en-US") -> str:
    option = recommended(provider, TTS, language)
    if option is None:
        return ""
    from sleeptalk.voice_catalog import default_voice_id, native

    # A measured native voice first, when the recommended model can speak it.
    for voice in native(provider, language)[:1]:
        if option.owns_voice(voice.voice_id) is not False:
            return voice.voice_id
    if option.default_voice:
        return option.default_voice
    # Models that take any library voice start on the curated default.
    return default_voice_id(provider)


def profile_model(option: ModelOption, voice_id: str = "") -> str:
    """What a profile's model field holds for this choice (Deepgram: the voice)."""
    if option.model_is_voice:
        return voice_id or option.default_voice
    return option.id


def launch_model(provider: str, role: str) -> str:
    """The English default as a profile writes it."""
    option = recommended(provider, role, "en-US")
    return profile_model(option) if option else ""


# ---------------------------------------------------------------------------
# Live lists: parsers (pure) and the fetch-and-cache wrapper.
# ---------------------------------------------------------------------------

def _price(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_openrouter(payload: Any, role: str) -> list[ModelOption]:
    """``/api/v1/models?output_modalities=transcription|speech``."""
    found: list[ModelOption] = []
    for item in payload.get("data", []) if isinstance(payload, dict) else []:
        model = str(item.get("id") or "")
        if not model:
            continue
        pricing = item.get("pricing") or {}
        prompt, completion = _price(pricing.get("prompt")), _price(pricing.get("completion"))
        per_hour: float | None = None
        if prompt is not None and not completion:
            if role == STT:
                # Per audio second, except the few priced per hour.
                per_hour = prompt if prompt >= 0.01 else prompt * 3600
            else:
                per_hour = prompt * CHARACTERS_PER_HOUR_OF_SPEECH
        voices = item.get("supported_voices")
        found.append(ModelOption(
            "openrouter", role, model, str(item.get("name") or model),
            usd_per_hour=round(per_hour, 4) if per_hour is not None else None,
            voices=tuple(str(v) for v in voices) if isinstance(voices, list) else None,
            default_voice=str(voices[0]) if isinstance(voices, list) and voices else "",
            source="live",
        ))
    return found


def parse_venice(payload: Any, role: str) -> list[ModelOption]:
    """``/api/v1/models?type=asr|tts``: per audio second, or per million characters."""
    found: list[ModelOption] = []
    for item in payload.get("data", []) if isinstance(payload, dict) else []:
        model = str(item.get("id") or "")
        spec = item.get("model_spec") or {}
        if not model:
            continue
        pricing = spec.get("pricing") or {}
        per_hour = None
        if role == STT and isinstance(pricing.get("per_audio_second"), dict):
            second = _price(pricing["per_audio_second"].get("usd"))
            per_hour = second * 3600 if second is not None else None
        elif role == TTS and isinstance(pricing.get("input"), dict):
            million = _price(pricing["input"].get("usd"))
            per_hour = million / 1_000_000 * CHARACTERS_PER_HOUR_OF_SPEECH if million is not None else None
        voices = spec.get("voices")
        privacy = str(spec.get("privacy") or "").lower()
        audio_format, usable = "", True
        formats = spec.get("supported_formats")
        if role == TTS and isinstance(formats, list):
            # A self-describing container first: a new model's raw PCM rate is unknown.
            audio_format = next((f for f in ("wav", "flac", "mp3") if f in formats), "")
            usable = bool(audio_format)
        found.append(ModelOption(
            "venice", role, model, str(spec.get("name") or model),
            usd_per_hour=round(per_hour, 4) if per_hour is not None else None,
            voices=tuple(str(v) for v in voices) if isinstance(voices, list) and voices else None,
            default_voice=str(voices[0]) if isinstance(voices, list) and voices else "",
            usable=usable, notes="" if usable else "Returns no audio format the call path can play.",
            privacy="anonymized" if privacy.startswith("anon") else privacy if privacy == "private" else "",
            audio_format=audio_format, source="live",
        ))
    return found


def parse_deepgram(payload: Any, role: str) -> list[ModelOption]:
    """``GET /v1/models`` (public). STT: streamed general models with their
    languages. TTS: the Aura-2 voices, grouped into one family entry."""
    if not isinstance(payload, dict):
        return []
    if role == STT:
        languages: dict[str, set[str]] = {}
        for item in payload.get("stt", []):
            name = str(item.get("canonical_name") or "")
            if not (item.get("streaming") and name.startswith("nova-") and name.endswith("-general")):
                continue
            model = name.removesuffix("-general")
            languages.setdefault(model, set()).update(primary_tag(tag) for tag in item.get("languages") or [])
        return [ModelOption("deepgram", STT, model, model, _F(tags), transport=STREAMING, source="live")
                for model, tags in sorted(languages.items(), reverse=True)]
    voices: list[str] = []
    tags: set[str] = set()
    for item in payload.get("tts", []):
        name = str(item.get("canonical_name") or "")
        if name.startswith("aura-2-"):
            voices.append(name)
            tags.update(primary_tag(tag) for tag in item.get("languages") or [])
    if not voices:
        return []
    return [ModelOption("deepgram", TTS, "aura-2", "Aura-2", _F(tags), voices=tuple(voices),
                        default_voice="aura-2-thalia-en", model_is_voice=True, source="live")]


def parse_elevenlabs(payload: Any) -> list[ModelOption]:
    """``GET /v1/models`` (needs the account's key): the speaking models."""
    found: list[ModelOption] = []
    for item in payload if isinstance(payload, list) else []:
        if not item.get("can_do_text_to_speech"):
            continue
        tags = {primary_tag(str(lang.get("language_id") or "")) for lang in item.get("languages") or []}
        found.append(ModelOption("elevenlabs", TTS, str(item.get("model_id") or ""), str(item.get("name") or ""),
                                 _F(tag for tag in tags if tag) or None, source="live"))
    return [option for option in found if option.id]


def parse_together_voices(payload: Any, model: str) -> tuple[str, ...]:
    """``GET /v1/voices?model=`` (needs the account's key)."""
    items = payload.get("data", payload) if isinstance(payload, dict) else payload
    voices = []
    for item in items if isinstance(items, list) else []:
        name = item.get("id") or item.get("name") if isinstance(item, dict) else item
        if name:
            voices.append(str(name))
    return tuple(voices)


_PUBLIC_LISTS: dict[tuple[str, str], str] = {
    ("openrouter", STT): "https://openrouter.ai/api/v1/models?output_modalities=transcription",
    ("openrouter", TTS): "https://openrouter.ai/api/v1/models?output_modalities=speech",
    ("venice", STT): "https://api.venice.ai/api/v1/models?type=asr",
    ("venice", TTS): "https://api.venice.ai/api/v1/models?type=tts",
    ("deepgram", STT): "https://api.deepgram.com/v1/models",
    ("deepgram", TTS): "https://api.deepgram.com/v1/models",
}


def live_source(provider: str, role: str) -> str:
    """"public" (no key), "key" (the account's key), or "" (curated only)."""
    provider = provider.strip().lower()
    if (provider, role) in _PUBLIC_LISTS:
        return "public"
    if (provider, role) in {("elevenlabs", TTS), ("together", TTS)}:
        return "key"
    return ""


async def refresh(
    provider: str, role: str, *, api_key: str = "", cache_dir: Path | None = None,
    session=None, now: float | None = None,
) -> tuple[ModelOption, ...]:
    """Fetch the live list (when the provider has one), cache it, and merge.

    A cache younger than the TTL is used as is. Any failure returns the
    cache or, failing that, the curated layer; it never raises.
    """
    provider = provider.strip().lower()
    now = time.time() if now is None else now
    cached_at, cached = _read_cache_entry(provider, role, cache_dir)
    if cached_at is not None and now - cached_at < CACHE_TTL_SECONDS:
        return _merge(curated(provider, role), cached)
    try:
        live = await _fetch_live(provider, role, api_key=api_key, session=session)
    except Exception:  # noqa: BLE001 — offline or refused: the cache or curated layer serves
        live = None
    if live is None:
        return _merge(curated(provider, role), cached)
    _write_cache(provider, role, live, cache_dir, now)
    return _merge(curated(provider, role), live)


async def _fetch_live(provider: str, role: str, *, api_key: str, session) -> list[ModelOption] | None:
    import aiohttp

    source = live_source(provider, role)
    if not source or (source == "key" and not api_key.strip()):
        return None
    owned = session is None
    client = session or aiohttp.ClientSession()
    timeout = aiohttp.ClientTimeout(total=FETCH_TIMEOUT_SECONDS)
    try:
        if source == "public":
            async with client.get(_PUBLIC_LISTS[(provider, role)], timeout=timeout) as response:
                if response.status != 200:
                    return None
                payload = await response.json(content_type=None)
            parser = {"openrouter": parse_openrouter, "venice": parse_venice, "deepgram": parse_deepgram}[provider]
            return parser(payload, role)
        if provider == "elevenlabs":
            async with client.get("https://api.elevenlabs.io/v1/models", headers={"xi-api-key": api_key},
                                  timeout=timeout) as response:
                if response.status != 200:
                    return None
                return parse_elevenlabs(await response.json(content_type=None))
        found = []
        for option in curated("together", TTS):
            async with client.get("https://api.together.xyz/v1/voices", params={"model": option.id},
                                  headers={"Authorization": f"Bearer {api_key}"}, timeout=timeout) as response:
                if response.status != 200:
                    continue
                voices = parse_together_voices(await response.json(content_type=None), option.id)
            if voices:
                found.append(replace(option, voices=voices, source="live"))
        return found
    finally:
        if owned:
            await client.close()


def _merge(curated_options: Iterable[ModelOption], live: Iterable[ModelOption]) -> tuple[ModelOption, ...]:
    """Curated facts win; live adds models, fills unknown prices, and adds voices."""
    merged: dict[str, ModelOption] = {option.id: option for option in curated_options}
    for option in live:
        known = merged.get(option.id)
        if known is None:
            merged[option.id] = option
            continue
        voices = known.voices
        if option.voices:
            voices = tuple(dict.fromkeys((*(known.voices or ()), *option.voices)))
        languages = known.languages
        if languages is not None and option.languages:
            languages = languages | option.languages
        merged[option.id] = replace(
            known,
            usd_per_hour=known.usd_per_hour if known.usd_per_hour is not None else option.usd_per_hour,
            voices=voices, languages=languages, privacy=known.privacy or option.privacy,
        )
    return tuple(merged.values())


# ---------------------------------------------------------------------------
# Cache: one private JSON file per provider and role.
# ---------------------------------------------------------------------------

def _cache_dir(cache_dir: Path | None) -> Path:
    if cache_dir is not None:
        return cache_dir
    from sleeptalk.home import resolve_layout

    return resolve_layout().root / "cache" / "speech-models"


def _cache_path(provider: str, role: str, cache_dir: Path | None) -> Path:
    return _cache_dir(cache_dir) / f"{provider}-{role}.json"


def _to_json(option: ModelOption) -> dict[str, Any]:
    return {
        "provider": option.provider, "role": option.role, "id": option.id, "label": option.label,
        "languages": sorted(option.languages) if option.languages is not None else None,
        "detects_language": option.detects_language, "transport": option.transport,
        "usd_per_hour": option.usd_per_hour,
        "voices": list(option.voices) if option.voices is not None else None,
        "default_voice": option.default_voice, "model_is_voice": option.model_is_voice,
        "no_hint_for": sorted(option.no_hint_for), "usable": option.usable,
        "notes": option.notes, "privacy": option.privacy, "audio_format": option.audio_format,
    }


def _from_json(item: dict[str, Any]) -> ModelOption:
    languages = item.get("languages")
    voices = item.get("voices")
    return ModelOption(
        str(item["provider"]), str(item["role"]), str(item["id"]), str(item.get("label") or ""),
        _F(languages) if isinstance(languages, list) else None,
        detects_language=bool(item.get("detects_language")),
        transport=str(item.get("transport") or BATCH),
        usd_per_hour=_price(item.get("usd_per_hour")),
        voices=tuple(voices) if isinstance(voices, list) else None,
        default_voice=str(item.get("default_voice") or ""),
        model_is_voice=bool(item.get("model_is_voice")),
        no_hint_for=_F(item.get("no_hint_for") or ()),
        usable=item.get("usable") is not False,
        notes=str(item.get("notes") or ""),
        privacy=str(item.get("privacy") or ""),
        audio_format=str(item.get("audio_format") or ""),
        source="live",
    )


def _read_cache_entry(provider: str, role: str, cache_dir: Path | None) -> tuple[float | None, list[ModelOption]]:
    path = _cache_path(provider, role, cache_dir)
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return None, []
    try:
        with os.fdopen(fd, encoding="utf-8") as stream:
            data = json.load(stream)
        return float(data["fetched_at"]), [_from_json(item) for item in data["models"]]
    except (OSError, ValueError, KeyError, TypeError):
        return None, []


def _read_cache(provider: str, role: str, cache_dir: Path | None) -> list[ModelOption]:
    return _read_cache_entry(provider, role, cache_dir)[1]


def _write_cache(provider: str, role: str, found: list[ModelOption], cache_dir: Path | None, now: float) -> None:
    import tempfile

    path = _cache_path(provider, role, cache_dir)
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".models-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump({"fetched_at": now, "models": [_to_json(option) for option in found]}, stream)
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)
    except OSError:
        return


# Measured, not published: TTS is time to the first audio byte, STT the time
# to transcribe one short turn. From the 2026-09-24 bake-off (STT figures ran
# with parallel load, so they compare models rather than promise a number).
# Add models as they are measured; anything absent shows as unknown.
MEASURED_ON = "2026-09-24"
MEASURED_LATENCY_MS: dict[tuple[str, str, str], int] = {
    ("openrouter", TTS, "x-ai/grok-voice-tts-1.0"): 260,
    ("openrouter", TTS, "qwen/qwen-audio-3.0-tts-flash"): 540,
    ("openrouter", TTS, "fish-audio/s2.1-pro-free:free"): 420,
    ("openrouter", TTS, "hexgrad/kokoro-82m"): 350,
    ("openrouter", STT, "microsoft/mai-transcribe-2"): 290,
    ("openrouter", STT, "nvidia/parakeet-tdt-0.6b-v3"): 300,
    ("openrouter", STT, "qwen/qwen3-asr-flash-2026-02-10"): 640,
    ("openrouter", STT, "qwen/qwen3-asr-0.6b"): 330,
    ("openrouter", STT, "openai/whisper-large-v3-turbo"): 1050,
}
# ElevenLabs on OpenRouter, measured 2026-10-07: first audio for a 3 s
# sentence, median of three (whole-sentence delivery, so longer sentences
# wait longer); Scribe the round trip of the bundled clip.
MEASURED_LATENCY_MS.update({
    ("openrouter", TTS, "elevenlabs/eleven-turbo-v2.5"): 350,
    ("openrouter", TTS, "elevenlabs/eleven-flash-v2.5"): 450,
    ("openrouter", TTS, "elevenlabs/eleven-flash-v2"): 460,
    ("openrouter", TTS, "elevenlabs/eleven-turbo-v2"): 510,
    ("openrouter", TTS, "elevenlabs/eleven-v4-turbo"): 1020,
    ("openrouter", TTS, "elevenlabs/eleven-v4"): 1260,
    ("openrouter", TTS, "elevenlabs/eleven-multilingual-v2"): 1280,
    ("openrouter", TTS, "elevenlabs/eleven-v3-conversational"): 1360,
    ("openrouter", TTS, "elevenlabs/eleven-v3"): 1540,
    ("openrouter", STT, "elevenlabs/scribe-v2"): 720,
    ("openrouter", STT, "elevenlabs/scribe-v2-medical"): 610,
})


# Venice, measured 2026-10-04 (median of three for TTS, two for STT).
MEASURED_LATENCY_MS.update({
    ("venice", TTS, "tts-xai-v1"): 980,
    ("venice", TTS, "tts-gradium-v1"): 1320,
    ("venice", TTS, "tts-inworld-1-5-max"): 1810,
    ("venice", TTS, "tts-elevenlabs-turbo-v2-5"): 1870,
    ("venice", TTS, "tts-kokoro"): 1930,
    ("venice", TTS, "tts-gemini-3-1-flash"): 3470,
    ("venice", TTS, "tts-minimax-speech-02-hd"): 5670,
    ("venice", TTS, "tts-qwen3-1-7b"): 8170,
    ("venice", TTS, "tts-chatterbox-hd"): 9430,
    ("venice", TTS, "tts-qwen3-0-6b"): 21100,
    ("venice", STT, "stt-xai-v1"): 760,
    ("venice", STT, "fal-ai/wizper"): 900,
    ("venice", STT, "nvidia/parakeet-tdt-0.6b-v3"): 1040,
    ("venice", STT, "openai/whisper-large-v3"): 1920,
    ("venice", STT, "elevenlabs/scribe-v2"): 1950,
})
# Cartesia, measured 2026-10-04: TTS first audio (median of three, English);
# STT the wait for the final transcript after the speech ends. Sonic 3.6, its
# snapshot, and Sonic 3 again on 2026-10-07 with a warm connection (median of
# six, three for the snapshot): the 380 ms first measured for 3.6 included a
# cold start of about 1.2 s.
MEASURED_LATENCY_MS.update({
    ("cartesia", TTS, "sonic-3.6"): 120,
    ("cartesia", TTS, "sonic-3.6-2026-08-27"): 125,
    ("cartesia", TTS, "sonic-3"): 170,
    ("cartesia", TTS, "sonic-preview"): 150,
    ("cartesia", TTS, "sonic-2"): 190,
    ("cartesia", TTS, "sonic-turbo"): 170,
    ("cartesia", STT, "ink-2"): 130,
    ("cartesia", STT, "ink-preview"): 130,
})
_MEASURED_ON_BY_PROVIDER = {"venice": "2026-10-04", "cartesia": "2026-10-07"}


def latency_ms(provider: str, role: str, model: str) -> int | None:
    return MEASURED_LATENCY_MS.get((provider.strip().lower(), role, model))


def payload(provider: str, role: str, language: str = "en-US", *, cache_dir: Path | None = None) -> dict[str, Any]:
    """JSON for a settings page: every model, the recommended one marked."""
    best = recommended(provider, role, language)
    from sleeptalk.speech.local_models import MODELS, download_info

    return {
        "recommended": best.id if best else "",
        "measured_on": _MEASURED_ON_BY_PROVIDER.get(provider.strip().lower(), MEASURED_ON),
        "models": [
            {**_to_json(option), "covers_language": option.covers(language), "source": option.source,
             "recommended": best is not None and option.id == best.id,
             "latency_ms": latency_ms(provider, role, option.id),
             **({"download": download_info(MODELS[option.id])}
                if option.provider == "local" and option.id in MODELS else {})}
            for option in options(provider, role, cache_dir=cache_dir)
        ],
    }


__all__ = [
    "BATCH", "LOCAL", "MEASURED_LATENCY_MS", "MEASURED_ON", "STREAMING", "STT", "TTS", "ModelOption",
    "ZH_OPENROUTER_STT", "ZH_OPENROUTER_TTS", "language_voices", "latency_ms",
    "curated", "find", "launch_model", "live_source", "options", "parse_deepgram", "parse_elevenlabs",
    "parse_openrouter", "parse_together_voices", "parse_venice", "payload", "profile_model", "providers",
    "recommended", "recommended_voice", "refresh",
]
