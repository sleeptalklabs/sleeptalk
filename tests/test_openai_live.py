"""Live protocol, browser duplex delivery, and independent backend lifetime."""
from __future__ import annotations

import asyncio
import base64
import json
import struct
from types import SimpleNamespace

import aiohttp
import pytest
from av import AudioFrame

from sleeptalk.config import SleeptalkConfig
from sleeptalk.openai_live import OpenAILiveClient, OpenAILiveError
from sleeptalk.openai_realtime import OpenAIRealtimeConfig
from sleeptalk.policy import VoicePolicy
from sleeptalk.presentation import PresentationState, reduce
from sleeptalk.routes.live import LiveRoute
from sleeptalk.session import RealtimeSession
from sleeptalk.session_events import SessionEventType
from sleeptalk.web_voice.session import WebCallSession


class Socket:
    def __init__(self, *, confirm=True, finalize=True):
        self.queue = asyncio.Queue()
        self.sent = []
        self.confirm, self.finalize = confirm, finalize
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        event = await self.queue.get()
        if event is None:
            raise StopAsyncIteration
        return SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=json.dumps(event))

    async def send_json(self, event):
        self.sent.append(event)
        if event["type"] == "session.start" and self.confirm:
            self.queue.put_nowait({"type": "session.started", "session": {"id": "live_test", "model": "gpt-live-1"}})
        if event["type"] == "session.close" and self.finalize:
            self.queue.put_nowait({"type": "session.closed", "usage": {"seconds": 1}})

    async def close(self):
        self.closed = True
        self.queue.put_nowait(None)


class HTTP:
    def __init__(self, socket):
        self.socket = socket
        self.closed = False
        self.url = ""

    async def ws_connect(self, url, **kwargs):
        self.url, self.options = url, kwargs
        return self.socket

    async def close(self):
        self.closed = True


def client(socket=None, **kwargs):
    socket = socket or Socket()
    http = HTTP(socket)
    return OpenAILiveClient(OpenAIRealtimeConfig(api_key="fake", model="gpt-live-1"),
                            instructions="Test", http_session_factory=lambda: http,
                            timeout=0.1, **kwargs), socket, http


async def eventually(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.01)


async def test_protocol_streams_without_realtime_turn_events_and_finalizes():
    live, ws, http = client()
    await live.connect()
    for _ in range(3):
        await live.append_audio(struct.pack("<960h", *([2000] * 960)))
    assert http.url == "wss://api.openai.com/v1/live/sessions"
    assert http.options["headers"]["Authorization"] == "Bearer fake"
    start = ws.sent[0]["session"]
    assert start["model"] == "gpt-live-1" and start["delegation"] == {"type": "client"}
    assert start["store"] is False
    assert [x["type"] for x in ws.sent] == ["session.start"] + ["session.input_audio.append"] * 3
    # Stateful 48 -> 24 kHz resampling preserves duration across frames.
    assert sum(len(base64.b64decode(x["audio"])) for x in ws.sent[1:]) == 2880
    await live.close()
    assert live.finalized and live.usage == {"seconds": 1}
    assert ws.closed and http.closed


async def test_start_timeout_and_provider_error_release_resources():
    live, ws, http = client(Socket(confirm=False))
    with pytest.raises(OpenAILiveError, match="TimeoutError"):
        await live.connect()
    assert ws.closed and http.closed
    live, ws, http = client()
    await live.connect()
    ws.queue.put_nowait({"type": "error", "error": {"code": "bad_model", "message": "secret-body"}})
    await eventually(lambda: live._failure is not None)
    with pytest.raises(OpenAILiveError, match="bad_model") as exc:
        await live.append_audio(b"\0\0")
    assert "secret-body" not in str(exc.value)
    await live.close()
    assert http.closed


async def test_abrupt_disconnect_wakes_consumer_and_incomplete_close_is_visible():
    live, ws, http = client()
    await live.connect()
    await live.next_event()
    ws.queue.put_nowait(None)
    with pytest.raises(OpenAILiveError, match="before final usage"):
        await live.next_event()
    await live.close()
    assert not live.finalized
    live, ws, http = client(Socket(finalize=False))
    await live.connect()
    await live.close()
    assert not live.finalized and http.closed


