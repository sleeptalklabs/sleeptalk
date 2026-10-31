"""Independent black-box acceptance for the browser RealtimeSession cutover.

The boundary design was approved and authored by
``codex-gpt5-browser-scope-rereview-20260822``.  These tests use public
entrypoints and public session methods only; they never assign runtime-private
state.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
import subprocess
import sys
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from av import AudioFrame
from sleeptalk.call_provider import CallContext, CallEnded
from sleeptalk.config import SleeptalkConfig
from sleeptalk.session_events import (
    InterruptionPhase,
    InterruptionSource,
    SessionEvent,
    SessionEventType,
    interruption,
    progress,
    response,
    tool_event,
)

from sleeptalk import web_call

JOIN_URL = "https://calls.example.test/#/call/browser-kernel"
MIC_SAMPLE = 8_000
REPLY_PCM = b"\x41\x52" * 960


REGISTRATION_PROBE = r"""
import json
import sys
from pathlib import Path

from sleeptalk.webcalls import register_web_call

MODE, PROFILE, READY = sys.argv[1:]
JOIN_URL = "https://calls.example.test/#/call/browser-kernel"
calls = []

async def fake_runtime(**kwargs):
    calls.append({key: str(value) for key, value in kwargs.items() if key != "on_ready"})
    kwargs["on_ready"](JOIN_URL)

# Package host auto-registers on import; override so link/detached never
# touch live profile composition or WebRTC in this acceptance check.
register_web_call(fake_runtime)
if MODE == "foreground":
    from sleeptalk import cli

    result = cli.main([
        "link",
        "--foreground",
        "--config", PROFILE,
        "--rendezvous", "https://calls.example.test",
        "--agent-instance", "pi-browser",
        "--platform-user-id", "test-user",
    ])
    payload = {"result": result, "calls": calls}
else:
    from sleeptalk import detached_call
    from sleeptalk.call_registry import ProfileIdentity, acquire_profile_lease, new_call_id

    ready = Path(READY)
    ready.touch()
    registry_root = ready.parent / "calls"
    identity = ProfileIdentity.from_config(config_path=PROFILE, profile="pi-browser")
    lease = acquire_profile_lease(identity=identity, root=registry_root)
    result = detached_call.main([
        "--child",
        "--ready-file", str(ready),
        f"--call-id={new_call_id()}",  # an ID may begin with "-"
        "--registry-root", str(registry_root),
        "--profile-key", identity.key,
        "--profile", identity.profile,
        "--profile-lock-fd", str(lease.fd),
        "--rendezvous", "https://calls.example.test",
        "--agent-instance", "pi-browser",
        "--conversation-id", "",
        "--platform-user-id", "test-user",
        "--ttl-seconds", "300",
        "--config", PROFILE,
    ])
    payload = {"result": result, "calls": calls, "ready": json.loads(ready.read_text())}

assert payload["result"] == 0, payload
assert len(payload["calls"]) == 1, payload
if MODE == "detached":
    assert payload["ready"]["url"] == JOIN_URL, payload
