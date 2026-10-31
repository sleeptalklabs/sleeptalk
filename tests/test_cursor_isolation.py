from __future__ import annotations

import json
from pathlib import Path
import sys
from unittest.mock import AsyncMock

import pytest

from sleeptalk.agents import cursor_isolation, onboarding_cli
from sleeptalk.config import AgentConfig
from sleeptalk.defaults import _build_cursor_acp_agent
from sleeptalk.runtime_probe import probe_runtime


FAKE_CURSOR = r'''
import json,os,sys
from pathlib import Path
workspace = Path(sys.argv[-1]) if sys.argv[-1] != 'acp' else None
home=Path(os.environ['HOME'])
assert Path.cwd() == home
assert os.environ['CURSOR_CONFIG_DIR'].startswith(str(home))
assert os.environ['CURSOR_DATA_DIR'].startswith(str(home))
assert os.environ['CURSOR_API_KEY'] == 'synthetic-key'
assert 'NODE_OPTIONS' not in os.environ
assert 'CURSOR_AUTH_TOKEN' not in os.environ
assert 'DEEPGRAM_API_KEY' not in os.environ
assert '--endpoint' in sys.argv
assert sys.argv[sys.argv.index('--endpoint')+1] == 'https://api2.cursor.sh'
methods=[]
for line in sys.stdin:
 q=json.loads(line); method=q['method']; methods.append(method)
 if method in {'authenticate','session/prompt','session/load'}: raise AssertionError('Unexpected user work or interactive authentication')
 if method=='initialize': result={'protocolVersion':1,'agentCapabilities':{'sessionCapabilities':{'list':{}}},'authMethods':[{'id':'cursor_login'}]}
 elif method=='session/new':
  workspace=Path(q['params']['cwd'])
  assert workspace != home
  assert q['params']['mcpServers'] == []
  (workspace/'protocol-methods.json').write_text(json.dumps(methods))
  result={'sessionId':'new-private-session','models':{'currentModelId':'native-default'}}
 elif method=='session/set_config_option':
  assert q['params']['configId']=='model'
  result={'configOptions':[{'id':'model','currentValue':q['params']['value']}]}
 elif method=='session/list':
  assert q['params']['cwd']==str(workspace)
  (workspace/'protocol-methods.json').write_text(json.dumps(methods))
  result={'sessions':[{'sessionId':'new-private-session','cwd':str(workspace)}]}
 else: result={}
 print(json.dumps({'jsonrpc':'2.0','id':q['id'],'result':result}),flush=True)
'''


def fake_cursor(tmp_path):
    script = tmp_path / 'agent'
    script.write_text(f'#!{sys.executable}\n' + FAKE_CURSOR)
    script.chmod(0o700)
    return script


def test_isolated_cursor_environment_contains_only_selected_key_and_system_paths(tmp_path):
    source = {'PATH': '/bin', 'CURSOR_API_KEY': 'synthetic-key', 'CURSOR_AUTH_TOKEN': 'other-token',
              'HOME': '/operator', 'NODE_OPTIONS': '--require=evil', 'DEEPGRAM_API_KEY': 'speech', 'CURSOR_API_URL': 'https://wrong.invalid'}
    env = cursor_isolation.environment(source, str(tmp_path))
    assert env['CURSOR_API_KEY'] == 'synthetic-key'
    assert not {'NODE_OPTIONS', 'CURSOR_AUTH_TOKEN', 'DEEPGRAM_API_KEY', 'CURSOR_API_URL'} & env.keys()
    assert env['HOME'] == env['USERPROFILE'] == str(tmp_path.resolve())
    assert env['GIT_CONFIG_VALUE_0'] == 'false'
    assert env['GIT_CONFIG_VALUE_1'] == '/dev/null'


