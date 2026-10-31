"""Dedicated volatile setup relay. It never writes submissions or knows provider keys."""
from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
import hashlib
import secrets
import time
from urllib.parse import urlsplit

import aiohttp
from aiohttp import web
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from sleeptalk.remote_crypto import ProtocolError, canonical, decode, encode, parse
from sleeptalk.remote_store import identifier

AUTH_DOMAIN = b'sleeptalk/setup/relay-auth/v1\0'
OPERATIONS = {'status','challenge','submission','cancel','registration','proof'}


def origin(value: str) -> str:
    parts = urlsplit(value)
    if parts.scheme != 'https' or not parts.hostname or parts.path or parts.query or parts.fragment or parts.username or parts.password or parts.port not in (None,443):
        raise ProtocolError('Setup service requires an exact HTTPS origin.')
    if not parts.hostname.isascii() or ':' in parts.hostname:
        raise ProtocolError('Use the public setup domain in its ASCII hostname form.')
    return 'https://'+parts.hostname


@dataclass
class HostConnection:
    socket: web.WebSocketResponse
    generation: str
    last_seen: float
    routes: dict = field(default_factory=dict)
    pending: dict = field(default_factory=dict)
    busy: set = field(default_factory=set)


class SetupRelay:
    def __init__(self, service_origin: str, host_keys: dict[str,str], *, clock=time.monotonic,
                 delivery_timeout: float = 5, request_limit: int = 120, global_limit: int = 1000):
        self.origin = origin(service_origin)
        self.host_keys = {}
        for host_id,key in host_keys.items():
            raw = decode(key)
            if len(raw) != 32 or hashlib.sha256(raw).hexdigest() != host_id:
                raise ProtocolError('Invalid enrolled relay host key.')
            self.host_keys[host_id] = Ed25519PublicKey.from_public_bytes(raw)
        self.clock,self.delivery_timeout = clock,delivery_timeout
        self.hosts: dict[str,HostConnection] = {}
        self.request_limit,self.global_limit = request_limit,global_limit
        self.traffic: dict[str,deque] = {}

    def _admit(self, *buckets):
        now = self.clock()
        for key in list(self.traffic):
            events = self.traffic[key]
            while events and events[0] <= now-60: events.popleft()
            if not events: del self.traffic[key]
        if len(self.traffic) >= 10000 and any(key not in self.traffic for key,_limit in buckets):
            raise web.HTTPTooManyRequests()
        for key,limit in buckets:
            events = self.traffic.setdefault(key,deque())
            if len(events) >= limit: raise web.HTTPTooManyRequests()
            events.append(now)

    def _admit_anonymous(self, request):
        # Requests that prove nothing share their own buckets. Behind a TLS
        # frontend every browser has the same remote address, so these must
        # never spend the budget of requests that carry a live capability.
        self._admit(('anon:*',self.global_limit),('anon:'+(request.remote or 'unknown'),self.request_limit))

    def _admit_authorized(self, request_id):
        self._admit(('auth:*',self.global_limit),('auth:'+request_id,self.request_limit))

    def _bound(self, request, *, browser=False):
        if request.headers.get('Host') != urlsplit(self.origin).netloc:
            raise web.HTTPForbidden()
        if browser and request.headers.get('Origin') != self.origin:
            raise web.HTTPForbidden()

    async def connect(self, request):
        self._bound(request)
        self._admit_anonymous(request)
        host_id = request.match_info['host_id']
        if host_id not in self.host_keys:
            raise web.HTTPForbidden()
        socket = web.WebSocketResponse(max_msg_size=49152,heartbeat=10)
        await socket.prepare(request)
        generation = identifier()
        challenge = {'host_id':host_id,'service_origin':self.origin,'nonce':encode(secrets.token_bytes(32)),
                     'connection_generation':generation}
        await socket.send_json({'type':'authenticate','challenge':challenge})
        connection = None
        try:
            async with asyncio.timeout(3):
                frame = await socket.receive()
                if frame.type != aiohttp.WSMsgType.TEXT: raise ProtocolError('Host authentication failed.')
                response = parse(frame.data.encode())
                if set(response) != {'type','signature'} or response['type'] != 'authenticate':
                    raise ProtocolError('Host authentication failed.')
                self.host_keys[host_id].verify(decode(response['signature']),AUTH_DOMAIN+canonical(challenge))
            connection = HostConnection(socket,generation,self.clock())
            previous = self.hosts.get(host_id)
            self.hosts[host_id] = connection
            if previous: await previous.socket.close()
            await socket.send_json({'type':'authenticated','connection_generation':generation})
            async for frame in socket:
                if frame.type != aiohttp.WSMsgType.TEXT: break
                message = parse(frame.data.encode())
                connection.last_seen = self.clock()
                if message.get('type') == 'heartbeat' and set(message) == {'type'}:
                    await socket.send_json({'type':'heartbeat'})
                elif message.get('type') == 'routes' and set(message) == {'type','routes'}:
                    routes = message['routes']
                    if not isinstance(routes,list) or len(routes) > 6: raise ProtocolError('Invalid route registration.')
                    accepted = {}
                    for route in routes:
                        if not isinstance(route,dict) or set(route) != {'request_id','capability_hash','expires_at'}:
                            raise ProtocolError('Invalid route registration.')
                        if len(decode(route['request_id'])) != 16 or len(route['capability_hash']) != 64 or not all(c in '0123456789abcdef' for c in route['capability_hash']):
                            raise ProtocolError('Invalid route registration.')
                        if type(route['expires_at']) is not int or not time.time() < route['expires_at'] <= time.time()+901:
                            continue
                        accepted[route['request_id']] = route
                    connection.routes = accepted
                    await socket.send_json({'type':'routes_registered'})
                elif message.get('type') == 'result' and set(message) == {'type','id','response'}:
                    future = connection.pending.get(message['id'])
                    if future and not future.done(): future.set_result(message['response'])
                else:
                    raise ProtocolError('Invalid host message.')
        except (Exception, asyncio.CancelledError):
            pass
        finally:
            if connection:
                if self.hosts.get(host_id) is connection: self.hosts.pop(host_id,None)
                for future in connection.pending.values():
                    if not future.done(): future.set_exception(ConnectionError('Host disconnected.'))
                connection.routes.clear()
            await socket.close()
        return socket

    async def forward(self, request):
        self._bound(request,browser=True)
        request_id,operation = request.match_info['request_id'],request.match_info['operation']
        if operation not in OPERATIONS:
            self._admit_anonymous(request)
            raise web.HTTPNotFound()
        header = request.headers.get('Authorization','')
        if not header.startswith('Bearer '):
            self._admit_anonymous(request)
            raise web.HTTPUnauthorized()
        capability = header[7:]
        try:
            raw_capability = decode(capability)
            if len(raw_capability) != 32: raise ValueError
        except ValueError:
            self._admit_anonymous(request)
            raise web.HTTPBadRequest() from None
        capability_hash = hashlib.sha256(raw_capability).hexdigest()
        connection = None
        for candidate in self.hosts.values():
            route = candidate.routes.get(request_id)
            if route and route['expires_at'] > time.time() and secrets.compare_digest(route['capability_hash'],capability_hash):
                connection = candidate
                break
        if connection is None:
            self._admit_anonymous(request)
        else:
            self._admit_authorized(request_id)
        try:
            body = parse(await request.read())
        except (ValueError,ProtocolError):
            raise web.HTTPBadRequest() from None
        if not connection or self.clock()-connection.last_seen > 30:
            return web.json_response({'code':'host_offline'},status=503)
        if request_id in connection.busy:
            return web.json_response({'code':'request_busy'},status=409)
        request_token = identifier()
        future = asyncio.get_running_loop().create_future()
        connection.pending[request_token] = future
        connection.busy.add(request_id)
        try:
            await connection.socket.send_json({'type':'request','id':request_token,'request_id':request_id,
                'operation':operation,'capability':capability,'body':body})
            body.clear()
            async with asyncio.timeout(self.delivery_timeout):
                response = await future
            return web.json_response(response)
        except (TimeoutError,ConnectionError,ConnectionResetError):
            return web.json_response({'code':'delivery_unknown'},status=503)
        finally:
            body.clear()
            connection.pending.pop(request_token,None)
            connection.busy.discard(request_id)
            if not future.done(): future.cancel()

    def app(self, *, assets=None):
        @web.middleware
        async def headers(request,handler):
            try:
                if request.headers.get('Host') != urlsplit(self.origin).netloc:
                    raise web.HTTPForbidden()
                response = await handler(request)
            except web.HTTPException as exc:
                response = web.json_response({'code':'request_rejected'},status=exc.status)
            except Exception:
                response = web.json_response({'code':'request_rejected'},status=400)
            response.headers.update({'Cache-Control':'no-store','Referrer-Policy':'no-referrer',
                'X-Content-Type-Options':'nosniff','Content-Security-Policy':"default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"})
            return response
        app = web.Application(client_max_size=32768,middlewares=[headers])
        app.router.add_get('/v1/hosts/{host_id}/connect',self.connect)
        app.router.add_post('/v1/setup/{request_id}/{operation}',self.forward)
        if assets is not None:
            from pathlib import Path
            assets = Path(assets)
            async def page(request):
                self._bound(request)
                self._admit_anonymous(request)
                return web.FileResponse(assets/'index.html')
            app.router.add_get('/entry',page)
            for name in ('app.js','style.css','app.js.LEGAL.txt','THIRD_PARTY_NOTICES.txt','assets.json'):
                async def asset(request, name=name):
                    self._bound(request)
                    self._admit_anonymous(request)
                    return web.FileResponse(assets/name)
                app.router.add_get('/entry/'+name,asset)
        async def cleanup(_):
            for connection in list(self.hosts.values()): await connection.socket.close()
        app.on_cleanup.append(cleanup)
        return app


