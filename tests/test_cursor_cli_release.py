"""Actual CLI handshake only; never uses a login, key or user session."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import signal

import pytest

from sleeptalk.harness_versions import inspect_cursor


@pytest.mark.asyncio
async def test_cursor_release_acp_initialization_and_missing_auth(tmp_path):
    binary = os.environ.get('SLEEPTALK_TEST_CURSOR_CLI')
    if not binary:
        pytest.skip('Select an isolated Cursor CLI for release qualification')
    expected = os.environ['SLEEPTALK_TEST_CURSOR_CLI_VERSION']
    selected = inspect_cursor(binary)
    assert selected['version'] == expected, selected
    assert Path(selected['binary']).is_file()
    home = tmp_path / 'home'
    home.mkdir()
    env = {'PATH': os.defpath, 'HOME': str(home), 'USERPROFILE': str(home),
           'XDG_CONFIG_HOME': str(home), 'XDG_CACHE_HOME': str(home),
           'CURSOR_CONFIG_DIR': str(home), 'CURSOR_DATA_DIR': str(home), 'NO_COLOR': '1'}
    process = await asyncio.create_subprocess_exec(
        selected['binary'], 'acp', env=env, cwd=home,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, start_new_session=True,
    )
    async def request(id, method, params):
        process.stdin.write((json.dumps({'jsonrpc': '2.0', 'id': id, 'method': method, 'params': params}) + '\n').encode())
        await process.stdin.drain()
        async with asyncio.timeout(20):
            for _ in range(100):
                raw = await process.stdout.readline()
                assert raw, 'CLI exited before responding'
                message = json.loads(raw)
                if message.get('id') == id:
                    return message
        pytest.fail('No matching ACP response')
    try:
        hello = await request(1, 'initialize', {'protocolVersion': 1, 'clientCapabilities': {},
                                               'clientInfo': {'name': 'sleeptalk-qualification', 'version': '1'}})
        assert hello['result']['protocolVersion'] == 1
        assert hello['result']['agentCapabilities']['loadSession'] is True
        assert 'cursor_login' in {m['id'] for m in hello['result']['authMethods']}
        session = await request(2, 'session/new', {'cwd': str(home), 'mcpServers': []})
        assert session['error']['code'] == -32000
        assert 'Authentication required' in session['error']['message']
        assert 'result' not in session
    finally:
        if process.returncode is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(process.wait(), 3)
            except TimeoutError:
                os.killpg(process.pid, signal.SIGKILL)
                await process.wait()
