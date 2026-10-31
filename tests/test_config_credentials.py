import os
from pathlib import Path
from types import SimpleNamespace
import traceback

import pytest

from sleeptalk.config import (
    ConfigError, SleeptalkConfig, _load_env, load_config,
    resolve_config_document, resolve_credential_env,
)
from sleeptalk.credential_store import CredentialStore

SECRET = 'sentinel-stored-speech-key'


@pytest.fixture
def private_store(tmp_path, monkeypatch):
    store = CredentialStore(tmp_path / 'private', backend='file')
    monkeypatch.setattr('sleeptalk.credential_store.default_root', lambda: store.root)
    monkeypatch.setattr('sleeptalk.credential_store._keyring_backend', lambda: None)
    return store


def _document(tmp_path, refs):
    path = tmp_path / 'sleeptalk.toml'
    fields = '\n'.join(f'{name} = "{ref}"' for name, ref in refs.items())
    path.write_text('[transport]\ntype="null"\n[agent]\ntype="pi"\n[stt]\nprovider="deepgram"\n[tts]\nprovider="cartesia"\n[credentials.refs]\n'+fields+'\n')
    return path


def test_reference_resolves_for_config_validation_and_overrides_legacy(private_store, tmp_path):
    deepgram = private_store.put_validated('deepgram', SECRET)
    cartesia = private_store.put_validated('cartesia', 'tts-sentinel')
    path = _document(tmp_path, {'DEEPGRAM_API_KEY': deepgram, 'CARTESIA_API_KEY': cartesia})
    ambient = {'DEEPGRAM_API_KEY': 'old', 'ANTHROPIC_API_KEY': 'agent'}
    config = load_config(path, env=ambient, check_binaries=False)
    resolved = resolve_credential_env(config, ambient)
    assert resolved == {'DEEPGRAM_API_KEY': SECRET, 'CARTESIA_API_KEY': 'tts-sentinel', 'ANTHROPIC_API_KEY': 'agent'}
    assert ambient == {'DEEPGRAM_API_KEY': 'old', 'ANTHROPIC_API_KEY': 'agent'}
    assert SECRET not in config.model_dump_json()
    assert SECRET not in repr(config)


def test_read_only_document_and_validation_do_not_open_store(tmp_path, monkeypatch):
    path = _document(tmp_path, {'DEEPGRAM_API_KEY': 'credential:'+'a'*32})
    def forbidden():
        raise AssertionError('store must not open')
    monkeypatch.setattr('sleeptalk.credential_store.CredentialStore', forbidden)
    config = SleeptalkConfig.model_validate(resolve_config_document(path).raw)
    assert config.credentials.refs['DEEPGRAM_API_KEY'] == 'credential:'+'a'*32
    load_config(path, env={}, check_env=False, check_binaries=False)


def test_missing_reference_does_not_fall_back_to_ambient_key(private_store, tmp_path):
    path = _document(tmp_path, {'DEEPGRAM_API_KEY': 'credential:'+'b'*32})
    with pytest.raises(ConfigError, match='unavailable') as error:
        load_config(path, env={'DEEPGRAM_API_KEY': SECRET}, check_binaries=False)
    assert SECRET not in str(error.value)


def test_wrong_provider_ref_is_rejected_without_revealing_value(private_store, tmp_path):
    ref = private_store.put_validated('cartesia', SECRET)
    path = _document(tmp_path, {'DEEPGRAM_API_KEY': ref})
    with pytest.raises(ConfigError, match='wrong provider') as error:
        load_config(path, env={}, check_binaries=False)
    assert SECRET not in str(error.value)


@pytest.mark.parametrize('mapping', [{'DEEPGRAM_API_KEY': SECRET}, {SECRET: 'credential:'+'c'*32}])
def test_malformed_reference_errors_never_echo_input(tmp_path, mapping):
    path = _document(tmp_path, mapping)
    with pytest.raises(ConfigError) as error:
        load_config(path, env={}, check_env=False, check_binaries=False)
    assert SECRET not in ''.join(traceback.format_exception(error.value))


def test_inactive_surface_credentials_are_not_opened(private_store, tmp_path):
    ref = private_store.put_validated('deepgram', SECRET)
    path = _document(tmp_path, {'DEEPGRAM_API_KEY': ref, 'DISCORD_BOT_TOKEN': 'credential:'+'f'*32})
    config = load_config(path, env={'CARTESIA_API_KEY': 'tts'}, check_binaries=False)
    assert resolve_credential_env(config, {})['DEEPGRAM_API_KEY'] == SECRET


