import hashlib
from pathlib import Path
import json

import pytest

pytest.importorskip('fido2')
pytest.importorskip('rfc8785')
pytest.importorskip('cryptography.hazmat.primitives.hpke')
from cryptography.hazmat.primitives.asymmetric import ec
from fido2.cose import ES256
from fido2.webauthn import Aaguid, AttestedCredentialData


from sleeptalk.credential_store import CredentialStore, CredentialError
from sleeptalk import remote_crypto as crypto
from sleeptalk.remote_store import RemoteStore, identifier
from cryptography.hazmat.primitives import hashes
from fido2.webauthn import AuthenticatorData, CollectedClientData

ORIGIN = "https://setup.example.com"


def assertion(challenge, operator, credential):
    client = CollectedClientData.create(type="webauthn.get", challenge=challenge, origin=ORIGIN)
    auth = AuthenticatorData.create(hashlib.sha256(b"setup.example.com").digest(), 5, 0)
    signature = operator.sign(bytes(auth) + client.hash, ec.ECDSA(hashes.SHA256()))
    return {"id":crypto.encode(credential.credential_id), "rawId":crypto.encode(credential.credential_id),
        "type":"public-key", "response":{"clientDataJSON":crypto.encode(bytes(client)),
        "authenticatorData":crypto.encode(bytes(auth)), "signature":crypto.encode(signature)}}


@pytest.fixture
def scenario(tmp_path):
    credentials = CredentialStore(tmp_path/'credentials', backend='file')
    now = [2000000000]
    store = RemoteStore(tmp_path/'remote', credentials, clock=lambda:now[0])
    operator_key = ec.generate_private_key(ec.SECP256R1())
    credential = AttestedCredentialData.create(Aaguid.NONE,b'fixture-operator',ES256.from_cryptography_key(operator_key.public_key()))
    operator_id,workspace_id = identifier(),identifier()
    store.register_confirmed_operator(operator_id=operator_id,credential=bytes(credential),workspace_id=workspace_id,
        binding='a'*64,origin=ORIGIN,enrollment_id=identifier())
    invitation = store.create_request(operator_id=operator_id,workspace_id=workspace_id,binding='a'*64,
        spec={'profile_path':str(tmp_path/'profile.toml')},
        fields={'deepgram.api_key':{'reference':None,'generation':0,'profiles':[]}},
        origin=ORIGIN,display_host='Synthetic host',display_workspace='Synthetic workspace')
    store.set_connection(identifier())
    return store,invitation,operator_key,credential,now


def submission(scenario, *, value='synthetic-secret'):
    store,invitation,key,credential,_ = scenario
    request_id = invitation['descriptor']['request_id']
    context = crypto.Context.model_validate(store.challenge(request_id,invitation['capability'],field='deepgram.api_key',submission_id=identifier())['payload']['context'])
    public_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(),crypto.decode(invitation['descriptor']['recipient_public_key']))
    enc,ct = crypto.encrypt(value,context,public_key)
    challenge = crypto.authorization_challenge(context,crypto.decode(enc),crypto.decode(ct))
    raw = crypto.canonical({'context':context.model_dump(),'enc':enc,'ct':ct,'assertion':assertion(challenge,key,credential)})
    return context.submission_id,raw


def reserve(scenario, raw):
    store,invitation,*_ = scenario
    return store.reserve(invitation['descriptor']['request_id'],invitation['capability'],raw)


def test_durable_writer_marker_survives_later_replacement_and_deletion(tmp_path):
    store = CredentialStore(tmp_path,backend='file')
    first = store.commit_validated('deepgram','secret-one',operation_id='first',operation_digest='a'*64)
    second = store.commit_validated('deepgram','secret-two',operation_id='second',operation_digest='b'*64,
        reference=first.reference,expected_generation=1)
    assert second.generation == 2
    assert store.operation_result('first') == first
    assert store.commit_validated('deepgram','secret-one',operation_id='first',operation_digest='a'*64) == first
    with pytest.raises(CredentialError):
        store.commit_validated('deepgram','changed',operation_id='first',operation_digest='c'*64)
    store.remove(first.reference,affected_profiles=[])
    assert store.operation_result('first') == first


