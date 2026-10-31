"""Tests for the sleeptalk init wizard's non-interactive seams."""

from __future__ import annotations

import tomllib

import pytest

from sleeptalk import voice_list, voice_sample, wizard
from sleeptalk.config import SleeptalkConfig


@pytest.fixture
def ready_cli(monkeypatch):
    monkeypatch.setattr(
        "sleeptalk.registries.locate_binary",
        lambda binary, fallbacks=(): f"/usr/bin/{binary}",
    )
    # Native dependencies (ffmpeg, libopus) belong to the host, not the test.
    monkeypatch.setattr(
        "sleeptalk.sysdeps.ensure_required_for_surface",
        lambda _surface, *, repair: [],
    )


def _answers(**overrides) -> wizard.WizardAnswers:
    answers = wizard.WizardAnswers(
        name="Test Bot",
        surface="discord",
        slug="test-bot",
        guild_id="123",
        client_id="456",
        token="secret-token-1",
        language="es-ES",
        stt_key="dg-secret-1",
        tts_key="cart-secret-1",
        tts_voice_id="voice-1",
    )
    for key, value in overrides.items():
        setattr(answers, key, value)
    return answers


# ---------------------------------------------------------------------------
# pure answer builders
# ---------------------------------------------------------------------------


def test_profile_toml_validates_against_schema():
    text = wizard.profile_toml(_answers())
    config = SleeptalkConfig.model_validate(tomllib.loads(text))
    assert config.stt.language == "es-ES"
    assert config.tts.provider == "cartesia"
    assert config.tts.model == "sonic-3.6"
    assert config.tts.voice_id == "voice-1"
    # per-slug token env var (repo convention: never clobber another bot)
    assert config.transport.token_env == "TEST_BOT_DISCORD_BOT_TOKEN"
    # secrets never land in TOML
    for secret in ("secret-token-1", "dg-secret-1", "cart-secret-1"):
        assert secret not in text


def test_env_entries_per_provider():
    entries = wizard.env_entries(_answers())
    assert entries == {
        "TEST_BOT_DISCORD_BOT_TOKEN": "secret-token-1",
        "DEEPGRAM_API_KEY": "dg-secret-1",
        "CARTESIA_API_KEY": "cart-secret-1",
    }
    entries = wizard.env_entries(_answers(tts_provider="elevenlabs"))
    assert "ELEVENLABS_API_KEY" in entries and "CARTESIA_API_KEY" not in entries
    entries = wizard.env_entries(_answers(agent_type="hermes_gateway", gateway_api_key="gw"))
    assert entries["HERMES_GATEWAY_API_KEY"] == "gw"


def test_slugify_and_default_language():
    assert wizard.slugify("My Cool Bot!") == "my-cool-bot"
    assert wizard.slugify("") == "sleeptalk"
    assert wizard.sample_line("es-MX").startswith("¡Hola!")
    assert wizard.sample_line("xx-ZZ") == wizard.SAMPLE_LINES["en"]


def test_detect_agents_never_blocks(monkeypatch):
    monkeypatch.setattr(
        "sleeptalk.registries.locate_binary",
        lambda binary, fallbacks=(): None,
    )
    assert wizard.detect_agents() == {}
    monkeypatch.setattr(
        "sleeptalk.registries.locate_binary",
        lambda binary, fallbacks=(): f"/usr/bin/{binary}",
    )
    detected = wizard.detect_agents()
    assert "pi" in detected
    assert "codex" in detected
    assert "claude_code" in detected


# ---------------------------------------------------------------------------
# file writing: one-shot, edit-don't-wipe
# ---------------------------------------------------------------------------


