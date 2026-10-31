from __future__ import annotations

import asyncio
import json
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sleeptalk.adapter import ControlAccepted, ControlRejected, PermissionDecision, PermissionRequested, ResolvePermission, typed_events
from sleeptalk.agents.pi_rpc import PiRPCClient, PiRPCError, OmpRPCClient
from sleeptalk.config import AgentConfig
from sleeptalk.defaults import _build_pi_agent, _build_omp_agent


def test_extension_decisions_and_native_provider_requests():
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node is required to exercise the Pi extension')
    import sleeptalk.agents.pi_rpc as module
    extension = Path(module.__file__).with_name('pi_permission_extension.js')
    subprocess.run([node, str(Path(__file__).parent / 'fixtures/pi_extension_test.mjs'), str(extension)], check=True, capture_output=True, text=True, timeout=15)


@pytest.mark.asyncio
@pytest.mark.parametrize('factory', [_build_pi_agent])
async def test_explicit_policy_is_passed_and_extension_acknowledged(monkeypatch, tmp_path, factory):
    client = factory(AgentConfig(workspace_dir=str(tmp_path), permission_mode='pending'), {})
    assert client.permission_mode == 'pending'
    monkeypatch.setattr(client, '_resolve_binary', lambda *_: 'test-binary')
    monkeypatch.setattr(client, '_check_binary', lambda *_: None)
    if isinstance(client, OmpRPCClient):
        monkeypatch.setattr(client, '_subscribe_subagents', AsyncMock())
    calls = []
    async def spawn(argv, env):
        calls.append((argv, env))
        client._proc = SimpleNamespace(returncode=None)
        await client._route_message({'type': 'extension_ui_request', 'method': 'setStatus', 'statusKey': 'sleeptalk-permissions', 'statusText': json.dumps({
            'version': 1, 'nonce': env['SLEEPTALK_PERMISSION_NONCE'], 'mode': 'pending', 'workspace': str(tmp_path),
        })})
    monkeypatch.setattr(client, '_spawn', spawn)
    await client._ensure_started()
    assert '--extension' in calls[0][0]
    assert '--no-extensions' in calls[0][0]
    assert '--approve' not in calls[0][0]
    assert ('--no-approve' in calls[0][0]) == (not isinstance(client, OmpRPCClient))
    assert '--auto-approve' not in calls[0][0]
    assert calls[0][1]['SLEEPTALK_ONBOARDING_PROBE'] == '0'


@pytest.mark.asyncio
async def test_missing_extension_fails_before_a_prompt_is_sent(monkeypatch):
    client = PiRPCClient(permission_mode='pending')
    client.permission_load_timeout = .01
    monkeypatch.setattr(client, '_resolve_binary', lambda *_: 'pi')
    monkeypatch.setattr(client, '_check_binary', lambda *_: None)
    monkeypatch.setattr(client, '_spawn', AsyncMock())
    terminate = AsyncMock()
    monkeypatch.setattr(client, '_terminate', terminate)
    send = AsyncMock()
    monkeypatch.setattr(client, '_send_raw', send)
    with pytest.raises(PiRPCError, match='permission extension'):
        await client.respond('must not be sent')
    terminate.assert_awaited_once()
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_pending_decisions_are_explicit_and_cannot_be_replayed():
    client = PiRPCClient(permission_mode='pending')
    client._send_raw = AsyncMock()
    events = []
    client._on_event = typed_events(events.append)
    await client._route_message({'type': 'extension_ui_request', 'method': 'confirm', 'id': 'upstream', 'message': 'Allow bash?'})
    client._send_raw.assert_not_awaited()
    permission = events[0]
    assert isinstance(permission, PermissionRequested)
    assert isinstance(await client.control(ResolvePermission(permission.request_id, PermissionDecision.ALLOW_SESSION)), ControlRejected)
    result = await client.control(ResolvePermission(permission.request_id, PermissionDecision.ALLOW_ONCE))
    assert isinstance(result, ControlAccepted)
    assert client._send_raw.call_args.args[0] == {'type': 'extension_ui_response', 'id': 'upstream', 'confirmed': True}
    assert isinstance(await client.control(ResolvePermission(permission.request_id, PermissionDecision.ALLOW_ONCE)), ControlRejected)