def test_signed_submission_commits_once_and_recovers_exact_receipt(scenario):
    store,invitation,*_ = scenario
    sid,raw = submission(scenario)
    pending,value = reserve(scenario,raw)
    assert value == 'synthetic-secret'
    assert reserve(scenario,raw) == (pending,None)
    receipt = store.commit(sid,value,{'stt_model':'fixture'},current_binding='a'*64)
    assert receipt['payload']['status'] == 'saved'
    crypto.verify(receipt['payload'],receipt['signature'],store.key.public_key(),purpose='receipt')
    assert reserve(scenario,raw) == (receipt,None)
    assert store.credentials.get(receipt['payload']['reference']) == value
    assert value not in (store.root/'remote.sqlite3').read_bytes().decode('latin1')


def test_same_submission_id_with_other_content_is_conflict(scenario):
    _,raw = submission(scenario)
    reserve(scenario,raw)
    altered = json.loads(raw)
    altered['ct'] = crypto.encode(b'changed')
    with pytest.raises(crypto.ProtocolError):
        reserve(scenario,crypto.canonical(altered))


@pytest.mark.parametrize('change',['cancel','revoke','expiry','binding'])
def test_authority_rechecked_after_provider_validation(scenario,change):
    store,invitation,*_,now = scenario
    sid,raw = submission(scenario)
    _,value = reserve(scenario,raw)
    if change == 'cancel': store.cancel(invitation['descriptor']['request_id'])
    if change == 'revoke': store.revoke(invitation['descriptor']['operator_id'])
    if change == 'expiry': now[0] += 901
    with pytest.raises(crypto.ProtocolError):
        store.commit(sid,value,{},current_binding='b'*64 if change == 'binding' else 'a'*64)
    assert store.credentials.operation_result('remote:'+sid) is None


def test_restart_without_writer_marker_requires_new_nonce(scenario):
    store,invitation,*_ = scenario
    sid,raw = submission(scenario)
    reserve(scenario,raw)
    store.set_connection(identifier())
    receipt,value = reserve(scenario,raw)
    assert receipt['payload']['status'] == 'interrupted' and value is None
    next_sid,next_raw = submission(scenario)
    assert next_sid != sid and reserve(scenario,next_raw)[1] == 'synthetic-secret'


def test_crash_after_writer_commit_recovers_before_cancel(scenario,monkeypatch):
    store,invitation,*_ = scenario
    sid,raw = submission(scenario)
    _,value = reserve(scenario,raw)
    original = store._saved
    def crash(*args): raise RuntimeError('synthetic process termination boundary')
    monkeypatch.setattr(store,'_saved',crash)
    with pytest.raises(RuntimeError):
        store.commit(sid,value,{'stt_model':'fixture'},current_binding='a'*64)
    assert store.credentials.operation_result('remote:'+sid) is not None
    monkeypatch.setattr(store,'_saved',original)
    store.cancel(invitation['descriptor']['request_id'])
    receipt,duplicate = reserve(scenario,raw)
    assert receipt['payload']['status'] == 'saved' and duplicate is None
    status = store.status(invitation['descriptor']['request_id'])['payload']
    assert status['status'] == 'cancelled' and status['fields']['deepgram.api_key']['status'] == 'saved'


def test_old_connection_and_wrong_operator_never_reserve(scenario):
    store,invitation,*_ = scenario
    sid,raw = submission(scenario)
    store.set_connection(identifier())
    with pytest.raises(crypto.ProtocolError): reserve(scenario,raw)
    sid,raw = submission(scenario)
    changed = json.loads(raw)
    changed['assertion']['response']['signature'] = crypto.encode(b'not-a-signature')
    with pytest.raises(crypto.ProtocolError): reserve(scenario,crypto.canonical(changed))
    assert store.credentials.operation_result('remote:'+sid) is None


def test_capability_and_status_nonce_are_separate_authorities(scenario):
    store,invitation,*_ = scenario
    request_id = invitation['descriptor']['request_id']
    with pytest.raises(crypto.ProtocolError):
        store.status(request_id,capability=crypto.encode(b'x'*32))
    nonce = identifier()
    status = store.status(request_id,capability=invitation['capability'],client_nonce=nonce)
    assert status['payload']['client_nonce'] == nonce
    assert 'key' not in status['payload'] and 'capability' not in status['payload']


