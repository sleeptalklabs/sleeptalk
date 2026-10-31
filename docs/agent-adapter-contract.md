# Agent adapter contract

## Implement AgentAdapter to be talkable

An agent is talkable when it streams speakable sentences and can be aborted while idle. Implement `AgentAdapter` in `sleeptalk/adapter.py`. `respond_streaming` yields sentences as they complete. `abort` is safe when nothing is running. Call `warmup` at boot and `close` at shutdown.

Do not add extra required methods. Voice already calls this Protocol.

## Legacy string on_event names

`on_event` stays `Callable[[str], None]`. Names voice already understands:

- `thinking`
- `text`
- `tool`
- `tool:<name>`
- `tooldetail:<detail>`
- `workbench:<json>`

Voice ignores unknown names. Do not invent a speakable string for permission or clarification.

## Versioned manifest and how to advertise capabilities

`CONTRACT_VERSION` is 1. Set `agent_capabilities` to an `AgentCapabilityManifest` on the instance. `adapter_capabilities(agent)` returns that object when the attribute is a manifest. Otherwise it returns `AgentCapabilityManifest.legacy_floor()`.

The floor is talkable only. Every named field is `SupportLevel.UNSUPPORTED`. Legacy adapters do not claim rich verbs.

## Three levels including voice_session_init

`SupportLevel` has three values. `unsupported` means the adapter does not implement the operation. `supported` means it can do the operation now, and it forbids a reason. `unavailable` means the adapter knows the operation and cannot complete it in this session. A reason is required.

`voice_sessions` uses the same three levels. That field is how an agent advertises `voice_session_init`. When the level is `supported`, `kinds` lists `web_link` and `discord_channel` as needed. Callers send `StartVoiceSession` with `WebVoiceTarget` or `DiscordVoiceTarget`.

## Permission and clarification never become approval

`PermissionRequested` has no `allowed` or `approved` field. `ResolvePermission.decision` has no default. A missing UI, a timeout, or a clarification answer is not approval.

`ClarificationRequested` carries a prompt. The answer is a separate `AnswerClarification`.

## Voice policy owns preambles, VAD, barge-in, and keepalive

Adapters report facts. The shared voice runtime owns preambles, VAD, barge-in presentation, progress narration, and keepalive. Do not reimplement those in an adapter.

## Existing Pi, Hermes, Claude, and gateway adapters implement nothing new

`PiRPCClient`, `HermesACPClient`, `ClaudeCodeClient`, and `HermesGatewayClient` stay as they are. They have no `agent_capabilities` attribute, so `adapter_capabilities` returns the legacy floor. They have no `control` method, so `adapter_control` returns `ControlRejected`.

## String shim. `publish_event` and `typed_events`

`publish_event(sink, event)` delivers a typed event to a `typed_events` wrapper. For a string callback it sends `event_to_legacy(event)` when that value is a string. Permission and clarification map to `None` and are omitted. They are never encoded as `thinking`.

`typed_events(callback)` wraps a typed consumer. The wrapper accepts `AdapterEvent` or `str`. A string is parsed with `event_from_legacy`.

`event_from_legacy` maps `thinking`, `text`, `tool`, `tool:<name>`, `tooldetail:<detail>`, and `workbench:<json>`. Everything else becomes `LegacyAgentEvent`.
