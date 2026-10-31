# Adding a built-in agent to sleeptalk

This page is for maintainers adding a first-party adapter inside the package.
To connect your own agent without changing Sleeptalk, use
[Bring your own agent](bring-your-own-agent.md): `type = "command"` needs no
Python, and `GenericStdioAgent` below is an internal helper.

New stdio agent = **a command, a kind-vocab table, one registration.** No
streaming or lifecycle code. The executable proof is the echo fixture
(`tests/runtime/test_echo_agent.py`) and the runnable demo in
`sleeptalk/examples/`.

## The three ingredients

### 1. The command

Anything that speaks NDJSON over stdio: one JSON object per line on stdout,
prompts as JSON on stdin. Package it as a `StdioAgentSpec`:

```python
from sleeptalk.streaming import StdioAgentSpec, TurnEvent

SPEC = StdioAgentSpec(
    args=("my-agent", "--stdio"),
    make_prompt=lambda text: {"type": "prompt", "message": text},
    translate=my_translate,          # stdout line -> TurnEvent
    vocab=MY_VOCAB,
    install_hint="install my-agent: npm i -g my-agent",
)
```

`translate` maps one parsed stdout line to a `TurnEvent`:

- `TurnEvent(kind="thinking")` — reasoning started (fires the filler pool once per turn)
- `TurnEvent(kind="text", text=delta)` — streamed reply text; the shared
  `SentenceStreamer` handles code fences, sentence splitting, and the
  first-clause fast-flush
- `TurnEvent(kind="tool", tool_name=..., tool_args={...})` — a tool call, narrated aloud
- `TurnEvent(terminal=True)` — turn complete
- `TurnEvent(error="...")` — abort the turn with this message

### 2. The kind-vocab table

How tool calls are narrated: which narration kind each tool name maps to,
and which arg field holds the spoken subject.

```python
from sleeptalk.streaming import ToolVocab

MY_VOCAB = ToolVocab(
    kinds={"shell": "bash", "read_file": "read", "web_search": "search"},
    subject_args={
        "bash": ("command",),
        "read": ("path", "file_path"),
        "search": ("query",),
    },
)
```

`{"command": "git status"}` becomes the spoken detail "running git status"
(subjects are shortened to a few speakable words — basenames, no flags, no
paths). Kinds absent from `subject_args` produce no detail line. Override
`verbs` to change phrasing (pi uses "searching the web for", for instance).

### 3. The registration

```python
from sleeptalk.registries import AGENTS

AGENTS.register(
    "my-agent",
    lambda config, env: GenericStdioAgent(SPEC, name="my-agent"),
    binary="my-agent",
    install_hint="install my-agent: npm i -g my-agent",
    config_fields={"model", "workspace_dir"},   # [agent] TOML keys you read
)
```

Then `config/profiles/<slug>.toml` (or `sleeptalk.toml`) selects it:

```toml
[agent]
type = "my-agent"
```

## What you get for free

Sentence streaming with first-clause fast-flush, speakable filtering (code
fences stripped), first-of-kind event dedup (`thinking` / `text` / `tool`),
idle + hard deadlines, keepalive ticks on audible silence, tool narration,
process lifecycle (spawn, NDJSON reader, 5s terminate → kill, close), and
abort.

## When you can't use GenericStdioAgent

If the protocol needs a handshake before prompts (see hermes-ACP), an
in-band abort (see pi), kill-and-resume continuity (see claude-code), or
isn't stdio at all (hermes-gateway, HTTP SSE), subclass or compose instead:
`StdioJsonRpcProcess` owns the process lifecycle, and `SentenceStreamer` /
`EventGate` / `DeadlineClock` / `narrate_tool` are individually reusable.
Look at `sleeptalk/agents/` — each adapter is transport + translation +
vocab table only.
