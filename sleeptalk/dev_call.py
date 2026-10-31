"""M0 browser-to-host WebRTC echo fixture.

The rendezvous relays signaling only. Audio flows directly between the
browser and this process as Opus over WebRTC; the received audio track is
published straight back to the browser so a human can prove microphone,
playback, codec negotiation, and NAT traversal before production tickets or
the agent runtime are involved.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Callable
from typing import Any
from urllib.parse import quote, urlencode, urlsplit, urlunsplit
from uuid import uuid4

import aiohttp
from aiortc import (
    MediaStreamTrack,
    RTCConfiguration,
    RTCIceServer,
    RTCPeerConnection,
    RTCRtpSender,
    RTCSessionDescription,
)
from aiortc.sdp import candidate_from_sdp

SIGNAL_VERSION = 1
DEFAULT_RENDEZVOUS_URL = "http://127.0.0.1:8788"


class DevCallError(RuntimeError):
    """Human-readable development-call failure."""


class EchoAudioTrack(MediaStreamTrack):
    """Forward every frame from a remote audio track back to its sender."""

    kind = "audio"

    def __init__(self, source: MediaStreamTrack) -> None:
        super().__init__()
        self.source = source

    async def recv(self):
        return await self.source.recv()


def dev_room_urls(rendezvous_url: str, room_id: str) -> tuple[str, str]:
    """Return (join URL, agent signaling URL) for one development room."""

    base = urlsplit(rendezvous_url.strip())
    if base.scheme not in {"http", "https"} or not base.netloc:
        raise DevCallError(
            "The rendezvous URL must start with http:// or https:// and include a host."
        )
    if not 8 <= len(room_id) <= 64 or any(char not in _ROOM_CHARS for char in room_id):
        raise DevCallError(
            "The development room ID must be 8-64 letters, numbers, - or _."
        )

    root_path = base.path.rstrip("/")
    join_path = f"{root_path}/" if root_path else "/"
    join = urlunsplit(
        (
            base.scheme,
            base.netloc,
            join_path,
            "",
            f"/dev/{quote(room_id, safe='-_')}",
        )
    )
    signal_scheme = "wss" if base.scheme == "https" else "ws"
    signal_path = f"{root_path}/api/dev/rooms/{quote(room_id, safe='-_')}/signal"
    signal = urlunsplit(
        (signal_scheme, base.netloc, signal_path, urlencode({"role": "agent"}), "")
    )
    return join, signal


async def run_dev_echo_call(
    rendezvous_url: str = DEFAULT_RENDEZVOUS_URL,
    *,
    room_id: str = "",
    on_ready: Callable[[str], None] = print,
) -> None:
    """Connect the echo peer, print its browser URL, and run until hangup."""

    room_id = room_id or secrets.token_urlsafe(12)
    join_url, signal_url = dev_room_urls(rendezvous_url, room_id)
    peer = RTCPeerConnection(
        RTCConfiguration(
            iceServers=[
                RTCIceServer(
                    urls=[
                        "stun:stun.cloudflare.com:3478",
                        "stun:stun.cloudflare.com:53",
                    ]
                )
            ]
        )
    )
    pending_candidates: list[dict[str, Any] | None] = []
    echo_attached = False

    @peer.on("track")
    def on_track(track: MediaStreamTrack) -> None:
        nonlocal echo_attached
        if track.kind != "audio" or echo_attached:
            return
        echo_attached = True
        peer.addTrack(EchoAudioTrack(track))

    try:
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            try:
                async with session.ws_connect(signal_url, heartbeat=15) as websocket:
                    on_ready(join_url)
                    async for message in websocket:
                        if message.type == aiohttp.WSMsgType.TEXT:
                            envelope = _parse_envelope(message.data)
                            if envelope is None:
                                continue
                            message_type = envelope["type"]
                            payload = envelope["payload"]
                            if message_type == "webrtc.offer":
                                await peer.setRemoteDescription(
                                    RTCSessionDescription(
                                        sdp=str(payload.get("sdp", "")),
                                        type="offer",
                                    )
                                )
                                _prefer_opus(peer)
                                for candidate in pending_candidates:
                                    await _add_ice_candidate(peer, candidate)
                                pending_candidates.clear()
                                answer = await peer.createAnswer()
                                await peer.setLocalDescription(answer)
                                local = peer.localDescription
                                if local is None:
                                    raise DevCallError("The echo peer could not create an answer.")
                                await websocket.send_json(
                                    _envelope(
                                        "webrtc.answer",
                                        {"sdp": local.sdp, "type": local.type},
                                    )
                                )
                            elif message_type == "webrtc.ice":
                                candidate = payload.get("candidate")
                                if peer.remoteDescription is None:
                                    pending_candidates.append(
                                        candidate if isinstance(candidate, dict) else None
                                    )
                                else:
                                    await _add_ice_candidate(
                                        peer, candidate if isinstance(candidate, dict) else None
                                    )
                            elif message_type == "webrtc.ice_complete":
                                if peer.remoteDescription is None:
                                    pending_candidates.append(None)
                                else:
                                    await peer.addIceCandidate(None)
                            elif message_type in {"call.hangup", "call.ended"}:
                                return
                            elif message_type == "call.error":
                                raise DevCallError("The browser could not finish the call setup.")
                        elif message.type == aiohttp.WSMsgType.ERROR:
                            raise DevCallError("The signaling connection failed.")
            except aiohttp.WSServerHandshakeError as exc:
                raise DevCallError(
                    f"The rendezvous rejected the development call (HTTP {exc.status})."
                ) from exc
            except aiohttp.ClientError as exc:
                raise DevCallError(
                    "The rendezvous is unreachable. Start the staging service or pass "
                    "--rendezvous with its public URL."
                ) from exc
    finally:
        await peer.close()


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
        raise DevCallError("This host's WebRTC stack does not support Opus audio.")
    for transceiver in peer.getTransceivers():
        if transceiver.kind == "audio":
            transceiver.setCodecPreferences(opus)


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
    if not isinstance(value.get("type"), str):
        return None
    payload = value.get("payload")
    if not isinstance(payload, dict):
        return None
    return value


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


_ROOM_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")
