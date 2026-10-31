# Live voice responsiveness

Choose **Local** in Speech settings. New local selections default to **Moonshine
Small Streaming** for listening and Kokoro for speaking. Streaming input and the
faster response behavior are standard; there is no experimental toggle.
Explicit Moonshine Tiny selections are preserved. Older profiles containing
`policy.experimental_responsiveness` still load, but that retired boolean is
ignored and omitted from serialized configuration.

## Scope and setup

The faster response policy activates automatically for every web, terminal, and
Discord cascade call, regardless of the listening provider, speaking provider,
or TTS fallback.
This includes Groq, Together, and speaking-only services such as Hume, Rime, and
PlayHT, as well as local and hosted mixtures. OpenAI Realtime and GPT-Live use
their own realtime routes and
native transcript handling; cascade endpointing does not control those routes.
Agent selection is independent of speech providers. Start a new call or terminal
session to load the updated policy; no extra toggle is required.

Terminal renders revisable words in its existing live transcript area. This
requires the interactive terminal display; log-only mode has no live captions.
Discord's optional live captions use the existing `DISCORD_TRANSCRIPT_DEBUG=1`
environment setting. Each utterance gets one message that updates at most once
per second and is finalized with the recognized text. Speakers have separate
messages; edits never block audio, and shutdown cancels pending updates.
As with the existing transcript debug output, protected Discord sessions do not
post captions to the text channel. Realtime/hybrid routes keep their existing
transcript behavior.

Batch preview uploads are enabled on web and terminal and, for Discord, only
when `DISCORD_TRANSCRIPT_DEBUG` is enabled and the session allows text output. Native streaming recognition and
faster response timing work on Discord even with captions off.

For local speech, Settings and the setup wizard install the required runtimes and download the
143 MB streaming model when requested. Manual installation uses
`sleeptalk[local,local-streaming]`. The streaming runtime is pinned to
`moonshine-voice==0.1.5`; its eight model files have pinned sizes and SHA-256
checksums. Local inference never downloads models or sends audio to a service.
A running service needs a human-controlled restart to load updated code.

The pinned runtime supports Apple silicon on macOS 15+ and Linux x86-64/ARM64.
It has no Intel Mac wheel; Moonshine Tiny remains available through sherpa-onnx.
CPU performance on a VPS has not been measured. Local speech is English-only.

## Response behavior

### Snappy

Selecting **Snappy** opts into shorter waits on web, terminal, and Discord
cascade calls. Patient and Balanced keep their existing timings, including
Balanced's automatic batch-listener compensation.

- Smart Turn first checks after 100 ms of fresh-voice silence and checks again
  around 200 ms if needed, instead of 250/500 ms. Inference adds to those times. The confidence floor stays at
  0.8; resumed speech invalidates pending verdicts and floor holders still veto
  early completion.
- With transcript evidence, complete short turns can finish after 180 ms of
  silence (220 ms for longer turns, 350 ms for monologues). Uncertain speech
  waits 650 ms; incomplete clauses wait 1400 ms, or 2000 ms in monologues.
  Explicit silence overrides and the capture minimum still apply to this path.
- Pending streaming text is checked every 50 ms. Moonshine's completed-segment
  caps become 220 ms for apparently complete text and 650 ms for incomplete
  clauses, with the same capture minimum and floor-holder protection.
- The response starts in smaller phrases: around 24 characters for the opening
  and 64 thereafter, versus the usual 64/140. Word and safe speech boundaries
  remain intact. These limits are local to the responding task.
- Canned opening acknowledgements and their prewarming are skipped. They cannot
  occupy the audio lane ahead of a ready answer. Agent speech, tool progress,
  permission questions, and long-running task updates still work.
- Isolated, echo-controlled audio yields earlier: duck after 50 ms of detected
  speech, interrupt after 100 ms, and release a duck after 250 ms of quiet.
  Initial grace and sentence-gap hold are capped at 50 ms, and interruption
  cooldown at 300 ms. Detector window sizes add granularity to these targets.
  Soft mode still resumes on backchannels; disabled interruptions remain off.
  Mixed speaker/microphone audio keeps its existing echo protection.

When a profile selects a different turn-taking preset from the shared base,
it replaces the base's endpoint timing overrides. Overrides written in that
profile still win, and non-timing controls such as disabling adaptive
endpointing remain inherited. This prevents a shared Balanced baseline from
silently overriding Snappy (or Patient); no saved profile is rewritten.

