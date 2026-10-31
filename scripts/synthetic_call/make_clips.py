"""The synthetic caller's lines, spoken by Grok "rex" through Sleeptalk's own
OpenRouter speech code and saved as 48 kHz mono WAVs in <out_dir>.

    uv run python scripts/synthetic_call/make_clips.py <out_dir> [en-US|es-ES|zh-CN]

Uses the saved OpenRouter key named by SYNTH_OPENROUTER_PROFILE (read
in-process by the credential store, never printed). A few cents of speech.
"""
from __future__ import annotations

import asyncio
import json
import sys
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from qa_env import openrouter_ref  # noqa: E402

LINES = {
    "en-US": {
        "short": "What's two plus two? Just the number, please.",
        "long": "Tell me how a bicycle works, in about eight full sentences. Don't search the web, just explain it.",
        "barge": "Wait, stop. What's the capital of France?",
        "backchannel": "Mm-hmm.",
        "split_a": "I'd like to know about",
        "split_b": "the history of tea, in two sentences.",
        "first_words": "Banana bread is my favorite. Repeat back the first word I just said.",
        "resume": "Okay, go on with the bicycle, where you left off.",
        "search": "Search the web for today's weather in Paris and tell me in one sentence.",
        # Tool work in the seeded workspace (start_call.py --seed).
        "tool_read": "Read the README in this folder and tell me what the project is called.",
        "tool_write": "Create a file named notes.txt in this folder, containing just the word hello.",
        "tool_shell": "How many Python files are in this folder? Count them and tell me the number.",
        "tool_long": "For each TODO comment in this folder's Python files, search the web for how other projects handle it, then give me a short recommendation for each one.",
        "tool_status": "What are you doing right now?",
        "tool_stop": "Stop. Cancel that.",
        "perm_yes_task": "Create a file named approved.txt containing the word yes.",
        "perm_yes": "Yes, go ahead.",
        "perm_no_task": "Create a file named denied.txt containing the word no.",
        "perm_no": "No, don't do that.",
        "sub_parallel": "Use two subagents in parallel. One counts the Python files in this folder, the other reads the README and finds the project name. Then tell me both answers.",
        "sub_status": "How are the subagents doing?",
        "sub_many": "Use three subagents, one for each Python file in this folder, to review that file for bugs.",
        "sub_cancel": "Cancel that. Stop all of them.",
        "sub_fail": "Use two subagents. One reads the file called missing.txt, the other reads the README. Tell me what each one found.",
        # Background jobs: hand the work off, keep talking, hear the result.
        "job_todo": "Make a todo app and give me a link to it. Keep it local, don't deploy it anywhere.",
        "job_chat": "While that's going, what's the capital of Japan?",
        "job_status": "How's the todo app going?",
        "job_where": "Where can I find it?",
    },
    "es-ES": {
        "short": "¿Cuánto es dos más dos? Solo el número, por favor.",
        "long": "Explícame cómo funciona una bicicleta, en unas ocho frases completas. No busques en internet, solo explícalo.",
        "barge": "Espera, para. ¿Cuál es la capital de Francia?",
        "backchannel": "Ajá.",
        "split_a": "Me gustaría saber sobre",
        "split_b": "la historia del té, en dos frases.",
        "first_words": "Plátano es mi fruta favorita. Repite la primera palabra que acabo de decir.",
        "resume": "Vale, sigue con la bicicleta, donde lo dejaste.",
        "search": "Busca en internet el tiempo de hoy en París y dímelo en una frase.",
    },
    "zh-CN": {
        "short": "二加二等于几？只说数字就好。",
        "long": "用大约八句完整的话告诉我自行车是怎么工作的。不要上网搜索，直接解释。",
        "barge": "等一下，停。法国的首都是哪里？",
        "backchannel": "嗯嗯。",
        "split_a": "我想了解一下",
        "split_b": "茶的历史，用两句话说。",
        "first_words": "香蕉是我最喜欢的水果。重复我刚才说的第一个词。",
        "resume": "好，继续讲自行车，从你停下的地方接着说。",
        "search": "上网搜一下巴黎今天的天气，用一句话告诉我。",
    },
}
# What a correct reply contains, per language (checked by analyze.py).
EXPECT = {
    "en-US": {"first_word": "banana", "barge_answer": "paris"},
    "es-ES": {"first_word": "plátano|platano", "barge_answer": "parís|paris"},
    "zh-CN": {"first_word": "香蕉", "barge_answer": "巴黎"},
}


async def main(out: Path, language: str) -> None:
    from sleeptalk.builder import tts_spec
    from sleeptalk.config import TTSConfig
    from sleeptalk.credential_store import CredentialStore
    from sleeptalk.speech.openai_audio import OpenAIAudioTTS

    ref = openrouter_ref()
    spec = tts_spec(TTSConfig(provider="openrouter", model="x-ai/grok-voice-tts-1.0", voice_id="rex", language=language))
    tts = OpenAIAudioTTS(spec, api_key=CredentialStore().get(ref))
    out.mkdir(parents=True, exist_ok=True)
    manifest = {}
    try:
        for name, text in LINES[language].items():
            pcm = await tts.synthesize_pcm_bytes(text, purpose="synthetic-qa")
            path = out / f"{name}.wav"
            with wave.open(str(path), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(48_000)
                wav.writeframes(pcm)
            manifest[name] = {"text": text, "ms": round(len(pcm) / 96)}
            print(name, manifest[name]["ms"], "ms")
    finally:
        await tts.close()
    (out / "manifest.json").write_text(json.dumps({**manifest, "_language": language, "_expect": EXPECT[language]}, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1]), sys.argv[2] if len(sys.argv) > 2 else "en-US"))
