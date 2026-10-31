"""Record the short voice samples the settings page plays before any key exists.

Maintainer tool. It reads provider keys from a local env file (never
printed, never written anywhere), speaks one line in each curated voice
through Sleeptalk's own adapters, compresses it with ffmpeg, and writes
``sleeptalk/voice_samples/<provider>/<voice>.mp3`` plus ``manifest.json``.
The samples ship in the package; people listening to them need no key.

    uv run python scripts/make_voice_samples.py --env-file path/to/.env
    uv run python scripts/make_voice_samples.py --provider cartesia --force
    uv run python scripts/make_voice_samples.py --normalize   # level the ones on disk
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

OUT = REPO / "sleeptalk" / "voice_samples"
PROVIDERS = ("cartesia", "deepgram", "elevenlabs", "openrouter", "venice", "local")
KEY_ENV = {
    "cartesia": "CARTESIA_API_KEY", "deepgram": "DEEPGRAM_API_KEY", "elevenlabs": "ELEVENLABS_API_KEY",
    "openrouter": "OPENROUTER_API_KEY", "venice": "VENICE_API_KEY",
    "local": "",  # no key: local speech runs on this computer
}
LINES = {
    "en": "Hi, I'm {name}. This is how I'll sound when your agent answers.",
    # No name: a romanized name read inside Chinese comes out as other syllables.
    "zh": "你好。你的助手回答时，我就是这个声音。",
    # The native voices say the same line in their own language, without a name.
    "es": "Hola. Así sonaré cuando responda tu agente.",
    "fr": "Bonjour. Voici ma voix quand votre agent vous répond.",
    "de": "Hallo. So klinge ich, wenn dein Agent antwortet.",
    "it": "Ciao. Questa è la mia voce quando risponde il tuo agente.",
    "pt": "Olá. É assim que vou soar quando o seu agente responder.",
    "nl": "Hallo. Zo klink ik wanneer je agent antwoordt.",
    "ja": "こんにちは。エージェントが答えるとき、この声でお話しします。",
    "ko": "안녕하세요. 에이전트가 답할 때 이 목소리로 말해요.",
    "hi": "नमस्ते। जब आपका एजेंट जवाब देगा, तो आवाज़ ऐसी होगी।",
}
# The voice's own language, for the line and for the adapter's language setting.
TTS_LANGUAGE = {"es": "es-ES", "fr": "fr-FR", "de": "de-DE", "it": "it-IT", "pt": "pt-BR", "nl": "nl-NL",
                "ja": "ja-JP", "ko": "ko-KR", "hi": "hi-IN", "zh": "zh-CN", "en": "en-US"}
# Extra samples beyond the curated list: the Chinese voices of the measured
# Chinese pick on OpenRouter.
EXTRA = (
    {"provider": "openrouter", "name": "Longanhuan", "voice_id": "longanhuan_v3.6",
     "model": "qwen/qwen-audio-3.0-tts-flash", "language": "zh"},
    {"provider": "openrouter", "name": "Loongjohn", "voice_id": "loongjohn",
     "model": "qwen/qwen-audio-3.0-tts-flash", "language": "zh"},
)
# Voices that speak every language: one sample per language, so a page in
# Spanish plays them speaking Spanish. Stored as <provider>/<lang>/<voice>.mp3
# under the manifest key "<voice>@<lang>".
MULTILINGUAL = {
    "openrouter": ("x-ai/grok-voice-tts-1.0", ("eve", "ara", "rex", "sal", "leo")),
    "venice": ("tts-xai-v1", ("eve", "ara", "rex", "leo", "sal")),
}
OTHER_LANGUAGES = ("es", "fr", "de", "it", "pt", "nl", "ja", "ko", "hi", "zh")


def sample_file(voice_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", voice_id) + ".mp3"


# Every sample plays at the same loudness: providers deliver anywhere from
# -40 to -14 LUFS, and one voice sounding twice as loud as the next reads as
# a better (or broken) voice rather than a different one.
TARGET_LUFS = -18.0
PEAK_LIMIT = 0.84  # -1.5 dBFS
MP3 = ["-ac", "1", "-ar", "24000", "-codec:a", "libmp3lame", "-b:a", "48k"]


def loudness(path: Path) -> float:
    """Integrated loudness in LUFS (EBU R128)."""
    out = subprocess.run(["ffmpeg", "-nostats", "-i", str(path), "-af", "ebur128", "-f", "null", "-"],
                         capture_output=True, text=True, check=True).stderr
    return float(re.findall(r"I:\s+(-?[\d.]+) LUFS", out)[-1])


def level_filter(path: Path) -> str:
    """Gain to the target loudness, with a limiter so the loud peaks don't clip."""
    return f"volume={TARGET_LUFS - loudness(path):.2f}dB,alimiter=limit={PEAK_LIMIT}:level=false"


def to_mp3(wav: bytes, target: Path) -> None:
    """Mono, 24 kHz, 48 kbps, silence trimmed at both ends, at the target
    loudness: a few seconds in about 20 KB."""
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "in.wav"
        trimmed = Path(tmp) / "trimmed.wav"
        source.write_bytes(wav)
        subprocess.run(
            ["ffmpeg", "-loglevel", "error", "-y", "-i", str(source),
             "-af", "silenceremove=start_periods=1:start_threshold=-45dB,areverse,"
                    "silenceremove=start_periods=1:start_threshold=-45dB,areverse,apad=pad_dur=0.15",
             str(trimmed)],
            check=True,
        )
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(trimmed),
                        "-af", level_filter(trimmed), *MP3, str(target)], check=True)