@pytest.mark.parametrize('action',['cancel','revoke'])
def test_revocation_wins_between_intent_and_writer(scenario,monkeypatch,action):
    from contextlib import contextmanager
    import threading
    store,invitation,*_ = scenario
    sid,raw = submission(scenario)
    _,value = reserve(scenario,raw)
    intent,release = threading.Event(),threading.Event()
    original = store.transaction
    seen = [False]
    @contextmanager
    def transaction():
        with original() as db:
            yield db
        if threading.current_thread().name == 'commit-worker' and not seen[0]:
            seen[0] = True
            intent.set()
            assert release.wait(5)
    monkeypatch.setattr(store,'transaction',transaction)
    errors=[]
    def work():
        try: store.commit(sid,value,{},current_binding='a'*64)
        except crypto.ProtocolError: errors.append('rejected')
    thread=threading.Thread(target=work,name='commit-worker')
    thread.start()
    assert intent.wait(5)
    if action == 'cancel': store.cancel(invitation['descriptor']['request_id'])
    else: store.revoke(invitation['descriptor']['operator_id'])
    release.set()
    thread.join(5)
    assert not thread.is_alive() and errors == ['rejected']
    assert store.credentials.operation_result('remote:'+sid) is None


@pytest.mark.parametrize('action',['cancel','revoke'])
def test_writer_lock_wins_before_revocation(scenario,monkeypatch,action):
    import threading
    store,invitation,*_ = scenario
    sid,raw = submission(scenario)
    _,value = reserve(scenario,raw)
    entered,release,attempted,ended = [threading.Event() for _ in range(4)]
    original = store.credentials.commit_validated
    def writer(*args,**kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args,**kwargs)
    monkeypatch.setattr(store.credentials,'commit_validated',writer)
    results=[]
    commit_thread=threading.Thread(target=lambda:results.append(store.commit(sid,value,{},current_binding='a'*64)))
    def revoke():
        attempted.set()
        if action == 'cancel': store.cancel(invitation['descriptor']['request_id'])
        else: store.revoke(invitation['descriptor']['operator_id'])
        ended.set()
    commit_thread.start()
    assert entered.wait(5)
    revoke_thread=threading.Thread(target=revoke)
    revoke_thread.start()
    assert attempted.wait(5) and not ended.wait(.05)
    release.set()
    commit_thread.join(5)
    revoke_thread.join(5)
    assert ended.is_set() and results[0]['payload']['status'] == 'saved'
    assert reserve(scenario,raw)[0] == results[0]


def test_direct_commit_retry_reconciles_writer_marker(scenario,monkeypatch):
    store,*_ = scenario
    sid,raw = submission(scenario)
    _,value = reserve(scenario,raw)
    original = store._saved
    monkeypatch.setattr(store,'_saved',lambda *args: (_ for _ in ()).throw(RuntimeError('crash')))
    with pytest.raises(RuntimeError): store.commit(sid,value,{},current_binding='a'*64)
    monkeypatch.setattr(store,'_saved',original)
    assert store.commit(sid,value,{},current_binding='a'*64)['payload']['status'] == 'saved'


def test_pairing_requires_possession_and_local_fingerprint_confirmation(tmp_path):
    from sleeptalk import remote_pairing as pairing
    from fido2.webauthn import AttestationObject
    store = RemoteStore(tmp_path/'remote',CredentialStore(tmp_path/'credentials',backend='file'),clock=lambda:2000000000)
    invitation = store.create_enrollment(workspace_id=identifier(),binding='a'*64,origin=ORIGIN,
        display_host='Host',display_workspace='Workspace',workspace_spec={})
    descriptor = pairing.EnrollmentDescriptor.model_validate(invitation['descriptor'])
    request_id = descriptor.enrollment_id
    key = ec.generate_private_key(ec.SECP256R1())
    credential = AttestedCredentialData.create(Aaguid.NONE,b'new-pair',ES256.from_cryptography_key(key.public_key()))
    client = CollectedClientData.create(type='webauthn.create',challenge=crypto.decode(descriptor.registration_challenge),origin=ORIGIN)
    auth = AuthenticatorData.create(hashlib.sha256(b'setup.example.com').digest(),0x45,0,credential)
    registration = {'id':crypto.encode(credential.credential_id),'rawId':crypto.encode(credential.credential_id),'type':'public-key',
        'response':{'clientDataJSON':crypto.encode(bytes(client)), 'attestationObject':crypto.encode(bytes(AttestationObject.create('none',auth,{})))}}
    encrypted = pairing.encrypt_registration(registration,descriptor=descriptor,capability=invitation['capability'])
    proof = store.enroll_registration(request_id,invitation['capability'],encrypted)
    assert store.enroll_registration(request_id,invitation['capability'],encrypted) == proof
    fingerprint = proof['payload']['fingerprint']
    with pytest.raises(crypto.ProtocolError):
        store.confirm_enrollment(request_id,fingerprint,current_binding='a'*64)
    signed = assertion(crypto.decode(proof['payload']['publicKey']['challenge']),key,credential)
    result = store.enroll_possession(request_id,invitation['capability'],signed)
    assert result['payload']['status'] == 'awaiting_confirmation'
    assert store.enroll_possession(request_id,invitation['capability'],signed) == result
    for actual,binding in [('0'*32,'a'*64),(fingerprint,'b'*64)]:
        with pytest.raises(crypto.ProtocolError):
            store.confirm_enrollment(request_id,actual,current_binding=binding)
    confirmed = store.confirm_enrollment(request_id,fingerprint,current_binding='a'*64)
    status = store.enrollment_status(request_id,capability=invitation['capability'],client_nonce=identifier())['payload']
    assert status['status'] == 'confirmed' and status['operator_id'] == confirmed['operator_id']
    assert store.routes()[0]['request_id'] == request_id