print(json.dumps(payload, sort_keys=True))
"""


@pytest.mark.parametrize("mode", ["foreground", "detached"])
def test_server_environment_link_modes_self_register_and_return_url(
    mode: str, tmp_path: Path
) -> None:
    """Both documented modes bootstrap the server runtime in a fresh process."""

    profile = tmp_path / "config" / "profiles" / "pi-browser.toml"
    profile.parent.mkdir(parents=True)
    profile.write_text('[agent]\ntype = "pi"\nslug = "pi-browser"\n', encoding="utf-8")
    ready = tmp_path / "ready.json"
    result = subprocess.run(
        [sys.executable, "-c", REGISTRATION_PROBE, mode, str(profile), str(ready)],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["result"] == 0
    assert len(payload["calls"]) == 1
    if mode == "foreground":
        assert JOIN_URL in result.stdout
    else:
        assert payload["ready"]["url"] == JOIN_URL


def pi_browser_config() -> SleeptalkConfig:
    """Credential-free Pi config with deterministic short endpoint timing."""

    return SleeptalkConfig.model_validate(
        {
            "transport": {"type": "null"},
            "agent": {"type": "pi", "name": "Pi Browser", "slug": "pi-browser"},
            "policy": {
                "instant_ack": False,
                "keepalive_interval_s": 0,
                "capture": {
                    "silence_ms": 20,
                    "min_utterance_ms": 20,
                    "min_transcript_words": 1,
                    "rms_threshold": 100,
                    "vad_mode": "rms",
                    "smart_turn": False,
                },
                "turns": {
                    "vad_threshold": 100,
                    "interrupt_min_ms": 20,
                    "bot_grace_ms": 0,
                    "duck_on_vad": False,
                },
            },
        }
    )


class StrictRealtimeSession:
    """A public-session spy whose legacy component properties explode."""

    session_id = "browser-kernel-session"

    def __init__(self, config: SleeptalkConfig) -> None:
        self.config = config
        self.policy = config.policy
        self.calls: list[tuple[str, Any]] = []
        self.appended_pcm: list[bytes] = []
        self.turn_pcm: list[bytes] = []
        self.output = None
        self.cancelled = 0
        self.closed = 0
        self.prepared = 0
        self.subscribers: list[Callable[[SessionEvent], Any]] = []
        self.turn_id_counter = 0

    @property
    def agent(self):  # pragma: no cover - failure sentinel
        raise AssertionError("browser accessed the agent outside RealtimeSession")

    @property
    def stt(self):  # pragma: no cover - failure sentinel
        raise AssertionError("browser accessed STT outside RealtimeSession")

    @property
    def streaming_stt(self):  # pragma: no cover - failure sentinel
        raise AssertionError("browser accessed streaming STT outside RealtimeSession")

    @property
    def tts(self):  # pragma: no cover - failure sentinel
        raise AssertionError("browser accessed TTS outside RealtimeSession")

    @property
    def route(self):  # pragma: no cover - failure sentinel
        raise AssertionError("browser accessed the route outside RealtimeSession")

    def subscribe(self, callback: Callable[[SessionEvent], Any]) -> Callable[[], None]:
        self.calls.append(("subscribe", callback))
        self.subscribers.append(callback)

        def unsubscribe() -> None:
            if callback in self.subscribers:
                self.subscribers.remove(callback)

        return unsubscribe

    def unsubscribe(self, callback: Callable[[SessionEvent], Any]) -> None:
        if callback in self.subscribers:
            self.subscribers.remove(callback)

    def emit(self, event: SessionEvent) -> None:
        for callback in tuple(self.subscribers):
            result = callback(event)
            if inspect.isawaitable(result):
                asyncio.create_task(result)

    def set_audio_output(self, output: Any) -> None:
        assert callable(getattr(output, "write", None))
        assert callable(getattr(output, "accounting", None))
        assert callable(getattr(output, "close", None))
        self.output = output
        self.calls.append(("set_audio_output", output))

    async def prepare(self) -> None:
        self.prepared += 1
        self.calls.append(("prepare", None))

    async def append_audio(self, pcm: bytes, *, participant_id: int = 0) -> None:
        assert pcm
        self.appended_pcm.append(bytes(pcm))
        self.calls.append(("append_audio", participant_id))

    async def run_turn(
        self,
        route_input: Any = None,
        *,
        text: str = "",
        pcm: bytes = b"",
        output: Any = None,
        extras: dict[str, Any] | None = None,
        turn_id: int | None = None,
    ) -> Any:
        del extras
        if route_input is not None:
            text = route_input.text
            pcm = route_input.pcm
        if output is not None:
            self.set_audio_output(output)
        assert self.output is not None, "browser must bind its AudioOutput before a turn"
        self.turn_id_counter = turn_id or self.turn_id_counter + 1
        self.calls.append(("run_turn", {"text": text, "pcm": bytes(pcm)}))
        if pcm:
            self.turn_pcm.append(bytes(pcm))

        current = self.turn_id_counter
        self.emit(response(self.session_id, current, SessionEventType.RESPONSE_STARTED))
        self.emit(
            progress(
                self.session_id,
                current,
                pool="reassure",
                text="Still checking the browser orchard.",
            )
        )
        self.emit(
            tool_event(
                self.session_id,
                current,
                SessionEventType.TOOL_STARTED,
                name="browser-search",
            )
        )
        self.emit(
            tool_event(
                self.session_id,
                current,
                SessionEventType.TOOL_COMPLETED,
                name="browser-search",
            )
        )
        await self.output.write(REPLY_PCM, current)
        self.emit(
            response(
                self.session_id,
                current,
                SessionEventType.RESPONSE_FIRST_OUTPUT,
                output_kind="audio",
            )
        )
        self.emit(response(self.session_id, current, SessionEventType.RESPONSE_COMPLETED))
        return SimpleNamespace(status="completed", turn_id=current)

    async def interrupt_presentation(
        self,
        turn_id: int | None = None,
        *,
        source: InterruptionSource = InterruptionSource.USER_SPEECH,
    ) -> None:
        self.cancelled += 1
        current = turn_id or max(self.turn_id_counter, 1)
        self.calls.append(("interrupt_presentation", current))
        self.emit(
            interruption(
                self.session_id,
                current,
                source=source,
                phase=InterruptionPhase.ANSWERING,
                played=0,
                generated=len(REPLY_PCM),
            )
        )

    async def aclose(self) -> None:
        self.closed += 1
        self.calls.append(("aclose", None))


def browser_frame(amplitude: int) -> AudioFrame:
    frame = AudioFrame(format="s16", layout="stereo", samples=960)
    sample = int(amplitude).to_bytes(2, "little", signed=True)
    frame.planes[0].update(sample * 2 * 960)
    frame.sample_rate = 48_000
    return frame


class FakeWebRTCStream:
    def __init__(self) -> None:
        self.incoming = [
            *(browser_frame(MIC_SAMPLE) for _ in range(6)),
            *(browser_frame(0) for _ in range(12)),
        ]
        self.outgoing: list[bytes] = []
        self.agent_states: list[dict[str, str]] = []
        self.authenticated: list[str] = []
        self.clear_calls = 0
        self.close_calls = 0
        self.close_reasons: list[str] = []
        self.telemetry_sent = False

    async def authenticate(self, pin: str) -> None:
        self.authenticated.append(pin)

    async def recv(self) -> AudioFrame:
        await asyncio.sleep(0)
        if self.incoming:
            return self.incoming.pop(0)
        # Let the committed turn and normalized-event renderer settle before
        # simulating the browser's explicit cancellation.
        await asyncio.sleep(0.12)
        raise CallEnded("user_cancel")

    async def send(self, frame: AudioFrame) -> None:
        self.outgoing.append(bytes(frame.planes[0])[: frame.samples * 4])

    def clear_output(self) -> int:
        self.clear_calls += 1
        cleared = len(self.outgoing)
        self.outgoing.clear()
        return cleared

    async def send_agent_state(self, *, state: str, task: str = "", detail: str = "") -> None:
        self.agent_states.append({"state": state, "task": task, "detail": detail})

    async def recv_telemetry(self) -> dict[str, object] | None:
        await asyncio.sleep(0)
        if self.telemetry_sent:
            return None
        self.telemetry_sent = True
        return {
            "stage": "transport_stats",
            "usesRelay": False,
            "currentRttMs": 12,
            "jitterBufferMs": 4,
        }

    async def close(self, *, reason: str = "agent_closed") -> None:
        self.close_calls += 1
        self.close_reasons.append(reason)


class StreamContext(AbstractAsyncContextManager[FakeWebRTCStream]):
    def __init__(self, stream: FakeWebRTCStream) -> None:
        self.stream = stream

    async def __aenter__(self) -> FakeWebRTCStream:
        return self.stream

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.stream.close(reason="user_cancel")


class FakeCallProvider:
    def __init__(self, stream: FakeWebRTCStream) -> None:
        self.stream = stream
        self.contexts: list[CallContext] = []
        self.ttls: list[int] = []

    async def mint(self, context: CallContext, *, ttl_seconds: int = 300):
        self.contexts.append(context)
        self.ttls.append(ttl_seconds)
        return SimpleNamespace(token="opaque-token", join_url=JOIN_URL)

    def connect(self, ticket: Any) -> StreamContext:
        assert ticket.join_url == JOIN_URL
        return StreamContext(self.stream)


@pytest.mark.asyncio
async def test_pi_webrtc_call_crosses_one_public_realtime_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PCM, UI state, output, cancellation, and close stay behind the kernel."""

    config = pi_browser_config()
    kernel = StrictRealtimeSession(config)
    stream = FakeWebRTCStream()
    provider = FakeCallProvider(stream)
    built: list[SleeptalkConfig] = []
    ready: list[str] = []

    def build(config_arg: SleeptalkConfig) -> StrictRealtimeSession:
        built.append(config_arg)
        return kernel

    monkeypatch.setattr(
        web_call,
        "load_runtime_config",
        lambda path, *, surface: config.model_copy(
            update={
                "transport": config.transport.model_copy(update={"type": surface})
            }
        ),
    )
    monkeypatch.setattr(web_call, "build_session", build)
    monkeypatch.setattr(web_call, "P2PCallProvider", lambda _url: provider)

    await web_call.run_profile_web_call(
        config_path="config/profiles/pi-browser.toml",
        rendezvous_url="https://calls.example.test",
        context=CallContext("pi-browser", "", "browser-user"),
        ttl_seconds=300,
        on_ready=ready.append,
    )

    assert len(built) == 1
    assert built[0].transport.type == "web"
    assert built[0].agent == config.agent
    assert config.agent.type == "pi"
    assert ready == [JOIN_URL]
    assert stream.authenticated == []
    assert provider.contexts[0].agent_instance == "pi-browser"
    assert kernel.prepared == 1
    assert kernel.appended_pcm and all(kernel.appended_pcm)
    assert kernel.turn_pcm and all(kernel.turn_pcm)
    assert any(name == "subscribe" for name, _value in kernel.calls)
    assert any(name == "set_audio_output" for name, _value in kernel.calls)
    assert stream.outgoing, "kernel AudioOutput PCM must reach the WebRTC stream"

    states = {item["state"] for item in stream.agent_states}
    assert {"working", "answering", "idle"} <= states
    assert any(item["task"] == "browser-search" for item in stream.agent_states)
    assert kernel.cancelled == 1
    assert kernel.closed == 1
    assert stream.close_calls == 1


