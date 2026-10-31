from __future__ import annotations

import os

import tomllib
from pathlib import Path

import pytest

from sleeptalk import setup_core
from sleeptalk import wizard
from sleeptalk.config import SleeptalkConfig
from sleeptalk.defaults import register_defaults
from sleeptalk.registries import TRANSPORTS


@pytest.fixture
def ready_cli(monkeypatch):
    monkeypatch.setattr(
        "sleeptalk.registries.locate_binary",
        lambda binary, fallbacks=(): f"/usr/bin/{binary}",
    )


def _answers(**overrides) -> wizard.WizardAnswers:
    answers = wizard.WizardAnswers(
        surface="discord",
        name="Test Bot",
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


def test_hybrid_profile_uses_openai_route_and_omits_cascade_providers():
    answers = _answers(
        route_type="hybrid",
        realtime_key="openai-secret-1",
        realtime_model="gpt-realtime",
        realtime_voice="marin",
        speech_model="gpt-4o-mini-tts",
        transcription_model="gpt-4o-mini-transcribe",
    )

    text = wizard.profile_toml(answers)
    parsed = tomllib.loads(text)

    assert parsed["route"] == {
        "type": "hybrid",
        "provider": "openai-realtime",
        "model": "gpt-realtime",
        "voice": "marin",
        "speech_model": "gpt-4o-mini-tts",
        "transcription_model": "gpt-4o-mini-transcribe",
        "language": "es-ES",
    }
    assert "stt" not in parsed
    assert parsed["tts"] == {"speed": 1.0}
    assert "openai-secret-1" not in text
    assert wizard.env_entries(answers) == {
        "TEST_BOT_DISCORD_BOT_TOKEN": "secret-token-1",
        "OPENAI_API_KEY": "openai-secret-1",
    }
    config = SleeptalkConfig.model_validate(parsed)
    assert config.route.type == "hybrid"


@pytest.mark.parametrize("route_type", ["cascade", "hybrid"])
def test_new_profile_defaults_to_normal_speech_and_silent_narration(route_type):
    parsed = tomllib.loads(wizard.profile_toml(_answers(route_type=route_type)))

    assert parsed["policy"]["narration_style"] == "silence"
    assert parsed["tts"]["speed"] == 1.0


def test_existing_profile_without_voice_defaults_keeps_legacy_behavior(tmp_path):
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    (profiles / "legacy.toml").write_text(
        '[agent]\ntype = "pi"\nname = "Legacy"\nslug = "legacy"\n'
        'workspace_dir = "."\n\n[transport]\ntype = "web"\n'
    )

    loaded = wizard.load_existing_answers("legacy", profiles)

    assert loaded is not None
    assert loaded.narration_style == ""
    assert loaded.tts_speed == 1.0


def test_hybrid_required_fields_do_not_require_stt_or_tts():
    answers = wizard.WizardAnswers(
        surface="terminal",
        route_type="hybrid",
        name="Hybrid Bot",
        realtime_key="openai-key",
    )
    assert wizard.required_answer_fields(answers) == ("name", "realtime_key")
    assert wizard.missing_answer_fields(answers) == []


def test_hybrid_voice_engine_choices_are_explicit():
    assert wizard.voice_engine_choices() == (
        ("cascade", "Speech service: your agent answers, a speech service listens and speaks"),
        ("hybrid", "OpenAI Realtime: hears you and talks back itself"),
    )


def test_surface_transport_candidates() -> None:
    assert wizard.SURFACES == ("discord", "web", "terminal")
    assert wizard.transport_for_surface("discord") == "discord"
    assert wizard.transport_for_surface("web") == "web"
    assert wizard.transport_for_surface("browser") == "web"
    assert wizard.transport_for_surface("terminal") == "terminal"
    assert wizard.transport_for_surface("null") == "null"
    assert wizard.surface_for_transport("null") == "null"
    with pytest.raises(ValueError, match="unknown surface"):
        wizard.transport_for_surface("slack")


def test_new_wizard_answers_default_to_web() -> None:
    assert wizard.WizardAnswers().surface == "web"


def test_load_existing_null_profile_stays_null(tmp_path) -> None:
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    (profiles / "lab.toml").write_text(
        '[agent]\ntype = "pi"\nname = "Lab"\nslug = "lab"\n'
        'workspace_dir = "."\n\n[transport]\ntype = "null"\n'
    )
    loaded = wizard.load_existing_answers("lab", profiles)
    assert loaded is not None
    assert loaded.surface == "null"
    assert tomllib.loads(wizard.profile_toml(loaded))["transport"]["type"] == "null"


def test_null_profile_is_not_rewritten_to_web() -> None:
    parsed = tomllib.loads(wizard.profile_toml(_answers(surface="null")))
    assert parsed["transport"]["type"] == "null"
    assert "guild_id" not in parsed["transport"]
    assert "sleeptalk run --config" in wizard.launch_command(
        _answers(surface="null"), "config/profiles/bot.toml"
    )
    answers = wizard.WizardAnswers(surface="null", name="N", stt_key="dg", tts_key="tts")
    assert wizard.missing_answer_fields(answers) == []


def test_web_transport_is_registered() -> None:
    register_defaults()
    assert "web" in TRANSPORTS
    assert TRANSPORTS.get("web").name == "web"


def test_web_overlay_does_not_inherit_discord_keys(tmp_path) -> None:
    from sleeptalk.config import load_resolved_config

    (tmp_path / "sleeptalk.toml").write_text(
        '[transport]\ntype = "discord"\nguild_id = "1"\nclient_id = "2"\n'
        'token_env = "DISCORD_BOT_TOKEN"\n[agent]\ntype = "pi"\n'
    )
    profiles = tmp_path / "config" / "profiles"
    profiles.mkdir(parents=True)
    (profiles / "web-bot.toml").write_text(
        wizard.profile_toml(_answers(surface="web", name="Web Bot", slug="web-bot"))
    )
    config = load_resolved_config(
        profiles / "web-bot.toml", check_env=False, check_binaries=False
    )
    assert config.transport.type == "web"
    assert config.transport.guild_id == ""
    assert "guild_id" not in config.transport.model_fields_set


def test_web_profile_builds_the_web_transport(tmp_path) -> None:
    from sleeptalk.builder import build_session
    from sleeptalk.config import load_config
    from sleeptalk.transports.web import WebTransport

    path = tmp_path / "web.toml"
    path.write_text('[transport]\ntype = "web"\n[agent]\ntype = "pi"\n')
    config = load_config(path, check_env=False, check_binaries=False)
    session = build_session(config, env={})
    assert config.transport.type == "web"
    assert isinstance(session.transport, WebTransport)


def test_profile_toml_maps_each_surface_to_its_transport() -> None:
    for surface, transport in (
        ("discord", "discord"),
        ("web", "web"),
        ("terminal", "terminal"),
    ):
        parsed = tomllib.loads(wizard.profile_toml(_answers(surface=surface)))
        assert parsed["transport"]["type"] == transport
        SleeptalkConfig.model_validate(parsed)


def test_profile_toml_omits_discord_keys_on_web_and_terminal() -> None:
    for surface in ("web", "terminal"):
        parsed = tomllib.loads(wizard.profile_toml(_answers(surface=surface)))
        assert "guild_id" not in parsed["transport"]
        assert "client_id" not in parsed["transport"]
        assert "token_env" not in parsed["transport"]


def test_load_existing_hybrid_profile_and_secret(tmp_path):
    answers = _answers(
        route_type="hybrid",
        realtime_key="openai-secret-1",
        realtime_model="gpt-realtime",
        realtime_voice="marin",
        speech_model="gpt-4o-mini-tts",
        transcription_model="gpt-4o-mini-transcribe",
    )
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    (profiles / "test-bot.toml").write_text(wizard.profile_toml(answers))
    loaded = wizard.load_existing_answers("test-bot", profiles)

    assert loaded is not None
    assert loaded.route_type == "hybrid"
    assert loaded.language == "es-ES"
    assert loaded.realtime_model == "gpt-realtime"
    assert loaded.realtime_voice == "marin"
    assert loaded.speech_model == "gpt-4o-mini-tts"
    assert loaded.transcription_model == "gpt-4o-mini-transcribe"
    (tmp_path / ".env").write_text("OPENAI_API_KEY=openai-secret-2\n")
    wizard.load_existing_secrets(loaded, tmp_path / ".env")
    assert loaded.realtime_key == "openai-secret-2"
    assert loaded.stt_key == ""
    assert loaded.tts_key == ""


def test_env_entries_skip_discord_token_off_discord() -> None:
    web = wizard.env_entries(_answers(surface="web"))
    assert not any("DISCORD" in key for key in web)
    discord = wizard.env_entries(_answers(surface="discord"))
    assert "TEST_BOT_DISCORD_BOT_TOKEN" in discord


def test_missing_fields_web_and_terminal_skip_discord_creds() -> None:
    for surface in ("web", "terminal"):
        answers = wizard.WizardAnswers(
            surface=surface, name="Bot", stt_key="dg", tts_key="tts"
        )
        problems = wizard.missing_answer_fields(answers)
        assert problems == []


def test_missing_fields_discord_still_needs_token() -> None:
    problems = wizard.missing_answer_fields(wizard.WizardAnswers(surface="discord"))
    assert any("--token" in item for item in problems)
    assert any("guild_id" in item for item in problems)


def test_agent_menu_never_auto_picks_detected_pi() -> None:
    entries = wizard.agent_menu_entries({"pi": "/opt/homebrew/bin/pi"})
    values = [value for _label, value in entries]
    assert values[0] != "pi" or len(values) > 1
    assert "pi" in values
    assert "claude_code" in values
    assert "cursor" in values
    assert "codex" in values
    pi_label = next(label for label, value in entries if value == "pi")
    assert "detected" in pi_label.lower()


def test_profile_toml_does_not_write_cartesia_voice_uuid_as_model() -> None:
    voice = "db6b0ed5-d5d3-463d-ae85-518a07d3c2b4"
    parsed = tomllib.loads(
        wizard.profile_toml(
            _answers(tts_provider="cartesia", tts_model=voice, tts_voice_id=voice)
        )
    )
    assert parsed["tts"]["voice_id"] == voice
    assert parsed["tts"]["model"] == "sonic-3.6"


def test_profile_toml_does_not_write_elevenlabs_voice_id_as_model() -> None:
    voice = "vGQNBgLaiM3EdZtxIiuY"
    parsed = tomllib.loads(
        wizard.profile_toml(
            _answers(tts_provider="elevenlabs", tts_model=voice, tts_voice_id=voice)
        )
    )
    assert parsed["tts"]["voice_id"] == voice
    assert parsed["tts"]["model"] == "eleven_multilingual_v2"


def test_tts_wizard_providers_include_deepgram() -> None:
    aggregators = ("openrouter", "groq", "together", "venice")
    assert wizard.TTS_WIZARD_PROVIDERS == ("cartesia", "deepgram", "elevenlabs", *aggregators)
    assert wizard.STT_WIZARD_PROVIDERS == ("deepgram", "cartesia", "elevenlabs", *aggregators, "local")
    assert wizard.parse_tts_mode("explore") == "explore"
    assert wizard.parse_tts_mode("known") == "known"
    with pytest.raises(ValueError):
        wizard.parse_tts_mode("describe")


def test_missing_runtime_extra_names_terminal_install() -> None:
    import importlib.util

    reason = wizard.missing_runtime_extra("terminal")
    if importlib.util.find_spec("sounddevice") is None:
        assert reason is not None
        assert "sleeptalk[terminal]" in reason
    else:
        assert reason is None
    import importlib.util

    discord_extra = wizard.missing_runtime_extra("discord")
    if importlib.util.find_spec("discord") is None:
        assert discord_extra is not None
        assert "sleeptalk[discord]" in discord_extra
    else:
        assert discord_extra is None
    assert wizard.missing_runtime_extra("web") is None


def test_terminal_config_load_fails_without_sounddevice(tmp_path, monkeypatch) -> None:
    import importlib.util

    from sleeptalk.config import ConfigError, load_config

    real_find_spec = importlib.util.find_spec

    def fake_find_spec(name: str, package: str | None = None):
        if name == "sounddevice":
            return None
        return real_find_spec(name, package)

    monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)
    monkeypatch.setattr(
        "sleeptalk.config.collect_missing_binaries", lambda _config, **_kwargs: []
    )
    path = tmp_path / "terminal.toml"
    path.write_text('[transport]\ntype = "terminal"\n[agent]\ntype = "pi"\n')
    with pytest.raises(ConfigError, match=r"sleeptalk\[terminal\]"):
        load_config(path, check_env=False, check_binaries=True)
    web = tmp_path / "web.toml"
    web.write_text('[transport]\ntype = "web"\n[agent]\ntype = "pi"\n')
    assert load_config(web, check_env=False, check_binaries=True).transport.type == "web"