def test_remote_cancel_requires_current_paired_assertion_and_retains_status_route(scenario):
    store,invitation,key,credential,_ = scenario
    request_id,capability = invitation['descriptor']['request_id'],invitation['capability']
    challenge = store.cancel_challenge(request_id,capability,identifier())['payload']
    with pytest.raises(crypto.ProtocolError):
        store.cancel_authorized(request_id,capability,challenge['challenge_id'],assertion(b'wrong',key,credential))
    signed = assertion(crypto.decode(challenge['publicKey']['challenge']),key,credential)
    store.cancel_authorized(request_id,capability,challenge['challenge_id'],signed)
    assert store.status(request_id,capability=capability,client_nonce=identifier())['payload']['status'] == 'cancelled'
    assert store.routes()[0]['request_id'] == request_id
    with pytest.raises(crypto.ProtocolError):
        store.cancel_authorized(request_id,capability,challenge['challenge_id'],signed)


def test_cancel_challenge_cannot_cross_reconnect(scenario):
    store,invitation,key,credential,_ = scenario
    request_id,capability = invitation['descriptor']['request_id'],invitation['capability']
    challenge = store.cancel_challenge(request_id,capability,identifier())['payload']
    signed = assertion(crypto.decode(challenge['publicKey']['challenge']),key,credential)
    store.set_connection(identifier())
    with pytest.raises(crypto.ProtocolError):
        store.cancel_authorized(request_id,capability,challenge['challenge_id'],signed)


def test_expired_keys_are_erased_and_tombstones_pruned(scenario):
    store,invitation,*_,now = scenario
    request_id = invitation['descriptor']['request_id']
    now[0] += 901
    assert store.request_state(request_id)['key'] is None
    now[0] += 86400
    assert store.routes() == []
    with pytest.raises(crypto.ProtocolError):
        store.status(request_id)


