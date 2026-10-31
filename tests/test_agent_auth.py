import hashlib
import json

import pytest
from pydantic import ValidationError

from sleeptalk.agent_auth import AgentAuthConfig, AgentAuthError, auth_scope, resolve_agent_auth_env
from sleeptalk.config import AgentConfig
from sleeptalk.credential_store import CredentialStore
from sleeptalk.runtime_binding import bind


def auth(**changes):
    return AgentAuthConfig(mode='api_key', protocol='openai-completions',
        base_url=changes.pop('base_url', 'https://provider.test/v1'),
        credential_ref=changes.pop('credential_ref', 'credential:' + 'a' * 32), **changes)


def test_explicit_credential_is_endpoint_scoped_and_never_falls_back(tmp_path):
    store = CredentialStore(tmp_path / 'keys', backend='file')
    choice = auth()
    reference = store.put_validated(auth_scope(choice), 'private-selected-key')
    choice = choice.model_copy(update={'credential_ref': reference})
    agent = AgentConfig(type='model', auth=choice)
    result = resolve_agent_auth_env(agent, {'OPENAI_API_KEY': 'ambient-key'}, store=store)
    assert result['SLEEPTALK_PROVIDER_CREDENTIAL'] == 'private-selected-key'
    assert json.loads(result['SLEEPTALK_PROVIDER_AUTH']) == {
        'mode': 'api_key', 'protocol': 'openai-completions', 'base_url': 'https://provider.test/v1'}
    agent.auth = choice.model_copy(update={'base_url': 'https://another.test/v1'})
    with pytest.raises(AgentAuthError, match='different authentication'):
        resolve_agent_auth_env(agent, result, store=store)
    agent.auth = choice.model_copy(update={'credential_ref': 'credential:' + '0' * 32})
    with pytest.raises(AgentAuthError, match='unavailable') as error:
        resolve_agent_auth_env(agent, result, store=store)
    assert 'private-selected-key' not in str(error.value)


def test_environment_cannot_forge_an_explicit_credential_selection():
    assert resolve_agent_auth_env(AgentConfig(), {
        'SLEEPTALK_PROVIDER_AUTH': 'forged', 'SLEEPTALK_PROVIDER_CREDENTIAL': 'secret',
        'PATH': '/bin'}) == {'PATH': '/bin'}


@pytest.mark.parametrize('url', ['http://provider.test', 'https://user:secret@provider.test',
    'https://provider.test?token=secret', 'https://provider.test/#secret',
    'https://provider.test/../admin', 'https://provider.test/%2e%2e/admin',
    'https://provider.test/\\admin', 'https://provider.test/%5cadmin',
    'https://provider.test/\nadmin', 'https://provider.test:bad'])
def test_endpoint_rejects_unsafe_or_ambiguous_addresses(url):
    with pytest.raises(ValidationError):
        auth(base_url=url)


def test_equivalent_endpoint_spelling_has_same_scope():
    assert auth_scope(auth(base_url='https://PROVIDER.test:443/v1/')) == auth_scope(auth())


def test_binding_preserves_existing_profiles_and_covers_explicit_auth(tmp_path):
    agent = AgentConfig(workspace_dir=str(tmp_path))
    before = bind(agent, host='a' * 64)
    old_runtime = agent.model_dump(exclude={'auth', 'source_path', 'name', 'slug', 'persona_instructions', 'persona_file'})
    assert before.runtime == hashlib.sha256(json.dumps(old_runtime, sort_keys=True).encode()).hexdigest()
    agent.auth = auth()
    assert bind(agent, host='a' * 64).runtime != before.runtime


async def test_missing_reference_fails_probe_before_launch(tmp_path, monkeypatch):
    from sleeptalk.runtime_probe import probe_runtime
    store = CredentialStore(tmp_path / 'keys', backend='file')
    class PrivateStore(CredentialStore):
        def __init__(self):
            super().__init__(store.root, backend='file')
    monkeypatch.setattr('sleeptalk.credential_store.CredentialStore', PrivateStore)
    report = await probe_runtime(AgentConfig(type='pi', workspace_dir=str(tmp_path), auth=auth()))
    assert report['status'] == 'needs_input'
    assert report['login'] == 'unavailable'