def _module_call_and_import_names(module: object) -> tuple[set[str], set[str]]:
    tree = ast.parse(inspect.getsource(module))
    called_names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name):
            called_names.add(node.func.id)
        elif isinstance(node.func, ast.Attribute):
            called_names.add(node.func.attr)
    imports = {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    return called_names, imports


def test_browser_source_cannot_revive_legacy_turn_ownership() -> None:
    """Static tripwire supplements the runtime spy for unreachable old paths."""

    from sleeptalk.web_voice import session as web_voice_session

    entry_calls, entry_imports = _module_call_and_import_names(web_call)
    session_calls, session_imports = _module_call_and_import_names(web_voice_session)
    called_names = entry_calls | session_calls
    imports = entry_imports | session_imports

    assert "stream_and_speak" not in called_names
    assert "transcribe_discord_utterance" not in called_names
    assert "respond_streaming" not in called_names
    assert "synthesize" not in called_names
    assert "app.calls.runner" not in imports

    required_kernel_calls = {"append_audio", "run_turn", "subscribe", "cancel", "aclose"}
    assert required_kernel_calls <= session_calls


def test_a_call_id_that_starts_with_a_dash_still_parses():
    """Generated IDs are URL-safe base64 and can begin with "-"; the child
    command passes every value as --flag=value so argparse never misreads one."""
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--call-id", required=True)
    assert parser.parse_args(["--call-id=-abc123"]).call_id == "-abc123"
    source = (Path(__file__).resolve().parents[2] / "sleeptalk" / "detached_call.py").read_text()
    assert 'f"--{name}={value}"' in source