class Agent:
    def __init__(self):
        self.prompts = []
        self.release = asyncio.Event()
        self.aborts = self.closes = 0

    async def respond_streaming(self, prompt, *, on_event=None):
        self.prompts.append(prompt)
        await self.release.wait()
        yield "The verified result is 42."

    async def abort(self):
        self.aborts += 1

    async def close(self):
        self.closes += 1


class Stream:
    def __init__(self):
        self.audio = []
        self.states = []
        self.presentations = []
        self.clears = 0

    async def send(self, frame):
        self.audio.append(bytes(frame.planes[0])[:frame.samples * 4])
        await asyncio.sleep(0)

    def clear_output(self):
        self.clears += 1
        return 0

    async def send_agent_state(self, **state):
        self.states.append(state)

    async def send_presentation(self, payload):
        self.presentations.append(payload)

    async def close(self):
        pass


def frame(value=1000):
    audio = AudioFrame(format="s16", layout="stereo", samples=960)
    audio.sample_rate = 48000
    audio.planes[0].update(struct.pack("<1920h", *([value] * 1920)))
    return audio


def browser(*, job_agent_factory=None, max_jobs=3):
    live, ws, http = client()
    agent = Agent()
    route = LiveRoute(config=live.config, agent=agent, client=live)
    config = SleeptalkConfig.model_validate({"transport": {"type": "web"},
                                            "route": {"type": "hybrid", "model": "gpt-live-1"},
                                            "policy": {"max_background_jobs": max_jobs}})
    policy = VoicePolicy.from_config(config.policy)
    policy.max_background_jobs = max_jobs
    session = RealtimeSession(SimpleNamespace(route=route, config=config, agent=agent,
        policy=policy, usage_meter=None, job_agent_factory=job_agent_factory))
    stream = Stream()
    web = WebCallSession(session=session, stream=stream, call_sid="test")
    return web, session, route, live, ws, agent, stream


def audio_event(value=2000):
    return {"type": "session.output_audio.delta", "delta": base64.b64encode(
        struct.pack("<2400h", *([value] * 2400))).decode()}


def caption(text, *, start=0, end=1000, speaker="input"):
    return {"type": f"session.{speaker}_transcript.delta", "delta": text,
            "start_ms": start, "end_ms": end}


async def test_browser_sends_during_playback_and_bypasses_local_turn_detection():
    web, session, route, live, ws, agent, stream = browser()
    try:
        await session.prepare()
        assert not ws.sent  # A waiting call link does not start a paid session.
        await web.feed_audio_frame(frame())
        ws.queue.put_nowait(audio_event())
        await eventually(lambda: len(stream.audio) > 0)
        # Even an active turn and active playback must not gate Live input.
        web._turn_in_flight = True
        web.playback.active = True
        web.assembler.observe = lambda *args: pytest.fail("Local endpointing must be bypassed")
        async def no_local_barge(*args, **kwargs):
            pytest.fail("Local barge cancellation must be bypassed")
        web._watch_web_barge_in = no_local_barge
        before = len(ws.sent)
        for _ in range(5):
            await web.feed_audio_frame(frame(0))
        await eventually(lambda: len(ws.sent) >= before + 5)
        assert [x["type"] for x in ws.sent[before:]] == ["session.input_audio.append"] * 5
    finally:
        await web.close()
    assert agent.closes == 1 and live.finalized


async def test_interleaved_captions_keep_both_speakers_and_original_fragments():
    web, session, route, live, ws, agent, stream = browser()
    try:
        await web.feed_audio_frame(frame())
        for event in [caption("Please"), caption("Sure", speaker="output"),
                      caption(" check that", start=1000, end=1600)]:
            ws.queue.put_nowait(event)
        await eventually(lambda: len(route._history) == 3)
        state = PresentationState()
        for event in session.events:
            state = reduce(state, event)
        assert [(x.kind, x.text) for x in state.lines] == [("user", "Please check that"), ("assistant", "Sure")]
        events = [x for x in session.events if x.type is SessionEventType.TRANSCRIPT_PARTIAL]
        assert events[-1].payload["raw"]["delta"] == " check that"
        assert events[-1].payload["raw"]["start_ms"] == 1000
    finally:
        await web.close()


