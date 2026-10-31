"""Temporary private-entry peer for the browser proof. No real accounts."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'sleeptalk'))
from sleeptalk import agent_auth_input
from sleeptalk.agent_auth import AgentAuthConfig
from sleeptalk.credential_store import CredentialStore


async def main(root):
    store = CredentialStore(root / 'keys', backend='file')
    original_session = agent_auth_input.AuthSession
    async def validate(selected, model):
        return 'available' if selected.credential == 'synthetic-browser-key' and model == 'synthetic-model' else 'unavailable'
    def session(spec, directory):
        return original_session(spec, directory, store=store, validator=validate)
    agent_auth_input.AuthSession = session
    agent_auth_input.CredentialStore = lambda: store
    auth = AgentAuthConfig(mode='api_key', protocol='openai-completions',
        base_url='https://synthetic-provider.test/v1', credential_ref='credential:' + '0' * 32)
    request = agent_auth_input.AuthRequest(request_id='browser-auth-proof', auth=auth, model='synthetic-model')
    result = await agent_auth_input.serve(request, user_browser_local=True,
        root=root / 'requests', on_ready=lambda result: print(json.dumps(result), flush=True))
    assert store.get(result['auth']['credential_ref']) == 'synthetic-browser-key'
    print(json.dumps({'status': result['status'], 'stored': True}), flush=True)


with tempfile.TemporaryDirectory(prefix='sleeptalk-auth-browser-') as directory:
    asyncio.run(main(Path(directory)))
