from __future__ import annotations

from importlib import resources
from typing import Literal

VoiceModel = Literal["silero_vad", "smart_turn_v3"]

_FILES: dict[VoiceModel, str] = {
    "silero_vad": "silero_vad.onnx",
    "smart_turn_v3": "smart-turn-v3.2-cpu.onnx",
}


def bundled_model_path(name: VoiceModel) -> str:
    filename = _FILES[name]
    return str(resources.files("sleeptalk.voice_loop.data").joinpath(filename))
