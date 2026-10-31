from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from sleeptalk.agents import onboarding_rpc as rpc
from sleeptalk.config import AgentConfig


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['', 'auto', 'invalid'])
async def test_pi_unprotected_probe_never_launches_project_extensions(monkeypatch, tmp_path, mode):
    from unittest.mock import AsyncMock
    exchange = AsyncMock()
    monkeypatch.setattr(rpc, '_stdio', exchange)
    result = await rpc.probe(AgentConfig(workspace_dir=str(tmp_path), permission_mode=mode), 'pi', {})
    exchange.assert_not_awaited()
    assert result['status'] == 'needs_input'
    assert result['permissions'] == 'unavailable'
    assert result['login'] == result['protocol'] == result['workspace'] == 'unknown'


@pytest.mark.asyncio
@pytest.mark.parametrize('name,binary', [('openclaw_acp', 'openclaw')])
async def test_acp_only_initializes_without_fs_or_terminal_capabilities(monkeypatch, tmp_path, name, binary):
    seen = []
    monkeypatch.setattr(rpc, '_binary', lambda binary, env: binary)
    async def exchange(argv, cwd, env, request):
        seen.append((argv, request))
        return {'jsonrpc': '2.0', 'id': request['id'], 'result': {'protocolVersion': 1, 'authMethods': []}}
    monkeypatch.setattr(rpc, '_stdio', exchange)
    result = await rpc.probe(AgentConfig(type=name, workspace_dir=str(tmp_path), permission_mode='deny'), name, {})
    assert result['protocol'] == 'available'
    assert result['workspace'] == result['login'] == 'unknown'
    assert result['permissions'] == 'inherited'
    assert seen[0][0] == [binary, 'acp']
    assert seen[0][1]['method'] == 'initialize'
    assert seen[0][1]['params']['clientCapabilities'] == {'fs': {'readTextFile': False, 'writeTextFile': False}, 'terminal': False}


@pytest.mark.asyncio
@pytest.mark.parametrize('response', [{'result': {}}, {'jsonrpc': '2.0', 'result': {'protocolVersion': 2}}, {'error': {'message': 'private-secret'}}])
async def test_invalid_protocol_and_provider_errors_never_return_secrets(monkeypatch, tmp_path, response):
    monkeypatch.setattr(rpc, '_binary', lambda binary, env: binary)
    async def exchange(*args):
        return response
    monkeypatch.setattr(rpc, '_stdio', exchange)
    result = await rpc.probe(AgentConfig(workspace_dir=str(tmp_path)), 'openclaw_acp', {})
    assert result['protocol'] == 'unknown'
    assert 'private-secret' not in json.dumps(result)


@pytest.mark.asyncio
async def test_timeout_and_cancellation_clean_up_real_fake_process(monkeypatch, tmp_path):
    script = tmp_path / 'fake.py'
    pidfile = tmp_path / 'pid'
    script.write_text(f'import os,time\nopen({str(pidfile)!r},"w").write(str(os.getpid()))\ntime.sleep(60)\n')
    task = asyncio.create_task(rpc._stdio([sys.executable, str(script)], str(tmp_path), {}, {'id': 'onboarding'}))
    for _ in range(200):
        # The child creates the file before writing into it; wait for content.
        if pidfile.exists() and pidfile.read_text().strip():
            break
        await asyncio.sleep(.01)
    assert pidfile.exists() and pidfile.read_text().strip()
    pid = int(pidfile.read_text())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize('frame', [
    {'type': 'extension_ui_request', 'id': 99, 'method': 'confirm'},
    {'jsonrpc': '2.0', 'id': 99, 'method': 'fs/read_text_file', 'params': {'path': '/private/file'}},
])
async def test_process_cannot_trigger_approval_or_file_reads(tmp_path, frame):
    script = tmp_path / 'fake.py'
    script.write_text('import json,sys,time\nsys.stdin.readline()\nprint(' + repr(json.dumps(frame)) + ',flush=True)\ntime.sleep(60)\n')
    with pytest.raises(ValueError, match='cannot execute'):
        await rpc._stdio([sys.executable, str(script)], str(tmp_path), {}, {'id': 'onboarding'})


