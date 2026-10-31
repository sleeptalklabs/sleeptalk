"""First-call defaults and explicit diagnostic consent at the write boundary."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from sleeptalk.config import SleeptalkConfig
from sleeptalk.logging_setup import LATENCY_ENABLED_ENV, LATENCY_LOG_ENV, setup_latency_log
from sleeptalk.setup_core import SetupAnswers, load_existing_answers, profile_toml
from sleeptalk.voice_loop import telemetry


def test_new_profile_is_quiet_and_keeps_the_full_written_result():
    import tomllib

    profile = tomllib.loads(profile_toml(SetupAnswers()))
    assert profile['transport']['type'] == 'web'
    assert profile['policy']['narration_style'] == 'silence'
    assert profile['policy']['reply_length'] == 'concise'
    assert profile['policy']['instant_ack_mode'] == 'delayed'
    assert profile['log']['latency'] is False
    assert SleeptalkConfig().log.level == 'WARNING'
    assert SleeptalkConfig().policy.background_jobs


def test_explicit_existing_preferences_survive_setup(tmp_path):
    (tmp_path/'voice.toml').write_text('[agent]\ntype="pi"\n[policy]\nreply_length="full"\ninstant_ack_mode="blind"\n[log]\nlatency=true\n')
    answers = load_existing_answers('voice', profiles_dir=tmp_path)
    assert answers is not None
    assert answers.reply_length == 'full'
    assert answers.instant_ack_mode == 'blind'
    assert answers.latency_log is True


def test_no_trace_opt_in_writes_neither_file_nor_stdout(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv('SLEEPTALK_HOME', str(tmp_path))
    telemetry.VoiceLatencyTrace(surface='web', transport='test', persona='test').mark('test')
    telemetry.log_latency_event(surface='web', transport='test', persona='test', stage='test')
    assert not (tmp_path/'logs').exists()
    assert capsys.readouterr().out == ''


def test_off_blocks_all_writers_even_with_an_inherited_path_and_stdout(tmp_path, monkeypatch, capsys):
    path=tmp_path/'must-not-exist.jsonl'
    monkeypatch.setenv(LATENCY_LOG_ENV, str(path))
    monkeypatch.setenv('SLEEPTALK_TELEMETRY_STDOUT', '1')
    assert setup_latency_log(logs_dir=tmp_path, enabled=False) is None
    telemetry.VoiceLatencyTrace(surface='web', transport='test', persona='test').mark('capture')
    telemetry.log_latency_event(surface='discord', transport='test', persona='test', stage='capture')
    assert not path.exists()
    assert capsys.readouterr().out == ''


def test_opt_in_timings_remove_content_and_preserve_measurements(tmp_path, monkeypatch):
    path=setup_latency_log(logs_dir=tmp_path, enabled=True)
    telemetry.log_latency_event(surface='web',transport='test',persona='test',stage='turn_complete',
        elapsedMs=321, transcript='PRIVATE_CONTENT', prompt='PRIVATE_CONTENT', error='PRIVATE_CONTENT',
        nested={'text':'PRIVATE_CONTENT','timeMs':123}, details='PRIVATE_CONTENT')
    text=path.read_text()
    assert 'PRIVATE_CONTENT' not in text
    record=json.loads(text)
    assert record['elapsedMs']==321 and record['nested']['timeMs']==123
    assert path.stat().st_mode & 0o777 == 0o600


def test_timings_rotate_and_never_follow_a_symlink(tmp_path, monkeypatch):
    monkeypatch.setattr(telemetry,'MAX_LOG_BYTES',800)
    path=setup_latency_log(logs_dir=tmp_path, enabled=True)
    for number in range(20):
        telemetry.log_latency_event(surface='web',transport='test',persona='test',stage='test', number=number)
    backup=Path(str(path)+'.1')
    assert path.stat().st_size<=800 and backup.stat().st_size<=800
    assert len(list(tmp_path.iterdir()))==2
    assert json.loads(path.read_text().splitlines()[-1])['number']==19
    target=tmp_path/'private-target'
    path.unlink()
    path.symlink_to(target)
    telemetry.log_latency_event(surface='web',transport='test',persona='test',stage='test')
    assert not target.exists()


def test_transcript_debug_stays_separate_from_performance_traces(tmp_path, monkeypatch):
    monkeypatch.setenv('SLEEPTALK_HOME',str(tmp_path))
    setup_latency_log(logs_dir=tmp_path,enabled=True)
    telemetry.log_chat_turn(surface='web',transport='test',persona='test',user_text='PRIVATE',assistant_text='PRIVATE')
    assert not (tmp_path/'logs'/'transcript_debug.jsonl').exists()


async def test_delayed_mode_never_plays_an_immediate_canned_ack():
    from sleeptalk.policy import VoicePolicy
    from sleeptalk.voice_policy_runtime import VoicePolicyRuntime
    from tests.test_reply_length import _Agent, _Ctx

    said=[]
    async def speak(line,ctx):
        said.append(line)
    runtime=VoicePolicyRuntime(agent=_Agent('The answer is ready.'), policy=VoicePolicy(instant_ack_mode='delayed'))
    assert await runtime.play_instant_ack('What time is it?',_Ctx(),speak_policy=speak) is None
    result=[line async for line in runtime.respond('What time is it?',_Ctx(),speak_policy=speak)]
    assert result==['The answer is ready.'] and said==[]
