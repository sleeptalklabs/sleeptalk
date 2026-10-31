from __future__ import annotations

import asyncio

import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from sleeptalk.adapter import (
    Cancel,
    ControlAccepted,
    Steer,
    SupportLevel,
    WorkerEvidence,
    WorkerPhase,
    adapter_capabilities,
    adapter_control,
    typed_events,
)
from sleeptalk.agents.hermes_gateway_adapter import (
    HermesGatewayClient,
    HermesGatewayError,
    QueueLink,
    _tool_event,
    _ws_url,
)
from tests.agent_conformance.cases import (
    case_adapter_does_not_own_voice_policy,
    case_clarification,
    case_permissions,
    case_steering,
    case_stream_yields_sentence,
    case_tool_lifecycle,
)
from tests.fixtures.hermes_tui_fake import FakeTui

FAKE_STDIO = Path(__file__).with_name("fixtures") / "hermes_tui_fake.py"


class _FallbackAgent:
    def __init__(self) -> None:
        self.warmed = False

    def respond_streaming(self, text: str, timeout: float = 300.0, on_event=None):
        del text, timeout, on_event

        async def _stream() -> AsyncIterator[str]:
            yield "ACP fallback."

        return _stream()

    async def warmup(self) -> None:
        self.warmed = True

    async def abort(self) -> None:
        return None

    async def close(self) -> None:
        return None


def _client(fake: FakeTui, **kwargs) -> HermesGatewayClient:
    return HermesGatewayClient(link=fake.link, permission_mode="pending", **kwargs)


@pytest.mark.asyncio
async def test_streaming_response_sentences() -> None:
    fake = FakeTui()
    client = _client(fake)
    result = await client.respond("price?")
    assert result == "Bitcoin is up. It gained 2%."
    methods = [name for name, _params in fake.link.calls]
    assert "session.create" in methods
    assert "prompt.submit" in methods


@pytest.mark.asyncio
async def test_existing_session_is_not_recreated() -> None:
    fake = FakeTui()
    client = _client(fake, session_id="live-session")
    await client.respond("price?")
    methods = [name for name, _params in fake.link.calls]
    assert "session.create" not in methods
    assert fake.created is False
    submit = [params for name, params in fake.link.calls if name == "prompt.submit"][0]
    assert submit["session_id"] == "live-session"


@pytest.mark.asyncio
async def test_tool_events_fire_with_detail() -> None:
    fake = FakeTui()
    seen: list[str] = []
    client = _client(fake)
    async for _ in client.respond_streaming("tool", on_event=seen.append):
        pass
    assert "tool:bash" in seen
    assert any(item.startswith("tooldetail:running") and "/" not in item for item in seen)


@pytest.mark.asyncio
async def test_interrupt_reaches_session() -> None:
    fake = FakeTui()
    client = _client(fake, session_id="s1")
    await client.warmup()
    await client.abort()
    assert ("session.interrupt", {"session_id": "s1"}) in fake.link.calls


@pytest.mark.asyncio
async def test_steer_and_cancel_reach_gateway() -> None:
    fake = FakeTui()
    client = _client(fake, session_id="s1")
    await client.warmup()
    steered = await adapter_control(client, Steer(instruction="be brief"))
    assert isinstance(steered, ControlAccepted)
    cancelled = await adapter_control(client, Cancel(target_id="turn-1"))
    assert isinstance(cancelled, ControlAccepted)
    methods = [name for name, _params in fake.link.calls]
    assert "session.steer" in methods
    assert "session.interrupt" in methods


@pytest.mark.asyncio
async def test_permission_and_clarification_conform() -> None:
    fake = FakeTui()
    client = _client(fake, session_id="s1")
    await case_permissions(client)
    await fake.settle()
    await case_clarification(client)
    # The clarification is answered after its turn ended; its reply must land
    # before the next turn starts, whatever the event loop's scheduling order.
    await fake.settle()
    await asyncio.sleep(0)
    await case_steering(client)
    await case_tool_lifecycle(client)
    await case_stream_yields_sentence(client)


@pytest.mark.asyncio
async def test_gateway_error_raises() -> None:
    fake = FakeTui()
    client = _client(fake)
    with pytest.raises(HermesGatewayError, match="boom"):
        await client.respond("fail")