@pytest.mark.asyncio
async def test_host_dispatch_validates_off_reader_then_finalizes_reference_profile(scenario,monkeypatch,tmp_path):
    from sleeptalk.remote_host import RemoteHost
    from sleeptalk.private_input import InputSpec
    from sleeptalk import remote_host
    store,invitation,*_ = scenario
    request_id = invitation['descriptor']['request_id']
    spec = InputSpec(request_id='caller-123',profile='fixture',workspace=str(tmp_path),agent='codex',surface='web',
        tts_provider='cartesia',profile_path=str(tmp_path/'profile.toml'))
    with store.transaction() as db:
        state = store._get(db,'request',request_id)
        state['spec'] = spec.model_dump()
        store._put(db,'request',request_id,state)
    monkeypatch.setattr(remote_host,'runtime_binding_digest',lambda spec:'a'*64)
    import asyncio
    entered,release = asyncio.Event(),asyncio.Event()
    async def validator(provider,value,spec):
        assert value == 'synthetic-secret'
        entered.set()
        await release.wait()
        return {'stt_model':'fixture'}
    async def prepare(spec,refs,details,*args):
        return '[credentials.refs]\nDEEPGRAM_API_KEY = "'+refs['deepgram']+'"\n'
    host = RemoteHost(store,ORIGIN,lease_root=tmp_path/'leases',validator=validator,profile_preparer=prepare)
    try:
        assert host.routes()
        sid,raw = submission(scenario)
        receipt = await host.dispatch(request_id,'submission',invitation['capability'],json.loads(raw))
        assert receipt['payload']['status'] == 'validating'
        await entered.wait()
        assert not Path(spec.profile_path).exists()
        release.set()
        while host.tasks:
            await asyncio.gather(*host.tasks)
        status = store.status(request_id)['payload']
        assert status['status'] == 'complete' and status['profile_ready']
        assert 'synthetic-secret' not in Path(spec.profile_path).read_text()
        assert store.request_state(request_id)['key'] is None
        assert store.credentials.status(status['fields']['deepgram.api_key']['reference']).profiles
        assert host.routes()
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_host_does_not_advertise_or_accept_writes_without_profile_lease(scenario,tmp_path):
    from sleeptalk.remote_host import RemoteHost
    from sleeptalk.private_input import lease
    store,invitation,*_ = scenario
    request_id = invitation['descriptor']['request_id']
    path = store.request_state(request_id)['spec']['profile_path']
    leases = tmp_path/'leases'
    with lease(leases/(hashlib.sha256(path.encode()).hexdigest()+'.lock')):
        host = RemoteHost(store,ORIGIN,lease_root=leases)
        assert host.routes() == []
        with pytest.raises(crypto.ProtocolError):
            await host.dispatch(request_id,'challenge',invitation['capability'],{'field':'deepgram.api_key','submission_id':identifier()})
        await host.close()


def test_new_matching_request_reuses_saved_values_after_cancel(scenario):
    store,invitation,*_ = scenario
    sid,raw = submission(scenario)
    _,value = reserve(scenario,raw)
    store.commit(sid,value,{'stt_model':'fixture'},current_binding='a'*64)
    old_id = invitation['descriptor']['request_id']
    store.cancel(old_id)
    state = store.request_state(old_id)
    new = store.create_request(operator_id=invitation['descriptor']['operator_id'],workspace_id=invitation['descriptor']['workspace_id'],
        binding='a'*64,spec=state['spec'],fields=state['targets'],origin=ORIGIN,display_host='Host',display_workspace='Workspace')
    current = store.request_state(new['descriptor']['request_id'])
    assert current['fields'] == state['fields'] and current['key'] is None
    assert current['generation'] == state['generation']
    assert new['descriptor']['request_id'] != old_id


def test_cli_invitation_uses_canonical_json_fragment(scenario):
    from sleeptalk.remote_local import remote_invitation_url
    _,invitation,*_ = scenario
    invitation = {'kind':'setup',**invitation}
    url = remote_invitation_url(invitation)
    assert crypto.parse(crypto.decode(url.split('#',1)[1])) == invitation


def test_repeated_caller_id_returns_existing_status_without_new_secret_capability(scenario):
    store,invitation,*_ = scenario
    old_id = invitation['descriptor']['request_id']
    with store.transaction() as db:
        state = store._get(db,'request',old_id)
        state['spec']['request_id'] = 'stable-caller-id'
        store._put(db,'request',old_id,state)
    repeated = store.create_request(operator_id=invitation['descriptor']['operator_id'],workspace_id=invitation['descriptor']['workspace_id'],
        binding='a'*64,spec=state['spec'],fields=state['targets'],origin=ORIGIN,display_host='Host',display_workspace='Workspace')
    assert repeated['request_id'] == old_id and 'capability' not in repeated
    assert len(store.requests()) == 1


@pytest.mark.asyncio
async def test_profile_finish_retries_transient_failure_but_is_bounded(scenario,tmp_path,monkeypatch):
    from sleeptalk import remote_host
    from sleeptalk.private_input import InputSpec
    store,invitation,*_ = scenario
    request_id = invitation['descriptor']['request_id']
    sid,raw = submission(scenario)
    _,value = reserve(scenario,raw)
    store.commit(sid,value,{},current_binding='a'*64)
    spec = InputSpec(request_id='caller-123',profile='fixture',workspace=str(tmp_path),agent='codex',surface='web',
        tts_provider='cartesia',profile_path=str(tmp_path/'profile.toml'))
    with store.transaction() as db:
        state = store._get(db,'request',request_id)
        state['spec'] = spec.model_dump()
        store._put(db,'request',request_id,state)
    monkeypatch.setattr(remote_host,'runtime_binding_digest',lambda spec:'a'*64)
    attempts = []
    async def failed(*args):
        attempts.append(True)
        raise ValueError('synthetic transient failure')
    host = remote_host.RemoteHost(store,ORIGIN,lease_root=tmp_path/'leases',profile_preparer=failed)
    import asyncio
    try:
        for _ in range(5):
            host.routes()
            await asyncio.gather(*host.tasks)
        assert len(attempts) == 3 and request_id not in host.finishing
        assert store.status(request_id)['payload']['profile_error'] == 'profile_validation_failed'
    finally:
        await host.close()


