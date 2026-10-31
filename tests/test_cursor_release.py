"""Actual Cursor Python SDK over deterministic Connect frames (no model backend).

Set SLEEPTALK_TEST_CURSOR_SDK_VERSION to require an exact wheel in release CI.
The HTTP transport is a fixture; AsyncClient, AsyncAgent, AsyncRun and parsing
are upstream code. Bundled JS discovery/conversion have separate tests.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from importlib.metadata import version
import json
import os

import pytest

# httpx comes with the Cursor SDK; CI's extras leave both out.
httpx = pytest.importorskip("httpx")

from sleeptalk.adapter import typed_events
from sleeptalk.agents.cursor_sdk_adapter import CursorSdkClient, CursorSdkError
from sleeptalk.agents.cursor_sdk_isolation import create_agent, _local_client


@pytest.fixture(autouse=True)
def exact_sdk():
    expected = os.environ.get('SLEEPTALK_TEST_CURSOR_SDK_VERSION')
    if expected:
        assert version('cursor-sdk') == expected
        import cursor_sdk  # noqa: F401 — must fail, never skip a required wheel
    else:
        pytest.importorskip('cursor_sdk')


def frame(value, flags=0):
    body = json.dumps(value).encode()
    return bytes([flags]) + len(body).to_bytes(4, 'big') + body


class WireStream(httpx.AsyncByteStream):
    def __init__(self, owner, run_id, scenario):
        self.owner, self.run_id, self.scenario = owner, run_id, scenario
        self.closed = False

    async def __aiter__(self):
        def message(kind, **fields):
            return frame({'sdkMessage': {'type': kind, 'agent_id': 'agent-fixture',
                                       'run_id': self.run_id, **fields}})
        if self.scenario == 'send_stall':
            await asyncio.Event().wait()
        yield message('system', subtype='init')
        yield message('thinking', text='Planning')
        if self.scenario == 'stall':
            await self.owner.cancelled.wait()
        else:
            for status in ['running', 'completed']:
                yield message('tool_call', name='read', call_id='read-1', status=status, args={'path': 'file.txt'})
            # Fragment a Connect frame to test upstream parser buffering.
            data = message('assistant', message={'content': [{'type': 'text', 'text': 'Hello from Cursor. '}]})
            yield data[:3]
            yield data[3:]
            if self.scenario == 'close':
                await self.owner.cancelled.wait()
            yield message('usage', usage={'inputTokens': 7, 'outputTokens': 3})
        if self.scenario == 'wait_stall':
            yield frame({}, 2)
            return
        status = 'cancelled' if self.owner.cancelled.is_set() else self.scenario if self.scenario in {'error', 'expired'} else 'finished'
        yield frame({'result': {'result': {'runId': self.run_id, 'agentId': 'agent-fixture', 'status': status}}})
        yield frame({}, 2)

    async def aclose(self):
        self.closed = True


class FixtureBridge:
    def __init__(self, cwd):
        self.cwd = str(cwd)
        self.requests, self.streams = [], []
        self.scenario = 'normal'
        self.cancelled = asyncio.Event()

    async def handle(self, request):
        method = request.url.path.rsplit('/', 1)[-1]
        body = request.content
        data = json.loads(body[5:] if method == 'Send' else body)
        self.requests.append((method, data))
        assert request.headers['authorization'] == 'Bearer fixture-bridge-token'
        if method == 'CreateAgent':
            options = data['options']
            assert options['tools'] == {'names': []}
            assert options['disallowedTools'] == ['mcp', 'task']
            assert not options['local'].get('settingSources')
            assert options['local']['cwd'] == [self.cwd]
            assert not options['local'].get('autoReview', False)
            return httpx.Response(200, json={'agentId': 'agent-fixture', 'model': {'id': 'chosen'}})
        if method == 'GetAgent':
            return httpx.Response(200, json={'agent': {'agentId': 'agent-fixture', 'local': {'cwd': self.cwd}}})
        if method == 'Send':
            self.cancelled.clear()
            stream = WireStream(self, f'run-{len(self.streams)}', self.scenario)
            self.streams.append(stream)
            return httpx.Response(200, headers={'content-type': 'application/connect+json'}, stream=stream)
        if method == 'CancelRun':
            assert data['runId'] == self.streams[-1].run_id
            self.cancelled.set()
            return httpx.Response(200, json={})
        if method == 'WaitLiveRun':
            await self.cancelled.wait()
            return httpx.Response(200, json={'result': {'runId': data['runId'], 'status': 'cancelled'}})
        assert method == 'CloseAgent', method
        return httpx.Response(200, json={})


@asynccontextmanager
async def client_fixture(tmp_path):
    bridge = FixtureBridge(tmp_path)
    async with httpx.AsyncClient(transport=httpx.MockTransport(bridge.handle)) as http:
        async with _local_client(base_url='http://127.0.0.1:9999', auth_token='fixture-bridge-token',
                               http_client=http, max_retries=0) as sdk:
            agent = await create_agent(sdk, str(tmp_path), 'chosen', 'synthetic-key')
            client = CursorSdkClient(cwd=str(tmp_path), agent=agent, permission_mode='deny')
            try:
                yield client, bridge
            finally:
                await client.close()
                await agent.close()


@pytest.mark.asyncio
async def test_actual_sdk_stream_identity_tools_usage_and_second_turn(tmp_path):
    async with client_fixture(tmp_path) as (client, bridge):
        await client.warmup()
        events = []
        text = [part async for part in client.respond_streaming('hello', on_event=typed_events(events.append))]
        assert ' '.join(text).strip() == 'Hello from Cursor.'
        identity = next(e for e in events if e.kind == 'session.identified')
        assert identity.upstream_session_id == 'agent-fixture' and identity.resumable is False
        assert len([e for e in events if e.kind == 'tool']) == 2
        usage = next(e for e in events if e.kind == 'usage.reported')
        assert (usage.input_tokens, usage.output_tokens) == (7, 3)
        assert await client.respond('again') == 'Hello from Cursor.'
        assert all(s.closed for s in bridge.streams)
        assert not any(method == 'CancelRun' for method, _ in bridge.requests)


@pytest.mark.asyncio
@pytest.mark.parametrize('scenario', ['stall', 'wait_stall'])
async def test_actual_sdk_timeout_cancels_and_next_turn_works(tmp_path, scenario):
    async with client_fixture(tmp_path) as (client, bridge):
        bridge.scenario = scenario
        # Progress can reset the idle deadline before the overall deadline
        # expires; either timeout must still cancel and release the run.
        with pytest.raises(CursorSdkError, match='timed out|exceeded maximum time'):
            await client.respond('stall', timeout=0.05)
        assert bridge.cancelled.is_set()
        assert bridge.streams[-1].closed
        assert client._run is None
        bridge.scenario = 'normal'
        assert await client.respond('again') == 'Hello from Cursor.'


@pytest.mark.asyncio
async def test_actual_sdk_send_timeout_closes_connection(tmp_path):
    async with client_fixture(tmp_path) as (client, bridge):
        bridge.scenario = 'send_stall'
        with pytest.raises(CursorSdkError, match='send timed out'):
            await client.respond('stall', timeout=0.05)
        assert bridge.streams[-1].closed
        # No upstream run ID arrived, so cancellation cannot target a run.
        assert not any(method == 'CancelRun' for method, _ in bridge.requests)


@pytest.mark.asyncio
async def test_actual_sdk_early_consumer_close_cancels_run(tmp_path):
    async with client_fixture(tmp_path) as (client, bridge):
        bridge.scenario = 'close'
        response = client.respond_streaming('close')
        assert (await anext(response)).strip() == 'Hello from Cursor.'
        await response.aclose()
        assert bridge.cancelled.is_set() and bridge.streams[-1].closed
        assert client._run is None


@pytest.mark.asyncio
@pytest.mark.parametrize('scenario', ['error', 'expired'])
async def test_actual_sdk_terminal_failure_is_not_success(tmp_path, scenario):
    async with client_fixture(tmp_path) as (client, bridge):
        bridge.scenario = scenario
        with pytest.raises(CursorSdkError, match='failed'):
            await client.respond('fail')
        assert bridge.streams[-1].closed


@pytest.mark.asyncio
async def test_local_bridge_override_keeps_cloud_auth_guard_and_env_isolation(monkeypatch):
    from cursor_sdk.errors import ConfigurationError
    monkeypatch.setenv('CURSOR_API_KEY', 'must-not-fall-back')
    async with _local_client(base_url='http://127.0.0.1:9999', auth_token='fixture') as client:
        with pytest.raises(ConfigurationError, match='api_key'):
            await client._agent_unary('GetAgent', {'agentId': 'bc-cloud'})
