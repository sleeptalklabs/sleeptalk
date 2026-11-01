# Support matrix

Public docs, wizard copy, CLI help, and release checks must match this table.

## Supported

| Axis | Value |
|---|---|
| OS | current macOS, current Ubuntu |
| Python | 3.11+ |
| Surfaces | Web, Terminal, Discord |
| Web delivery | Hosted relay (proven against production 2026-09-20), local browser, or your tailnet with `--tailnet` (proven Mac to iPhone and Ubuntu VPS to iPhone, including after a VPS reboot, 2026-09-20). Every start receipt names its `delivery` |
| Route | Cascade (speech-to-text, agent, text-to-speech), or OpenAI Realtime (the `hybrid` route: one OpenAI key; Realtime hears and speaks, starts its reply before the transcript arrives, and hands work to the agent). Realtime proven live in terminal calls on macOS and browser calls from an iPhone to an Ubuntu VPS, tailnet and relay, 2026-09-25. See [OpenAI Realtime](realtime-hybrid.md) |
| Key entry | The setup wizard's masked prompt, the private local page, or a single-use phone link; both pages offer every speech provider with its free usage and card requirement |
| Setup by an agent | `sleeptalk host ensure` (offers a key already on the host; `--reuse-key` uses it once the user says yes), `host input link` (browser calls and Discord bots, speech key reused with `--speech-from-profile`), `host start-voice`; keys never pass through the agent |
| Speech via an aggregator | `provider = "openrouter"`, `"venice"`, `"groq"`, or `"together"` for both listening and speaking with one key (defaults: Parakeet listening, Grok Voice or Cartesia Sonic speaking); batch listening with tightened endpointing and kept-alive connections, so a warm turn listens and starts speaking in about a third of a second each. OpenRouter proven live 2026-09-22. See [Voice from a key you already have](reuse-your-key.md) |
| Key reuse | `host ensure` finds a speech-capable key in Sleeptalk's store, the environment, Hermes's `.env` and `config.yaml`, OpenClaw's `.env` and `openclaw.json`, or pi's `auth.json` and `models.json`, and sets voice up from it; never a project's `.env` |
| Voices | A curated default and picks per provider, any library ID, changeable from the call page and saved to the profile. See [Voices](voices.md) |
| Language | `en-US` by default; other languages follow the profile `language` setting |
| Agent families | pi, Hermes, Cursor, Codex, OpenClaw, Claude Code |
| Local speech | STT and TTS `provider = "local"`: Moonshine Small Streaming by default, Piper/Kokoro/Pocket TTS for voice, English, no speech key, models downloaded during setup. See [Local speech](local-speech.md) |
| Just a model (no agent) | `type = "model"`: OpenRouter, OpenAI, Anthropic, Ollama, or any OpenAI-compatible endpoint; one API key or none for a local server; no tools. See [Just a model](model-only.md) |
| Background workers | Eligible Cascade agents, including with Local speech: fresh agent sessions keep work running while the conversation continues; three concurrent jobs by default, more queue. Jobs end with the call. “Just a model” and command adapters have no worker factory. See [background workers](background-workers.md) |
| Install | `python -m pip install sleeptalk` from PyPI in a Python 3.11+ environment, the GitHub Release wheel bundle with its installer and dependency constraints, or `sh scripts/install.sh` from a checkout. See the [README](../README.md#get-started) |

Supported means the adapter or surface is registered, covered by automated
tests, and used as a product path. Choose the workspace, adapter, and surface
explicitly. Agents that set up Sleeptalk on a user's behalf must never ask for
speech keys in chat or write secret answers files; use the private browser
input page instead.

### Bring your own agent (community, preview)

`type = "command"`, `"http"`, and `"bridge"` and installed plugin adapters are
community-supported. Sleeptalk supports the contract, the scaffolds, and
`sleeptalk adapter test`; it does not certify a user's agent. See
[Bring your own agent](bring-your-own-agent.md).

BYOA means a harness can connect through a documented interface without a
Sleeptalk fork or mandatory SDK. It does not promise automatic compatibility
with every interactive CLI, attachment to an existing UI conversation, or
every optional capability. The guide lists [what the user needs](bring-your-own-agent.md#what-you-need)
and [integration checks](bring-your-own-agent.md#check-your-integration).
The [BYOA v1 readiness gate](release-checklist.md#byoa-v1-readiness-gate)
must be completed before promoting custom integration paths out of preview.

## Preview

GPT Live (`gpt-live-1`): a separate continuous-audio browser implementation
selected under the same OpenAI speech service. Eligible agents can run
background workers; other configurations use one backend task at a time.
Terminal and Discord reject it. Live microphone,
speaker echo, interruption feel, and long-call checks remain open. See
[GPT Live](gpt-live.md) and [voice modes](voice-modes.md).

OpenAI Realtime in a Discord voice channel: the route runs there, but it has
not been checked live. See [OpenAI Realtime](realtime-hybrid.md).

Any other adapter, route experiment, language tag, or OS is Preview. Preview
code is not described as Supported in the README, site, or wizard copy.

## Not in scope

Accounts, billing, credits, provider resale, a single Sleeptalk key, Windows
as Supported, native mobile or desktop apps, telephony, multi-user rooms.