async def test_delegation_waits_for_transcript_deduplicates_and_does_not_block_audio():
    web, session, route, live, ws, agent, stream = browser()
    try:
        await web.feed_audio_frame(frame())
        event = {"type": "session.delegation.created", "offset_ms": 1000,
                 "delegation": {"id": "task-1", "target": "client"}}
        ws.queue.put_nowait(event)
        ws.queue.put_nowait(event)
        await asyncio.sleep(0.05)
        assert not agent.prompts
        ws.queue.put_nowait(caption("Check my code"))
        await eventually(lambda: len(agent.prompts) == 1)
        await eventually(lambda: stream.states and stream.states[-1]["state"] == "working")
        assert "Check my code" in agent.prompts[0]
        await web.feed_audio_frame(frame())
        ws.queue.put_nowait(audio_event())
        await eventually(lambda: bool(stream.audio))
        await web.interrupt_presentation()
        assert agent.aborts == 0  # Speech stop is not task cancellation.
        await eventually(lambda: stream.states[-1]["state"] == "working")
        agent.release.set()
        await eventually(lambda: any(x["type"] == "session.commentary.append" for x in ws.sent))
        assert next(x for x in ws.sent if x["type"] == "session.commentary.append")["delegation_id"] == "task-1"
    finally:
        await web.close()


async def test_explicit_stop_drops_late_audio_and_close_cancels_backend():
    web, session, route, live, ws, agent, stream = browser()
    try:
        await web.feed_audio_frame(frame())
        ws.queue.put_nowait(caption("Check my code"))
        ws.queue.put_nowait({"type": "session.delegation.created", "offset_ms": 1000,
                            "delegation": {"id": "task-1"}})
        await eventually(lambda: bool(agent.prompts))
        ws.queue.put_nowait(audio_event())
        await eventually(lambda: bool(stream.audio))
        await web.interrupt_presentation()
        await asyncio.sleep(0.02)
        count = len(stream.audio)
        ws.queue.put_nowait(audio_event())
        await asyncio.sleep(0.05)
        assert len(stream.audio) == count
        for _ in range(4):
            ws.queue.put_nowait(audio_event(0))
        ws.queue.put_nowait(audio_event())
        await eventually(lambda: len(stream.audio) > count)
    finally:
        await web.close()
    assert agent.aborts == 1 and agent.closes == 1
    assert not route._tasks


async def test_bad_audio_fails_visibly_and_stops_output():
    web, session, route, live, ws, agent, stream = browser()
    try:
        await web.feed_audio_frame(frame())
        ws.queue.put_nowait({"type": "session.output_audio.delta", "delta": "!!!"})
        await eventually(lambda: route._failure is not None)
        await eventually(lambda: live.finalized)
        assert any(x.type is SessionEventType.ERROR and x.payload["fatal"] for x in session.events)
        with pytest.raises(OpenAILiveError):
            await web.feed_audio_frame(frame())
    finally:
        await web.close()


async def test_late_caption_updates_original_segment_in_timestamp_order():
    web, session, route, live, ws, agent, stream = browser()
    try:
        await web.feed_audio_frame(frame())
        for event in [caption("check", start=500, end=900),
                      caption("Another request", start=5000, end=6000),
                      caption("Please ", start=0, end=500)]:
            ws.queue.put_nowait(event)
        await eventually(lambda: len(route._history) == 3)
        state = PresentationState()
        for event in session.events:
            state = reduce(state, event)
        assert [line.text for line in state.lines] == ["Please check", "Another request"]
    finally:
        await web.close()


async def test_late_transcript_before_permission_prompt_is_not_approval():
    from sleeptalk.adapter import PermissionRequested, PermissionDecision
    web, session, route, live, ws, agent, stream = browser()
    try:
        for _ in range(100):
            await web.feed_audio_frame(frame(0))
        await eventually(lambda: route._input_samples >= 96000)
        event = PermissionRequested("p1", "May I write?", frozenset({PermissionDecision.ALLOW_ONCE}))
        route._backend_event("task", route._backend_revision, event)
        assert route._pending_after_ms >= 2000
        # Caption delivery lag must not turn speech from before the prompt into consent.
        await route._answer_pending("allow once", [{"start_ms": 1500}], "reply")
        assert route._pending is event
    finally:
        await web.close()


async def test_input_clock_fills_webrtc_dtx_silence_without_new_browser_frames():
    web, session, route, live, ws, agent, stream = browser()
    try:
        await web.feed_audio_frame(frame())
        await eventually(lambda: len([e for e in ws.sent if e["type"] == "session.input_audio.append"]) >= 5)
        audio = [base64.b64decode(e["audio"]) for e in ws.sent if e["type"] == "session.input_audio.append"]
        assert any(audio[0])
        assert not any(audio[-1])
        assert len(audio[-1]) == 960  # 20 ms at 24 kHz PCM16
    finally:
        await web.close()


