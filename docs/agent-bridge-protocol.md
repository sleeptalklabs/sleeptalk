# Sleeptalk Agent Bridge v1

The public remote protocol for a non-Python agent. The wire is language-neutral JSON frames on one authenticated TLS stream. Semantics match `AgentAdapter` in `sleeptalk/adapter.py`. Local Python agents implement that Protocol in process. They do not speak this protocol.

[Bridge Lite](#bridge-lite-profile) uses the same frames without the session machinery, over a local command's stdio or one streamed HTTP request per turn. Start with [Bring your own agent](bring-your-own-agent.md) to choose between them.

Vendor gateways are not this protocol. Hermes TUI Gateway, OpenClaw Gateway, and other first-party control planes stay behind their named adapters. A remote agent implements Agent Bridge v1. It does not impersonate a vendor wire format.

Related lookup:

- [Bring your own agent](bring-your-own-agent.md) for choosing a route, scaffolds, and `sleeptalk adapter test`
- [Agent adapter contract](agent-adapter-contract.md) for the in-process seam
- [Write a custom agent adapter](custom-agent.md) for a Python in-process implementation

## Transport

The session uses TLS 1.2 or newer. Certificate verification is required.

The stream is one of:

- WebSocket at `wss://<host>/v1/agent-bridge` with subprotocol `sleeptalk-agent-bridge-v1`
- TLS TCP that carries one UTF-8 JSON object per line

One connection is one agent session. Query strings carry no credentials. `ws://` and plaintext TCP are valid only on loopback in tests.

## Authentication

Remote connections use a scoped bearer credential after TLS is up. The token is an installation credential or a session credential. The scope is this bridge only.

The client sends the token in `hello.auth` or in the WebSocket `Authorization` header as `Bearer <token>`. The token never appears in the URL, in a query parameter, in a fragment, or in a log line.

A failed hello returns `hello.err` with `code` `auth`. The error message names the class of failure. It does not echo the token.

## Handshake and version negotiation

The first application frame on a new connection is `hello`. The peer replies with `hello.ok` or `hello.err`. No other application frame is valid before that reply.

`hello.v` is the major protocol version. This document is version `1`. The server accepts only an exact major match. A mismatch returns `hello.err` with `code` `version_mismatch` and does not run the agent.

`CONTRACT_VERSION` in `adapter.py` is also `1`. Bridge v1 carries that contract. Additive optional fields inside a major version are backwards compatible. A receiver ignores unknown optional fields. A receiver rejects an unknown required capability with `hello.err` and `code` `unknown_capability`.

Capabilities use the same names and levels as `AgentCapabilityManifest`. Each field is `unsupported`, `supported`, or `unavailable`. `unavailable` requires `reason`. `supported` forbids `reason`. `hello.ok.capabilities` is the intersection the session may use. Frames for a capability that is not `supported` are protocol errors. Capability keys accept snake_case (`session_identity`) and camelCase (`sessionIdentity`).

The talkable floor needs no capability bit. Text in, sentence stream, abort, heartbeat, credit, and errors always work.

`hello.ok` also returns session limits. Defaults:

| Limit | Default | Role |
| --- | --- | --- |
| `heartbeatMs` | `15000` | Idle heartbeat period |
| `missedHeartbeats` | `2` | Misses before the connection is dead |
| `sendQueue` | `32` | Max unacked application frames held per side |
| `initialCredit` | `16` | Starting send credit |
| `replayWindowMs` | `120000` | Idempotency and resume replay window |
| `maxFrameBytes` | `65536` | Hard frame size |

`hello.ok` also returns `sessionId` and `resumeToken`. `resumeToken` is a secret. Treat it like the bearer token.

## Frame envelope

Every frame is one JSON object.

| Field | Required | Meaning |
| --- | --- | --- |
| `v` | yes | Major version. `1` on this protocol. |
| `type` | yes | Frame discriminant. |
| `id` | yes on application frames | Correlation id. Unique per sender for the replay window. |
| `ts` | no | UTC timestamp. |
| `turnId` | turn-scoped frames | Non-negative integer. Matches one `turn.prompt`. |
| `ref` | replies | `id` of the frame this answers. |
| `requestId` | permission and clarification | Ties a request to its resolve or answer. |
| `callId` | tool frames | Optional upstream tool call id. |

Canonical keys are camelCase. A receiver also accepts the snake_case aliases used by the Python dataclasses. Those aliases include `turn_id`, `request_id`, `call_id`, `session_id`, `target_id`, `input_tokens`, `output_tokens`, and `cost_minor_units`. A sender picks one shape and keeps it for the connection.

`hello` and `heartbeat` may omit `id`. Application frames that cause work or report work must carry `id`.

## Host to agent

The host is the Sleeptalk voice runtime.

| `type` | Fields | Adapter method |
| --- | --- | --- |
| `hello` | `v`, `auth`, `capabilities`, `client` | Connect |
| `session.warmup` | `id` | `warmup` |
| `session.close` | `id` | `close` |
| `turn.prompt` | `id`, `turnId`, `text`, `mode` | `respond_streaming` when `mode` is `stream` or omitted. `respond` when `mode` is `full`. |
| `turn.abort` | `id`, `turnId` | `abort` |
| `permission.resolve` | `id`, `turnId`, `requestId`, `decision` | `control(ResolvePermission)` |
| `clarification.answer` | `id`, `turnId`, `requestId`, `answer` | `control(AnswerClarification)` |
| `steer` | `id`, `turnId`, `instruction`, `targetId` | `control(Steer)` |
| `cancel` | `id`, `turnId`, `targetId` | `control(Cancel)` |
| `voice_session.start` | `id`, `requestId`, `target` | `control(StartVoiceSession)` |
| `credit` | `add` | Backpressure grant |
| `heartbeat` | `lastId` | Liveness |
| `disconnect` | `reason` | Orderly teardown |

`mode` on `turn.prompt` is `stream` or `full`. One `turn.prompt` may be in flight. The next prompt is valid only after `turn.completed`, `turn.aborted`, or a fatal `error`. `turn.abort` is valid during that turn and when idle.

`decision` on `permission.resolve` is required. Legal values are `deny`, `allow_once`, and `allow_session`. There is no default.

`target` on `voice_session.start` is `{"kind":"web_link","returnUrl":...}` or `{"kind":"discord_channel","guildId":"...","channelId":"..."}`.

`auth` on `hello` is `{"scheme":"bearer","token":"<opaque>"}` when the WebSocket header is absent.

## Agent to host

| `type` | Fields | Adapter event or result |
| --- | --- | --- |
| `hello.ok` | `v`, `sessionId`, `resumeToken`, `capabilities`, `limits` | Handshake success |
| `hello.err` | `code`, `message` | Handshake failure |
| `session.identified` | `id`, `upstreamSessionId`, `resumable` | `SessionIdentified` |
| `thinking.started` | `id`, `turnId` | `ThinkingStarted` |
| `sentence` | `id`, `turnId`, `text` | One yield from `respond_streaming` |
| `assistant.delta` | `id`, `turnId`, `text` | `AssistantDelta`. Optional. Voice uses `sentence`. |
| `tool` | `id`, `turnId`, `name`, `phase`, `callId`, `detail`, `resultSummary` | `ToolEvent` |
| `permission.requested` | `id`, `turnId`, `requestId`, `prompt`, `options`, `toolCallId` | `PermissionRequested` |
| `clarification.requested` | `id`, `turnId`, `requestId`, `prompt`, `choices` | `ClarificationRequested` |
| `status.changed` | `id`, `turnId`, `state`, `summary` | `StatusChanged` |
| `usage.reported` | `id`, `turnId`, `inputTokens`, `outputTokens`, `costMinorUnits`, `currency` | `UsageReported` |
| `worker` | `id`, `turnId`, `workerId`, `phase`, `label`, `parentTurnId`, `detached`, `summary` | `WorkerEvent` |
| `legacy` | `id`, `turnId`, `value` | `LegacyAgentEvent` |
| `error` | `id`, `turnId`, `code`, `message`, `retryable`, `fatal` | `AgentError` |
| `turn.completed` | `id`, `turnId`, `ref`, `text` | Stream or `respond` finished. `text` is set for `mode=full`. |
| `turn.aborted` | `id`, `turnId`, `ref` | Abort finished |
| `control.accepted` | `id`, `ref`, `requestId`, `resource` | `ControlAccepted` |
| `control.rejected` | `id`, `ref`, `reason`, `retryable` | `ControlRejected` |
| `credit` | `add` | Credit request or return |
| `heartbeat` | `lastId` | Liveness |

`tool.phase` is `started`, `updated`, `completed`, or `failed`.

`status.changed.state` is `idle`, `working`, `blocked`, `completed`, `cancelled`, or `failed`.

`permission.requested` has no `allowed` field and no `approved` field. `options` lists the decisions the agent will accept.

`sentence.text` is a speakable sentence. The agent yields early and often. The host owns preambles, VAD, barge-in presentation, progress narration, and keepalive. The agent reports facts.

## Heartbeat

Each side sends `heartbeat` when it has been silent for `heartbeatMs`. `lastId` is the last application `id` that side received. Two missed periods make the connection dead. The surviving side stops work as if `turn.abort` ran, then reconnects if it is the client.

Heartbeat frames do not consume credit. Heartbeat frames do not carry secrets.

## Bounded queues and backpressure

Each side holds at most `sendQueue` unacked application frames. Each application frame the agent sends consumes one credit. The host grants credit with `{"type":"credit","add":N}`. `hello.ok.limits.initialCredit` is the starting balance.

When credit is `0`, the agent sends no application frames. Heartbeat, fatal `error`, and `turn.aborted` remain legal. A host that never grants credit gets `error` with `code` `backpressure` and `retryable` true. The queue does not grow past `sendQueue`. A frame that would exceed the queue is dropped on the sender and surfaced as that error.

The host applies the same credit window toward the agent for control bursts. `turn.prompt` still follows the one-in-flight rule.

A slow TTS consumer is a credit stall, not an unbounded buffer.

## Correlation, idempotency, and replay

`id` identifies a frame for ack, resume, and replay. A side-effecting frame uses a stable `id` for its logical operation. The receiver executes that `id` once inside `replayWindowMs`. A duplicate inside the window returns the original result and does not run again. A duplicate outside the window returns `error` with `code` `replay_expired` and `retryable` false.

Side-effecting types are `turn.prompt`, `turn.abort`, `permission.resolve`, `clarification.answer`, `steer`, `cancel`, `voice_session.start`, `session.warmup`, and `session.close`.

Replies set `ref` to the request `id`. Turn-scoped replies also set `turnId`.

## Reconnect

The client opens a new TLS stream and sends `hello` with the same bearer token, the same `sessionId`, and `resumeToken`. The server rotates `resumeToken` on `hello.ok`. Both sides replay unacked application frames whose `id` is still inside `replayWindowMs`. Frames outside the window are not replayed. The sender then treats those operations as `replay_expired`.

A resume `hello` that fails authentication or presents an unknown `resumeToken` returns `hello.err` with `code` `auth`. The old session is not attached.

After resume, in-flight abort still means abort. A half-delivered sentence stream does not resume mid-sentence. The agent sends `turn.aborted` or starts a fresh `turn.completed` only if the original turn already finished.

## Permission cannot become approval

`permission.requested` asks. It does not decide.

`permission.resolve.decision` is the only approval signal. A missing host UI, a timeout, a dropped connection, a clarification answer, a heartbeat miss, or an absent `control` implementation is not `allow_once` and not `allow_session`. The agent stays `blocked` or treats the request as `deny`.

The string event shim on the local adapter omits permission and clarification. The bridge does the same. Those frames never appear as `thinking.started`.

## Secrets

The bearer token and `resumeToken` are secrets. They never appear in URLs, logs, task files, transcripts, `error.message`, heartbeat bodies, or protocol diagnostics. Frame dumps in tests use placeholders. A logger that prints raw frames must redact `auth`, `token`, `resumeToken`, and `Authorization`.

## Errors

`error` and `hello.err` share codes.

| `code` | Meaning |
| --- | --- |
| `auth` | Missing, expired, or unscoped credential |
| `version_mismatch` | Major version is not `1` |
| `unknown_capability` | Required capability is not in v1 |
| `replay_expired` | Duplicate `id` outside the window |
| `backpressure` | Credit or queue exhausted |
| `protocol` | Malformed frame or out-of-order type |
| `agent` | Upstream agent failed |
| `abort` | Work stopped by `turn.abort` |

`retryable` and `fatal` match `AgentError`. `fatal` true ends the session.

## Example. Python-shaped prompt and sentence stream

A Python host speaks snake_case aliases. The remote agent still understands them.

```json
{"v":1,"type":"hello","auth":{"scheme":"bearer","token":"<redacted>"},"capabilities":{"session_identity":{"level":"supported"},"tool_lifecycle":{"level":"supported"},"permissions":{"level":"unsupported"},"clarification":{"level":"unsupported"},"structured_events":{"level":"supported"},"status":{"level":"unsupported"},"usage":{"level":"unsupported"},"steering":{"level":"unsupported"},"targeted_cancellation":{"level":"unsupported"},"workers":{"level":"unsupported"},"voice_sessions":{"level":"unsupported"}},"client":{"name":"sleeptalk","language":"python"}}
{"v":1,"type":"hello.ok","session_id":"sess-1","resume_token":"<redacted>","capabilities":{"session_identity":{"level":"supported"},"tool_lifecycle":{"level":"supported"},"permissions":{"level":"unsupported"},"clarification":{"level":"unsupported"},"structured_events":{"level":"supported"},"status":{"level":"unsupported"},"usage":{"level":"unsupported"},"steering":{"level":"unsupported"},"targeted_cancellation":{"level":"unsupported"},"workers":{"level":"unsupported"},"voice_sessions":{"level":"unsupported"}},"limits":{"heartbeat_ms":15000,"missed_heartbeats":2,"send_queue":32,"initial_credit":16,"replay_window_ms":120000,"max_frame_bytes":65536}}
{"v":1,"type":"turn.prompt","id":"p-1","turn_id":1,"text":"hello there","mode":"stream"}
{"v":1,"type":"session.identified","id":"e-1","turn_id":1,"upstream_session_id":"agent-77","resumable":false}
{"v":1,"type":"thinking.started","id":"e-2","turn_id":1}
{"v":1,"type":"tool","id":"e-3","turn_id":1,"name":"bash","phase":"started","detail":"running echo hello"}
{"v":1,"type":"sentence","id":"e-4","turn_id":1,"text":"You said hello there."}
{"v":1,"type":"turn.completed","id":"e-5","turn_id":1,"ref":"p-1"}
```

## Example. TypeScript-shaped permission and abort

A TypeScript agent speaks canonical camelCase. The host still understands it.

```json
{"v":1,"type":"hello","auth":{"scheme":"bearer","token":"<redacted>"},"capabilities":{"permissions":{"level":"supported"},"clarification":{"level":"unsupported"},"sessionIdentity":{"level":"unsupported"},"toolLifecycle":{"level":"supported"},"structuredEvents":{"level":"supported"},"status":{"level":"supported"},"usage":{"level":"supported"},"steering":{"level":"unsupported"},"targetedCancellation":{"level":"unsupported"},"workers":{"level":"unsupported"},"voiceSessions":{"level":"unsupported"}},"client":{"name":"remote-ts","language":"typescript"}}
{"v":1,"type":"hello.ok","sessionId":"sess-2","resumeToken":"<redacted>","capabilities":{"permissions":{"level":"supported"},"clarification":{"level":"unsupported"},"sessionIdentity":{"level":"unsupported"},"toolLifecycle":{"level":"supported"},"structuredEvents":{"level":"supported"},"status":{"level":"supported"},"usage":{"level":"supported"},"steering":{"level":"unsupported"},"targetedCancellation":{"level":"unsupported"},"workers":{"level":"unsupported"},"voiceSessions":{"level":"unsupported"}},"limits":{"heartbeatMs":15000,"missedHeartbeats":2,"sendQueue":32,"initialCredit":16,"replayWindowMs":120000,"maxFrameBytes":65536}}
{"v":1,"type":"turn.prompt","id":"p-9","turnId":3,"text":"delete build artifacts","mode":"stream"}
{"v":1,"type":"status.changed","id":"e-10","turnId":3,"state":"working"}
{"v":1,"type":"permission.requested","id":"e-11","turnId":3,"requestId":"perm-3","prompt":"Allow rm -rf dist?","options":["deny","allow_once"]}
{"v":1,"type":"status.changed","id":"e-12","turnId":3,"state":"blocked"}
{"v":1,"type":"permission.resolve","id":"c-3","turnId":3,"requestId":"perm-3","decision":"deny"}
{"v":1,"type":"control.accepted","id":"e-13","ref":"c-3","requestId":"perm-3"}
{"v":1,"type":"turn.abort","id":"a-1","turnId":3}
{"v":1,"type":"turn.aborted","id":"e-14","turnId":3,"ref":"a-1"}
{"v":1,"type":"usage.reported","id":"e-15","turnId":3,"inputTokens":120,"outputTokens":8,"costMinorUnits":3,"currency":"USD"}
{"v":1,"type":"error","id":"e-16","turnId":3,"code":"abort","message":"turn aborted","retryable":false,"fatal":false}
```

The `permission.requested` frame has no approval field. The only decision in the trace is the host's `permission.resolve`.

## Threat walk of a remote connection

1. **Token in the URL.** An attacker copies `wss://host/v1/agent-bridge?token=...` from a proxy log. The server rejects any hello whose token arrived as a query parameter or fragment. Operators never put the bearer token in a shareable URL.

2. **Missing permission UI.** The host process has no operator. The agent emits `permission.requested` and waits. Time passes. The host sends nothing. The agent does not invent `allow_once`. Work stays blocked or the agent denies. A test that treats silence as approval fails this protocol.

3. **Replayed allow.** An attacker replays `permission.resolve` with `decision` `allow_session` after the window. The server returns `replay_expired` and does not grant the tool. Inside the window the same `id` returns the original result. A new allow needs a new `id` bound to a live `requestId`.

4. **Resume hijack.** An attacker presents `sessionId` without `resumeToken`, or with a leaked token printed in a debug dump. Hello fails `auth`. Log redaction of `resumeToken` is part of the defense.

5. **TLS downgrade.** A client is pointed at `ws://` on a public host. The server does not speak the protocol in plaintext off loopback. Certificate verification stays on.

6. **Queue bomb.** A remote agent streams unbounded `assistant.delta` frames at a slow speaker. Credit hits `0`. Extra frames never enter the queue. The host keeps heartbeat and can `turn.abort`.

7. **Confused deputy through a vendor gateway.** A host is configured to speak Hermes Gateway JSON-RPC toward a user-supplied URL and call that Agent Bridge. That configuration is invalid. Vendor gateways stay on their adapters. Agent Bridge endpoints speak only these frames.

8. **Error echo.** An auth failure includes the presented token in `message`. The protocol forbids that. Diagnostics name `auth` and a stable reason token such as `expired`.

9. **Clarification as approval.** A host answers `clarification.requested` and the agent treats the answer as `allow_once`. That is a protocol violation. Clarification returns text. Permission returns a `decision`.

## Local process versus remote network

A loopback test and a public host are not the same threat.

On loopback, plaintext TCP or `ws://` is allowed so a fixture can prove the frames without certificates. The process boundary is the operating system. The token still must not appear in a URL, a query string, a log line, or an error message.

Off loopback, the stream is TLS 1.2 or newer with certificate verification. `start_tcp` and `start_ws` in the Python reference listen in the clear, so they refuse any non-loopback bind. A production remote listener needs a TLS terminator in front, or a later TLS accept path. `0.0.0.0` is not loopback.

A local in-process `AgentAdapter` never speaks this protocol. A local command speaks the Lite profile below, which carries no bridge token. Do not point an untrusted child at a bridge token.

## Deployment and security defaults

1. Mint a scoped bearer token. Put it in the environment. Never commit it.
2. Bind the reference server on `127.0.0.1` unless a TLS terminator owns the public port.
3. Advertise only the capabilities the agent actually implements. `unsupported` is honest. A false `supported` fails conformance.
4. Keep `maxFrameBytes` and `sendQueue` at the hello limits. Do not raise them to "make it faster."
5. Treat `join_url`-style public handles as out of scope here. This protocol carries `resumeToken`, which is a secret.

Independent host fixture for a non-Python peer:

```
BRIDGE_TOKEN=<token> node sleeptalk/tests/fixtures/bridge_independent_host.mjs session 127.0.0.1 <port>
```

`session` completes one streamed turn. `mismatch` sends `v=2` and expects `hello.err` with `code` `version_mismatch`. `drop` hangs up after `hello.ok` so the reference server can accept a later client.

## Bridge Lite profile

Approved 2026-09-16 as an amendment to the public agent protocol decision.
Implemented in `sleeptalk/agents/bridge_lite.py` and selected with
`type = "command"` or `type = "http"`.

Bridge Lite uses the frame names, fields, key styles, and meanings above. It
removes what a local pipe or a single HTTP request already provides.

| Bridge v1 | Lite over stdio (`command`) | Lite over HTTP (`http`) |
| --- | --- | --- |
| TLS and bearer token | None; the process boundary is the trust boundary | Bearer header from `token_env`; `http://` only on loopback |
| `hello` / `hello.ok` | Host sends `hello` with `"profile":"lite"`; answering is optional | Optional `GET` on the endpoint returns `hello.ok`; a `hello.ok` frame in a reply also counts |
| `resumeToken`, reconnect, replay | None | None; `sessionId` in each prompt identifies the conversation |
| Credit, send queue, heartbeat | None; the pipe applies backpressure | None |
| `id` on agent frames | Optional | Optional |
| `turnId` on agent frames | Optional; frames without it belong to the current turn | Optional |

### Required frames

Host to agent: `turn.prompt`, `turn.abort`. Stdio also sends `hello` at start
and `session.close` at shutdown.

Agent to host: `assistant.delta` or `sentence`, then `turn.completed`,
`turn.aborted`, or `error`. Over HTTP, the end of the response body also ends
the turn.

Lite hosts split `assistant.delta` text into sentences. Agents don't need to
send `sentence`.

`error` ends the turn in Lite. `code` `abort` ends it quietly.

### Optional frames

Everything else in the agent-to-host table, once `hello.ok` advertises the
matching capability. Unadvertised `permission.requested` and
`clarification.requested` frames are ignored, because no host control would
answer them. Other unadvertised event frames are accepted.

Over stdio, `permission.resolve`, `clarification.answer`, `steer`, and
`cancel` are sent when advertised. Agents may reply `control.accepted` or
`control.rejected` with `ref`. Without a reply within two seconds, the host
reports the control as delivered.

Over HTTP there is no host-to-agent channel during a turn. Permissions,
clarification, steering, and targeted cancellation are `unavailable`.
Aborting closes the request.

`voice_session.start` is not part of Lite.

### Abort

Over stdio, the host stops speaking at once and sends `turn.abort`. It then
waits `abort_grace_s` (default 2 seconds) for `turn.aborted`,
`turn.completed`, or `error` for that turn, dropping any other frames. If
none arrives, it restarts the program before the next turn.

### HTTP reply formats

| `Content-Type` | Parsed as |
| --- | --- |
| `application/x-ndjson`, `application/jsonl`, `application/stream+json` | One frame per line; non-JSON lines are text |
| `text/event-stream` | Each event's `data` is a frame or text; `[DONE]` completes the turn |
| `application/json` | A frame, a list of frames, or an object with `text`, `reply`, `output`, `message`, or `content` |
| anything else | Streamed text |

### Security

- Command agents get an environment allowlist: a base set plus names in
  `[agent.options].env`. Speech and Discord credentials never pass, even with
  `inherit_env = true`.
- HTTP URLs cannot carry credentials, and `headers` cannot set
  `Authorization`, `Cookie`, or API-key headers. Use `token_env`.
- A permission request still is not approval, over either transport.
