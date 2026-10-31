# Docs

Start with the root [README](../README.md). This folder holds the rest.

Sleeptalk is built for ongoing voice conversations while agents carry out
long-running work. Start with [voice modes](voice-modes.md) to choose how the
conversation works, then [background workers](background-workers.md) to keep
talking while tasks run.

## Use Sleeptalk

- [Set up through an agent](agent-setup.md). Install from a clean host, keep the intended workspace, choose a call device, and enter credentials privately.
- [First-call defaults and troubleshooting](first-call-defaults.md). Quiet setup, the full voice picker, concise speech with complete text, and opt-in performance recording.
- [Voice modes](voice-modes.md). Cascade and Local speech, GPT Realtime, and the separate GPT Live browser preview.
- [Background workers](background-workers.md). Run several tasks during a call, ask for progress, steer a named job, and hear the result.
- [OpenAI Realtime](realtime-hybrid.md). OpenAI handles voice and delegates specialist work to your agent.
- [GPT Live](gpt-live.md). Continuous browser conversation with a separate specialist backend; preview limits and controls.
- [Support matrix](launch-support-matrix.md). What is supported, preview, and out of scope.
- [Discord setup](discord.md). The bot token, permissions, intents, and who can talk to it.
- [Deployment](deployment.md). Keep a bot alive on a host with launchd, systemd, or Docker.
- [Calls over your tailnet](tailnet-calls.md). Tailscale instead of the hosted relay: no relay, no TURN, nothing billed.
- [Voice from a key you already have](reuse-your-key.md). OpenRouter, Venice, Groq, or Together as the speech provider; `host ensure` offers a compatible key and asks before using it for voice.
- [Where your keys live](your-keys.md). No account, no server: keys are checked with their provider and kept in a private store on the host, and what each key page does with them.
- [Voices](voices.md). Curated voices per provider, custom IDs, and changing the voice during a call.
- [Local speech](local-speech.md). No speech key: streaming Moonshine with Kokoro, Piper, or Pocket TTS on your machine; use a local model too for an offline call.
- [Just a model](model-only.md). One API key or a local Ollama, no agent.
- [Multi-participant rooms](multi-participant-rooms.md). Discord rooms with more than one human or bot.
- [Voice UX standard](voice-ux-standard.md). The shipped conversation rules and the fixtures that lock them.
- [Voice speech patterns](voice-speech-patterns.md). Wake words, stop words, and pronunciation rules.

## Connect an agent

- [Bring your own agent](bring-your-own-agent.md). Connect another agent: one that speaks ACP works with its command; anything else needs a small adapter (a command, a web endpoint, a remote bridge, or a plugin). Check it with `sleeptalk adapter test`.
- [Agent bridge protocol](agent-bridge-protocol.md). The wire protocol for a non-Python remote agent.
- [Custom agent](custom-agent.md). In-process Python adapter walkthrough.
- [Agent adapter contract](agent-adapter-contract.md). What every adapter must implement.
- [Agent conformance](agent-conformance.md). Opt a vendor adapter into the conformance suite.
- [Harness upgrade policy](harness-upgrades.md). Support existing versions, qualify upgrades, and track the rollout for every built-in harness.
- [Adding a built-in agent](adding-an-agent.md). Maintainer notes for first-party adapters.
- [Component contracts](component-contracts.md). Register transports, agents, STT, and TTS.

## Operate the browser-link service

- [Browser-link service operations](browser-service-operations.md). Running the Rendezvous Worker.
- [Caller authentication threat model](caller-auth-threat-model.md).
- [Release checklist](release-checklist.md). The one-time launch settings, the QA scenarios for every release, and the tagging steps.

## Decisions and research

- [Architecture decision records](adr/).
