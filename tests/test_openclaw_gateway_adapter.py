from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import aiohttp
import pytest

from sleeptalk.adapter import (
    Cancel,
    ControlAccepted,
    ControlRejected,
    PermissionDecision,
    PermissionRequested,
    ResolvePermission,
    Steer,
    SupportLevel,
    ToolEvent,
    ToolPhase,
    adapter_capabilities,
    adapter_control,
    typed_events,
)
from sleeptalk.agents.openclaw_gateway_adapter import (
    CONNECT_TIMEOUT_S,
    DEFAULT_GATEWAY_URL,
    AiohttpGatewaySession,
    OpenClawGatewayClient,
    OpenClawGatewayError,
    TOOL_EVENTS_CAP,
    _FrameMux,
    _error_from_frame,
    _ws_url,
)
from tests.agent_conformance.cases import (
    case_adapter_does_not_own_voice_policy,
    case_permissions,
    case_steering,
    case_stream_yields_sentence,
    case_tool_lifecycle,
)
from tests.fixtures.openclaw_gateway import FakeGatewaySession


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


def _client(fake: FakeGatewaySession, **kwargs) -> OpenClawGatewayClient:
    return OpenClawGatewayClient(
        session=fake,
        token="test-token",
        permission_mode="pending",
        **kwargs,
    )


@pytest.mark.asyncio
async def test_handshake_sends_protocol_4_and_token() -> None:
    fake = FakeGatewaySession()
    client = _client(fake)
    await client.warmup()
    assert fake.connect_count == 1
    connect = [params for name, params in fake.calls if name == "connect"]
    assert len(connect) == 1
    params = connect[0]
    assert params["minProtocol"] == 4
    assert params["maxProtocol"] == 4
    assert params["role"] == "operator"
    assert params["auth"]["token"] == "test-token"
    assert "device" not in params
    assert params["caps"] == [TOOL_EVENTS_CAP]
    assert "operator.read" in params["scopes"]
    assert "operator.write" in params["scopes"]
    assert "operator.approvals" in params["scopes"]
    await client.close()


@pytest.mark.asyncio
async def test_empty_token_never_sends_connect() -> None:
    fake = FakeGatewaySession()
    fallback = _FallbackAgent()
    client = OpenClawGatewayClient(
        session=fake,
        token="",
        fallback_factory=lambda: fallback,
    )
    result = await client.respond("hello")
    assert result == "ACP fallback."
    assert not any(name == "connect" for name, _params in fake.calls)
    assert fake.connect_count == 0
    assert client.fallback_reason is not None
    assert "token" in client.fallback_reason
    assert fallback.warmed is True
    await client.close()


@pytest.mark.asyncio
async def test_existing_session_is_not_created() -> None:
    fake = FakeGatewaySession()
    client = _client(fake, session_key="live-session")
    await client.respond("price?")
    methods = [name for name, _params in fake.calls]
    assert "sessions.create" not in methods
    assert fake.created is False
    resolve = [params for name, params in fake.calls if name == "sessions.resolve"][0]
    assert resolve["key"] == "live-session"
    assert any(name == "sessions.subscribe" for name, _params in fake.calls)
    send = [params for name, params in fake.calls if name == "chat.send"][0]
    assert send["sessionKey"] == "live-session"
    await client.close()


@pytest.mark.asyncio
async def test_streaming_response_sentences() -> None:
    fake = FakeGatewaySession()
    client = _client(fake)
    result = await client.respond("price?")
    assert result == "Bitcoin is up. It gained 2%."
    methods = [name for name, _params in fake.calls]
    assert "sessions.create" in methods
    assert "chat.send" in methods
    await client.close()


@pytest.mark.asyncio
async def test_tool_lifecycle_start_before_update() -> None:
    fake = FakeGatewaySession()
    seen: list[object] = []
    client = _client(fake)
    async for _ in client.respond_streaming("tool", on_event=typed_events(seen.append)):
        pass
    tools = [event for event in seen if isinstance(event, ToolEvent)]
    assert tools
    assert tools[0].phase is ToolPhase.STARTED
    assert any(event.phase is ToolPhase.UPDATED for event in tools) or any(
        event.phase is ToolPhase.COMPLETED for event in tools
    )
    await client.close()


