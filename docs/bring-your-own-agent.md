# Bring your own agent

Sleeptalk ships adapters for Claude Code, Codex, Cursor, Hermes, OpenClaw, and
Pi. Other agents can talk too, but it isn't plug and play: unless your agent
already speaks ACP, you write a small adapter that follows Sleeptalk's
contract. Pick a route, generate a working starting point, and let
`sleeptalk adapter test` tell you whether it works.

Before you start, check that your agent can answer one message at a time and
return its reply within a few seconds. An agent that only runs on a schedule,
or replies somewhere else (a chat app, email, a CI job), can't hold a spoken
conversation, however the adapter is written.

Status: preview. Custom adapters are community-supported. Sleeptalk supports
the contract and the tools; it doesn't certify your agent.

## What bringing your own harness means

Connect the agent harness you already run through a built-in adapter, ACP,
or a small adapter to Sleeptalk's public protocol. You do not need to fork
Sleeptalk or use a Sleeptalk SDK. A harness is the program that runs your
agent, including its tools, model access, instructions, and conversation
state. A model API key alone is the separate [model-only route](model-only.md).

Sleeptalk handles listening, speech output, turn-taking, and the voice
presentation of interruptions and progress. Your harness continues to own
its tools, credentials, permissions, and memory. The adapter connects those
two sides; it must forward interruption requests to the work it starts.
Stopping speech does not undo a tool action that has already happened.

Compatibility means the connected harness can receive a text turn, return
text, finish or fail the turn, and handle interruption and shutdown. Stream
replies for responsive voice. Tool progress, permission requests,
clarification, steering, and reconnect are additional capabilities: only
expect them when the chosen transport and adapter implement them. In
particular, HTTP turns cannot receive mid-turn permission decisions or
steering; use a command or bridge connection when your harness needs those.

Connecting does not automatically attach to a conversation already open in
your agent's UI, import its history, or preserve memory after a process
restart. Those behaviors depend on the harness and adapter. A command stays
running between turns; an HTTP endpoint receives a stable `sessionId`, but
your service must use it to keep conversation state.

## What you need

- A supported macOS or Ubuntu host running Sleeptalk, with Python 3.11+.
  Keep that host running during calls, even when you talk from your phone.
- A working harness and its own model login or credentials. For a local
  harness, provide its machine-readable command and workspace. For a remote
  harness, provide an endpoint reachable from the Sleeptalk host and its
  authentication. An arbitrary interactive CLI or chat webpage is not an
  agent endpoint.
- A built-in adapter, a compatible ACP entry point, or an adapter you or
  the harness author supplies. ACP avoids writing translation code, but
  individual agents and versions still need checking.
