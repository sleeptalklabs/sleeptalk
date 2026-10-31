#!/usr/bin/env python3
"""Exercise hosted-access safety controls without printing credentials."""

from __future__ import annotations

import argparse
import asyncio
import fractions
import json
import logging
import math
import os
import tempfile
import time
from array import array
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from importlib.metadata import version as package_version
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import uuid4

import aiohttp
from aioice.ice import TransportPolicy
from aiortc import (
    AudioStreamTrack,
    MediaStreamError,
    MediaStreamTrack,
    RTCConfiguration,
    RTCIceServer,
    RTCPeerConnection,
    RTCSessionDescription,
)
from aiortc.sdp import SessionDescription
from av import AudioFrame
from av.audio.resampler import AudioResampler

from sleeptalk.call_provider import (
    BidirectionalAudioStream,
    CallContext,
    CallProviderError,
    CallTicket,
    IceServerConfig,
    P2PCallProvider,
)
from sleeptalk.service_grant import ServiceGrantStore


_AIORTC_VERSION = "1.15.0"
_AIOICE_VERSION = "0.10.2"
_MEDIA_DEADLINE_SECONDS = 60.0
_REQUIRED_TONE_FRAMES = 3
_TONE_FREQUENCY_HZ = 997
_TONE_AMPLITUDE = 12_000


