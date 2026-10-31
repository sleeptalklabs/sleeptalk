from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from aiortc import AudioStreamTrack, RTCConfiguration, RTCPeerConnection
from aiortc.sdp import SessionDescription


SCRIPT = Path(__file__).parents[1] / "scripts" / "prove_hosted_staging.py"
SPEC = importlib.util.spec_from_file_location("hosted_staging_proof", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
proof = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = proof
SPEC.loader.exec_module(proof)


def redemption_payload(*, policy: str = "relay") -> dict[str, object]:
    routing_id = "r" * 24
    return {
        "browserSignalUrl": (
            f"wss://call-staging.sleep.talk/api/tickets/{routing_id}/signal"
        ),
        "browserAccessKey": "browser-secret",
        "iceTransportPolicy": policy,
        "iceServers": [
            {
                "urls": [
                    "stun:stun.cloudflare.com:3478",
                    "turn:turn.cloudflare.com:3478?transport=udp",
                    "turns:turn.cloudflare.com:443?transport=tcp",
                ],
                "username": "turn-user",
                "credential": "turn-secret",
            }
        ],
    }


def test_redemption_secrets_are_not_in_representations() -> None:
    redemption = proof.parse_browser_redemption(
        "https://call-staging.sleep.talk",
        "r" * 24,
        redemption_payload(),
        "relay",
    )
    plan = proof.choose_ice_plan(
        redemption,
        proof.ScenarioName.FORCED_RELAY,
        "tls",
    )

    rendered = f"{redemption!r} {plan!r}"
    assert "browser-secret" not in rendered
    assert "turn-user" not in rendered
    assert "turn-secret" not in rendered
    assert plan.relay_only is True
    assert plan.synthetic_peer_tls_443_endpoint == "passed"
    assert plan.servers[0].urls == (
        "turns:turn.cloudflare.com:443?transport=tcp",
    )


def test_forced_relay_udp_selects_one_udp_endpoint() -> None:
    redemption = proof.parse_browser_redemption(
        "https://call-staging.sleep.talk",
        "r" * 24,
        redemption_payload(),
        "relay",
    )

    plan = proof.choose_ice_plan(
        redemption,
        proof.ScenarioName.FORCED_RELAY,
        "udp",
    )

    assert plan.relay_only is True
    assert plan.synthetic_peer_tls_443_endpoint == "not_requested"
    assert plan.servers[0].urls == (
        "turn:turn.cloudflare.com:3478?transport=udp",
    )


def test_redemption_rejects_cross_origin_signaling() -> None:
    payload = redemption_payload()
    payload["browserSignalUrl"] = (
        "wss://attacker.example/api/tickets/" + "r" * 24 + "/signal"
    )

    with pytest.raises(proof.ProofFailure, match="invalid_redemption"):
        proof.parse_browser_redemption(
            "https://call-staging.sleep.talk",
            "r" * 24,
            payload,
            "relay",
        )


def test_forced_relay_answer_keeps_only_the_host_relay() -> None:
    sdp = "\r\n".join(
        [
            "v=0",
            "o=- 1 1 IN IP4 0.0.0.0",
            "s=-",
            "t=0 0",
            "a=group:BUNDLE 0",
            "m=audio 9 UDP/TLS/RTP/SAVPF 96",
            "c=IN IP4 0.0.0.0",
            "a=mid:0",
            "a=sendrecv",
            "a=rtcp-mux",
            "a=rtpmap:96 opus/48000/2",
            "a=ice-ufrag:test",
            "a=ice-pwd:testtesttesttesttesttest",
            "a=candidate:1 1 udp 2130706431 192.168.1.2 5000 typ host",
            (
                "a=candidate:2 1 udp 1694498815 203.0.113.5 40000 "
                "typ srflx raddr 192.168.1.2 rport 5000"
            ),
            (
                "a=candidate:3 1 udp 16777215 198.51.100.5 50000 "
                "typ relay raddr 203.0.113.5 rport 40000"
            ),
            "a=end-of-candidates",
            "",
        ]
    )

    filtered = SessionDescription.parse(proof._relay_answer_sdp(sdp))

    assert [
        candidate.type
        for media in filtered.media
        for candidate in media.ice_candidates
    ] == ["relay"]


@pytest.mark.asyncio
async def test_tone_detector_recognizes_three_probe_frames() -> None:
    track = proof.SyntheticToneTrack()
    detector = proof.ToneDetector()

    detected = [detector.detected(await track.recv()) for _ in range(3)]
    assert detected == [True, True, True]


@pytest.mark.asyncio
async def test_relay_adapter_removes_selectable_host_protocols() -> None:
    peer = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    peer.addTrack(AudioStreamTrack())
    try:
        proof.AiortcProbeAdapter.set_relay_only_before_gathering(peer)
        offer = await peer.createOffer()
        await peer.setLocalDescription(offer)
        connection = proof.AiortcProbeAdapter._connection(peer)
        assert len(connection._protocols) >= 1

        retained = connection._protocols[0]
        retained.local_candidate.type = "relay"
        connection._local_candidates = [retained.local_candidate]
        proof.AiortcProbeAdapter.prune_non_relay_protocols(peer)

        assert connection._protocols == [retained]
        assert all(
            protocol.local_candidate.type == "relay"
            for protocol in connection._protocols
        )
    finally:
        await peer.close()