@pytest.mark.asyncio
async def test_agent_tool_event_publishes_lifecycle() -> None:
    fake = FakeGatewaySession()
    seen: list[object] = []
    client = _client(fake)
    async for _ in client.respond_streaming("agent-tool", on_event=typed_events(seen.append)):
        pass
    tools = [event for event in seen if isinstance(event, ToolEvent)]
    assert tools
    assert tools[0].phase is ToolPhase.STARTED
    await client.close()


@pytest.mark.asyncio
async def test_approval_then_resolve_permission() -> None:
    fake = FakeGatewaySession()
    client = _client(fake, session_key="s1")
    events: list[object] = []

    async def _consume() -> None:
        async for _ in client.respond_streaming("permission", on_event=typed_events(events.append)):
            pass

    task = asyncio.create_task(_consume())
    request = await _wait_for_permission(events)
    assert not hasattr(request, "allowed")
    assert not hasattr(request, "approved")
    accepted = await adapter_control(
        client,
        ResolvePermission(request_id=request.request_id, decision=PermissionDecision.ALLOW_ONCE),
    )
    assert isinstance(accepted, ControlAccepted)
    await task
    resolve = [params for name, params in fake.calls if name == "exec.approval.resolve"][0]
    assert resolve == {"id": "perm-1", "decision": "allow-once"}
    await client.close()


@pytest.mark.asyncio
async def test_abort_correlates_to_active_run() -> None:
    fake = FakeGatewaySession()
    client = _client(fake, session_key="s1")
    await client.warmup()
    await client.abort()
    assert not any(name == "chat.abort" for name, _params in fake.calls)
    cancelled = await adapter_control(client, Cancel(target_id="run-1"))
    assert isinstance(cancelled, ControlAccepted)
    assert any(
        name == "chat.abort" and params.get("runId") == "run-1" for name, params in fake.calls
    )
    await client.close()


@pytest.mark.asyncio
async def test_reconnect_reuses_idempotency_key() -> None:
    fake = FakeGatewaySession(fail_first_send=True)
    client = _client(fake, session_key="s1")
    result = await client.respond("hello")
    assert result
    assert fake.send_keys[0]
    assert fake.send_keys[0] == fake.send_keys[1]
    assert len(fake.send_keys) == 2
    assert fake.connect_count >= 2
    await client.close()


@pytest.mark.asyncio
async def test_failed_reconnect_handshake_clears_session() -> None:
    class _FailSecondHandshake(FakeGatewaySession):
        def __init__(self, **kwargs) -> None:
            super().__init__(**kwargs)
            self._reject_reconnect = False

        async def handle(self, method: str, params: dict) -> dict:
            if method == "connect" and self._reject_reconnect:
                self._reject_reconnect = False
                raise OpenClawGatewayError("AUTH_TOKEN_MISMATCH")
            if method == "chat.send" and self.fail_first_send:
                self._reject_reconnect = True
            return await super().handle(method, params)

    fake = _FailSecondHandshake(fail_first_send=True)
    client = _client(fake, session_key="s1")
    with pytest.raises(OpenClawGatewayError, match="AUTH_TOKEN_MISMATCH"):
        await client.respond("hello")
    assert client._session is None
    connects_after_fail = sum(1 for name, _params in fake.calls if name == "connect")
    await client.warmup()
    connects_after_warmup = sum(1 for name, _params in fake.calls if name == "connect")
    assert client._session is not None
    assert connects_after_warmup > connects_after_fail
    await client.close()


@pytest.mark.asyncio
async def test_foreign_session_final_does_not_end_turn() -> None:
    fake = FakeGatewaySession(
        extra_events=[
            {
                "event": "chat",
                "payload": {
                    "state": "final",
                    "sessionKey": "other",
                    "runId": "other-run",
                    "seq": 1,
                },
            }
        ]
    )
    client = _client(fake, session_key="s1")
    result = await client.respond("hello")
    assert result == "Bitcoin is up. It gained 2%."
    await client.close()


@pytest.mark.asyncio
async def test_foreign_approval_is_not_auto_resolved() -> None:
    fake = FakeGatewaySession(
        extra_events=[
            {
                "event": "exec.approval.requested",
                "payload": {
                    "id": "other-1",
                    "request": {
                        "sessionKey": "other",
                        "command": "rm -rf /",
                        "allowedDecisions": ["allow-once", "deny"],
                    },
                },
            }
        ]
    )
    client = OpenClawGatewayClient(
        session=fake,
        token="test-token",
        permission_mode="auto",
        session_key="s1",
    )
    result = await client.respond("hello")
    assert result
    assert not any(
        name == "exec.approval.resolve" and params.get("id") == "other-1"
        for name, params in fake.calls
    )
    await client.close()