async def test_specialist_send_failure_closes_without_self_cancellation_deadlock():
    web, session, route, live, ws, agent, stream = browser()
    original = ws.send_json
    async def send(event):
        if event["type"] == "session.commentary.append":
            raise ConnectionError("disconnected")
        await original(event)
    ws.send_json = send
    try:
        await web.feed_audio_frame(frame())
        ws.queue.put_nowait(caption("Check my code"))
        ws.queue.put_nowait({"type": "session.delegation.created", "offset_ms": 1000,
                            "delegation": {"id": "task"}})
        await eventually(lambda: bool(agent.prompts))
        agent.release.set()
        await eventually(lambda: live.finalized)
        assert route._failure and not route.active_task_id
        assert not any(e.payload.get("name") == "hybrid.task.completed" for e in session.events)
    finally:
        await web.close()


async def test_live_session_meter_keeps_final_seconds_and_incomplete_usage_distinct(tmp_path):
    from sleeptalk.metering import UsageMeter
    for finalize in (True, False):
        web, session, route, live, ws, agent, stream = browser()
        ws.finalize = finalize
        path = tmp_path / f"{finalize}.jsonl"
        meter = route._usage_meter = UsageMeter(log_path=path)
        await web.feed_audio_frame(frame())
        await web.close()
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        assert rows[-1]["status"] == ("completed" if finalize else "incomplete")
        assert rows[-1]["finalized"] is finalize
        assert meter.snapshot()["totals"] == ({"liveSessionSeconds": 1} if finalize else {})


async def test_cancelled_close_caller_does_not_discard_final_usage():
    live, ws, http = client(Socket(finalize=False))
    await live.connect()
    closing = asyncio.create_task(live.close())
    await eventually(lambda: any(e["type"] == "session.close" for e in ws.sent))
    closing.cancel()
    await asyncio.gather(closing, return_exceptions=True)
    ws.queue.put_nowait({"type": "session.closed", "usage": {"seconds": 2}})
    await live.close()
    assert live.finalized and live.usage == {"seconds": 2} and http.closed
    assert sum(e["type"] == "session.close" for e in ws.sent) == 1


async def test_close_drains_pending_context_errors_before_final_receipt():
    live, ws, http = client(Socket(finalize=False))
    await live.connect()
    closing = asyncio.create_task(live.close())
    await eventually(lambda: any(e["type"] == "session.close" for e in ws.sent))
    ws.queue.put_nowait({"type": "error", "error": {"code": "context_injection_incomplete"}})
    ws.queue.put_nowait({"type": "session.closed", "usage": {"seconds": 3}})
    await closing
    assert live.finalized and live.usage == {"seconds": 3} and not live._failure


@pytest.mark.parametrize("words", ["Cancel the task", "Cancel it", "Please cancel it.", "Cancel that"])
async def test_explicit_cancel_while_permission_pending_does_not_grant_it(words):
    from sleeptalk.adapter import PermissionRequested, PermissionDecision
    web, session, route, live, ws, agent, stream = browser()
    try:
        await web.feed_audio_frame(frame())
        ws.queue.put_nowait(caption("Check my code"))
        ws.queue.put_nowait({"type": "session.delegation.created", "offset_ms": 1000,
                            "delegation": {"id": "task"}})
        await eventually(lambda: bool(agent.prompts))
        route._pending = PermissionRequested("p", "Allow?", frozenset({PermissionDecision.ALLOW_ONCE}))
        ws.queue.put_nowait(caption(words, start=2000, end=3000))
        ws.queue.put_nowait({"type": "session.delegation.created", "offset_ms": 3000,
                            "delegation": {"id": "cancel"}})
        await eventually(lambda: agent.aborts == 1)
        assert route._pending is None and route.active_task_id is None
        assert len(agent.prompts) == 1
    finally:
        await web.close()