def test_custom_discord_token_reference_is_supported(private_store):
    ref = private_store.put_validated('discord', SECRET)
    config = SleeptalkConfig.model_validate({'transport': {'type':'discord','token_env':'CUSTOM_BOT_TOKEN'}, 'credentials': {'refs': {'CUSTOM_BOT_TOKEN': ref}}})
    assert resolve_credential_env(config, {})['CUSTOM_BOT_TOKEN'] == SECRET


def test_env_loading_keeps_precedence_and_interpolation_without_global_mutation(tmp_path, monkeypatch):
    project = tmp_path / 'project'
    profiles = project / 'config' / 'profiles'
    profiles.mkdir(parents=True)
    home = tmp_path / 'home'
    home.mkdir()
    cwd = tmp_path / 'cwd'
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.setenv('SLEEPTALK_HOME', str(home))
    monkeypatch.setenv('CREDENTIAL_TEST_BASE', 'ambient')
    (profiles/'.env').write_text('CREDENTIAL_TEST_BASE=profile\nCREDENTIAL_TEST_FIRST=profile\nCREDENTIAL_TEST_EXPAND=${CREDENTIAL_TEST_BASE}\n')
    (project/'.env').write_text('CREDENTIAL_TEST_FIRST=project\nCREDENTIAL_TEST_SECOND=project\nCREDENTIAL_TEST_EXPAND2=${CREDENTIAL_TEST_FIRST}\n')
    (home/'.env').write_text('CREDENTIAL_TEST_SECOND=home\nCREDENTIAL_TEST_THIRD=home\n')
    (cwd/'.env').write_text('CREDENTIAL_TEST_THIRD=cwd\nCREDENTIAL_TEST_FOURTH=cwd\n')
    before = dict(os.environ)
    env = _load_env(profiles/'bot.toml')
    assert env['CREDENTIAL_TEST_BASE'] == 'ambient'
    assert env['CREDENTIAL_TEST_FIRST'] == 'profile'
    assert env['CREDENTIAL_TEST_SECOND'] == 'project'
    assert env['CREDENTIAL_TEST_THIRD'] == 'home'
    assert 'CREDENTIAL_TEST_FOURTH' not in env, "the working directory's .env is never read"
    assert env['CREDENTIAL_TEST_EXPAND'] == 'ambient'
    assert env['CREDENTIAL_TEST_EXPAND2'] == 'profile'
    assert dict(os.environ) == before


async def test_doctor_resolves_refs_without_mutating_explicit_environment(private_store, tmp_path):
    from sleeptalk.doctor import run_doctor
    refs = {'DEEPGRAM_API_KEY': private_store.put_validated('deepgram', SECRET), 'CARTESIA_API_KEY': private_store.put_validated('cartesia', 'tts')}
    path = _document(tmp_path, refs)
    env = {}
    report = await run_doctor(path, env=env, live=False)
    rung = next(r for r in report.rungs if r.name == 'env vars present')
    assert rung.status == 'ok'
    assert env == {}
    assert SECRET not in repr(report)


async def test_doctor_missing_ref_does_not_attempt_provider_requests(private_store, tmp_path, monkeypatch):
    from sleeptalk.doctor import run_doctor
    path = _document(tmp_path, {'DEEPGRAM_API_KEY': 'credential:'+'d'*32})
    async def forbidden(*args):
        raise AssertionError('no network after credential failure')
    monkeypatch.setattr('sleeptalk.doctor._provider_connectivity', forbidden)
    report = await run_doctor(path, env={'DEEPGRAM_API_KEY': 'legacy'}, live=True)
    assert report.failed
    assert next(r for r in report.rungs if r.name == 'env vars present').status == 'fail'


def test_builder_sends_stored_speech_key_only_to_provider(private_store, tmp_path, monkeypatch):
    from sleeptalk.builder import build_session
    from sleeptalk.agents.construction import build_agent
    path = _document(tmp_path, {'DEEPGRAM_API_KEY': private_store.put_validated('deepgram', SECRET)})
    (tmp_path/'.env').write_text('ANTHROPIC_API_KEY=agent-dotenv-auth\nCARTESIA_API_KEY=tts\nTEST_DISCORD_BOT_TOKEN=bot-sentinel\n')
    config = load_config(path, check_env=False, check_binaries=False)
    captured = []
    def capture_agent(section, env):
        result = build_agent(section, env)
        captured.append(result[0])
        return result
    class ProviderReached(Exception):
        pass
    def capture_provider(spec, env, **kwargs):
        assert env['DEEPGRAM_API_KEY'] == SECRET
        assert env['CARTESIA_API_KEY'] == 'tts'
        raise ProviderReached()
    monkeypatch.setattr('sleeptalk.agents.construction.build_agent', capture_agent)
    monkeypatch.setattr('sleeptalk.builder.build_stt_components', capture_provider)
    with pytest.raises(ProviderReached):
        build_session(config)
    child_env = captured[0]._subprocess_env
    assert child_env['ANTHROPIC_API_KEY'] == 'agent-dotenv-auth'
    assert 'DEEPGRAM_API_KEY' not in child_env
    assert 'CARTESIA_API_KEY' not in child_env
    assert 'TEST_DISCORD_BOT_TOKEN' not in child_env
    assert os.environ.get('ANTHROPIC_API_KEY') != 'agent-dotenv-auth'


