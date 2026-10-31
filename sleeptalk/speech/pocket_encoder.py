"""Repair the pinned Pocket export's traced, fixed-length input padding.

The January 2026 encoder pads/crops every input to 24960 samples (1.04 s).
Restore Mimi's ceil(length / frame_size) * frame_size padding in a derived
file, leaving the downloaded, checksum-verified model untouched.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import tempfile

from sleeptalk.speech.common import SpeechError

_SOURCE_SHA256 = "e8f2f6d301ffb96e398b138a7dc6d3038622d236044636b73d920bab85890260"
_DERIVED_NAME = "encoder-dynamic-v1.onnx"


def _repair(model):
    import numpy as np
    from onnx import helper, numpy_helper

    nodes = []
    replaced = 0
    for node in model.graph.node:
        if node.name == "/Constant_2":
            if (node.op_type != "Constant" or list(node.output) != ["/Constant_2_output_0"]
                    or numpy_helper.to_array(node.attribute[0].t).item() != 24960):
                raise SpeechError("Unexpected Pocket encoder padding; download the model again.")
            # Mimi's 24 kHz / 12.5 Hz frame rate gives 1920 samples per frame.
            nodes.extend([
                helper.make_node("Constant", [], ["sleeptalk_frame"],
                                 value=numpy_helper.from_array(np.array(1920, dtype=np.int64))),
                helper.make_node("Constant", [], ["sleeptalk_round"],
                                 value=numpy_helper.from_array(np.array(1919, dtype=np.int64))),
                helper.make_node("Add", ["/Gather_output_0", "sleeptalk_round"], ["sleeptalk_rounded"]),
                helper.make_node("Div", ["sleeptalk_rounded", "sleeptalk_frame"], ["sleeptalk_frames"]),
                helper.make_node("Mul", ["sleeptalk_frames", "sleeptalk_frame"], ["/Constant_2_output_0"]),
            ])
            replaced += 1
        else:
            nodes.append(node)
    if replaced != 1:
        raise SpeechError("Unexpected Pocket encoder graph; download the model again.")
    del model.graph.node[:]
    model.graph.node.extend(nodes)
    return model


def encoder_path(directory: Path) -> Path:
    target = directory / _DERIVED_NAME
    if target.is_file():
        return target
    source = directory / "encoder.onnx"
    if hashlib.sha256(source.read_bytes()).hexdigest() != _SOURCE_SHA256:
        raise SpeechError("Pocket encoder does not match the pinned model; download it again.")
    try:
        import onnx
    except ImportError:
        raise SpeechError("Pocket voice cloning needs the updated local runtime. "
                          "Run sleeptalk settings to install sleeptalk[local].") from None
    model = _repair(onnx.load(str(source)))
    onnx.checker.check_model(model)
    # Concurrent preview/call workers may prepare the same derived file.
    fd, temporary = tempfile.mkstemp(prefix=".encoder-", suffix=".onnx", dir=directory)
    os.close(fd)
    try:
        onnx.save(model, temporary)
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return target