@pytest.mark.asyncio
async def test_incompatible_gateway_falls_back() -> None:
    fallback = _FallbackAgent()

    def _boom(_method: str, _params: dict) -> dict:
        raise HermesGatewayError("incompatible Hermes gateway: first event was 'nope'")

    client = HermesGatewayClient(
        link=QueueLink(_boom),
        fallback_factory=lambda: fallback,
    )
    result = await client.respond("hello")
    assert result == "ACP fallback."
    assert client.fallback_reason is not None
    assert "incompatible" in client.fallback_reason
    assert fallback.warmed is True


@pytest.mark.asyncio
async def test_configured_url_does_not_spawn_local_tui(monkeypatch: pytest.MonkeyPatch) -> None:
    spawned: list[list[str]] = []

    async def _forbid(*argv: str, **_kwargs):
        spawned.append(list(argv))
        raise AssertionError("local TUI must not spawn when gateway_url is set")

    monkeypatch.setattr("asyncio.create_subprocess_exec", _forbid)
    fallback = _FallbackAgent()
    client = HermesGatewayClient(
        base_url="http://127.0.0.1:9",
        session_id="kept",
        fallback_factory=lambda: fallback,
    )
    with pytest.raises(HermesGatewayError, match="dashboard unavailable"):
        await client.respond("hello")
    assert spawned == []
    assert fallback.warmed is False


@pytest.mark.asyncio
async def test_stdio_fixture_streams() -> None:
    from sleeptalk.agents.hermes_gateway_adapter import StdioTuiLink

    link = StdioTuiLink([sys.executable, str(FAKE_STDIO)], cwd=".")
    await link.start()
    client = HermesGatewayClient(link=link, session_id="s1")
    try:
        result = await client.respond("hello")
        assert "Hello there." in result
    finally:
        await client.close()


def test_tool_event_mapping() -> None:
    kind, detail = _tool_event({"tool_name": "web_search", "args": {"query": "btc price"}})
    assert kind == "search" and detail == "searching for btc price"
    kind, detail = _tool_event({"tool_name": "read_file", "args": {"path": "/a/b/main.py"}})
    assert kind == "read" and detail == "reading main.py"
    kind, detail = _tool_event({"tool_name": "memory", "args": {}})
    assert kind == "other" and detail == ""
    kind, detail = _tool_event({"tool_name": "grep", "args": {"pattern": "keepalive"}})
    assert kind == "search" and detail == "searching for keepalive"
    kind, detail = _tool_event({"tool_name": "websearch", "args": {}})
    assert kind == "search" and detail == ""


def test_ws_url_rewrites_http() -> None:
    assert _ws_url("http://host:9119") == "ws://host:9119/api/ws"
    assert _ws_url("https://host/api/ws") == "wss://host/api/ws"
    assert _ws_url("ws://host/api/ws") == "ws://host/api/ws"


def test_ws_url_keeps_http_query_token() -> None:
    assert _ws_url("http://host:9119/?token=abc") == "ws://host:9119/api/ws?token=abc"
    assert _ws_url("http://host:9119/api/ws", "from-env") == (
        "ws://host:9119/api/ws?token=from-env"
    )


def test_manifest_is_honest() -> None:
    manifest = adapter_capabilities(HermesGatewayClient())
    assert manifest.structured_events.level is SupportLevel.SUPPORTED
    assert manifest.steering.level is SupportLevel.SUPPORTED
    assert manifest.targeted_cancellation.level is SupportLevel.SUPPORTED
    assert manifest.usage.level is SupportLevel.UNSUPPORTED
    assert manifest.status.level is SupportLevel.UNSUPPORTED
    assert manifest.workers.level is SupportLevel.SUPPORTED
    assert manifest.workers.evidence is WorkerEvidence.LIFECYCLE_NATIVE


