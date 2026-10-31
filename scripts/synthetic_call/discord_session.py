"""A Discord voice call without Discord: the real live session, sink,
endpointing, barge-in and cascade route, fed the caller's clips as the voice
receive path would and played into a fake voice client that records what
would have been sent.

    uv run python scripts/synthetic_call/discord_session.py <run_dir> [--scenarios a,b] [--fault-proxy URL]

Speech is OpenRouter through the saved key named by SYNTH_OPENROUTER_PROFILE
(qa_env.py); the agent is fault_agent.py, which answers the scenario
questions from canned replies (or fails on request). SYNTH_CLIPS points at a
make_clips.py folder. A manual tool: real speech, a few cents per run.

What is faked, and only that: Discord's decrypt and Opus decode (frames
arrive as PCM), the voice client's player (a thread reads the audio source
every 20 ms, like discord.py's), and the Discord user. Everything between is
the code a live Discord call runs.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import threading
import time
import wave
from array import array
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from qa_env import openrouter_ref  # noqa: E402

FRAME_MS = 20
FRAME_BYTES = 48 * FRAME_MS * 2 * 2  # 48 kHz stereo s16
LOUD_RMS = 300


def _rms(pcm: bytes) -> float:
    samples = array("h", pcm[: len(pcm) - len(pcm) % 2])
    return (sum(s * s for s in samples) / len(samples)) ** 0.5 if samples else 0.0


class FakeVoice:
    """The voice client: plays a source on a thread, 20 ms per read."""

    def __init__(self, clock) -> None:
        self.clock = clock
        # The profile's server (guild_id = "1"), as access checks expect.
        self.channel = SimpleNamespace(id=1, members=[], guild=SimpleNamespace(id=1))
        self.frames: list[tuple[float, float]] = []  # (t, rms); opus frames count as loud
        self.stops: list[float] = []
        self.pauses: list[float] = []
        self._source = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._paused = threading.Event()
        self._listening = False
        self.sink = None

    # Receive side
    def listen(self, sink, after=None) -> None:
        self.sink = sink
        self._listening = True

    def is_listening(self) -> bool:
        return self._listening

    def stop_listening(self) -> None:
        self._listening = False

    def is_connected(self) -> bool:
        return True

    # Send side
    def play(self, source, *, after=None) -> None:
        self._stop.clear()
        self._paused.clear()
        self._source = source

        def run() -> None:
            error = None
            try:
                next_at = time.monotonic()
                while not self._stop.is_set():
                    if self._paused.is_set():
                        time.sleep(FRAME_MS / 1000)
                        next_at = time.monotonic()
                        continue
                    data = source.read()
                    if not data:
                        break
                    opus = getattr(source, "is_opus", lambda: False)()
                    self.frames.append((self.clock(), 10_000.0 if opus else _rms(data)))
                    next_at += FRAME_MS / 1000
                    time.sleep(max(0.0, next_at - time.monotonic()))
            except Exception as exc:  # noqa: BLE001 - reported through after, as discord.py does
                error = exc
            finally:
                self._source = None
                if after is not None:
                    after(error)

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()

    def is_playing(self) -> bool:
        return self._source is not None and not self._paused.is_set()

    def is_paused(self) -> bool:
        return self._source is not None and self._paused.is_set()

    def pause(self) -> None:
        self.pauses.append(self.clock())
        self._paused.set()

    def resume(self) -> None:
        self._paused.clear()

    def stop(self) -> None:
        if self._source is not None:
            self.stops.append(self.clock())
        self._stop.set()


class PassthroughDecoder:
    def decode(self, data: bytes, fec: bool = False) -> bytes:  # noqa: ARG002 - Opus decoder shape
        return data


def _clip_frames(path: Path) -> list[bytes]:
    with wave.open(str(path), "rb") as wav:
        assert wav.getframerate() == 48_000 and wav.getsampwidth() == 2, path
        mono = array("h", wav.readframes(wav.getnframes()))
        if wav.getnchannels() == 2:
            mono = mono[::2]
    stereo = array("h", [0]) * (len(mono) * 2)
    stereo[0::2] = mono
    stereo[1::2] = mono
    raw = stereo.tobytes()
    raw += b"\0" * (-len(raw) % FRAME_BYTES)
    return [raw[i:i + FRAME_BYTES] for i in range(0, len(raw), FRAME_BYTES)]


def _profile(run: Path, workspace: Path, control: Path, fault_proxy: str) -> Path:
    from sleeptalk.config import packaged_example_root
    from sleeptalk.speech_choice import for_new_profile

    home = run / "home"
    (home / "profiles").mkdir(parents=True, exist_ok=True)
    root = home / "sleeptalk.toml"
    if not root.exists():
        root.write_bytes(packaged_example_root().read_bytes())
    choice = for_new_profile("openrouter", "en-US")
    base = fault_proxy.rstrip("/")
    stt_base = f'base_url = "{base}/stt"\n' if base else ""
    tts_base = f'base_url = "{base}/tts"\n' if base else ""
    command = [sys.executable, str(HERE / "fault_agent.py"), str(control)]
    text = f'''[agent]
type = "command"
name = "synthetic-qa"
slug = "synthetic-qa"
workspace_dir = {json.dumps(str(workspace))}

[agent.options]
command = {json.dumps(command)}

[route]
type = "cascade"

[transport]
type = "discord"
guild_id = "1"
client_id = "1"
authorization_required = false

[policy]
narration_style = "keyboard"

[stt]
provider = "openrouter"
model = "{choice.stt_model}"
language = "en-US"
{stt_base}
[tts]
provider = "openrouter"
model = "{choice.tts_model}"
voice_id = "eve"
language = "en-US"
{tts_base}
[log]
latency = true

[credentials.refs]
OPENROUTER_API_KEY = {json.dumps(openrouter_ref())}
'''
    import tomllib

    from sleeptalk.config import AgentConfig
    from sleeptalk.runtime_binding import bind, toml

    # The agent's runtime binding, as setup writes it (start_call.py does the same).
    agent = AgentConfig.model_validate(tomllib.loads(text)["agent"])
    text = text.replace("\n[credentials.refs]", toml(bind(agent)) + "\n[credentials.refs]")
    path = home / "profiles" / "synthetic-qa.toml"
    path.write_text(text)
    path.chmod(0o600)
    return path


class Call:
    def __init__(self, run: Path, clips: Path, control: Path, fault_proxy: str) -> None:
        self.run = run
        self.clips = clips
        self.control = control
        self.fault_proxy = fault_proxy
        self.started = time.monotonic()
        self.events: list[dict] = []
        self.user = SimpleNamespace(id=4242, bot=False, name="caller", display_name="caller",
                                    mention="<@4242>")

    def clock(self) -> float:
        return round(time.monotonic() - self.started, 3)

    def note(self, kind: str, **fields) -> None:
        row = {"t": self.clock(), "e": kind, **fields}
        self.events.append(row)
        print(json.dumps(row), flush=True)

    async def open(self) -> None:
        workspace = self.run / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        os.environ["SLEEPTALK_HOME"] = str(self.run / "home")
        profile = _profile(self.run, workspace, self.control, self.fault_proxy)
        from sleeptalk.builder import build_session
        from sleeptalk.config import load_resolved_config
        from sleeptalk.discord import DiscordEnv, PiDiscordBot
        from sleeptalk.discord_voice.live_session import PiDiscordLiveSession
        from sleeptalk.session_events import SessionEventType

        # A placeholder token: nothing here talks to Discord.
        os.environ.setdefault("DISCORD_BOT_TOKEN", "synthetic-not-a-token")
        # Root sleeptalk.toml plus the profile, as a real call composes them.
        config = load_resolved_config(profile, check_binaries=False)
        from sleeptalk.logging_setup import setup_profile_logging

        setup_profile_logging(profile, config)  # home/logs: the run's log and latency traces
        env = {**os.environ, config.transport.token_env: "synthetic-not-a-token"}
        session = build_session(config, env=env)
        self.bot = PiDiscordBot(config, DiscordEnv(token="synthetic-not-a-token"),
                                policy=session.policy, realtime_session=session,
                                stt_rest_timeout_seconds=getattr(config.stt, "rest_timeout_seconds", None))

        def on_event(event) -> None:
            if event.type is SessionEventType.TRANSCRIPT_FINAL:
                self.note("transcript", speaker=str(event.payload.get("speaker")),
                          text=str(event.payload.get("text", ""))[:200])
            elif event.type in {SessionEventType.RESPONSE_CANCELLED, SessionEventType.INTERRUPTION}:
                self.note(event.type.value)

        self.bot.realtime_session.subscribe(on_event)
        self.voice = FakeVoice(self.clock)
        self.live = PiDiscordLiveSession(self.bot, self.voice, None)
        self.live.start()
        sink = self.voice.sink
        sink._dave_decrypt = lambda _user_id, frame: frame
        sink.decoders = _Decoders()
        await asyncio.sleep(3)  # kernel prepare (agent start, speech warmup)

    async def say(self, clip: str, *, pause_after_ms: int = 0) -> None:
        frames = _clip_frames(self.clips / f"{clip}.wav")
        self.note("user_start", clip=clip)
        next_at = time.monotonic()
        for frame in frames:
            self.voice.sink._process_opus(self.user, frame)
            next_at += FRAME_MS / 1000
            await asyncio.sleep(max(0.0, next_at - time.monotonic()))
        # A Discord client sends a few silent frames, then stops sending.
        for _ in range(5):
            self.voice.sink._process_opus(self.user, b"\0" * FRAME_BYTES)
            await asyncio.sleep(FRAME_MS / 1000)
        self.note("user_end", clip=clip)
        member = SimpleNamespace(id=self.user.id)
        self.voice.sink.on_voice_member_speaking_stop(member)
        if pause_after_ms:
            await asyncio.sleep(pause_after_ms / 1000)

    def loud_since(self, t: float) -> list[float]:
        return [ft for ft, level in list(self.voice.frames) if ft >= t and level >= LOUD_RMS]

    async def wait_audio(self, after: float, timeout: float = 60.0) -> float | None:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            loud = self.loud_since(after)
            if loud:
                return loud[0]
            await asyncio.sleep(0.05)
        return None

    async def wait_quiet(self, quiet: float = 2.5, timeout: float = 150.0) -> None:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if (not self.loud_since(self.clock() - quiet) and not self.live.turn_lock.locked()
                    and not self.voice.sink.buffers):
                return
            await asyncio.sleep(0.1)

    async def wait_said(self, after: float, word: str, timeout: float = 40.0) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if any(word in e["text"].lower() for e in _since(self, after, "transcript", "assistant")):
                return True
            await asyncio.sleep(0.1)
        return False

    def set_agent(self, mode: str) -> None:
        state = {"stt": "pass", "tts": "pass", "agent": mode}
        self.control.write_text(json.dumps(state))

    async def close(self) -> None:
        self.live.stop()
        await asyncio.sleep(1)
        with contextlib_suppress():
            await self.bot.realtime_session.close()


class _Decoders(dict):
    def setdefault(self, key, default=None):  # noqa: ARG002 - the sink makes an Opus Decoder here
        return super().setdefault(key, PassthroughDecoder())


class contextlib_suppress:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return True


def _since(call: Call, t: float, kind: str, speaker: str | None = None) -> list[dict]:
    return [e for e in call.events if e["t"] >= t and e["e"] == kind
            and (speaker is None or e.get("speaker", "").endswith(speaker))]


async def scenario(call: Call, name: str, proxy: str) -> dict:
    t0 = call.clock()
    call.set_agent("ok")
    result: dict = {"name": name}
    if name == "first_words":
        await call.say("first_words")
        stop = call.clock()
        first = await call.wait_audio(stop)
        await call.wait_quiet()
        said = " ".join(e["text"] for e in _since(call, t0, "transcript", "assistant")).lower()
        result.update(heard=first and first - stop, ok=first is not None and "banana" in said)
    elif name == "short":
        await call.say("short")
        stop = call.clock()
        first = await call.wait_audio(stop)
        await call.wait_quiet()
        said = " ".join(e["text"] for e in _since(call, t0, "transcript", "assistant")).lower()
        result.update(heard=first and first - stop, ok=first is not None and "four" in said)
    elif name == "long_silent":
        await call.say("long")
        stop = call.clock()
        first = await call.wait_audio(stop)
        await call.wait_quiet(quiet=4)
        loud = call.loud_since(stop)
        span = (loud[-1] - loud[0]) if loud else 0
        result.update(heard=first and first - stop, answerSeconds=round(span, 1),
                      ok=first is not None and span > 12 and not _since(call, t0, "response.cancelled"))
    elif name == "barge":
        await call.say("long")
        stop = call.clock()
        first = await call.wait_audio(stop)
        await asyncio.sleep(4)
        barge_at = call.clock()
        await call.say("barge")
        await call.wait_said(barge_at, "paris")
        await call.wait_quiet()
        stops = sorted(s for s in call.voice.stops + call.voice.pauses if s >= barge_at)
        said = " ".join(e["text"] for e in _since(call, barge_at, "transcript", "assistant")).lower()
        result.update(heard=first and first - stop, stoppedMs=round((stops[0] - barge_at) * 1000) if stops else None,
                      ok=bool(stops) and "paris" in said)
    elif name == "resume":
        await call.say("resume")
        stop = call.clock()
        first = await call.wait_audio(stop)
        await call.wait_quiet(quiet=4)
        result.update(heard=first and first - stop, ok=first is not None)
    elif name == "split":
        await call.say("split_a", pause_after_ms=700)
        await call.say("split_b")
        stop = call.clock()
        first = await call.wait_audio(stop)
        await call.wait_quiet()
        users = [e["text"] for e in _since(call, t0, "transcript", "user")]
        said = " ".join(e["text"] for e in _since(call, t0, "transcript", "assistant")).lower()
        result.update(heard=first and first - stop, userTurns=users,
                      ok=len(users) == 1 and "tea" in said)
    elif name == "backchannel":
        await call.say("long")
        stop = call.clock()
        first = await call.wait_audio(stop)
        await asyncio.sleep(4)
        mid = call.clock()
        await call.say("backchannel")
        await call.wait_quiet(quiet=4)
        loud = call.loud_since(mid + 3)
        users = [e["text"] for e in _since(call, mid, "transcript", "user")]
        result.update(heard=first and first - stop, userTurns=users,
                      ok=bool(loud) and len(loud) > 100 and not users)
    elif name.startswith("agent_"):
        call.set_agent(name.removeprefix("agent_"))
        await call.say("short")
        stop = call.clock()
        first = await call.wait_audio(stop, timeout=40)
        await call.wait_quiet()
        result.update(heard=first and first - stop, ok=first is not None)
    elif name.startswith("tts_"):
        if not proxy:
            return {"name": name, "skipped": "needs --fault-proxy"}
        import urllib.request

        fault = name.removeprefix("tts_")
        request = urllib.request.Request(f"{proxy}/_fault", data=json.dumps({"tts": fault}).encode(), method="POST")
        urllib.request.urlopen(request).read()
        await call.say("short")
        stop = call.clock()
        first = await call.wait_audio(stop, timeout=40)
        await call.wait_quiet()
        urllib.request.urlopen(urllib.request.Request(
            f"{proxy}/_fault", data=json.dumps({"tts": "pass"}).encode(), method="POST")).read()
        result.update(heard=first and first - stop, ok=first is not None)
    result["heard"] = round(result["heard"], 2) if isinstance(result.get("heard"), float) else result.get("heard")
    call.note("scenario", **result)
    await asyncio.sleep(2)
    return result


DEFAULT = "first_words,short,long_silent,barge,resume,split,backchannel,agent_error,agent_crash,agent_empty"


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir")
    parser.add_argument("--scenarios", default=DEFAULT)
    parser.add_argument("--fault-proxy", default="", help="fault_proxy.py's address, for the tts_* scenarios")
    args = parser.parse_args()
    clips = Path(os.environ.get("SYNTH_CLIPS", ""))
    if not (clips / "manifest.json").is_file():
        raise SystemExit("Set SYNTH_CLIPS to a folder make_clips.py wrote.")
    run = Path(args.run_dir).resolve()
    run.mkdir(parents=True, exist_ok=True)
    control = run / "control.json"
    call = Call(run, clips, control, args.fault_proxy)
    call.set_agent("ok")
    await call.open()
    results = []
    try:
        for name in args.scenarios.split(","):
            results.append(await scenario(call, name.strip(), args.fault_proxy))
    finally:
        await call.close()
        (run / "events.json").write_text(json.dumps(call.events, indent=1))
    print()
    for row in results:
        flag = "--  " if "skipped" in row else ("ok  " if row.get("ok") else "FAIL")
        extra = {k: v for k, v in row.items() if k not in {"name", "ok"}}
        print(f"{flag} {row['name']:14} {json.dumps(extra)}")
    failed = sum(1 for row in results if "skipped" not in row and not row.get("ok"))
    print(f"SUMMARY discord {len(results) - failed}/{len(results)} ok")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