@pytest.mark.asyncio
@pytest.mark.parametrize('status,body,expected', [
    (200, b'{"apiKeyName":"private name","createdAt":"2026-01-01","userEmail":"private@example.test"}', 'available'),
    (401, b'private error', 'unavailable'), (403, b'private error', 'unknown'),
    (302, b'', 'unknown'), (200, b'{}', 'unknown'), (200, b'x'*70000, 'unknown'),
], ids=['valid', 'unauthorized', 'forbidden', 'redirect', 'malformed', 'oversized'])
async def test_native_key_check_is_bounded_and_never_follows_redirects(monkeypatch, status, body, expected):
    import aiohttp
    class Content:
        async def iter_chunked(self, size):
            for start in range(0, len(body), size): yield body[start:start+size]
    class Response:
        content = Content()
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
    response=Response(); response.status=status
    class Client:
        def __init__(self, *, timeout, trust_env):
            assert timeout.total == 8 and not trust_env
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def get(self, url, *, headers, allow_redirects):
            assert url == 'https://api.cursor.com/v1/me'
            assert headers == {'Authorization': 'Basic c3ludGhldGljLWtleTo='}
            assert allow_redirects is False
            return response
    monkeypatch.setattr(aiohttp, 'ClientSession', Client)
    assert await cursor_isolation.verify_api_key('synthetic-key') == expected


@pytest.mark.asyncio
@pytest.mark.parametrize('model', ['', 'chosen-model'])
async def test_real_protocol_process_binds_workspace_without_auth_or_prompt(monkeypatch, tmp_path, model):
    script = fake_cursor(tmp_path)
    monkeypatch.setattr(onboarding_cli, '_cursor_binary', lambda env: str(script))
    monkeypatch.setattr(cursor_isolation, 'verify_api_key', AsyncMock(return_value='available'))
    capture = AsyncMock()
    monkeypatch.setattr(onboarding_cli, '_capture', capture)
    result = await probe_runtime(AgentConfig(type='cursor_acp', runtime='acp', on_incompatible='fail', workspace_dir=str(tmp_path), permission_mode='pending', model=model), env={'CURSOR_API_KEY': 'synthetic-key', 'NODE_OPTIONS': 'evil', 'CURSOR_AUTH_TOKEN': 'other'})
    assert result['status'] == 'ready', result
    assert result['adapter'] == 'cursor_acp' and result['runtime'] == 'acp'
    assert result['login'] == result['workspace'] == result['permissions'] == 'available'
    assert 'synthetic-key' not in json.dumps(result) and 'new-private-session' not in json.dumps(result)
    methods = json.loads((tmp_path/'protocol-methods.json').read_text())
    assert methods == ['initialize', 'session/new', *(['session/set_config_option'] if model else []), 'session/list']
    capture.assert_not_awaited()


@pytest.mark.asyncio
async def test_unverified_key_never_starts_cursor_or_browser(monkeypatch, tmp_path):
    monkeypatch.setattr(cursor_isolation, 'verify_api_key', AsyncMock(return_value='unavailable'))
    async def forbidden(*args, **kwargs): raise AssertionError('No process allowed')
    monkeypatch.setattr(onboarding_cli, '_process', forbidden)
    result = await onboarding_cli.probe(AgentConfig(workspace_dir=str(tmp_path), permission_mode='deny'), 'cursor_acp', {'CURSOR_API_KEY': 'synthetic-key'})
    assert result['status'] == 'needs_input' and result['login'] == 'unavailable'


@pytest.mark.asyncio
@pytest.mark.parametrize('bad', ['workspace', 'model', 'permission_request'])
async def test_cursor_binding_and_server_work_fail_closed(monkeypatch, tmp_path, bad):
    monkeypatch.setattr(cursor_isolation, 'verify_api_key', AsyncMock(return_value='available'))
    script=fake_cursor(tmp_path)
    source=script.read_text()
    if bad=='workspace': source=source.replace("'cwd':str(workspace)", "'cwd':'/wrong-workspace'")
    elif bad=='model': source=source.replace("'currentValue':q['params']['value']", "'currentValue':'wrong-model'")
    else: source=source.replace("'result':result", "'method':'session/request_permission','params':{}")
    script.write_text(source)
    monkeypatch.setattr(onboarding_cli, '_cursor_binary', lambda env: str(script))
    result=await onboarding_cli.probe(AgentConfig(workspace_dir=str(tmp_path), permission_mode='pending', model='chosen'), 'cursor_acp', {'CURSOR_API_KEY':'synthetic-key'})
    assert result['status']=='needs_input'
    assert result['workspace']!='available'


