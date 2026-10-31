import asyncio
import json

import pytest
from aiortc import MediaStreamError
from av import AudioFrame
from sleeptalk.call_provider import (
    BidirectionalAudioStream,
    CallContext,
    CallEnded,
    CallProvider,
    CallProviderError,
    P2PCallProvider,
    QueuedAudioTrack,
    _parse_browser_telemetry,
    _parse_mint_response,
    _WebRTCAudioStream,
)
from sleeptalk.cli import EXIT_CONFIG, main


def mint_response(**overrides):
    token = "a" * 43
    routing_id = "r" * 24
    value = {
        "ticket": token,
        "routingId": routing_id,
        "joinUrl": f"https://calls.example.test/#/call/{routing_id}/{token}",
        "agentSignalUrl": f"wss://calls.example.test/api/tickets/{routing_id}/signal",
        "agentAccessKey": "host-secret",
        "expiresAt": "2026-08-15T12:05:00.000Z",
        "sessionCapSeconds": 1800,
        "inactivitySeconds": 90,
        "iceServers": [
            {"urls": ["stun:stun.cloudflare.com:3478"]},
            {
                "urls": ["turns:turn.cloudflare.com:443?transport=tcp"],
                "username": "ephemeral-user",
                "credential": "ephemeral-secret",
            },
        ],
    }
    value.update(overrides)
    return json.dumps(value)


def test_call_context_serializes_server_side_identifiers():
    context = CallContext(" campbell ", " telegram:chat-1 ", " telegram:user-2 ")

    assert context.as_payload() == {
        "agentInstance": "campbell",
        "conversationId": "telegram:chat-1",
        "platformUserId": "telegram:user-2",
    }


@pytest.mark.parametrize("field", ["agent_instance", "conversation_id", "platform_user_id"])
def test_call_context_rejects_missing_identifiers(field):
    values = {
        "agent_instance": "campbell",
        "conversation_id": "chat-1",
        "platform_user_id": "user-1",
    }
    values[field] = ""

    with pytest.raises(CallProviderError, match="between 1 and 256"):
        CallContext(**values).as_payload()


def test_mint_response_keeps_context_out_of_public_url():
    ticket = _parse_mint_response(
        mint_response(),
        "https://calls.example.test",
    )

    assert ticket.join_url == (
        f"https://calls.example.test/#/call/{ticket.routing_id}/{ticket.token}"
    )
    assert "conversation" not in ticket.join_url
    assert "host-secret" not in ticket.join_url
    assert "host-secret" not in ticket.agent_signal_url
    assert ticket.agent_access_key == "host-secret"
    assert "?" not in ticket.agent_signal_url
    assert ticket.session_cap_seconds == 1800
    assert ticket.inactivity_seconds == 90
    assert ticket.ice_servers[1].username == "ephemeral-user"
    assert ticket.ice_servers[1].credential == "ephemeral-secret"
    assert ticket.signal_types == frozenset()


def test_mint_response_advertises_presentation_when_the_worker_lists_it():
    ticket = _parse_mint_response(
        mint_response(signalTypes=["call.agent_state", "call.presentation"]),
        "https://calls.example.test",
    )

    assert "call.presentation" in ticket.signal_types
    assert "call.agent_state" in ticket.signal_types


@pytest.mark.asyncio
async def test_transcripts_never_go_through_the_signaling_relay():
    class Probe(_WebRTCAudioStream):
        def __init__(self) -> None:
            super().__init__(
                "wss://calls.example.test/api/tickets/routing/signal",
                (),
                signal_types=frozenset({"call.presentation", "call.agent_state"}),
            )
            self.signaled: list[str] = []

        async def _send_signal(self, message_type: str, _payload: dict) -> None:
            self.signaled.append(message_type)

    stream = Probe()
    try:
        await stream.send_presentation({"presence": "thinking", "lines": [], "muted": False})
        await stream.send_agent_state("working", "Reading .env")
        assert stream.signaled == []
        # Held, latest per type, until the page's data channel opens.
        assert set(stream._display_pending) == {"call.presentation", "call.agent_state"}
    finally:
        await stream.peer.close()


