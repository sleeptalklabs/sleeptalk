# Component contracts

Sleeptalk assembles a transport, agent, STT provider, and TTS provider through
registrations. A component can live in another installed Python package.
Its implementation, registration, and private option schema are sufficient;
the builder and profile loader do not need a branch for its name.

`build_session(config)` returns the public `RealtimeSession`. Its private
component assembly has no separate lifecycle API. Discord requires this session
for commands, transcription, cancellation, and cleanup. Cascade owns speech
policy through `VoicePolicyRuntime`; transports own capture and playback.

Provider construction goes through `sleeptalk.provider_factory`, which uses the
registries below. Adapter callbacks enter through `typed_events`; speech-policy
activity is normalized by `agent_signal` at that same adapter boundary.

## Registration and discovery

The registries are public imports from `sleeptalk.registries`.

| Kind | Registry | Python entry point group | Factory arguments |
| --- | --- | --- | --- |
| Agent | `AGENTS` | `sleeptalk.agents` | `AgentConfig`, environment mapping |
| Transport | `TRANSPORTS` | `sleeptalk.transports` | `SleeptalkConfig`, environment mapping |
| STT | `STT_PROVIDERS` | `sleeptalk.stt` | `ProviderSpec`, environment mapping |
| TTS | `TTS_PROVIDERS` | `sleeptalk.tts` | `ProviderSpec`, environment mapping |

An entry point names a zero-argument callback that registers its advertised
component name. For example, an extension's `pyproject.toml` can contain:

```toml
[project.entry-points."sleeptalk.agents"]
orchard = "orchard_voice:register"
```

The callback can declare private settings and readiness requirements:

```python
from sleeptalk.registries import AGENTS, ComponentOptions

class OrchardOptions(ComponentOptions):
	endpoint: str

def register():
	AGENTS.register(
		"orchard",
		lambda config, env: OrchardAgent(config.options["endpoint"], env["ORCHARD_KEY"]),
		options_schema=OrchardOptions,
		required_env={"ORCHARD_KEY": "Obtain a key from your Orchard operator."},
	)
```

`OrchardAgent` in this example is the extension's implementation of the
[agent contract](agent-adapter-contract.md). Each entry point callback registers
one advertised component. The selected callback loads lazily and runs once.
Listing names reads package metadata without importing extension modules.
Duplicate advertised names and collisions with registered names or aliases
raise errors. A failed callback rolls back registry changes and can be retried.
An extension cannot replace existing registrations.

A registration can also pass `probe=`, an async function of the config section
and environment that returns `host probe-runtime` fields. It must never send a
prompt or start user work. Built-in bring-your-own types (`command`, `http`,
`bridge`) use it; see [Bring your own agent](bring-your-own-agent.md).

In-process applications can call `register()` before loading a profile.
Installed entry points make the same registration available to the normal CLI.

## Profile settings

Existing common profile fields keep their meaning. New component settings live
under the selected section's `options` table:

```toml
[agent]
type = "orchard"

[agent.options]
endpoint = "https://orchard.example"
```

`ComponentOptions` rejects unknown keys. The profile loader validates required
values and materializes schema defaults before construction. A private schema
cannot reuse common field names such as `model` or `base_url`. Components
without a schema reject nonempty private options. Fallback TTS entries use the
same validation through `[[tts.fallback]]` and `[tts.fallback.options]`.

Agent factories receive normalized private values in `config.options`.
Transport factories receive them in `config.transport.options`. Speech factories
receive them in `ProviderSpec.options`, alongside common provider settings.
Secrets come from the environment mapping and remain outside profile TOML.

Registrations declare common fields they understand with `config_fields`.
They can also declare environment variables, executables, Python dependencies,
and a connectivity check. Agent runtime selection, construction, and doctor
resolve the same registration with the same environment. Built-in runtime
selection policy does not restrict independently registered agent names.

TTS registrations own `provider_defaults` and `normalize_model`. Agent
registrations can supply `conversation_context` for browser continuation.
These hooks keep vendor decisions out of the composition code.

