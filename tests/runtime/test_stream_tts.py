from sleeptalk.stream_tts import StreamPCMAudioSource


def test_stream_source_underrun_returns_silence():
    source = StreamPCMAudioSource()
    frame = source.read()
    assert frame == b"\x00" * StreamPCMAudioSource.FRAME_BYTES


def test_stream_source_accounts_consumed_audio_without_padding_or_discarded_bytes():
    consumed = []
    source = StreamPCMAudioSource(on_read=consumed.append)
    source.feed(b"\x01" * (source.FRAME_BYTES + 100))
    source.read()
    source.read()  # Underrun silence while the partial frame is retained.
    assert consumed == [source.FRAME_BYTES]
    source.finish()
    source.read()
    source.read()
    assert consumed == [source.FRAME_BYTES, 100]

    source = StreamPCMAudioSource(on_read=consumed.append)
    source.feed(b"\x02" * source.FRAME_BYTES)
    source.discard()
    assert source.read() == b""
    assert consumed == [source.FRAME_BYTES, 100]


def test_stream_source_returns_exact_frames():
    source = StreamPCMAudioSource()
    source.feed(b"\x01" * (StreamPCMAudioSource.FRAME_BYTES * 2 + 100))
    frame1 = source.read()
    frame2 = source.read()
    assert frame1 == b"\x01" * StreamPCMAudioSource.FRAME_BYTES
    assert frame2 == b"\x01" * StreamPCMAudioSource.FRAME_BYTES
    # 100 leftover bytes, not finished: underrun silence
    assert source.read() == b"\x00" * StreamPCMAudioSource.FRAME_BYTES


def test_stream_source_finish_drains_then_ends():
    source = StreamPCMAudioSource()
    source.feed(b"\x02" * 100)
    source.finish()
    frame = source.read()
    assert len(frame) == StreamPCMAudioSource.FRAME_BYTES
    assert frame[:100] == b"\x02" * 100
    assert frame[100:] == b"\x00" * (StreamPCMAudioSource.FRAME_BYTES - 100)
    assert source.read() == b""


def test_stream_source_finish_empty_ends_immediately():
    source = StreamPCMAudioSource()
    source.finish()
    assert source.read() == b""


def test_stream_source_is_not_opus():
    assert StreamPCMAudioSource().is_opus() is False