@pytest.mark.asyncio
async def test_real_fake_process_exchange_suppresses_logs_and_filters_secrets(tmp_path):
    script = tmp_path / 'fake.py'
    script.write_text('import json,os,sys\nr=json.loads(sys.stdin.readline())\nassert "DEEPGRAM_API_KEY" not in os.environ\nprint("provider-private-log",file=sys.stderr)\nprint("nonjson-private-log")\nprint(json.dumps({"id":r["id"],"type":"response","success":True,"data":{}}))\n')
    result = await rpc._stdio([sys.executable, str(script)], str(tmp_path), {'DEEPGRAM_API_KEY': 'secret'}, {'id': 'onboarding'})
    assert result == {'id': 'onboarding', 'type': 'response', 'success': True, 'data': {}}


@pytest.mark.asyncio
async def test_stdio_waits_for_ready_without_sending_anything(tmp_path):
    script = tmp_path / 'fake.py'
    script.write_text('import json,time\nprint(json.dumps({"jsonrpc":"2.0","method":"event","params":{"type":"gateway.ready"}}),flush=True)\ntime.sleep(60)\n')
    observed = await rpc._stdio([sys.executable, str(script)], str(tmp_path), {}, None)
    assert observed == {'type': 'gateway.ready'}


@pytest.mark.asyncio
@pytest.mark.parametrize('name', ['acp', 'hermes_gateway'])
@pytest.mark.parametrize('mode', ['pending', 'deny', 'auto'])
async def test_local_hermes_probe_starts_only_the_installed_acp_hermes_with_approvals(monkeypatch, tmp_path, name, mode):
    """Without hermes_root the installed Hermes is the runtime: it is probed
    over ACP only when approvals stay with the caller, and never for the
    gateway type or without approvals. Secrets never appear in the report."""
    from unittest.mock import AsyncMock
    spawn = AsyncMock(side_effect=AssertionError('must not spawn Hermes'))
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    monkeypatch.setattr(rpc, '_binary', lambda binary, env: binary)  # hermetic: no real Hermes needed or used
    result = await rpc.probe(AgentConfig(
        type=name, workspace_dir=str(tmp_path), permission_mode=mode,
        provider='openai', model='selected-model',
    ), name, {'OPENAI_API_KEY': 'private-secret'})
    assert result['status'] == 'needs_input'
    assert 'private-secret' not in json.dumps(result)
    if mode == 'auto':
        spawn.assert_not_awaited()
        assert result['permissions'] == 'unavailable' and 'pending or deny' in result['next_action']
    elif name == 'hermes_gateway':
        spawn.assert_not_awaited()
        assert '--gateway-url' in result['next_action'] and '--agent hermes' in result['next_action']
    else:
        spawn.assert_awaited_once()
        assert spawn.await_args.args[1] == 'acp'


@pytest.mark.asyncio
@pytest.mark.parametrize('name', ['acp', 'hermes_gateway'])
@pytest.mark.parametrize('mode', ['pending', 'deny'])
async def test_isolated_hermes_probe_never_starts_ambient_runtime(monkeypatch, tmp_path, name, mode):
    from unittest.mock import AsyncMock
    spawn = AsyncMock(side_effect=AssertionError('must not spawn Hermes'))
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    result = await rpc.probe(AgentConfig(
        type=name, workspace_dir=str(tmp_path), permission_mode=mode, options={'hermes_root': str(tmp_path)},
        provider='openai', model='selected-model',
    ), name, {'OPENAI_API_KEY': 'private-secret'})
    spawn.assert_not_awaited()
    assert result['status'] == 'needs_input'
    assert all(result[field] == 'unknown' for field in ('protocol', 'login', 'workspace', 'permissions'))
    assert 'pinned Hermes virtual' in result['next_action']
    assert 'private-secret' not in json.dumps(result)


@pytest.mark.asyncio
@pytest.mark.parametrize('name', ['hermes_gateway', 'openclaw'])
async def test_remote_plaintext_gateway_is_rejected_before_credentials_sent(tmp_path, name):
    # The profile itself refuses a plaintext remote gateway...
    with pytest.raises(ValueError, match='unencrypted'):
        AgentConfig(workspace_dir=str(tmp_path), gateway_url='http://example.test:9191')
    # ...and the probe still refuses one that bypassed validation.
    agent = AgentConfig.model_construct(**{**AgentConfig(workspace_dir=str(tmp_path)).__dict__, 'gateway_url': 'http://example.test:9191'})
    result = await rpc.probe(agent, name, {'OPENCLAW_GATEWAY_TOKEN': 'secret'})
    assert result['protocol'] == 'unknown'
    assert 'TLS' in result['next_action']