@pytest.mark.asyncio
async def test_display_mirrors_arrive_on_the_pages_data_channel():
    from aiortc import RTCPeerConnection

    from sleeptalk.call_provider import DISPLAY_CHANNEL_LABEL

    stream = _WebRTCAudioStream("wss://calls.example.test/x/signal", ())
    page = RTCPeerConnection()
    received: asyncio.Queue[str] = asyncio.Queue()
    try:
        await stream.send_agent_state("thinking", "before open")
        channel = page.createDataChannel(DISPLAY_CHANNEL_LABEL)
        channel.on("message", received.put_nowait)
        page.addTransceiver("audio", direction="recvonly")
        await page.setLocalDescription(await page.createOffer())
        await stream.peer.setRemoteDescription(page.localDescription)
        await stream.peer.setLocalDescription(await stream.peer.createAnswer())
        await page.setRemoteDescription(stream.peer.localDescription)

        first = json.loads(await asyncio.wait_for(received.get(), 10))
        assert first["type"] == "call.agent_state"
        assert first["payload"]["task"] == "before open"

        await stream.send_presentation({"presence": "speaking", "lines": [{"text": "hi"}]})
        second = json.loads(await asyncio.wait_for(received.get(), 10))
        assert second["type"] == "call.presentation"
        assert second["payload"]["lines"] == [{"text": "hi"}]

        # A long call's snapshot outgrows the channel: its newest part is
        # sent, not nothing (the page froze for the rest of the call).
        lines = [{"kind": "user", "text": f"line {n} " + "x" * 500} for n in range(300)]
        await stream.send_presentation({"presence": "idle", "lines": lines})
        raw = await asyncio.wait_for(received.get(), 10)
        third = json.loads(raw)
        assert len(raw.encode()) <= 64 * 1024
        assert third["payload"]["lines"][-1] == lines[-1]
        assert len(third["payload"]["lines"]) < len(lines)
    finally:
        await page.close()
        await stream.peer.close()


@pytest.mark.asyncio
async def test_interrupt_cuts_the_turn_without_ending_the_call():
    stream = _WebRTCAudioStream(
        "wss://calls.example.test/api/tickets/routing/signal",
        (),
    )
    stream._websocket = object()
    cuts: list[str] = []

    async def cut() -> None:
        cuts.append("cut")

    stream.set_interrupt_handler(cut)
    await stream._handle_signal({"type": "call.interrupt", "payload": {}})
    assert cuts == ["cut"]
    assert not stream._closed.is_set()


@pytest.mark.asyncio
async def test_ice_failed_does_not_end_the_call_immediately():
    stream = _WebRTCAudioStream(
        "wss://calls.example.test/api/tickets/routing/signal",
        (),
    )
    stream._ice_recovery_seconds = 10
    stream._handle_connection_state("failed")
    assert not stream._closed.is_set()
    stream._cancel_ice_recovery()
    await stream.close()


@pytest.mark.asyncio
async def test_ice_failed_ends_the_call_after_recovery_timeout():
    stream = _WebRTCAudioStream(
        "wss://calls.example.test/api/tickets/routing/signal",
        (),
    )
    stream._ice_recovery_seconds = 0.01
    stream._handle_connection_state("failed")
    await asyncio.sleep(0.05)
    assert stream._closed.is_set()
    assert stream._end_reason == "connection_lost"
    await stream.close()


@pytest.mark.asyncio
async def test_ice_connected_cancels_recovery():
    stream = _WebRTCAudioStream(
        "wss://calls.example.test/api/tickets/routing/signal",
        (),
    )
    stream._ice_recovery_seconds = 0.05
    stream._handle_connection_state("failed")
    stream._handle_connection_state("connected")
    await asyncio.sleep(0.08)
    assert not stream._closed.is_set()
    await stream.close()


@pytest.mark.asyncio
async def test_recv_keeps_a_replacement_track_after_media_error():
    stream = _WebRTCAudioStream(
        "wss://calls.example.test/api/tickets/routing/signal",
        (),
    )
    loop = asyncio.get_running_loop()

    class DeadTrack:
        kind = "audio"

        async def recv(self):
            await asyncio.sleep(0.02)
            raise MediaStreamError("ended")

    class LiveTrack:
        kind = "audio"

        async def recv(self):
            return AudioFrame(format="s16", layout="mono", samples=960)

    dead = DeadTrack()
    live = LiveTrack()
    stream._remote_track = loop.create_future()
    stream._remote_track.set_result(dead)
    task = asyncio.create_task(stream.recv())
    await asyncio.sleep(0.005)
    stream._accept_remote_track(live)
    frame = await task
    assert isinstance(frame, AudioFrame)
    stream._cancel_ice_recovery()
    await stream.close()


