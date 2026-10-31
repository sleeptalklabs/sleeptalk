from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import tomllib

import pytest

from sleeptalk import setup_core
from sleeptalk import cli, launcher, wizard
from sleeptalk.credential_store import CredentialStore
from sleeptalk.interactive_profile import InteractiveProfile


@pytest.fixture
def setup_home(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    monkeypatch.setenv("SLEEPTALK_HOME", str(tmp_path / "new-home"))
    for key in ("DEEPGRAM_API_KEY", "CARTESIA_API_KEY", "ELEVENLABS_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr("sleeptalk.credential_store.default_root", lambda: tmp_path / "private")
    monkeypatch.setattr("sleeptalk.runtime_binding.host_identity", lambda: "a" * 64)
    monkeypatch.setattr("sleeptalk.agents.readiness.probe", lambda _: SimpleNamespace(ready=True))
    monkeypatch.setattr(setup_core, "agent_menu_entries", lambda **_: [("Codex ready", "codex")])
    monkeypatch.setattr(setup_core, "validate_answers", AsyncMock(return_value=[]))
    monkeypatch.setattr("sleeptalk.setup.ensure_dependencies", Mock(return_value=()))
    monkeypatch.setattr("sleeptalk.stt_roundtrip.deepgram_roundtrip", AsyncMock(return_value=("hello", 10)))
    monkeypatch.setattr("sleeptalk.stt_roundtrip.cartesia_roundtrip", AsyncMock(return_value=("hello", 11)))
    monkeypatch.setattr("sleeptalk.stt_roundtrip.elevenlabs_roundtrip", AsyncMock(return_value=("hello", 12)))

    async def prove(answers):
        assert answers.tts_voice_id
        sample = tmp_path / "sample.wav"
        sample.write_bytes(b"sample")
        return sample

    monkeypatch.setattr(setup_core, "_prove_selected_voice", prove)
    return workspace


def script(monkeypatch, *, selects=(), texts=()):
    prompts = []
    choices = iter(selects)
    values = iter(texts)

    def select(prompt, **kwargs):
        prompts.append(prompt)
        value = next(choices)
        return SimpleNamespace(ask_async=AsyncMock(return_value=value))

    async def text(prompt, **kwargs):
        prompts.append(prompt)
        return next(values)

    monkeypatch.setattr("questionary.select", select)
    monkeypatch.setattr("questionary.confirm", Mock(side_effect=AssertionError("No launch confirmation needed")))
    monkeypatch.setattr(wizard, "_ask_text", text)
    return prompts


async def test_first_run_asks_only_agent_one_provider_one_key_and_destination(setup_home, monkeypatch):
    prompts = script(monkeypatch, selects=["codex", "deepgram", "web"], texts=["deepgram-test"])
    path, surface = await wizard._configure(workspace=str(setup_home))
    assert surface == "web"
    assert len(prompts) == 4
    document = tomllib.loads(path.read_text())
    assert document["tts"]["provider"] == "deepgram"
    assert document["tts"]["voice_id"] == wizard.DEFAULT_DEEPGRAM_VOICE
    assert document["stt"]["provider"] == "deepgram"
    assert document["agent"]["workspace_dir"] == str(setup_home)
    assert document["route"]["type"] == "cascade"
    assert set(document["credentials"]["refs"]) == {"DEEPGRAM_API_KEY"}
    assert "deepgram-test" not in path.read_text()
    assert not (launcher.resolve_layout().root / ".env").exists()
    assert launcher.selected_profile(setup_home) == path
    assert launcher.selected_surface(setup_home) == "web"
    from sleeptalk.config import load_resolved_config
    assert load_resolved_config(path, check_env=False, check_binaries=False).binding is not None


async def test_interrupted_setup_keeps_validated_key_and_resumes(setup_home, monkeypatch):
    # The key validates, then the call-destination prompt is abandoned (Ctrl-C).
    script(monkeypatch, selects=["codex", "deepgram", None], texts=["deepgram-test"])
    assert await wizard._configure(workspace=str(setup_home)) == 1
    assert CredentialStore().default_reference("deepgram")
    assert launcher.selected_profile(setup_home) is None
    prompts = script(monkeypatch, selects=["codex", "deepgram", "terminal"])
    _, surface = await wizard._configure(workspace=str(setup_home))
    assert surface == "terminal"
    assert not any("API key" in p for p in prompts)


async def test_saved_keys_reused_in_second_workspace(setup_home, monkeypatch):
    script(monkeypatch, selects=["codex", "deepgram", "web"], texts=["deepgram-test"])
    first, _ = await wizard._configure(workspace=str(setup_home))
    second_workspace = setup_home.parent / "another"
    second_workspace.mkdir()
    monkeypatch.chdir(second_workspace)
    prompts = script(monkeypatch, selects=["codex", "deepgram", "terminal"])
    second, _ = await wizard._configure(workspace=str(second_workspace))
    assert first != second
    assert len(prompts) == 3
    assert tomllib.loads(first.read_text())["credentials"] == tomllib.loads(second.read_text())["credentials"]
    assert launcher.selected_profile(setup_home) == first


async def test_settings_save_does_not_launch_or_reset_default(setup_home, monkeypatch):
    script(monkeypatch, selects=["codex", "deepgram", "web"], texts=["deepgram-test"])
    path, _ = await wizard._configure(workspace=str(setup_home))
    # A profile can retain Discord details while browser remains the preferred call.
    text = path.read_text().replace('"type" = "web"', '"type" = "discord"\n"guild_id" = "123"\n"client_id" = "456"')
    path.write_text(text)
    prompts = script(monkeypatch, selects=[wizard.EDIT_DONE, wizard.LAUNCH_LATER])
    assert await wizard._configure(profile=path.stem, settings=True, workspace=str(setup_home)) == 0
    assert prompts == ["What do you want to change?", "Start a call now?"]
    assert launcher.selected_surface(setup_home) == "web"
    assert tomllib.loads(path.read_text())["transport"]["guild_id"] == "123"


async def test_discord_override_adds_connection_without_changing_default(setup_home, monkeypatch):
    script(monkeypatch, selects=["codex", "deepgram", "web"], texts=["deepgram-test"])
    path, _ = await wizard._configure(workspace=str(setup_home))
    prompts = script(monkeypatch, selects=["open"], texts=["discord-test"])
    _stub_discord(monkeypatch)
    assert await wizard._configure(profile=path.stem, surface="discord", workspace=str(setup_home)) == (path, "discord")
    assert launcher.selected_surface(setup_home) == "web"
    doc = tomllib.loads(path.read_text())
    assert doc["transport"]["type"] == "discord"
    assert doc["transport"]["guild_id"] == "123"
    assert doc["transport"]["client_id"] == "456", "application ID comes from the token, not a prompt"
    assert doc["transport"]["authorization_required"] is False, "open mode is written explicitly"
    assert "Application ID" not in " ".join(prompts)
    assert "discord-test" not in path.read_text()
    assert not launcher.resolve_layout().env_path.exists()


def _stub_discord(monkeypatch):
    from sleeptalk.discord_check import ApplicationIdentity, VoiceChannel

    monkeypatch.setattr("questionary.confirm", lambda *a, **k: SimpleNamespace(ask_async=AsyncMock(return_value=True)))
    monkeypatch.setattr(wizard.discord_check, "check_token", AsyncMock(return_value="Bot"))
    monkeypatch.setattr(wizard.discord_check, "fetch_application", AsyncMock(return_value=ApplicationIdentity("456", "Bot", "https://invite")))
    monkeypatch.setattr(wizard.discord_check, "list_guilds", AsyncMock(return_value=[SimpleNamespace(id="123", name="Server")]))
    monkeypatch.setattr(wizard.discord_check, "check_guild_membership", AsyncMock())
    monkeypatch.setattr(wizard.discord_check, "probe_gateway_intents", AsyncMock())
    monkeypatch.setattr(wizard.discord_check, "list_voice_channels", AsyncMock(return_value=[
        VoiceChannel("20", "voice", True, ()),
        VoiceChannel("21", "locked", False, ("connect",)),
    ]))


async def test_discord_setup_defaults_to_only_you_in_one_channel(setup_home, monkeypatch):
    script(monkeypatch, selects=["codex", "deepgram", "web"], texts=["deepgram-test"])
    path, _ = await wizard._configure(workspace=str(setup_home))
    prompts = script(monkeypatch, selects=["restricted", "20"], texts=["discord-test", "123456789012345678"])
    _stub_discord(monkeypatch)
    assert await wizard._configure(profile=path.stem, surface="discord", workspace=str(setup_home)) == (path, "discord")
    transport = tomllib.loads(path.read_text())["transport"]
    assert transport["authorization_required"] is True
    assert transport["operator_ids"] == "123456789012345678"
    assert transport["allowed_channel_ids"] == "20"
    assert "Who can talk to this bot?" in prompts
    # The restriction survives a settings round trip.
    answers = wizard.load_existing_answers(path.stem, path.parent)
    assert answers.authorization_required and answers.operator_ids == "123456789012345678"


async def test_discord_user_id_must_be_numeric(setup_home, monkeypatch):
    script(monkeypatch, selects=["codex", "deepgram", "web"], texts=["deepgram-test"])
    path, _ = await wizard._configure(workspace=str(setup_home))
    script(monkeypatch, selects=["restricted", "20"], texts=["discord-test", "operator#1234", "123456789012345678"])
    _stub_discord(monkeypatch)
    assert await wizard._configure(profile=path.stem, surface="discord", workspace=str(setup_home)) == (path, "discord")
    assert tomllib.loads(path.read_text())["transport"]["operator_ids"] == "123456789012345678"


async def test_back_from_voice_settings_preserves_original_provider(setup_home, monkeypatch):
    script(monkeypatch, selects=["codex", "deepgram", "web"], texts=["deepgram-test"])
    path, _ = await wizard._configure(workspace=str(setup_home))
    original = tomllib.loads(path.read_text())
    script(monkeypatch, selects=["voice", "elevenlabs", wizard._BACK, wizard.EDIT_DONE, wizard.LAUNCH_LATER])
    assert await wizard._configure(profile=path.stem, settings=True, workspace=str(setup_home)) == 0
    saved = tomllib.loads(path.read_text())
    assert saved["tts"] == original["tts"]
    assert saved["credentials"] == original["credentials"]


async def test_settings_keeps_selected_filename_when_bot_slug_differs(setup_home, monkeypatch):
    script(monkeypatch, selects=["codex", "deepgram", "web"], texts=["deepgram-test"])
    path, _ = await wizard._configure(workspace=str(setup_home))
    renamed = path.with_name("my-browser-profile.toml")
    path.rename(renamed)
    script(monkeypatch, selects=[wizard.EDIT_DONE, wizard.LAUNCH_LATER])
    assert await wizard._configure(profile=renamed.stem, settings=True, workspace=str(setup_home)) == 0
    assert not path.exists()
    assert launcher.selected_profile(setup_home) == renamed


def test_default_reference_reuse_checks_provider_and_removed_keys(setup_home):
    from sleeptalk.credential_store import CredentialError

    store = CredentialStore()
    ref = store.put_validated("deepgram", "key")
    store.set_default_reference("deepgram", ref)
    assert CredentialStore().default_reference("deepgram") == ref
    with pytest.raises(CredentialError, match="another provider"):
        store.set_default_reference("cartesia", ref)
    store.remove(ref, affected_profiles=[])
    assert store.default_reference("deepgram") is None


def test_profile_edit_preserves_inherited_binding_and_unknown_settings(setup_home):
    home = launcher.resolve_layout()
    home.profiles_dir.mkdir(parents=True)
    home.root_config.write_text('[agent]\nmodel = "inherited-model"\n[policy]\nfillers = false\n')
    answers = wizard.WizardAnswers(agent_type="hermes", workspace_dir=str(setup_home), surface="web", tts_voice_id="voice")
    path = home.profiles_dir / "sleeptalk.toml"
    path.write_text(wizard.profile_toml(answers) + '\n[agent.auth]\nmode = "api_key"\nprotocol = "openai-completions"\nbase_url = "https://api.example.com/v1"\ncredential_ref = "credential:' + '1' * 32 + '"\n')
    state = InteractiveProfile(path, answers)
    answers.narration_style = "silence"
    state.write(answers, env_path=home.env_path)
    from sleeptalk.config import load_resolved_config
    config = load_resolved_config(path, check_env=False, check_binaries=False)
    assert config.agent.model == "inherited-model"
    assert config.agent.auth.credential_ref == "credential:" + "1" * 32
    assert config.policy.narration_style == "silence"
    assert config.policy.fillers is False


async def test_settings_preserves_legacy_profile_key_over_global_default(setup_home, monkeypatch):
    home = launcher.resolve_layout()
    home.profiles_dir.mkdir(parents=True)
    home.root_config.write_text('[transport]\ntype = "web"\n')
    home.env_path.write_text('DEEPGRAM_API_KEY=owned-deepgram\nCARTESIA_API_KEY=owned-cartesia\n')
    answers = wizard.WizardAnswers(workspace_dir=str(setup_home), tts_voice_id="voice")
    path = home.profiles_dir / "sleeptalk.toml"
    path.write_text(wizard.profile_toml(answers))
    store = CredentialStore()
    store.set_default_reference("deepgram", store.put_validated("deepgram", "other-account"))
    script(monkeypatch, selects=[wizard.EDIT_DONE, wizard.LAUNCH_LATER])
    assert await wizard._configure(profile=path.stem, settings=True, workspace=str(setup_home)) == 0
    from dotenv import dotenv_values
    assert dotenv_values(home.env_path, interpolate=False)["DEEPGRAM_API_KEY"] == "owned-deepgram"
    assert "DEEPGRAM_API_KEY" not in tomllib.loads(path.read_text())["credentials"]["refs"]


@pytest.mark.parametrize("arguments,settings,surface", [([], False, None), (["settings"], True, None), (["--web"], False, "web"), (["--terminal"], False, "terminal"), (["--discord"], False, "discord")])
def test_cli_front_door(arguments, settings, surface, monkeypatch):
    run = Mock(return_value=0)
    monkeypatch.setattr(launcher, "run", run)
    assert cli.main(arguments) == 0
    run.assert_called_once_with(settings=settings, surface=surface)


def test_sync_wizard_launches_after_setup_loop_closes(setup_home, monkeypatch):
    import asyncio

    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    path = Path("test.toml")
    monkeypatch.setattr(wizard, "_configure", AsyncMock(return_value=(path, "terminal")))

    def launch(selected, *, surface):
        with pytest.raises(RuntimeError):
            asyncio.get_running_loop()
        assert selected == path and surface == "terminal"
        return 0

    monkeypatch.setattr(launcher, "launch", launch)
    assert wizard.run_init() == 0


async def test_settings_switches_speech_provider_to_cartesia_with_one_key(setup_home, monkeypatch):
    script(monkeypatch, selects=["codex", "deepgram", "web"], texts=["deepgram-test"])
    path, _ = await wizard._configure(workspace=str(setup_home))
    prompts = script(monkeypatch, selects=["speech", "cartesia", wizard.EDIT_DONE, wizard.LAUNCH_LATER], texts=["cartesia-test"])
    assert await wizard._configure(profile=path.stem, settings=True, workspace=str(setup_home)) == 0
    assert prompts == ["What do you want to change?", "Speech service (listening and speaking):",
                       "Cartesia API key:", "What do you want to change?", "Start a call now?"]
    document = tomllib.loads(path.read_text())
    assert (document["stt"]["provider"], document["stt"]["model"]) == ("cartesia", "ink-2")
    assert (document["tts"]["provider"], document["tts"]["voice_id"]) == ("cartesia", wizard.DEFAULT_CARTESIA_VOICE)
    assert set(document["credentials"]["refs"]) == {"CARTESIA_API_KEY"}


async def test_settings_voice_picks_within_the_service_in_use(setup_home, monkeypatch):
    """One service for both directions, as on the settings page: Voice lists that
    service's voices. It used to ask for a provider and crash on OpenRouter."""
    monkeypatch.setattr("sleeptalk.stt_roundtrip.openai_audio_roundtrip", AsyncMock(return_value=("hello", 13)))
    script(monkeypatch, selects=["codex", "openrouter", "web"], texts=["or-test"])
    path, _ = await wizard._configure(workspace=str(setup_home))
    played = []

    async def play(answers, voice_id, console):
        played.append(voice_id)
        return True

    monkeypatch.setattr(wizard, "_play_sample", play)
    monkeypatch.setattr("questionary.confirm", lambda *a, **k: SimpleNamespace(ask_async=AsyncMock(return_value=True)))
    prompts = script(monkeypatch, selects=["voice", "ara", wizard.EDIT_DONE, wizard.LAUNCH_LATER])
    monkeypatch.setattr("questionary.confirm", lambda *a, **k: SimpleNamespace(ask_async=AsyncMock(return_value=True)))
    assert await wizard._configure(profile=path.stem, settings=True, workspace=str(setup_home)) == 0
    assert "Pick a voice (each plays a sample):" in prompts and "Which voice provider?" not in prompts
    assert played == ["ara"]
    document = tomllib.loads(path.read_text())
    assert (document["stt"]["provider"], document["tts"]["provider"], document["tts"]["voice_id"]) == ("openrouter", "openrouter", "ara")


async def test_settings_speech_menu_offers_every_service_and_no_mixing(setup_home, monkeypatch):
    offered = {}

    def select(prompt, **kwargs):
        offered[prompt] = [choice.value for choice in kwargs["choices"]]
        return SimpleNamespace(ask_async=AsyncMock(return_value=wizard._BACK))

    script(monkeypatch, selects=["codex", "deepgram", "web"], texts=["deepgram-test"])
    path, _ = await wizard._configure(workspace=str(setup_home))
    script(monkeypatch, selects=["speech", wizard.EDIT_DONE, wizard.LAUNCH_LATER])
    original = __import__("questionary").select
    calls = iter([original, select, original, original])
    monkeypatch.setattr("questionary.select", lambda prompt, **kwargs: next(calls)(prompt, **kwargs))
    assert await wizard._configure(profile=path.stem, settings=True, workspace=str(setup_home)) == 0
    services = offered["Speech service (listening and speaking):"]
    assert set(wizard.SPEECH_SERVICES) | {"realtime", wizard._BACK} == set(services)
    assert "mixed" not in services
    # No key found here: Local leads, as on the settings page.
    assert services[0] == "local"


def test_local_sits_right_under_found_keys_in_the_terminal_menu():
    order = wizard.speech_menu_order()
    assert order[0] == "local" and sorted(order) == sorted(wizard.SPEECH_SERVICES)


def test_answers_file_speech_provider_populates_both_directions():
    answers = wizard.answers_from_mapping({"name": "bot", "speech_provider": "cartesia", "speech_key": "k"})
    assert (answers.stt_provider, answers.stt_model, answers.tts_provider) == ("cartesia", "ink-2", "cartesia")
    assert (answers.stt_key, answers.tts_key, answers.tts_voice_id) == ("k", "k", wizard.DEFAULT_CARTESIA_VOICE)
    with pytest.raises(ValueError, match="speech_provider"):
        wizard.answers_from_mapping({"name": "bot", "speech_key": "k"})
    with pytest.raises(ValueError, match="one of"):
        wizard.answers_from_mapping({"name": "bot", "speech_provider": "playht"})
    one_key = wizard.answers_from_mapping({"name": "bot", "speech_provider": "elevenlabs", "speech_key": "el-key"})
    assert (one_key.stt_provider, one_key.stt_model, one_key.tts_provider) == ("elevenlabs", "scribe_v2", "elevenlabs")
    assert one_key.stt_key == one_key.tts_key == "el-key" and one_key.tts_voice_id


async def test_settings_save_can_start_a_call(setup_home, monkeypatch):
    script(monkeypatch, selects=["codex", "deepgram", "web"], texts=["deepgram-test"])
    path, _ = await wizard._configure(workspace=str(setup_home))
    script(monkeypatch, selects=[wizard.EDIT_DONE, "terminal"])
    assert await wizard._configure(profile=path.stem, settings=True, workspace=str(setup_home)) == (path, "terminal")
    assert launcher.selected_surface(setup_home) == "terminal"


async def test_settings_switches_speech_provider_to_elevenlabs_with_one_key(setup_home, monkeypatch):
    script(monkeypatch, selects=["codex", "deepgram", "web"], texts=["deepgram-test"])
    path, _ = await wizard._configure(workspace=str(setup_home))
    prompts = script(monkeypatch, selects=["speech", "elevenlabs", wizard.EDIT_DONE, wizard.LAUNCH_LATER], texts=["el-test"])
    assert await wizard._configure(profile=path.stem, settings=True, workspace=str(setup_home)) == 0
    assert prompts == ["What do you want to change?", "Speech service (listening and speaking):",
                       "ElevenLabs API key:", "What do you want to change?", "Start a call now?"]
    document = tomllib.loads(path.read_text())
    assert (document["stt"]["provider"], document["stt"]["model"]) == ("elevenlabs", "scribe_v2")
    assert (document["tts"]["provider"], document["tts"]["voice_id"]) == ("elevenlabs", wizard.DEFAULT_ELEVENLABS_VOICE)
    assert set(document["credentials"]["refs"]) == {"ELEVENLABS_API_KEY"}


async def test_first_run_local_speech_asks_no_key_and_downloads_models(setup_home, monkeypatch):
    from sleeptalk.speech import local_models

    fetched = []
    monkeypatch.setattr("sleeptalk.sysdeps.missing_local_extra", lambda: None)
    monkeypatch.setattr("sleeptalk.sysdeps.missing_streaming_extra", lambda: None)
    monkeypatch.setattr(local_models, "is_ready", lambda model, root=None: model.id in fetched)
    monkeypatch.setattr(local_models, "ensure_model", lambda model, **kwargs: fetched.append(model.id))
    monkeypatch.setattr("sleeptalk.stt_roundtrip.local_roundtrip", AsyncMock(return_value=("sleep talk check", 20)))
    prompts = script(monkeypatch, selects=["codex", "local", "download", "web"], texts=[])
    path, _ = await wizard._configure(workspace=str(setup_home))
    assert len(prompts) == 4 and prompts[1] == "Speech service (listening and speaking):"
    assert prompts[2] == "Download these local speech models?"
    assert not any("key" in prompt.lower() for prompt in prompts)
    assert fetched == ["moonshine-small-streaming-en", "kokoro-en"]
    document = tomllib.loads(path.read_text())
    assert (document["stt"]["provider"], document["stt"]["model"]) == ("local", "moonshine-small-streaming-en")
    assert (document["tts"]["provider"], document["tts"]["voice_id"]) == ("local", "af_heart")
    assert document["credentials"]["refs"] == {}
    assert not (setup_home / ".env").exists() or "API_KEY" not in (setup_home / ".env").read_text()


async def test_settings_save_on_profile_without_route_section(setup_home, monkeypatch):
    """A hand-written profile may omit [route]; editing it must not produce an empty table."""
    script(monkeypatch, selects=["codex", "deepgram", "web"], texts=["deepgram-test"])
    path, _ = await wizard._configure(workspace=str(setup_home))
    blocks = [block for block in path.read_text().split("\n\n") if not block.startswith('["route"]')]
    path.write_text("\n\n".join(blocks))
    assert "route" not in tomllib.loads(path.read_text())
    script(monkeypatch, selects=[wizard.EDIT_DONE, wizard.LAUNCH_LATER])
    assert await wizard._configure(profile=path.stem, settings=True, workspace=str(setup_home)) == 0
    assert tomllib.loads(path.read_text())["route"]["type"] == "cascade"


async def test_settings_switches_to_openai_realtime_and_back(setup_home, monkeypatch):
    """Realtime is one of the speech choices: its key lands in the private
    store, the profile gets a hybrid [route], and another pick undoes it."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    checked = AsyncMock()
    monkeypatch.setattr(setup_core, "hybrid_connectivity_check", checked)
    script(monkeypatch, selects=["codex", "deepgram", "web"], texts=["deepgram-test"])
    path, _ = await wizard._configure(workspace=str(setup_home))
    prompts = script(monkeypatch, selects=["speech", "realtime", "cedar", wizard.EDIT_DONE, wizard.LAUNCH_LATER],
                     texts=["sk-realtime-test"])
    assert await wizard._configure(profile=path.stem, settings=True, workspace=str(setup_home)) == 0
    assert prompts[:4] == ["What do you want to change?", "Speech service (listening and speaking):",
                           "OpenAI API key (https://platform.openai.com/api-keys):", "Voice:"]
    assert checked.await_args_list[0].args[0].realtime_key == "sk-realtime-test"
    document = tomllib.loads(path.read_text())
    assert (document["route"]["type"], document["route"]["voice"]) == ("hybrid", "cedar")
    assert "stt" not in document and set(document["credentials"]["refs"]) == {"OPENAI_API_KEY"}
    store = CredentialStore()
    assert store.get(document["credentials"]["refs"]["OPENAI_API_KEY"]) == "sk-realtime-test"
    env = launcher.resolve_layout().root / ".env"
    assert "sk-realtime-test" not in path.read_text() and not (env.exists() and "sk-realtime-test" in env.read_text())

    script(monkeypatch, selects=["speech", "cartesia", wizard.EDIT_DONE, wizard.LAUNCH_LATER], texts=["cartesia-test"])
    assert await wizard._configure(profile=path.stem, settings=True, workspace=str(setup_home)) == 0
    document = tomllib.loads(path.read_text())
    assert document["route"] == {"type": "cascade"}
    assert (document["stt"]["provider"], document["tts"]["provider"]) == ("cartesia", "cartesia")
    assert set(document["credentials"]["refs"]) == {"CARTESIA_API_KEY"}
