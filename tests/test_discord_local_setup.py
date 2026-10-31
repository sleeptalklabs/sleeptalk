"""Reuse local speech for a second Discord bot without a speech credential."""
import tomllib
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from sleeptalk import settings_page
from sleeptalk.credential_store import CredentialStore
from sleeptalk.discord_check import VoiceChannel
from sleeptalk.private_input import InputSession, InputSpec, atomic_json, speech_seed, status


@pytest.mark.parametrize('stt,tts,model,voice', [
    ('local', 'local', 'kokoro-en', 'af_heart'),
    ('local', 'local', 'piper-lessac-medium', 'lessac'),
    ('cartesia', 'local', 'kokoro-en', 'af_heart'),
    ('local', 'cartesia', 'sonic-3.6', 'voice-1'),
])
async def test_create_discord_profile_reuses_local_speech(tmp_path, monkeypatch, stt, tts, model, voice):
    store = CredentialStore(tmp_path / 'store', backend='file')
    monkeypatch.setattr('sleeptalk.credential_store.default_root', lambda: tmp_path / 'store')
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    # The selected source need not live in the globally resolved profiles directory.
    profiles = tmp_path / 'selected-profiles'
    profiles.mkdir()
    source = profiles / 'local.toml'
    stt_model = 'moonshine-small-streaming-en' if stt == 'local' else 'ink-2'
    source.write_text(
        f'[agent]\ntype = "pi"\nworkspace_dir = "{tmp_path}"\n'
        '[transport]\ntype = "web"\n'
        f'[stt]\nprovider = "{stt}"\nmodel = "{stt_model}"\nlanguage = "en-US"\n'
        f'[tts]\nprovider = "{tts}"\nmodel = "{model}"\nvoice_id = "{voice}"\n'
        'language = "en-US"\nspeed = 1.25\n'
    )
    cloud_ref = ''
    if 'cartesia' in (stt, tts):
        assert not settings_page.describe_profile(source)['discord']['speech_ready']
        cloud_ref = store.put_validated('cartesia', 'speech-key', request_id='speech-key')
        with source.open('a') as f:
            f.write(f'[credentials.refs]\nCARTESIA_API_KEY = "{cloud_ref}"\n')
    original = source.read_bytes()
    existing = profiles / 'kanye-discord.toml'
    existing.write_text('[transport]\ntype = "discord"\n')
    assert settings_page.describe_profile(source)['discord']['speech_ready']
    assert settings_page.describe_profile(source)['discord']['suggested_profile'] == 'discord'

    async def channels(token, guild):
        assert token == 'bot-token' and guild == '456'
        return [VoiceChannel(id='789', name='General', eligible=True, missing_permissions=())]

    monkeypatch.setattr('sleeptalk.discord_check.list_voice_channels', channels)
    page = settings_page.SettingsPage(source, profiles_dir=profiles, environ={}, state_dir=tmp_path / 'state')
    page.discord.reference = store.put_validated('discord', 'bot-token', request_id='bot-key')
    page.discord.details = {'application': {'id': '123'}, 'guilds': [{'id': '456', 'name': 'Server'}]}
    async with TestClient(TestServer(page.app())) as client:
        page.origin = str(client.make_url('/'))[:-1]
        session = await client.post('/session', json={'token': page.token}, headers={'Origin': page.origin})
        headers = {'Origin': page.origin, 'X-Settings-CSRF': (await session.json())['csrf']}
        response = await client.post('/api/discord/finish', headers=headers, json={
            'profile': 'local-koko', 'guild_id': '456', 'channel_id': '789', 'operator_id': '111',
        })
        result = await response.json()
        assert response.status == 200, result
    document = tomllib.loads(Path(result['path']).read_text())
    assert document['stt']['provider'] == stt
    assert document['stt']['model'] == stt_model
    assert document['tts'] == {'provider': tts, 'model': model, 'voice_id': voice, 'language': 'en-US', 'speed': 1.25}
    assert document['transport']['type'] == 'discord'
    assert document['transport']['allowed_channel_ids'] == '789'
    assert document['transport']['operator_ids'] == '111'
    assert document['transport']['authorization_required'] is True
    expected = {'DISCORD_BOT_TOKEN': page.discord.reference}
    if cloud_ref:
        expected['CARTESIA_API_KEY'] = cloud_ref
    assert document['credentials']['refs'] == expected
    assert source.read_bytes() == original
    assert existing.read_text() == '[transport]\ntype = "discord"\n'


def test_local_speech_seed_survives_request_restore_without_asking_for_a_key(tmp_path):
    source = tmp_path / 'local.toml'
    source.write_text('[stt]\nprovider = "local"\nmodel = "moonshine-small-streaming-en"\n'
                      '[tts]\nprovider = "local"\nmodel = "kokoro-en"\nvoice_id = "af_heart"\n')
    spec = InputSpec(request_id='local-discord', profile='bot', workspace=str(tmp_path), agent='pi',
                     surface='discord', speech_from_profile='local')
    spec, refs, details = speech_seed(spec, source_path=source)
    assert refs == {}
    directory = tmp_path / 'setup' / spec.request_id
    atomic_json(directory / 'request.json', spec.model_dump())
    atomic_json(directory / 'state.json', {'status': 'waiting', 'created_at': 1, 'expires_at': 9999999999,
                                         'refs': refs, 'details': details, 'mode': 'setup'})
    session = InputSession(directory, store=CredentialStore(tmp_path / 'store', backend='file'))
    session.restore()
    assert session.allowed_providers() == {'discord'}
    assert session.state['details']['local']['tts_model'] == 'kokoro-en'
    assert status(spec.request_id, root=directory.parent)['pending_providers'] == ['discord']