class ProofFailure(RuntimeError):
    """A credential-safe, fixed-code proof failure."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ScenarioName(str, Enum):
    DIRECT_FIRST = "directFirst"
    FORCED_RELAY = "forcedRelay"


@dataclass(frozen=True, slots=True)
class BrowserRedemption:
    signal_url: str = field(repr=False)
    access_key: str = field(repr=False)
    ice_servers: tuple[IceServerConfig, ...] = field(repr=False)
    server_policy: Literal["all", "relay"]


@dataclass(frozen=True, slots=True)
class IcePlan:
    servers: tuple[IceServerConfig, ...] = field(repr=False)
    relay_only: bool
    synthetic_peer_tls_443_endpoint: Literal["not_requested", "passed"]


@dataclass(frozen=True, slots=True)
class SelectedPath:
    path: Literal["direct", "relay"]
    local_candidate: Literal["host", "srflx", "prflx", "relay", "unknown"]
    remote_candidate: Literal["host", "srflx", "prflx", "relay", "unknown"]
    relay_protocol: Literal["none", "udp", "tcp", "tls", "unknown"]


def _signal_envelope(message_type: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "v": 1,
        "id": str(uuid4()),
        "type": message_type,
        "payload": payload or {},
    }


def _parse_signal(raw: str) -> dict[str, Any] | None:
    if len(raw) > 64 * 1024:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(value, dict) or value.get("v") != 1:
        return None
    if not isinstance(value.get("type"), str) or not isinstance(value.get("payload"), dict):
        return None
    return value


def _parse_ice_servers(value: object) -> tuple[IceServerConfig, ...]:
    if not isinstance(value, list) or len(value) > 8:
        raise ProofFailure("invalid_redemption")
    parsed: list[IceServerConfig] = []
    for raw_server in value:
        if not isinstance(raw_server, dict):
            raise ProofFailure("invalid_redemption")
        raw_urls = raw_server.get("urls")
        urls = raw_urls if isinstance(raw_urls, list) else [raw_urls]
        if not urls or len(urls) > 8:
            raise ProofFailure("invalid_redemption")
        clean_urls: list[str] = []
        for raw_url in urls:
            if not isinstance(raw_url, str) or not 1 <= len(raw_url) <= 2048:
                raise ProofFailure("invalid_redemption")
            lowered = raw_url.lower()
            if not lowered.startswith(("stun:", "turn:", "turns:")):
                raise ProofFailure("invalid_redemption")
            clean_urls.append(raw_url)
        username = raw_server.get("username", "")
        credential = raw_server.get("credential", "")
        if not isinstance(username, str) or not isinstance(credential, str):
            raise ProofFailure("invalid_redemption")
        if any(url.lower().startswith("turn") for url in clean_urls) and not (
            username and credential
        ):
            raise ProofFailure("invalid_redemption")
        parsed.append(
            IceServerConfig(
                urls=tuple(clean_urls),
                username=username,
                credential=credential,
            )
        )
    return tuple(parsed)


def parse_browser_redemption(
    origin: str,
    routing_id: str,
    payload: dict[str, Any],
    expected_policy: Literal["all", "relay"],
) -> BrowserRedemption:
    signal_url = payload.get("browserSignalUrl")
    access_key = payload.get("browserAccessKey")
    policy = payload.get("iceTransportPolicy")
    if not isinstance(signal_url, str) or not isinstance(access_key, str) or not access_key:
        raise ProofFailure("invalid_redemption")
    source = urlsplit(origin)
    signal = urlsplit(signal_url)
    expected_scheme = "wss" if source.scheme == "https" else "ws"
    expected_path = f"/api/tickets/{routing_id}/signal"
    if (
        signal.scheme != expected_scheme
        or signal.netloc != source.netloc
        or signal.path != expected_path
        or signal.query
        or signal.fragment
        or policy != expected_policy
    ):
        raise ProofFailure("invalid_redemption")
    return BrowserRedemption(
        signal_url=signal_url,
        access_key=access_key,
        ice_servers=_parse_ice_servers(payload.get("iceServers")),
        server_policy=expected_policy,
    )


def _is_turns_443_tcp(url: str) -> bool:
    lowered = url.lower()
    endpoint, _, query = lowered.partition("?")
    return (
        endpoint.startswith("turns:")
        and endpoint.rsplit(":", 1)[-1] == "443"
        and "transport=tcp" in query.split("&")
    )


def _is_turn_3478_udp(url: str) -> bool:
    lowered = url.lower()
    endpoint, _, query = lowered.partition("?")
    return (
        endpoint.startswith("turn:")
        and endpoint.rsplit(":", 1)[-1] == "3478"
        and "transport=udp" in query.split("&")
    )


def choose_ice_plan(
    redemption: BrowserRedemption,
    scenario: ScenarioName,
    forced_relay_transport: Literal["udp", "tls"] = "udp",
) -> IcePlan:
    if scenario is ScenarioName.DIRECT_FIRST:
        if redemption.server_policy != "all":
            raise ProofFailure("wrong_server_policy")
        return IcePlan(
            servers=redemption.ice_servers,
            relay_only=False,
            synthetic_peer_tls_443_endpoint="not_requested",
        )
    if redemption.server_policy != "relay":
        raise ProofFailure("wrong_server_policy")
    predicate = _is_turn_3478_udp if forced_relay_transport == "udp" else _is_turns_443_tcp
    for server in redemption.ice_servers:
        for url in server.urls:
            if predicate(url):
                return IcePlan(
                    servers=(
                        IceServerConfig(
                            urls=(url,),
                            username=server.username,
                            credential=server.credential,
                        ),
                    ),
                    relay_only=True,
                    synthetic_peer_tls_443_endpoint=(
                        "passed" if forced_relay_transport == "tls" else "not_requested"
                    ),
                )
    raise ProofFailure(
        "tls_443_not_offered"
        if forced_relay_transport == "tls"
        else "turn_udp_not_offered"
    )


class SyntheticToneTrack(AudioStreamTrack):
    """A deterministic, paced 48 kHz mono tone kept entirely in memory."""

    def __init__(self) -> None:
        super().__init__()
        self._sample_offset = 0

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
        values = array(
            "h",
            (
                int(
                    _TONE_AMPLITUDE
                    * math.sin(
                        2
                        * math.pi
                        * _TONE_FREQUENCY_HZ
                        * (self._sample_offset + index)
                        / sample_rate
                    )
                )
                for index in range(samples)
            ),
        )
        self._sample_offset += samples
        frame = AudioFrame(format="s16", layout="mono", samples=samples)
        frame.planes[0].update(values.tobytes())
        frame.pts = self._timestamp
        frame.sample_rate = sample_rate
        frame.time_base = fractions.Fraction(1, sample_rate)
        return frame


class ToneDetector:
    def __init__(self) -> None:
        self._resampler = AudioResampler(format="s16", layout="mono", rate=48_000)

    def detected(self, frame: AudioFrame) -> bool:
        converted = self._resampler.resample(frame)
        for normalized in converted:
            samples = array("h")
            samples.frombytes(bytes(normalized.planes[0]))
            if not samples:
                continue
            rms = math.sqrt(sum(value * value for value in samples) / len(samples))
            crossings = sum(
                1 for left, right in zip(samples, samples[1:]) if (left < 0) != (right < 0)
            )
            if rms >= 1_000 and 20 <= crossings <= 80:
                return True
        return False


class AiortcProbeAdapter:
    """The only code allowed to depend on locked aiortc/aioice internals."""

    @staticmethod
    def _connection(peer: RTCPeerConnection) -> Any:
        if (
            package_version("aiortc") != _AIORTC_VERSION
            or package_version("aioice") != _AIOICE_VERSION
        ):
            raise ProofFailure("private_api_mismatch")
        transceivers = peer.getTransceivers()
        if len(transceivers) != 1:
            raise ProofFailure("private_api_mismatch")
        sender_transport = transceivers[0].sender.transport
        ice_transport = getattr(sender_transport, "transport", None)
        connection = getattr(ice_transport, "_connection", None)
        if connection is None:
            raise ProofFailure("private_api_mismatch")
        return connection

    @classmethod
    def set_relay_only_before_gathering(cls, peer: RTCPeerConnection) -> None:
        connection = cls._connection(peer)
        if connection._local_candidates_start or connection._protocols:
            raise ProofFailure("private_api_mismatch")
        connection._transport_policy = TransportPolicy.RELAY

    @classmethod
    def prune_non_relay_protocols(cls, peer: RTCPeerConnection) -> None:
        connection = cls._connection(peer)
        if not connection._local_candidates_end or connection._check_list:
            raise ProofFailure("private_api_mismatch")
        kept: list[Any] = []
        discarded: list[Any] = []
        for protocol in connection._protocols:
            candidate = getattr(protocol, "local_candidate", None)
            if getattr(candidate, "type", None) == "relay":
                kept.append(protocol)
            else:
                discarded.append(protocol)
        if len(kept) != 1 or not connection.local_candidates:
            raise ProofFailure("relay_transport_unavailable")
        if any(candidate.type != "relay" for candidate in connection.local_candidates):
            raise ProofFailure("relay_policy_not_enforced")
        connection._protocols = kept
        for protocol in discarded:
            transport = getattr(protocol, "transport", None)
            if transport is not None:
                transport.close()

    @classmethod
    def selected_path(cls, peer: RTCPeerConnection) -> SelectedPath:
        connection = cls._connection(peer)
        nominated = list(connection._nominated.values())
        if len(nominated) != 1:
            raise ProofFailure("selected_path_unavailable")
        pair = nominated[0]
        local = _safe_candidate_kind(getattr(pair, "local_candidate", None))
        remote = _safe_candidate_kind(getattr(pair, "remote_candidate", None))
        path: Literal["direct", "relay"] = "relay" if "relay" in {local, remote} else "direct"
        protocol: Literal["none", "udp", "tcp", "tls", "unknown"] = "none"
        if path == "relay":
            if local == "relay":
                if connection.turn_ssl and connection.turn_transport == "tcp":
                    protocol = "tls"
                elif connection.turn_transport == "tcp":
                    protocol = "tcp"
                elif connection.turn_transport == "udp":
                    protocol = "udp"
                else:
                    protocol = "unknown"
            else:
                protocol = "unknown"
        return SelectedPath(path, local, remote, protocol)


def _safe_candidate_kind(candidate: object) -> Literal[
    "host", "srflx", "prflx", "relay", "unknown"
]:
    kind = getattr(candidate, "type", None)
    return kind if kind in {"host", "srflx", "prflx", "relay"} else "unknown"


def _relay_answer_sdp(sdp: str) -> str:
    """Keep only the host's relay route for the two-sided relay probe."""
    try:
        description = SessionDescription.parse(sdp)
    except Exception as exc:
        raise ProofFailure("invalid_answer") from exc
    relay_candidates = 0
    for media in description.media:
        if media.kind not in {"audio", "video"}:
            continue
        media.ice_candidates = [
            candidate for candidate in media.ice_candidates if candidate.type == "relay"
        ]
        relay_candidates += len(media.ice_candidates)
    if relay_candidates == 0:
        raise ProofFailure("remote_relay_not_offered")
    return str(description)


