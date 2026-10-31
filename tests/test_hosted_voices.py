"""Hosted cloning: explicit consent, provider isolation and no automatic retries."""
import base64
import io
import json
import struct
import wave
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import TestClient, TestServer

from sleeptalk.speech import hosted_voices as voices


def recording(seconds=10, sample=1000):
    out = io.BytesIO()
    with wave.open(out, 'wb') as wav:
        wav.setparams((1, 2, 48000, 0, 'NONE', 'not compressed'))
        wav.writeframes(struct.pack('<h', sample) * 48000 * seconds)
    return out.getvalue()


def body(**changes):
    return dict(provider='cartesia', name='My voice', language='en', consent=True,
                audio=base64.b64encode(b'x').decode(), offset=0, upload_id='', final=False) | changes


@pytest.mark.parametrize('change', [{'consent': False}, {'consent': 'true'}, {'provider': []},
    {'name': ''}, {'name': 'bad\nname'}, {'offset': True}, {'audio': '!'}, {'language': 'xx'}, {'extra': 1}])
def test_invalid_uploads(change):
    with pytest.raises(voices.CloneError):
        voices.Upload().accept(body(**change), 'profile:key')


def test_upload_binding_order_expiry_and_replay(monkeypatch):
    upload = voices.Upload()
    token, _ = upload.accept(body(), 'profile:key')
    for changed, binding in [({}, 'other:key'), ({'provider': 'elevenlabs'}, 'profile:key'),
                             ({'offset': 0}, 'profile:key'), ({'name': 'Changed'}, 'profile:key')]:
        with pytest.raises(voices.CloneError):
            upload.accept(body(offset=1, upload_id=token) | changed, binding)
    monkeypatch.setattr(voices.time, 'monotonic', lambda: float('inf'))
    with pytest.raises(voices.CloneError):
        upload.accept(body(offset=1, upload_id=token), 'profile:key')


@pytest.mark.parametrize('provider,seconds,valid', [('cartesia', 10, True), ('cartesia', 60, True),
    ('cartesia', 61, False), ('elevenlabs', 120, True), ('elevenlabs', 121, False), ('elevenlabs', 9, False)])
def test_duration_limits(provider, seconds, valid):
    if valid:
        voices.validate_audio(provider, recording(seconds))
    else:
        with pytest.raises(voices.CloneError):
            voices.validate_audio(provider, recording(seconds))


@pytest.mark.parametrize('audio', [b'bad', recording()[:-10], recording(sample=0)])
def test_invalid_audio(audio):
    with pytest.raises(voices.CloneError):
        voices.validate_audio('cartesia', audio)


class Response:
    def __init__(self, payload, status=200):
        self.payload, self.status = payload, status
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        pass
    async def json(self):
        return self.payload


class Session:
    def __init__(self, responses):
        self.responses, self.calls = iter(responses), []
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        pass
    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return next(self.responses)


@pytest.mark.parametrize('provider', ['cartesia', 'elevenlabs'])
async def test_provider_multipart_and_verification(monkeypatch, provider):
    response = {'id': 'cartesia-id'} if provider == 'cartesia' else {'voice_id': 'eleven-id', 'requires_verification': True}
    session = Session([Response(response)])
    monkeypatch.setattr(voices.aiohttp, 'ClientSession', lambda **kw: session)
    audio = recording()
    result = await voices.create_voice(provider, 'secret', 'My voice', 'en', audio)
    method, url, options = session.calls[0]
    assert method == 'POST' and options['allow_redirects'] is False
    fields = {field[0]['name']: field[2] for field in options['data']._fields}
    assert fields['name'] == 'My voice'
    assert 'secret' not in json.dumps(result)
    if provider == 'cartesia':
        assert url == 'https://api.cartesia.ai/voices/clone'
        assert fields['clip'] == audio and fields['access'] == 'private' and fields['language'] == 'en'
        assert options['headers']['Cartesia-Version'] == '2026-08-14'
        assert result['available']
    else:
        assert url == 'https://api.elevenlabs.io/v1/voices/add'
        assert fields['files'] == audio
        assert options['headers'] == {'xi-api-key': 'secret'}
        assert result['requires_verification'] and not result['available']


@pytest.mark.parametrize('status', [302, 400, 401, 403, 422, 429, 500])
async def test_provider_errors_are_redacted_and_not_retried(status):
    session = Session([Response({'detail': 'SECRET echoed audio'}, status)])
    with pytest.raises(voices.CloneError) as error:
        await voices._request(session, 'POST', 'https://provider.test')
    assert 'SECRET' not in str(error.value)
    assert len(session.calls) == 1


