from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from sleeptalk.adapter import ControlAccepted, ControlRejected, PermissionDecision, PermissionRequested, ResolvePermission, typed_events
from sleeptalk.agents.hermes_adapter import HermesACPClient


def request(*options):
    return {'jsonrpc': '2.0', 'id': 'server-request', 'method': 'session/request_permission', 'params': {
        'sessionId': 'session', 'toolCall': {'toolCallId': 'tool', 'title': 'Run command'},
        'options': list(options) or [
            {'optionId': 'opaque-yes', 'kind': 'allow_once'},
            {'optionId': 'opaque-no', 'kind': 'reject_once'},
        ],
    }}


@pytest.mark.asyncio
@pytest.mark.parametrize('decision,expected', [(PermissionDecision.ALLOW_ONCE, 'opaque-yes'), (PermissionDecision.DENY, 'opaque-no')])
async def test_pending_emits_request_and_sends_only_explicit_decision(decision, expected):
    client = HermesACPClient(permission_mode='pending')
    client._send = AsyncMock()
    events = []
    client._on_event = typed_events(events.append)
    await client._handle_agent_request(request())
    client._send.assert_not_awaited()
    permission = events[0]
    assert isinstance(permission, PermissionRequested)
    assert permission.tool_call_id == 'tool'
    assert permission.options == {PermissionDecision.ALLOW_ONCE, PermissionDecision.DENY}
    result = await client.control(ResolvePermission(permission.request_id, decision))
    assert isinstance(result, ControlAccepted)
    assert client._send.call_args.args[0]['result']['outcome'] == {'outcome': 'selected', 'optionId': expected}
    assert isinstance(await client.control(ResolvePermission(permission.request_id, decision)), ControlRejected)
    assert client._send.await_count == 1


@pytest.mark.asyncio
async def test_unsupported_allow_is_rejected_and_deny_cancels_if_no_deny_option():
    client = HermesACPClient(permission_mode='pending')
    client._send = AsyncMock()
    events = []
    client._on_event = typed_events(events.append)
    await client._handle_agent_request(request({'optionId': 'once', 'kind': 'allow_once'}))
    permission = events[0]
    assert isinstance(await client.control(ResolvePermission(permission.request_id, PermissionDecision.ALLOW_SESSION)), ControlRejected)
    client._send.assert_not_awaited()
    await client.control(ResolvePermission(permission.request_id, PermissionDecision.DENY))
    assert client._send.call_args.args[0]['result']['outcome'] == {'outcome': 'cancelled'}


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['deny', 'pending'])
async def test_deny_and_no_listener_never_choose_first_allow(mode):
    client = HermesACPClient(permission_mode=mode)
    client._send = AsyncMock()
    await client._handle_agent_request(request({'optionId': 'allow_once', 'kind': 'allow_once'}))
    assert client._send.call_args.args[0]['result']['outcome'] == {'outcome': 'cancelled'}


@pytest.mark.asyncio
async def test_aborted_request_cannot_authorize_reused_server_request_id():
    client = HermesACPClient(permission_mode='pending')
    client._send = AsyncMock()
    events = []
    client._on_event = typed_events(events.append)
    await client._handle_agent_request(request())
    old_id = events[-1].request_id
    await client.abort()
    await client._handle_agent_request(request())
    assert events[-1].request_id != old_id
    assert isinstance(await client.control(ResolvePermission(old_id, PermissionDecision.ALLOW_ONCE)), ControlRejected)
    assert client._send.await_count == 1
    assert client._send.call_args.args[0]['result']['outcome']['outcome'] == 'cancelled'
    await client.close()
    assert isinstance(await client.control(ResolvePermission(events[-1].request_id, PermissionDecision.ALLOW_ONCE)), ControlRejected)


@pytest.mark.asyncio
async def test_auto_retains_explicit_compatibility_policy():
    client = HermesACPClient(permission_mode='auto')
    client._send = AsyncMock()
    await client._handle_agent_request(request())
    assert client._send.call_args.args[0]['result']['outcome']['optionId'] == 'opaque-yes'


def test_invalid_policy_rejected():
    with pytest.raises(ValueError, match='permission mode'):
        HermesACPClient(permission_mode='pendng')


