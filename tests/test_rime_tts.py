"""Rime sends the operator's speed step as speedAlpha, in the model's direction."""

import pytest

from sleeptalk.providers import ProviderSpec
from sleeptalk.speech.rime import RimeTTS


def _capture(monkeypatch):
    import sleeptalk.speech.rime as module

    requests = []

    class Response:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def read(self):
            return b"RIFF"

    class Client(Response):
        def post(self, url, **kwargs):
            requests.append(kwargs)
            return Response()

    monkeypatch.setattr(module.aiohttp, "ClientSession", Client)
    return requests


@pytest.mark.parametrize(
    ("model", "speed", "expected"),
    [
        ("mistv3", 1.25, 1.25),  # higher is faster
        ("arcana", 1.5, 1.5),
        ("arcana", 2.0, 1.5),  # retired 2x step snaps onto the staircase first
        ("mistv2", 1.25, 0.8),  # lower is faster, so the step is reciprocated
        ("mist", 0.5, 2.0),
    ],
)
async def test_speed_step_becomes_model_direction_speed_alpha(monkeypatch, model, speed, expected):
    requests = _capture(monkeypatch)
    tts = RimeTTS(ProviderSpec(provider="rime", model=model, voice_id="cove", speed=speed), api_key="k")
    await tts.synthesize_wav_bytes("Hello.")
    assert requests[0]["json"]["speedAlpha"] == expected


@pytest.mark.parametrize("speed", [None, 1.0])
async def test_default_speed_sends_no_speed_alpha(monkeypatch, speed):
    requests = _capture(monkeypatch)
    tts = RimeTTS(ProviderSpec(provider="rime", model="mistv3", voice_id="cove", speed=speed), api_key="k")
    await tts.synthesize_wav_bytes("Hello.")
    assert "speedAlpha" not in requests[0]["json"]