def test_builder_selects_live_only_for_exact_model():
    from sleeptalk.builder import build_session
    config = SleeptalkConfig.model_validate({"agent": {"type": "pi"}, "transport": {"type": "null"},
                                           "route": {"type": "hybrid", "model": "gpt-live-1"}})
    session = build_session(config, env={"OPENAI_API_KEY": "test"})
    assert isinstance(session.route, LiveRoute) and session.continuous_input()
    assert callable(session._built.job_agent_factory)


def test_settings_exposes_live_and_saves_shared_model(tmp_path):
    from sleeptalk.settings_page import _realtime_card, apply_setting
    path = tmp_path / "test.toml"
    path.write_text('[route]\ntype="hybrid"\nmodel="gpt-realtime-2.1"\n')
    card = _realtime_card({}, {}, "en-US")
    assert "gpt-live-1" in [x["id"] for x in card["models"]["tts"]["models"]]
    apply_setting(path, "tts.model", "gpt-live-1")
    assert 'model = "gpt-live-1"' in path.read_text()
    with pytest.raises(ValueError):
        apply_setting(path, "tts.model", "made-up")


async def test_correction_settles_old_task_before_replacement_and_hides_old_draft():
    web, session, route, live, ws, agent, stream = browser()
    cancelled = asyncio.Event()
    async def answer(prompt, *, on_event=None):
        agent.prompts.append(prompt)
        if len(agent.prompts) == 1:
            yield "Old unverified draft"
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        else:
            assert cancelled.is_set()
            yield "Corrected verified result"
    agent.respond_streaming = answer
    try:
        await web.feed_audio_frame(frame())
        ws.queue.put_nowait(caption("Check my code"))
        ws.queue.put_nowait({"type": "session.delegation.created", "offset_ms": 1000,
                            "delegation": {"id": "old"}})
        await eventually(lambda: len(agent.prompts) == 1)
        assert not any(x["type"] == "session.commentary.append" for x in ws.sent)
        ws.queue.put_nowait(caption("Actually check the tests instead", start=2000, end=3000))
        ws.queue.put_nowait({"type": "session.delegation.created", "offset_ms": 3000,
                            "delegation": {"id": "corrected"}})
        await eventually(lambda: any(x["type"] == "session.commentary.append" for x in ws.sent))
        updates = [x for x in ws.sent if x["type"] == "session.commentary.append"]
        assert all(x["delegation_id"] == "corrected" for x in updates)
        assert "Old unverified" not in str(updates)
        assert "Check my code" in agent.prompts[1]
        assert "Reconcile any actions already performed" in agent.prompts[1]
        assert agent.aborts == 1
    finally:
        await web.close()


async def test_permissions_require_explicit_new_reply_and_preserve_agent_control():
    from sleeptalk.adapter import PermissionRequested, PermissionDecision, ControlAccepted
    web, session, route, live, ws, agent, stream = browser()
    controls = []
    async def control(command):
        controls.append(command)
        return ControlAccepted()
    agent.control = control
    try:
        await web.feed_audio_frame(frame())
        route._pending = PermissionRequested("permission-1", "May I write the file?", frozenset({PermissionDecision.ALLOW_ONCE, PermissionDecision.DENY}))
        route._pending_after_ms = 2000
        await route._answer_pending("yes", [{"start_ms": 1000}], "delegation-test")
        assert not controls
        await route._answer_pending("yes but wait", [{"start_ms": 2500}], "delegation-test")
        assert not controls
        await route._answer_pending("allow once", [{"start_ms": 3000}], "delegation-test")
        assert controls[0].request_id == "permission-1"
        assert controls[0].decision is PermissionDecision.ALLOW_ONCE
        assert route._pending is None
    finally:
        await web.close()


async def test_unknown_or_cancelled_delegation_cannot_revive_after_close():
    web, session, route, live, ws, agent, stream = browser()
    await web.feed_audio_frame(frame())
    ws.queue.put_nowait({"type": "session.delegation.created", "offset_ms": 1000,
                        "delegation": {"id": "waiting-for-text"}})
    await asyncio.sleep(0.02)
    await web.close()
    assert not agent.prompts and not route._tasks


