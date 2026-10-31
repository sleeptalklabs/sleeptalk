"""Fresh Hermes private setup persists the same verified runtime and credential."""
import os
from pathlib import Path
import tempfile

import pytest

from sleeptalk.agent_auth import AgentAuthConfig, AgentAuthChoice, auth_scope
from sleeptalk.credential_store import CredentialStore
from sleeptalk.private_input import InputSpec, prepare_profile


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    if not all(os.environ.get(name) for name in ('SLEEPTALK_TEST_HERMES_ROOT', 'SLEEPTALK_TEST_HERMES_PYTHON')):
        pytest.skip('Set pinned Hermes source and Python fixture paths for real setup proof.')
    with tempfile.TemporaryDirectory(prefix='hermes-profile-', dir='/tmp') as directory:
        root = Path(directory).resolve()
        monkeypatch.setenv('SLEEPTALK_HOME', str(root/'home'))
        class PrivateStore(CredentialStore):
            def __init__(self):
                super().__init__(root/'keys', backend='file')
        monkeypatch.setattr('sleeptalk.credential_store.CredentialStore', PrivateStore)
        yield root, PrivateStore()


@pytest.mark.parametrize('runtime', ['acp', 'gateway'])
async def test_real_hermes_private_profile_preserves_verified_custom_auth(isolated, monkeypatch, runtime):
    from sleeptalk.config import load_resolved_config
    from sleeptalk.runtime_binding import bind
    root, store = isolated
    choice = AgentAuthChoice(mode='api_key', protocol='openai-completions', base_url='https://custom.test/v1')
    ref = store.put_validated(auth_scope(choice), 'private-model-key')
    auth = AgentAuthConfig(**choice.model_dump(), credential_ref=ref)
    spec = InputSpec(request_id='hermes-profile-123',profile='hermes',workspace=str(root),agent='hermes_gateway' if runtime == 'gateway' else 'hermes',
        runtime=runtime,permission_mode='pending',surface='web',tts_provider='cartesia',model='selected-model',
        provider='custom',pi_bin=os.environ['SLEEPTALK_TEST_HERMES_PYTHON'],
        options={'hermes_root':os.environ['SLEEPTALK_TEST_HERMES_ROOT']},auth=auth)
    calls = []
    async def verify(selection, model):
        calls.append((selection.credential, selection.base_url, model))
        return 'available'
    monkeypatch.setattr('sleeptalk.agents.provider_auth.verify',verify)
    refs = {name:store.put_validated(name,'synthetic-speech-key') for name in ('deepgram','cartesia')}
    details = {'deepgram':{'stt_model':'nova-3'},'cartesia':{'voice_id':'selected-voice'}}
    path = root/'.sleeptalk/profiles/hermes.toml'
    document = await prepare_profile(spec,refs,details,store,{},path,root/'leases')
    assert calls == [('private-model-key','https://custom.test/v1','selected-model')]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(document)
    cfg = load_resolved_config(path,check_env=False,check_binaries=False)
    assert cfg.agent.auth == auth and cfg.agent.options['hermes_root'] == spec.options['hermes_root']
    assert cfg.agent.permission_mode == 'pending' and cfg.agent.runtime == runtime
    assert cfg.agent.model == 'selected-model' and cfg.agent.workspace_dir == str(root)
    assert cfg.binding == bind(cfg.agent)
    assert 'private-model-key' not in document and 'synthetic-speech-key' not in document


async def test_remote_configuration_requires_real_hermes_before_model_entry(isolated,monkeypatch):
    from sleeptalk.remote_host import selected_spec
    root, _ = isolated
    monkeypatch.setattr('sleeptalk.setup.ensure_dependencies',lambda *args,**kwargs:())
    async def forbidden(*args): pytest.fail('Configuration-only selection must not contact the provider.')
    monkeypatch.setattr('sleeptalk.agents.provider_auth.verify',forbidden)
    spec = InputSpec(request_id='hermes-remote-123',profile='remote',workspace=str(root),agent='hermes',runtime='acp',
        permission_mode='deny',surface='web',tts_provider='cartesia',model='selected-model',provider='custom',
        pi_bin=os.environ['SLEEPTALK_TEST_HERMES_PYTHON'],options={'hermes_root':os.environ['SLEEPTALK_TEST_HERMES_ROOT']},
        auth_request=AgentAuthChoice(mode='api_key',protocol='openai-completions',base_url='https://custom.test/v1'))
    assert (await selected_spec(spec)).auth_request == spec.auth_request
    with pytest.raises(ValueError):
        await selected_spec(spec.model_copy(update={'agent':'codex'}))
    with pytest.raises(ValueError):
        await selected_spec(spec.model_copy(update={'permission_mode':'auto'}))