class SyntheticBrowserPeer:
    """A synthetic aiortc peer that speaks the deployed browser signaling role."""

    def __init__(self, redemption: BrowserRedemption, plan: IcePlan) -> None:
        self._redemption = redemption
        self._plan = plan
        self._peer = RTCPeerConnection(
            RTCConfiguration(
                iceServers=[
                    RTCIceServer(
                        urls=list(server.urls),
                        username=server.username or None,
                        credential=server.credential or None,
                    )
                    for server in plan.servers
                ]
            )
        )
        self._tone = SyntheticToneTrack()
        self._peer.addTrack(self._tone)
        if plan.relay_only:
            AiortcProbeAdapter.set_relay_only_before_gathering(self._peer)
        loop = asyncio.get_running_loop()
        self._remote_track: asyncio.Future[MediaStreamTrack] = loop.create_future()
        self._terminal = asyncio.Event()
        self._failure: asyncio.Future[ProofFailure] = loop.create_future()
        self._session: aiohttp.ClientSession | None = None
        self._websocket: aiohttp.ClientWebSocketResponse | None = None
        self._signal_task: asyncio.Task[None] | None = None
        self._offer_sent = False

        @self._peer.on("track")
        def on_track(track: MediaStreamTrack) -> None:
            if track.kind == "audio" and not self._remote_track.done():
                self._remote_track.set_result(track)

    async def open(self) -> None:
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=None, sock_connect=15)
        )
        try:
            self._websocket = await self._session.ws_connect(
                self._redemption.signal_url,
                heartbeat=15,
            )
            await self._websocket.send_json(
                _signal_envelope(
                    "call.auth",
                    {"role": "browser", "key": self._redemption.access_key},
                )
            )
        except (aiohttp.ClientError, ConnectionError, OSError) as exc:
            raise ProofFailure("browser_signal_failed") from exc
        self._signal_task = asyncio.create_task(
            self._run_signaling(),
            name="hosted-proof-browser-signaling",
        )

    async def _run_signaling(self) -> None:
        websocket = self._websocket
        if websocket is None:
            self._fail("browser_signal_failed")
            return
        try:
            async for message in websocket:
                if message.type != aiohttp.WSMsgType.TEXT:
                    if message.type in {
                        aiohttp.WSMsgType.CLOSE,
                        aiohttp.WSMsgType.CLOSED,
                        aiohttp.WSMsgType.ERROR,
                    }:
                        break
                    continue
                envelope = _parse_signal(message.data)
                if envelope is None:
                    continue
                await self._handle_signal(envelope)
                if self._terminal.is_set():
                    return
        except asyncio.CancelledError:
            raise
        except ProofFailure as exc:
            self._fail(exc.code)
            return
        except Exception:
            self._fail("browser_signal_failed")
            return
        if not self._terminal.is_set():
            self._fail("browser_signal_closed")

    async def _handle_signal(self, envelope: dict[str, Any]) -> None:
        message_type = envelope["type"]
        payload = envelope["payload"]
        websocket = self._websocket
        if websocket is None:
            self._fail("browser_signal_failed")
            return
        if message_type == "peer.ready" and not self._offer_sent:
            self._offer_sent = True
            offer = await self._peer.createOffer()
            await self._peer.setLocalDescription(offer)
            if self._plan.relay_only:
                AiortcProbeAdapter.prune_non_relay_protocols(self._peer)
            local = self._peer.localDescription
            if local is None:
                self._fail("offer_failed")
                return
            await websocket.send_json(
                _signal_envelope("webrtc.offer", {"sdp": local.sdp, "type": local.type})
            )
        elif message_type == "webrtc.answer":
            sdp = payload.get("sdp")
            if not isinstance(sdp, str) or not sdp:
                self._fail("invalid_answer")
                return
            if self._plan.relay_only:
                sdp = _relay_answer_sdp(sdp)
            await self._peer.setRemoteDescription(RTCSessionDescription(sdp=sdp, type="answer"))
        elif message_type in {"call.hangup", "call.cancel", "call.ended"}:
            self._terminal.set()
        elif message_type == "call.error":
            self._fail("signal_rejected")

    def _fail(self, code: str) -> None:
        if not self._failure.done():
            self._failure.set_result(ProofFailure(code))

    async def _until(self, predicate: Any, code: str, deadline: float) -> None:
        while not predicate():
            if self._failure.done():
                raise self._failure.result()
            if self._signal_task is not None and self._signal_task.done():
                try:
                    self._signal_task.result()
                except asyncio.CancelledError:
                    pass
                except Exception as exc:
                    raise ProofFailure("browser_signal_failed") from exc
                raise ProofFailure("browser_signal_closed")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProofFailure(code)
            await asyncio.sleep(min(0.05, remaining))

    async def wait_connected(self, deadline: float) -> None:
        await self._until(
            lambda: self._peer.connectionState == "connected",
            "ice_connect_timeout",
            deadline,
        )

    async def receive_echo(self, deadline: float) -> int:
        await self._until(lambda: self._remote_track.done(), "browser_audio_timeout", deadline)
        track = self._remote_track.result()
        detector = ToneDetector()
        detected = 0
        while detected < _REQUIRED_TONE_FRAMES:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProofFailure("browser_audio_silent")
            try:
                frame = await asyncio.wait_for(track.recv(), timeout=remaining)
            except (MediaStreamError, asyncio.TimeoutError) as exc:
                raise ProofFailure("browser_audio_silent") from exc
            if not isinstance(frame, AudioFrame):
                raise ProofFailure("browser_audio_invalid")
            if detector.detected(frame):
                detected += 1
        return detected

    async def wait_terminal(self, timeout_seconds: float = 3.0) -> None:
        if self._terminal.is_set():
            return
        try:
            await asyncio.wait_for(self._terminal.wait(), timeout=timeout_seconds)
        except asyncio.TimeoutError as exc:
            raise ProofFailure("termination_timeout") from exc

    async def safe_evidence(self, setup_ms: int) -> dict[str, Any]:
        selected = AiortcProbeAdapter.selected_path(self._peer)
        stats = await self._peer.getStats()
        packets_sent = 0
        packets_received = 0
        bytes_sent = 0
        bytes_received = 0
        for value in stats.values():
            if getattr(value, "type", None) == "outbound-rtp":
                packets_sent += int(getattr(value, "packetsSent", 0))
                bytes_sent += int(getattr(value, "bytesSent", 0))
            elif getattr(value, "type", None) == "inbound-rtp":
                packets_received += int(getattr(value, "packetsReceived", 0))
            elif getattr(value, "type", None) == "transport":
                bytes_received += int(getattr(value, "bytesReceived", 0))
        if packets_sent <= 0 or packets_received <= 0 or bytes_sent <= 0 or bytes_received <= 0:
            raise ProofFailure("transport_counters_empty")
        return {
            "status": "passed",
            "configuredPolicy": self._redemption.server_policy,
            "observedPath": selected.path,
            "localCandidateType": selected.local_candidate,
            "remoteCandidateType": selected.remote_candidate,
            "relayProtocol": selected.relay_protocol,
            "syntheticPeerTls443Endpoint": self._plan.synthetic_peer_tls_443_endpoint,
            "scope": "synthetic_browser_role_peer",
            "hostNonSilentFrames": _REQUIRED_TONE_FRAMES,
            "syntheticPeerNonSilentFrames": _REQUIRED_TONE_FRAMES,
            "packetsSent": packets_sent,
            "packetsReceived": packets_received,
            "bytesSent": bytes_sent,
            "transportBytesReceived": bytes_received,
            "setupMs": max(0, min(setup_ms, 120_000)),
        }

    async def aclose(self) -> None:
        failed = False
        tasks = [task for task in (self._signal_task,) if task is not None]
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tone.stop()
        try:
            await self._peer.close()
        except Exception:
            failed = True
        websocket = self._websocket
        if websocket is not None and not websocket.closed:
            try:
                await websocket.close()
            except Exception:
                failed = True
        session = self._session
        if session is not None and not session.closed:
            try:
                await session.close()
            except Exception:
                failed = True
        if failed:
            raise ProofFailure("media_cleanup_failed")