def test_pending_remote_replacement_cannot_resurrect_deleted_reference(tmp_path):
    store = CredentialStore(tmp_path,backend='file')
    created = store.commit_validated('deepgram','first',operation_id='create',operation_digest='a'*64)
    store.remove(created.reference,affected_profiles=[])
    with pytest.raises(CredentialError):
        store.commit_validated('deepgram','replacement',operation_id='replace',operation_digest='b'*64,
            reference=created.reference,expected_generation=1)
    assert store.operation_result('replace') is None


def test_profile_requires_same_verified_model_credential_generation(scenario):
    from sleeptalk.agent_auth import AgentAuthConfig, auth_scope
    store, invitation, *_ = scenario
    rid = invitation['descriptor']['request_id']
    auth = AgentAuthConfig(mode='api_key', protocol='openai-completions',
        base_url='https://provider.test/v1', credential_ref='credential:' + '0' * 32)
    ref = store.credentials.put_validated(auth_scope(auth), 'model-key')
    auth = auth.model_copy(update={'credential_ref': ref})
    with store.transaction() as db:
        state = store._get(db, 'request', rid)
        state['spec'].update(profile='verified', auth=auth.model_dump())
        store._put(db, 'request', rid, state)
    sid, raw = submission(scenario)
    _, value = reserve(scenario, raw)
    store.commit(sid, value, {}, current_binding='a' * 64)
    generation = store.credentials.status(ref).generation
    with pytest.raises(crypto.ProtocolError, match='Model credential'):
        store.finalize(rid, 'profile', current_binding=lambda: 'a' * 64)
    store.credentials.replace_validated(ref, 'changed-model-key', affected_profiles=[])
    with pytest.raises(CredentialError, match='changed'):
        store.finalize(rid, 'profile', current_binding=lambda: 'a' * 64, auth_generations={ref: generation})
    assert not Path(store.request_state(rid)['spec']['profile_path']).exists()
    store.finalize(rid, 'profile', current_binding=lambda: 'a' * 64,
        auth_generations={ref: store.credentials.status(ref).generation})
    assert store.credentials.status(ref).profiles


def model_scenario(scenario, tmp_path):
    from sleeptalk.agent_auth import AgentAuthChoice
    from sleeptalk.private_input import InputSpec
    store, previous, key, credential, now = scenario
    choice = AgentAuthChoice(mode='api_key', protocol='openai-completions', base_url='https://provider.test/v1')
    spec = InputSpec(request_id='model-entry-123', profile='hermes', workspace=str(tmp_path),
        agent='hermes', runtime='acp', permission_mode='deny', surface='web', tts_provider='cartesia',
        model='selected-model', provider='custom', auth_request=choice, profile_path=str(tmp_path/'hermes.toml'))
    invitation = store.create_request(operator_id=previous['descriptor']['operator_id'],
        workspace_id=previous['descriptor']['workspace_id'], binding='a'*64, spec=spec.model_dump(),
        fields={'agent.credential': {'reference':None, 'generation':0, 'profiles':[]}},
        origin=ORIGIN, display_host='Host', display_workspace='Workspace')
    return (store, invitation, key, credential, now), spec


def model_submission(scenario, value='synthetic-model-secret'):
    store, invitation, key, credential, _ = scenario
    context = crypto.Context.model_validate(store.challenge(invitation['descriptor']['request_id'],
        invitation['capability'], field='agent.credential', submission_id=identifier())['payload']['context'])
    public = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), crypto.decode(invitation['descriptor']['recipient_public_key']))
    enc, ct = crypto.encrypt(value, context, public)
    challenge = crypto.authorization_challenge(context, crypto.decode(enc), crypto.decode(ct))
    return context.submission_id, crypto.canonical({'context':context.model_dump(), 'enc':enc, 'ct':ct,
        'assertion':assertion(challenge,key,credential)})