This reduces pauses and overlaps agent generation with speech output. It is
still a cascade: an agent turn starts from committed speech, not a guessed
partial transcript, and there is no guarantee of simultaneous listening and
speaking on every transport. More aggressive turn completion can cut into
thinking pauses; smaller TTS phrases can affect prosody and request count,
and earlier semantic checks can spend more local inference work on short pauses.
Actual audible latency still depends on STT, agent, TTS, and network speed.

### Shared behavior

The timings below describe Patient and Balanced unless noted above.

- **Local streaming input:** a warm CPU recognizer consumes audio on one native worker.
  Revisable partial text reaches the live display while the person speaks; only the
  final transcript starts an agent turn. A bounded three-second input queue
  falls back to local utterance recovery if inference cannot keep up. New
  captures and cancellation reject late results from old captures.
- **Cartesia streaming input:** live PCM goes to Cartesia's streaming recognizer
  over a WebSocket. Revisable partial transcripts appear in the live display as they
  arrive and clear when the final user transcript is committed. Explicit
  finalization remains in use; the Moonshine-specific completed-segment fallback
  below applies only to local streaming input. Cartesia also uses the shared
  silence timing and Smart Turn checks described below.
- **ElevenLabs streaming input:** the existing **Scribe v2** selection uses
  Scribe v2 Realtime with live, revisable transcripts. Explicit Scribe v1
  selections use batch recognition with the bounded previews described below.
  Sleeptalk owns turn completion and sends one manual commit at the end of a capture; provider segment commits do not start agent
  turns. Audio is sent in 100 ms chunks. Short captures are padded to two seconds
  at finalization so Scribe can process them. Each capture closes its socket on
  completion, failure, or cancellation to isolate late results. Failed streams
  use the full captured utterance through the existing Scribe v2 REST fallback.
  First visible words follow the provider's buffering and network timing.
- **Deepgram streaming input:** Nova-2 and Nova-3 use the existing live audio
  adapter. Interim revisions appear in the live display and clear when the final user
  transcript is committed. Stable segments and Deepgram's speech-final signal
  retain their completion thresholds; the shared fresh-voice clock and later
  Smart Turn check apply. The display does not commit turns.
- **Batch transcript previews:** OpenRouter, Venice, Groq, Together, explicit
  ElevenLabs Scribe v1, and local Moonshine Tiny remain batch listeners.
  Web and terminal calls, and Discord with captions enabled, display revisable
  snapshots while the caller speaks: the first
  request follows about two seconds of captured audio, with later snapshots at
  least two seconds apart. Each preview replaces the displayed text; no chunk
  stitching is used. Only one preview request per participant is in flight, with
  at most six requests and 30 seconds of buffered audio per capture. Silence
  alone does not trigger uploads. Errors or a four-second timeout stop previews
  for that capture, and cancellation/reset fences reject late results. These
  extra hosted uploads incur additional transcription cost; local previews use
  additional CPU. Local native decoding is serialized so a cancelled preview
  worker cannot overlap final recognition. Preview text never affects
  endpointing or becomes an agent message; the full utterance still uses normal
  batch transcription and pause-ahead checks. Long turns can outlast the preview
  budget; their complete final transcript still comes from the full recording.
- **Silence timing:** all live cascade listeners measure silence from the
  last fresh voice. VAD wind-down audio stays in the recording but cannot
  restart the endpoint timer. Fresh voice resets the deadline.
- **Early turn completion:** Smart Turn checks after 250 ms without fresh voice.
  It requires at least 0.8 confidence, or the configured threshold if higher.
  Every live cascade listener gets one later check at 500 ms if the first
  result is inconclusive. New speech invalidates pending results. Text floor-holder vetoes and normal
  endpoint fallback remain active.
- **Streaming endpoint fallback:** completed Moonshine segments cap silence at
  600 ms for apparently complete text and 1200 ms for incomplete clauses,
  subject to the configured minimum and floor-holder protection. Timers subtract
  elapsed silence from the last fresh voice window: Silero STOPPING frames stay
  in captured audio but cannot postpone the deadline. Fresh voice resets it.
- **Faster interruption (web):** the first isolated-audio grace is at most 100 ms and
  does not repeat when playback restarts; drain hold is capped at 100 ms. The
  separate barge detector uses 32 ms attack and 120 ms hangover, counting fresh
  speech. Playback pauses reversibly after 80 ms of qualified evidence. Existing
  sustain, loudness, cooldown, and interruption-mode settings remain active;
  disabling interruption also disables ducking.
