# Voice from a key you already have

OpenRouter, Groq, Together, and Venice expose the same two audio routes an
OpenAI key does: one that turns speech into text and one that turns text
into speech. If your agent already runs on one of them, that key can give
you voice with nothing new to sign up for. Sleeptalk treats each of them as
a speech provider: `provider = "openrouter"` in the profile's `[stt]` and
`[tts]` sections, one key for both.

## What you get

| Service | Listens with | Speaks with | What it costs |
| --- | --- | --- | --- |
| OpenRouter | Parakeet TDT 0.6B | Grok Voice (Eve); Deepgram Flux voices free | Listening nine cents an hour; Grok about a dollar an hour of speech |
| Groq | Whisper large v3 turbo | Orpheus | A few cents an hour |
| Together | Parakeet TDT 0.6B | Cartesia Sonic (Skylar) | Listening for cents an hour; speech a few dollars an hour |
| Venice | Parakeet TDT 0.6B | Grok Voice (Eve) | From your prepaid credits, about a dollar an hour of speech |

Any other model the service lists works too: set `model` under `[stt]` or
`[tts]` to its slug. On OpenRouter the voice name picks the model, so
choosing Harper switches to Microsoft's MAI-Voice-2 Flash, Eve to Grok
Voice, a Kokoro name such as `af_sky` to Kokoro, and an ElevenLabs voice
such as `george` or `jessica` to ElevenLabs Turbo v2.5, from the call page's
Voice menu or the profile. OpenRouter also carries ElevenLabs Scribe v2 for
listening (`elevenlabs/scribe-v2`).

## The one trade-off

Listening through these services is one request per turn: Sleeptalk decides
you have finished speaking, uploads the clip, and waits for the text. A
dedicated speech account (Deepgram, Cartesia) streams words as you say them
and decides the end of your turn itself, so replies start sooner there.
Sleeptalk compensates where it can: with a batch listener the default
endpointing uses the tighter silence windows (the "aggressive" values), each
adapter keeps its connection open across turns, and the defaults are the
models measured fastest, so a warm turn listens in about a third of a second
and starts speaking in about a third of a second. Accuracy is the same or
better: the same models are on the menu. Set `endpointing = "relaxed"` under
`[policy]` if you prefer the longer pauses.

## Zero setup, through your agent

When `sleeptalk host ensure` finds that a profile still needs speech keys,
it looks for a key that could do the job:

- Sleeptalk's own credential store, when a "just a model" profile already
  uses one of these services;
- the host's environment;
- the agent's own files: `~/.hermes/.env` and `~/.hermes/config.yaml` for
  Hermes, `~/.openclaw/.env` and `~/.openclaw/openclaw.json` for OpenClaw,
  `~/.pi/agent/auth.json` and `models.json` for pi (and `~/.omp/agent` for
  oh-my-pi). A custom provider that points at one of these services counts,
  with its key inline, under its own variable name, or as a secret
  reference. A key given as a shell command is never run.

A project's `.env` is deliberately not a source: a cloned repository can
ship one, and a key planted there would be offered to you as yours. Dotenv
files are read literally, so `${OTHER_SECRET}` is never expanded.

If it finds one, it only offers it. Nothing is read, sent, or copied yet.
The receipt stays `needs_input` and carries `reusable_keys` (each service
and where its key is, never the value) and an `ask` line for the agent to
put to you: "I found your OpenRouter key (~/.hermes/.env) on this computer.
Want me to use it for voice?" If you say yes, the agent reruns with that
service:

```bash
sleeptalk host ensure --profile default --agent hermes --surface web --json
# the user says yes to the OpenRouter key
sleeptalk host ensure --profile default --agent hermes --surface web --reuse-key openrouter --json
sleeptalk host start-voice --profile default --json
```

That run reads the key where it lives, proves it on both audio routes (a
small paid request to that service), copies it into the private credential
store, and writes the profile with a reference to it. It comes back ready
with `reused_key` and a `say` line: which service, which model listens,
which voice speaks, and what it costs. The original stays where it was, so
if you rotate it there, replace it in Sleeptalk too.

If you say no, the agent sends you the key page instead, and you can say
"use a different service" at any time later. `--no-key-reuse` skips the
lookup entirely. If the chosen key fails the check, the receipt says so
with a fixed code and the agent falls back to the private key page. The
value never passes through the agent, a receipt, or a file it can read.

## On the settings page and in the wizard

The settings page (and an agent's key request, which opens in it) offers
OpenRouter, Venice, Groq, and Together as cards with the same facts as the
other services, so someone without an agent-held key can paste one there. The setup wizard you run in
a terminal looks for the same keys and offers each one first: "use your key
from ~/.hermes/.env; nothing to paste, a copy goes into Sleeptalk's store".
The settings page looks only after you click **Look on this computer**.

## Checking a key yourself

The same two checks setup runs, against any of the four services, with the
key taken from the environment and never printed:

```bash
VENICE_API_KEY=... uv run python scripts/check_speech_key.py venice
```

It prints the transcript of the bundled clip and the time to the first
spoken audio.

## In a profile

```toml
[stt]
provider = "openrouter"
model = "openai/whisper-large-v3-turbo"

[tts]
provider = "openrouter"
voice_id = "flux-hannah-en"

[credentials.refs]
OPENROUTER_API_KEY = "credential:…"
```

`output_format` under `[tts]` defaults to `pcm` on OpenRouter and Venice
and `wav` on Groq and Together; `pcm_rate` under `[tts.options]` overrides
the assumed 24 kHz if a model returns something else.
