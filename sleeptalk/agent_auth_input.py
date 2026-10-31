"""User-owned local browser entry for explicitly selected model credentials."""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
import secrets
import time

from aiohttp import web
from pydantic import BaseModel, ConfigDict, Field

from sleeptalk.agent_auth import AgentAuthConfig, auth_metadata, auth_scope
from sleeptalk.agents.provider_auth import ProviderSelection, verify
from sleeptalk.credential_store import CredentialError, CredentialStore
from sleeptalk.private_input import atomic_json, lease, state_root

LIFETIME = 900


class AuthRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', hide_input_in_errors=True)
    request_id: str = Field(pattern=r'^[A-Za-z0-9_-]{8,128}$')
    auth: AgentAuthConfig
    model: str = Field(min_length=1, max_length=512)


def directory(request_id: str, root: Path | None = None) -> Path:
    import re
    if not re.fullmatch(r'[A-Za-z0-9_-]{8,128}', request_id):
        raise ValueError('Use a valid request ID.')
    return (root or state_root() / 'agent-auth') / request_id


def _completed(request_id: str, auth: AgentAuthConfig, reference: str, store) -> dict:
    try:
        metadata = store.status(reference)
        available = metadata.available and metadata.provider == auth_scope(auth)
    except CredentialError:
        available = False
    if not available:
        return {'status': 'needs_input', 'request_id': request_id, 'code': 'credential_unavailable',
            'next_action': 'The saved model credential is unavailable. Start private entry with a new request ID.'}
    return {'status': 'complete', 'request_id': request_id,
        'auth': {**auth_metadata(auth), 'credential_ref': reference}}


def status(request_id: str, root: Path | None = None, *, store=None) -> dict:
    path = directory(request_id, root)
    try:
        state = json.loads((path / 'status.json').read_text())
    except FileNotFoundError:
        return {'status': 'unknown', 'request_id': request_id}
    selected = store if store is not None else CredentialStore()
    if state['status'] == 'complete':
        auth = AgentAuthConfig.model_validate(state['auth'])
        return _completed(request_id, auth, auth.credential_ref, selected)
    if (path / 'request.json').exists():
        spec = AuthRequest.model_validate_json((path / 'request.json').read_text())
        committed = selected.operation_result('agent-auth:' + request_id)
        if committed and committed.provider == auth_scope(spec.auth):
            return _completed(request_id, spec.auth, committed.reference, selected)
    if state['status'] == 'waiting':
        if (path / 'cancel.json').exists():
            state['status'] = 'cancelled'
        elif time.time() >= state['expires_at']:
            state['status'] = 'expired'
    return state


def cancel(request_id: str, root: Path | None = None, *, store=None) -> dict:
    path = directory(request_id, root)
    with lease(path / 'write.lock'):
        state = status(request_id, root, store=store)
        if state['status'] == 'waiting':
            atomic_json(path / 'cancel.json', {})
            state['status'] = 'cancelled'
        return state


PAGE = b'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Sleeptalk model credential</title></head><body><main><h1>Connect your model provider</h1><p id="scope"></p><p>Enter the selected API key or already-issued OAuth access token here. Keep it out of chat.</p><form><label>Private credential <input id="credential" type="password" autocomplete="off" required maxlength="32768"></label><label><input id="confirm" type="checkbox" required> I trust this provider endpoint to receive this credential.</label><button>Validate and save</button></form><button id="cancel" type="button">Cancel</button><p id="status" role="status">Opening private entry...</p></main><script src="/auth.js"></script></body></html>'''
SCRIPT = b'''const output=document.querySelector('#status'),form=document.querySelector('form'),input=document.querySelector('#credential');let csrf='',expired=false,timer;
let invitation=location.hash.slice(1);history.replaceState(null,'',location.pathname);
async function request(path,body){const r=await fetch(path,{method:body?'POST':'GET',credentials:'same-origin',cache:'no-store',redirect:'error',headers:{'Content-Type':'application/json','X-Setup-CSRF':csrf},...(body?{body:JSON.stringify(body)}:{})});const result=await r.json();if(!r.ok)throw new Error();return result}
function finish(message){expired=true;input.value='';form.hidden=true;document.querySelector('#cancel').hidden=true;clearTimeout(timer);output.textContent=message}
(async()=>{try{const s=await request('/session',{invitation});invitation='';csrf=s.csrf;document.querySelector('#scope').textContent=s.mode+' via '+s.protocol+' at '+s.base_url+' for '+s.model;output.textContent='Ready for private entry.';timer=setTimeout(()=>finish('Invitation expired. Return to your agent.'),Math.max(0,s.expires_at*1000-Date.now()))}catch{finish('Invitation unavailable. Return to your agent.')}})();
form.addEventListener('submit',async e=>{e.preventDefault();if(expired)return;const value=input.value;input.value='';form.querySelector('button').disabled=true;try{const s=await request('/credential',{value,confirmed:document.querySelector('#confirm').checked});if(s.status==='complete')finish('Credential saved. Return to your agent.');else output.textContent='The provider could not verify this credential and model. Check them privately and retry.'}catch{output.textContent='Check status with your agent before retrying.'}finally{form.querySelector('button').disabled=false}});
document.querySelector('#cancel').addEventListener('click',async()=>{try{await request('/cancel',{});finish('Cancelled.')}catch{finish('Return to your agent to cancel.')}});
'''