async def test_live_key_check_uses_live_protocol_not_realtime_or_tts(tmp_path, monkeypatch):
    from sleeptalk.settings_page import switch_to_realtime
    from sleeptalk.credential_store import CredentialStore
    path = tmp_path / "profile.toml"
    path.write_text('[agent]\ntype="pi"\n[transport]\ntype="web"\n')
    checked = []
    async def live_check(config):
        checked.append(config.model)
    async def wrong_check(config):
        pytest.fail("Live must not require the old Realtime and Speech endpoints")
    monkeypatch.setattr("sleeptalk.openai_live.check_openai_live_connectivity", live_check)
    monkeypatch.setattr("sleeptalk.openai_realtime.check_openai_connectivity", wrong_check)
    await switch_to_realtime(path, "test-key", "marin", model="gpt-live-1", store=CredentialStore())
    assert checked == ["gpt-live-1"]
    assert 'model = "gpt-live-1"' in path.read_text()
    assert "test-key" not in path.read_text()


async def delegate(route, text, identity, start):
    route._caption(caption(text, start=start, end=start + 1000), "user")
    await route._delegate(identity, start + 1000)


def commentary(ws):
    return "".join(e["content"] for e in ws.sent if e["type"] == "session.commentary.append")


async def test_live_worker_keeps_reading_on_followups_and_cancel_it_stops_it():
    from sleeptalk.adapter import ControlAccepted, Steer, ToolEvent, ToolPhase
    worker = Agent()
    controls = []

    async def control(command):
        controls.append(command)
        return ControlAccepted()

    async def read(prompt, *, on_event=None):
        worker.prompts.append(prompt)
        on_event(ToolEvent("read_file", "read", ToolPhase.STARTED, detail="RAI.pdf"))
        await worker.release.wait()
        yield "The RAI paper describes an embodied agent framework."

    worker.control = control
    worker.respond_streaming = read
    web, session, route, live, ws, agent, stream = browser(job_agent_factory=lambda: worker)
    try:
        await web.feed_audio_frame(frame())
        await delegate(route, "Read the RAI embodied agent framework PDF and tell me about it", "read", 0)
        await eventually(lambda: bool(worker.prompts))
        job = session.jobs.job
        assert job.active and not agent.prompts
        assert any(e.payload.get("name") == "job.started" for e in session.events)
        await delegate(route, "Were you able to learn anything?", "status", 2000)
        await delegate(route, "No, keep doing it. Read the PDF we already have and tell me about it.", "continue", 4000)
        await delegate(route, "No, keep doing that one", "keep", 6000)
        assert session.jobs.job is job and len(session.jobs.jobs) == 1
        assert worker.aborts == 0 and len(worker.prompts) == 1
        assert len(controls) == 1 and isinstance(controls[0], Steer)
        assert "Read the PDF we already have" in controls[0].instruction
        assert "wait, change" not in commentary(ws) and "explicitly cancel" not in commentary(ws)
        await delegate(route, "Cancel it", "cancel", 8000)
        assert job.phase == "cancelled" and worker.aborts == 1 and worker.closes == 1
        assert "was cancelled" in commentary(ws)
        worker.release.set()
        await asyncio.sleep(0)
        assert "RAI paper describes" not in commentary(ws)
    finally:
        await web.close()


async def test_live_queues_workers_and_delivers_verified_reports_without_manual_turns():
    first, second = Agent(), Agent()
    pool = iter([first, second])
    web, session, route, live, ws, agent, stream = browser(job_agent_factory=lambda: next(pool), max_jobs=1)
    manual_turns = []

    async def no_manual_turn(*args, **kwargs):
        manual_turns.append(True)
        raise AssertionError("continuous Live must not run a manual turn for job news")

    route.run_turn = no_manual_turn
    try:
        await web.feed_audio_frame(frame())
        await delegate(route, "Read the RAI paper", "first", 0)
        await eventually(lambda: bool(first.prompts))
        await delegate(route, "Find the HuGo paper", "second", 2000)
        assert len(session.jobs.jobs) == 2 and session.jobs.jobs[1].queued
        assert not second.prompts and not agent.prompts
        first.release.set()
        await eventually(lambda: bool(second.prompts))
        await eventually(lambda: "verified result is 42" in commentary(ws))
        assert not manual_turns
        second.release.set()
        await eventually(lambda: all(j.phase == "completed" for j in session.jobs.jobs))
    finally:
        await web.close()
    assert first.closes == second.closes == 1


