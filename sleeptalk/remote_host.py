"""Host-owned remote setup orchestration; the relay never validates provider keys."""
from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

from sleeptalk.private_input import InputError, InputSpec, lease, state_root, prepare_profile, validate_provider, _validation_code
from sleeptalk.remote_crypto import ProtocolError, canonical, decode
from sleeptalk.remote_relay import HostRelayClient, origin as validate_origin
from sleeptalk.remote_store import FIELDS, RemoteStore, digest, identifier
from sleeptalk.runtime_binding import bind, canonical_workspace


def runtime_binding_digest(spec: InputSpec) -> str:
    binding = bind(spec.agent_config()).model_dump()
    return digest({'runtime': binding, 'auth_request': spec.auth_request.model_dump()}) if spec.auth_request else digest(binding)


async def selected_spec(spec: InputSpec) -> InputSpec:
    from sleeptalk.host import resolve_profile_path
    from sleeptalk.runtime_probe import probe_runtime
    spec = spec.model_copy(update={'workspace':str(canonical_workspace(spec.workspace)),
        'profile_path':str(resolve_profile_path(profile=spec.profile).resolve())})
    from sleeptalk.setup import ensure_dependencies
    if ensure_dependencies(spec.surface,repair=False):
        raise InputError('Install the selected surface dependencies before remote setup.')
    if spec.auth_request:
        from sleeptalk.agent_environment import agent_environment
        from sleeptalk.agents.construction import resolve_agent
        from sleeptalk.config import SleeptalkConfig, _validate_component_choices
        from sleeptalk.agents.hermes_bridge import probe_configuration
        agent = spec.agent_config()
        selected = resolve_agent(agent, agent_environment())
        if selected.registration.name not in {'acp', 'hermes_gateway'} or (selected.selection and selected.selection.fallback_from):
            raise InputError('Private remote model collection requires an explicitly selected protected Hermes runtime.')
        if _validate_component_choices(SleeptalkConfig(agent=agent)):
            raise InputError('The selected model and installation settings do not apply to this adapter.')
        report = await probe_configuration(agent, spec.auth_request.model_dump())
        if report.get('configuration') != 'available' or report.get('login') != 'unknown' or any(report.get(key) != 'available' for key in ('workspace', 'permissions', 'protocol')):
            raise InputError('The protected Hermes configuration could not be verified before private entry.')
    elif (await probe_runtime(spec.agent_config()))['status'] != 'ready':
        raise InputError('The selected adapter must pass host probe-runtime before remote setup.')
    if spec.surface == 'discord':
        import re
        if not all(re.fullmatch(r'[0-9]{1,20}',getattr(spec,key)) for key in ('guild_id','channel_id','operator_id')):
            raise InputError('Select the Discord server, channel and user IDs before remote setup.')
    return spec


async def prepare_pairing(store: RemoteStore, origin: str, spec: InputSpec, display_host='Sleeptalk host') -> dict:
    origin = validate_origin(origin)
    spec = await selected_spec(spec)
    return store.create_enrollment(workspace_id=identifier(),binding=runtime_binding_digest(spec),origin=origin,
        display_host=display_host,display_workspace=Path(spec.workspace).name,workspace_spec=spec.model_dump())


async def start_setup(store: RemoteStore, origin: str, operator_id: str, workspace_id: str,
                      spec: InputSpec, display_host='Sleeptalk host') -> dict:
    origin = validate_origin(origin)
    spec = await selected_spec(spec)
    if spec.replace_reference:
        metadata = store.credentials.status(spec.replace_reference)
        if set(metadata.profiles) != set(spec.affected_profiles):
            raise InputError('Every affected profile must be explicitly selected for replacement.')
        fields = {field:{'reference':metadata.reference,'generation':metadata.generation,'profiles':list(metadata.profiles)}
            for field,provider in FIELDS.items() if provider == metadata.provider}
        if metadata.provider not in spec.speech_providers | ({'discord'} if spec.surface == 'discord' else set()):
            raise InputError('The replacement provider is outside this profile scope.')
    else:
        fields = {field:{'reference':None,'generation':0,'profiles':[]} for field,provider in FIELDS.items()
            if provider in spec.speech_providers | ({'discord'} if spec.surface == 'discord' else set())}
    if spec.auth_request:
        fields['agent.credential'] = {'reference': None, 'generation': 0, 'profiles': []}
    binding = runtime_binding_digest(spec)
    # The creation transaction records the caller's stable ID alongside the request.
    result = store.create_request(operator_id=operator_id,workspace_id=workspace_id,binding=binding,
        spec=spec.model_dump(),fields=fields,origin=origin,display_host=display_host,display_workspace=Path(spec.workspace).name)
    return {'kind':'setup',**result} if 'descriptor' in result else result