async def echo_probe_tone(stream: BidirectionalAudioStream, deadline: float) -> int:
    detector = ToneDetector()
    detected = 0
    while detected < _REQUIRED_TONE_FRAMES:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ProofFailure("host_audio_silent")
        try:
            frame = await asyncio.wait_for(stream.recv(), timeout=remaining)
        except (CallProviderError, MediaStreamError, asyncio.TimeoutError) as exc:
            raise ProofFailure("host_audio_silent") from exc
        if detector.detected(frame):
            detected += 1
        await stream.send(frame)
    return detected


async def prove_media_scenario(
    provider: P2PCallProvider,
    ticket: CallTicket,
    redemption: BrowserRedemption,
    scenario: ScenarioName,
    forced_relay_transport: Literal["udp", "tls"] = "udp",
) -> dict[str, Any]:
    plan = choose_ice_plan(redemption, scenario, forced_relay_transport)
    browser = SyntheticBrowserPeer(redemption, plan)
    deadline = time.monotonic() + _MEDIA_DEADLINE_SECONDS
    started = time.monotonic()
    try:
        await browser.open()
        async with provider.connect(ticket) as stream:
            await browser.wait_connected(deadline)
            host_task = asyncio.create_task(
                echo_probe_tone(stream, deadline),
                name="hosted-proof-host-audio",
            )
            browser_task = asyncio.create_task(
                browser.receive_echo(deadline),
                name="hosted-proof-browser-audio",
            )
            try:
                host_audio, browser_audio = await asyncio.gather(host_task, browser_task)
            finally:
                for task in (host_task, browser_task):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(host_task, browser_task, return_exceptions=True)
            if host_audio < _REQUIRED_TONE_FRAMES or browser_audio < _REQUIRED_TONE_FRAMES:
                raise ProofFailure("bidirectional_audio_incomplete")
            evidence = await browser.safe_evidence(int((time.monotonic() - started) * 1000))
            if scenario is ScenarioName.FORCED_RELAY:
                expected_tls = (
                    "passed" if forced_relay_transport == "tls" else "not_requested"
                )
                if (
                    evidence["localCandidateType"] != "relay"
                    or evidence["relayProtocol"] != forced_relay_transport
                    or evidence["syntheticPeerTls443Endpoint"] != expected_tls
                ):
                    raise ProofFailure("relay_policy_not_enforced")
        await browser.wait_terminal()
        return evidence
    finally:
        try:
            await asyncio.wait_for(browser.aclose(), timeout=5)
        except asyncio.TimeoutError as exc:
            raise ProofFailure("media_cleanup_timeout") from exc