@pytest.mark.asyncio
async def test_unscoped_approval_is_ignored_in_pending_mode() -> None:
    fake = FakeGatewaySession(
        extra_events=[
            {
                "event": "exec.approval.requested",
                "payload": {"id": "loose-1", "command": "rm -rf /"},
            }
        ]
    )
    seen: list[object] = []
    client = _client(fake, session_key="s1")
    async for _ in client.respond_streaming("hello", on_event=typed_events(seen.append)):
        pass
    assert not any(
        isinstance(event, PermissionRequested) and event.request_id == "loose-1"
        for event in seen
    )
    await client.close()


@pytest.mark.asyncio
async def test_steer_adopts_replacement_run_id() -> None:
    class _SteerReplacesRun(FakeGatewaySession):
        async def handle(self, method: str, params: dict) -> dict:
            if method == "sessions.steer":
                self.calls.append((method, params))
                return {"ok": True, "runId": "run-2"}
            return await super().handle(method, params)

    fake = _SteerReplacesRun()
    client = _client(fake, session_key="s1")
    task = asyncio.create_task(client.respond("hang", timeout=5.0))

    async def _wait_for_send() -> None:
        while not fake.send_keys:
            await asyncio.sleep(0)

    await asyncio.wait_for(_wait_for_send(), timeout=2.0)
    accepted = await adapter_control(client, Steer(instruction="be brief"))
    assert isinstance(accepted, ControlAccepted)
    await fake.push(
        {
            "event": "chat",
            "payload": {
                "state": "aborted",
                "runId": "run-1",
                "sessionKey": "s1",
                "seq": 1,
            },
        }
    )
    await fake.push(
        {
            "event": "chat",
            "payload": {
                "state": "delta",
                "deltaText": "Steered answer.",
                "runId": "run-2",
                "sessionKey": "s1",
                "seq": 2,
            },
        }
    )
    await fake.push(
        {
            "event": "chat",
            "payload": {
                "state": "final",
                "runId": "run-2",
                "sessionKey": "s1",
                "seq": 3,
            },
        }
    )
    result = await asyncio.wait_for(task, timeout=2.0)
    assert "Steered" in result
    await client.close()


@pytest.mark.asyncio
async def test_nested_approval_is_auto_resolved() -> None:
    fake = FakeGatewaySession(
        extra_events=[
            {
                "event": "exec.approval.requested",
                "payload": {
                    "id": "real-1",
                    "request": {
                        "sessionKey": "s1",
                        "runId": "run-1",
                        "command": "ls",
                        "allowedDecisions": ["allow-once", "deny"],
                    },
                },
            }
        ]
    )
    client = OpenClawGatewayClient(
        session=fake,
        token="test-token",
        permission_mode="auto",
        session_key="s1",
    )
    result = await client.respond("hello")
    assert result
    assert any(
        name == "exec.approval.resolve" and params.get("id") == "real-1"
        for name, params in fake.calls
    )
    await client.close()


@pytest.mark.asyncio
async def test_disconnect_event_retries_inflight_once() -> None:
    fake = FakeGatewaySession(extra_events=[{"event": "_disconnect", "payload": {}}])
    client = _client(fake)
    result = await client.respond("hello")
    assert result == "Bitcoin is up. It gained 2%."
    assert fake.connect_count >= 2
    assert len(fake.send_keys) == 2
    assert fake.send_keys[0] == fake.send_keys[1]
    await client.close()


@pytest.mark.asyncio
async def test_close_during_turn_does_not_reconnect() -> None:
    fake = FakeGatewaySession()
    client = _client(fake, session_key="s1")
    task = asyncio.create_task(client.respond("hang", timeout=5.0))

    async def _wait_for_send() -> None:
        while not fake.send_keys:
            await asyncio.sleep(0)

    await asyncio.wait_for(_wait_for_send(), timeout=2.0)
    connects = fake.connect_count
    sends = len(fake.send_keys)
    await client.close()
    with pytest.raises(OpenClawGatewayError, match="closed"):
        await task
    assert fake.connect_count == connects
    assert len(fake.send_keys) == sends


