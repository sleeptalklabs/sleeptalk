# Synthetic calls

Real web calls with a scripted caller. Each run starts a local web call on a throwaway Sleeptalk home and opens it in headless Chromium. The browser's microphone is replaced with pre-recorded caller lines, spoken by Grok "rex". The run records what the agent says back: its audio level every 20 ms, a recording, and the transcripts the host sends over the call. Then it scores eight scenarios.

| Scenario | Checks |
|---|---|
| `first_words` | The first word of the call is heard |
| `short` | A two-sentence question is heard whole and answered |
| `long_silent` | A long answer plays to the end while the caller is silent (no cut-off) |
| `barge` | Talking over an answer stops it quickly, and the new question is answered |
| `resume` | "Go on" continues where the answer stopped |
| `split` | A 700 ms pause mid-sentence stays one turn |
| `backchannel` | "Mm-hmm" / "Ajá" / "嗯嗯" during an answer doesn't end it |
| `search` | A turn that needs a tool (web search) completes |

`latency.py` breaks each turn down:
- `endpoint`: the caller stops, then the turn ends.
- `stt`: the transcript is ready.
- `agent`: the agent's first text arrives.
- `tts`: the first answer audio arrives.
- `ack` and `answer`: the first audio of each kind is queued.
- `heard`: the caller stops, then first hears the agent.

## Run

The runs use real providers and real agents, and spend a little provider credit each. They are a manual check, not part of CI.

```bash
cd remote-entry-web && npm ci && cd ..      # Playwright, once
export SYNTH_OPENROUTER_PROFILE=~/.sleeptalk/profiles/default.toml   # a profile with a saved OpenRouter key
export SYNTH_KEYS_ENV=~/somewhere/.env      # optional: DEEPGRAM_API_KEY, CARTESIA_API_KEY, OPENAI_API_KEY
sh scripts/synthetic_call/run.sh omp /tmp/synth/omp            # one call
sh scripts/synthetic_call/matrix.sh                             # every agent, every service, three languages
```

`start_call.py` options:
- `--agent`: `omp`, `pi`, `hermes`, `claude_code`, `codex`, `cursor`, `openclaw`, or `model`.
- `--speech`: `openrouter`, `deepgram`, `cartesia`, or `realtime`.
- `--language`: `en-US`, `es-ES`, or `zh-CN`. Caller lines exist for these three.
- `--voice`: a voice ID.
- `--conversation-id`: a conversation to continue.
- `--openclaw-read`: OpenClaw with its gateway token.

- `--fault-proxy URL`: send speech through `fault_proxy.py` (below).
- `--agent fault --fault-control PATH`: a command agent (`fault_agent.py`) that misbehaves on request.

`SYNTH_ONLY=barge,split` runs only those scenarios. `SYNTH_LOG_LEVEL=DEBUG` turns up the host's log.

## Tool runs, permissions, subagents, long calls

- `--seed` puts a small project in the agent's workspace (a README naming it "Lantern", three Python files with a TODO each), so answers can be checked.
- `--permission-mode pending` makes the agent ask before using a tool.
- `SYNTH_ONLY=script` with `SYNTH_SCRIPT` (JSON steps: `name`, `clip`, then `after` ms to talk over work in progress, or `done`/`wait`/`quiet` to wait for the reply) runs a scripted conversation. `analyze_tools.py` writes `tools.txt`: what each step showed, and its check (the answer, or the file the agent wrote or didn't).
- `SYNTH_ONLY=soak` with `SYNTH_SOAK_MINUTES` runs ordinary turns in rotation for that long, to watch latency and memory over a long call.

```bash
SYNTH_ONLY=script SYNTH_SCRIPT='[{"name":"tool_write","clip":"tool_write"},{"name":"perm_yes_task","clip":"perm_yes_task","after":25000},{"name":"perm_yes","clip":"perm_yes"}]' \
sh scripts/synthetic_call/run.sh claude_code /tmp/synth/perm --seed --permission-mode pending
```

## Discord without Discord

`discord_session.py` runs a Discord voice call in-process: the real live session, receive sink, endpointing, barge-in and cascade route, with the caller's clips fed in as 20 ms frames at real-time pace and the bot's audio played into a fake voice client that records what would have been sent. Only Discord's decrypt and Opus decode, the voice client's player, and the user are faked. Speech is OpenRouter; the agent is `fault_agent.py`, which answers the scenario questions from canned replies.

```bash
SYNTH_CLIPS=/tmp/synth/clips/en-US uv run python scripts/synthetic_call/discord_session.py /tmp/synth/discord
uv run python scripts/synthetic_call/fault_proxy.py 8799 /tmp/synth/ctl.json &   # for tts_* scenarios
SYNTH_CLIPS=... uv run python scripts/synthetic_call/discord_session.py /tmp/synth/discord-tts \
  --scenarios short,tts_500,tts_stall --fault-proxy http://127.0.0.1:8799
```

Scenarios: `first_words`, `short`, `long_silent`, `barge`, `resume`, `split`, `backchannel`, `agent_error`, `agent_crash`, `agent_empty`, and `tts_<fault>` with the proxy. `events.json` holds the timeline; the run's `home/logs` holds the host's latency traces.

## Failure injection

The `faults` scenario sets a fault, asks a short question, and checks the caller hears something (or the page shows a line) instead of silence. `fault_proxy.py` sits in front of OpenRouter speech and fails on request (`401`, `429`, `500`, `stall`, `cut`, `garble`). `fault_agent.py` answers, reports an error, crashes, returns nothing, prints garbage, is slow, or hangs.

```bash
uv run python scripts/synthetic_call/fault_proxy.py 8791 /tmp/synth/ctl.json &
SYNTH_ONLY=faults SYNTH_FAULT_PROXY=http://127.0.0.1:8791 \
SYNTH_FAULT_PLAN='[{"name":"stt_429","set":{"stt":"429"}},{"name":"agent_crash","set":{"agent":"crash"}}]' \
sh scripts/synthetic_call/run.sh fault /tmp/synth/faults --fault-proxy http://127.0.0.1:8791 --fault-control /tmp/synth/ctl.json
```

Each step can set `wait` and `done` in ms for slow faults. `faults.txt` holds the verdicts.

Set `SYNTH_CHROMIUM` to use another Chromium when Playwright's own download is missing. Output goes to `$SYNTH_OUT`, by default `$TMPDIR/sleeptalk-synthetic-call`. Each run's `report.txt` holds the verdicts.

## What it touches

- **Speech key:** the OpenRouter key is used by reference and never copied. Other keys go into the run's process environment only.
- **"Just a model":** the run stores the OpenRouter key once as a model key, as setup does, and `run.sh` removes that entry afterwards.
- **Agents:** each agent works in an empty folder inside the run directory.
- **OpenClaw:** it starts a fresh conversation, not the main one.
- **Not covered:** live microphones, Discord's own network and encryption (DAVE), real Discord clients, and the phone page through the relay. Those stay human checks.