@pytest.mark.asyncio
async def test_model_entry_verifies_exact_scope_then_finalizes_reference(scenario,tmp_path,monkeypatch):
    import asyncio
    from sleeptalk import remote_host
    from sleeptalk.agent_auth import auth_scope
    fixture, spec = model_scenario(scenario,tmp_path)
    store, invitation, *_ = fixture
    metadata = invitation['descriptor']['model_auth']
    assert metadata == {**spec.auth_request.model_dump(), 'model':spec.model, 'provider':spec.provider}
    checked = []
    async def verify(selection, model):
        assert selection.base_url == metadata['base_url'] and model == spec.model
        assert selection.credential == 'synthetic-model-secret'
        checked.append('verified')
        return 'available'
    async def prepare(selected, refs, details, credentials, *args):
        assert checked == ['verified'] and selected.auth_request is None
        assert not refs and not details
        assert credentials.status(selected.auth.credential_ref).provider == auth_scope(spec.auth_request)
        assert credentials.get(selected.auth.credential_ref) == 'synthetic-model-secret'
        checked.append('prepared')
        return '[agent.auth]\ncredential_ref = "' + selected.auth.credential_ref + '"\n'
    monkeypatch.setattr('sleeptalk.agents.provider_auth.verify', verify)
    monkeypatch.setattr(remote_host,'runtime_binding_digest',lambda selected:'a'*64)
    host = remote_host.RemoteHost(store,ORIGIN,lease_root=tmp_path/'leases',profile_preparer=prepare)
    try:
        host.routes()
        sid, raw = model_submission(fixture)
        rid = invitation['descriptor']['request_id']
        await host.dispatch(rid,'submission',invitation['capability'],json.loads(raw))
        while host.tasks:
            await asyncio.gather(*host.tasks)
        assert checked == ['verified','prepared']
        status = store.status(rid)['payload']
        assert status['profile_ready'] and status['status'] == 'complete'
        ref = status['fields']['agent.credential']['reference']
        assert store.credentials.status(ref).profiles
        assert 'synthetic-model-secret' not in Path(spec.profile_path).read_text()
        assert reserve(fixture,raw)[1] is None
        assert store.credentials.status(ref).generation == 1
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_invalid_model_entry_never_commits_or_prepares(scenario,tmp_path,monkeypatch):
    import asyncio
    from sleeptalk import remote_host
    fixture, spec = model_scenario(scenario,tmp_path)
    store, invitation, *_ = fixture
    async def verify(*args): return 'unavailable'
    async def prepare(*args): pytest.fail('Unverified model credential reached profile preparation.')
    monkeypatch.setattr('sleeptalk.agents.provider_auth.verify',verify)
    monkeypatch.setattr(remote_host,'runtime_binding_digest',lambda selected:'a'*64)
    host = remote_host.RemoteHost(store,ORIGIN,lease_root=tmp_path/'leases',profile_preparer=prepare)
    try:
        host.routes()
        sid, raw = model_submission(fixture)
        rid = invitation['descriptor']['request_id']
        await host.dispatch(rid,'submission',invitation['capability'],json.loads(raw))
        while host.tasks:
            await asyncio.gather(*host.tasks)
        assert store.credentials.operation_result('remote:'+sid) is None
        assert not Path(spec.profile_path).exists()
        assert not store.status(rid)['payload']['profile_ready']
    finally:
        await host.close()


def test_model_collection_intent_changes_binding_and_requires_matching_fields(scenario,tmp_path):
    from sleeptalk.remote_host import runtime_binding_digest
    fixture, spec = model_scenario(scenario,tmp_path)
    store, invitation, *_ = fixture
    assert runtime_binding_digest(spec) != runtime_binding_digest(spec.model_copy(update={'auth_request':None}))
    changed = spec.model_copy(update={'auth_request':spec.auth_request.model_copy(update={'base_url':'https://other.test/v1'})})
    assert runtime_binding_digest(spec) != runtime_binding_digest(changed)
    with pytest.raises(crypto.ProtocolError, match='scope'):
        store.create_request(operator_id=invitation['descriptor']['operator_id'],
            workspace_id=invitation['descriptor']['workspace_id'], binding='a'*64, spec=spec.model_dump(),
            fields={'deepgram.api_key':{'reference':None,'generation':0,'profiles':[]}},
            origin=ORIGIN,display_host='Host',display_workspace='Workspace')
