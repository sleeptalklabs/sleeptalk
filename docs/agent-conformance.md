# How to opt a vendor adapter into conformance

Upstream harness changes also follow the [upgrade policy](harness-upgrades.md).
Passing the shared fakes does not qualify a new upstream release; exercise
the actual runtime and record its exact version and evidence separately.

The cases live in the package at `sleeptalk.conformance`, so they run from an installed wheel. `sleeptalk adapter test` runs them against any profile; see [Bring your own agent](bring-your-own-agent.md#sleeptalk-adapter-test). `sleeptalk/tests/agent_conformance` wraps the same cases for pytest. The suite is the protocol check. Live-provider uptime is a different column.

## Run the in-tree fakes

These fakes prove the suite. They are not vendor adapters.

```bash
cd sleeptalk && uv run pytest tests/agent_conformance -q
```

The package ships three deterministic adapters: an in-process native fake, `AcpClient` against a local fake ACP server, and an HTTP-less gateway-shaped fake that claims `tool_lifecycle`.

## Add a vendor card

1. Implement `AgentAdapter` and set `agent_capabilities` to an `AgentCapabilityManifest`.
2. Add a factory that returns your adapter. Keep live keys out of the default suite.
3. In the vendor card's verification, import the case functions and run them against that factory:

```python
from tests.agent_conformance.cases import (
    case_stream_yields_sentence,
    case_permissions,
)
from tests.agent_conformance.harness import advertised, require, skip_unless
```

Outside this repository, import `sleeptalk.conformance.cases` directly. The cases raise `ConformanceSkip` and `ConformanceFailure`; `sleeptalk.conformance.runner.run_adapter_test(factory, adapter=..., mode="fixture")` runs them all and returns a report.

4. Name the conformance profile the card requires. The card stays red until the suite runs against that factory.

Do not edit the shared fakes to make a vendor look green.

## Capability gates

`advertised(adapter)` returns the manifest. `skip_unless(adapter, field)` skips when the field is `unsupported` or `unavailable`, and it prints the reason. `require(adapter, field)` uses the same gate for a claim that must then show behavior.

A `supported` advertisement never becomes a skip. If the field is `supported` and the behavior is missing, the case fails.

`unavailable` is not a protocol failure. It needs a reason. `voice_sessions` at `unavailable` must reject `StartVoiceSession`. It must not return `ControlAccepted`.

## Two columns

| Column | What it answers | Failure |
| --- | --- | --- |
| Protocol correctness | Does the adapter keep the contract it advertised? | A `supported` field with no matching event or control. |
| Live-provider availability | Can this session reach the vendor right now? | An `unavailable` reason. Missing keys, a down API, or a logged-out CLI. |

Do not fold those columns together. A skipped live call is availability. A false `supported` bit is a protocol fail.

## What the suite checks

Talkable floor, on every adapter: warmup then close, at least one sentence, idle abort, mid-stream abort, timeout or cancel that settles, thinking before text when both occur, and no `voice_policy_runtime` import. Events are facts. They are not preamble text.

Capability-gated: tool lifecycle, permissions, clarification, steering, usage, and `voice_sessions` unavailable. Permissions stay a request. `PermissionRequested` has no `allowed` or `approved` field. `ResolvePermission` requires `decision`. If `permissions` is `supported`, a request must appear.

See [Agent adapter contract](agent-adapter-contract.md) for the types.