class HostRelayClient:
    """One owned connection. The coordinator alone mutates host authorization state."""
    def __init__(self, store, service_origin: str, dispatch, *, session=None):
        self.store,self.origin,self.dispatch = store,origin(service_origin),dispatch
        self.session = session
        self.connected = asyncio.Event()
        self.closed = asyncio.Event()
        self.socket = None

    async def run(self):
        owned = self.session is None
        session = self.session or aiohttp.ClientSession(trust_env=False,timeout=aiohttp.ClientTimeout(total=None,connect=10))
        try:
            while not self.closed.is_set():
                try:
                    await self._connection(session)
                except (Exception,asyncio.CancelledError):
                    if self.closed.is_set(): break
                    if asyncio.current_task().cancelling(): raise
                self.connected.clear()
                try: await asyncio.wait_for(self.closed.wait(),timeout=1)
                except TimeoutError: pass
        finally:
            if owned: await session.close()

    async def _connection(self, session):
        async with session.ws_connect(self.origin+'/v1/hosts/'+self.store.host_id+'/connect',max_msg_size=49152,heartbeat=10) as socket:
            self.socket = socket
            frame = await asyncio.wait_for(socket.receive(),timeout=3)
            message = parse(frame.data.encode())
            challenge = message['challenge']
            if set(challenge) != {'host_id','service_origin','nonce','connection_generation'} or challenge['host_id'] != self.store.host_id or challenge['service_origin'] != self.origin or len(decode(challenge['nonce'])) != 32 or len(decode(challenge['connection_generation'])) != 16:
                raise ProtocolError('Relay authentication challenge is invalid.')
            signature = encode(self.store.key.sign(AUTH_DOMAIN+canonical(challenge)))
            await socket.send_json({'type':'authenticate','signature':signature})
            reply = parse((await asyncio.wait_for(socket.receive(),timeout=3)).data.encode())
            if reply != {'type':'authenticated','connection_generation':challenge['connection_generation']}:
                raise ProtocolError('Relay authentication did not complete.')
            self.store.set_connection(challenge['connection_generation'])
            await socket.send_json({'type':'routes','routes':self.store.routes()})
            reply = parse((await asyncio.wait_for(socket.receive(),timeout=3)).data.encode())
            if reply != {'type':'routes_registered'}: raise ProtocolError('Relay routes were not accepted.')
            self.connected.set()
            queue = asyncio.Queue(maxsize=8)
            async def reader():
                async for frame in socket:
                    if frame.type != aiohttp.WSMsgType.TEXT: break
                    message = parse(frame.data.encode())
                    if message.get('type') in {'heartbeat','routes_registered'}: continue
                    if set(message) != {'type','id','request_id','operation','capability','body'} or message['type'] != 'request' or message['operation'] not in OPERATIONS:
                        raise ProtocolError('Invalid relay request.')
                    queue.put_nowait(message)
                raise ConnectionError('Relay connection ended.')
            async def coordinator():
                while True:
                    message = await queue.get()
                    try:
                        response = await self.dispatch(message['request_id'],message['operation'],message['capability'],message['body'])
                    except Exception:
                        response = {'code':'request_rejected'}
                    await socket.send_json({'type':'result','id':message['id'],'response':response})
                    message.clear()
            async def heartbeat():
                while True:
                    await asyncio.sleep(10)
                    await socket.send_json({'type':'heartbeat'})
                    await socket.send_json({'type':'routes','routes':self.store.routes()})
            tasks = [asyncio.create_task(fn()) for fn in (reader,coordinator,heartbeat)]
            try:
                done,_ = await asyncio.wait(tasks,return_when=asyncio.FIRST_COMPLETED)
                for task in done: task.result()
            finally:
                self.connected.clear()
                for task in tasks: task.cancel()
                await asyncio.gather(*tasks,return_exceptions=True)
                while not queue.empty(): queue.get_nowait().clear()
                self.socket = None

    async def close(self):
        self.closed.set()
        if self.socket: await self.socket.close()
