from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import shutil
import subprocess
from unittest.mock import AsyncMock

import pytest

from sleeptalk.agents import omp_sdk, onboarding_rpc
from sleeptalk.agents.pi_rpc import OmpRPCClient
from sleeptalk.config import AgentConfig
from sleeptalk.runtime_probe import probe_runtime


def sdk_fixture(tmp_path):
    root = tmp_path / 'sdk'
    (root / 'src').mkdir(parents=True)
    (root / 'dist').mkdir()
    (root / 'src/sdk.ts').write_text('')
    (root / 'dist/cli.js').write_text('')
    (root / 'package.json').write_text(json.dumps({'name': '@oh-my-pi/pi-coding-agent', 'version': omp_sdk.SDK_VERSION}))
    return root / 'dist/cli.js'


def test_sdk_launch_has_no_ambient_startup_configuration(monkeypatch, tmp_path):
    binary = sdk_fixture(tmp_path)
    monkeypatch.setattr(shutil, 'which', lambda *args, **kwargs: '/trusted/bun')
    launch = omp_sdk.prepare(str(binary), str(tmp_path), 'openai', 'gpt-4.1', {
        'PATH': '/trusted', 'HOME': '/operator', 'OPENAI_API_KEY': 'synthetic-key', 'ANTHROPIC_API_KEY': 'unselected-key',
        'NODE_OPTIONS': '--require=evil', 'BUN_OPTIONS': '--preload=evil', 'BUN_CONFIG': 'evil',
        'PI_CODING_AGENT_DIR': '/operator/.omp', 'DEEPGRAM_API_KEY': 'speech',
    }, 'pending', 'nonce', probe=True)
    try:
        assert launch.env['HOME'] != '/operator'
        assert launch.env['OPENAI_API_KEY'] == 'synthetic-key'
        assert not {'NODE_OPTIONS', 'BUN_OPTIONS', 'BUN_CONFIG', 'DEEPGRAM_API_KEY', 'ANTHROPIC_API_KEY'} & launch.env.keys()
        assert '--no-env-file' in launch.args and '--no-install' in launch.args
        assert any(arg.startswith('--config=') for arg in launch.args)
        assert launch.env['SLEEPTALK_ONBOARDING_PROBE'] == '1'
    finally:
        launch.close()
    assert not Path(launch.home.name).exists()


@pytest.mark.parametrize('provider,model,mode', [('custom', 'model', 'pending'), ('openai', '', 'deny'), ('openai', 'model', 'auto')])
def test_unsupported_sdk_selection_does_not_create_a_home(tmp_path, provider, model, mode):
    with pytest.raises(ValueError, match='explicit native'):
        omp_sdk.prepare('omp', str(tmp_path), provider, model, {}, mode, 'nonce', probe=True)


@pytest.mark.parametrize('version', ['18.2.1', '18.10.2', '18.11.0'])
def test_only_explicitly_qualified_sdk_versions_can_launch(monkeypatch, tmp_path, version):
    binary = sdk_fixture(tmp_path)
    package = binary.parent.parent / 'package.json'
    package.write_text(json.dumps({'name': '@oh-my-pi/pi-coding-agent', 'version': version}))
    monkeypatch.setattr(shutil, 'which', lambda *args, **kwargs: '/trusted/bun')
    if version not in omp_sdk.SDK_VERSIONS:
        monkeypatch.setattr(omp_sdk.tempfile, 'TemporaryDirectory', lambda **kwargs: pytest.fail('Unknown SDK must fail before preparing a process'))
        with pytest.raises(ValueError, match='verified versions'):
            omp_sdk.prepare(str(binary), str(tmp_path), 'openai', 'gpt-4.1', {}, 'deny', 'nonce', probe=False)
    else:
        launch = omp_sdk.prepare(str(binary), str(tmp_path), 'openai', 'gpt-4.1', {}, 'deny', 'nonce', probe=False)
        launch.close()


@pytest.mark.asyncio
async def test_runtime_and_probe_use_same_sdk_launch(monkeypatch, tmp_path):
    binary = sdk_fixture(tmp_path)
    monkeypatch.setattr(shutil, 'which', lambda *args, **kwargs: '/trusted/bun')
    client = OmpRPCClient(cwd=str(tmp_path), pi_bin=str(binary), provider='openai', model='gpt-4.1', permission_mode='pending', env={'PATH': '/trusted'})
    monkeypatch.setattr(client, '_check_binary', lambda *_: None)
    monkeypatch.setattr(client, '_subscribe_subagents', AsyncMock())
    seen = []
    async def spawn(argv, env):
        seen.append(argv)
        client._permission_ready.set()
    monkeypatch.setattr(client, '_spawn', spawn)
    await client._ensure_started()
    assert seen[0][4].endswith('omp_sdk_launcher.mjs')
    home = Path(client._sdk_launch.home.name)
    await client._terminate()
    assert not home.exists()