@pytest.mark.asyncio
async def test_ice_closed_still_ends_the_call():
    stream = _WebRTCAudioStream(
        "wss://calls.example.test/api/tickets/routing/signal",
        (),
    )
    stream._handle_connection_state("closed")
    assert stream._closed.is_set()
    assert stream._end_reason == "connection_lost"
    await stream.close()


def test_mint_response_rejects_unauthenticated_turn_servers():
    ticket = _parse_mint_response(
        mint_response(
            iceServers=[
                {"urls": ["turn:turn.cloudflare.com:3478?transport=udp"]}
            ]
        ),
        "https://calls.example.test",
    )

    assert ticket.ice_servers[0].urls == ("stun:stun.cloudflare.com:3478",)


def test_mint_response_rejects_cross_origin_signal_url():
    with pytest.raises(CallProviderError, match="invalid ticket URL"):
        _parse_mint_response(
            mint_response(
                agentSignalUrl="wss://attacker.example/api/tickets/routing-id/signal"
            ),
            "https://calls.example.test",
        )


def test_p2p_provider_satisfies_call_provider_seam():
    provider = P2PCallProvider("https://calls.example.test")

    assert isinstance(provider, CallProvider)


def test_queued_audio_track_is_bidirectional_stream_output():
    async def exercise():
        track = QueuedAudioTrack()
        frame = AudioFrame(format="s16", layout="mono", samples=960)
        await track.send(frame)
        assert await track.recv() is frame
        track.stop()
        with pytest.raises(MediaStreamError):
            await track.recv()

    asyncio.run(exercise())


def test_queued_audio_track_clear_drops_buffered_frames():
    async def exercise():
        track = QueuedAudioTrack()
        await track.send(AudioFrame(format="s16", layout="mono", samples=960))
        await track.send(AudioFrame(format="s16", layout="mono", samples=960))

        assert track.clear() == 2

        track.stop()
        with pytest.raises(MediaStreamError):
            await track.recv()

    asyncio.run(exercise())


def test_queued_audio_track_keeps_media_clock_alive_with_silence():
    async def exercise():
        track = QueuedAudioTrack()
        silence = await track.recv()

        assert silence.sample_rate == 48_000
        assert silence.samples == 960
        assert silence.layout.name == "stereo"
        assert bytes(silence.planes[0]) == bytes(silence.planes[0].buffer_size)
        assert silence.pts == 0

    asyncio.run(exercise())


def test_bidirectional_audio_protocol_is_runtime_checkable():
    class FakeStream:
        async def recv(self):
            return AudioFrame(format="s16", layout="mono", samples=960)

        async def send(self, _frame):
            return None

        def clear_output(self):
            return 0

        async def wait_closed(self):
            return "peer_hung_up"

    assert isinstance(FakeStream(), BidirectionalAudioStream)


def test_browser_transport_telemetry_is_bounded_and_sanitized():
    parsed = _parse_browser_telemetry(
        {
            "stage": "transport_stats",
            "browserElapsedMs": 1234.56789,
            "usesRelay": True,
            "currentRttMs": 84.25,
            "candidatePairId": "pair-1",
            "ignoredNestedValue": {"secret": "nope"},
        }
    )

    assert parsed == {
        "stage": "transport_stats",
        "browserElapsedMs": 1234.568,
        "usesRelay": True,
        "currentRttMs": 84.25,
        "candidatePairId": "pair-1",
    }
    assert _parse_browser_telemetry({"stage": "admin_delete"}) is None


class FakeSocket:
    def __init__(self):
        self.closed = False
        self.messages = []
        self.message_sent = asyncio.Event()

    async def send_json(self, message):
        self.messages.append(message)
        self.message_sent.set()


class FakeOutgoing:
    def __init__(self):
        self.frames = []

    async def send(self, frame):
        self.frames.append(frame)


def bare_stream():
    stream = object.__new__(_WebRTCAudioStream)
    stream._closed = asyncio.Event()
    stream._end_reason = "call_ended"
    stream._websocket = FakeSocket()
    stream._remote_track = asyncio.get_running_loop().create_future()
    stream.outgoing = FakeOutgoing()
    return stream


@pytest.mark.asyncio
async def test_bearer_ticket_immediately_allows_outbound_audio():
    stream = bare_stream()
    frame = AudioFrame(format="s16", layout="mono", samples=960)
    await stream.send(frame)
    assert stream.outgoing.frames == [frame]


def test_link_cli_rejects_ttl_over_five_minutes(capsys):
    result = main(["link", "--ttl-seconds", "301"])

    assert result == EXIT_CONFIG
    assert "between 1 and 300 seconds" in capsys.readouterr().err