def test_system_deps_libopus_is_discord_only() -> None:
    assert "libopus" in wizard.system_dep_names("discord")
    assert "libopus" not in wizard.system_dep_names("web")
    assert "libopus" not in wizard.system_dep_names("terminal")
    assert "libopus" not in wizard.system_dep_names("null")
    assert wizard.can_auto_install(["brew", "install", "opus"]) is True
    assert wizard.can_auto_install(["sudo", "apt-get", "install", "-y", "libopus0"]) is False
    assert wizard.can_auto_install(None) is False


def test_launch_command_uses_link_for_web() -> None:
    profile = "config/profiles/bot.toml"
    assert "sleeptalk link --config" in wizard.launch_command(
        _answers(surface="web"), profile
    )
    assert "sleeptalk run --config" in wizard.launch_command(
        _answers(surface="discord"), profile
    )
    assert "sleeptalk run --config" in wizard.launch_command(
        _answers(surface="terminal"), profile
    )


def test_launch_command_web_is_package_owned() -> None:
    line = wizard.launch_command(_answers(surface="web"), "config/profiles/bot.toml")
    assert line.startswith("sleeptalk link --config")
    assert "--foreground" in line
    assert "server" not in line
    assert "cd " not in line


def test_resolve_rendezvous_env_wins_over_files(tmp_path) -> None:
    from sleeptalk.wizard import resolve_rendezvous_url

    home = tmp_path / "home.env"
    home.write_text("SLEEPTALK_RENDEZVOUS_URL=https://home.example\n")
    assert (
        resolve_rendezvous_url({"SLEEPTALK_RENDEZVOUS_URL": "https://env.example"}, home)
        == "https://env.example"
    )


