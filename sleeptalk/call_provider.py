"""Call-provider seam and the v1 peer-to-peer WebRTC implementation.

The provider owns rendezvous/ticket mechanics and yields a transport-neutral
bidirectional stream of decoded audio frames. The voice runtime can consume
and produce those frames without knowing whether the provider is P2P WebRTC
or a future managed room adapter.
"""

from __future__ import annotations

import asyncio
import fractions
import inspect
import hashlib
import ipaddress
import json
import logging
import math
import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import aiohttp
from aiortc import (
    AudioStreamTrack,
    MediaStreamError,
    MediaStreamTrack,
    RTCConfiguration,
    RTCIceServer,
    RTCPeerConnection,
    RTCRtpSender,
    RTCSessionDescription,
)
from aiortc.sdp import candidate_from_sdp
from av import AudioFrame

from .presentation import fit_snapshot
from .service_grant import ServiceGrant, ServiceGrantError, ServiceGrantStore

logger = logging.getLogger(__name__)

SIGNAL_VERSION = 1
RENDEZVOUS_URL_ENV = "SLEEPTALK_RENDEZVOUS_URL"
HOSTED_RENDEZVOUS_URL = "https://call.sleep.talk"
LOCAL_RENDEZVOUS_URL = "http://127.0.0.1:8788"
DEFAULT_RENDEZVOUS_URL = HOSTED_RENDEZVOUS_URL
DEFAULT_TICKET_TTL_SECONDS = 5 * 60
SETUP_TICKET_TTL_SECONDS = 10 * 60
_HEARTBEAT_SECONDS = 15
# Bounded signaling reconnect, mirroring the browser's policy in
# rendezvous/web/reconnect.ts: 3 attempts at 1s, 2s, 4s. Media is a separate
# peer-to-peer path, so a signaling blip must not end a call whose audio is
# still flowing.
_SIGNAL_RECONNECT_MAX_ATTEMPTS = 3
_SIGNAL_RECONNECT_BASE_DELAY_S = 1.0
# Worker close codes that mean "do not come back": bad credential, ticket ended.
_SIGNAL_FINAL_CLOSE_CODES = frozenset({4003, 4004})
_ICE_RECOVERY_SECONDS = 10.0
# The page opens this data channel for transcript and agent-state mirrors
# (rendezvous/web/main.ts). Same bound the relay put on a signal message.
DISPLAY_CHANNEL_LABEL = "sleeptalk-display"
_MAX_DISPLAY_BYTES = 64 * 1024
# Room for the envelope around a fitted snapshot.
_DISPLAY_ENVELOPE_BYTES = 512
_MAX_ICE_RECOVERIES = 2
_CLOSE_SENTINEL = object()
_TELEMETRY_CLOSE_SENTINEL = object()
_BROWSER_TELEMETRY_STAGES = {
    "ticket_redeemed",
    "talk_tapped",
    "microphone_ready",
    "signal_connected",
    "peer_ready_received",
    "offer_sent",
    "answer_received",
    "remote_track",
    "remote_audio_playing",
    "ice_state",
    "ice_restart",
    "connection_state",
    "peer_connected",
    "transport_stats",
    "transport_stats_error",
    "call_ended",
}
_TELEMETRY_FIELDS = {
    "browserElapsedMs",
    "permissionMs",
    "iceState",
    "connectionState",
    "reason",
    "attempt",
    "error",
    "usesRelay",
    "candidatePairId",
    "localCandidateType",
    "remoteCandidateType",
    "protocol",
    "relayProtocol",
    "currentRttMs",
    "availableOutgoingBitrateKbps",
    "inboundAudioJitterMs",
    "jitterBufferMs",
    "packetsLost",
    "packetsReceived",
    "packetsSent",
    "bytesReceived",
    "bytesSent",
    "concealedSamples",
}


class CallProviderError(RuntimeError):
    """Human-readable call-provider failure."""