@pytest.mark.asyncio
async def test_native_subagent_events_are_workers() -> None:
    fake = FakeTui()
    seen: list[object] = []
    client = _client(fake)
    async for _ in client.respond_streaming(
        "subagent", on_event=typed_events(seen.append)
    ):
        pass
    workers = [item for item in seen if getattr(item, "kind", None) == "worker"]
    assert [item.worker_id for item in workers] == [
        "sa-0-1a2b3c4d",
        "sa-0-1a2b3c4d",
        "sa-0-1a2b3c4d",
    ]
    assert workers[0].phase is WorkerPhase.STARTED
    assert workers[0].label == "Explore the repo"
    assert workers[0].detached is True
    assert workers[1].phase is WorkerPhase.RUNNING
    assert workers[2].phase is WorkerPhase.COMPLETED
    assert workers[2].summary == "Mapped the layout"
    tools = [item for item in seen if getattr(item, "kind", None) == "tool"]
    assert tools == []
    texts = [
        item
        for item in seen
        if getattr(item, "kind", None) == "assistant.delta"
        and "child tokens" in getattr(item, "text", "")
    ]
    assert texts == []


@pytest.mark.asyncio
async def test_live_delegate_task_is_a_worker_not_a_tool() -> None:
    fake = FakeTui()
    seen: list[object] = []
    client = _client(fake)
    async for _ in client.respond_streaming(
        "delegate", on_event=typed_events(seen.append)
    ):
        pass
    workers = [item for item in seen if getattr(item, "kind", None) == "worker"]
    tools = [item for item in seen if getattr(item, "kind", None) == "tool"]
    assert [item.worker_id for item in workers] == ["sa-0-live", "sa-0-live"]
    assert workers[0].phase is WorkerPhase.STARTED
    assert workers[0].label == "Repo architecture"
    assert workers[0].detached is True
    assert workers[1].phase is WorkerPhase.COMPLETED
    assert [item.name for item in tools if item.name] == ["bash"]


@pytest.mark.asyncio
async def test_child_steer_and_cancel_use_subagent_rpcs() -> None:
    fake = FakeTui()
    client = _client(fake, session_id="s1")
    await client.warmup()
    steered = await adapter_control(
        client, Steer(instruction="keep going", target_id="sa-0-1a2b3c4d")
    )
    assert isinstance(steered, ControlAccepted)
    cancelled = await adapter_control(client, Cancel(target_id="sa-0-1a2b3c4d"))
    assert isinstance(cancelled, ControlAccepted)
    assert (
        "subagent.steer",
        {
            "session_id": "s1",
            "subagent_id": "sa-0-1a2b3c4d",
            "text": "keep going",
        },
    ) in fake.link.calls
    assert (
        "subagent.interrupt",
        {"session_id": "s1", "subagent_id": "sa-0-1a2b3c4d"},
    ) in fake.link.calls
    methods = [name for name, _params in fake.link.calls]
    assert "session.steer" not in methods
    assert methods.count("session.interrupt") == 0


@pytest.mark.asyncio
async def test_unknown_gateway_events_do_not_end_the_turn() -> None:
    fake = FakeTui()
    client = _client(fake)
    result = await client.respond("noise")
    assert result == "Bitcoin is up. It gained 2%."


def test_adapter_does_not_own_voice_policy() -> None:
    case_adapter_does_not_own_voice_policy(
        HermesGatewayClient(), sentences=["Bitcoin is up."], events=[]
    )


async def test_a_refused_gateway_socket_never_quotes_its_token(monkeypatch):
    """The Hermes socket URL carries the session token; aiohttp's own error
    text repeats the URL, so the error Sleeptalk raises must not."""
    import aiohttp
    from yarl import URL

    from sleeptalk.agents.hermes_gateway_adapter import HermesGatewayError, WebsocketTuiLink

    secret = "tok-SENTINEL-1234"
    url = f"ws://127.0.0.1:9/api/ws?token={secret}"

    async def refuse(self, target, **kwargs):
        info = aiohttp.RequestInfo(URL(target), "GET", {}, URL(target))
        raise aiohttp.WSServerHandshakeError(info, (), status=401, message="Unauthorized")

    monkeypatch.setattr(aiohttp.ClientSession, "ws_connect", refuse)
    link = WebsocketTuiLink(url)
    with pytest.raises(HermesGatewayError) as failure:
        await link.start()
    assert secret not in str(failure.value) and "401" in str(failure.value)
    assert failure.value.__cause__ is None


