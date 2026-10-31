"""ElevenLabs accepts speed 0.7 to 1.2; the operator step is clamped on the wire.

On 2026-09-17 a profile at 1.25x reached the API unclamped and every sentence
came back 400 invalid_voice_settings, so the caller heard nothing.
"""

import pytest

from sleeptalk.providers import ProviderSpec
from sleeptalk.speech.elevenlabs import ElevenLabsTTS


def _capture(monkeypatch):
    import sleeptalk.speech.elevenlabs as module

    requests = []

    class Response:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def read(self):
            return b""

    class Client(Response):
        def post(self, url, **kwargs):
            requests.append(kwargs)
            return Response()

    monkeypatch.setattr(module.aiohttp, "ClientSession", Client)
    monkeypatch.setattr(module, "_elevenlabs_wav_bytes", lambda audio, fmt: b"RIFF")
    return requests


@pytest.mark.parametrize(
    ("speed", "expected"),
    [(0.5, 0.7), (1.0, 1.0), (1.25, 1.2), (1.5, 1.2), (2.0, 1.2)],
)
async def test_speed_step_is_clamped_to_accepted_interval(monkeypatch, speed, expected):
    requests = _capture(monkeypatch)
    tts = ElevenLabsTTS(
        ProviderSpec(provider="elevenlabs", model="eleven_multilingual_v2", voice_id="v", speed=speed),
        api_key="k",
    )
    await tts.synthesize_wav_bytes("Hello.")
    assert requests[0]["json"]["voice_settings"] == {"speed": expected}


async def test_no_speed_sends_no_voice_settings(monkeypatch):
    requests = _capture(monkeypatch)
    tts = ElevenLabsTTS(
        ProviderSpec(provider="elevenlabs", model="eleven_multilingual_v2", voice_id="v"),
        api_key="k",
    )
    await tts.synthesize_wav_bytes("Hello.")
    assert "voice_settings" not in requests[0]["json"]