- Speech configured separately: a speech-provider key or local speech, plus
  a browser, terminal, or Discord call surface. See the [getting-started
  guide](../README.md#get-started) and [support matrix](launch-support-matrix.md).

If your harness has no compatible interface yet, its author or an integration
developer needs to write the adapter. End users of an already
integrated harness only need to install and configure it. Python and Node
scaffolds are provided; other languages can implement the same wire
protocol. An SDK is optional convenience, not a prerequisite.

## Pick a route

| Your agent | Profile | You write |
| --- | --- | --- |
| Already speaks ACP (Gemini CLI, Goose, Kiro, Qwen Code, Cline, and others) | `type = "command"`, `protocol = "acp"` | Nothing |
| A program or script in any language, on this machine | `type = "command"` | A JSON-lines wrapper around your harness, including interruption handling |
| A web service you already run | `type = "http"` | One streaming route |
| A long-lived remote agent that needs permissions, steering, or reconnects | `type = "bridge"` | An Agent Bridge v1 server |
| A Python agent that needs the full in-process contract | Your own `type`, from an installed package | A class and a `register()` |

If you aren't sure, start with `command`.

## Start with a working adapter

```sh
sleeptalk adapter new my-agent --kind command            # or --language node
sleeptalk adapter test --config my-agent/sleeptalk.agent.toml --fixture
```

The scaffold is an echo agent with fixture behavior for the compatibility
checks. Open `my-agent/agent.py` and replace the marked part of `reply` with
your harness call. Preserve streaming, conversation state, and cancellation
as needed by your harness, then exercise real text turns:

```sh
sleeptalk adapter test --config my-agent/sleeptalk.agent.toml --live
```

Copy the `[agent]` section from `sleeptalk.agent.toml` into your Sleeptalk
profile and start a call as usual. `--live` checks the adapter with text
prompts; it does not use a microphone or prove a voice call. Follow the
[call checks below](#check-your-integration) before sharing the integration.

### Or use the setup wizard

`sleeptalk init` offers **My own agent** beside the built-in harnesses. It asks
what kind of agent you have (a program, an ACP agent, a web service, a remote
bridge, or "not built yet", which creates a starter agent under
`<home>/agents/<slug>/`). It checks the agent without sending a prompt, then
writes the profile. Tokens go to the private store; the profile only references them.

Headless setup takes the same answers:

```toml
# sleeptalk init --non-interactive --answers answers.toml
name = "Own Bot"
surface = "web"
agent_type = "http"
agent_token = "..."                 # kept in the private store; the profile references it under token_env
agent_options = { url = "https://agent.example.com/sleeptalk/turn", token_env = "OWN_BOT_AGENT_TOKEN" }
# for ACP agents: agent_type = "command", agent_permission_mode = "pending",
# agent_options = { protocol = "acp", command = ["gemini", "--experimental-acp"] }
```

Other kinds:

```sh
sleeptalk adapter new my-agent --kind http     [--language node]
sleeptalk adapter new my-agent --kind acp
sleeptalk adapter new my-agent --kind bridge   [--language node]
sleeptalk adapter new my-agent --kind python
```

## `command`: a program that reads and prints JSON lines

```toml
[agent]
type = "command"
workspace_dir = "."

[agent.options]
command = ["node", "agent.mjs"]
env = ["OPENAI_API_KEY"]   # names only; supply values through the host environment
```

Sleeptalk starts the program once and keeps it running between turns. It
writes one JSON object per line to stdin and reads one per line from stdout.
Print logs to stderr. Anything on stdout that isn't a JSON frame is ignored,
with one warning.

The minimum:

```text
Sleeptalk → agent   {"type":"turn.prompt","turnId":1,"text":"what's in my inbox?"}
agent → Sleeptalk   {"type":"assistant.delta","turnId":1,"text":"You have three "}
agent → Sleeptalk   {"type":"assistant.delta","turnId":1,"text":"new messages."}
agent → Sleeptalk   {"type":"turn.completed","turnId":1}

Sleeptalk → agent   {"type":"turn.abort","turnId":2}        the caller interrupted
agent → Sleeptalk   {"type":"turn.aborted","turnId":2}
```

Send text as it arrives, in any size. Sleeptalk splits sentences, removes
markdown and code, and starts speaking at the first clause. `turnId` is
optional in replies; include it if your agent can overlap turns.

If the agent reports a failure, send
`{"type":"error","message":"model quota exceeded"}` and the turn ends with
that message.

### Report what the agent is doing

Everything below is optional. Advertise what you send by answering the
`hello` Sleeptalk writes at startup:

```text
Sleeptalk → agent   {"type":"hello","v":1,"profile":"lite","capabilities":{...}}
agent → Sleeptalk   {"type":"hello.ok","capabilities":{"tool_lifecycle":"supported","usage":"supported"}}
```

| Frame | Capability | What Sleeptalk does with it |
| --- | --- | --- |
| `{"type":"thinking.started"}` | none | Plays a short filler while the agent thinks |
| `{"type":"tool","name":"read","phase":"started","detail":"reading the readme","callId":"t1"}` | `tool_lifecycle` | Narrates tool work; `phase` is `started`, `updated`, `completed`, or `failed` |
| `{"type":"status.changed","state":"blocked"}` | `status` | Tracks working, blocked, and idle states |
| `{"type":"usage.reported","inputTokens":120,"outputTokens":40}` | `usage` | Records usage |
| `{"type":"permission.requested","requestId":"p1","prompt":"Delete dist?","options":["deny","allow_once"]}` | `permissions` | Asks the caller, then sends `permission.resolve` with `decision` |
| `{"type":"clarification.requested","requestId":"c1","prompt":"Which file?"}` | `clarification` | Asks the caller, then sends `clarification.answer` |
| steering and cancel | `steering`, `targeted_cancellation` | Sends `steer` or `cancel` frames |

Reply to `permission.resolve`, `clarification.answer`, `steer`, and `cancel`
with `{"type":"control.accepted","ref":"<frame id>"}` or
`{"type":"control.rejected","ref":"<frame id>","reason":"..."}`. Without a
reply within two seconds, Sleeptalk treats the frame as delivered.

A permission request is never approval. Silence, a timeout, or a closed
connection means the agent keeps waiting or denies.

An agent that doesn't answer `hello` still works; it gets the talkable floor.
A permission or clarification request from an agent that didn't advertise it
is ignored, because nothing would answer it.

### Environment

Command agents receive a small base environment (`PATH`, `HOME`, locale,
proxy and certificate settings) plus the variable names listed in `env`.
Speech-provider and Discord credentials never pass through. Set
`inherit_env = true` to pass everything else, the same way built-in adapters
do.

### Other options

| Option | Default | Meaning |
| --- | --- | --- |
| `protocol` | `"lite"` | `"acp"` to speak ACP instead |
| `hello_timeout_s` | `1.0` | How long startup waits for `hello.ok` |
| `idle_timeout_s` | `120` | A turn fails after this long with no frame |
| `first_frame_timeout_s` | `30` | Until the program has sent any frame at all, a turn fails after this long; it's probably not speaking the protocol |
| `abort_grace_s` | `2.0` | After an abort, Sleeptalk waits this long for `turn.aborted`, then restarts the program |

## `command` with ACP: agents that already speak it

```toml
[agent]
type = "command"
workspace_dir = "."
permission_mode = "pending"

[agent.options]
protocol = "acp"
command = ["gemini", "--experimental-acp"]
env = ["GEMINI_API_KEY"]
```

Check your agent's documentation for its ACP command. Sleeptalk uses the same
ACP client as its Cursor integration.

`permission_mode` is `pending` by default: tool approvals go to the caller.
`deny` refuses them. `auto` approves everything, and setup probes refuse it.

The Hermes adapter keeps its own `type = "acp"` (alias `hermes`). That type
means Hermes; use `type = "command"` for any other ACP agent.

### Hermes set up by an agent

An agent that sets Hermes up for you through `sleeptalk host …` uses the
`hermes` on your PATH, whatever version it is, over ACP. Hermes keeps its own
model login and configuration; Sleeptalk holds no model credential for it.
Permissions must be `pending` or `deny`, so tool approvals stay with the
caller. Nothing to clone, nothing to pin, and `hermes update` is your
business.

```bash
sleeptalk host ensure --profile default --agent hermes --surface web --json
```

If Hermes already runs on OpenRouter, Venice, Groq, or Together, `host
ensure` finds that key in `~/.hermes/.env` or `~/.hermes/config.yaml` and
the receipt's `ask` line offers it. If the user says yes, the agent reruns
with `--reuse-key <service>`, which proves the key can listen and speak and
comes back ready; nothing is pasted anywhere. Otherwise the agent sends a
link:

```bash
sleeptalk host input link --request-id setup-001 --profile default \
  --workspace /path/to/workspace --agent hermes --permission-mode pending
```

The link's page offers every speech provider, with its free usage and whether
a card is needed; the user picks one there, and that one key both listens and
speaks. `--speech-provider` only puts a provider first.

Once the browser call works, the agent can offer a Discord bot as well; the
same link flow collects the bot token and walks the user through the rest.
See [Discord](discord.md#set-up-by-your-agent).

#### The pinned isolated runtime (optional)

For a hardened session, Sleeptalk can instead launch an isolated Hermes from
a pinned, hash-checked source checkout in a fresh home, with only the
workspace, model, and credential you selected, so nothing on the host can
swap the model or credential mid-call. It is selected explicitly with
`options.hermes_root`; without it, the installed Hermes runs. The pinned
source recommendation is Hermes 0.21.7, upstream tag `v0.21.7`. The previously
reviewed 0.21.3 source is still accepted. Your installed Hermes is
left untouched.

Prepare the checkout once per host, with Python 3.14:

```bash
git clone --depth 1 --branch v0.21.7 https://github.com/NousResearch/hermes-agent.git /opt/hermes-0.21.7
python3.14 -m venv /opt/hermes-0.21.7/venv
/opt/hermes-0.21.7/venv/bin/pip install -e '/opt/hermes-0.21.7[anthropic]'
```

Hermes 0.21.7 needs its own Python 3.14 environment: its upstream metadata
omits core dependencies on older Python versions. This does not change the
Python version required by Sleeptalk or update your installed Hermes.

The isolated Hermes needs an OpenAI or Anthropic API key of its own. Put it
in the state directory's `.env` yourself, over SSH, as `ANTHROPIC_API_KEY` or
`OPENAI_API_KEY`; or, on a host where you have a browser, let the agent run
`sleeptalk host auth start --user-browser-local` and enter it privately. It
never goes through chat. Then the agent passes the checkout to setup:

```bash
sleeptalk host input link --request-id setup-001 --profile default \
  --workspace /path/to/workspace --agent hermes --permission-mode pending \
  --pi-bin /opt/hermes-0.21.7/venv/bin/python \
  --agent-option hermes_root=/opt/hermes-0.21.7 \
  --provider anthropic --model <model>
```

`hermes-gateway`, which attaches to a running Hermes dashboard, is for a
person at the keyboard (`sleeptalk connect hermes`); agents use `hermes`.

## `http`: a web service

```toml
[agent]
type = "http"

[agent.options]
url = "https://agent.example.com/sleeptalk/turn"
token_env = "MY_AGENT_TOKEN"   # sent as Authorization: Bearer <value>
```

Each turn is a `POST` with the `turn.prompt` frame plus a stable `sessionId`
for your conversation memory. Stream the reply in any of these formats:

| Response `Content-Type` | Body |
| --- | --- |
| `application/x-ndjson` | One frame per line, as for `command` |
| `text/event-stream` | `data:` lines holding frames or plain text; `data: [DONE]` ends the turn |
| `application/json` | One frame, a list of frames, or `{"text":"..."}` |
| `text/plain` (or anything else) | Streamed plain text |

The response ending also ends the turn. When the caller interrupts, Sleeptalk
closes the request; stop work when your framework reports the disconnect.

Optional: answer `GET` on the same URL with
`{"type":"hello.ok","capabilities":{...}}` so Sleeptalk knows your
capabilities before the first turn. `HEAD` is used as a readiness probe.

HTTP turns can't receive a message mid-turn, so permissions, clarification,
steering, and cancellation are reported as unavailable. Use `command` or
`bridge` for those.

Plain `http://` is accepted only on loopback. Put secrets in `token_env`,
never in `headers` or the URL.

## `bridge`: a remote agent with full session features

```toml
[agent]
type = "bridge"

[agent.options]
url = "wss://agents.example.com/v1/agent-bridge"
token_env = "MY_AGENT_BRIDGE_TOKEN"
```

The remote side implements [Agent Bridge v1](agent-bridge-protocol.md):
authenticated WebSocket or TLS, heartbeats, credit, replay, and reconnect.
`--kind bridge` generates a Python WebSocket server built on Sleeptalk's
reference implementation. `--kind bridge --language node` generates a
dependency-free Node server over the protocol's JSON-lines TCP transport:
`tcp://127.0.0.1:port` on the same machine, or `tls://host:port` with
`TLS_CERT_FILE` and `TLS_KEY_FILE` set. It doesn't support session resume; a
dropped connection starts a new session. Other languages implement the frames
in the protocol document.

Permission decisions, clarification answers, steering, and cancellation wait
up to five seconds for the agent's `control.accepted` or `control.rejected`.
No answer is reported as a retryable rejection.

Voice sessions started by the agent aren't available over any type yet. They
need the host-owned request flow in
agent-initiated sessions, where Sleeptalk mints
the call and the agent only receives a public link.

## `python`: an installed plugin

```sh
sleeptalk adapter new my-agent --kind python
cd my-agent && uv pip install -e .
```

The package registers its type through the `sleeptalk.agents` entry point and
implements the [adapter contract](agent-adapter-contract.md) in process. See
[component contracts](component-contracts.md) for private option schemas and
discovery. The generated `tests/test_conformance.py` runs the same checks as
`adapter test`.

## `sleeptalk adapter test`

```sh
sleeptalk adapter test --config PROFILE             # no prompt is sent
sleeptalk adapter test --config PROFILE --fixture   # the scaffold's test prompts
sleeptalk adapter test --config PROFILE --live      # a few short real prompts; may spend tokens
sleeptalk adapter test --config PROFILE --json      # one JSON report for agents and CI
```

The report has three columns.

| Column | Question | A failure means |
| --- | --- | --- |
| protocol | Does the agent keep the contract it advertised? | Fix the agent |
| availability | Could Sleeptalk start and reach it? | Fix installation, command, URL, or credentials |
| voice | Will it feel good on a call? | Warnings only |

Voice warnings cover time to the first sentence (target under 1.5 seconds),
markdown reaching speech, very long or fragmented sentences, filler phrases
the host already provides, and silent gaps over eight seconds without a tool
or status frame.

`--fixture` sends the prompts `hello`, `slow`, `hang`, `tool`, `permission`,
and `clarify`. Scaffolds handle them so you can see every capability pass
before you change anything. `--live` never exercises permissions,
clarification, or steering, because those would act on your workspace.

The exit status is `0` when nothing failed, `1` when a check failed, and `2`
when the profile couldn't be loaded.

`sleeptalk host probe-runtime` uses the same types during setup. Probes start
the program, send `hello`, `initialize`, or a bridge handshake, or send `GET`
and `HEAD` requests. They never send a prompt.

## Troubleshooting

**It only says "Still working on it" and never answers.** The program isn't
sending frames Sleeptalk understands. The most common cause is an existing
agent's interactive app run as a plain program, for example
`command = ["opencode"]`. Use its ACP entry point instead:

```toml
[agent]
type = "command"
permission_mode = "pending"

[agent.options]
protocol = "acp"
command = ["opencode", "acp"]
inherit_env = true
```

`sleeptalk adapter test` warns about this for agents it recognizes, and a
program that never sends a frame now fails after `first_frame_timeout_s`
with the same advice.

**The agent can't find its API key.** Command agents only receive the
variables listed in `env`. Add the name there, or set `inherit_env = true`
for CLIs that read their own login and settings.

**`adapter test` passes but a call doesn't.** A plain `adapter test` never
sends a prompt. Run it with `--live`, then check a real call. Even a passing
`--live` report does not check the microphone, speech providers, or call
surface.

## Check your integration

1. Run `sleeptalk adapter test --config PROFILE` to check startup and the
   contract without sending a prompt. Use `--fixture` for a scaffold or adapter that implements
   the documented fixture prompts; passing the unchanged echo scaffold does
   not validate your harness.
2. Run `sleeptalk adapter test --config PROFILE --live --json` against the real harness.
   Read failures, skips, warnings, and advertised capabilities. It may spend
   model tokens and ask the agent to inspect the current directory with a
   tool; use an appropriate test workspace.
3. Make a real call on the surface you intend to use. Check a first reply,
   a follow-up that needs conversation context, interruption during a long
   reply, and a new turn after interruption. Confirm that interrupted work
   stops in the harness as well as in the audio.
4. If you advertise tool progress or permissions, exercise those with a
   harmless action. Check that a denial is respected and an unanswered
   permission request never becomes approval. The `--live` checker does not
   exercise permission, clarification, or steering controls.
5. Record the Sleeptalk and harness versions, connection type, call surface,
   and supported capabilities when sharing the adapter. Describe whether it
   starts a new session, attaches to an existing one, or resumes after a
   disconnect. Report automated and human call checks separately.

These checks establish evidence for your integration; they do not change
its preview status. Live microphone, Discord speech, and service restart
checks require a person.

## Make it good on voice

- Stream text as soon as the model produces it. Don't wait for the full reply.
- Report tool work. Silence during a long tool call sounds like a hang.
- Stop quickly on `turn.abort`. Barge-in feels broken when speech keeps going.
- Don't write your own "one moment" filler, keepalive, or progress narration.
  Sleeptalk owns those.
- Plain spoken language beats markdown. Sleeptalk strips it, but lists and
  tables still read awkwardly.

## Reference

- [Agent Bridge v1 and the Lite profile](agent-bridge-protocol.md)
- [Agent adapter contract](agent-adapter-contract.md)
- [Conformance suite](agent-conformance.md)
- [Component contracts](component-contracts.md)
- Built-in adapter internals: [adding a built-in adapter](adding-an-agent.md),
  [in-process Python adapter walkthrough](custom-agent.md)
