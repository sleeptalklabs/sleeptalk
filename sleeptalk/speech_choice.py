"""The rules for moving a profile's speech choices, for every settings surface.

The settings page and the terminal wizard both change a language, a model or
a voice. Each change can drag the others along: a new language moves models
and voices that were only there as the old language's recommendation; a new
speaking model with its own voices moves a voice it can't speak. These
functions hold those rules once, over plain values, so the two surfaces can't
drift apart. They read the catalog; they never touch a file.
"""
from __future__ import annotations

from dataclasses import dataclass, replace


@dataclass(frozen=True)
class SpeechChoice:
    stt_provider: str
    stt_model: str
    tts_provider: str
    tts_model: str
    voice: str
    language: str


def tts_option(provider: str, model: str, voice: str):
    """The catalog entry behind a speaking model; for Deepgram the voice names it."""
    from sleeptalk import speech_catalog

    choices = (speech_catalog.options if provider == "local" else speech_catalog.curated)(provider, speech_catalog.TTS)
    option = next((o for o in choices if o.id == model and not o.model_is_voice), None)
    if option is not None:
        return option
    named = voice or model
    return next((o for o in choices if o.model_is_voice and (named in (o.voices or ()) or named.startswith(o.id + "-"))), None)


def follow_language(choice: SpeechChoice, language: str) -> tuple[SpeechChoice, list[str]]:
    """The choice after a language change, and what moved.

    A model or voice that was the old language's recommendation, or that
    can't serve the new language, becomes the new language's recommendation.
    One the person chose themselves stays while it still fits. What moved is
    named "listening model", "speaking model", "voice".
    """
    from sleeptalk import speech_catalog as catalog
    from sleeptalk.builder import stt_model_for_provider, tts_model_for_provider
    from sleeptalk.voice_catalog import curated

    old = choice.language
    moved = replace(choice, language=language)
    changed: list[str] = []
    same_language = catalog.primary_tag(old) == catalog.primary_tag(language)

    provider = choice.stt_provider
    before, after = catalog.recommended(provider, catalog.STT, old), catalog.recommended(provider, catalog.STT, language)
    if before is not None and after is not None:
        current = catalog.find(provider, catalog.STT, stt_model_for_provider(provider, choice.stt_model), cached=False)
        if current is not None and current.id != after.id and (current.id == before.id or current.covers(language) is False):
            moved = replace(moved, stt_model=after.id)
            changed.append("listening model")

    provider = choice.tts_provider
    before, after = catalog.recommended(provider, catalog.TTS, old), catalog.recommended(provider, catalog.TTS, language)
    if before is None or after is None:
        return moved, changed
    voice = choice.voice
    current = tts_option(provider, tts_model_for_provider(provider, choice.tts_model), voice)
    new_voice = catalog.recommended_voice(provider, language)
    model_moves = current is not None and current.id != after.id and (current.id == before.id or current.covers(language) is False)
    if model_moves and not after.model_is_voice:
        moved = replace(moved, tts_model=after.id)
        changed.append("speaking model")
    # A starting voice (the old recommendation, or one of the curated English
    # picks when the language itself changed) follows the language; a voice
    # pasted in by the person stays unless the new model can't speak it.
    starter = voice == catalog.recommended_voice(provider, old) or not voice or (
        not same_language and (voice in {v.voice_id for v in curated(provider)} or voice in catalog.language_voices(provider)))
    model = after if model_moves else current
    unspeakable = model is not None and model.owns_voice(voice) is False
    if model_moves and after.model_is_voice:
        unspeakable = True
    if new_voice and voice != new_voice and (starter or unspeakable):
        moved = replace(moved, voice=new_voice)
        if model_moves and after.model_is_voice:
            changed.append("speaking model")
        changed.append("voice")
    return moved, changed


