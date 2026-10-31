"""Replay a mono PCM WAV through local streaming STT at microphone cadence.

No microphone, service restart, or cloud calls. Model must already be installed.
Example: uv run --extra local-streaming python scripts/benchmark_local_streaming.py clip.wav
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
import wave
from pathlib import Path

from sleeptalk.providers import ProviderSpec
from sleeptalk.speech.local_models import resample_int16
from sleeptalk.speech.local_streaming_stt import LocalStreamingSTT, MODEL_ID


async def benchmark(args):
    with wave.open(str(args.wav)) as wav:
        if wav.getnchannels() != 1 or wav.getsampwidth() != 2:
            raise ValueError("Use a mono signed 16-bit PCM WAV")
        frames = wav.getnframes() if not args.seconds else min(wav.getnframes(), int(args.seconds*wav.getframerate()))
        pcm = resample_int16(wav.readframes(frames), wav.getframerate(), 48_000)
    duration = len(pcm)/96_000
    stt = LocalStreamingSTT(ProviderSpec(provider='local',model=MODEL_ID),models_root=args.models_root)
    try:
        began=time.perf_counter()
        await stt.warmup()
        load_ms=round((time.perf_counter()-began)*1000)
        start=time.perf_counter()
        text=''
        partials=[]
        segment_finishes=[]
        was_final=False
        # Feed real trailing silence, so the native segmenter can finish before
        # we explicitly flush. Timings are relative to file start/end, not word alignment.
        replay=pcm+bytes(96_000)
        for off in range(0,len(replay),1920):
            await asyncio.sleep(max(0,start+off/96_000-time.perf_counter()))
            await stt.append_pcm(0,replay[off:off+1920])
            signal=stt.turn_completion_signal(0)
            elapsed=round((time.perf_counter()-start)*1000)
            if signal.transcript != text:
                text=signal.transcript
                partials.append({'at_ms':elapsed,'text':text})
            if signal.is_final and not was_final:
                segment_finishes.append(elapsed)
            was_final=signal.is_final
        began=time.perf_counter()
        final=await stt.finish_transcription(0)
        return {'model':MODEL_ID,'audio_ms':round(duration*1000),'load_ms':load_ms,
                'first_partial_ms':partials[0]['at_ms'] if partials else None,
                'segment_complete_ms':segment_finishes,'finish_ms':round((time.perf_counter()-began)*1000),
                'final':final,'partials':partials}
    finally:
        await stt.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('wav',type=Path)
    parser.add_argument('--models-root',type=Path)
    parser.add_argument('--seconds',type=float,default=0,help='Optional excerpt length')
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    result=asyncio.run(benchmark(args))
    encoded=json.dumps(result,indent=2)
    if args.output:
        args.output.write_text(encoded+'\n')
    print(encoded)


if __name__ == '__main__':
    main()