class RemoteHost:
    def __init__(self, store: RemoteStore, origin: str, *, lease_root=None, validator=None,
                 profile_preparer=None, session=None):
        self.store, self.origin = store, validate_origin(origin)
        self.lease_root = Path(lease_root) if lease_root else state_root()
        self.validator = validator or validate_provider
        self.profile_preparer = profile_preparer or prepare_profile
        self.profile_leases = {}
        self.tasks = set()
        self.finishing = set()
        self.finish_attempts = {}
        self.client = HostRelayClient(self,self.origin,self.dispatch,session=session)
        self.run_task = None
        self.host_lease = None
        self.host_id, self.key = store.host_id, store.key

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, *_):
        await self.close()

    async def start(self):
        if self.run_task:
            return
        self.host_lease = lease(self.store.root/'serve.lock')
        self.host_lease.__enter__()
        self.run_task = asyncio.create_task(self.client.run())

    async def run(self):
        await self.start()
        try:
            await self.run_task
        finally:
            await self.close()

    async def close(self):
        await self.client.close()
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*self.tasks,return_exceptions=True)
        if self.run_task:
            self.run_task.cancel()
            await asyncio.gather(self.run_task,return_exceptions=True)
            self.run_task = None
        for held in self.profile_leases.values():
            held.__exit__(None,None,None)
        self.profile_leases.clear()
        if self.host_lease:
            self.host_lease.__exit__(None,None,None)
            self.host_lease = None

    def background(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    def set_connection(self, generation):
        self.store.set_connection(generation)

    def routes(self):
        active = {}
        for state in self.store.requests():
            if state['descriptor']['relay_origin'] != self.origin:
                continue
            request_id = state['descriptor']['request_id']
            active[request_id] = state
            if request_id not in self.profile_leases:
                lock_id = hashlib.sha256(state['spec']['profile_path'].encode()).hexdigest()
                held = lease(self.lease_root/(lock_id+'.lock'))
                try:
                    held.__enter__()
                except InputError:
                    continue
                self.profile_leases[request_id] = held
            if (all(f['status'] == 'saved' for f in state['fields'].values()) and request_id not in self.finishing
                    and self.finish_attempts.get(request_id,0) < 3):
                self.finishing.add(request_id)
                self.background(self.finish(request_id))
        for request_id in set(self.profile_leases)-active.keys():
            self.profile_leases.pop(request_id).__exit__(None,None,None)
        with self.store.transaction() as db:
            enrollments = {key for key,state in self.store._all(db,'enrollment')
                if state['descriptor']['relay_origin'] == self.origin}
            completed = {key for key,state in self.store._all(db,'request')
                if state['status'] != 'open' and state['descriptor']['relay_origin'] == self.origin}
        allowed = set(self.profile_leases) | enrollments | completed
        return [route for route in self.store.routes() if route['request_id'] in allowed]

    async def dispatch(self, request_id, operation, capability, body):
        if not isinstance(body,dict):
            raise ProtocolError('Invalid request body.')
        with self.store.transaction() as db:
            enrollment = self.store._get(db,'enrollment',request_id)
        if enrollment:
            if operation == 'status' and set(body) == {'client_nonce'} and len(decode(body['client_nonce'])) == 16:
                return self.store.enrollment_status(request_id,capability=capability,client_nonce=body['client_nonce'])
            if operation == 'registration':
                return self.store.enroll_registration(request_id,capability,canonical(body))
            if operation == 'proof' and set(body) == {'assertion'}:
                return self.store.enroll_possession(request_id,capability,body['assertion'])
            raise ProtocolError('Invalid pairing operation.')
        if operation == 'status' and set(body) == {'client_nonce'} and len(decode(body['client_nonce'])) == 16:
            return self.store.status(request_id,capability=capability,client_nonce=body['client_nonce'])
        if request_id not in self.profile_leases:
            raise ProtocolError('This host has not acquired the profile lease.')
        if operation == 'challenge' and set(body) == {'field','submission_id'}:
            return self.store.challenge(request_id,capability,**body)
        if operation == 'submission':
            receipt,value = self.store.reserve(request_id,capability,canonical(body))
            if value is not None:
                self.background(self.validate(request_id,receipt['payload']['submission_id'],receipt['payload']['field'],value))
            return receipt
        if operation == 'cancel':
            if set(body) == {'phase','client_nonce'} and body['phase'] == 'challenge':
                return self.store.cancel_challenge(request_id,capability,body['client_nonce'])
            if set(body) == {'phase','challenge_id','assertion'} and body['phase'] == 'confirm':
                return self.store.cancel_authorized(request_id,capability,body['challenge_id'],body['assertion'])
        raise ProtocolError('Invalid setup operation.')

    async def validate(self, request_id, submission_id, field, value):
        try:
            state = self.store.request_state(request_id)
            spec = InputSpec.model_validate(state['spec'])
            async with asyncio.timeout(60):
                if field == 'agent.credential':
                    from sleeptalk.agents.provider_auth import ProviderSelection, verify
                    if not spec.auth_request:
                        raise InputError('Model credential scope is missing.')
                    selected = ProviderSelection(**spec.auth_request.model_dump(), credential=value)
                    if await verify(selected, spec.model) != 'available':
                        raise InputError('The selected model credential was not verified.')
                    details = {}
                else:
                    details = await self.validator(FIELDS[field],value,spec)
            self.store.commit(submission_id,value,details,current_binding=lambda:runtime_binding_digest(spec))
            state = self.store.request_state(request_id)
            if state['status'] == 'open' and all(f['status'] == 'saved' for f in state['fields'].values()):
                if request_id not in self.finishing:
                    self.finishing.add(request_id)
                    self.background(self.finish(request_id))
        except asyncio.CancelledError:
            self.store.fail(submission_id,'interrupted')
            raise
        except Exception as exc:
            self.store.fail(submission_id,'validation_timeout' if isinstance(exc,TimeoutError) else _validation_code(exc))
        finally:
            value = ''

    async def finish(self, request_id):
        self.finish_attempts[request_id] = self.finish_attempts.get(request_id,0)+1
        try:
            state = self.store.request_state(request_id)
            spec = InputSpec.model_validate(state['spec'])
            if spec.replace_reference:
                self.store.complete_repair(request_id)
                return
            intent_spec = spec
            if spec.auth_request:
                from sleeptalk.agent_auth import AgentAuthConfig
                selected_auth = AgentAuthConfig(**spec.auth_request.model_dump(),
                    credential_ref=state['fields']['agent.credential']['reference'])
                spec = spec.model_copy(update={'auth': selected_auth, 'auth_request': None})
            refs = {FIELDS[f]:data['reference'] for f,data in state['fields'].items() if f != 'agent.credential'}
            details = {FIELDS[f]:data['details'] for f,data in state['fields'].items() if f != 'agent.credential'}
            choices = {key:getattr(spec,key) for key in ('guild_id','channel_id','operator_id')}
            auth_generations = ({spec.auth.credential_ref: self.store.credentials.status(spec.auth.credential_ref).generation}
                                if spec.auth else {})
            async with asyncio.timeout(90):
                document = await self.profile_preparer(spec,refs,details,self.store.credentials,
                    choices,Path(spec.profile_path),self.lease_root)
            self.store.finalize(request_id,document,current_binding=lambda:runtime_binding_digest(intent_spec),
                                auth_generations=auth_generations)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.store.profile_failed(request_id)
        finally:
            self.finishing.discard(request_id)