@pytest.mark.asyncio
async def test_a_cancelled_reply_never_leaks_into_the_next_one() -> None:
    """The caller kept talking, so the first reply was cancelled mid-stream.
    Hermes goes on sending it until the interrupt lands; that late text must
    not be read as the next reply (two answers woven together mid-word)."""
    link: QueueLink
    tasks: set[asyncio.Task[None]] = set()

    async def old_run() -> None:
        await link.push({"type": "message.delta", "payload": {"text": "Old reply starts. "}})
        await asyncio.sleep(0.05)
        await link.push({"type": "message.delta", "payload": {"text": "Late old text. "}})
        await link.push({"type": "message.complete", "payload": {}})

    async def new_run() -> None:
        await asyncio.sleep(0.1)  # a model takes longer to start than the old run to trail off
        await link.push({"type": "message.delta", "payload": {"text": "New answer."}})
        await link.push({"type": "message.complete", "payload": {}})

    def handle(method: str, params: dict) -> dict:
        if method == "session.create":
            return {"session_id": "s1"}
        if method == "prompt.submit":
            run = old_run if params["text"] == "slow" else new_run
            task = asyncio.get_running_loop().create_task(run())
            tasks.add(task)
            return {"status": "streaming"}
        if method == "session.interrupt":
            return {"status": "interrupted"}  # it stops later, not now
        raise RuntimeError(method)

    link = QueueLink(handle)
    client = HermesGatewayClient(link=link, permission_mode="auto")
    stream = client.respond_streaming("slow")
    assert await anext(stream) == "Old reply starts."
    await stream.aclose()  # cancelled for the caller's next words
    assert await client.respond("next") == "New answer."
    assert ("session.interrupt", {"session_id": "s1"}) in link.calls


@pytest.mark.asyncio
async def test_a_queued_prompt_skips_the_busy_run_it_waited_behind(monkeypatch) -> None:
    """Hermes queues a prompt that arrives mid-run (interrupting the run,
    which may take its time to stop). Everything until that run ends and the
    queued one starts belongs to the old run."""
    monkeypatch.setattr("sleeptalk.agents.hermes_gateway_adapter._SETTLE_PREVIOUS_RUN_S", 0.05)
    link: QueueLink
    tasks: set[asyncio.Task[None]] = set()
    state = {"running": False, "queued": None}

    async def run(text: str) -> None:
        state["running"] = True
        await link.push({"type": "message.start"})
        if text == "slow":
            await link.push({"type": "message.delta", "payload": {"text": "<job title='todo'>Build "}})
            await asyncio.sleep(0.2)  # a tool that won't stop for the interrupt
            await link.push({"type": "message.delta", "payload": {"text": "it.</job> Late old text. "}})
        else:
            await link.push({"type": "message.delta", "payload": {"text": f"Answer to {text}."}})
        await link.push({"type": "message.complete", "payload": {}})
        state["running"] = False
        if state["queued"] is not None:
            queued, state["queued"] = state["queued"], None
            await run(queued)

    def handle(method: str, params: dict) -> dict:
        if method == "session.create":
            return {"session_id": "s1"}
        if method == "prompt.submit":
            if state["running"]:
                state["queued"] = params["text"]
                return {"status": "queued"}
            task = asyncio.get_running_loop().create_task(run(params["text"]))
            tasks.add(task)
            return {"status": "streaming"}
        if method == "session.interrupt":
            return {"status": "interrupted"}
        raise RuntimeError(method)

    link = QueueLink(handle)
    client = HermesGatewayClient(link=link, permission_mode="auto")
    stream = client.respond_streaming("slow")
    first = asyncio.ensure_future(anext(stream))
    await asyncio.sleep(0.02)
    first.cancel()  # the caller kept talking before a sentence ended
    await asyncio.gather(first, return_exceptions=True)
    await stream.aclose()
    assert await client.respond("next") == "Answer to next."
    await asyncio.gather(*tasks)