@pytest.mark.asyncio
async def test_abort_cancels_pending_requests_and_rejects_old_decisions():
    client = PiRPCClient(permission_mode='pending')
    client._send_raw = AsyncMock()
    events = []
    client._on_event = typed_events(events.append)
    await client._route_message({'type': 'extension_ui_request', 'method': 'confirm', 'id': 'upstream'})
    await client.abort()
    assert client._send_raw.call_args.args[0]['cancelled'] is True
    assert isinstance(await client.control(ResolvePermission(events[0].request_id, PermissionDecision.ALLOW_ONCE)), ControlRejected)


@pytest.mark.asyncio
@pytest.mark.parametrize('method', ['confirm', 'select', 'input', 'editor'])
async def test_no_listener_or_unknown_dialog_is_never_autoapproved(method):
    client = PiRPCClient(permission_mode='pending')
    client._send_raw = AsyncMock()
    await client._route_message({'type': 'extension_ui_request', 'method': method, 'id': 'request', 'options': ['Allow']})
    assert client._send_raw.call_args.args[0] == {'type': 'extension_ui_response', 'id': 'request', 'cancelled': True}


@pytest.mark.asyncio
@pytest.mark.parametrize('decision', [PermissionDecision.ALLOW_ONCE, PermissionDecision.DENY])
async def test_real_extension_rpc_roundtrip_only_executes_after_approval(tmp_path, decision):
    import sys
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node is required to exercise the Pi extension')
    fixture = Path(__file__).parent / 'fixtures/pi_permission_runtime.mjs'
    launcher = tmp_path / 'fake-pi'
    launcher.write_text(f'#!{sys.executable}\nimport os,sys\nos.execv({node!r}, [{node!r}, {str(fixture)!r}, *sys.argv[1:]])\n')
    launcher.chmod(0o700)
    marker = tmp_path / 'tool-ran'
    client = PiRPCClient(cwd=str(tmp_path), pi_bin=str(launcher), permission_mode='pending', env={'TEST_TOOL_MARKER': str(marker)})
    events = []
    async def consume():
        return [part async for part in client.respond_streaming('exercise fake tool', on_event=typed_events(events.append))]
    task = asyncio.create_task(consume())
    try:
        async with asyncio.timeout(3):
            while not any(isinstance(event, PermissionRequested) for event in events):
                if task.done():
                    await task
                await asyncio.sleep(.01)
        assert not marker.exists()
        permission = next(event for event in events if isinstance(event, PermissionRequested))
        await client.control(ResolvePermission(permission.request_id, decision))
        await asyncio.wait_for(task, 3)
        assert marker.exists() == (decision is PermissionDecision.ALLOW_ONCE)
    finally:
        if not task.done():
            task.cancel()
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('client_type', [PiRPCClient, OmpRPCClient])
@pytest.mark.parametrize('mode', ['pending', 'deny'])
@pytest.mark.parametrize('extra_args', [['--extension', 'untrusted.js'], ['-e', 'untrusted.js'], ['--approve'], ['--hook', 'untrusted.js'], ['--no-extensions=false']])
async def test_protected_launch_rejects_extra_args_before_spawn(client_type, mode, extra_args):
    client = client_type(permission_mode=mode, extra_args=extra_args)
    client._spawn = AsyncMock()
    with pytest.raises(PiRPCError, match='extra_args'):
        await client._ensure_started()
    client._spawn.assert_not_awaited()


