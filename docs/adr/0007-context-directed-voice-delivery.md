# ADR 0007: Caller context selects voice delivery

Date: 2026-09-09
Status: Accepted, amended 2026-09-10 to separate explicit local Web from agent Web

## Problem

Sleeptalk had separate Terminal and Web implementations, but the Host CLI
always asked for an interface. An agent already knows whether it is answering
in a local terminal or through a chat service. Asking the user to choose local
or hosted networking exposes an implementation detail and can send a local
call through hosted infrastructure without need.

## Usage

A local Terminal invocation identifies itself explicitly:

```bash
sleeptalk host ensure --profile default --surface terminal --route cascade --agent auto --json
sleeptalk host start-voice terminal --profile default
```

An agent invocation defaults to Web and may supply its integration name:

```bash
sleeptalk host ensure --profile default --route cascade --agent auto --json
sleeptalk host start-voice --profile default --json
```

The integration may supply its own name. `slack`, `whatsapp`, `telegram`, and
future chat integrations all have the same meaning: return a hosted link. The
Host does not maintain a list of chat products. Missing context also returns a
hosted link because `host` is the agent-facing command group. Explicit `web`
is the same-machine browser path; explicit `terminal` is attached native
audio. See ADR 0009. A named chat context still wins over explicit Web because
a localhost link is unusable on the recipient's device.

## Shape

`caller_context.py` parses the integration value into `CallerContext` and maps
each invocation once to Terminal, local Web, or remote Web. `local`, `terminal`,
`desktop`, and the reserved `unknown` value select an attached Terminal
session. Any integration name and a missing value select the hosted Web link
when the surface is omitted. Explicit Web with no named chat context selects
the local browser service.

During setup, the CLI records the inferred interface so dependency and doctor
checks match the first run. That stored value does not control later starts.
A local start uses an in-memory Terminal transport copy and never rewrites the
profile. A hosted start uses the existing bounded Web child. Both paths keep
the agent, the workspace, STT, and TTS on the machine where Sleeptalk runs.

The browser makes the second decision. `browserRuntimePolicy()` examines the
browser that opened the link. Regular browsers keep direct-first ICE. Any
detected embedded browser requests relay-only ICE. Generic iOS and Android
webview signatures are the primary classification; product markers are only
additional hints. The Worker requires TURN credentials before it consumes a
relay-required ticket.

## Synthesis decision

The implementation uses the smaller context-aware Host design. It reuses the
current CLI and call provider instead of adding a new hosting service. From
the capability-based candidate, it takes three constraints: profile state
does not override invocation context, the browser classifies the actual
opener, and a relay-required redemption fails before ticket mutation.

The design rejects a new context-file protocol and a general `VoiceHost`
service for v0.1. The environment and typed Python boundary provide the same
decision without another public subsystem.

## Tradeoffs accepted

- We accept Web as the agent-command default because an agent normally needs a
  link it can return to the caller. A local operator explicitly requests Web
  or Terminal according to the interface they want.
- We accept a detached one-call process for local Web so the printed URL stays
  reachable after the invoking command returns. The Host registry exposes and
  stops that bounded process.
- We accept relay cost for detected embedded browsers in exchange for a
  dependable in-app path across chat products.

## Alternatives considered

A universal hosted join page would keep one launch path, but it would make a
same-machine browser depend on hosted infrastructure. The original decision
also rejected a local browser server as duplication. Operator feedback showed
that Terminal audio and a browser UI are distinct requested surfaces, so ADR
0009 supersedes that part of this decision with a bounded one-call service.

## Open questions and risks

- Which agent integrations can set `SLEEPTALK_CALLER_CONTEXT` directly?
- Which chat clients expose reliable embedded-browser signals, microphone
  capture, and playback on each supported mobile operating system?
- What production TURN budget covers relay-only embedded-browser sessions?

## Next implementation step

Run real calls across a representative chat-client matrix through staging and
record browser classification, relay, microphone, playback, and failure
evidence under Astra plan 06.
