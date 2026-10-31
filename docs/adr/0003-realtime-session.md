# ADR 0003: RealtimeSession — one public session contract for all routes

Date: 2026-08-20
Status: Proposed

## Relationship to Roadmap 2.0 and the product vocabulary

Implements Roadmap 2.0 §4.3 (canonical session contract) and closes gap
`r2-gap-session`. Terms follow the product vocabulary:
a **session** is the canonical live voice conversation object; a **route**
(`cascade` | `native` | `hybrid`) is the assembly strategy; an **operation**
is the catalog-priceable unit of provider work; an **attempt** is one concrete
provider request. This ADR names the contract; Phase 1
(`r2-cascade-core`, `r2-route-protocol`, `r2-session-events`) implements it.

## Context

Two route families exist with no shared contract:

- **Cascade** (`sleeptalk/builder.py` `VoiceSession` +
  `server/app/calls/runner.py` `stream_and_speak`): registry composition; six
  concurrent turn tasks (producer, synthesizer, player, preamble, gap watcher,
  watchdog); `_turn_generation` epoch-scoped stale-audio dropping; agent
  abort; `_interrupted_context` resume; sliding-idle watchdog recovery.
- **Native** (`server/app/discord_realtime/openai_client.py`
  `MarinRealtimeClient`): persistent WebSocket; `response.cancel`; TurnScope
  tool interruption; 40-minute session rollover with `SessionState`
  snapshot/restore; playout-fraction partial-output tracking.
- **Hybrid** (`server/app/realtime_hybrid_discord_bot.py`): a native client
  delegating to an `AgentAdapter` worker via `delegate_to_agent` tools.

Events and identity are ad-hoc per surface: latency stages are disjoint
vocabularies; `UsageMeter` attaches only to cascade; session IDs are invented
per surface (`discord-{channel}`, `marin-realtime-{id(self):x}`,
`openai-rt-{hex}`) and the native one changes across rollover.

## Decision

Define one **`RealtimeSession`** protocol — the public, route-agnostic session
contract — plus a **`Route`** protocol that each route type implements behind
it. The session owns identity, lifecycle, event normalization, and usage
identity; the route owns audio-to-audio assembly. Transports, agents, and
providers plug in as today (registries + `AgentAdapter` + `TTSAdapter`), and
per ADR 0001 the implementation lands in platform-neutral modules (package
`sleeptalk` runtime; `app/calls/` for surface adapters), never importing a
platform SDK from the session.

### 2026-08-28 amendment: hybrid semantic ownership

Hybrid is opt-in and uses one deterministic ownership gate before semantic
output is allowed. Every committed utterance receives an immutable
`LocalOwned`, `RealtimeOwned`, or `SpecialistOwned` assignment with an intent
ID and presentation epoch. Mixed utterances belong wholly to the specialist.
The positive specialist table covers workspace/tool work, external research,
live facts, side effects, permissions, explicit delegation, and follow-ups to
specialist work; unmatched conversation belongs to Realtime. Local ownership
is reserved for presence, repeat/status, speech interruption, permission or
clarification replies, and explicit task cancellation.

OpenAI Realtime runs with automatic response creation disabled and no domain
tools. A response may be created only after a `RealtimeOwned` assignment is
recorded. The configured long-lived `AgentAdapter` is the sole authority for
specialist work. Realtime is the only mouth. The specialist appends raw
findings. Realtime tells the user that information in its own voice and must
not invent facts beyond the brief. One top-level specialist task runs at a
time, although that agent may coordinate subagents.

Presentation interruption and task cancellation are separate operations.
`interrupt_presentation()` revokes the presentation epoch, clears queued
audio, and cancels any active Realtime response while detached specialist work
continues. `cancel_task(task_id)` is explicit, terminal, and invokes adapter
control/abort once. Late task events remain journaled but cannot speak or
reopen a terminal task. A session-owned reducer assigns a monotonic sequence
and owner/intent/task/presentation correlation; it survives provider
reconnects, while process-crash recovery remains out of scope.