def test_resolve_rendezvous_reads_home_env(tmp_path) -> None:
    from sleeptalk.wizard import resolve_rendezvous_url

    home = tmp_path / "home.env"
    home.write_text("SLEEPTALK_RENDEZVOUS_URL=https://home.example\n")
    assert resolve_rendezvous_url({}, home) == "https://home.example"


def test_resolve_rendezvous_fallback_is_hosted() -> None:
    from sleeptalk.call_provider import HOSTED_RENDEZVOUS_URL
    from sleeptalk.wizard import resolve_rendezvous_url

    assert resolve_rendezvous_url({}, Path("/nope")) == HOSTED_RENDEZVOUS_URL
    assert HOSTED_RENDEZVOUS_URL == "https://call.sleep.talk"


def test_answers_file_accepts_surface_and_tts_mode() -> None:
    answers = wizard.answers_from_mapping(
        {
            "name": "Web Bot",
            "surface": "web",
            "agent_type": "codex",
            "stt_key": "dg",
            "tts_key": "tts",
            "tts_provider": "elevenlabs",
            "tts_mode": "known",
            "tts_voice_id": "voice-9",
            "tts_model": "eleven_multilingual_v2",
        }
    )
    assert answers.surface == "web"
    assert answers.agent_type == "codex"
    assert answers.tts_mode == "known"
    parsed = tomllib.loads(wizard.profile_toml(answers))
    assert parsed["transport"]["type"] == "web"
    assert parsed["agent"]["type"] == "codex"
    assert parsed["tts"]["provider"] == "elevenlabs"