@pytest.mark.asyncio
async def test_close_during_reconnect_does_not_resend() -> None:
    class _RacingSession(FakeGatewaySession):
        def __init__(self) -> None:
            super().__init__()
            self.reconnect_entered = asyncio.Event()
            self.allow_reconnect = asyncio.Event()

        async def connect(self) -> None:
            if self.connect_count:
                self.reconnect_entered.set()
                await self.allow_reconnect.wait()
            await super().connect()

    fake = _RacingSession()
    client = _client(fake, session_key="s1")
    task = asyncio.create_task(client.respond("hang", timeout=5.0))

    async def _wait_for_send() -> None:
        while not fake.send_keys:
            await asyncio.sleep(0)

    await asyncio.wait_for(_wait_for_send(), timeout=2.0)
    await fake.push({"event": "_disconnect", "payload": {}})
    await asyncio.wait_for(fake.reconnect_entered.wait(), timeout=2.0)
    sends = len(fake.send_keys)
    await client.close()
    fake.allow_reconnect.set()
    with pytest.raises(OpenClawGatewayError, match="closed"):
        await task
    assert len(fake.send_keys) == sends


@pytest.mark.asyncio
async def test_close_during_fallback_warmup_does_not_serve() -> None:
    class _BlockingFallback:
        def __init__(self) -> None:
            self.entered = asyncio.Event()
            self.release = asyncio.Event()
            self.closed = False

        async def warmup(self) -> None:
            self.entered.set()
            await self.release.wait()

        async def close(self) -> None:
            self.closed = True

        async def abort(self) -> None:
            return None

        def respond_streaming(self, text: str, timeout: float = 300.0, on_event=None):
            del text, timeout, on_event

            async def _stream() -> AsyncIterator[str]:
                yield "fallback replied"

            return _stream()

    fallback = _BlockingFallback()
    client = OpenClawGatewayClient(
        session=FakeGatewaySession(fail_connect=True),
        token="bad-token",
        fallback_factory=lambda: fallback,
    )
    task = asyncio.create_task(client.respond("hello"))
    await asyncio.wait_for(fallback.entered.wait(), timeout=2.0)
    await client.close()
    fallback.release.set()
    with pytest.raises(OpenClawGatewayError, match="closed"):
        await task
    assert fallback.closed is True
    assert client._fallback is None


@pytest.mark.asyncio
async def test_ws_connect_honors_connect_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    import sleeptalk.agents.openclaw_gateway_adapter as adapter

    monkeypatch.setattr(adapter, "CONNECT_TIMEOUT_S", 0.2)

    class _HangHTTP:
        async def ws_connect(self, *args, **kwargs):
            del args, kwargs
            await asyncio.sleep(30)

        async def close(self) -> None:
            return None

    monkeypatch.setattr(aiohttp, "ClientSession", lambda *args, **kwargs: _HangHTTP())
    session = AiohttpGatewaySession("ws://127.0.0.1:9")
    started = asyncio.get_running_loop().time()
    with pytest.raises(OpenClawGatewayError, match="unreachable"):
        await session.connect()
    assert asyncio.get_running_loop().time() - started < 2.0
    assert CONNECT_TIMEOUT_S == 15.0