def test_hybrid_stored_key_does_not_replace_agent_openai_auth(private_store):
    from sleeptalk.builder import build_session
    reference = private_store.put_validated('openai', SECRET)
    config = SleeptalkConfig.model_validate({'transport': {'type':'null'}, 'route': {'type':'hybrid'}, 'credentials': {'refs': {'OPENAI_API_KEY': reference}}})
    session = build_session(config, env={'OPENAI_API_KEY': 'agent-openai-auth'})
    assert session.route._realtime.config.api_key == SECRET
    assert session.agent._subprocess_env['OPENAI_API_KEY'] == 'agent-openai-auth'


@pytest.mark.parametrize('factory_name', ['_build_pi_agent', '_build_omp_agent', '_build_codex_agent', '_build_acp_agent', '_build_openclaw_acp_agent', '_build_claude_code_agent', '_build_hermes_gateway_agent'])
def test_factories_preserve_explicit_agent_environment(factory_name):
    from sleeptalk import defaults
    env = {'ANTHROPIC_API_KEY': 'agent-auth', 'PATH': '/supplied/path'}
    agent = getattr(defaults, factory_name)(SleeptalkConfig().agent, env)
    if factory_name == '_build_claude_code_agent':
        forwarded = agent._session_kwargs()['env']
    else:
        forwarded = getattr(agent, '_subprocess_env', getattr(agent, '_env', None))
    assert forwarded == env


def test_discord_authorization_fields_are_valid_for_registered_transport(tmp_path):
    path = tmp_path/'discord.toml'
    path.write_text('[transport]\ntype="discord"\nauthorization_required=true\noperator_ids="123"\nspeaker_ids="123"\nlistener_ids="123"\n')
    config = load_config(path, env={}, check_env=False, check_binaries=False)
    assert config.transport.authorization_required
    assert config.transport.operator_ids == '123'


def test_adapter_resolves_binary_from_explicit_environment_path(tmp_path):
    from sleeptalk.agents.pi_rpc import PiRPCClient
    binary = tmp_path/'pi'
    binary.write_text('#!/bin/sh\nexit 0\n')
    binary.chmod(0o755)
    client = PiRPCClient(env={'PATH': str(tmp_path)})
    assert client._resolve_binary() == str(binary)


def test_switching_from_custom_discord_token_to_web_skips_unused_reference(monkeypatch):
    from sleeptalk.config import transport_config_for_surface
    config = SleeptalkConfig.model_validate({'transport': {'type':'discord','token_env':'CUSTOM_BOT_TOKEN'}, 'credentials': {'refs': {'CUSTOM_BOT_TOKEN':'credential:'+'e'*32}}})
    config = config.model_copy(update={'transport': transport_config_for_surface(config.transport, 'web')})
    def forbidden():
        raise AssertionError('unused Discord store must not open')
    monkeypatch.setattr('sleeptalk.credential_store.CredentialStore', forbidden)
    assert resolve_credential_env(config, {}) == {}


def test_init_secret_flags_read_privately(monkeypatch, capsys):
    import io
    from sleeptalk.cli import _read_secret_flags

    piped = {"speech_key": "-", "name": "Bot"}
    monkeypatch.setattr("sys.stdin", io.StringIO("sk-from-stdin\n"))
    _read_secret_flags(piped)
    assert piped == {"speech_key": "sk-from-stdin", "name": "Bot"}

    literal = {"tts_key": "sk-literal"}
    _read_secret_flags(literal)
    assert literal == {"tts_key": "sk-literal"}
    warning = capsys.readouterr().err
    assert "--tts-key" in warning and "sk-literal" not in warning

    monkeypatch.setattr("sys.stdin", io.StringIO("one\n"))
    with pytest.raises(ValueError, match="Only one key"):
        _read_secret_flags({"stt_key": "-", "tts_key": "-"})