@pytest.mark.asyncio
async def test_non_interactive_terminal_reports_missing_extra(
    tmp_path, monkeypatch, ready_cli
) -> None:
    import importlib.util

    real_find_spec = importlib.util.find_spec

    def fake_find_spec(name: str, package: str | None = None):
        if name == "sounddevice":
            return None
        return real_find_spec(name, package)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SLEEPTALK_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)
    monkeypatch.setattr(
        "sleeptalk.sysdeps.run_install", lambda _cmd: (False, "not installed")
    )
    answers = wizard.answers_from_mapping(
        {
            "name": "Term Bot",
            "surface": "terminal",
            "stt_key": "dg",
            "tts_key": "tts",
            "tts_voice_id": "voice-1",
        }
    )
    lines: list[str] = []
    code = await wizard.run_non_interactive(answers, live=False, out=lines.append)
    assert code == 1, lines
    assert any("sleeptalk[terminal]" in line for line in lines)
    home = Path(os.environ["SLEEPTALK_HOME"])
    assert list(home.rglob("*.toml")) == []
    assert not (home / ".env").exists()



@pytest.mark.asyncio
async def test_non_interactive_web_writes_and_prints_link(
    tmp_path, monkeypatch, ready_cli
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SLEEPTALK_HOME", str(tmp_path / "home"))
    answers = wizard.answers_from_mapping(
        {
            "name": "Web Bot",
            "surface": "web",
            "stt_key": "dg",
            "tts_key": "tts",
            "tts_voice_id": "voice-1",
        }
    )
    lines: list[str] = []
    code = await wizard.run_non_interactive(answers, live=False, out=lines.append)
    assert code == 0, lines
    profile_path = tmp_path / "home" / "profiles" / "web-bot.toml"
    assert profile_path.is_file()
    assert tomllib.loads(profile_path.read_text())["transport"]["type"] == "web"
    assert any("sleeptalk link --config" in line for line in lines)
    assert not any(
        line.startswith("run it: sleeptalk run") for line in lines
    )


@pytest.mark.asyncio
async def test_non_interactive_hybrid_writes_without_cascade_providers(
    tmp_path, monkeypatch, ready_cli
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SLEEPTALK_HOME", str(tmp_path / "home"))
    answers = wizard.answers_from_mapping(
        {
                "name": "Hybrid Bot",
                "surface": "web",
                "route_type": "hybrid",
            "realtime_key": "openai-secret-1",
            "realtime_model": "gpt-realtime",
            "realtime_voice": "marin",
            "speech_model": "gpt-4o-mini-tts",
            "transcription_model": "gpt-4o-mini-transcribe",
        }
    )
    lines: list[str] = []

    code = await wizard.run_non_interactive(answers, live=False, out=lines.append)

    assert code == 0, lines
    profile_path = tmp_path / "home" / "profiles" / "hybrid-bot.toml"
    parsed = tomllib.loads(profile_path.read_text())
    assert parsed["route"]["type"] == "hybrid"
    assert "stt" not in parsed
    assert parsed["tts"] == {"speed": 1.0}
    assert not (tmp_path / "home" / ".env").exists()
    assert wizard._stored_secrets(profile_path) == {"OPENAI_API_KEY": "openai-secret-1"}


@pytest.mark.asyncio
async def test_hybrid_live_validation_uses_patchable_connectivity_hook(monkeypatch, ready_cli):
    answers = _answers(
        surface="terminal", route_type="hybrid", realtime_key="openai-secret-1"
    )
    called: list[str] = []

    async def fail(_answers):
        called.append("hybrid")
        raise RuntimeError("OpenAI Realtime is unavailable")

    monkeypatch.setattr(setup_core, "hybrid_connectivity_check", fail)

    problems = await wizard.validate_answers(answers, live=True)

    assert called == ["hybrid"]
    assert problems == ["OpenAI Realtime is unavailable"]


@pytest.mark.asyncio
async def test_hybrid_connectivity_check_uses_the_selected_models(monkeypatch):
    answers = _answers(
        surface="terminal",
        route_type="hybrid",
        realtime_key="openai-secret-1",
        realtime_model="gpt-realtime-2.1",
        realtime_voice="marin",
        speech_model="gpt-4o-mini-tts",
        transcription_model="gpt-4o-mini-transcribe",
    )
    seen = []

    async def check(config):
        seen.append(config)

    monkeypatch.setattr("sleeptalk.openai_realtime.check_openai_connectivity", check)

    await wizard.hybrid_connectivity_check(answers)

    assert seen[0].api_key == "openai-secret-1"
    assert seen[0].model == "gpt-realtime-2.1"
    assert seen[0].voice == "marin"
    assert seen[0].speech_model == "gpt-4o-mini-tts"
    assert seen[0].transcription_model == "gpt-4o-mini-transcribe"


def test_edit_section_labels_start_with_surface() -> None:
    keys = [key for key, _label in wizard.edit_section_labels()]
    assert keys[0] == "surface"
    assert "agent" in keys
    assert "voice" in keys


def test_create_slug_leaves_an_edit_alone(tmp_path) -> None:
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    (profiles / "martha.toml").write_text("[agent]\nname = \"martha\"\n")
    answers = wizard.WizardAnswers(name="Martha Two", slug="martha")
    wizard.apply_create_slug(answers, profiles, editing=True)
    assert answers.slug == "martha"


def test_create_slug_uses_name_when_other_profiles_exist(tmp_path) -> None:
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    (profiles / "martha.toml").write_text("[agent]\nname = \"martha\"\n")
    answers = wizard.WizardAnswers(name="Alpha Bot")
    wizard.apply_create_slug(answers, profiles)
    assert answers.slug == "alpha-bot"


def test_create_slug_first_profile_uses_name(tmp_path) -> None:
    answers = wizard.WizardAnswers(name="Alpha Bot")
    wizard.apply_create_slug(answers, tmp_path / "missing-profiles")
    assert answers.slug == "alpha-bot"


def test_create_slug_avoids_a_taken_filename(tmp_path) -> None:
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    (profiles / "alpha-bot.toml").write_text("[agent]\nname = \"Alpha Bot\"\n")
    answers = wizard.WizardAnswers(name="Alpha Bot")
    wizard.apply_create_slug(answers, profiles)
    assert answers.slug == "alpha-bot-2"


def test_create_write_does_not_overwrite_sleeptalk_toml(tmp_path) -> None:
    home = tmp_path / "home"
    profiles = home / "profiles"
    profiles.mkdir(parents=True)
    (profiles / "martha.toml").write_text("[agent]\nname = \"martha\"\nslug = \"martha\"\n")
    first = wizard.WizardAnswers(
        name="Alpha Bot",
        surface="web",
        stt_key="dg",
        tts_key="tts",
        tts_voice_id="voice-1",
    )
    wizard.apply_create_slug(first, profiles)
    wizard.write_files(first, profiles_dir=profiles, env_path=home / ".env")
    second = wizard.WizardAnswers(
        name="Bravo Bot",
        surface="web",
        stt_key="dg",
        tts_key="tts",
        tts_voice_id="voice-1",
    )
    wizard.apply_create_slug(second, profiles)
    wizard.write_files(second, profiles_dir=profiles, env_path=home / ".env")
    names = sorted(path.name for path in profiles.glob("*.toml"))
    assert names == ["alpha-bot.toml", "bravo-bot.toml", "martha.toml"]
    assert not (profiles / "sleeptalk.toml").exists()


def test_write_files_sets_env_mode_0600_and_second_write_is_noop(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    home = tmp_path / "home"
    profiles = home / "profiles"
    env_path = home / ".env"
    answers = _answers(surface="web", guild_id="", client_id="", token="")
    # A .env left readable by others is tightened even when keys move out of it.
    env_path.parent.mkdir(parents=True)
    env_path.write_text("UNRELATED=keep\n")
    env_path.chmod(0o644)
    written = wizard.write_files(answers, profiles_dir=profiles, env_path=env_path)
    assert any(str(path).endswith(".toml") for path in written)
    assert (env_path.stat().st_mode & 0o777) == 0o600
    assert env_path.read_text() == "UNRELATED=keep\n"
    profile = next(profiles.glob("*.toml"))
    before = (
        profile.read_bytes(),
        env_path.read_bytes(),
        profile.stat().st_mtime_ns,
        env_path.stat().st_mtime_ns,
    )
    again = wizard.write_files(answers, profiles_dir=profiles, env_path=env_path)
    assert again == []
    after = (
        profile.read_bytes(),
        env_path.read_bytes(),
        profile.stat().st_mtime_ns,
        env_path.stat().st_mtime_ns,
    )
    assert before == after


def test_write_files_recovers_after_profile_commit_is_interrupted(
    tmp_path, monkeypatch
) -> None:
    home = tmp_path / "home"
    profiles = home / "profiles"
    env_path = home / ".env"
    answers = _answers(surface="web", guild_id="", client_id="", token="")
    profile_path = profiles / f"{answers.slug}.toml"
    real_replace = os.replace
    failed_once = False

    def interrupt_profile_commit(source, target):
        nonlocal failed_once
        if Path(target) == profile_path and not failed_once:
            failed_once = True
            raise OSError("interrupted profile commit")
        return real_replace(source, target)

    monkeypatch.setattr(os, "replace", interrupt_profile_commit)

    with pytest.raises(OSError, match="interrupted profile commit"):
        wizard.write_files(answers, profiles_dir=profiles, env_path=env_path)

    assert not profile_path.exists()
    assert not env_path.exists() or "dg-secret-1" not in env_path.read_text(encoding="utf-8")

    written = wizard.write_files(answers, profiles_dir=profiles, env_path=env_path)

    assert str(profile_path) in written
    assert profile_path.is_file()
    assert wizard._stored_secrets(profile_path)["DEEPGRAM_API_KEY"] == "dg-secret-1"


@pytest.mark.asyncio
async def test_offline_init_does_not_call_run_install(
    tmp_path, monkeypatch, ready_cli
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SLEEPTALK_HOME", str(tmp_path / "home"))
    calls: list[list[str]] = []

    def boom(cmd):
        calls.append(list(cmd))
        raise AssertionError(f"install invoked offline: {cmd}")

    monkeypatch.setattr("sleeptalk.sysdeps.run_install", boom)
    answers = wizard.answers_from_mapping(
        {
            "name": "Web Bot",
            "surface": "web",
            "stt_key": "dg",
            "tts_key": "tts",
            "tts_voice_id": "voice-1",
        }
    )
    code = await wizard.run_non_interactive(answers, live=False, out=lambda _line: None)
    assert code == 0
    assert calls == []


@pytest.mark.asyncio
async def test_shared_setup_blocks_on_required_surface_system_dependency(
    tmp_path, monkeypatch
) -> None:
    from sleeptalk import setup, sysdeps

    answers = _answers(surface="discord")

    async def valid(_answers, *, live=True):
        return []

    monkeypatch.setattr("sleeptalk.setup_core.validate_answers", valid)
    monkeypatch.setattr(setup_core, "missing_runtime_extra", lambda _surface: None)
    monkeypatch.setattr(
        sysdeps,
        "ensure_required_for_surface",
        lambda _surface, *, repair: ["libopus missing — install libopus"],
        raising=False,
    )

    result = await setup.ensure_setup(
        answers,
        profiles_dir=tmp_path / "profiles",
        env_path=tmp_path / ".env",
        live=False,
    )

    assert result.status == "needs_input"
    assert result.phase == "dependencies"
    assert result.messages == ("libopus missing — install libopus",)
    assert not (tmp_path / "profiles" / f"{answers.slug}.toml").exists()


def test_advanced_settings_do_not_repeat_the_main_menu() -> None:
    advanced = [key for key, _label in wizard.advanced_section_labels()]
    assert not set(advanced) & set(wizard.MAIN_MENU_SECTIONS)
    everything = {key for key, _label in wizard.edit_section_labels()}
    assert set(advanced) | set(wizard.MAIN_MENU_SECTIONS) == everything
    assert advanced[:3] == ["language", "models", "speed"]  # speech settings stay together


def test_answers_file_keeps_the_models_it_names_for_a_speech_provider() -> None:
    answers = wizard.answers_from_mapping({
        "name": "Chinese", "agent_type": "pi", "language": "zh-CN", "speech_provider": "openrouter",
        "speech_key": "sk-or-key", "stt_model": "microsoft/mai-transcribe-2",
        "tts_model": "qwen/qwen-audio-3.0-tts-flash"})
    assert answers.stt_model == "microsoft/mai-transcribe-2"
    assert answers.tts_model == "qwen/qwen-audio-3.0-tts-flash"


def test_with_no_agent_installed_the_menu_starts_on_the_default(monkeypatch) -> None:
    """Fresh machine: "Just a model" is always ready and used to sort to the
    top, so the cursor sat on it while oh-my-pi was labelled the default."""
    from sleeptalk.agents import readiness
    from sleeptalk.wizard import agent_menu_entries

    from types import SimpleNamespace

    def probe(name):
        ready = name == "model"
        return SimpleNamespace(ready=ready, menu_suffix=lambda: "ready" if ready else "not installed")

    monkeypatch.setattr(readiness, "probe", probe)
    assert agent_menu_entries(ready_first=True)[0][1] == "omp"


def test_a_profile_can_open_calls_on_this_computer():
    """`delivery = "local"`: the settings page's This computer, written by the shared profile writer."""
    answers = _answers(route_type="cascade")
    answers.surface, answers.delivery = "web", "local"
    parsed = tomllib.loads(wizard.profile_toml(answers))
    assert parsed["transport"]["options"]["delivery"] == "local"