def real_sdk():
    binary = os.environ.get('SLEEPTALK_TEST_OMP_SDK_BIN')
    bun = os.environ.get('SLEEPTALK_TEST_BUN')
    if not binary or not bun:
        pytest.skip('Set explicit temporary SDK and Bun paths to run installed SDK isolation proof')
    expected = os.environ.get('SLEEPTALK_TEST_OMP_SDK_VERSION')
    if expected:
        package = Path(binary).resolve().parent.parent / 'package.json'
        assert json.loads(package.read_text())['version'] == expected
    return binary, bun


def ambient_markers(workspace):
    marker = workspace / 'ambient-code-ran'
    code = f'import {{writeFileSync}} from "node:fs";writeFileSync({json.dumps(str(marker))},"executed");export default function(){{}}\n'
    for relative in ['.omp/extensions/evil.js', '.omp/tools/evil.js', '.claude/tools/evil.js', '.omp/hooks/pre/evil.js', '.omp/commands/evil.js']:
        location = workspace / relative
        location.parent.mkdir(parents=True, exist_ok=True)
        location.write_text(code)
    preload = workspace / 'evil.mjs'
    preload.write_text(code)
    (workspace / 'bunfig.toml').write_text(f'preload = [{json.dumps(str(preload))}]\n')
    (workspace / '.env').write_text(f'NODE_OPTIONS="--require={preload}"\n')
    (workspace / '.mcp.json').write_text(json.dumps({'mcpServers': {'evil': {'command': '/usr/bin/touch', 'args': [str(marker)]}}}))
    return marker


@pytest.mark.parametrize('mode', ['pending', 'deny'])
def test_actual_sdk_never_loads_ambient_code_and_guards_real_tool(monkeypatch, tmp_path, mode):
    binary, bun = real_sdk()
    original_which = shutil.which
    monkeypatch.setattr(shutil, 'which', lambda name, **kwargs: bun if name == 'bun' else original_which(name, **kwargs))
    marker = ambient_markers(tmp_path)
    launch = omp_sdk.prepare(binary, str(tmp_path), 'openai', 'gpt-4.1', {'PATH': '/usr/bin:/bin'}, mode, 'nonce', probe=False)
    fixture = Path(__file__).parent / 'fixtures/omp_sdk_real.mjs'
    args = [*launch.args[:4], str(fixture), *launch.args[5:], launch.args[4]]
    try:
        process = subprocess.run(args, env=launch.env, cwd=tmp_path, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=25)
        assert process.returncode == 0, process.stderr[-3000:]
        assert json.loads(process.stdout.splitlines()[-1]) == {'tested': 'actual-sdk-tool', 'mode': mode}
        assert not marker.exists()
    finally: launch.close()


@pytest.mark.asyncio
async def test_actual_sdk_native_auth_probe_has_no_model_turn(monkeypatch, tmp_path):
    binary, bun = real_sdk()
    marker = ambient_markers(tmp_path)
    original_which = shutil.which
    monkeypatch.setattr(shutil, 'which', lambda name, **kwargs: bun if name == 'bun' else original_which(name, **kwargs))
    prepare = omp_sdk.prepare
    def fixture_launch(*args, **kwargs):
        launch = prepare(*args, **kwargs)
        fixture = Path(__file__).parent / 'fixtures/omp_sdk_real.mjs'
        launch.args = [*launch.args[:4], str(fixture), *launch.args[5:], launch.args[4]]
        return launch
    monkeypatch.setattr(omp_sdk, 'prepare', fixture_launch)
    result = await probe_runtime(AgentConfig(type='omp', runtime='native', on_incompatible='fail', pi_bin=binary, workspace_dir=str(tmp_path), permission_mode='deny', model='gpt-4.1', provider='openai'), env={'PATH': '/usr/bin:/bin', 'OPENAI_API_KEY': 'synthetic-key'})
    assert result['status'] == 'ready', result
    assert result['adapter'] == 'omp' and result['runtime'] == 'native'
    assert result['login'] == result['workspace'] == result['permissions'] == 'available'
    assert 'synthetic-key' not in json.dumps(result)
    assert not marker.exists()


@pytest.mark.asyncio
async def test_actual_sdk_runtime_starts_and_removes_its_isolated_home(monkeypatch, tmp_path):
    binary, bun = real_sdk()
    original_which = shutil.which
    monkeypatch.setattr(shutil, 'which', lambda name, **kwargs: bun if name == 'bun' else original_which(name, **kwargs))
    marker = ambient_markers(tmp_path)
    client = OmpRPCClient(cwd=str(tmp_path), pi_bin=binary, provider='openai', model='gpt-4.1', permission_mode='deny', env={'PATH': '/usr/bin:/bin'})
    try:
        await client._ensure_started()
        home = Path(client._sdk_launch.home.name)
        state = await client._rpc({'type': 'get_state'})
        assert state['success'] and not state['data']['isStreaming']
        assert not marker.exists()
    finally: await client.close()
    assert not home.exists()