Assistant self-knowledge follows the same authority boundary. Realtime may
answer only from its visible conversation and stable general knowledge;
questions about prior-session memory, private user state, tools, skills, or
runtime capabilities belong to the specialist. The route may answer only a
narrow, deterministic set of topology questions about the composite
assistant itself. Realtime receives an immutable role contract containing the
public display name and these boundaries, but no persona, capability manifest,
workspace data, tool schemas, memory, or private specialist state. The role
contract is framing, not an ownership mechanism: Realtime remains tool-free
and cannot delegate work itself.

Each owner-specific assignment carries a typed reason, and
`hybrid.intent.committed` serializes that reason before any acknowledgment or
answer begins. This makes routing visible to every surface without introducing
a second ownership event hierarchy. Sharing semantic history across Realtime,
local, and specialist owners is explicitly deferred; a future cross-owner
ledger requires separate retention, privacy, and projection decisions.

### 1. Identity

| Identity | Form | Rules |
|---|---|---|
| `session_id` | opaque string, minted at session creation | **Stable for the life of the session, including recovery and rollover.** Never derived from a Python object id or a reconnectable transport connection. Survives native rollover (state snapshot must carry it). |
| `turn_id` | monotonic integer per session (today's `_turn_generation` generalized) | Bumped on turn commit and on teardown. Uniquely orders turns within a session; stale output is dropped by comparing `turn_id`. |
| `attempt_id` | `{session_id}:{turn_id}:{provider}:{operation}:{seq}` | Metering and idempotency key. Today's `{trace_id}:agent:1` / `{trace_id}:tts:{i}:stream` strings generalize to this shape; the server-issued opaque attempt ID required by ADR 0002 for hosted billing is a control-plane overlay on the same key, not a second identity. |
| `provider` | provider registry key (`deepgram`, `cartesia`, …; agent adapters report the adapter name) | Matches the provider catalog vocabulary. |
| `operation` | dotted operation name (`stt.stream`, `tts.stream`, `tts.buffered`, `agent.stream`, and native `rt.audio` per audio-second) | Operations are the metering vocabulary. The priced catalog (ADR 0004) contains speech operations only; `agent.stream` is metered for diagnostics and identity, never billed. Diagnostic latency marks are not operations. |

Turn-level tracing (`VoiceLatencyTrace.trace_id`) remains a diagnostic overlay
keyed by `(session_id, turn_id)`; it is not an identity of record.

### 2. Lifecycle state machine

```
CREATED ──connect()──▶ CONNECTING ──▶ ACTIVE ──close()──▶ CLOSING ──▶ CLOSED
                         │              │  ▲
                         │              ▼  │
                         │           RECOVERING
                         └──connect failed──▶ CLOSED (error)
```

- `connect()`: acquire transport and route resources. Idempotent while
  CONNECTING/ACTIVE (no-op, as `MarinRealtimeClient.connect` today).
- `start()` (implicit in ACTIVE): turn loop runs; input audio accepted.
- `cancel()`: cancel the **current turn** only (see §4). The session stays
  ACTIVE.
- `recover()`: attempt restoration after a recoverable failure. Returns a
  `RecoveryOutcome` (`restored` | `restarted` | `failed`). While RECOVERING,
  new turns are refused with a machine-checkable error; an in-flight turn is
  terminated under cancellation semantics. `recover()` **never** silently
  resumes mid-utterance audio across the failure (non-goal: seamless
  mid-utterance failover).
- `close()`: single-writer, idempotent shutdown (`LifecycleSupervisor`
  semantics). On close: cancel in-flight turn, abort agent, finish open
  attempts (`status="session_closed"`), emit `session_summary`, flush
  transport. Nothing from the session may produce audio or agent output after
  `close()` returns — the "fresh floor" rule from Discord teardown.

Every transition emits a normalized lifecycle event (§3), so the state
machine is directly testable by asserting the event sequence.

### 3. Normalized events

One event envelope, one vocabulary, emitted for every route:

```text
SessionEvent {
  session_id, turn_id?, timestamp,
  type: one of the normalized types below,
  payload: type-specific, raw provider fields preserved under payload.raw
}
```

| Type | When | Notes |
|---|---|---|
| `session.state` | lifecycle transition | `payload.state` ∈ the §2 states |
| `transcript.partial` / `transcript.final` | STT or native server transcription | speaker: `user` \| `assistant` |
| `response.started` / `response.first_output` / `response.completed` / `response.cancelled` | turn-level response lifecycle | first_output = first audio OR first speakable text, per route, recorded in `payload.kind` |
| `agent.event` | agent `thinking` / `text` / `tool` / `workbench` | normalized from `AgentAdapter.on_event` names |
| `tool.started` / `tool.completed` / `tool.failed` | tool calls (both routes) | native function-call events and cascade agent tool events map here |
| `progress` | tiered preamble / narration / knock | replaces route-specific ack stages |
| `interruption` | barge-in detected or applied | `payload.source`: `user_speech` \| `cancel`; `payload.phase`: `working` \| `answering` (today's `_interrupted_context.phase`) |
| `error` | recoverable or fatal | `payload.fatal: bool`; fatal ⇒ session leaves ACTIVE per §2 |
| `usage.attempt.started` / `usage.attempt.finished` / `usage.summary` | mirror of `UsageMeter` | **raw provider units preserved verbatim** (characters, bytes, seconds, tokens) alongside normalized totals |

Route-specific detail stays available — native OpenAI events and cascade
latency stages map into the vocabulary and the originals are preserved under
`payload.raw`. Latency marks (`VoiceLatencyTrace`) remain diagnostic and are
not part of the public contract, though Phase 1 should emit them from the
same code paths so timings stay comparable.

### 4. Cancellation and partial-output semantics (testable)

Cancellation is defined at the **turn** level, epoch-scoped, with an explicit
contract each route must satisfy. These clauses are the contract-test
fixtures for `r2-route-contract-tests`:

1. **Monotonic generation.** A new turn commit (or session teardown) bumps
   `turn_id`. Any output task holding an older `turn_id` must stop before
   producing further user-audible output (cascade: `stale_response_dropped`;
   the general rule).
2. **Propagation order.** `cancel()` must, in order: (a) stop playback /
   playout of the old turn, (b) cancel route-internal tasks (the six cascade
   tasks; native `TurnScope.interrupt()`), (c) abort upstream work
   (`AgentAdapter.abort()`; native `response.cancel`), (d) finish every open
   attempt with `status="cancelled"`.
3. **Partial output is accounted by what crossed the playout boundary, not
   what was generated.** Cascade: spoken sentence list (per-sentence
   granularity). Native: playout-fraction truncation (byte-continuous).
   The event payload carries both `generated` and `played` measures; routes
   may differ in granularity but must report both and never report generated
   audio as spoken.
4. **Interruption context.** A cancelled answering turn records an
   interruption context (transcript, spoken-so-far, phase) sufficient for a
   resume offer; a fresh substantive turn clears it. Native routes may
   report "unsupported" for resume — that is a declared route difference
   (§5), not a contract violation.
5. **Cancellation is safe when idle.** Cancelling with no active turn is a
   no-op, never an error (cascade barge-in and native `cancel_response`
   already behave this way).
6. **Watchdog stall recovery is cancellation + error.** A turn idle past
   policy limits aborts upstream work, fails the turn with a recoverable
   `error` event, and leaves the session ACTIVE.

### 5. Route types and declared material differences

The contract deliberately does not hide route differences. Every route
implements §1–§4 and declares, in session metadata, its route type and
capabilities:

| Dimension | `cascade` | `native` | `hybrid` |
|---|---|---|---|
| Assembly | STT → agent → TTS (sentence-at-a-time) | single speech-to-speech model | realtime model delegating turns to agent worker |
| Turn detection | local VAD + endpointing | server VAD + local owner commits | native rules |
| Cancellation of upstream | `agent.abort()` + TTS task cancel | `response.cancel` + TurnScope | both |
| Partial-output granularity | per sentence | byte/playout fraction | native granularity |
| Resume offer after interruption | supported | **not supported** (server owns context) | not supported |
| Recovery mechanisms | watchdog, transport reconnect, TTS reconnect-once | session rollover (state snapshot; `session_id` preserved) | native rollover |
| Usage operations | `stt.stream`, `agent.stream`, `tts.stream`/`tts.buffered` | `rt.audio` (fused; single operation) | `rt.audio` + `agent.stream` |

A route must fail loudly (capability error at construction, not a runtime
surprise) when asked for an unsupported capability, e.g. resume on native.
`CascadeRoute` is the first production implementation of the contract; the
existing OpenAI Realtime code conforms to the contract as an internal
compatibility implementation before native routes are sold publicly
(Roadmap 2.0 §4.3). Hybrid proves the contract composes two upstreams under
one `turn_id` epoch.

### 6. Public shape (Phase 1 handoff)

Code interfaces Phase 1 must implement (names are the contract; signatures
illustrative):

- `sleeptalk/session.py` — `RealtimeSession` (protocol + cascade
  production class): `session_id`, `route_type`, `connect/start/cancel/close/
  recover`, `send_audio(chunk)`, output audio stream (`async for audio in
  session.output`), `async for event in session.events`, `usage` snapshot.
  `build_session()` returns a `RealtimeSession`; `VoiceSession` migrates into
  it (rename-on-touch per vocabulary).
- `sleeptalk/route.py` — `Route` protocol: `prepare(session)` /
  `run_turn(input, ctx)` / `cancel_turn(turn_id)` / `close()`, plus
  `capabilities` (streaming, interruption granularity, resume support).
  Implementations: `CascadeRoute` (wraps today's `stream_and_speak` pipeline
  mechanics), `NativeRealtimeRoute` (wraps a `MarinRealtimeClient`-shaped
  client), `HybridRoute`.
- `sleeptalk/events.py` — `SessionEvent` envelope + normalized
  types (§3) and the mapping layer from route-native events.
- `UsageMeter` gains: `session_id` from the session (no private minting),
  native-route attachment (`rt.audio`), and `RecoveryOutcome`-aware attempt
  finishing.
- Contract-test fixtures (`r2-route-contract-tests`): a scripted in-memory
  route harness (NullTransport descendant) that drives every §2 transition
  and every §4 clause — stale-audio drop, propagation order, played-vs-
  generated accounting, idle-cancel no-op, watchdog abort — asserted as
  event sequences. One suite runs against all route implementations.

### 7. Verification walkthroughs (as required by the task)

- **Discord cascade trace** (from `stream_and_speak` + barge-in): CREATED →
  connect (transport up) → ACTIVE → `response.started(turn 1)` →
  `transcript.final(user)` → `agent.event`/`progress` →
  `response.first_output` → user barge-in: `interruption(phase=answering)` →
  cancel propagation (playback cut → tasks cancelled → `agent.abort` →
  attempts `cancelled`) → partial output `played < generated` → ACTIVE →
  resume turn 2 (bump, stale drop of turn-1 audio) → watchdog fires on a
  stalled turn 3 → `error(recoverable)` → turn 4 completes →
  `usage.summary` → close → CLOSING → CLOSED. Together the two walkthroughs
  cover every state in §2 (CONNECTING and RECOVERING appear in the native
  trace) and every cancellation clause except §4 clause 5 (idle-cancel
  no-op), which the §6 contract-test suite asserts for every route
  implementation.
- **Native proof trace** (MarinRealtimeClient rollover): CREATED →
  CONNECTING → ACTIVE → server-VAD `interruption` cuts playback →
  `response.cancelled` → tool call `tool.started/completed` → 40-min
  `recover()` → RECOVERING → ACTIVE with **same `session_id`**, state
  restored → close → CLOSED. Rollover demonstrates the stability requirement
  on `session_id` and the `restored` recovery outcome.

## Non-goals

- No seamless mid-utterance failover across process or provider death —
  recovery restarts turns, it does not splice audio.
- No public launch of native routes; native implementations are internal
  compatibility proofs under this contract until their own gates pass.
- No change to the transport/agent/provider seams or to ADR 0002's access
  and billing requirements; this ADR adds only the session layer above them.

## Consequences

- `VoiceSession` becomes an implementation detail absorbed by
  `RealtimeSession`; `stream_and_speak`'s six-task pipeline becomes cascade
  route internals; `PiDiscordLiveSession`'s duck-typed response state moves
  behind the contract.
- Native clients must adopt a stable `session_id` (replacing
  `marin-realtime-{id(self):x}`) and normalized event emission; the OpenAI
  client's rollover must preserve the id.
- Latency diagnostics keep their current stage names but gain a mapping into
  normalized events so cross-route comparison is possible.
- The Discord "fresh floor" teardown, watchdog recovery, and playout-fraction
  accounting rules become contract clauses instead of tribal knowledge.