class CallEnded(CallProviderError):
    """The remote call ended while audio was pending."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True, slots=True)
class CallContext:
    """Server-side identifiers attached to a minted call ticket."""

    agent_instance: str
    conversation_id: str
    platform_user_id: str

    def as_payload(self) -> dict[str, str]:
        return {
            "agentInstance": _bounded_identifier("agent instance", self.agent_instance),
            "conversationId": _bounded_identifier("conversation ID", self.conversation_id),
            "platformUserId": _bounded_identifier("platform user ID", self.platform_user_id),
        }


@dataclass(frozen=True, slots=True)
class CallTicket:
    """Mint result; only join_url is safe to send to the end user."""

    token: str
    routing_id: str
    join_url: str
    agent_signal_url: str
    agent_access_key: str
    expires_at: str
    session_cap_seconds: int
    inactivity_seconds: int
    ice_servers: tuple[IceServerConfig, ...]
    signal_types: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class IceServerConfig:
    urls: tuple[str, ...]
    username: str = ""
    credential: str = ""


@runtime_checkable
class BidirectionalAudioStream(Protocol):
    """Decoded audio in and decoded audio out, independent of call provider."""

    async def recv(self) -> AudioFrame: ...

    async def send(self, frame: AudioFrame) -> None: ...

    def clear_output(self) -> int: ...

    async def wait_closed(self) -> str: ...


@runtime_checkable
class CallProvider(Protocol):
    """Ticket in, connected bidirectional audio stream out."""

    async def mint(
        self,
        context: CallContext,
        *,
        ttl_seconds: int = DEFAULT_TICKET_TTL_SECONDS,
    ) -> CallTicket: ...

    def connect(
        self, ticket: CallTicket
    ) -> AbstractAsyncContextManager[BidirectionalAudioStream]: ...


@dataclass(frozen=True)
class _TrackedAudioFrame:
    frame: AudioFrame
    consumed: asyncio.Future[bool]


class QueuedAudioTrack(AudioStreamTrack):
    """Paced 48 kHz output that emits silence whenever runtime audio is idle.

    A WebRTC sender must keep its media clock moving between bot utterances.
    Blocking ``recv()`` on an empty queue made browsers conceal the missing
    packets and grow their jitter buffer to ~780 ms, delaying the next ack and
    leaving stale answer audio audible after barge-in.
    """

    kind = "audio"

    def __init__(self, *, max_frames: int = 16) -> None:
        super().__init__()
        self._frames: asyncio.Queue[AudioFrame | object] = asyncio.Queue(max_frames)
        self._receipts: set[asyncio.Future[bool]] = set()

    async def recv(self) -> AudioFrame:
        if self.readyState != "live":
            raise MediaStreamError
        sample_rate = 48_000
        samples = 960
        if hasattr(self, "_timestamp"):
            self._timestamp += samples
            wait = self._start + self._timestamp / sample_rate - time.time()
            if wait > 0:
                await asyncio.sleep(wait)
        else:
            self._start = time.time()
            self._timestamp = 0

        if self.readyState != "live":
            raise MediaStreamError
        frame = self._frames.get_nowait() if not self._frames.empty() else None
        while isinstance(frame, _TrackedAudioFrame) and (
            frame.consumed.cancelled()
            or (frame.consumed.done() and not frame.consumed.result())
        ):
            frame = self._frames.get_nowait() if not self._frames.empty() else None
        receipt = frame.consumed if isinstance(frame, _TrackedAudioFrame) else None
        if isinstance(frame, _TrackedAudioFrame):
            frame = frame.frame
        if frame is _CLOSE_SENTINEL:
            raise MediaStreamError
        if frame is None:
            frame = AudioFrame(format="s16", layout="stereo", samples=samples)
            frame.planes[0].update(bytes(frame.planes[0].buffer_size))
        frame.pts = self._timestamp
        frame.sample_rate = sample_rate
        frame.time_base = fractions.Fraction(1, sample_rate)
        if receipt is not None and not receipt.done():
            receipt.set_result(True)
        return frame  # type: ignore[return-value]

    async def send(self, frame: AudioFrame) -> None:
        await self._frames.put(frame)

    async def send_played(self, frame: AudioFrame) -> bool:
        """Wait until the paced sender consumes this frame, or discards it."""
        if self.readyState != "live":
            return False
        consumed = asyncio.get_running_loop().create_future()
        self._receipts.add(consumed)
        queued = asyncio.create_task(self._frames.put(_TrackedAudioFrame(frame, consumed)))
        try:
            await asyncio.wait((queued, consumed), return_when=asyncio.FIRST_COMPLETED)
            if self.readyState != "live" and not consumed.done():
                consumed.set_result(False)
            return await consumed
        finally:
            self._receipts.discard(consumed)
            if not consumed.done():
                consumed.cancel()
            if not queued.done():
                queued.cancel()
            await asyncio.gather(queued, return_exceptions=True)

    def clear(self) -> int:
        """Drop queued audio frames, preserving an already-enqueued close."""

        cleared = 0
        closed = False
        while not self._frames.empty():
            value = self._frames.get_nowait()
            if value is _CLOSE_SENTINEL:
                closed = True
            else:
                cleared += 1
                if isinstance(value, _TrackedAudioFrame) and not value.consumed.done():
                    value.consumed.set_result(False)
        if closed:
            self._frames.put_nowait(_CLOSE_SENTINEL)
        for consumed in self._receipts:
            if not consumed.done():
                consumed.set_result(False)
        return cleared

    def stop(self) -> None:
        if self.readyState == "ended":
            return
        self.clear()
        super().stop()


class P2PCallProvider:
    """Opaque tickets plus direct browser↔host Opus WebRTC audio."""

    def __init__(
        self,
        rendezvous_url: str = DEFAULT_RENDEZVOUS_URL,
        *,
        mint_token: str | None = None,
        grant_store: ServiceGrantStore | None = None,
    ) -> None:
        self.rendezvous_url = _validated_rendezvous(rendezvous_url)
        # An explicit token remains available only for a legacy self-hosted
        # service. Hosted access uses an origin-scoped installation grant.
        self.mint_token = mint_token.strip() if mint_token else None
        self.grant_store = grant_store or ServiceGrantStore()

    async def mint(
        self,
        context: CallContext,
        *,
        ttl_seconds: int = DEFAULT_TICKET_TTL_SECONDS,
    ) -> CallTicket:
        if not 1 <= ttl_seconds <= DEFAULT_TICKET_TTL_SECONDS:
            raise CallProviderError("Call links must expire between 1 and 300 seconds.")
        raw = await self._post_ticket({"ttlSeconds": ttl_seconds}, context)
        return _parse_mint_response(raw, self.rendezvous_url)

    async def mint_setup(self, *, ttl_seconds: int = SETUP_TICKET_TTL_SECONDS) -> CallTicket:
        """Mint a media-free setup link. Only hosted installation access can."""
        if not 1 <= ttl_seconds <= SETUP_TICKET_TTL_SECONDS:
            raise CallProviderError("Setup links must expire between 1 and 600 seconds.")
        if self.mint_token:
            raise CallProviderError("Setup links need the hosted call service, not a legacy self-hosted token.")
        raw = await self._post_ticket({"kind": "setup", "ttlSeconds": ttl_seconds}, None)
        return _parse_mint_response(raw, self.rendezvous_url, route="setup")

    async def _post_ticket(self, payload: dict[str, Any], context: CallContext | None) -> str:
        endpoint = _origin_endpoint(self.rendezvous_url, "/api/tickets")
        headers: dict[str, str] = {"Idempotency-Key": str(uuid4())}
        timeout = aiohttp.ClientTimeout(total=15)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                if self.mint_token and context is not None:
                    headers["Authorization"] = f"Bearer {self.mint_token}"
                    payload.update(context.as_payload())
                else:
                    grant = await self._installation_grant(session)
                    headers["Authorization"] = grant.authorization
                async with session.post(
                    endpoint,
                    json=payload,
                    headers=headers,
                    allow_redirects=False,
                ) as response:
                    raw = await response.text()
                    if response.status != 201:
                        raise CallProviderError(_service_error(response.status, raw))
        except ServiceGrantError as exc:
            raise CallProviderError(str(exc)) from exc
        except aiohttp.ClientError as exc:
            raise CallProviderError(
                "The call service is unreachable. Check the rendezvous URL and try again."
            ) from exc
        return raw

    async def _installation_grant(self, session: aiohttp.ClientSession) -> ServiceGrant:
        grant = self.grant_store.load_or_create_pending(self.rendezvous_url)
        if grant.enrolled:
            return grant
        endpoint = _origin_endpoint(self.rendezvous_url, "/api/installations/enroll")
        async with session.post(
            endpoint,
            json={
                "installationId": grant.installation_id,
                "secretHash": hashlib.sha256(grant.secret.encode("utf-8")).hexdigest(),
            },
            headers={"Idempotency-Key": grant.enrollment_request_id},
            allow_redirects=False,
        ) as response:
            raw = await response.text()
            if response.status not in {200, 201}:
                raise CallProviderError(_service_error(response.status, raw))
        return self.grant_store.mark_enrolled(grant)

    @asynccontextmanager
    async def connect(self, ticket: CallTicket) -> AsyncIterator[BidirectionalAudioStream]:
        stream = _WebRTCAudioStream(
            ticket.agent_signal_url,
            ticket.ice_servers,
            role="agent",
            access_key=ticket.agent_access_key,
            signal_types=ticket.signal_types,
        )
        await stream.open()
        try:
            yield stream
        finally:
            await stream.close()


class _WebRTCAudioStream:
    def __init__(
        self,
        signal_url: str,
        ice_servers: tuple[IceServerConfig, ...],
        *,
        role: str = "agent",
        access_key: str = "",
        signal_types: frozenset[str] = frozenset(),
    ) -> None:
        self.signal_url = signal_url
        self.role = role
        self.access_key = access_key
        self.signal_types = signal_types
        self.outgoing = QueuedAudioTrack()
        self._remote_track: asyncio.Future[MediaStreamTrack] | None = None
        self._closed = asyncio.Event()
        self._end_reason = "call_ended"
        self._session: aiohttp.ClientSession | None = None
        self._websocket: aiohttp.ClientWebSocketResponse | None = None
        self._signal_task: asyncio.Task[None] | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._pending_candidates: list[dict[str, Any] | None] = []
        self._telemetry: asyncio.Queue[dict[str, Any] | object] = asyncio.Queue(maxsize=128)
        self._latest_transport_stats: dict[str, Any] = {}
        self._on_interrupt: Callable[[], Awaitable[None] | None] | None = None
        self._on_settings: Callable[[dict[str, Any]], Awaitable[None] | None] | None = None
        self._ice_recovery_seconds = _ICE_RECOVERY_SECONDS
        self._ice_failures = 0
        self._ice_recovery_task: asyncio.Task[None] | None = None
        self._active_remote_track: MediaStreamTrack | None = None
        # Why the signaling socket went away, for the call_complete record:
        # close code and reason text, or the transport error. Empty while the
        # call is up or when the peer ended it with an explicit envelope.
        self.end_detail = ""
        self.signal_reconnects = 0
        self._closing_by_us = False
        self._reconnect_base_delay_s = _SIGNAL_RECONNECT_BASE_DELAY_S

        # Transcript and agent-state mirrors ride a data channel the page
        # opens, so they stay end to end (DTLS) and never cross the relay.
        self._display_channel: Any = None
        self._display_oversized: set[str] = set()
        self._display_pending: dict[str, dict[str, Any]] = {}
        self.peer = self._new_peer(ice_servers)

    def _new_peer(self, ice_servers: tuple[IceServerConfig, ...]) -> RTCPeerConnection:
        peer = RTCPeerConnection(
            RTCConfiguration(
                iceServers=[
                    RTCIceServer(
                        urls=list(server.urls),
                        username=server.username or None,
                        credential=server.credential or None,
                    )
                    for server in ice_servers
                ]
            )
        )
        peer.addTrack(self.outgoing)

        @peer.on("track")
        def on_track(track: MediaStreamTrack) -> None:
            if peer is self.peer:
                self._accept_remote_track(track)

        @peer.on("datachannel")
        def on_datachannel(channel: Any) -> None:
            if peer is not self.peer or channel.label != DISPLAY_CHANNEL_LABEL:
                return
            self._display_channel = channel
            if channel.readyState == "open":
                self._flush_display()
            else:
                channel.on("open", self._flush_display)

        @peer.on("connectionstatechange")
        def on_connection_state() -> None:
            # A peer replaced before negotiation reports "closed" as it goes;
            # only the current peer's state speaks for the call.
            if peer is self.peer:
                self._handle_connection_state(peer.connectionState)

        return peer

    async def _adopt_ice_servers(self, value: Any) -> None:
        """Use the TURN credentials the call service sends with redemption.

        The service mints them only once a browser redeems the link. Before
        the browser's offer arrives the peer has negotiated nothing, so it is
        rebuilt with them; after that the call keeps the peer it has.
        """
        if not isinstance(value, list) or self.peer.remoteDescription is not None:
            return
        ice_servers = _parse_ice_servers(value)
        previous, self.peer = self.peer, self._new_peer(ice_servers)
        await previous.close()

    async def open(self) -> None:
        self._remote_track = asyncio.get_running_loop().create_future()
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=15)
        self._session = aiohttp.ClientSession(timeout=timeout)
        try:
            self._websocket = await self._session.ws_connect(self.signal_url, heartbeat=15)
            if not self.access_key:
                raise CallProviderError("The call service returned no signaling credential.")
            await self._websocket.send_json(
                _envelope("call.auth", {"role": self.role, "key": self.access_key})
            )
        except aiohttp.WSServerHandshakeError as exc:
            await self._session.close()
            raise CallProviderError(
                f"The call service rejected the agent connection (HTTP {exc.status})."
            ) from exc
        except aiohttp.ClientError as exc:
            await self._session.close()
            raise CallProviderError(
                "The call service is unreachable. Check the rendezvous URL and try again."
            ) from exc
        self._signal_task = asyncio.create_task(self._run_signaling(), name="call-signaling")
        self._heartbeat_task = asyncio.create_task(self._heartbeat(), name="call-heartbeat")

    async def recv(self) -> AudioFrame:
        return await self._recv_frame()

    def _accept_remote_track(self, track: MediaStreamTrack) -> None:
        if track.kind != "audio":
            return
        if self._remote_track is None or self._remote_track.done():
            self._remote_track = asyncio.get_running_loop().create_future()
        if not self._remote_track.done():
            self._remote_track.set_result(track)

    def _reset_remote_track(self) -> None:
        current = self._remote_track
        if current is not None and not current.done():
            return
        self._remote_track = asyncio.get_running_loop().create_future()

    def _handle_connection_state(self, state: str) -> None:
        if state == "connected":
            self._ice_failures = 0
            self._cancel_ice_recovery()
            return
        if state == "failed":
            self._begin_ice_recovery()
            return
        if state == "closed" and not self._closed.is_set():
            self._end_reason = "connection_lost"
            self._closed.set()

    def _begin_ice_recovery(self) -> None:
        if self._closed.is_set() or self._ice_recovery_task is not None:
            return
        if self._ice_failures >= _MAX_ICE_RECOVERIES:
            self._end_reason = "connection_lost"
            self._closed.set()
            return
        self._ice_failures += 1
        self._ice_recovery_task = asyncio.get_running_loop().create_task(
            self._ice_recovery_timeout(),
            name="call-ice-recovery",
        )

    async def _ice_recovery_timeout(self) -> None:
        try:
            await asyncio.sleep(self._ice_recovery_seconds)
        except asyncio.CancelledError:
            return
        if not self._closed.is_set():
            self._end_reason = "connection_lost"
            self._closed.set()

    def _cancel_ice_recovery(self) -> None:
        task = getattr(self, "_ice_recovery_task", None)
        self._ice_recovery_task = None
        if task is not None and not task.done():
            task.cancel()

    async def _recv_frame(self) -> AudioFrame:
        while True:
            try:
                return await self._recv_frame_once()
            except MediaStreamError:
                if self._closed.is_set():
                    raise CallEnded(self._end_reason) from None
                current = self._remote_track
                failed = self._active_remote_track
                if current is not None and current.done() and current.result() is not failed:
                    continue
                self._reset_remote_track()

    async def _recv_frame_once(self) -> AudioFrame:
        remote = self._remote_track
        if remote is None:
            raise CallProviderError("The call provider has not been connected.")
        if not remote.done():
            closed = asyncio.create_task(self._closed.wait())
            try:
                done, _ = await asyncio.wait({remote, closed}, return_when=asyncio.FIRST_COMPLETED)
                if closed in done:
                    raise CallEnded(self._end_reason)
            finally:
                closed.cancel()
                await asyncio.gather(closed, return_exceptions=True)
        track = remote.result()
        self._active_remote_track = track
        receive = asyncio.create_task(track.recv())
        closed = asyncio.create_task(self._closed.wait())
        try:
            done, _ = await asyncio.wait({receive, closed}, return_when=asyncio.FIRST_COMPLETED)
            if closed in done:
                raise CallEnded(self._end_reason)
            frame = receive.result()
        finally:
            for task in (receive, closed):
                if not task.done():
                    task.cancel()
            await asyncio.gather(receive, closed, return_exceptions=True)
        if not isinstance(frame, AudioFrame):
            raise CallProviderError("The call provider received a non-audio frame.")
        return frame

    async def send(self, frame: AudioFrame) -> None:
        if self._closed.is_set():
            raise CallEnded(self._end_reason)
        await self.outgoing.send(frame)

    async def send_played(self, frame: AudioFrame) -> bool:
        if self._closed.is_set():
            raise CallEnded(self._end_reason)
        return await self.outgoing.send_played(frame)

    async def _send_signal(self, message_type: str, payload: dict[str, Any]) -> None:
        if self._closed.is_set():
            raise CallEnded(self._end_reason)
        websocket = self._websocket
        if websocket is None or websocket.closed:
            # Signaling is between sockets (reconnecting). Display-only
            # mirrors are dropped rather than ending the call.
            return
        try:
            await websocket.send_json(_envelope(message_type, payload))
        except (aiohttp.ClientError, ConnectionError, RuntimeError):
            if self._closed.is_set():
                raise CallEnded(self._end_reason) from None

    async def send_agent_state(self, state: str, task: str = "", detail: str = "") -> None:
        """Mirror the agent's work state onto the web orb UI (display-only)."""
        self._send_display("call.agent_state", {"state": state, "task": task, "detail": detail})

    async def send_presentation(self, payload: dict) -> None:
        """Mirror the presentation snapshot onto the web UI (display-only)."""
        self._send_display("call.presentation", payload)

    def _send_display(self, message_type: str, payload: dict[str, Any]) -> None:
        """Send a display mirror on the page's data channel, never the relay.

        Each message is a full snapshot, so until the channel opens only the
        latest of each type is kept. A page without the channel (an older
        page) simply shows no transcript.
        """
        if self._closed.is_set():
            raise CallEnded(self._end_reason)
        channel = self._display_channel
        if channel is None or channel.readyState != "open":
            self._display_pending[message_type] = payload
            return
        data = json.dumps(_envelope(message_type, payload), separators=(",", ":"))
        if len(data.encode()) > _MAX_DISPLAY_BYTES and message_type == "call.presentation":
            # A long call outgrows the channel; send its newest part rather
            # than nothing (the page froze for the rest of the call).
            payload = fit_snapshot(payload, _MAX_DISPLAY_BYTES - _DISPLAY_ENVELOPE_BYTES)
            data = json.dumps(_envelope(message_type, payload), separators=(",", ":"))
        if len(data.encode()) > _MAX_DISPLAY_BYTES:
            if message_type not in self._display_oversized:
                self._display_oversized.add(message_type)
                logger.warning("%s is %d bytes, over the display channel's %d; not sent",
                               message_type, len(data.encode()), _MAX_DISPLAY_BYTES)
            return
        try:
            channel.send(data)
        except Exception:  # noqa: BLE001 - display-only; the call carries on
            return

    def _flush_display(self) -> None:
        pending, self._display_pending = self._display_pending, {}
        for message_type, payload in pending.items():
            if not self._closed.is_set():
                self._send_display(message_type, payload)

    def set_settings_handler(
        self, handler: Callable[[dict[str, Any]], Awaitable[None] | None] | None
    ) -> None:
        """Receive in-call playback, narration, and response-length changes."""
        self._on_settings = handler

    def set_interrupt_handler(
        self, handler: Callable[[], Awaitable[None] | None] | None
    ) -> None:
        """Cut the current turn without ending the call."""
        self._on_interrupt = handler

    def clear_output(self) -> int:
        return self.outgoing.clear()

    async def wait_closed(self) -> str:
        await self._closed.wait()
        return self._end_reason

    async def recv_telemetry(self) -> dict[str, Any] | None:
        value = await self._telemetry.get()
        return None if value is _TELEMETRY_CLOSE_SENTINEL else value  # type: ignore[return-value]

    def transport_snapshot(self) -> dict[str, Any]:
        return dict(self._latest_transport_stats)

    async def close(self, *, reason: str = "agent_closed") -> None:
        """Close the call and notify the peer.

        ``reason`` distinguishes an operator cancel (``user_cancel`` sends
        ``call.cancel`` so the browser shows the cancelled state) from an
        ordinary agent teardown (``call.hangup``) — r2-browser-delivery:
        both peers observe the same terminal state. A reason previously
        recorded by the host session (``pending_close_reason``) wins.
        """
        recorded = getattr(self, "pending_close_reason", None)
        if recorded is not None:
            reason = recorded
        self._closing_by_us = True
        websocket = self._websocket
        if websocket and not websocket.closed:
            message_type = "call.cancel" if reason == "user_cancel" else "call.hangup"
            try:
                await websocket.send_json(_envelope(message_type, {"reason": reason}))
            except (ConnectionError, RuntimeError):
                pass
        self._cancel_ice_recovery()
        self._closed.set()
        self._close_telemetry()
        current = asyncio.current_task()
        for task in (self._heartbeat_task, self._signal_task):
            if task and task is not current and not task.done():
                task.cancel()
        for task in (self._heartbeat_task, self._signal_task):
            if task and task is not current:
                await asyncio.gather(task, return_exceptions=True)
        self.outgoing.stop()
        await self.peer.close()
        if websocket and not websocket.closed:
            await websocket.close()
        if self._session and not self._session.closed:
            await self._session.close()

    async def _run_signaling(self) -> None:
        try:
            while True:
                websocket = self._websocket
                if websocket is None:
                    return
                loss = await self._pump_signals(websocket)
                if loss is None or self._closed.is_set() or self._closing_by_us:
                    return
                if not await self._reconnect_signaling(*loss):
                    return
        finally:
            self._closed.set()

    async def _pump_signals(
        self, websocket: aiohttp.ClientWebSocketResponse
    ) -> tuple[int | None, str] | None:
        """Relay one socket's messages until it ends.

        Returns None when the call ended through an explicit envelope (the
        reason is already recorded), otherwise the close code and reason text
        of the loss. aiohttp's ``async for`` swallows CLOSE frames, so this
        reads messages directly: a server close and a dropped connection must
        stay distinguishable in the record, and neither is a hangup.
        """
        while True:
            try:
                message = await websocket.receive()
            except (aiohttp.ClientError, ConnectionError, asyncio.TimeoutError) as exc:
                return websocket.close_code, f"{type(exc).__name__}: {exc}"
            if message.type == aiohttp.WSMsgType.TEXT:
                envelope = _parse_envelope(message.data)
                if envelope is None:
                    continue
                await self._handle_signal(envelope)
                if self._closed.is_set():
                    return None
            elif message.type == aiohttp.WSMsgType.CLOSE:
                return message.data, str(message.extra or "")
            elif message.type == aiohttp.WSMsgType.ERROR:
                return websocket.close_code, f"error: {message.data}"
            elif message.type in {aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.CLOSED}:
                return websocket.close_code, ""

    async def _reconnect_signaling(self, code: int | None, text: str) -> bool:
        """Re-open signaling after a loss. False means the call is over.

        The worker keeps the ticket live across a signaling drop and accepts
        the same agent key again, the way it does for the browser. A close
        code that says the ticket is gone (4003/4004) is final: the peer's
        real reason was on the socket we lost, and the detail keeps the code.
        """
        detail = _describe_signal_loss(code, text)
        if code in _SIGNAL_FINAL_CLOSE_CODES:
            self._end_reason = "signal_closed"
            self.end_detail = detail
            return False
        session = self._session
        for attempt in range(1, _SIGNAL_RECONNECT_MAX_ATTEMPTS + 1):
            if self._closed.is_set() or self._closing_by_us or session is None:
                return False
            await asyncio.sleep(self._reconnect_base_delay_s * 2 ** (attempt - 1))
            if self._closed.is_set() or self._closing_by_us:
                return False
            try:
                websocket = await session.ws_connect(self.signal_url, heartbeat=15)
                await websocket.send_json(
                    _envelope("call.auth", {"role": self.role, "key": self.access_key})
                )
            except (aiohttp.ClientError, ConnectionError, OSError) as exc:
                detail = f"{detail}; reconnect {attempt} failed: {type(exc).__name__}: {exc}"
                continue
            self._websocket = websocket
            self.signal_reconnects += 1
            self.end_detail = ""
            return True
        self._end_reason = "signal_lost" if code in (None, 1006) else "signal_closed"
        self.end_detail = f"{detail}; gave up after {_SIGNAL_RECONNECT_MAX_ATTEMPTS} reconnect attempts"
        return False

    async def _handle_signal(self, envelope: dict[str, Any]) -> None:
        websocket = self._websocket
        if websocket is None:
            return
        message_type = envelope["type"]
        payload = envelope["payload"]
        if message_type == "webrtc.offer":
            await self.peer.setRemoteDescription(
                RTCSessionDescription(sdp=str(payload.get("sdp", "")), type="offer")
            )
            _prefer_opus(self.peer)
            for candidate in self._pending_candidates:
                await _add_ice_candidate(self.peer, candidate)
            self._pending_candidates.clear()
            answer = await self.peer.createAnswer()
            await self.peer.setLocalDescription(answer)
            local = self.peer.localDescription
            if local is None:
                raise CallProviderError("The agent could not create a WebRTC answer.")
            await websocket.send_json(
                _envelope("webrtc.answer", {"sdp": local.sdp, "type": local.type})
            )
        elif message_type == "webrtc.ice":
            candidate = payload.get("candidate")
            parsed = candidate if isinstance(candidate, dict) else None
            if self.peer.remoteDescription is None:
                self._pending_candidates.append(parsed)
            else:
                await _add_ice_candidate(self.peer, parsed)
        elif message_type == "webrtc.ice_complete":
            if self.peer.remoteDescription is None:
                self._pending_candidates.append(None)
            else:
                await self.peer.addIceCandidate(None)
        elif message_type in {"call.hangup", "call.ended"}:
            self._end_reason = str(payload.get("reason", "peer_hung_up"))
            self._closed.set()
        elif message_type == "call.error":
            self._end_reason = "browser_error"
            self._closed.set()
        elif message_type == "ticket.redeemed":
            self._put_telemetry({"stage": "ticket_redeemed"})
            if isinstance(payload, dict):
                await self._adopt_ice_servers(payload.get("iceServers"))
        elif message_type == "call.telemetry":
            telemetry = _parse_browser_telemetry(payload)
            if telemetry is not None:
                if telemetry["stage"] == "transport_stats":
                    self._latest_transport_stats = {
                        key: value for key, value in telemetry.items() if key != "stage"
                    }
                self._put_telemetry(telemetry)
        elif message_type == "call.settings":
            handler = self._on_settings
            if callable(handler):
                result = handler(payload if isinstance(payload, dict) else {})
                if inspect.isawaitable(result):
                    await result
        elif message_type == "call.interrupt":
            handler = self._on_interrupt
            if callable(handler):
                result = handler()
                if inspect.isawaitable(result):
                    await result

    async def _heartbeat(self) -> None:
        # A failed ping is not a verdict on the call: the signaling loop owns
        # loss detection and reconnect, and the socket may be mid-swap here.
        while not self._closed.is_set():
            await asyncio.sleep(_HEARTBEAT_SECONDS)
            websocket = self._websocket
            if websocket is None or websocket.closed:
                continue
            try:
                await websocket.send_json(_envelope("ping"))
            except (aiohttp.ClientError, ConnectionError, RuntimeError):
                continue

    def _put_telemetry(self, value: dict[str, Any]) -> None:
        if self._telemetry.full():
            self._telemetry.get_nowait()
        self._telemetry.put_nowait(value)

    def _close_telemetry(self) -> None:
        if self._telemetry.full():
            self._telemetry.get_nowait()
        self._telemetry.put_nowait(_TELEMETRY_CLOSE_SENTINEL)