- **Terminal and Discord interruption:** isolated audio uses the shorter first
  grace and drain hold; Discord can duck after 80 ms of qualified evidence.
  Terminal speaker echo protection remains active, and terminal still requires
  `SLEEPTALK_TERMINAL_FULL_DUPLEX=1` for headphone/AEC barge-in capture. Terminal
  playback cannot pause/resume for soft ducking. Existing detector thresholds,
  per-speaker ownership, and interruption-off settings remain active.
- **Earlier output:** task-local phrase streaming releases an opening at a word
  boundary around 64 characters, then fragments around 140 characters. Sentence
  and clause boundaries take precedence and code fences remain hidden. Local
  TTS synthesizes each phrase as a batch and prefetches the next phrase. Cartesia
  streams PCM for each phrase over HTTP so playback can start before that phrase
  finishes synthesizing. ElevenLabs TTS still synthesizes each phrase as a batch;
  its phrases can start before the agent finishes the full response.
- **Batch alternative:** for Moonshine Tiny, pause-ahead transcription starts at
  220 ms instead of 300 ms, preserving an explicit disable or shorter setting.

This uses scheduling ideas from Unmute, not Kyutai's STT/TTS models. Phrase
splits can affect prosody, and earlier endpoint decisions can cut off thinking
pauses. The selected agent's generation time and local TTS startup still follow
the endpoint decision. Hosted speech also depends on network and synthesis
latency; the local measurements below do not measure hosted performance.

## Verification

Latency traces retain the historical name `localResponsiveness` for all live
cascade calls. Smart Turn events use
`local_early_pause` or `local_pause_recheck` and include confidence, attempt,
inference time, silence, stale status, and whether the result committed.
`endpointSilenceMs` for live cascade calls uses the fresh-voice clock. Browser playback
stop telemetry measures host output clearing, not the last sample heard on the
device. Automated tests cover timing, stale-result fences, resumed speech,
streaming delivery, setup defaults, and old-profile compatibility. Live
microphone, listening, and service restarts remain human gates.

### Prerecorded checks on this Mac

On an M2 Pro / 16 GiB host, warmed recognition paced at 20 ms per audio frame:

| Clip | Duration | First provisional text | First recognizable phrase | Final flush |
| --- | ---: | ---: | ---: | ---: |
| Synthetic English request | 7.96 s | 481 ms | 901 ms ("I want the") | 1 ms |
| Moonshine's human-read *A Tale of Two Cities* fixture | 44.37 s | 1660 ms | 1880 ms ("It was.") | 6 ms |

Times start at the **file's beginning**, including any leading silence, not the
first spoken sample. The first provisional text in both clips was an incorrect
"Yeah"; text was revised as more audio arrived. The synthetic result retained
the intended sentence but appended a spurious "1.". The human recording also
had word substitutions. Streaming feasibility is established, not recognition
accuracy across accents or noisy rooms.

The replay adds one second of real trailing silence before calling finish, so
the 1–6 ms flush measures only the remaining work. It is **not** the complete
speech-to-final delay. The synthetic segment became complete at 8401 ms, about
440 ms after the audio file ended. None of these numbers measure browser paint,
agent latency, answer audio, or live barge-in.

Reproduce with an installed model and a mono signed 16-bit PCM WAV:

```bash
uv run --extra local --extra local-streaming python scripts/benchmark_local_streaming.py clip.wav --output result.json
```

The script records every partial and segment-completion time. The browser's
`input_partial` telemetry records text length and source without storing the
partial text. A useful live comparison should separately measure first visible
words, last speech to committed transcript, and last speech to answer audio.
Live microphone, listening, and service restarts remain human gates.

Sources: [ElevenLabs realtime API](https://elevenlabs.io/docs/api-reference/speech-to-text/v-1-speech-to-text-realtime),
[ElevenLabs commit strategies](https://elevenlabs.io/docs/eleven-api/guides/how-to/speech-to-text/realtime/transcripts-and-commit-strategies),
[Moonshine streaming runtime](https://github.com/moonshine-ai/moonshine),
[human-speech fixture](https://github.com/moonshine-ai/moonshine/blob/main/test-assets/two_cities.wav),
[Unmute](https://github.com/kyutai-labs/unmute),
[Kyutai STT](https://kyutai.org/stt/).