def test_local_and_remote_cli_choices_round_trip_through_input_spec(tmp_path):
    import argparse
    from sleeptalk.agent_setup_options import add_arguments, choices, toml_value
    from sleeptalk.private_input import InputSpec
    import tomllib
    parser = argparse.ArgumentParser()
    add_arguments(parser)
    args = parser.parse_args(['--agent-auth-mode', 'api_key', '--agent-auth-protocol', 'openai-completions',
        '--agent-auth-url', 'https://provider.test/v1', '--agent-credential-ref', 'credential:' + 'a' * 32,
        '--agent-option', 'hermes_root=/isolated/hermes'])
    selected = choices(args)
    spec = InputSpec(request_id='auth-choices', profile='voice', workspace=str(tmp_path), agent='acp',
        surface='web', tts_provider='cartesia', **selected)
    restored = InputSpec.model_validate_json(spec.model_dump_json()).agent_config()
    assert restored.auth == auth()
    assert restored.options == {'hermes_root': '/isolated/hermes'}
    options = {'nested': {'a': 'a\nb', 'items': [True, 1, 1.5, '👋']}}
    assert tomllib.loads('options = ' + toml_value(options))['options'] == options


def test_actual_construction_resolves_selected_reference(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from sleeptalk.agents import construction
    store = CredentialStore(tmp_path / 'keys', backend='file')
    selected = auth()
    ref = store.put_validated(auth_scope(selected), 'selected-private-key')
    agent = AgentConfig(type='model', auth=selected.model_copy(update={'credential_ref': ref}))
    class PrivateStore(CredentialStore):
        def __init__(self):
            super().__init__(store.root, backend='file')
    monkeypatch.setattr('sleeptalk.credential_store.CredentialStore', PrivateStore)
    observed = []
    def factory(section, environment):
        observed.append(environment['SLEEPTALK_PROVIDER_CREDENTIAL'])
        return SimpleNamespace(**{name: lambda: None for name in construction._CORE_METHODS})
    monkeypatch.setattr(construction, 'resolve_agent', lambda *args, **kwargs:
        construction.AgentConstruction(SimpleNamespace(name='fixture', factory=factory), None))
    construction.build_agent(agent, {'OPENAI_API_KEY': 'ambient'})
    assert observed == ['selected-private-key']


@pytest.mark.parametrize('adapter', ['acp', 'hermes', 'hermes_gateway'])
def test_optional_hermes_install_selector_preserves_existing_binding(tmp_path, adapter):
    agent = AgentConfig(type=adapter, workspace_dir=str(tmp_path))
    original = bind(agent, host='a' * 64)
    agent.options = {'hermes_root': ''}
    assert bind(agent, host='a' * 64) == original
    agent.options = {'hermes_root': '/selected/hermes'}
    assert bind(agent, host='a' * 64).runtime != original.runtime


@pytest.mark.parametrize('field', ['model', 'provider'])
@pytest.mark.parametrize('bad', ['invisible\nlabel', 'provider\u202eelpmaxe', 'scope\u2066hidden', 'control\x85name', ''])
def test_remote_consent_rejects_misleading_model_labels(tmp_path, field, bad):
    from sleeptalk.agent_auth import AgentAuthChoice
    from sleeptalk.private_input import InputSpec
    values = dict(request_id='private-model-123',profile='voice',workspace=str(tmp_path),agent='acp',
        surface='web',tts_provider='cartesia',model='selected-model',provider='custom',
        auth_request=AgentAuthChoice(mode='api_key',protocol='openai-completions',base_url='https://provider.test'))
    values[field] = bad
    with pytest.raises(ValueError, match='bounded model'):
        InputSpec(**values)
