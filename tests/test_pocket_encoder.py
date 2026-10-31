"""Reference padding must retain speech beyond the export's traced first second."""
import hashlib

import numpy as np
import pytest

from sleeptalk.speech import pocket_encoder
from sleeptalk.speech.common import SpeechError

onnx = pytest.importorskip('onnx')
ort = pytest.importorskip('onnxruntime')


def traced_encoder():
    h = onnx.helper
    # Reproduce the pinned export's input-length arithmetic and negative Pad.
    nodes = [
        h.make_node('Shape', ['audio'], ['shape']),
        h.make_node('Constant', [], ['axis'], value=onnx.numpy_helper.from_array(np.array(2, np.int64))),
        h.make_node('Gather', ['shape', 'axis'], ['/Gather_output_0']),
        h.make_node('Constant', [], ['/Constant_2_output_0'], name='/Constant_2',
                    value=onnx.numpy_helper.from_array(np.array(24960, np.int64))),
        h.make_node('Sub', ['/Constant_2_output_0', '/Gather_output_0'], ['padding']),
        h.make_node('Constant', [], ['zero_axis'], value=onnx.numpy_helper.from_array(np.array([0], np.int64))),
        h.make_node('Unsqueeze', ['padding', 'zero_axis'], ['pad_end']),
        h.make_node('Constant', [], ['zeros'], value=onnx.numpy_helper.from_array(np.zeros(5, np.int64))),
        h.make_node('Concat', ['zeros', 'pad_end'], ['pads'], axis=0),
        h.make_node('Pad', ['audio', 'pads'], ['padded']),
    ]
    graph = h.make_graph(nodes, 'reference-padding',
                        [h.make_tensor_value_info('audio', onnx.TensorProto.FLOAT, [1, 1, 'samples'])],
                        [h.make_tensor_value_info('padded', onnx.TensorProto.FLOAT, [1, 1, 'padded_samples'])])
    return h.make_model(graph, opset_imports=[h.make_opsetid('', 17)], ir_version=8)


@pytest.mark.parametrize('seconds', [1.04, 3, 13, 15.024, 30])
def test_repaired_encoder_preserves_entire_reference_and_only_pads_frame_end(seconds):
    x = np.arange(round(seconds * 24000), dtype=np.float32)[None, None, :]
    original = ort.InferenceSession(traced_encoder().SerializeToString())
    broken = original.run(None, {'audio': x})[0]
    assert broken.shape[-1] == 24960
    repaired = pocket_encoder._repair(traced_encoder())
    onnx.checker.check_model(repaired)
    engine = ort.InferenceSession(repaired.SerializeToString())
    result = engine.run(None, {'audio': x})[0]
    np.testing.assert_array_equal(result[..., :x.shape[-1]], x)
    assert result.shape[-1] == ((x.shape[-1] + 1919) // 1920) * 1920
    assert not np.any(result[..., x.shape[-1]:])
    if seconds == 1.04:
        np.testing.assert_array_equal(result, broken)


def test_preparation_keeps_source_intact_and_reuses_complete_derived_file(tmp_path, monkeypatch):
    source = tmp_path / 'encoder.onnx'
    source.write_bytes(traced_encoder().SerializeToString())
    before = source.read_bytes()
    monkeypatch.setattr(pocket_encoder, '_SOURCE_SHA256', hashlib.sha256(before).hexdigest())
    result = pocket_encoder.encoder_path(tmp_path)
    assert source.read_bytes() == before
    assert result != source
    onnx.checker.check_model(str(result))
    monkeypatch.setattr(pocket_encoder, '_repair', lambda _: pytest.fail('should reuse derived model'))
    assert pocket_encoder.encoder_path(tmp_path) == result
    assert not list(tmp_path.glob('.encoder-*'))


def test_unrecognized_model_fails_without_modifying_anything(tmp_path):
    (tmp_path / 'encoder.onnx').write_bytes(b'unexpected model')
    with pytest.raises(SpeechError, match='pinned model'):
        pocket_encoder.encoder_path(tmp_path)
    assert sorted(p.name for p in tmp_path.iterdir()) == ['encoder.onnx']


def test_failed_preparation_leaves_no_cache_file(tmp_path, monkeypatch):
    source = tmp_path / 'encoder.onnx'
    source.write_bytes(traced_encoder().SerializeToString())
    monkeypatch.setattr(pocket_encoder, '_SOURCE_SHA256', hashlib.sha256(source.read_bytes()).hexdigest())
    def fail(*args):
        raise OSError('disk full')
    monkeypatch.setattr(onnx, 'save', fail)
    with pytest.raises(OSError, match='disk full'):
        pocket_encoder.encoder_path(tmp_path)
    assert sorted(p.name for p in tmp_path.iterdir()) == ['encoder.onnx']