def test_link_cli_uses_profile_session_and_returns_detached_url(
    monkeypatch, capsys, tmp_path
):
    captured = {}
    profile = tmp_path / "profiles" / "d3gen.toml"
    profile.parent.mkdir(parents=True)
    profile.write_text(
        '[agent]\ntype = "pi"\nslug = "d3gen"\n',
        encoding="utf-8",
    )

    def launch(request):
        captured["request"] = request
        return "https://calls.example.test/#/call/opaque"

    monkeypatch.setattr("sleeptalk.host.launch_detached_call", launch)

    result = main(
        [
            "link",
            "--config",
            str(profile),
            "--agent-instance",
            "d3gen",
        ]
    )

    assert result == 0
    assert capsys.readouterr().out.strip() == "https://calls.example.test/#/call/opaque"
    assert captured["request"].context.conversation_id == ""
    assert captured["request"].config_path == str(profile.resolve())



# ---------------------------------------------------------------------------
# Signaling loss: attribution and bounded reconnect
# ---------------------------------------------------------------------------


class ScriptedSocket:
    """A signaling socket that replays scripted messages, then closes."""

    def __init__(self, messages, *, close_code=None):
        import aiohttp

        self._messages = list(messages)
        self.closed = False
        self.close_code = close_code
        self.sent = []
        self._closed_msg = aiohttp.WSMessage(aiohttp.WSMsgType.CLOSED, None, None)

    async def receive(self):
        if self._messages:
            message = self._messages.pop(0)
            if message.type.name in {"CLOSE", "CLOSED", "ERROR"}:
                self.closed = True
            return message
        self.closed = True
        return self._closed_msg

    async def send_json(self, message):
        if self.closed:
            raise ConnectionError("socket closed")
        self.sent.append(message)

    async def close(self):
        self.closed = True


class ScriptedSession:
    def __init__(self, sockets):
        self._sockets = list(sockets)
        self.connect_calls = 0
        self.closed = False

    async def ws_connect(self, url, heartbeat=None):
        import aiohttp

        self.connect_calls += 1
        if not self._sockets:
            raise aiohttp.ClientConnectionError("rendezvous unreachable")
        nxt = self._sockets.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def text(message_type, payload):
    import aiohttp

    return aiohttp.WSMessage(
        aiohttp.WSMsgType.TEXT,
        json.dumps({"v": 1, "id": "m1", "type": message_type, "payload": payload}),
        None,
    )


def close_frame(code, reason=""):
    import aiohttp

    return aiohttp.WSMessage(aiohttp.WSMsgType.CLOSE, code, reason)


def signaling_stream(first_socket, session):
    stream = object.__new__(_WebRTCAudioStream)
    stream._closed = asyncio.Event()
    stream._end_reason = "call_ended"
    stream._websocket = first_socket
    stream._session = session
    stream.signal_url = "wss://calls.example.test/api/tickets/r/signal"
    stream.role = "agent"
    stream.access_key = "host-secret"
    stream.end_detail = ""
    stream.signal_reconnects = 0
    stream._closing_by_us = False
    stream._reconnect_base_delay_s = 0.0
    stream._display_channel = None
    stream._display_pending = {}
    return stream


@pytest.mark.asyncio
async def test_server_close_that_ends_the_ticket_is_final_and_recorded():
    # The worker closes with 4004 when the ticket is already over. That is not
    # a hangup and not worth retrying; the record must keep the code.
    session = ScriptedSession([])
    stream = signaling_stream(ScriptedSocket([close_frame(4004, "Call ended.")]), session)

    await stream._run_signaling()

    assert stream._closed.is_set()
    assert stream._end_reason == "signal_closed"
    assert "4004" in stream.end_detail and "Call ended." in stream.end_detail
    assert session.connect_calls == 0
    assert await stream.wait_closed() == "signal_closed"


@pytest.mark.asyncio
async def test_dropped_signaling_reconnects_and_keeps_the_call():
    # Connection lost without a close frame: re-open, re-auth with the same
    # key, and carry on. The peer's later hangup arrives on the new socket.
    replacement = ScriptedSocket(
        [text("pong", {}), text("call.ended", {"reason": "peer_hung_up"})]
    )
    session = ScriptedSession([replacement])
    stream = signaling_stream(ScriptedSocket([]), session)

    await stream._run_signaling()

    assert session.connect_calls == 1
    assert stream.signal_reconnects == 1
    assert replacement.sent[0]["type"] == "call.auth"
    assert replacement.sent[0]["payload"] == {"role": "agent", "key": "host-secret"}
    assert stream._end_reason == "peer_hung_up"
    assert stream.end_detail == ""