async def wait_for_no_reservations(
    session: aiohttp.ClientSession,
    origin: str,
    admin_headers: dict[str, str],
    installation_id: str,
) -> bool:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        status, payload, _ = await request_json(
            session,
            "GET",
            f"{origin}/api/admin/installations/{installation_id}",
            headers=admin_headers,
        )
        if status == 200 and payload.get("reservations") == []:
            return True
        await asyncio.sleep(0.2)
    return False


async def request_json(
    session: aiohttp.ClientSession,
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any], Mapping[str, str]]:
    async with session.request(
        method,
        url,
        headers=headers,
        json=body,
        allow_redirects=False,
    ) as response:
        try:
            payload = await response.json()
        except (aiohttp.ContentTypeError, json.JSONDecodeError):
            payload = {}
        return response.status, payload, response.headers


def has_turn_credentials(payload: dict[str, Any]) -> bool:
    ice_servers = payload.get("iceServers")
    if not isinstance(ice_servers, list):
        return False
    for server in ice_servers:
        if not isinstance(server, dict):
            continue
        urls = server.get("urls", [])
        if not isinstance(urls, list):
            urls = [urls]
        if any(str(url).lower().startswith("turn") for url in urls):
            return bool(server.get("username")) and bool(server.get("credential"))
    return False


