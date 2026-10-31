"""`--speech-provider` and friends are optional: with no flag the key page
offers every provider and the user picks there."""
from types import SimpleNamespace

import pytest

from sleeptalk.agent_setup_options import speech_provider_choices
from sleeptalk.private_input import InputError


def flags(**values):
    return SimpleNamespace(**{"speech_provider": "", "stt_provider": "", "tts_provider": "", "tts_voice_id": "",
                              "speech_from_profile": "", **values})


def test_reusing_a_profile_replaces_every_provider_flag():
    assert speech_provider_choices(flags(speech_from_profile="default")) == {"speech_from_profile": "default"}
    with pytest.raises(InputError, match="reuses that profile"):
        speech_provider_choices(flags(speech_from_profile="default", speech_provider="cartesia"))
    with pytest.raises(InputError, match="reuses that profile"):
        speech_provider_choices(flags(speech_from_profile="default", tts_voice_id="v"))


def test_no_flag_leaves_the_choice_to_the_page():
    assert speech_provider_choices(flags()) == {}


def test_a_named_provider_goes_first_and_locks_only_with_a_voice():
    assert speech_provider_choices(flags(speech_provider="cartesia")) == {"preferred_provider": "cartesia"}
    assert speech_provider_choices(flags(speech_provider="cartesia", tts_voice_id="v1")) == {
        "stt_provider": "cartesia", "tts_provider": "cartesia", "tts_voice_id": "v1"}
    assert speech_provider_choices(flags(tts_provider="elevenlabs")) == {"stt_provider": "deepgram", "tts_provider": "elevenlabs"}


@pytest.mark.parametrize("given, message", [
    (dict(speech_provider="cartesia", tts_provider="cartesia"), "pass one form"),
    (dict(stt_provider="deepgram"), "needs --tts-provider"),
    (dict(tts_voice_id="flux-kit-en"), "needs the provider"),
    (dict(speech_provider="local"), "Local speech collects no key"),
    (dict(tts_provider="local"), "Local speech collects no key"),
])
def test_unusable_combinations_explain_themselves(given, message):
    with pytest.raises(InputError, match=message):
        speech_provider_choices(flags(**given))


def test_provider_facts_cover_every_key_provider():
    import json
    from pathlib import Path
    import sleeptalk
    from sleeptalk.private_input import KEY_PROVIDERS
    facts = json.loads((Path(sleeptalk.__file__).parent / "_setup" / "providers.json").read_text())["providers"]
    by_id = {fact["id"]: fact for fact in facts}
    assert tuple(fact["id"] for fact in facts if fact["key_url"]) == KEY_PROVIDERS
    assert [fact["id"] for fact in facts if fact["primary"]] == ["cartesia", "deepgram"]
    for fact in facts:
        assert fact["free"] and fact["card"] and fact["tagline"], fact["id"]
    assert by_id["local"]["key_url"] == ""


def test_link_command_accepts_a_discord_surface(tmp_path, monkeypatch, capsys):
    """The Discord recipe in `host --help` must parse; it did not until 2026-09-21."""
    import json
    from sleeptalk.cli import main

    seen = {}

    async def inspect(spec, **kwargs):
        seen.update(spec.model_dump())
        seen["entry"] = kwargs.get("entry")
        return {"schema_version": 1, "operation": "private_input", "status": "waiting"}

    monkeypatch.setattr("sleeptalk.private_input.start", inspect)
    main(["host", "input", "link", "--request-id", "discord-cli-001", "--profile", "discord", "--surface", "discord",
          "--speech-from-profile", "default", "--agent", "hermes", "--workspace", str(tmp_path)])
    assert json.loads(capsys.readouterr().out)["status"] == "waiting"
    assert seen["surface"] == "discord" and seen["speech_from_profile"] == "default" and seen["entry"] == "link"
    main(["host", "input", "link", "--request-id", "web-cli-001", "--profile", "default",
          "--agent", "hermes", "--workspace", str(tmp_path)])
    capsys.readouterr()
    assert seen["surface"] == "web"


def test_the_speech_provider_flag_accepts_every_registered_speech_provider():
    """The VPS agent hit an argparse refusal for openrouter on 2026-09-21; the choices now come from one list."""
    import argparse
    from sleeptalk.agent_setup_options import SPEECH_PROVIDER_CHOICES, add_speech_provider_arguments
    from sleeptalk.private_input import KEY_PROVIDERS
    from sleeptalk.speech.openai_audio import PRESETS

    assert set(KEY_PROVIDERS) <= set(SPEECH_PROVIDER_CHOICES) and set(PRESETS) <= set(SPEECH_PROVIDER_CHOICES)
    parser = argparse.ArgumentParser()
    add_speech_provider_arguments(parser)
    assert speech_provider_choices(parser.parse_args(["--speech-provider", "venice"])) == {"preferred_provider": "venice"}
