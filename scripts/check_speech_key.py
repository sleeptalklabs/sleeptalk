"""Prove one aggregator key listens and speaks, and time it.

    OPENROUTER_API_KEY=... uv run python scripts/check_speech_key.py openrouter
    VENICE_API_KEY=...     uv run python scripts/check_speech_key.py venice
    GROQ_API_KEY=...       uv run python scripts/check_speech_key.py groq
    TOGETHER_API_KEY=...   uv run python scripts/check_speech_key.py together

Runs the same two checks setup runs: transcribe the bundled speech clip and
synthesize one sentence in the default voice. Prints the transcript, the
audio length, and the round-trip times. The key is read from the
environment and never printed.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time


async def main(provider: str) -> int:
    from sleeptalk.builder import stt_spec, tts_spec
    from sleeptalk.config import STTConfig, TTSConfig
    from sleeptalk.defaults import register_defaults
    from sleeptalk.speech.openai_audio import PRESETS, OpenAIAudioTTS
    from sleeptalk.stt_roundtrip import openai_audio_roundtrip

    register_defaults()
    if provider not in PRESETS:
        print(f"unknown service {provider!r}; one of {', '.join(PRESETS)}", file=sys.stderr)
        return 2
    preset = PRESETS[provider]
    key = os.environ.get(preset.env, "").strip()
    if not key:
        print(f"set {preset.env} in the environment first", file=sys.stderr)
        return 2
    print(f"{preset.label}: listening with {preset.stt_model}, speaking with {preset.tts_voice} on {preset.tts_model}")
    transcript, ms = await openai_audio_roundtrip(stt_spec(STTConfig(provider=provider)), key)
    print(f"  listen  {ms:6.0f} ms  transcript: {transcript!r}")
    tts = OpenAIAudioTTS(tts_spec(TTSConfig(provider=provider)), api_key=key)
    started = time.monotonic()
    first = None
    total = 0
    async for chunk in tts.stream_pcm("Hello. This is a quick check of the speaking side."):
        if first is None:
            first = (time.monotonic() - started) * 1000
        total += len(chunk)
    done = (time.monotonic() - started) * 1000
    print(f"  speak   first audio {first or 0:6.0f} ms, complete {done:6.0f} ms, {total / 96_000:.1f} s of speech")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "openrouter")))