@pytest.mark.parametrize('url', ['ws://127.0.0.1:9119', 'ws://100.101.102.103:9119', 'http://box.tail1234.ts.net:9119', 'wss://gw.example.com'])
def test_loopback_tailnet_and_tls_gateways_are_accepted(tmp_path, url):
    assert AgentConfig(workspace_dir=str(tmp_path), gateway_url=url).gateway_url == url


@pytest.mark.asyncio
async def test_explicit_hermes_gateway_still_checks_admission_without_spawning(monkeypatch, tmp_path):
    from unittest.mock import AsyncMock
    from sleeptalk.agents import hermes_gateway_adapter as hermes
    calls = []
    class Link:
        def __init__(self, *args, **kwargs): pass
        async def start(self): calls.append('start')
        async def close(self): calls.append('close')
    monkeypatch.setattr(hermes, 'WebsocketTuiLink', Link)
    spawn = AsyncMock(side_effect=AssertionError('must not spawn Hermes'))
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    result = await rpc.probe(AgentConfig(
        workspace_dir=str(tmp_path), gateway_url='https://example.test',
    ), 'hermes_gateway', {})
    spawn.assert_not_awaited()
    assert calls == ['start', 'close']
    assert result['protocol'] == result['gateway_access'] == 'available'
    assert result['status'] == 'needs_input'
    assert result['login'] == result['workspace'] == result['permissions'] == 'unknown'


@pytest.mark.asyncio
async def test_openclaw_read_only_handshake_is_not_model_login(monkeypatch, tmp_path):
    from sleeptalk.agents import openclaw_gateway_adapter as gateway
    calls = []
    class Session:
        def __init__(self, *args, **kwargs): pass
        async def connect(self): calls.append('connect')
        async def events(self):
            events = asyncio.Queue()
            events.put_nowait({'event': 'connect.challenge'})
            return events
        async def request(self, method, params):
            calls.append((method, params))
            return {'protocol': gateway.PROTOCOL}
        async def close(self): calls.append('close')
    monkeypatch.setattr(gateway, 'AiohttpGatewaySession', Session)
    result = await rpc.probe(AgentConfig(workspace_dir=str(tmp_path), permission_mode='pending'), 'openclaw', {'OPENCLAW_GATEWAY_TOKEN': 'secret'})
    assert result['gateway_access'] == result['protocol'] == 'available'
    assert result['login'] == result['workspace'] == 'unknown'
    assert result['status'] == 'needs_input'
    assert len(calls) == 3 and calls[-1] == 'close'
    method, params = calls[1]
    assert method == 'connect'
    assert params['scopes'] == ['operator.read'] and params['caps'] == []
    assert params['auth'] == {'token': 'secret'}
    assert 'secret' not in json.dumps(result)


@pytest.mark.asyncio
async def test_hermes_gateway_failure_closes_link_and_redacts_exception(monkeypatch, tmp_path):
    from sleeptalk.agents import hermes_gateway_adapter as hermes
    closed = []
    class Link:
        def __init__(self, *args, **kwargs): pass
        async def start(self): raise RuntimeError('secret token private URL')
        async def close(self): closed.append(True)
    monkeypatch.setattr(hermes, 'WebsocketTuiLink', Link)
    result = await rpc.probe(AgentConfig(workspace_dir=str(tmp_path), gateway_url='https://example.test'), 'hermes_gateway', {})
    assert closed == [True]
    assert result['protocol'] == 'unknown'
    assert 'secret' not in json.dumps(result)


@pytest.mark.asyncio
async def test_missing_binary_and_timeout_are_actionable(monkeypatch, tmp_path):
    def missing(*args): raise FileNotFoundError('private-path')
    monkeypatch.setattr(rpc, '_binary', missing)
    result = await rpc.probe(AgentConfig(workspace_dir=str(tmp_path), permission_mode="pending"), 'pi', {})
    assert result['protocol'] == 'unavailable' and 'Install' in result['next_action']
    monkeypatch.setattr(rpc, '_binary', lambda *args: 'pi')
    monkeypatch.setattr(rpc, 'PROBE_TIMEOUT', .01)
    async def slow(*args, **kwargs): await asyncio.sleep(60)
    monkeypatch.setattr(rpc, '_stdio', slow)
    result = await rpc.probe(AgentConfig(workspace_dir=str(tmp_path), permission_mode="pending"), 'pi', {})
    assert result['status'] == 'needs_input' and 'timed out' in result['next_action']


