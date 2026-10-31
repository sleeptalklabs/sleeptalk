"""Pocket TTS callback audio, with bounded buffering and cooperative cancellation.

The pinned ONNX engine generates each text segment's latents before decoding
its audio in chunks. It is not Kyutai's native token-by-token streaming runtime.
The shared local phrase scheduler keeps text segments short for first audio.
"""
from __future__ import annotations

import asyncio
import queue
import threading
import wave
from collections.abc import AsyncIterator, Callable
from pathlib import Path

from sleeptalk.speech.common import SpeechError


def generation_config(directory: Path, voice: str):
    import numpy as np
    import sherpa_onnx

    from sleeptalk.speech.local_voices import MAX_REFERENCE_SECONDS, is_custom, reference_path

    # Uploaded references use opaque IDs; caller-supplied paths are never opened.
    references = {"pocket-bria": "bria.wav", "pocket-loona": "loona.wav"}
    if is_custom(voice):
        try:
            source = reference_path(voice)
        except ValueError as exc:
            raise SpeechError(str(exc)) from None
    elif voice in references:
        source = directory / "test_wavs" / references[voice]
    else:
        raise SpeechError(f"Unknown Pocket TTS voice: {voice}")
    with wave.open(str(source), "rb") as clip:
        if clip.getsampwidth() != 2 or clip.getnchannels() != 1:
            raise SpeechError("Pocket TTS reference must be mono 16-bit PCM.")
        samples = np.frombuffer(clip.readframes(clip.getnframes()), dtype=np.int16)
        config = sherpa_onnx.GenerationConfig()
        config.reference_audio = samples.astype(np.float32) / 32768.0
        config.reference_sample_rate = clip.getframerate()
    config.num_steps = 5
    config.extra = {"chunk_size": "5", "temperature": "0.7"}
    if is_custom(voice):
        # sherpa-onnx otherwise silently conditions on only the first 10 seconds,
        # even though the uploaded reference can contain up to 30 seconds.
        config.extra = {**config.extra, "max_reference_audio_len": str(MAX_REFERENCE_SECONDS)}
    return config


async def stream_pocket(engine, text: str, directory: Path, voice: str,
                        stopped: threading.Event, inference_lock: threading.Lock,
                        register: Callable[[asyncio.Task], None]) -> AsyncIterator[bytes]:
    """Copy native buffers before callbacks return; queue at most 160 ms of PCM."""
    import numpy as np
    import soxr

    loop = asyncio.get_running_loop()
    wake = asyncio.Event()
    chunks: queue.Queue[bytes] = queue.Queue(maxsize=8)

    def send(pcm: bytes) -> bool:
        while not stopped.is_set():
            try:
                chunks.put(pcm, timeout=0.05)
                loop.call_soon_threadsafe(wake.set)
                return True
            except queue.Full:
                pass
        return False

    def produce():
        with inference_lock:
            if stopped.is_set():
                return
            config = generation_config(directory, voice)
            resampler = soxr.ResampleStream(engine.sample_rate, 48_000, 1, dtype="float32")
            pending = bytearray()
            emitted = False

            def convert(samples, *, last=False):
                nonlocal emitted
                audio = resampler.resample_chunk(np.asarray(samples, dtype=np.float32), last=last)
                pending.extend(np.clip(audio * 32767, -32768, 32767).astype(np.int16).tobytes())
                while len(pending) >= 1920 or (last and pending):
                    pcm = bytes(pending[:1920])
                    del pending[:1920]
                    if not send(pcm):
                        return False
                    emitted = True
                return not stopped.is_set()

            def callback(samples, progress):
                if stopped.is_set():
                    return 0
                # Pocket's C++ callback returns 1 to continue and 0 to stop.
                return int(convert(samples))

            engine.generate(text, config, callback=callback)
            if not stopped.is_set():
                convert(np.empty(0, dtype=np.float32), last=True)
                if not emitted:
                    raise SpeechError("Pocket TTS returned no audio.")

    worker = asyncio.create_task(asyncio.to_thread(produce))
    register(worker)
    worker.add_done_callback(lambda _: wake.set())
    try:
        while True:
            if stopped.is_set():
                raise asyncio.CancelledError
            wake.clear()
            try:
                pcm = chunks.get_nowait()
            except queue.Empty:
                if worker.done():
                    await worker  # propagate inference errors or cancellation
                    break
                await wake.wait()
            else:
                yield pcm
    finally:
        stopped.set()
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