@pytest.mark.asyncio
async def test_auto_preserves_project_trust_and_extra_args(monkeypatch):
    client = PiRPCClient(permission_mode='auto', extra_args=['--extension', 'custom.js'])
    monkeypatch.setattr(client, '_resolve_binary', lambda *_: 'pi')
    monkeypatch.setattr(client, '_check_binary', lambda *_: None)
    client._spawn = AsyncMock()
    await client._ensure_started()
    argv = client._spawn.call_args.args[0]
    assert '--approve' in argv
    assert '--no-extensions' not in argv
    assert argv[-2:] == ['--extension', 'custom.js']


@pytest.mark.asyncio
async def test_installed_pi_loads_only_guard_in_isolated_home(tmp_path):
    """Exercise installed Pi without credentials, prompts, or network access."""
    pi = shutil.which('pi')
    node = shutil.which('node')
    if not pi or not node:
        pytest.skip('Installed Pi and Node are required for CLI isolation proof')
    home = tmp_path / 'home'
    agent = home / '.pi' / 'agent'
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    project_marker = tmp_path / 'project-extension-ran'
    global_marker = tmp_path / 'global-extension-ran'
    for directory, marker in [(workspace / '.pi' / 'extensions', project_marker), (agent / 'extensions', global_marker)]:
        directory.mkdir(parents=True)
        (directory / 'untrusted.js').write_text(
            'import { writeFileSync } from "node:fs";\n'
            f'writeFileSync({json.dumps(str(marker))}, "executed");\n'
            'export default function () {}\n'
        )
    network_marker = tmp_path / 'network-attempt'
    preload = tmp_path / 'block-network.cjs'
    preload.write_text(
        'const fs = require("node:fs");\n'
        f'const deny = () => {{ fs.writeFileSync({json.dumps(str(network_marker))}, "attempted"); throw new Error("Network disabled in isolation proof"); }};\n'
        'globalThis.fetch = deny;\n'
        'require("node:net").Socket.prototype.connect = deny;\n'
        'for (const name of ["http", "https", "tls"]) { const mod = require("node:" + name); for (const key of ["request", "get", "connect"]) if (key in mod) mod[key] = deny; }\n'
        'require("node:module").syncBuiltinESMExports();\n'
    )
    client = PiRPCClient(cwd=str(workspace), pi_bin=pi, permission_mode='deny', env={
        'HOME': str(home), 'PI_CODING_AGENT_DIR': str(agent),
        'XDG_CONFIG_HOME': str(home / '.config'), 'XDG_CACHE_HOME': str(home / '.cache'),
        'PATH': f'{Path(node).parent}:/usr/bin:/bin',
        'NODE_OPTIONS': f'--require={preload}', 'NO_COLOR': '1',
    })
    try:
        await client._ensure_started()
        assert client._permission_ready.is_set()
        state = await client._rpc({'type': 'get_state'})
        assert state.get('success') is True
        assert not project_marker.exists()
        assert not global_marker.exists()
        assert not network_marker.exists()
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['pending', 'deny'])
async def test_protected_omp_fails_before_launching_ambient_code(monkeypatch, tmp_path, mode):
    client = _build_omp_agent(AgentConfig(workspace_dir=str(tmp_path), permission_mode=mode, provider='openai', model='gpt-4.1'), {})
    monkeypatch.setattr(client, '_resolve_binary', lambda *_: 'omp')
    monkeypatch.setattr(client, '_check_binary', lambda *_: None)
    client._spawn = AsyncMock()
    with pytest.raises(PiRPCError, match='standalone compiled CLI'):
        await client._ensure_started()
    client._spawn.assert_not_awaited()


@pytest.mark.asyncio
async def test_omp_auto_retains_legacy_launch(monkeypatch):
    client = OmpRPCClient(permission_mode='auto')
    monkeypatch.setattr(client, '_resolve_binary', lambda *_: 'omp')
    monkeypatch.setattr(client, '_check_binary', lambda *_: None)
    monkeypatch.setattr(client, '_subscribe_subagents', AsyncMock())
    client._spawn = AsyncMock()
    await client._ensure_started()
    argv = client._spawn.call_args.args[0]
    assert '--auto-approve' in argv
    assert '--no-extensions' not in argv
