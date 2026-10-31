"""Uploaded references persist privately and reach Pocket through authenticated settings."""
import base64
import io
import json
import stat
import struct
import sys
import wave
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer

from sleeptalk.speech import local_voices as voices


def recording(seconds=3, sample=3000, rate=24000, channels=1):
    out = io.BytesIO()
    with wave.open(out, 'wb') as clip:
        clip.setparams((channels, 2, rate, 0, 'NONE', 'not compressed'))
        clip.writeframes(struct.pack('<h', sample) * rate * seconds * channels)
    return out.getvalue()


@pytest.fixture(autouse=True)
def private_home(tmp_path, monkeypatch):
    monkeypatch.setenv('SLEEPTALK_HOME', str(tmp_path / 'home'))


def test_saved_voice_is_private_persistent_and_only_offered_for_pocket(tmp_path):
    from sleeptalk import speech_catalog, voice_catalog
    from sleeptalk.speech.local_models import resolve_tts_model, voice_index
    saved = voices.save_voice('My voice', recording())
    voice = saved['voice_id']
    assert voices.list_voices() == [saved]
    assert voices.reference_path(voice).read_bytes() == recording()
    assert stat.S_IMODE(voices.reference_path(voice).stat().st_mode) == 0o600
    assert stat.S_IMODE(voices.voices_dir().stat().st_mode) == 0o700
    model = resolve_tts_model('', voice)
    assert model.id == voices.MODEL and voice_index(model, voice) == 0
    assert voice_catalog.find('local', voice).name == 'My voice'
    from sleeptalk.voice_list import list_local_voices
    assert any(v.id == voice and v.name == 'My voice' for v in list_local_voices())
    catalog = speech_catalog.payload('local', 'tts')
    assert [m['id'] for m in catalog['models'] if voice in (m['voices'] or [])] == [voices.MODEL]
    from sleeptalk.speech_choice import for_new_profile, follow_language
    choice = for_new_profile('local', 'en-US', voice=voice)
    assert choice.tts_model == voices.MODEL and choice.voice == voice
    changed, _ = follow_language(choice, 'en-GB')
    assert changed.voice == voice
    # The name never becomes a filename or a profile value.
    other = voices.save_voice('../../my voice', recording())
    assert voices.reference_path(other['voice_id']).parent == voices.voices_dir()
    assert not (tmp_path / 'my voice').exists()


@pytest.mark.parametrize('audio', [b'garbage', recording(1), recording(31), recording(sample=0),
                                   recording(rate=8000), recording(channels=2), recording()[:-10]])
def test_bad_recordings_leave_no_saved_voice(audio):
    with pytest.raises(ValueError):
        voices.save_voice('Example', audio)
    assert voices.list_voices() == []


def test_missing_and_path_like_references_fail_closed(tmp_path):
    for value in ('../private.wav', '/tmp/private.wav', 'pocket-custom-' + '0'*32):
        with pytest.raises(ValueError):
            voices.reference_path(value)
    saved = voices.save_voice('Example', recording())
    path = voices.reference_path(saved['voice_id'])
    path.unlink()
    path.symlink_to(tmp_path / 'private.wav')
    assert voices.list_voices() == []
    with pytest.raises(ValueError):
        voices.reference_path(saved['voice_id'])


@pytest.mark.parametrize('seconds', [3, 13, 30])
async def test_uploaded_reference_reaches_streaming_engine(monkeypatch, seconds):
    import numpy as np
    from sleeptalk.providers import ProviderSpec
    from sleeptalk.speech.local_tts import LocalTTS
    saved = voices.save_voice('Example', recording(seconds))
    monkeypatch.setitem(sys.modules, 'sherpa_onnx', SimpleNamespace(GenerationConfig=SimpleNamespace))
    seen = []
    def generate(text, config, *, callback):
        seen.append(config)
        callback(np.full(2400, 0.2, dtype=np.float32), 1)
    speaker = LocalTTS(ProviderSpec(provider='local', model=voices.MODEL, voice_id=saved['voice_id']))
    speaker._tts = SimpleNamespace(sample_rate=24000, generate=generate)
    try:
        assert await speaker.synthesize_pcm('A new sentence in the uploaded voice.')
        assert seen[0].reference_sample_rate == 24000
        assert len(seen[0].reference_audio) == seconds * 24000
        assert seen[0].reference_audio[0] == 3000 / 32768
        # The native engine caps the reference even when all samples are supplied.
        assert float(seen[0].extra['max_reference_audio_len']) >= seconds
        speaker.set_voice('af_heart')
        speaker.set_voice(saved['voice_id'])
        assert speaker.model.id == voices.MODEL
    finally:
        await speaker.close()


@pytest.fixture
async def page_client(tmp_path):
    from sleeptalk.settings_page import SettingsPage
    profile = tmp_path / 'default.toml'
    profile.write_text('[agent]\ntype="hermes"\n[transport]\ntype="web"\n[stt]\nprovider="local"\n[tts]\nprovider="local"\nmodel="pocket-tts-en"\nvoice_id="pocket-bria"\n')
    page = SettingsPage(profile, profiles_dir=tmp_path, environ={})
    client = TestClient(TestServer(page.app()))
    await client.start_server()
    page.origin = str(client.make_url('/'))[:-1]
    await client.post('/session', json={'token': page.token}, headers={'Origin': page.origin})
    headers = {'Origin': page.origin, 'X-Settings-CSRF': page.csrf}
    try:
        yield page, client, headers
    finally:
        await client.close()