@pytest.mark.asyncio
async def test_a_reply_sent_only_at_the_end_is_heard() -> None:
    """Hermes sends a short reply in message.complete alone, no deltas."""
    link: QueueLink

    def handle(method: str, params: dict) -> dict:
        if method == "session.create":
            return {"session_id": "s1"}
        if method == "prompt.submit":
            asyncio.get_running_loop().create_task(link.push(
                {"type": "message.complete", "payload": {"text": "One hundred thirty-two."}}))
            return {"status": "streaming"}
        raise RuntimeError(method)

    link = QueueLink(handle)
    client = HermesGatewayClient(link=link, permission_mode="auto")
    assert await client.respond("What's twelve times eleven?") == "One hundred thirty-two."


@pytest.mark.asyncio
async def test_text_from_a_model_call_orphaned_by_an_interrupt_is_never_spoken() -> None:
    """A run interrupted while waiting for its model leaves that call
    running; its text arrives as the next run's deltas. Only the clean
    message.complete text of the next run may be used."""
    link: QueueLink
    submits = []

    async def first_run() -> None:
        await link.push({"type": "message.start"})

    async def stopped() -> None:
        await link.push({"type": "message.complete",
                         "payload": {"text": "Operation interrupted: waiting for model response (2.1s elapsed)."}})

    async def second_run() -> None:
        await link.push({"type": "message.start"})
        for piece in ("I've kicked", "<job title='To", " off a job.", "do'>Build it.</job>", " Done soon."):
            await link.push({"type": "message.delta", "payload": {"text": piece}})
        await link.push({"type": "message.complete",
                         "payload": {"text": "<job title='Todo'>Build it.</job> Done soon."}})

    def handle(method: str, params: dict) -> dict:
        if method == "session.create":
            return {"session_id": "s1"}
        if method == "prompt.submit":
            submits.append(params["text"])
            asyncio.get_running_loop().create_task(first_run() if len(submits) == 1 else second_run())
            return {"status": "streaming"}
        if method == "session.interrupt":
            asyncio.get_running_loop().create_task(stopped())
            return {"status": "interrupted"}
        raise RuntimeError(method)

    link = QueueLink(handle)
    client = HermesGatewayClient(link=link, permission_mode="auto")
    stream = client.respond_streaming("Make a todo app.")
    first = asyncio.ensure_future(anext(stream))
    await asyncio.sleep(0.02)
    first.cancel()  # the caller went on talking while Hermes waited for its model
    await asyncio.gather(first, return_exceptions=True)
    await stream.aclose()
    sentences = [s async for s in client.respond_streaming("Make a todo app. Keep it local.")]
    assert " ".join(sentences) == "<job title='Todo'>Build it.</job> Done soon."


@pytest.mark.asyncio
async def test_a_job_page_sees_hermes_think_write_and_use_tools() -> None:
    from sleeptalk.adapter import ReasoningDelta, ToolEvent, ToolPhase, typed_events

    link: QueueLink

    async def run() -> None:
        for event in (
            {"type": "reasoning.delta", "payload": {"text": "A claw machine needs physics."}},
            {"type": "tool.generating", "payload": {"name": "write_file"}},
            {"type": "tool.start", "payload": {"tool_id": "t1", "name": "terminal", "context": "ls -la claw"}},
            {"type": "tool.complete", "payload": {"tool_id": "t1", "name": "terminal", "args": {"command": "ls -la claw"}}},
            {"type": "message.delta", "payload": {"text": "Built it."}},
            {"type": "message.complete", "payload": {"text": "Built it."}},
        ):
            await link.push(event)

    def handle(method: str, params: dict) -> dict:
        if method == "session.create":
            return {"session_id": "s1"}
        if method == "prompt.submit":
            asyncio.get_running_loop().create_task(run())
            return {"status": "streaming"}
        raise RuntimeError(method)

    link = QueueLink(handle)
    client = HermesGatewayClient(link=link, permission_mode="auto")
    seen: list = []
    assert [s async for s in client.respond_streaming("go", on_event=typed_events(seen.append))] == ["Built it."]
    assert ReasoningDelta("A claw machine needs physics.") in seen
    tools = [(e.phase, e.display_name, e.label, e.call_id) for e in seen if isinstance(e, ToolEvent)]
    assert (ToolPhase.UPDATED, "write_file", "writing…", None) in tools
    assert (ToolPhase.STARTED, "terminal", "ls -la claw", "t1") in tools
    assert (ToolPhase.COMPLETED, "terminal", None, "t1") in tools