@pytest.mark.asyncio
@pytest.mark.parametrize('decision', [PermissionDecision.ALLOW_ONCE, PermissionDecision.DENY])
async def test_real_acp_process_stays_blocked_until_decision(tmp_path, decision):
    import json
    import sys
    from pathlib import Path
    from tests.test_acp_agent import FAKE_ACP_SERVER

    script = tmp_path / 'fake-hermes'
    script.write_text(f'#!{sys.executable}\n' + FAKE_ACP_SERVER)
    script.chmod(0o700)
    wire = tmp_path / 'wire.jsonl'
    client = HermesACPClient(binary=str(script), cwd=str(tmp_path), permission_mode='pending', env={
        'ACP_MODE': 'permission', 'ACP_SENTENCE': 'Finished.', 'ACP_WIRE_LOG': str(wire),
    })
    events = []
    async def consume():
        return [part async for part in client.respond_streaming('fake operation', on_event=typed_events(events.append))]
    task = asyncio.create_task(consume())
    try:
        async with asyncio.timeout(3):
            while not any(isinstance(event, PermissionRequested) for event in events):
                if task.done():
                    await task
                await asyncio.sleep(.01)
        sent = [json.loads(line) for line in wire.read_text().splitlines()]
        assert all('result' not in frame for frame in sent)
        permission = next(event for event in events if isinstance(event, PermissionRequested))
        await client.control(ResolvePermission(permission.request_id, decision))
        await asyncio.wait_for(task, 3)
        sent = [json.loads(line) for line in wire.read_text().splitlines()]
        reply = next(frame for frame in sent if 'result' in frame)
        assert reply['result']['outcome']['optionId'] == ('allow_once' if decision is PermissionDecision.ALLOW_ONCE else 'deny')
    finally:
        if not task.done():
            task.cancel()
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('options', [
    [{'optionId': 'allow_session', 'kind': 'allow_always'}, {'optionId': 'allow_always', 'kind': 'allow_always'}],
    [{'optionId': 'allow_always', 'kind': 'allow_always'}, {'optionId': 'allow_session', 'kind': 'allow_always'}],
    [{'optionId': 'allow_always', 'kind': 'allow_always'}],
])
async def test_session_approval_never_selects_permanent_hermes_option(options):
    client = HermesACPClient(permission_mode='pending')
    client._send = AsyncMock()
    events = []
    client._on_event = typed_events(events.append)
    await client._handle_agent_request(request(*options))
    permission = events[0]
    result = await client.control(ResolvePermission(permission.request_id, PermissionDecision.ALLOW_SESSION))
    if any(option['optionId'] == 'allow_session' for option in options):
        assert isinstance(result, ControlAccepted)
        assert client._send.call_args.args[0]['result']['outcome']['optionId'] == 'allow_session'
    else:
        assert PermissionDecision.ALLOW_SESSION not in permission.options
        assert isinstance(result, ControlRejected)
        client._send.assert_not_awaited()


@pytest.mark.asyncio
async def test_permission_prompt_does_not_export_tool_payload():
    client = HermesACPClient(permission_mode='pending')
    client._send = AsyncMock()
    events = []
    client._on_event = typed_events(events.append)
    frame = request()
    frame['params']['reason'] = 'sentinel-private-reason' * 10000
    frame['params']['toolCall']['title'] = 'sentinel-private-command' * 10000
    await client._handle_agent_request(frame)
    assert events[0].prompt == 'Allow the pending agent tool operation?'
    assert events[0].tool_call_id == 'tool'


@pytest.mark.asyncio
@pytest.mark.parametrize('options', [
    [{'optionId': 'deny', 'kind': 'reject_once'}, {'optionId': 'deny_always', 'kind': 'reject_always'}],
    [{'optionId': 'deny_always', 'kind': 'reject_always'}, {'optionId': 'deny', 'kind': 'reject_once'}],
    [{'optionId': 'deny_always', 'kind': 'reject_always'}],
    [{'optionId': 'opaque-permanent', 'kind': 'reject_always'}],
])
async def test_deny_never_selects_permanent_option(options):
    client = HermesACPClient(permission_mode='deny')
    client._send = AsyncMock()
    await client._handle_agent_request(request(*options))
    outcome = client._send.call_args.args[0]['result']['outcome']
    assert outcome == ({'outcome': 'selected', 'optionId': 'deny'} if any(option['optionId'] == 'deny' for option in options) else {'outcome': 'cancelled'})