async def prove(
    origin: str,
    admin_token: str,
    forced_relay_transport: Literal["udp", "tls"] = "udp",
) -> dict[str, Any]:
    origin = origin.rstrip("/")
    admin_headers = {
        "Authorization": f"Bearer {admin_token}",
        "Content-Type": "application/json",
    }
    context = CallContext("staging-smoke", "staging-smoke", "staging-smoke")
    halted_revision: int | None = None
    installation_id: str | None = None
    installation_revoked = False
    enrollments = 0
    tickets_minted = 0
    results: dict[str, bool] = {}
    media: dict[str, dict[str, Any]] = {
        ScenarioName.FORCED_RELAY.value: {"status": "not_run"},
        ScenarioName.DIRECT_FIRST.value: {"status": "not_run"},
    }
    failure_code: str | None = None
    cleanup_failures: list[str] = []

    timeout = aiohttp.ClientTimeout(total=20)
    with tempfile.TemporaryDirectory(prefix="sleeptalk-staging-smoke-") as directory:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            try:
                async with session.get(origin, allow_redirects=False) as root:
                    results["asset"] = root.status == 200
                    results["no_referrer"] = root.headers.get("Referrer-Policy") == "no-referrer"

                status_code, status, _ = await request_json(
                    session,
                    "GET",
                    f"{origin}/api/admin/status",
                    headers=admin_headers,
                )
                if status_code != 200 or status.get("mode") != "running":
                    raise ProofFailure("staging_not_running")
                results["admin_status"] = True

                unauthenticated, unauthenticated_body, _ = await request_json(
                    session,
                    "POST",
                    f"{origin}/api/tickets",
                    headers={"Idempotency-Key": "unauthenticated-smoke"},
                    body={"ttlSeconds": 30},
                )
                results["missing_grant_rejected"] = (
                    unauthenticated == 401
                    and unauthenticated_body.get("error", {}).get("code") == "grant_invalid"
                )

                legacy, legacy_body, _ = await request_json(
                    session,
                    "POST",
                    f"{origin}/api/tickets",
                    headers={
                        "Authorization": f"Bearer {admin_token}",
                        "Idempotency-Key": "legacy-token-smoke",
                    },
                    body={"ttlSeconds": 30},
                )
                results["legacy_shared_token_rejected"] = (
                    legacy == 401 and legacy_body.get("error", {}).get("code") == "grant_invalid"
                )

                grant_store = ServiceGrantStore(Path(directory) / "credentials")
                provider = P2PCallProvider(origin, grant_store=grant_store)
                ticket = await provider.mint(context, ttl_seconds=90)
                tickets_minted += 1
                grant = grant_store.load(origin)
                if grant is None or not grant.enrolled:
                    raise ProofFailure("fresh_install_not_persisted")
                installation_id = grant.installation_id
                enrollments = 1
                results["fresh_install_minted"] = True

                browser_status, browser, _ = await request_json(
                    session,
                    "POST",
                    f"{origin}/api/tickets/{ticket.routing_id}/redeem",
                    headers={"User-Agent": "Mozilla/5.0 Safari/605.1.15"},
                    body={"ticket": ticket.token},
                )
                results["ordinary_browser_direct_first"] = (
                    browser_status == 200
                    and browser.get("iceTransportPolicy") == "all"
                    and has_turn_credentials(browser)
                )
                if not results["ordinary_browser_direct_first"]:
                    raise ProofFailure("ordinary_credentials_missing")
                direct_redemption = parse_browser_redemption(
                    origin,
                    ticket.routing_id,
                    browser,
                    "all",
                )
                try:
                    media[ScenarioName.DIRECT_FIRST.value] = await prove_media_scenario(
                        provider,
                        ticket,
                        direct_redemption,
                        ScenarioName.DIRECT_FIRST,
                    )
                except ProofFailure as exc:
                    media[ScenarioName.DIRECT_FIRST.value] = {
                        "status": "failed",
                        "code": exc.code,
                    }
                    raise
                results["direct_first_media"] = True
                results["ordinary_reservation_released"] = await wait_for_no_reservations(
                    session,
                    origin,
                    admin_headers,
                    grant.installation_id,
                )

                inspect_status, inspection, _ = await request_json(
                    session,
                    "GET",
                    f"{origin}/api/admin/installations/{grant.installation_id}",
                    headers=admin_headers,
                )
                results["installation_inspect"] = (
                    inspect_status == 200 and inspection.get("grant") == "active"
                )

                halt_status, halted, _ = await request_json(
                    session,
                    "POST",
                    f"{origin}/api/admin/control",
                    headers=admin_headers,
                    body={
                        "action": "halt",
                        "expectedRevision": status["revision"],
                        "reason": "staging smoke test",
                    },
                )
                if halt_status != 200 or halted.get("mode") != "halted":
                    raise ProofFailure("staging_halt_failed")
                halted_revision = int(halted["revision"])

                try:
                    await provider.mint(context, ttl_seconds=30)
                except CallProviderError as exc:
                    results["halt_blocks_mint"] = "temporarily disabled" in str(exc)
                else:
                    results["halt_blocks_mint"] = False

                resume_status, resumed, _ = await request_json(
                    session,
                    "POST",
                    f"{origin}/api/admin/control",
                    headers=admin_headers,
                    body={
                        "action": "resume",
                        "expectedRevision": halted_revision,
                        "reason": "staging smoke test complete",
                    },
                )
                if resume_status != 200 or resumed.get("mode") != "running":
                    raise ProofFailure("staging_resume_failed")
                halted_revision = None
                results["halt_resume"] = True

                relay_ticket = await provider.mint(context, ttl_seconds=90)
                tickets_minted += 1
                same_grant = grant_store.load(origin)
                results["grant_reused"] = same_grant == grant

                embedded_status, embedded, _ = await request_json(
                    session,
                    "POST",
                    f"{origin}/api/tickets/{relay_ticket.routing_id}/redeem",
                    headers={"User-Agent": "Telegram iOS WebView"},
                    body={"ticket": relay_ticket.token},
                )
                results["embedded_relay_credentials"] = (
                    embedded_status == 200
                    and embedded.get("iceTransportPolicy") == "relay"
                    and has_turn_credentials(embedded)
                )
                if not results["embedded_relay_credentials"]:
                    raise ProofFailure("embedded_relay_credentials_missing")
                forced_redemption = parse_browser_redemption(
                    origin,
                    relay_ticket.routing_id,
                    embedded,
                    "relay",
                )
                try:
                    media[ScenarioName.FORCED_RELAY.value] = await prove_media_scenario(
                        provider,
                        relay_ticket,
                        forced_redemption,
                        ScenarioName.FORCED_RELAY,
                        forced_relay_transport,
                    )
                except ProofFailure as exc:
                    media[ScenarioName.FORCED_RELAY.value] = {
                        "status": "failed",
                        "code": exc.code,
                    }
                    raise
                results["forced_relay_media"] = True
                results["embedded_reservation_released"] = await wait_for_no_reservations(
                    session,
                    origin,
                    admin_headers,
                    grant.installation_id,
                )

                revoke_status, revoked, _ = await request_json(
                    session,
                    "POST",
                    f"{origin}/api/admin/installations/{grant.installation_id}/revoke",
                    headers=admin_headers,
                    body={"reason": "completed staging smoke test"},
                )
                results["revoke"] = revoke_status == 200 and revoked.get("revoked") is True
                installation_revoked = results["revoke"] is True

                try:
                    await provider.mint(context, ttl_seconds=30)
                except CallProviderError as exc:
                    results["revoked_grant_rejected"] = "revoked" in str(exc)
                else:
                    results["revoked_grant_rejected"] = False
            except ProofFailure as exc:
                failure_code = exc.code
            except Exception:
                failure_code = "unexpected"
            finally:
                if halted_revision is not None:
                    try:
                        current_status, current, _ = await request_json(
                            session,
                            "GET",
                            f"{origin}/api/admin/status",
                            headers=admin_headers,
                        )
                        if (
                            current_status != 200
                            or current.get("mode") != "halted"
                            or current.get("revision") != halted_revision
                        ):
                            cleanup_failures.append("halt_revision_changed")
                        else:
                            resume_status, resumed, _ = await request_json(
                                session,
                                "POST",
                                f"{origin}/api/admin/control",
                                headers=admin_headers,
                                body={
                                    "action": "resume",
                                    "expectedRevision": halted_revision,
                                    "reason": "staging smoke cleanup",
                                },
                            )
                            if resume_status != 200 or resumed.get("mode") != "running":
                                cleanup_failures.append("resume_failed")
                    except Exception:
                        cleanup_failures.append("resume_failed")
                if installation_id is not None and not installation_revoked:
                    try:
                        revoke_status, revoked, _ = await request_json(
                            session,
                            "POST",
                            f"{origin}/api/admin/installations/{installation_id}/revoke",
                            headers=admin_headers,
                            body={"reason": "staging smoke cleanup"},
                        )
                        if revoke_status != 200 or revoked.get("revoked") is not True:
                            cleanup_failures.append("revoke_failed")
                    except Exception:
                        cleanup_failures.append("revoke_failed")

    failed = sorted(name for name, passed in results.items() if passed is not True)
    if failed and failure_code is None:
        failure_code = "control_check_failed"
    cleanup_ok = not cleanup_failures
    if not cleanup_ok and failure_code is None:
        failure_code = "cleanup_failed"
    return {
        "schemaVersion": 2,
        "ok": failure_code is None,
        "checks": sorted(name for name, passed in results.items() if passed is True),
        "media": media,
        "cleanupOk": cleanup_ok,
        "resourceUse": {
            "enrollments": enrollments,
            "ticketsMinted": tickets_minted,
            "relayAttemptUnitsReserved": tickets_minted * 4,
        },
        "forcedRelayTransport": forced_relay_transport,
        **({"failureCode": failure_code} if failure_code is not None else {}),
    }


