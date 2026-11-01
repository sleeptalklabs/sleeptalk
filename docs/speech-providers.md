# Speech providers

Choose how the conversation works in [Voice modes](voice-modes.md), then
choose the service that supplies speech. These are different choices:

- **Conversation style:** your agent supplies the answers (Cascade), or a
  voice model talks with you and hands specialist work to your agent (Hybrid).
- **Provider:** the account/API you connect, or Local on your host.
- **Model and voice:** the listening model, speaking model, and voice offered
  through that provider. A model can be available through several providers.

Browser, terminal, and Discord are call surfaces. Local, tailnet, and hosted
links are browser-delivery choices. See the [support matrix](launch-support-matrix.md)
for the combinations available.

## Services offered in setup

The table describes Sleeptalk's integrations, not every capability a provider
may sell. Model examples come from the shipped catalog; availability can vary
by account and change upstream. The settings page lists model and voice choices
for the selected service.

| Provider | Configuration ID | Listening in Sleeptalk | Speaking in Sleeptalk |
| --- | --- | --- | --- |
| Cartesia | `cartesia` | Ink | Sonic |
| Deepgram | `deepgram` | Nova | Flux and Aura-2 voices |
| ElevenLabs | `elevenlabs` | Scribe | ElevenLabs speech models and voices |
| OpenRouter | `openrouter` | Parakeet, Whisper, Scribe, and other catalog models | Grok Voice, ElevenLabs, Deepgram, Kokoro, Orpheus, and other catalog models |
| Venice | `venice` | Parakeet, Whisper, and other catalog models | Grok Voice, Kokoro, ElevenLabs, and other catalog models |
| Together | `together` | Parakeet or Whisper | Cartesia Sonic, Orpheus, or Kokoro |
| Groq | `groq` | Whisper | Orpheus |
| Local | `local` | Moonshine Small Streaming or Tiny | Kokoro, Piper, or Kyutai Pocket TTS |
| OpenAI | `openai-realtime` on the Hybrid route | The selected voice model hears the audio | GPT Realtime or GPT Live speaks and delegates work to your agent |

The first eight services are Cascade choices. A single provider can cover both
listening and speaking, or a profile can use different providers under `[stt]`
and `[tts]`. Separate cloud providers need their own credentials. Setup's shared
speech-service picker uses one provider for both directions.

Local needs no speech API key, but does need compatible runtimes and model
downloads. It is English-only. Text still goes to your selected agent; choose
a local model and a same-machine call for offline operation after setup.
See [Local speech](local-speech.md), including the Pocket model bundle's license
notes.

OpenAI is a separate conversation path. Under **OpenAI Realtime → Model**,
GPT Realtime runs one specialist task at a time. GPT Live is a browser-only
preview and can use background workers on eligible agents. These use an OpenAI
API credential; they do not select an OpenAI coding agent for you. See
[Realtime](realtime-hybrid.md) and [GPT Live](gpt-live.md).

## Provider accounts versus model makers

If you select Grok Voice through OpenRouter, Sleeptalk calls OpenRouter with
your OpenRouter key. That is different from a direct xAI integration. Likewise,
ElevenLabs or Cartesia models offered through another service use that service's
API, available models, voices, and billing. A direct provider account's custom
voice IDs may not be available through an aggregator.

A model maker appearing in the catalog does not automatically become another
provider card. The catalog includes additional models from vendors such as
Microsoft, NVIDIA, Qwen, MiniMax, and others through the providers above.
See [using an existing key](reuse-your-key.md) and [Voices](voices.md).

## Additional speech-output adapters

These adapters exist in the package registry but are outside the standard
speech-service picker. They are preview or legacy paths configured in `[tts]`,
and need a separate `[stt]` listening provider. Registration is not proof of
current service access or a live-tested call.

| Adapter | Configuration ID | Scope and limits |
| --- | --- | --- |
| Rime | `rime` | Speech output through the registered HTTP adapter; current live compatibility is unverified. This does not add Rime speech recognition or its voice-agent products to Sleeptalk. |
| Hume Octave | `hume` (alias `hume-octave`) | Speech output through the Hume TTS API. Upstream access ends November 13, 2026; see the notice below. |
| PlayHT / PlayAI | `playht` (alias `playai`) | Legacy speech-output adapter for `api.play.ht`; requires an API key, user ID, and voice ID. Current service availability is unverified. |

Hume's [official TTS documentation](https://dev.hume.ai/voice/docs/text-to-speech-tts/overview)
announces that TTS and EVI API access ends **November 13, 2026 at 12:01 a.m. EST**
(checked October 31, 2026). Hume is therefore not a durable choice for a new
Sleeptalk setup. The retained adapter is documented here for existing profiles.

Keep credentials in the [private credential store](your-keys.md), with references
in profiles. Custom speech plugins can extend the built-in inventory through
[component contracts](component-contracts.md); their installation does not make
them part of this built-in list.

## Models, voices, and costs

Choose a model that covers your language, then a voice supported by that model.
The [voice guide](voices.md) covers curated voices, custom IDs, and cloning.
Cloud speech usage and agent-model usage are separate charges. Check the
provider's current plan and model access; free-tier and pricing claims are not
part of this inventory.

This inventory does not change the 0.1.0 release's
[unverified human-check record](release-checklist.md#010-release-decision-2026-10-31).
