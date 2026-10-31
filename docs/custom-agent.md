# Write a custom agent adapter

Start with [Bring your own agent](bring-your-own-agent.md). Most agents need no Python: a command, a web endpoint, an ACP agent, or a remote bridge is a profile section, and `sleeptalk adapter new my-agent --kind python` generates a working version of this page's plugin.

Use this page when you want the in-process walkthrough: you implement `AgentAdapter` in Python with public `sleeptalk` imports only. Do not import `server.app`.

The runnable proof is `sleeptalk/examples/custom_agent.py`.

## What you will have

A class that voice can boot, talk to, interrupt, and shut down. After `warmup`, the agent has a session identity. `respond_streaming` yields speakable sentences. `abort` is safe when idle. `close` releases the run.

## Implement the five methods

Import the seam and the shared stream helpers.

```python
from sleeptalk.adapter import (
    AgentCapabilityManifest,
    CapabilitySupport,
    SessionIdentified,
    SupportLevel,
    VoiceSessionCapability,
    WorkerCapability,
    publish_event,
)
from sleeptalk.registries import AGENTS
from sleeptalk.streaming import EventGate, SentenceStreamer, ToolVocab, narrate_tool
```

Keep the `AgentAdapter` signatures. Do not add required methods. Voice already calls this Protocol. Optional extras are `agent_capabilities` and async `control`. See [Agent adapter contract](agent-adapter-contract.md).

| Method | When voice calls it | What you do |
| --- | --- | --- |
| `warmup` | Once at boot | Preheat process, model, or identity. |
| `respond_streaming` | Each spoken turn | Yield sentences as they complete. Fire `on_event` for progress. |
| `respond` | Slash commands and briefings | Return the full text. Joining the stream is enough. |
| `abort` | Barge-in, and also when idle | Stop current work. Return at once when nothing is running. |
| `close` | Shutdown | Tear down subprocesses and sockets. |

## Advertise identity

Set `agent_capabilities` to an `AgentCapabilityManifest` with `session_identity` at `supported`. In `warmup`, store an `upstream_session_id`. On each stream, publish `SessionIdentified` through `publish_event`.

A string `on_event` callback never sees that event. Permission and clarification are omitted the same way. Pass `typed_events(callback)` when the consumer wants the typed object.

Do not invent a speakable string for identity, permission, or clarification.

## Stream sentences

Feed model text through `SentenceStreamer`. Yield each completed sentence. Flush the tail at a clean turn end. Drop the buffer on abort. Do not flush a partial after abort.

Use `EventGate` for first-of-kind `thinking`, `text`, and `tool`. Emit `tool:<kind>` and `tooldetail:<detail>` through `narrate_tool` and a `ToolVocab`. Voice ignores unknown names.

The shared voice runtime owns preambles, VAD, barge-in presentation, progress narration, and keepalive. Report facts. Do not replay those policies inside the adapter.

## Abort

Hold an `asyncio.Event`. `abort` sets it. The stream checks the event between yields and returns. Call `abort` when idle in tests and at runtime. The method must not raise because nothing is running.

## Close

Set a closed flag and release resources. A later `respond_streaming` raises. `warmup` after `close` is not required.

## Register

```python
AGENTS.register(
    "custom-echo",
    lambda config, env: CustomAgent(),
    install_hint="custom agent example needs only python3",
)
```

Then select it.

```toml
[agent]
type = "custom-echo"
```

`config` and `env` are the `[agent]` table and the process environment. Read secrets from `env`, not from the TOML file.

For installed package discovery and validated private settings, use the
`sleeptalk.agents` entry point and `ComponentOptions` described in
[Component contracts](component-contracts.md). The normal CLI loads a selected
installed extension without changes to Sleeptalk's built-in registrations.

## Common errors

| Failure | Cause | Fix |
| --- | --- | --- |
| `Unknown agent 'custom-echo'` | The factory never registered | Import the module that calls `AGENTS.register` before `build_session`. |
| Stream hangs with no sentences | You yielded raw tokens or never flushed | Use `SentenceStreamer.feed` and `flush`. |
| Idle barge-in crashes | `abort` assumed a live turn | Make `abort` a no-op when the event is already set. |
| Permission became a spoken `thinking` | You encoded a rich event as a string | Publish the typed event. The string shim omits it. |
| Import error on `server.app` | The adapter reached into the web harness | Import `sleeptalk.adapter`, `sleeptalk.streaming`, and `sleeptalk.registries` only. |

## Run the example

From the `sleeptalk` package root, with the package on `PYTHONPATH`:

```bash
python examples/custom_agent.py
```

You should see `CUSTOM-AGENT ACCEPTANCE: OK`.

Focused tests:

```bash
uv run python -m pytest tests/test_custom_agent_example.py -q
```