async def run_ticket_echo_call(
    rendezvous_url: str,
    context: CallContext,
    *,
    ttl_seconds: int = DEFAULT_TICKET_TTL_SECONDS,
    on_ready: Callable[[str], None] = print,
) -> None:
    """Mint an M1 ticket, print its public URL, and echo until the call ends."""

    provider = P2PCallProvider(rendezvous_url)
    ticket = await provider.mint(context, ttl_seconds=ttl_seconds)
    on_ready(ticket.join_url)
    try:
        async with provider.connect(ticket) as stream:
            while True:
                frame = await stream.recv()
                await stream.send(frame)
    except (CallEnded, MediaStreamError):
        return


def _is_loopback_host(host: str | None) -> bool:
    if not host:
        return False
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _validated_rendezvous(value: str) -> str:
    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise CallProviderError(
            "The rendezvous URL must start with http:// or https:// and include a host."
        )
    # The ticket and both peers' signaling keys cross this connection, so
    # plain http is only for a service on this machine.
    if parsed.scheme == "http" and not _is_loopback_host(parsed.hostname):
        raise CallProviderError(
            "The rendezvous URL must use https:// unless it is on this machine "
            "(localhost or 127.0.0.1)."
        )
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))


def _origin_endpoint(base_url: str, suffix: str) -> str:
    parsed = urlsplit(base_url)
    path = f"{parsed.path.rstrip('/')}{suffix}"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _parse_mint_response(raw: str, rendezvous_url: str, *, route: str = "call") -> CallTicket:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CallProviderError("The call service returned an unreadable response.") from exc
    if not isinstance(value, dict):
        raise CallProviderError("The call service returned an unreadable response.")
    try:
        ticket = CallTicket(
            token=str(value["ticket"]),
            routing_id=str(value["routingId"]),
            join_url=str(value["joinUrl"]),
            agent_signal_url=str(value["agentSignalUrl"]),
            agent_access_key=str(value["agentAccessKey"]),
            expires_at=str(value["expiresAt"]),
            session_cap_seconds=int(value["sessionCapSeconds"]),
            inactivity_seconds=int(value["inactivitySeconds"]),
            ice_servers=_parse_ice_servers(value.get("iceServers")),
            signal_types=_parse_signal_types(value.get("signalTypes")),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise CallProviderError("The call service returned an incomplete ticket.") from exc
    origin = urlsplit(rendezvous_url)
    join = urlsplit(ticket.join_url)
    if route == "setup" and value.get("kind") != "setup":
        raise CallProviderError("This call service doesn't support setup links yet.")
    signal = urlsplit(ticket.agent_signal_url)
    expected_signal_scheme = "wss" if origin.scheme == "https" else "ws"
    if (
        len(ticket.token) < 32
        or len(ticket.routing_id) < 20
        or not ticket.agent_access_key
        or join.scheme != origin.scheme
        or join.netloc != origin.netloc
        or join.query
        or join.fragment != f"/{route}/{ticket.routing_id}/{ticket.token}"
        or signal.scheme != expected_signal_scheme
        or signal.netloc != origin.netloc
        or signal.query
        or signal.path != f"/api/tickets/{ticket.routing_id}/signal"
    ):
        raise CallProviderError("The call service returned an invalid ticket URL.")
    return ticket


def _parse_signal_types(value: Any) -> frozenset[str]:
    if not isinstance(value, list):
        return frozenset()
    return frozenset(
        item.strip()
        for item in value
        if isinstance(item, str) and item.strip() and len(item.strip()) <= 64
    )


def _parse_ice_servers(value: Any) -> tuple[IceServerConfig, ...]:
    fallback = (IceServerConfig(("stun:stun.cloudflare.com:3478",)),)
    if not isinstance(value, list):
        return fallback
    parsed: list[IceServerConfig] = []
    for entry in value[:8]:
        if not isinstance(entry, dict):
            continue
        raw_urls = entry.get("urls", [])
        if isinstance(raw_urls, str):
            raw_urls = [raw_urls]
        if not isinstance(raw_urls, list):
            continue
        urls = tuple(
            url.strip()
            for url in raw_urls[:8]
            if isinstance(url, str)
            and len(url.strip()) <= 512
            and url.strip().lower().startswith(
                ("stun:", "stuns:", "turn:", "turns:")
            )
        )
        if not urls:
            continue
        username = entry.get("username", "")
        credential = entry.get("credential", "")
        username = username.strip() if isinstance(username, str) else ""
        credential = credential.strip() if isinstance(credential, str) else ""
        uses_turn = any(
            url.lower().startswith(("turn:", "turns:")) for url in urls
        )
        if uses_turn and not (username and credential):
            continue
        parsed.append(IceServerConfig(urls, username, credential))
    return tuple(parsed) or fallback


_SERVICE_ERRORS = {
    "enrollment_limited": "This network has reached the hosted installation limit. Try again later.",
    "grant_invalid": "This installation credential is invalid. Reset hosted access before trying again.",
    "grant_revoked": "This installation credential was revoked. Reset hosted access before trying again.",
    "mint_limited": "This installation has reached its call-link limit. Try again later.",
    "mint_in_progress": "A call link is already being prepared. Try again shortly.",
    "setup_limited": "This installation has reached its setup-link limit. Try again later.",
    "invalid_kind": "This call service doesn't support setup links yet.",
    "concurrency_limited": "End an existing call or wait for an unused link to expire.",
    "relay_allowance_exhausted": "The hosted relay allowance has been reached. Try again later.",
    "service_disabled": "The hosted call service is temporarily disabled.",
    "service_misconfigured": "The hosted call service is not configured for calls.",
    "turn_unavailable": "The hosted relay is unavailable. Try again or open the link in a full browser.",
}


def _service_error(status: int, raw: str) -> str:
    try:
        value = json.loads(raw)
        code = value.get("error", {}).get("code")
        if isinstance(code, str) and code in _SERVICE_ERRORS:
            return _SERVICE_ERRORS[code]
    except (json.JSONDecodeError, AttributeError):
        pass
    if status >= 500:
        return "The call service couldn't create a link right now. Try again shortly."
    return "The call service rejected the link request."


async def _add_ice_candidate(
    peer: RTCPeerConnection, candidate_data: dict[str, Any] | None
) -> None:
    if candidate_data is None:
        await peer.addIceCandidate(None)
        return
    candidate_text = str(candidate_data.get("candidate", ""))
    if candidate_text.startswith("candidate:"):
        candidate_text = candidate_text.split(":", 1)[1]
    if not candidate_text:
        return
    candidate = candidate_from_sdp(candidate_text)
    candidate.sdpMid = _optional_string(candidate_data.get("sdpMid"))
    line_index = candidate_data.get("sdpMLineIndex")
    candidate.sdpMLineIndex = int(line_index) if isinstance(line_index, (int, float)) else None
    await peer.addIceCandidate(candidate)


def _prefer_opus(peer: RTCPeerConnection) -> None:
    opus = [
        codec
        for codec in RTCRtpSender.getCapabilities("audio").codecs
        if codec.mimeType.lower() == "audio/opus"
    ]
    if not opus:
        raise CallProviderError("This host's WebRTC stack does not support Opus audio.")
    for transceiver in peer.getTransceivers():
        if transceiver.kind == "audio":
            transceiver.setCodecPreferences(opus)


def _describe_signal_loss(code: int | None, text: str) -> str:
    """Human line for the record: what the socket said when it went away."""
    if code is None or code == 1006:
        base = "signaling connection lost without a close frame"
        return f"{base} ({text})" if text else base
    base = f"signaling socket closed by server with code {code}"
    return f"{base} {text!r}" if text else base


def _envelope(message_type: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "v": SIGNAL_VERSION,
        "id": str(uuid4()),
        "type": message_type,
        "payload": payload or {},
    }


def _parse_envelope(raw: str) -> dict[str, Any] | None:
    if len(raw) > 64 * 1024:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(value, dict) or value.get("v") != SIGNAL_VERSION:
        return None
    if not isinstance(value.get("type"), str) or not isinstance(value.get("payload"), dict):
        return None
    return value


def _parse_browser_telemetry(payload: dict[str, Any]) -> dict[str, Any] | None:
    stage = payload.get("stage")
    if not isinstance(stage, str) or stage not in _BROWSER_TELEMETRY_STAGES:
        return None
    result: dict[str, Any] = {"stage": stage}
    for key in _TELEMETRY_FIELDS:
        value = payload.get(key)
        if isinstance(value, bool):
            result[key] = value
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            if math.isfinite(value) and abs(value) <= 10**12:
                result[key] = round(float(value), 3)
        elif isinstance(value, str) and len(value) <= 128:
            result[key] = value
    return result


def _bounded_identifier(label: str, value: str) -> str:
    clean = value.strip()
    if not clean or len(clean) > 256:
        raise CallProviderError(f"The {label} must be between 1 and 256 characters.")
    return clean


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
