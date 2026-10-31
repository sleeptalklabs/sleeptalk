# Voice speech patterns

Cascade local-intent policy and the shared TTS pronunciation table. Neither is a SKILL.md rule. Neither applies to OpenAI realtime.

## How to add a presence phrase

Cascade classifies a short list of connection-check utterances after final STT text is known, and before it starts `agent.stream`. A match skips the agent and speaks the table reply through the existing TTS path.

The table lives in `sleeptalk/voice/speech_patterns.py`.

## Add a row

1. Open `_IDLE_REPLIES` in `speech_patterns.py`.
2. Add one normalized key and one idle reply. The key is lowercase, with punctuation stripped and whitespace collapsed. Example: `you still there` maps to `Yep, I'm here.`
3. Do not add a second classifier. `busy=True` already uses one reply for every matched phrase: `Yep, I'm here, still working on it`.
4. Add the idle case, a wrapped case if a greeting can precede the new phrase, and a near-miss that must stay `None` in `tests/runtime/test_voice_speech_patterns.py`.

A wrapper is one leading `hello`, `hey`, `hi`, `um`, `uh`, or `so` when the remainder is already a table key. `Hello?` matches as itself. `Hello? Can you explain this function?` does not, because the remainder is not a presence phrase.

Match the whole utterance after normalize, plus at most one wrapper. Do not substring-search.

## How to add a halt phrase

Halt is a second exact-family classifier in the same module. A match still skips the agent. The reply is always `Stopped.` Idle and busy use that same line.

Bare control words follow the Discord list (`stop`, `cancel`, `never mind`, `that's enough`). Spoken slash-command chrome is filler (`slash`, `pipeline`, `control`, `pi`, `bot`). `Pipeline control slash Stop.` matches. `stop the deployment` does not.

Add the live STT line, a bare `stop`, and a near-miss that must stay `None` in `tests/runtime/test_voice_speech_patterns.py`.

## How to add a pronunciation entry

`apply_pronunciation` in `sleeptalk/speech/lexicon.py` is the only rewrite. Every local TTS adapter already calls it. Do not add a provider-specific table.

Pick the stage, then add one term and one spoken form.

1. Exact compounds such as `Node.js` or `AGENTS.md` go in `project_specific` in `tts_lexicon.json`.
2. Hostnames are detected in code. Do not list `example.com` by hand.
3. File suffixes go in `extensions` (`.py` → `dot py`).
4. Commands and standalone acronyms go in `commands` or the existing standalone sections.

Add a row to the table in `tests/runtime/test_tts_lexicon.py`. Include the second-pass case when the spoken form still contains a word that a later stage might rewrite (`api` inside `api dot example dot com`).
