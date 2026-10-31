import asyncio
import hashlib
import time

import pytest
from aiohttp.test_utils import TestClient, TestServer

pytest.importorskip('fido2')
pytest.importorskip('rfc8785')
pytest.importorskip('cryptography.hazmat.primitives.hpke')
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.hazmat.primitives.serialization import Encoding,PublicFormat

from sleeptalk.remote_relay import SetupRelay,AUTH_DOMAIN
from sleeptalk.remote_crypto import canonical,encode
from sleeptalk.remote_store import identifier

ORIGIN='https://setup.example.com'
HEADERS={'Host':'setup.example.com','Origin':ORIGIN}


@pytest.fixture
async def relay():
    key=ed25519.Ed25519PrivateKey.generate()
    public=key.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw)
    host_id=hashlib.sha256(public).hexdigest()
    service=SetupRelay(ORIGIN,{host_id:encode(public)},delivery_timeout=.1)
    async with TestClient(TestServer(service.app())) as client:
        yield service,client,host_id,key


async def host(relay):
    service,client,host_id,key=relay
    socket=await client.ws_connect('/v1/hosts/'+host_id+'/connect',headers=HEADERS)
    challenge=(await socket.receive_json())['challenge']
    await socket.send_json({'type':'authenticate','signature':encode(key.sign(AUTH_DOMAIN+canonical(challenge)))})
    assert (await socket.receive_json())['type']=='authenticated'
    request_id=identifier()
    capability=encode(b'c'*32)
    await socket.send_json({'type':'routes','routes':[{'request_id':request_id,'capability_hash':hashlib.sha256(b'c'*32).hexdigest(),'expires_at':int(time.time())+600}]})
    assert (await socket.receive_json())['type']=='routes_registered'
    return socket,request_id,capability


async def post(relay,request_id,capability,operation='status',**kwargs):
    return await relay[1].post('/v1/setup/'+request_id+'/'+operation,
        headers={**HEADERS,'Authorization':'Bearer '+capability},json=kwargs or {'client_nonce':identifier()})


async def test_authenticated_live_host_roundtrip_is_volatile(relay):
    socket,request_id,capability=await host(relay)
    request=asyncio.create_task(post(relay,request_id,capability))
    message=await socket.receive_json()
    assert message['operation']=='status' and message['capability']==capability
    await socket.send_json({'type':'result','id':message['id'],'response':{'payload':{'status':'open'},'signature':'synthetic-signature'}})
    response=await request
    assert response.status==200
    assert (await response.json())['payload']['status']=='open'
    connection=relay[0].hosts[relay[2]]
    assert not connection.pending and not connection.busy
    await socket.close()
    response=await post(relay,request_id,capability)
    assert response.status==503 and (await response.json())['code']=='host_offline'
    assert not relay[0].hosts


async def test_invalid_host_signature_never_registers_routes(relay):
    service,client,host_id,key=relay
    socket=await client.ws_connect('/v1/hosts/'+host_id+'/connect',headers=HEADERS)
    await socket.receive_json()
    await socket.send_json({'type':'authenticate','signature':encode(b'x'*64)})
    await socket.receive()
    assert not service.hosts


async def test_delivery_timeout_discards_buffer_and_late_reply(relay):
    socket,request_id,capability=await host(relay)
    response=await post(relay,request_id,capability,'submission',context={'fixture':True},ct='synthetic-ciphertext')
    message=await socket.receive_json()
    assert response.status==503 and (await response.json())['code']=='delivery_unknown'
    connection=relay[0].hosts[relay[2]]
    assert not connection.pending and not connection.busy
    await socket.send_json({'type':'result','id':message['id'],'response':{'late':True}})
    await socket.close()


async def test_wrong_capability_and_origin_do_not_reach_host(relay):
    socket,request_id,capability=await host(relay)
    response=await post(relay,request_id,encode(b'w'*32))
    assert response.status==503
    response=await relay[1].post('/v1/setup/'+request_id+'/status',headers={'Host':'setup.example.com','Origin':'https://evil.example'},json={})
    assert response.status==403
    response=await relay[1].post('/v1/setup/'+request_id+'/status',headers={**HEADERS,'Authorization':'Bearer '+capability},data=b'x'*32769)
    assert response.status==413
    assert not relay[0].hosts[relay[2]].pending
    await socket.close()


async def test_reconnecting_host_invalidates_old_routing(relay):
    old,request_id,capability=await host(relay)
    new,new_id,new_cap=await host(relay)
    assert request_id not in relay[0].hosts[relay[2]].routes
    assert (await post(relay,request_id,capability)).status==503
    await old.close()
    await new.close()


async def test_anonymous_flood_cannot_starve_a_live_capability(relay):
    """Behind a TLS frontend every request shares one remote address."""
    socket,request_id,capability=await host(relay)
    relay[0].request_limit=5
    statuses=[(await post(relay,identifier(),encode(b'x'*32))).status for _ in range(6)]
    assert statuses[-1]==429 and set(statuses[:-2])=={503}
    request=asyncio.create_task(post(relay,request_id,capability))
    message=await socket.receive_json()
    await socket.send_json({'type':'result','id':message['id'],'response':{'payload':{'status':'open'},'signature':'s'}})
    assert (await request).status==200
    await socket.close()


async def test_a_live_capability_is_still_rate_limited(relay):
    socket,request_id,capability=await host(relay)
    relay[0].request_limit=2
    relay[0].hosts[relay[2]].busy.add(request_id)  # answered without reaching the host
    assert [(await post(relay,request_id,capability)).status for _ in range(3)]==[409,409,429]
    await socket.close()


def test_origin_matches_browser_canonical_serialization():
    from sleeptalk.remote_relay import origin
    assert origin('https://SETUP.EXAMPLE.COM:443') == 'https://setup.example.com'
    assert origin('https://setup.example.com') == 'https://setup.example.com'