class AuthSession:
    def __init__(self, spec: AuthRequest, root: Path, *, store=None, validator=verify):
        self.spec, self.root = spec, root
        self.path = directory(spec.request_id, root)
        self.store = store if store is not None else CredentialStore()
        self.validator = validator
        self.origin = ''
        self.invitation, self.cookie, self.csrf = (secrets.token_urlsafe(32) for _ in range(3))
        # One session per invitation; a repeat exchange needs its cookie.
        self.invitation_used = False
        self.cookie_name = 'agent_auth_' + hashlib.sha256(spec.request_id.encode()).hexdigest()[:16]
        self.expires_at = time.time() + LIFETIME
        self.lock = asyncio.Lock()

    def active(self):
        if status(self.spec.request_id, self.root, store=self.store)['status'] != 'waiting':
            raise web.HTTPGone()

    def authenticated(self, request):
        self.active()
        if not secrets.compare_digest(request.cookies.get(self.cookie_name, ''), self.cookie):
            raise web.HTTPUnauthorized()
        if not secrets.compare_digest(request.headers.get('X-Setup-CSRF', ''), self.csrf):
            raise web.HTTPForbidden()

    async def exchange(self, request):
        self.active()
        body = await request.json()
        if not isinstance(body, dict) or not isinstance(body.get('invitation'), str) or not secrets.compare_digest(body['invitation'], self.invitation):
            raise web.HTTPUnauthorized()
        if self.invitation_used and not secrets.compare_digest(request.cookies.get(self.cookie_name, ''), self.cookie):
            raise web.HTTPUnauthorized()
        self.invitation_used = True
        response = web.json_response({**auth_metadata(self.spec.auth), 'model': self.spec.model,
            'csrf': self.csrf, 'expires_at': self.expires_at})
        response.set_cookie(self.cookie_name, self.cookie, httponly=True, samesite='Strict', max_age=LIFETIME)
        return response

    async def submit(self, request):
        self.authenticated(request)
        async with self.lock:
            self.active()
            body = await request.json()
            if not isinstance(body, dict) or set(body) != {'value', 'confirmed'} or body.get('confirmed') is not True:
                raise web.HTTPBadRequest()
            value = body.get('value')
            if not isinstance(value, str) or not value or len(value) > 32768:
                raise web.HTTPBadRequest()
            try:
                selected = ProviderSelection.from_environment({'SLEEPTALK_PROVIDER_AUTH': json.dumps(auth_metadata(self.spec.auth)), 'SLEEPTALK_PROVIDER_CREDENTIAL': value})
                async with asyncio.timeout(15):
                    result = await self.validator(selected, self.spec.model)
                with lease(self.path / 'write.lock'):
                    self.active()
                    if result != 'available':
                        return web.json_response({'status': 'needs_input'})
                    committed = self.store.commit_validated(auth_scope(self.spec.auth), value,
                        operation_id='agent-auth:' + self.spec.request_id,
                        operation_digest=hashlib.sha256(self.spec.model_dump_json().encode()).hexdigest())
                    reference = committed.reference
                    receipt = {'status': 'complete', 'request_id': self.spec.request_id,
                        'auth': {**auth_metadata(self.spec.auth), 'credential_ref': reference}}
                    atomic_json(self.path / 'status.json', receipt)
                return web.json_response({'status': 'complete'})
            finally:
                body.clear()
                value = ''

    async def cancel(self, request):
        self.authenticated(request)
        return web.json_response(cancel(self.spec.request_id, self.root, store=self.store))

    def app(self):
        @web.middleware
        async def boundary(request, handler):
            if not self.origin or request.headers.get('Host') != self.origin.removeprefix('http://'):
                raise web.HTTPForbidden()
            if request.method != 'GET' and request.headers.get('Origin') != self.origin:
                raise web.HTTPForbidden()
            try:
                response = await handler(request)
            except web.HTTPException as exc:
                response = web.json_response({'status': 'unavailable'}, status=exc.status)
            except Exception:
                response = web.json_response({'status': 'needs_input'}, status=400)
            response.headers.update({'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer',
                'X-Content-Type-Options': 'nosniff', 'Permissions-Policy': 'camera=(), microphone=(), geolocation=()',
                'Content-Security-Policy': "default-src 'none'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"})
            return response
        app = web.Application(middlewares=[boundary], client_max_size=40000)
        async def page(request): return web.Response(body=PAGE, content_type='text/html')
        async def script(request): return web.Response(body=SCRIPT, content_type='application/javascript')
        app.router.add_get('/', page)
        app.router.add_get('/auth.js', script)
        app.router.add_post('/session', self.exchange)
        app.router.add_post('/credential', self.submit)
        app.router.add_post('/cancel', self.cancel)
        return app


