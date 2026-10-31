import os
import subprocess
import sys

from sleeptalk.agent_environment import agent_environment, sdk_environment


def test_speech_bot_credentials_removed_agent_auth_preserved():
    source = {'DEEPGRAM_API_KEY': 'sentinel', 'CARTESIA_API_KEY': 'sentinel', 'DISCORD_BOT_TOKEN': 'sentinel', 'DISCORD_CUSTOM_TOKEN': 'sentinel', 'TEST_DISCORD_BOT_TOKEN': 'sentinel', 'ANTHROPIC_API_KEY': 'agent-auth', 'OPENAI_API_KEY': 'agent-auth', 'CURSOR_API_KEY': 'agent-auth', 'PATH': '/bin'}
    result = agent_environment(source)
    assert 'sentinel' not in result.values()
    assert result == {key: value for key, value in source.items() if value != 'sentinel'}
    assert source['DEEPGRAM_API_KEY'] == 'sentinel'


def test_real_child_cannot_see_inherited_speech_keys(monkeypatch):
    monkeypatch.setenv('DEEPGRAM_API_KEY', 'sentinel-private-value')
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'agent-auth')
    result = subprocess.run([sys.executable, '-c', 'import os; print(os.environ.get("DEEPGRAM_API_KEY", "absent")); print(os.environ["ANTHROPIC_API_KEY"])'], env=agent_environment(), capture_output=True, text=True, check=True)
    assert result.stdout == 'absent\nagent-auth\n'
    assert os.environ['DEEPGRAM_API_KEY'] == 'sentinel-private-value'


def test_sdk_merge_blanks_secrets_without_mutating_parent(monkeypatch):
    monkeypatch.setenv('DEEPGRAM_API_KEY', 'sentinel-private-value')
    overrides = sdk_environment({'CARTESIA_API_KEY': 'other-secret', 'ANTHROPIC_API_KEY': 'agent-auth'})
    merged = {**os.environ, **overrides}
    assert merged['DEEPGRAM_API_KEY'] == ''
    assert merged['CARTESIA_API_KEY'] == ''
    assert merged['ANTHROPIC_API_KEY'] == 'agent-auth'
    assert os.environ['DEEPGRAM_API_KEY'] == 'sentinel-private-value'


async def test_cursor_sdk_fails_before_bridge_when_it_cannot_isolate_keys(monkeypatch):
    from types import SimpleNamespace
    import pytest
    from sleeptalk.agents.cursor_sdk_adapter import CursorSdkError, SdkCursorFactory
    launched = []
    async def launch_bridge(**kwargs):
        launched.append(kwargs)
        raise AssertionError('bridge must not start')
    monkeypatch.setitem(sys.modules, 'cursor_sdk', SimpleNamespace(AsyncClient=SimpleNamespace(launch_bridge=launch_bridge), LocalAgentOptions=object))
    monkeypatch.setenv('DEEPGRAM_API_KEY', 'sentinel-private-value')
    factory = SdkCursorFactory(cwd='/tmp', model='', api_key='agent-auth', agent_id='')
    with pytest.raises(CursorSdkError, match='cannot isolate') as error:
        await factory.start()
    assert 'sentinel' not in str(error.value)
    assert not launched