async def test_chunked_upload_auth_order_persistence_and_profile_selection(page_client, monkeypatch):
    from sleeptalk import settings_page
    page, client, headers = page_client
    audio = recording(10)
    body = dict(model=voices.MODEL, name='New voice', audio='', offset=0, upload_id='', final=False)
    route = '/api/speech/local/voices'
    body['audio'] = base64.b64encode(audio[:8192]).decode()
    assert (await client.post(route, json=body, headers={'Origin':page.origin})).status == 403
    bad = {**body, 'model':'kokoro-en'}
    assert (await client.post(route, json=bad, headers=headers)).status == 400
    token = ''
    for offset in range(0, len(audio), 8192):
        chunk = audio[offset:offset+8192]
        body.update(audio=base64.b64encode(chunk).decode(), offset=offset, upload_id=token,
                    final=offset+len(chunk)==len(audio))
        assert len(json.dumps(body).encode()) < 16384
        response = await client.post(route, json=body, headers=headers)
        assert response.status == 200, await response.text()
        result = await response.json()
        token = result.get('upload_id', token)
        if offset == 0:
            assert voices.list_voices() == []
            assert (await client.post(route, json={**body, 'offset':123, 'upload_id':token}, headers=headers)).status == 409
    voice = result['voice']['voice_id']
    assert page.voice_upload is None
    assert voices.reference_path(voice).read_bytes() == audio
    assert settings_page._local_models('', voices.MODEL, voice)[1].id == voices.MODEL
    state = settings_page.describe_profile(page.profile_path)
    assert any(v['voice_id']==voice for v in state['voice']['voices'])
    async def prove(*args): pass
    monkeypatch.setattr(settings_page, 'prove_local_speech', prove)
    await settings_page.switch_to_local(page.profile_path, '', voices.MODEL, voice)
    assert settings_page.describe_profile(page.profile_path)['voice']['voice_id'] == voice


async def test_custom_preview_uses_pocket_without_changing_current_profile(page_client, monkeypatch):
    from sleeptalk.speech.local_tts import LocalTTS
    page, client, headers = page_client
    voice = voices.save_voice('Example', recording())['voice_id']
    before = page.profile_path.read_bytes()
    seen = []
    async def synth(self, text):
        seen.append((self.model.id, self.spec.voice_id))
        return recording()
    monkeypatch.setattr(LocalTTS, 'synthesize_wav_bytes', synth)
    response = await client.post('/api/speech/local/voices/preview', json={'model': voices.MODEL, 'voice_id':voice}, headers=headers)
    assert response.status == 200 and response.content_type == 'audio/wav'
    assert seen == [(voices.MODEL, voice)]
    assert page.profile_path.read_bytes() == before
    assert (await client.post('/api/speech/local/voices/preview', json={'model':'kokoro-en', 'voice_id':'af_heart'}, headers=headers)).status == 422
    assert (await client.post('/api/speech/local/voices/preview', json={'model':voices.MODEL, 'voice_id':'af_heart'}, headers=headers)).status == 422


async def test_interrupted_expired_and_oversized_uploads_do_not_save(page_client):
    page, client, headers = page_client
    route = '/api/speech/local/voices'
    body = dict(model=voices.MODEL, name='Example', audio=base64.b64encode(b'1234').decode(),
                offset=0, upload_id='', final=False)
    response = await client.post(route, json=body, headers=headers)
    token = (await response.json())['upload_id']
    assert (await client.post(route, json={**body, 'offset':4, 'upload_id':'wrong'}, headers=headers)).status == 409
    page.voice_upload['expires'] = 0
    assert (await client.post(route, json={**body, 'offset':4, 'upload_id':token}, headers=headers)).status == 409
    page.voice_upload.update(expires=float('inf'), audio=bytearray(voices.MAX_AUDIO_BYTES))
    assert (await client.post(route, json={**body, 'offset':voices.MAX_AUDIO_BYTES, 'upload_id':token}, headers=headers)).status == 413
    assert page.voice_upload is None and voices.list_voices() == []
    assert (await client.post(route, json={**body, 'audio':'not base64!'}, headers=headers)).status == 400
    assert (await client.post(route, json={**body, 'final':True}, headers=headers)).status == 422
    assert voices.list_voices() == []


async def test_voice_upload_through_the_sealed_phone_channel(tmp_path):
    import asyncio
    from tests.test_remote_settings import _room, _call
    fake, server, room, _key = await _room(tmp_path)
    task = asyncio.create_task(room.run())
    audio = recording()
    token = ''
    try:
        await fake.expect('call.auth')
        await fake.send('peer.ready')
        for index, offset in enumerate(range(0, len(audio), 8192)):
            chunk = audio[offset:offset+8192]
            body = dict(model=voices.MODEL, name='Phone upload test',
                        audio=base64.b64encode(chunk).decode(), offset=offset, upload_id=token,
                        final=offset+len(chunk)==len(audio))
            status, _, data = await _call(fake, index+1, 'POST', '/api/speech/local/voices', body)
            assert status == 200, data
            result = json.loads(data)
            token = result.get('upload_id', token)
        assert voices.reference_path(result['voice']['voice_id']).read_bytes() == audio
        assert 'Phone upload test' not in ''.join(fake.raw)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await server.close()