## Runtime boundaries

| Component | Runtime contract | Ownership |
| --- | --- | --- |
| Agent | `warmup`, `respond_streaming`, `respond`, `abort`, `close` | Agent protocol, process, and upstream conversation |
| Transport | `async run(session)` | Capture devices, participant admission, playback, and presentation |
| STT | `STTComponents(rest=..., streaming=...)`, with at least one implementation | Provider request encoding, connections, and transcription |
| TTS | `synthesize(text)` returning an async iterator of PCM chunks | Provider request encoding, decoding, and resampling |

The speech protocols live in `sleeptalk.speech.contracts`. All runtime speech
input and output is signed little-endian int16 PCM, mono, at 48 kHz. WAV and
compressed payloads are decoded at the provider or explicit file-playback
boundary. A transport's route output never detects a file format.

REST STT implements `transcribe_pcm`. Streaming STT implements `append_pcm`,
`finish_transcription`, `turn_completion_signal`, `reset`, and `close`.
Streaming STT is optional. The shared voice policy accepts REST-only providers.
Closing a TTS iterator releases that request. Providers with shared resources
release them through `close`; optional `warmup` preheats the primary provider.

### TTS speed is one operator step, translated per provider

The operator picks one of the steps in `SPEED_STEPS` (`sleeptalk/speech_speed.py`)
and every surface shows that step, for example `1.25x`. Providers accept
different intervals, and some read the number backwards, so the step is
translated once, at the wire, by `wire_speed(provider, speed, model=...)`.

Every TTS registration must own a `SpeedProfile` in `sleeptalk/speech_speed.py`
before it ships. `tests/test_tts_speed.py` fails for any built-in TTS provider
without one. The profile records the accepted interval, whether lower means
faster, and the source of those bounds: the provider's API reference or an
observed rejection. A provider whose interval depends on the model declares a
`name:model` entry beside the bare name. A provider from another package
declares its profile from its `register()` callback with
`declare_speed_profile`.

A missing or too-wide profile is a silent outage. The provider answers 400 on
every sentence and the caller hears nothing. Never send `spec.speed` to a
provider without passing it through `wire_speed` first.

`FallbackTTS` wraps the ordered primary and fallback providers. It retries on
an exception or empty response before any audio has been yielded. It does not
retry after partial audio, consumer cancellation, or output failure. Its cache
identity includes every provider's voice settings and private options. It
closes every provider even if another provider's cleanup fails.

## Session and presentation

`RealtimeSession` owns preparation, semantic turn IDs, serialized turns,
continuations, cancellation, and resource cleanup. Transports use public
methods including `append_audio`, `transcribe`, `run_turn`, `interrupt_presentation`, and
`aclose`. Transport capture generations do not assign semantic turn IDs.

`TurnLifecycle` callbacks associate transport presentation with a shared turn.
Automatic continuations inherit their output, participant context, and
lifecycle callbacks. Interruption, cancellation, new user turns, and closure
invalidate older continuation callbacks.

New turns wait for in-progress interruption cleanup, even when the old turn's
foreground task has already finished. Closure drains that cleanup before
releasing providers. A queued interruption retains its original turn ID.

`AudioOutput.finish_turn` flushes a presentation segment and is idempotent when
no new audio has been written. The session finalizes synchronous turns,
including failures. A detached Hybrid route owns completion of its later
output. `discard_turn` fences stale writes before clearing playback.

Web's registered transport uses the already assembled session. Its private
options are `local`, `rendezvous_url`, and `ttl_seconds`. The default uses a local
rendezvous and a 300-second link lifetime. The existing `sleeptalk link` path
uses the same browser runner.

## Executable evidence

`bash scripts/prove_component_contracts.sh` runs the component and session
contract tests. It includes a profile with four unknown components, a real
turn through those components, and entry point discovery in a fresh process.
It also checks strict options, agent factory selection, ordered fallback,
canonical PCM, continuation fencing, output finalization, and Web cleanup.

These tests establish automated contract behavior. Live provider compatibility
and microphone experience require a live call on a real host.