async def test_lists_account_clones_and_verification_after_reload(monkeypatch):
    session = Session([Response({'voices': [
        {'voice_id': 'stock', 'category': 'premade'},
        {'voice_id': 'ready', 'category': 'cloned', 'voice_verification': {'requires_verification': True, 'is_verified': True}},
        {'voice_id': 'pending', 'category': 'cloned', 'voice_verification': {'requires_verification': True, 'is_verified': False}},
        {'voice_id': 'banned', 'category': 'cloned', 'safety_control': 'BAN'}]})])
    monkeypatch.setattr(voices.aiohttp, 'ClientSession', lambda **kw: session)
    result = await voices.list_voices('elevenlabs', 'secret')
    assert [(v['voice_id'], v['available']) for v in result] == [('ready', True), ('pending', False), ('banned', False)]


async def test_cartesia_owned_pagination(monkeypatch):
    session = Session([Response({'data': [{'id': 'a'}], 'next_page': 'a'}), Response({'data': [{'id': 'b', 'status': 'archived'}]})])
    monkeypatch.setattr(voices.aiohttp, 'ClientSession', lambda **kw: session)
    result = await voices.list_voices('cartesia', 'secret')
    assert [v['voice_id'] for v in result] == ['a', 'b'] and not result[1]['available']
    assert session.calls[1][2]['params'] == {'limit': '100', 'is_owner': 'true', 'starting_after': 'a'}


@pytest.fixture
async def client(tmp_path, monkeypatch):
    from sleeptalk.settings_page import SettingsPage
    from sleeptalk.credential_store import CredentialStore
    monkeypatch.setenv('SLEEPTALK_HOME', str(tmp_path / 'home'))
    monkeypatch.setattr(CredentialStore, 'get', lambda self, ref: 'private-key')
    monkeypatch.setattr(CredentialStore, 'status', lambda self, ref: SimpleNamespace(provider='cartesia'))
    path = tmp_path / 'default.toml'
    path.write_text('[agent]\ntype="hermes"\n[transport]\ntype="web"\n[stt]\nprovider="cartesia"\n[tts]\nprovider="cartesia"\n[credentials.refs]\nCARTESIA_API_KEY="saved-ref"\n')
    page = SettingsPage(path, profiles_dir=tmp_path, environ={})
    client = TestClient(TestServer(page.app()))
    await client.start_server()
    page.origin = str(client.make_url('/'))[:-1]
    await client.post('/session', json={'token': page.token}, headers={'Origin': page.origin})
    headers = {'Origin': page.origin, 'X-Settings-CSRF': page.csrf}
    try:
        yield page, client, headers
    finally:
        await client.close()


async def test_authenticated_chunks_create_once_without_switching_profile(client, monkeypatch):
    page, client, headers = client
    original = page.profile_path.read_bytes()
    create = AsyncMock(return_value={'voice_id': 'new-voice', 'name': 'My voice', 'available': True})
    monkeypatch.setattr(voices, 'create_voice', create)
    route = '/api/speech/voices/clone'
    assert (await client.post(route, json=body(), headers={'Origin': page.origin})).status == 403
    assert (await client.post(route, json=body(provider='elevenlabs'), headers=headers)).status == 422
    assert (await client.post(route, json=body(consent=False), headers=headers)).status == 422
    audio = recording()
    token = ''
    for offset in range(0, len(audio), 8192):
        chunk = audio[offset:offset + 8192]
        request = body(audio=base64.b64encode(chunk).decode(), offset=offset, upload_id=token, final=offset + len(chunk) == len(audio))
        assert len(json.dumps(request)) < 16384
        response = await client.post(route, json=request, headers=headers)
        assert response.status == 200, await response.text()
        result = await response.json()
        token = result.get('upload_id', token)
    assert result['voice']['voice_id'] == 'new-voice'
    create.assert_awaited_once_with('cartesia', 'private-key', 'My voice', 'en', audio)
    assert page.profile_path.read_bytes() == original
    assert page.hosted_voice_upload.pending is None
    assert (await client.post(route, json=request, headers=headers)).status == 422
    assert create.await_count == 1


async def test_list_auth_and_busy(client, monkeypatch):
    page, client, headers = client
    listing = AsyncMock(return_value=[])
    monkeypatch.setattr(voices, 'list_voices', listing)
    assert (await client.post('/api/speech/voices', json={'provider': 'cartesia'})).status == 403
    assert (await client.post('/api/speech/voices', json={'provider': 'cartesia'}, headers=headers)).status == 200
    listing.assert_awaited_once_with('cartesia', 'private-key')
    async with page.clone_lock:
        assert (await client.post('/api/speech/voices/clone', json=body(), headers=headers)).status == 409
