from __future__ import annotations

import asyncio
from types import SimpleNamespace

from sleeptalk.policy import VoicePolicy
from sleeptalk.session import RealtimeSession
from sleeptalk.web_voice.session import WebCallSession


class _SettingsStream:
    def __init__(self) -> None:
        self.settings_handler = None
        self.presentations: list[dict] = []
        self.presentation_sent = asyncio.Event()

    async def send(self, _frame) -> None:
        return None

    def clear_output(self) -> int:
        return 0

    def set_settings_handler(self, handler) -> None:
        self.settings_handler = handler

    async def send_presentation(self, payload: dict) -> None:
        self.presentations.append(payload)
        self.presentation_sent.set()


class _SettingsSession:
    def __init__(self) -> None:
        self.policy = VoicePolicy()
        self.narration_requests: list[object] = []
        self.reply_length_requests: list[object] = []

    def set_audio_output(self, _output) -> None:
        return None

    def subscribe(self, _callback):
        return lambda: None

    def current_tts_speed(self) -> float:
        return 1.0

    def set_narration_style(self, value: object) -> str:
        self.narration_requests.append(value)
        self.policy.narration_style = str(value)
        return self.policy.narration_style

    def set_reply_length(self, value: object) -> str:
        self.reply_length_requests.append(value)
        self.policy.reply_length = str(value)
        return self.policy.reply_length

    async def aclose(self) -> None:
        return None


def test_session_normalizes_live_policy_settings() -> None:
    policy = VoicePolicy()
    session = RealtimeSession(SimpleNamespace(policy=policy, route=None))

    assert session.set_narration_style(" SILENCE ") == "silence"
    assert session.set_reply_length(" SUMMARIZED ") == "summarized"
    assert policy.narration_style == "silence"
    assert policy.reply_length == "summarized"

    assert session.set_narration_style("unknown") == "silence"
    assert session.set_reply_length("unknown") == "full"


async def test_browser_controls_use_the_session_settings_api() -> None:
    stream = _SettingsStream()
    kernel = _SettingsSession()
    web = WebCallSession(stream=stream, call_sid="opaque", session=kernel)

    assert stream.settings_handler is not None
    await stream.presentation_sent.wait()
    stream.presentation_sent.clear()
    stream.settings_handler(
        {"narrationStyle": "silence", "replyLength": "summarized"}
    )
    await stream.presentation_sent.wait()
    stream.presentation_sent.clear()
    stream.settings_handler({"narrationStyle": "narrated", "replyLength": "full"})
    await stream.presentation_sent.wait()
    stream.presentation_sent.clear()
    stream.settings_handler({"narration": "keyboard"})
    await stream.presentation_sent.wait()
    await web.close()

    assert kernel.narration_requests == ["silence", "narrated", "keyboard"]
    assert kernel.reply_length_requests == ["summarized", "full"]
    assert stream.presentations[-1]["narrationStyle"] == "keyboard"
    assert stream.presentations[-1]["replyLength"] == "full"


async def test_diagnostics_is_call_only_and_late_controls_cannot_reenable_it(tmp_path, monkeypatch):
    from sleeptalk.logging_setup import LATENCY_ENABLED_ENV, LATENCY_LOG_ENV
    from sleeptalk.voice_loop import telemetry

    monkeypatch.setenv(LATENCY_ENABLED_ENV, '0')
    monkeypatch.delenv(LATENCY_LOG_ENV, raising=False)
    web = WebCallSession(stream=_SettingsStream(), call_sid='test', session=_SettingsSession(),
                         profile_path=str(tmp_path/'profiles'/'voice.toml'))
    web.apply_settings({'diagnostics': 'true'})  # Only a real boolean is consent.
    assert not telemetry.latency_logging_enabled()
    web.apply_settings({'diagnostics': True})
    telemetry.log_latency_event(surface='web', transport='test', persona='test', stage='during_call')
    path=tmp_path/'logs'/'voice_latency.jsonl'
    assert 'during_call' in path.read_text()
    assert web._presentation.diagnostics_enabled is True
    await web.close()
    before=path.read_text()
    web.apply_settings({'diagnostics': True})
    telemetry.log_latency_event(surface='web',transport='test',persona='test',stage='after_call')
    assert not telemetry.latency_logging_enabled()
    assert path.read_text()==before