@pytest.mark.asyncio
async def test_cancelled_ws_connect_closes_http(monkeypatch: pytest.MonkeyPatch) -> None:
    entered = asyncio.Event()
    closed = {"http": False}

    class _HangHTTP:
        async def ws_connect(self, *args, **kwargs):
            del args, kwargs
            entered.set()
            await asyncio.Event().wait()

        async def close(self) -> None:
            closed["http"] = True

    monkeypatch.setattr(aiohttp, "ClientSession", lambda *args, **kwargs: _HangHTTP())
    session = AiohttpGatewaySession("ws://127.0.0.1:9")
    task = asyncio.create_task(session.connect())
    await asyncio.wait_for(entered.wait(), timeout=2.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed["http"] is True


@pytest.mark.asyncio
async def test_failed_fallback_warmup_closes_agent() -> None:
    class _FailWarmup:
        def __init__(self) -> None:
            self.closed = False

        async def warmup(self) -> None:
            raise RuntimeError("warmup boom")

        async def close(self) -> None:
            self.closed = True

        async def abort(self) -> None:
            return None

        def respond_streaming(self, text: str, timeout: float = 300.0, on_event=None):
            del text, timeout, on_event

            async def _stream() -> AsyncIterator[str]:
                yield "fallback replied"

            return _stream()

    fallback = _FailWarmup()
    client = OpenClawGatewayClient(
        session=FakeGatewaySession(fail_connect=True),
        token="bad-token",
        fallback_factory=lambda: fallback,
    )
    with pytest.raises(RuntimeError, match="warmup boom"):
        await client.respond("hello")
    assert fallback.closed is True
    assert client._fallback is None
    await client.close()


@pytest.mark.asyncio
async def test_cancelled_fallback_warmup_closes_agent() -> None:
    class _BlockingWarmup:
        def __init__(self) -> None:
            self.entered = asyncio.Event()
            self.closed = False

        async def warmup(self) -> None:
            self.entered.set()
            await asyncio.Event().wait()

        async def close(self) -> None:
            self.closed = True

        async def abort(self) -> None:
            return None

        def respond_streaming(self, text: str, timeout: float = 300.0, on_event=None):
            del text, timeout, on_event

            async def _stream() -> AsyncIterator[str]:
                yield "fallback replied"

            return _stream()

    fallback = _BlockingWarmup()
    client = OpenClawGatewayClient(
        session=FakeGatewaySession(fail_connect=True),
        token="bad-token",
        fallback_factory=lambda: fallback,
    )
    task = asyncio.create_task(client.respond("hello"))
    await asyncio.wait_for(fallback.entered.wait(), timeout=2.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert fallback.closed is True
    assert client._fallback is None


@pytest.mark.asyncio
async def test_cancelled_turn_does_not_steal_next_event() -> None:
    fake = FakeGatewaySession()
    client = _client(fake, session_key="s1")
    hang = asyncio.create_task(client.respond("hang", timeout=5.0))

    async def _wait_for_send() -> None:
        while not fake.send_keys:
            await asyncio.sleep(0)

    await asyncio.wait_for(_wait_for_send(), timeout=2.0)
    await asyncio.sleep(0)
    hang.cancel()
    with pytest.raises(asyncio.CancelledError):
        await hang

    seen: list[object] = []

    async def _second() -> None:
        async for _ in client.respond_streaming(
            "permission", timeout=2.0, on_event=typed_events(seen.append)
        ):
            pass

    second = asyncio.create_task(_second())
    permission = await asyncio.wait_for(_wait_for_permission(seen), timeout=2.0)
    assert isinstance(permission, PermissionRequested)
    second.cancel()
    with pytest.raises(asyncio.CancelledError):
        await second
    await client.close()


@pytest.mark.asyncio
async def test_cancelled_handshake_closes_session() -> None:
    class _HangConnect(FakeGatewaySession):
        def __init__(self) -> None:
            super().__init__()
            self.entered = asyncio.Event()
            self.close_count = 0

        async def request(self, method: str, params: dict | None = None):
            if method == "connect":
                self.entered.set()
                await asyncio.Event().wait()
            return await super().request(method, params)

        async def close(self) -> None:
            self.close_count += 1
            await super().close()

    fake = _HangConnect()
    client = _client(fake)
    task = asyncio.create_task(client.warmup())
    await asyncio.wait_for(fake.entered.wait(), timeout=2.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert client._session is None
    assert fake.close_count >= 1
    await client.close()


@pytest.mark.asyncio
async def test_cancelled_request_drops_pending_mux() -> None:
    entered = asyncio.Event()

    class _HangWS:
        closed = False

        async def send_str(self, value: str) -> None:
            del value
            entered.set()
            await asyncio.Event().wait()

    session = AiohttpGatewaySession("ws://example")
    session._ws = _HangWS()  # type: ignore[assignment]
    task = asyncio.create_task(session.request("chat.send", {"idempotencyKey": "k"}))
    await asyncio.wait_for(entered.wait(), timeout=2.0)
    assert session._mux._pending
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert session._mux._pending == {}


@pytest.mark.asyncio
async def test_request_timeout_drops_pending_mux(monkeypatch: pytest.MonkeyPatch) -> None:
    import sleeptalk.agents.openclaw_gateway_adapter as adapter

    monkeypatch.setattr(adapter, "REQUEST_TIMEOUT_S", 0.05)

    class _SilentWS:
        closed = False

        async def send_str(self, value: str) -> None:
            del value
            return None

    session = AiohttpGatewaySession("ws://example")
    session._ws = _SilentWS()  # type: ignore[assignment]
    with pytest.raises(OpenClawGatewayError):
        await session.request("chat.send", {"idempotencyKey": "k"})
    assert session._mux._pending == {}


@pytest.mark.asyncio
async def test_mux_drops_tick_events() -> None:
    mux = _FrameMux()
    await mux.ingest({"type": "event", "event": "tick", "payload": {}})
    await mux.ingest({"type": "event", "event": "chat", "payload": {"state": "delta"}})
    events = mux.events()
    assert events.qsize() == 1
    assert events.get_nowait()["event"] == "chat"


@pytest.mark.asyncio
async def test_cached_completed_send_ends_turn() -> None:
    class _CompletedDuringDisconnect(FakeGatewaySession):
        async def handle(self, method: str, params: dict) -> dict:
            if method == "chat.send":
                key = str(params.get("idempotencyKey") or "")
                self.send_keys.append(key)
                if len(self.send_keys) == 1:
                    asyncio.get_running_loop().call_soon(
                        self._events.put_nowait,
                        {"event": "_disconnect", "payload": {}},
                    )
                    return {"runId": "run-1", "status": "started", "sessionKey": "s1"}
                return {"runId": "run-1", "status": "ok", "sessionKey": "s1"}
            return await super().handle(method, params)

    fake = _CompletedDuringDisconnect()
    client = _client(fake, session_key="s1")
    result = await client.respond("hello", timeout=2.0)
    assert result
    assert len(fake.send_keys) == 2
    assert fake.send_keys[0] == fake.send_keys[1]
    await client.close()


@pytest.mark.asyncio
async def test_cached_timeout_send_raises() -> None:
    class _TimeoutAck(FakeGatewaySession):
        async def handle(self, method: str, params: dict) -> dict:
            if method == "chat.send":
                key = str(params.get("idempotencyKey") or "")
                self.send_keys.append(key)
                return {"runId": "run-1", "status": "timeout", "sessionKey": "s1"}
            return await super().handle(method, params)

    fake = _TimeoutAck()
    client = _client(fake, session_key="s1")
    with pytest.raises(OpenClawGatewayError, match="timed out"):
        await client.respond("hello", timeout=2.0)
    await client.close()


@pytest.mark.asyncio
async def test_concurrent_warmup_opens_one_session() -> None:
    class _SlowConnect(FakeGatewaySession):
        async def connect(self) -> None:
            await asyncio.sleep(0.05)
            await super().connect()

    fake = _SlowConnect()
    client = _client(fake)
    await asyncio.gather(client.warmup(), client.warmup())
    assert fake.connect_count == 1
    await client.close()


@pytest.mark.asyncio
async def test_startup_sidecars_reconnects_before_retry() -> None:
    fake = FakeGatewaySession(startup_sidecars=True)
    client = _client(fake)
    await client.warmup()
    assert fake.connect_count >= 2
    assert any(name == "connect" for name, _params in fake.calls)
    await client.close()


@pytest.mark.asyncio
async def test_slow_startup_sidecars_keep_retrying() -> None:
    fake = FakeGatewaySession(startup_sidecars=12)
    client = _client(fake)
    await client.warmup()
    assert fake.connect_count >= 13
    assert client.fallback_reason is None
    await client.close()


def test_startup_retry_delay_is_read_from_the_error() -> None:
    # OpenClaw 2026.10 puts retryAfterMs beside the code, not in details.
    error = _error_from_frame(
        {
            "error": {
                "code": "UNAVAILABLE",
                "message": "gateway starting; retry shortly",
                "retryable": True,
                "retryAfterMs": 500,
                "details": {"reason": "startup-sidecars"},
            }
        }
    )
    assert error.retryable
    assert error.reason == "startup-sidecars"
    assert error.retry_after_ms == 500


@pytest.mark.asyncio
async def test_mux_close_fails_pending_requests() -> None:
    mux = _FrameMux()
    future = mux.wait("1")
    mux.fail_open(OpenClawGatewayError("disconnected", retryable=True))
    assert future.done()
    with pytest.raises(OpenClawGatewayError, match="disconnected"):
        future.result()
    assert mux.events().get_nowait()["event"] == "_disconnect"


@pytest.mark.asyncio
async def test_auth_failure_does_not_switch_to_another_runtime() -> None:
    fake = FakeGatewaySession(reject_connect=True)
    fallback = _FallbackAgent()
    client = OpenClawGatewayClient(
        session=fake,
        token="bad-token",
        fallback_factory=lambda: fallback,
    )
    with pytest.raises(OpenClawGatewayError, match="AUTH_TOKEN_MISMATCH"):
        await client.respond("hello")
    assert fallback.warmed is False
    assert not any(method == "chat.send" for method, _ in fake.calls)
    await client.close()


@pytest.mark.asyncio
async def test_timeout_on_send_retries_inflight_once() -> None:
    fake = FakeGatewaySession(fail_first_send_timeout=True)
    client = _client(fake, session_key="s1")
    result = await client.respond("hello")
    assert result == "Bitcoin is up. It gained 2%."
    assert len(fake.send_keys) == 2
    assert fake.send_keys[0] == fake.send_keys[1]
    await client.close()


@pytest.mark.asyncio
async def test_request_wraps_connection_error() -> None:
    class _ClosedWS:
        closed = False

        async def send_str(self, value: str) -> None:
            del value
            raise aiohttp.ClientConnectionError("write failed")

    session = AiohttpGatewaySession("ws://example")
    session._ws = _ClosedWS()  # type: ignore[assignment]
    with pytest.raises(OpenClawGatewayError, match="write failed") as caught:
        await session.request("chat.send", {"idempotencyKey": "k"})
    assert caught.value.retryable is True


@pytest.mark.asyncio
async def test_disconnect_before_challenge_fails_fast() -> None:
    fake = FakeGatewaySession(disconnect_before_challenge=True)
    fallback = _FallbackAgent()
    client = OpenClawGatewayClient(
        session=fake,
        token="test-token",
        fallback_factory=lambda: fallback,
    )
    started = asyncio.get_running_loop().time()
    result = await client.respond("hello")
    elapsed = asyncio.get_running_loop().time() - started
    assert result == "ACP fallback."
    assert elapsed < 2.0
    assert client.fallback_reason is not None
    assert "disconnect" in client.fallback_reason
    assert fallback.warmed is True
    await client.close()


@pytest.mark.asyncio
async def test_disconnect_during_handshake_falls_back() -> None:
    fake = FakeGatewaySession(fail_connect=True)
    fallback = _FallbackAgent()
    client = OpenClawGatewayClient(
        session=fake,
        token="test-token",
        fallback_factory=lambda: fallback,
    )
    result = await client.respond("hello")
    assert result == "ACP fallback."
    assert client.fallback_reason is not None
    assert "disconnect" in client.fallback_reason
    await client.close()


@pytest.mark.asyncio
async def test_protocol_mismatch_falls_back() -> None:
    fake = FakeGatewaySession(protocol=3)
    fallback = _FallbackAgent()
    client = OpenClawGatewayClient(
        session=fake,
        token="test-token",
        fallback_factory=lambda: fallback,
    )
    result = await client.respond("hello")
    assert result == "ACP fallback."
    assert any(name == "connect" for name, _params in fake.calls)
    assert client.fallback_reason is not None
    assert "protocol" in client.fallback_reason
    await client.close()


@pytest.mark.asyncio
async def test_steer_and_permissions_conform() -> None:
    fake = FakeGatewaySession()
    client = _client(fake, session_key="s1")
    await case_permissions(client)
    await case_steering(client)
    await case_tool_lifecycle(client)
    await case_stream_yields_sentence(client)
    await client.close()


@pytest.mark.asyncio
async def test_steer_rejected_when_not_advertised() -> None:
    fake = FakeGatewaySession(methods=("chat.send", "chat.abort", "sessions.resolve"))
    client = _client(fake, session_key="s1")
    await client.warmup()
    result = await adapter_control(client, Steer(instruction="be brief"))
    assert isinstance(result, ControlRejected)
    await client.close()


@pytest.mark.asyncio
async def test_gateway_error_raises() -> None:
    fake = FakeGatewaySession()
    client = _client(fake)
    with pytest.raises(OpenClawGatewayError, match="boom"):
        await client.respond("fail")
    await client.close()


def test_ws_url_rewrites_http() -> None:
    assert _ws_url("http://host:18789") == "ws://host:18789"
    assert _ws_url("https://host:18789") == "wss://host:18789"
    assert _ws_url("ws://127.0.0.1:18789") == "ws://127.0.0.1:18789"


def test_manifest_is_honest() -> None:
    manifest = adapter_capabilities(OpenClawGatewayClient(token="t"))
    assert manifest.structured_events.level is SupportLevel.SUPPORTED
    assert manifest.tool_lifecycle.level is SupportLevel.SUPPORTED
    assert manifest.permissions.level is SupportLevel.SUPPORTED
    assert manifest.session_identity.level is SupportLevel.SUPPORTED
    assert manifest.targeted_cancellation.level is SupportLevel.SUPPORTED
    assert manifest.clarification.level is SupportLevel.UNSUPPORTED
    assert manifest.usage.level is SupportLevel.UNSUPPORTED
    assert manifest.status.level is SupportLevel.UNSUPPORTED
    assert manifest.workers.level is SupportLevel.UNSUPPORTED
    assert manifest.voice_sessions.level is SupportLevel.UNSUPPORTED


def test_adapter_does_not_own_voice_policy() -> None:
    case_adapter_does_not_own_voice_policy(
        OpenClawGatewayClient(token="t"), sentences=["Bitcoin is up."], events=[]
    )


def test_builder_reads_gateway_url_and_token() -> None:
    from sleeptalk.config import AgentConfig
    from sleeptalk.defaults import _build_openclaw_agent

    config = AgentConfig(slug="bot", gateway_session_id="kept")
    agent = _build_openclaw_agent(config, {"OPENCLAW_GATEWAY_TOKEN": "secret"})
    assert isinstance(agent, OpenClawGatewayClient)
    assert agent.url == DEFAULT_GATEWAY_URL
    assert agent.token == "secret"
    assert agent.session_key == "kept"
    assert agent.password == ""

    password_agent = _build_openclaw_agent(config, {"OPENCLAW_GATEWAY_PASSWORD": "pw"})
    assert password_agent.password == "pw"
    assert password_agent.token == ""


def test_gateway_url_skips_openclaw_binary() -> None:
    from sleeptalk.config import AgentConfig
    from sleeptalk.defaults import register_defaults
    from sleeptalk.registries import AGENTS

    register_defaults()
    registration = AGENTS.get("openclaw")
    assert registration.binary_for(AgentConfig()) == "openclaw"
    assert registration.binary_for(AgentConfig(gateway_url="ws://127.0.0.1:18789")) == ""


def test_gateway_url_requires_token_env() -> None:
    from sleeptalk.config import AgentConfig, SleeptalkConfig, collect_missing_env
    from sleeptalk.defaults import register_defaults

    register_defaults()
    config = SleeptalkConfig(
        agent=AgentConfig(type="openclaw", gateway_url="ws://127.0.0.1:18789")
    )
    missing = collect_missing_env(config, {})
    assert any("OPENCLAW_GATEWAY_TOKEN" in item for item in missing)
    present = collect_missing_env(config, {"OPENCLAW_GATEWAY_TOKEN": "secret"})
    assert not any("OPENCLAW_GATEWAY_TOKEN" in item for item in present)


async def _wait_for_permission(events: list[object]):
    async def _poll():
        while True:
            found = next((event for event in events if isinstance(event, PermissionRequested)), None)
            if found is not None:
                return found
            await asyncio.sleep(0.02)

    return await asyncio.wait_for(_poll(), timeout=3.0)


@pytest.mark.asyncio
async def test_a_second_call_on_the_same_profile_gets_its_own_session() -> None:
    """The gateway keeps sessions by label; the profile's earlier call held
    "synthetic-qa", so every later call failed ("label already in use")."""
    fake = FakeGatewaySession(labels_in_use=("synthetic-qa",))
    client = _client(fake, name="synthetic-qa")
    assert await client.respond("price?") == "Bitcoin is up. It gained 2%."
    labels = [params["label"] for name, params in fake.calls if name == "sessions.create"]
    assert labels[0] == "synthetic-qa" and labels[1].startswith("synthetic-qa ")
    await client.close()