async def serve(spec: AuthRequest, *, user_browser_local: bool, on_ready, root: Path | None = None) -> dict:
    if not user_browser_local:
        raise ValueError('Model credential entry requires a user-controlled browser on this runtime host.')
    root = root or state_root() / 'agent-auth'
    session = AuthSession(spec, root)
    with lease(session.path / 'running.lock'):
        saved = session.path / 'request.json'
        if saved.exists() and json.loads(saved.read_text()) != spec.model_dump():
            raise ValueError('This request ID belongs to different provider choices.')
        current = status(spec.request_id, root)
        if current['status'] == 'complete':
            return current
        if current['status'] not in {'unknown', 'waiting'}:
            return current
        atomic_json(saved, spec.model_dump())
        atomic_json(session.path / 'status.json', {'status': 'waiting', 'request_id': spec.request_id, 'expires_at': session.expires_at})
        runner = web.AppRunner(session.app(), access_log=None)
        await runner.setup()
        try:
            site = web.TCPSite(runner, '127.0.0.1', 0)
            await site.start()
            session.origin = f'http://127.0.0.1:{runner.addresses[0][1]}'
            on_ready({'status': 'needs_input', 'request_id': spec.request_id,
                'input_url': session.origin + '/#' + session.invitation,
                'next_action': 'Give this private invitation to the user. Do not open it with an agent tool.'})
            while status(spec.request_id, root)['status'] == 'waiting':
                await asyncio.sleep(.25)
            return status(spec.request_id, root)
        finally:
            session.invitation = ''
            await runner.cleanup()


def add_parser(host_sub) -> None:
    parser = host_sub.add_parser('auth', help='Privately validate and save a model-provider credential.')
    sub = parser.add_subparsers(dest='auth_command', required=True)
    start = sub.add_parser('start', help='Run a private entry page for the user on this host.')
    start.add_argument('--request-id', required=True)
    start.add_argument('--mode', choices=('api_key',), required=True)
    start.add_argument('--protocol', choices=('openai-completions', 'openai-responses', 'openai-codex-responses', 'anthropic-messages', 'cursor'), required=True)
    start.add_argument('--base-url', required=True)
    start.add_argument('--model', required=True)
    start.add_argument('--user-browser-local', action='store_true')
    for name in ('status', 'cancel'):
        sub.add_parser(name).add_argument('--request-id', required=True)


def run(args) -> int | None:
    if args.host_command != 'auth':
        return None
    def emit(receipt): print(json.dumps(receipt, sort_keys=True), flush=True)
    try:
        if args.auth_command == 'start':
            auth = AgentAuthConfig(mode=args.mode, protocol=args.protocol, base_url=args.base_url,
                credential_ref='credential:' + '0' * 32)
            spec = AuthRequest(request_id=args.request_id, auth=auth, model=args.model)
            receipt = asyncio.run(serve(spec, user_browser_local=args.user_browser_local, on_ready=emit))
        else:
            receipt = (status if args.auth_command == 'status' else cancel)(args.request_id)
    except (ValueError, OSError, CredentialError):
        emit({'status': 'needs_input', 'code': 'model_auth_unavailable',
            'next_action': 'Check the selected provider, model and private storage. Local entry requires a user-controlled browser on this runtime host.'})
        return 2
    emit(receipt)
    return 0 if receipt['status'] in {'complete', 'cancelled'} else 2