def test_write_files_roundtrip_and_edit(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".gitignore").write_text("node_modules\n")
    answers = _answers()
    written = wizard.write_files(
        answers, profiles_dir=tmp_path / "config/profiles", env_path=tmp_path / ".env"
    )
    assert (tmp_path / "sleeptalk.toml").is_file()
    assert str(tmp_path / "sleeptalk.toml") in written
    assert (tmp_path / "config/profiles/test-bot.toml").is_file()
    profile_path = tmp_path / "config/profiles/test-bot.toml"
    # The token lives in the private store; the profile only references it.
    assert wizard._stored_secrets(profile_path)["TEST_BOT_DISCORD_BOT_TOKEN"] == "secret-token-1"
    assert "secret-token-1" not in profile_path.read_text()
    assert not (tmp_path / ".env").exists() or "secret-token-1" not in (tmp_path / ".env").read_text()

    # Re-run on the same profile edits answers, never wipes unrelated lines,
    # and drops a plain-text copy of a key the store now holds.
    (tmp_path / ".env").write_text("UNRELATED=keepme\nTEST_BOT_DISCORD_BOT_TOKEN=old-plain\n")
    loaded = wizard.load_existing_answers("test-bot", tmp_path / "config/profiles")
    assert loaded is not None and loaded.language == "es-ES"
    loaded.language = "fr-FR"
    loaded.token = "secret-token-2"
    wizard.write_files(
        loaded, profiles_dir=tmp_path / "config/profiles", env_path=tmp_path / ".env"
    )
    profile = tomllib.loads(profile_path.read_text())
    assert profile["stt"]["language"] == "fr-FR"
    env_lines = (tmp_path / ".env").read_text().splitlines()
    assert "UNRELATED=keepme" in env_lines
    assert not any(line.startswith("TEST_BOT_DISCORD_BOT_TOKEN=") for line in env_lines)
    assert wizard._stored_secrets(profile_path)["TEST_BOT_DISCORD_BOT_TOKEN"] == "secret-token-2"

    # Editing loads the stored key back, and an unchanged rerun rewrites nothing.
    again = wizard.load_existing_answers("test-bot", tmp_path / "config/profiles")
    wizard.load_existing_secrets(again, tmp_path / ".env", profile_path)
    assert again.token == "secret-token-2"
    assert wizard.write_files(again, profiles_dir=tmp_path / "config/profiles", env_path=tmp_path / ".env") == []
    # .gitignore got .env
    assert ".env" in (tmp_path / ".gitignore").read_text().splitlines()


def test_load_existing_secrets_from_env(tmp_path):
    answers = wizard.WizardAnswers(slug="martha", tts_provider="elevenlabs")
    (tmp_path / ".env").write_text(
        "MARTHA_DISCORD_BOT_TOKEN=bot-secret\n"
        "DEEPGRAM_API_KEY=dg-secret\n"
        "ELEVENLABS_API_KEY=el-secret\n"
    )
    wizard.load_existing_secrets(answers, tmp_path / ".env")
    assert answers.token == "bot-secret"
    assert answers.stt_key == "dg-secret"
    assert answers.tts_key == "el-secret"
    answers.tts_key = "typed-new"
    wizard.load_existing_secrets(answers, tmp_path / ".env")
    assert answers.tts_key == "typed-new"


def test_audio_is_a_setting_not_an_interview_question():
    """Terminal audio is asked at boot, so setup must not spend a question."""
    menu = [key for key, _label in wizard.edit_section_labels()]
    interview = wizard.interview_section_keys()
    for key in ("audio", "narration", "reply"):
        assert key in menu
        assert key not in interview


def test_edit_section_labels_cover_the_interview():
    keys = [key for key, _label in wizard.edit_section_labels()]
    assert keys == [
        "surface",
        "delivery",
        "audio",
        "narration",
        "reply",
        "latency",
        "voice_engine",
        "system",
        "agent",
        "name",
        "token",
        "ids",
        "access",
        "language",
        "models",
        "speed",
        "turns",
        "speech",
        "voice",
    ]
    assert wizard.EDIT_DONE == "__done__"
    assert wizard.EDIT_ALL == "__all__"


def test_load_existing_missing(tmp_path):
    assert wizard.load_existing_answers("nope", tmp_path) is None
    assert wizard.existing_profiles(tmp_path) == []


# ---------------------------------------------------------------------------
# voice curation + playback file lifecycle
# ---------------------------------------------------------------------------


def test_curate_filters_by_language():
    voices = [
        voice_list.Voice(id="a", name="Ana", language="es"),
        voice_list.Voice(id="b", name="Bob", language="en"),
        voice_list.Voice(id="c", name="Clara", language="es"),
    ]
    picks, warning = voice_list.curate(voices, "es-MX")
    assert [v.id for v in picks] == ["a", "c"] and warning is None


def test_curate_fallback_warns_honestly():
    voices = [voice_list.Voice(id="b", name="Bob", language="en")]
    picks, warning = voice_list.curate(voices, "ja-JP")
    assert picks and "ja" in warning
    picks, warning = voice_list.curate([], "ja-JP")
    assert not picks and "paste a voice ID" in warning


@pytest.mark.asyncio
async def test_play_file_deletes_after_success(tmp_path, monkeypatch):
    sample = tmp_path / "s.wav"
    sample.write_bytes(b"RIFF")
    monkeypatch.setattr(
        voice_sample, "_PLAYERS", (("true", ["/usr/bin/true", "{file}"]),)
    )
    assert await voice_sample.play_file(sample) is True
    assert not sample.exists()


@pytest.mark.asyncio
async def test_play_file_no_player_leaves_file(tmp_path, monkeypatch):
    sample = tmp_path / "s.wav"
    sample.write_bytes(b"RIFF")
    monkeypatch.setattr(voice_sample, "find_player", lambda: None)
    assert await voice_sample.play_file(sample) is False
    assert sample.exists()  # left behind for manual playback