@pytest.mark.asyncio
async def test_runtime_uses_same_isolation_and_applies_registered_model(monkeypatch, tmp_path):
    script=fake_cursor(tmp_path)
    client=_build_cursor_acp_agent(AgentConfig(workspace_dir=str(tmp_path), permission_mode='pending', model='chosen'), {'CURSOR_API_KEY':'synthetic-key'})
    monkeypatch.setattr(client, '_resolve_binary', lambda *_: str(script))
    try:
        await client._ensure_started()
        home=Path(client._isolated_home.name)
        assert client.model=='chosen' and client._model_session==client._session_id
        assert home != tmp_path
    finally: await client.close()
    assert not home.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("relative", [".cursor/hooks.json", ".claude/settings.json", ".claude/settings.local.json"])
async def test_project_hook_configs_block_probe_and_runtime_before_launch(monkeypatch, tmp_path, relative):
    config = tmp_path / relative
    config.parent.mkdir(parents=True)
    config.write_text('{"hooks":{"beforeShellExecution":[{"command":"private-command"}]}}')
    check = AsyncMock()
    monkeypatch.setattr(cursor_isolation, "verify_api_key", check)
    result = await onboarding_cli.probe(AgentConfig(workspace_dir=str(tmp_path), permission_mode="pending"), "cursor_acp", {"CURSOR_API_KEY": "synthetic-key"})
    assert result["permissions"] == "unavailable"
    assert "private-command" not in json.dumps(result)
    check.assert_not_awaited()
    client = _build_cursor_acp_agent(AgentConfig(workspace_dir=str(tmp_path), permission_mode="deny"), {"CURSOR_API_KEY": "synthetic-key"})
    client._spawn = AsyncMock()
    try:
        with pytest.raises(ValueError, match="hook configuration"):
            await client._ensure_started()
        client._spawn.assert_not_awaited()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_native_permissions_never_export_values_or_grant_permanent_approval(tmp_path):
    from sleeptalk.adapter import PermissionDecision, ResolvePermission, ControlRejected, typed_events
    client = _build_cursor_acp_agent(AgentConfig(workspace_dir=str(tmp_path), permission_mode="pending"), {"CURSOR_API_KEY": "synthetic-key"})
    client._send_json = AsyncMock()
    events = []
    client._on_event = typed_events(events.append)
    options = [{"optionId": "allow-always", "kind": "allow_always"}, {"optionId": "allow-once", "kind": "allow_once"}, {"optionId": "reject-once", "kind": "reject_once"}]
    await client._handle_permission(8, {"options": options, "toolCall": {"title": "secret-command", "toolCallId": "tool-id"}})
    assert events[-1].prompt == "Allow the pending agent tool operation?"
    assert events[-1].options == frozenset({PermissionDecision.ALLOW_ONCE, PermissionDecision.DENY})
    assert isinstance(await client._resolve_permission(ResolvePermission(request_id="8", decision=PermissionDecision.ALLOW_SESSION)), ControlRejected)
    client._send_json.assert_not_awaited()
    await client._resolve_permission(ResolvePermission(request_id="8", decision=PermissionDecision.ALLOW_ONCE))
    assert client._send_json.call_args.args[0]["result"]["outcome"]["optionId"] == "allow-once"
    await client._handle_permission(9, {"options": [{"optionId": "allow-always"}]})
    assert client._send_json.call_args.args[0]["result"]["outcome"] == {"outcome": "cancelled"}
    await client.close()