async def test_live_worker_permissions_require_new_explicit_audio(monkeypatch):
    from sleeptalk.adapter import ControlAccepted, PermissionRequested, PermissionDecision, ResolvePermission
    monkeypatch.setattr("sleeptalk.jobs._ASK_GRACE_S", 0)
    worker = Agent()
    controls = []

    async def work(prompt, *, on_event=None):
        worker.prompts.append(prompt)
        on_event(PermissionRequested("permission", "May I write the file?", frozenset({PermissionDecision.ALLOW_ONCE, PermissionDecision.DENY})))
        await worker.release.wait()
        yield "The write finished."

    async def control(command):
        controls.append(command)
        worker.release.set()
        return ControlAccepted()

    worker.respond_streaming, worker.control = work, control
    web, session, route, live, ws, agent, stream = browser(job_agent_factory=lambda: worker)
    try:
        await web.feed_audio_frame(frame())
        await delegate(route, "Update the file", "start", 2000)
        await eventually(lambda: route._worker_question is not None)
        await route._delegate_job("old", "allow once", [{"start_ms": 1000}])
        await route._delegate_job("vague", "yes but wait", [{"start_ms": 5000}])
        await route._delegate_job("keep", "No, keep doing it", [{"start_ms": 6000}])
        assert not controls and session.jobs.permission_pending
        await route._delegate_job("allow", "allow once", [{"start_ms": 7000}])
        assert len(controls) == 1 and isinstance(controls[0], ResolvePermission)
        assert controls[0].request_id == "permission" and controls[0].decision is PermissionDecision.ALLOW_ONCE
        await eventually(lambda: session.jobs.job.phase == "completed")
    finally:
        await web.close()


@pytest.mark.parametrize("answer", ["Cancel it", "Cancel the task", "Please cancel it.", "Yes"])
async def test_live_ambiguous_worker_cancel_accepts_the_answer_instead_of_asking_again(answer):
    pool = iter([Agent(), Agent()])
    web, session, route, live, ws, agent, stream = browser(job_agent_factory=lambda: next(pool))
    try:
        await web.feed_audio_frame(frame())
        await delegate(route, "Read the RAI paper", "first", 0)
        await delegate(route, "Find the HuGo paper", "second", 2000)
        await delegate(route, "Cancel it", "cancel", 4000)
        target = session.jobs.confirming
        assert target is not None and all(j.active for j in session.jobs.jobs)
        await delegate(route, answer, "confirmed", 6000)
        assert target.phase == "cancelled" and session.jobs.confirming is None
        assert sum(j.active for j in session.jobs.jobs) == 1
    finally:
        await web.close()


async def test_live_disconnect_stops_running_and_queued_workers():
    first, second = Agent(), Agent()
    pool = iter([first, second])
    web, session, route, live, ws, agent, stream = browser(job_agent_factory=lambda: next(pool), max_jobs=1)
    try:
        await web.feed_audio_frame(frame())
        await delegate(route, "Read the RAI paper", "first", 0)
        await eventually(lambda: bool(first.prompts))
        await delegate(route, "Find the HuGo paper", "second", 2000)
        await route._fail(ConnectionError("lost Live connection"))
        assert all(j.phase == "cancelled" for j in session.jobs.jobs)
        assert first.aborts == first.closes == 1 and not second.prompts
        assert ws.closed
    finally:
        await web.close()


async def test_live_worker_error_is_not_announced_as_success():
    from sleeptalk.adapter import AgentError
    worker = Agent()

    async def work(prompt, *, on_event=None):
        on_event(AgentError("read_failed", "Could not read the PDF", retryable=False))
        yield "I finished reading it."

    worker.respond_streaming = work
    web, session, route, live, ws, agent, stream = browser(job_agent_factory=lambda: worker)
    try:
        await web.feed_audio_frame(frame())
        await delegate(route, "Read the RAI paper", "first", 0)
        await eventually(lambda: "Could not read the PDF" in commentary(ws))
        assert session.jobs.job.phase == "failed"
        assert "I finished reading it" not in commentary(ws)
    finally:
        await web.close()


async def test_old_undelegated_captions_do_not_hide_cancel_it():
    web, session, route, live, ws, agent, stream = browser()
    try:
        await web.feed_audio_frame(frame())
        await delegate(route, "Read the paper", "start", 0)
        await eventually(lambda: bool(agent.prompts))
        route._caption(caption("Thanks for checking", start=2000, end=3000), "user")
        await delegate(route, "Cancel it", "cancel", 6000)
        assert agent.aborts == 1 and route.active_task_id is None
        assert len(agent.prompts) == 1
    finally:
        await web.close()