@pytest.mark.asyncio
async def test_exhausted_reconnect_reports_signal_lost_with_detail():
    session = ScriptedSession([])
    stream = signaling_stream(ScriptedSocket([]), session)

    await stream._run_signaling()

    assert session.connect_calls == 3
    assert stream._end_reason == "signal_lost"
    assert "without a close frame" in stream.end_detail
    assert "gave up after 3" in stream.end_detail
    assert stream._closed.is_set()


@pytest.mark.asyncio
async def test_clean_server_close_without_envelope_is_not_a_hangup():
    # A 1000 close with no call.ended first: retry, and if the worker refuses
    # every time, say the server closed it rather than the placeholder.
    session = ScriptedSession([])
    stream = signaling_stream(ScriptedSocket([close_frame(1000, "Call ended.")]), session)

    await stream._run_signaling()

    assert stream._end_reason == "signal_closed"
    assert "code 1000" in stream.end_detail
    assert stream._end_reason != "call_ended"


@pytest.mark.asyncio
async def test_display_sends_are_held_until_the_channel_opens_and_fail_once_closed():
    stream = signaling_stream(ScriptedSocket([]), ScriptedSession([]))
    stream._websocket.closed = True

    await stream.send_agent_state("working")  # held for the data channel, no exception
    assert stream._display_pending["call.agent_state"]["state"] == "working"

    stream._closed.set()
    with pytest.raises(CallEnded):
        await stream.send_agent_state("working")


@pytest.mark.asyncio
async def test_failed_heartbeat_ping_does_not_end_the_call(monkeypatch):
    monkeypatch.setattr("sleeptalk.call_provider._HEARTBEAT_SECONDS", 0.001)
    socket = ScriptedSocket([])
    socket.closed = False

    async def boom(message):
        raise ConnectionError("mid-swap")

    socket.send_json = boom
    stream = signaling_stream(socket, ScriptedSession([]))

    task = asyncio.create_task(stream._heartbeat())
    await asyncio.sleep(0.02)
    assert stream._closed.is_set() is False
    stream._closed.set()
    await asyncio.wait_for(task, timeout=1)


@pytest.mark.asyncio
async def test_host_takes_turn_credentials_from_redemption_before_negotiating():
    from aiortc import RTCPeerConnection

    stream = _WebRTCAudioStream("wss://calls.example.test/x/signal", ())
    stream._websocket = object()  # _handle_signal only needs a live socket
    first = stream.peer
    turn = {
        "urls": ["turn:turn.cloudflare.com:3478?transport=udp"],
        "username": "redeem-user",
        "credential": "redeem-secret",
    }
    page = RTCPeerConnection()
    try:
        await stream._handle_signal(
            {"type": "ticket.redeemed", "payload": {"iceServers": [turn]}}
        )
        assert stream.peer is not first
        servers = stream.peer._RTCPeerConnection__configuration.iceServers
        assert [server.username for server in servers] == ["redeem-user"]
        assert not stream._closed.is_set(), "retiring the old peer must not end the call"

        # Once the browser's offer is in, a replayed redemption changes nothing.
        page.addTransceiver("audio")
        await page.setLocalDescription(await page.createOffer())
        await stream.peer.setRemoteDescription(page.localDescription)
        negotiated = stream.peer
        await stream._handle_signal(
            {"type": "ticket.redeemed", "payload": {"iceServers": [turn]}}
        )
        assert stream.peer is negotiated

        # The package's local rendezvous sends no ICE servers: nothing to adopt.
        await stream._handle_signal({"type": "ticket.redeemed", "payload": {}})
        assert stream.peer is negotiated
    finally:
        await page.close()
        await stream.peer.close()


@pytest.mark.parametrize("url", ["http://127.0.0.1:8788", "http://localhost:8788", "http://[::1]:8788",
                                 "https://call.sleep.talk"])
def test_rendezvous_accepts_https_or_this_machine(url):
    from sleeptalk.call_provider import _validated_rendezvous

    assert _validated_rendezvous(url) == url


@pytest.mark.parametrize("url", ["http://call.sleep.talk", "http://192.168.1.20:8788", "http://127.0.0.1.evil.example"])
def test_plain_http_rendezvous_off_this_machine_is_refused(url):
    from sleeptalk.call_provider import _validated_rendezvous

    with pytest.raises(CallProviderError, match="https://"):
        _validated_rendezvous(url)