async def run_with_safe_loop_errors(
    origin: str,
    admin_token: str,
    forced_relay_transport: Literal["udp", "tls"],
) -> dict[str, Any]:
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    background_failure = False

    def capture_background_failure(
        _loop: asyncio.AbstractEventLoop,
        _context: dict[str, Any],
    ) -> None:
        nonlocal background_failure
        background_failure = True

    loop.set_exception_handler(capture_background_failure)
    try:
        report = await prove(origin, admin_token, forced_relay_transport)
        await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(previous_handler)
    if background_failure and report.get("ok") is True:
        report["ok"] = False
        report["failureCode"] = "background_task_failed"
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--origin", default="https://call-staging.sleep.talk")
    parser.add_argument(
        "--forced-relay-transport",
        choices=("udp", "tls"),
        default="udp",
        help="TURN transport for the forced-relay synthetic peer",
    )
    args = parser.parse_args()
    admin_token = os.getenv("RENDEZVOUS_ADMIN_TOKEN", "").strip()
    if not admin_token:
        raise SystemExit("RENDEZVOUS_ADMIN_TOKEN is required")
    logger_levels = {
        logger_name: logging.getLogger(logger_name).level
        for logger_name in ("aiohttp", "aiortc", "aioice")
    }
    for logger_name in logger_levels:
        logging.getLogger(logger_name).setLevel(logging.CRITICAL)
    try:
        report = asyncio.run(
            run_with_safe_loop_errors(
                args.origin,
                admin_token,
                args.forced_relay_transport,
            )
        )
    except Exception:
        report = {
            "schemaVersion": 2,
            "ok": False,
            "checks": [],
            "media": {
                ScenarioName.FORCED_RELAY.value: {"status": "not_run"},
                ScenarioName.DIRECT_FIRST.value: {"status": "not_run"},
            },
            "cleanupOk": False,
            "failureCode": "unexpected",
            "forcedRelayTransport": args.forced_relay_transport,
        }
    finally:
        for logger_name, level in logger_levels.items():
            logging.getLogger(logger_name).setLevel(level)
    print(json.dumps(report, separators=(",", ":"), sort_keys=True))
    return 0 if report.get("ok") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