@pytest.mark.asyncio
@pytest.mark.parametrize('output', ['x' * 70000, '\\n'.join(['noise'] * 300)])
async def test_process_output_is_bounded(tmp_path, output):
    script = tmp_path / 'fake.py'
    script.write_text('import sys,time\nsys.stdin.readline()\nprint(' + repr(output.replace('\\n', '\n')) + ',flush=True)\ntime.sleep(60)\n')
    with pytest.raises(ValueError):
        async with asyncio.timeout(2):
            await rpc._stdio([sys.executable, str(script)], str(tmp_path), {}, {'id': 'onboarding'})


@pytest.mark.asyncio
async def test_openclaw_rejected_auth_closes_connection_without_returning_error_body(monkeypatch, tmp_path):
    from sleeptalk.agents import openclaw_gateway_adapter as gateway
    closed = []
    class Session:
        def __init__(self, *args, **kwargs): pass
        async def connect(self): pass
        async def events(self):
            events = asyncio.Queue()
            events.put_nowait({'event': 'connect.challenge'})
            return events
        async def request(self, *args): raise ValueError('private-token-response')
        async def close(self): closed.append(True)
    monkeypatch.setattr(gateway, 'AiohttpGatewaySession', Session)
    result = await rpc.probe(AgentConfig(workspace_dir=str(tmp_path)), 'openclaw', {'OPENCLAW_GATEWAY_TOKEN': 'secret'})
    assert closed == [True]
    assert result['login'] == 'unknown' and 'gateway_access' not in result
    assert 'private-token-response' not in json.dumps(result)


@pytest.mark.asyncio
@pytest.mark.parametrize('name', ['openclaw_acp'])
async def test_legacy_acp_pending_mode_preserves_prompts(monkeypatch, tmp_path, name):
    monkeypatch.setattr(rpc, '_binary', lambda binary, env: binary)
    async def exchange(*args):
        return {'jsonrpc': '2.0', 'result': {'protocolVersion': 1}}
    monkeypatch.setattr(rpc, '_stdio', exchange)
    result = await rpc.probe(AgentConfig(workspace_dir=str(tmp_path), permission_mode='pending'), name, {})
    assert result['permissions'] == 'inherited'


@pytest.mark.asyncio
@pytest.mark.parametrize('parent_exits', [False, True])
async def test_probe_reaps_descendants_even_if_launcher_exits(tmp_path, parent_exits):
    import contextlib
    import signal

    child = tmp_path / 'child.py'
    child_pidfile = tmp_path / 'child.pid'
    child.write_text(
        'import os,signal,time\nfrom pathlib import Path\n'
        'signal.signal(signal.SIGTERM, signal.SIG_IGN)\n'
        f'pidfile = Path({str(child_pidfile)!r})\n'
        'pending = pidfile.with_suffix(".tmp")\n'
        'pending.write_text(str(os.getpid()))\n'
        'pending.replace(pidfile)\n'
        'time.sleep(60)\n'
    )
    parent = tmp_path / 'parent.py'
    parent.write_text(
        'import json,subprocess,sys,time\n'
        f'from pathlib import Path\nsubprocess.Popen([{sys.executable!r}, {str(child)!r}])\n'
        f'while not Path({str(child_pidfile)!r}).exists(): time.sleep(.01)\n'
        'request=json.loads(sys.stdin.readline())\n'
        + ('print(json.dumps({"id":request["id"]}), flush=True)\n' if parent_exits else 'time.sleep(60)\n')
    )
    task = asyncio.create_task(rpc._stdio([sys.executable, str(parent)], str(tmp_path), {}, {'id': 'onboarding'}))
    child_pid = None
    try:
        async with asyncio.timeout(3):
            while not child_pidfile.exists():
                await asyncio.sleep(.01)
        child_pid = int(child_pidfile.read_text())
        if parent_exits:
            assert await asyncio.wait_for(task, 3) == {'id': 'onboarding'}
        else:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        async with asyncio.timeout(3):
            while True:
                try:
                    os.kill(child_pid, 0)
                except ProcessLookupError:
                    break
                await asyncio.sleep(.01)
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if child_pid:
            with contextlib.suppress(ProcessLookupError):
                os.kill(child_pid, signal.SIGKILL)