def normalize(target: Path) -> None:
    """Bring a sample already on disk to the target loudness."""
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / target.name
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(target),
                        "-af", level_filter(target), *MP3, str(out)], check=True)
        shutil.copyfile(out, target)


async def speak(provider: str, voice_id: str, model: str, text: str, env: dict[str, str], language: str = "en") -> bytes:
    from sleeptalk.builder import tts_spec
    from sleeptalk.config import TTSConfig
    from sleeptalk.provider_factory import build_tts

    section = TTSConfig(provider=provider, voice_id=voice_id, language=TTS_LANGUAGE.get(language, "en-US"),
                        **({"model": model} if model else {}))
    adapter = build_tts(tts_spec(section), env)
    try:
        return await adapter.synthesize_wav_bytes(text)
    finally:
        close = getattr(adapter, "aclose", None) or getattr(adapter, "close", None)
        if close is not None:
            result = close()
            if asyncio.iscoroutine(result):
                await result


def wanted(providers: tuple[str, ...]) -> list[dict]:
    from sleeptalk.voice_catalog import CURATED

    items = [{"provider": v.provider, "name": v.name, "voice_id": v.voice_id,
              "model": v.model,
              "language": "en"}
             for p in providers for v in CURATED.get(p, ())]
    items += [dict(extra) for extra in EXTRA if extra["provider"] in providers]
    # Measured native voices per language (voice_catalog._NATIVE).
    from sleeptalk.voice_catalog import _NATIVE, native

    seen = {item["voice_id"] for item in items}
    for provider, tag in _NATIVE:
        if provider not in providers:
            continue
        for voice in native(provider, tag):
            if voice.voice_id not in seen:
                seen.add(voice.voice_id)
                items.append({"provider": provider, "name": voice.name, "voice_id": voice.voice_id,
                              "model": "", "language": tag.split("-", 1)[0]})
    for provider, (model, voices) in MULTILINGUAL.items():
        if provider not in providers:
            continue
        for voice_id in voices:
            for language in OTHER_LANGUAGES:
                items.append({"provider": provider, "name": voice_id.title(), "voice_id": voice_id, "model": model,
                              "language": language, "key": f"{voice_id}@{language}"})
    return items


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env-file", help="A dotenv file holding the providers' API keys (not needed with --normalize).")
    parser.add_argument("--provider", action="append", choices=PROVIDERS, help="Only these providers (repeatable).")
    parser.add_argument("--force", action="store_true", help="Re-record samples that already exist.")
    parser.add_argument("--normalize", action="store_true",
                        help="Bring the samples already on disk to one loudness; records nothing.")
    args = parser.parse_args()
    if not shutil.which("ffmpeg"):
        print("ffmpeg is required to compress the samples.", file=sys.stderr)
        return 2
    if args.normalize:
        for path in sorted(OUT.rglob("*.mp3")):
            normalize(path)
            print(f"leveled {path.relative_to(OUT)}")
        return 0
    providers = tuple(args.provider or PROVIDERS)
    if not args.env_file and any(KEY_ENV[p] for p in providers):
        parser.error("--env-file is required to record cloud samples")
    from dotenv import dotenv_values

    from sleeptalk.speech.openai_audio import PRESETS, model_for_voice

    values = ({k: v for k, v in dotenv_values(Path(args.env_file).expanduser(), interpolate=False).items() if v}
              if args.env_file else {})
    manifest_path = OUT / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.is_file() else {}
    failures = 0
    for item in wanted(providers):
        provider, voice = item["provider"], item["voice_id"]
        env_name = KEY_ENV[provider]
        if env_name and env_name not in values:
            print(f"skip {provider}/{item['name']}: no {env_name} in the env file")
            continue
        key = item.get("key", voice)
        relative = f"{provider}/{item['language']}/{sample_file(voice)}" if "key" in item else f"{provider}/{sample_file(voice)}"
        target = OUT / relative
        if target.is_file() and not args.force and key in manifest.get(provider, {}):
            print(f"keep {relative}")
            continue
        text = LINES[item["language"]].format(name=item["name"])
        try:
            wav = await asyncio.wait_for(speak(provider, voice, item["model"], text, {env_name: values[env_name]} if env_name else {}, item["language"]), 60)
            to_mp3(wav, target)
        except Exception as exc:  # noqa: BLE001 — report and continue; never echo a key
            failures += 1
            print(f"FAILED {provider}/{item['name']}: {type(exc).__name__}")
            continue
        entry = {"file": relative, "name": item["name"], "language": item["language"], "line": text, "voice": voice}
        if item["model"]:
            entry["model"] = item["model"]
        elif provider in PRESETS:
            # Which of the provider's models spoke it, so the settings page
            # never offers a Grok sample for an Orpheus voice of the same name.
            entry["model"] = item["model"] or model_for_voice(PRESETS[provider], voice, "")
        manifest.setdefault(provider, {})[key] = entry
        print(f"wrote {relative} ({target.stat().st_size // 1024} KB)")
    OUT.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=1, ensure_ascii=False, sort_keys=True) + "\n")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
