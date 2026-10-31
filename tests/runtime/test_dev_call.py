import asyncio

import pytest
from sleeptalk.dev_call import (
    DevCallError,
    EchoAudioTrack,
    _parse_envelope,
    dev_room_urls,
)


def test_dev_room_urls_build_join_fragment_and_agent_websocket():
    join, signal = dev_room_urls("https://calls.example.test", "room_1234")

    assert join == "https://calls.example.test/#/dev/room_1234"
    assert signal == (
        "wss://calls.example.test/api/dev/rooms/room_1234/signal?role=agent"
    )


def test_dev_room_urls_preserve_origin_path_and_use_ws_for_local_dev():
    join, signal = dev_room_urls("http://127.0.0.1:8788/staging/", "room-1234")

    assert join == "http://127.0.0.1:8788/staging/#/dev/room-1234"
    assert signal == (
        "ws://127.0.0.1:8788/staging/api/dev/rooms/room-1234/signal?role=agent"
    )


@pytest.mark.parametrize(
    "url, room",
    [
        ("calls.example.test", "room-1234"),
        ("ftp://calls.example.test", "room-1234"),
        ("https://calls.example.test", "short"),
        ("https://calls.example.test", "bad room"),
        ("https://calls.example.test", "x" * 65),
    ],
)
def test_dev_room_urls_reject_bad_origins_and_room_ids(url, room):
    with pytest.raises(DevCallError):
        dev_room_urls(url, room)


def test_parse_envelope_accepts_version_one_object_payload_only():
    assert _parse_envelope(
        '{"v":1,"id":"m1","type":"peer.ready","payload":{}}'
    ) == {"v": 1, "id": "m1", "type": "peer.ready", "payload": {}}
    assert _parse_envelope("not json") is None
    assert _parse_envelope('{"v":2,"type":"peer.ready","payload":{}}') is None
    assert _parse_envelope('{"v":1,"type":"peer.ready","payload":[]}') is None


def test_echo_audio_track_forwards_the_source_frame():
    frame = object()

    class Source:
        async def recv(self):
            return frame

    echo = EchoAudioTrack(Source())

    assert asyncio.run(echo.recv()) is frame