# ---------------------------------------------------------------------------
# non-interactive / answers-file path
# ---------------------------------------------------------------------------

ANSWERS_TOML = """\
name = "Test Bot"
surface = "discord"
agent_type = "pi"
guild_id = "123"
client_id = "456"
token = "secret-token-1"
language = "es-ES"
stt_key = "dg-secret-1"
tts_provider = "cartesia"
tts_key = "cart-secret-1"
tts_voice_id = "voice-1"
"""


def test_answers_from_mapping_roundtrip():
    raw = tomllib.loads(ANSWERS_TOML)
    raw["tts_speed"] = 1.5
    answers = wizard.answers_from_mapping(raw)
    assert answers.slug == "test-bot"  # derived from name
    assert answers.language == "es-ES"
    assert answers.tts_speed == 1.5
    assert isinstance(answers.tts_speed, float)
    with pytest.raises(ValueError, match="unknown answers key"):
        wizard.answers_from_mapping({"bogus": "1"})


@pytest.mark.parametrize("value", [True, "1.5"])
def test_answers_from_mapping_rejects_non_numeric_tts_speed(value):
    with pytest.raises(ValueError, match="tts_speed must be a number"):
        wizard.answers_from_mapping({"tts_speed": value})


def test_answers_degraded_language_flips_stt_model():
    raw = tomllib.loads(ANSWERS_TOML)
    raw["language"] = "pl-PL"
    answers = wizard.answers_from_mapping(raw)
    assert answers.stt_model == "nova-2"
    raw["language"] = "es-ES"  # fully supported languages keep nova-3
    answers = wizard.answers_from_mapping(raw)
    assert answers.stt_model == "nova-3"


def test_wizard_and_agent_defaults_use_sleeptalk_name():
    assert wizard.WizardAnswers().name == "sleeptalk"
    assert SleeptalkConfig().agent.name == "sleeptalk"


def test_missing_answer_fields_are_fix_stating():
    problems = wizard.missing_answer_fields(wizard.WizardAnswers(surface="discord"))
    # name has a non-empty schema default ("sleeptalk"), so it's never missing
    assert len(problems) == len(wizard.REQUIRED_ANSWER_FIELDS) - 1
    assert any("--token" in p for p in problems)


@pytest.mark.asyncio
async def test_non_interactive_writes_same_profile_as_interactive(tmp_path, monkeypatch, ready_cli):
    """The answers-file path produces an identical profile to the interactive
    path (both funnel through write_files/profile_toml)."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SLEEPTALK_HOME", str(tmp_path / "home"))
    answers = wizard.answers_from_mapping(tomllib.loads(ANSWERS_TOML))
    lines = []
    code = await wizard.run_non_interactive(
        answers,
        live=False,
        out=lines.append,
    )
    assert code == 0, lines
    profile_path = tmp_path / "home" / "profiles" / "test-bot.toml"
    assert profile_path.is_file()
    # identical to what the interactive interview would have written, plus
    # references to the keys it put in the private store
    text = profile_path.read_text()
    assert text.startswith(wizard.profile_toml(answers).rstrip("\n"))
    assert "[credentials.refs]" in text
    assert any("sleeptalk run --config" in line for line in lines)


@pytest.mark.asyncio
async def test_non_interactive_missing_fields_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    lines = []
    monkeypatch.setenv("SLEEPTALK_HOME", str(tmp_path / "home"))
    code = await wizard.run_non_interactive(
        wizard.WizardAnswers(), live=False, out=lines.append
    )
    assert code == 1
    assert not (tmp_path / "home").exists()


def test_cli_answers_file(tmp_path, monkeypatch, ready_cli):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SLEEPTALK_HOME", str(tmp_path / "home"))
    from sleeptalk.cli import main

    answers_path = tmp_path / "answers.toml"
    answers_path.write_text(ANSWERS_TOML)
    code = main(["init", "--answers", str(answers_path), "--offline"])
    assert code == 0
    assert (tmp_path / "home" / "profiles" / "test-bot.toml").is_file()


def test_cli_flag_overrides_answers_file(tmp_path, monkeypatch, ready_cli):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SLEEPTALK_HOME", str(tmp_path / "home"))
    from sleeptalk.cli import main

    answers_path = tmp_path / "answers.toml"
    answers_path.write_text(ANSWERS_TOML)
    code = main([
        "init", "--answers", str(answers_path), "--offline",
        "--language", "fr-FR", "--slug", "custom-slug",
    ])
    assert code == 0
    profile = tomllib.loads(
        (tmp_path / "home" / "profiles" / "custom-slug.toml").read_text()
    )
    assert profile["stt"]["language"] == "fr-FR"
