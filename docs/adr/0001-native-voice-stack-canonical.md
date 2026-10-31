# ADR 0001: Native Discord voice stack is canonical

Date: 2026-08-13
Status: Accepted

## Context

The repo contains three Discord voice stacks:

- **Native** (`app/pi_discord_bot.py` + `app/discord_native/*` + `app/stream_tts.py`):
  discord.py + `voice_recv`, our own VAD/endpointing/turn-taking, streaming STT
  (Deepgram) + streaming TTS (Cartesia), sentence-at-a-time agent streaming.
- **Realtime hybrid** (`app/realtime_hybrid_discord_bot.py`, `app/discord_realtime/*`):
  speech-to-speech via OpenAI Realtime.
- **Pipecat** (`app/pipecat_bot.py`): framework pipeline.

A second call transport (phone/SIP first) is a product goal. The architecture
audit (2026-08) classified the native stack as verdict C — Discord-coupled at
the session/transport shell, platform-neutral at the agent/STT/TTS core — and
proposed a staged extraction (provider/connection/media layering, canonical
PCM at 16 kHz mono, lifecycle state machine, epoch-scoped playback).

## Decision

1. The **native stack is canonical**. All new voice work — including the
   multi-transport extraction — targets it.
2. The **realtime hybrid and Pipecat stacks are frozen**: no new features, no
   refactors, bug-fixes only if trivial. They are not deleted yet (reference
   value), but nothing may depend on them for new work.
3. Multi-transport work proceeds as the staged PR sequence agreed in the
   audit review:
   - PR 1: characterization tests around existing native behavior (no
     production changes).
   - PR 2: extract utterance assembly (VAD/endpointing/buffering) from the
     Discord sink; keep preroll history and any realtime processing queue as
     separate structures.
   - PR 3: lifecycle state machine + epoch-scoped response playback behind
     the Discord implementation.
   - PR 4: in-memory test adapter.
   - PR 5: phone/SIP adapter to prove the abstraction.

## Consequences

- `voice_adapter.py`, `realtime_hybrid_discord_bot.py`, `pipecat_bot.py`, and
  `discord_realtime/` are not reference architectures for new code.
- New abstractions land in `app/calls/` (platform-neutral) and must not import
  `discord`.
- Frozen stacks may be deleted in a future cleanup once the extraction is
  proven; until then their tests keep running.
