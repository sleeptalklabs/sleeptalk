import asyncio
import json
import time

from aiohttp.test_utils import TestClient, TestServer
import pytest

from sleeptalk.agent_auth import AgentAuthConfig, auth_scope
from sleeptalk.agent_auth_input import AuthRequest, AuthSession, cancel, status
from sleeptalk.credential_store import CredentialStore
from sleeptalk.private_input import atomic_json


@pytest.fixture
def session(tmp_path):
    async def validator(selected, model):
        assert selected.credential == 'private-sentinel'
        assert model == 'chosen-model'
        return 'available'
    auth = AgentAuthConfig(mode='api_key', protocol='openai-completions',
        base_url='https://provider.test/v1', credential_ref='credential:' + '0' * 32)
    spec = AuthRequest(request_id='auth-request', auth=auth, model='chosen-model')
    result = AuthSession(spec, tmp_path / 'requests',
        store=CredentialStore(tmp_path / 'store', backend='file'), validator=validator)
    atomic_json(result.path / 'status.json', {'status': 'waiting',
        'request_id': spec.request_id, 'expires_at': result.expires_at})
    return result


async def client_for(session):
    client = TestClient(TestServer(session.app()))
    await client.start_server()
    session.origin = str(client.make_url('/')).rstrip('/')
    client.session.headers.update({'Origin': session.origin})
    response = await client.post('/session', json={'invitation': session.invitation})
    assert response.status == 200
    client.session.headers.update({'X-Setup-CSRF': (await response.json())['csrf']})
    return client


async def test_private_credential_commits_only_after_confirmation_and_validation(session):
    async with await client_for(session) as client:
        body = {'value': 'private-sentinel', 'confirmed': False}
        assert (await client.post('/credential', json=body)).status == 400
        assert status(session.spec.request_id, session.root)['status'] == 'waiting'
        body['confirmed'] = True
        response = await client.post('/credential', json=body)
        assert response.status == 200
        assert await response.json() == {'status': 'complete'}
        receipt = status(session.spec.request_id, session.root, store=session.store)
        assert 'private-sentinel' not in json.dumps(receipt)
        assert session.store.get(receipt['auth']['credential_ref']) == 'private-sentinel'
        assert session.store.status(receipt['auth']['credential_ref']).provider == auth_scope(session.spec.auth)
        assert (await client.post('/credential', json=body)).status == 410
        assert cancel(session.spec.request_id, session.root, store=session.store)['status'] == 'complete'


async def test_host_origin_csrf_expiry_and_value_free_failures(session):
    async with await client_for(session) as client:
        for headers in ({'Host': 'wrong.test'}, {'Origin': 'https://wrong.test'}, {'X-Setup-CSRF': 'wrong'}):
            response = await client.post('/credential', json={'value': 'private-sentinel', 'confirmed': True}, headers=headers)
            assert response.status == 403
        async def failure(*args): raise ValueError('private-sentinel')
        session.validator = failure
        response = await client.post('/credential', json={'value': 'private-sentinel', 'confirmed': True})
        assert response.status == 400
        assert 'private-sentinel' not in await response.text()
        atomic_json(session.path / 'status.json', {'status': 'waiting', 'expires_at': time.time() - 1})
        assert (await client.post('/credential', json={'value': 'private-sentinel', 'confirmed': True})).status == 410


async def test_cancel_during_validation_prevents_write(session):
    entered, resume = asyncio.Event(), asyncio.Event()
    async def validator(*args):
        entered.set()
        await resume.wait()
        return 'available'
    session.validator = validator
    async with await client_for(session) as client:
        pending = asyncio.create_task(client.post('/credential', json={'value': 'private-sentinel', 'confirmed': True}))
        await entered.wait()
        assert cancel(session.spec.request_id, session.root)['status'] == 'cancelled'
        resume.set()
        assert (await pending).status == 410
        assert not (session.store.root / 'store.sqlite3').exists()


async def test_no_invitation_without_explicit_local_user_control(session):
    from sleeptalk.agent_auth_input import serve
    with pytest.raises(ValueError, match='user-controlled'):
        await serve(session.spec, user_browser_local=False, on_ready=lambda _: pytest.fail('invitation emitted'))


async def test_committed_key_survives_receipt_failure_and_cancellation(session, monkeypatch):
    from sleeptalk import agent_auth_input
    atomic_json(session.path / 'request.json', session.spec.model_dump())
    real_write = agent_auth_input.atomic_json
    def fail_receipt(path, value):
        if value.get('status') == 'complete':
            raise OSError('crash after credential commit')
        real_write(path, value)
    monkeypatch.setattr(agent_auth_input, 'atomic_json', fail_receipt)
    async with await client_for(session) as client:
        response = await client.post('/credential', json={'value': 'private-sentinel', 'confirmed': True})
        assert response.status == 400
    receipt = status(session.spec.request_id, session.root, store=session.store)
    assert receipt['status'] == 'complete'
    assert session.store.get(receipt['auth']['credential_ref']) == 'private-sentinel'
    assert cancel(session.spec.request_id, session.root, store=session.store) == receipt


def test_auth_cli_never_starts_without_user_local_flag(capsys):
    from sleeptalk.cli import main
    code = main(['host', 'auth', 'start', '--request-id', 'auth-request', '--mode', 'api_key',
        '--protocol', 'openai-completions', '--base-url', 'https://provider.test/v1', '--model', 'selected'])
    assert code == 2
    report = json.loads(capsys.readouterr().out)
    assert 'input_url' not in report
    assert 'user-controlled' in report['next_action']


@pytest.mark.parametrize('lost_receipt', [False, True])
async def test_removed_credential_is_unavailable_without_resurrection(session, lost_receipt):
    atomic_json(session.path / 'request.json', session.spec.model_dump())
    async with await client_for(session) as client:
        assert (await client.post('/credential', json={'value': 'private-sentinel', 'confirmed': True})).status == 200
    receipt = status(session.spec.request_id, session.root, store=session.store)
    session.store.remove(receipt['auth']['credential_ref'], affected_profiles=[])
    if lost_receipt:
        atomic_json(session.path / 'status.json', {'status': 'waiting', 'expires_at': session.expires_at})
    result = status(session.spec.request_id, session.root, store=session.store)
    assert result['status'] == 'needs_input' and result['code'] == 'credential_unavailable'
    assert 'new request ID' in result['next_action']
    assert cancel(session.spec.request_id, session.root, store=session.store) == result


async def test_invitation_cannot_open_a_second_session_without_its_cookie(session):
    async with await client_for(session) as client:
        # client_for already exchanged the invitation; this client holds no
        # cookie (aiohttp drops IP cookies), like a second person with the link.
        client.session.cookie_jar.clear()
        response = await client.post('/session', json={'invitation': session.invitation})
        assert response.status == 401
