from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

from sleeptalk.voice_loop.models import bundled_model_path

_HASHES = {
    "silero_vad": "597d30b3ec076608d059477bb14cfeffdf951bf5cae370d38f65d33bbfe82004",
    "smart_turn_v3": "2bb026316b14a660486a75b1733cd3fbab8c2fd0314dc9af7be49f8cca967e4f",
}


def test_bundled_weights_match_known_hashes() -> None:
    for name, digest in _HASHES.items():
        path = Path(bundled_model_path(name))
        assert path.is_file(), path
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest


def test_silero_gate_loads_without_pipecat() -> None:
    pytest.importorskip("onnxruntime")
    sys.modules.pop("pipecat", None)
    from sleeptalk.voice.configs import SileroVadConfig
    from sleeptalk.voice_loop.vad import SileroVadGate

    SileroVadGate(SileroVadConfig())
    assert "pipecat" not in sys.modules


@pytest.mark.asyncio
async def test_smart_turn_scores_silence_without_pipecat() -> None:
    pytest.importorskip("onnxruntime")
    pytest.importorskip("soxr")
    sys.modules.pop("pipecat", None)
    from sleeptalk.voice_loop.smart_turn import SmartTurnScorer

    silence = b"\x00" * (48000 * 2 * 2)
    probability = await SmartTurnScorer().score(silence)
    assert 0.0 <= probability <= 1.0
    assert "pipecat" not in sys.modules