@pytest.mark.asyncio
@pytest.mark.parametrize('name', ['pi'])
@pytest.mark.parametrize('login,expected', [('available', 'ready'), ('unknown', 'needs_input'), ('unavailable', 'needs_input')])
async def test_pi_ready_requires_extension_workspace_and_live_model_auth(monkeypatch, tmp_path, name, login, expected):
    monkeypatch.setattr(rpc, '_binary', lambda binary, env: binary)
    async def exchange(argv, cwd, env, request, *, nonce):
        assert '--extension' in argv
        assert '--no-extensions' in argv
        assert '--approve' not in argv
        assert ('--no-approve' in argv) == (name == 'pi')
        assert env['SLEEPTALK_ONBOARDING_PROBE'] == '1'
        assert nonce == env['SLEEPTALK_PERMISSION_NONCE']
        return {'type': 'response', 'success': True, 'data': {}, '_onboarding': {
            'version': 1, 'mode': 'pending', 'workspace': cwd, 'model': 'selected', 'provider': 'openai', 'login': login,
        }}
    monkeypatch.setattr(rpc, '_stdio', exchange)
    result = await rpc.probe(AgentConfig(workspace_dir=str(tmp_path), permission_mode='pending', provider='openai', model='selected'), name, {})
    assert result['status'] == expected
    assert result['permissions'] == result['workspace'] == 'available'
    assert result['login'] == login
    assert str(tmp_path) not in json.dumps(result)


@pytest.mark.asyncio
@pytest.mark.parametrize('change', [{'workspace': '/different'}, {'model': 'different'}, {'provider': 'different'}, {'mode': 'auto'}])
async def test_pi_binding_mismatch_never_reports_ready(monkeypatch, tmp_path, change):
    monkeypatch.setattr(rpc, '_binary', lambda binary, env: binary)
    async def exchange(argv, cwd, env, request, *, nonce):
        return {'type': 'response', 'success': True, 'data': {}, '_onboarding': {
            'version': 1, 'mode': 'pending', 'workspace': cwd, 'model': 'selected', 'provider': 'openai', 'login': 'available', **change,
        }}
    monkeypatch.setattr(rpc, '_stdio', exchange)
    result = await rpc.probe(AgentConfig(workspace_dir=str(tmp_path), permission_mode='pending', provider='openai', model='selected'), 'pi', {})
    assert result['status'] == 'needs_input'


@pytest.mark.asyncio
@pytest.mark.parametrize('provided_nonce', ['wrong', None])
async def test_stdio_cannot_accept_missing_or_mismatched_permission_attestation(tmp_path, provided_nonce):
    response = {'id': 'onboarding', 'type': 'response', 'success': True, 'data': {}}
    frames = [response]
    if provided_nonce:
        frames.append({'type': 'extension_ui_request', 'method': 'setStatus', 'statusKey': 'sleeptalk-permissions', 'statusText': json.dumps({'version': 1, 'nonce': provided_nonce})})
    script = tmp_path / 'untrusted-attestation.py'
    script.write_text('import sys\nsys.stdin.readline()\n' + ''.join(f'print({json.dumps(frame)!r}, flush=True)\n' for frame in frames))
    with pytest.raises(ValueError):
        await rpc._stdio([sys.executable, str(script)], str(tmp_path), {}, {'id': 'onboarding'}, nonce='required')


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['', 'auto', 'pending', 'deny'])
async def test_omp_probe_does_not_launch_ambient_custom_tools_or_mcp(monkeypatch, tmp_path, mode):
    from unittest.mock import AsyncMock
    exchange = AsyncMock()
    monkeypatch.setattr(rpc, '_stdio', exchange)
    monkeypatch.setattr(rpc, '_binary', lambda binary, env: binary)
    result = await rpc.probe(AgentConfig(workspace_dir=str(tmp_path), permission_mode=mode), 'omp', {})
    exchange.assert_not_awaited()
    assert result['status'] == 'needs_input'
    assert result['protocol'] == result['workspace'] == result['login'] == 'unknown'
    assert result['permissions'] == 'unavailable'
    assert 'pending' in result['next_action'] and 'deny' in result['next_action']