def for_new_profile(provider: str, language: str, *, stt_model: str = "", tts_model: str = "",
                    voice: str = "") -> SpeechChoice:
    """A new profile's speech: the language's recommendation, then the person's picks.

    Picks go through the same moves as later edits, so a voice the chosen
    speaking model can't speak moves to one it can. With no voice picked the
    voice stays empty, and the key check picks one for the language.
    """
    from sleeptalk import speech_catalog as catalog
    from sleeptalk.builder import stt_model_for_provider

    listening = catalog.recommended(provider, catalog.STT, language)
    speaking = catalog.recommended(provider, catalog.TTS, language)
    choice = SpeechChoice(
        stt_provider=provider, stt_model=listening.id if listening else stt_model_for_provider(provider),
        tts_provider=provider, tts_model=speaking.id if speaking and not speaking.model_is_voice else "",
        voice="", language=language)
    if voice:
        choice = with_voice(choice, voice)
    if stt_model:
        choice = with_model(choice, "stt", stt_model)
    if tts_model:
        choice = with_model(choice, "tts", tts_model)
    return choice


def usable_model(provider: str, role: str, model: object):
    """The catalog option for a model the person picked; ValueError if it isn't one."""
    from sleeptalk import speech_catalog

    if not isinstance(model, str) or not model.strip() or len(model) > 200:
        raise ValueError("That model isn't one of the choices.")
    option = next((o for o in speech_catalog.options(provider, role) if o.id == model.strip()), None)
    if option is None or not option.usable:
        raise ValueError("That model isn't one of the choices.")
    return option


def with_model(choice: SpeechChoice, role: str, model: object) -> SpeechChoice:
    """The choice after picking another model of the same provider and role.

    A speaking model with its own voices takes the voice only if the voice is
    one of them; otherwise the voice moves to the model's default, so a call
    never pairs a voice with a model that can't speak it. Deepgram's model is
    the voice itself, so there the voice is set instead.
    """
    provider = choice.stt_provider if role == "stt" else choice.tts_provider
    option = usable_model(provider, role, model)
    if role == "stt":
        return replace(choice, stt_model=option.id)
    if option.model_is_voice:
        # The model is a voice family (Deepgram's flux, aura-2): a voice of
        # that family stays, anything else starts on the family's default.
        owned = option.owns_voice(choice.voice)
        if owned is None:
            owned = choice.voice.startswith(option.id + "-")
        return replace(choice, voice=choice.voice if owned else option.default_voice or option.id)
    moved = replace(choice, tts_model=option.id)
    if option.default_voice and choice.voice != option.default_voice and (
            (option.voices is not None and choice.voice not in option.voices)
            or (option.voices is None and _listed_by_another(provider, option.id, choice.voice))):
        moved = replace(moved, voice=option.default_voice)
    return moved


def _listed_by_another(provider: str, model: str, voice: str) -> bool:
    """True when another speaking model lists this voice as one of its own.

    A model with no voice list takes library voices, not another model's.
    """
    from sleeptalk import speech_catalog

    return any(o.id != model and o.voices is not None and voice in o.voices
               for o in speech_catalog.curated(provider, speech_catalog.TTS))


def with_voice(choice: SpeechChoice, voice: str) -> SpeechChoice:
    """The choice after picking a voice: a model that can't speak it moves to one that can."""
    from sleeptalk import speech_catalog
    from sleeptalk.builder import tts_model_for_provider

    moved = replace(choice, voice=voice)
    current = tts_option(choice.tts_provider, tts_model_for_provider(choice.tts_provider, choice.tts_model), voice)
    if current is None or current.owns_voice(voice) is not False:
        return moved
    choices = (speech_catalog.options if choice.tts_provider == "local" else speech_catalog.curated)(
        choice.tts_provider, speech_catalog.TTS)
    speaker = next((o for o in choices
                    if not o.model_is_voice and o.owns_voice(voice)), None)
    return replace(moved, tts_model=speaker.id) if speaker is not None else moved
