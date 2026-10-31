# GPT Live browser calls

GPT Live keeps a spoken conversation open while your configured agent handles
files, tools, research, and longer reasoning. It uses a separate implementation
from [GPT Realtime](realtime-hybrid.md), even though both are selected under
the same OpenAI speech service. See [voice modes](voice-modes.md) for the split.

This browser preview runs [background workers](background-workers.md) when
the configured agent has a worker factory and background jobs are enabled.
Otherwise it falls back to one backend task at a time. Automated checks cover
job delegation and cleanup; live microphone and long-call checks remain open.

GPT-Live-1 is available under **OpenAI Realtime → Model** on the settings
page. **GPT Live** is the first and default model for new browser profiles. Retain your configured agent,
and start a new browser call. Voice and language take effect on the next
call. Standalone voice previews are disabled for this model.

For an existing profile with an OpenAI credential reference, the route is:

```toml
[route]
type = "hybrid"
provider = "openai-realtime"
model = "gpt-live-1"
voice = "marin"
language = "en-US"
```

The provider name remains compatible with existing OpenAI profiles. Exact
model selection dispatches to a separate Live implementation. Other models
continue to use the existing Realtime route. Browser calls are the supported
surface; terminal and Discord execution reject this route. Use the normal
browser call link workflow. No running service needs to be restarted for the
automated checks below.

## Who does what

| Responsibility | Owner |
| --- | --- |
| Continuous listening, speech, conversational timing, ordinary explanations | GPT-Live |
| Files, code, tools, private context, current information, long reasoning | Configured agent / specialist |
| Task identity, duplicate suppression, cancellation, stale result rejection, permission replies | Sleeptalk |
| Background workers, concurrency, queueing, progress and completion | Sleeptalk's job manager, using fresh instances of the configured agent |

GPT-Live has no direct tool execution path. Its client delegation event
contains an ID and audio offset, not a finished tool request. Sleeptalk waits
briefly for delayed caller captions, combines the new caller fragments with
bounded conversation history, and sends that context to the existing
agent worker. If the request is still missing, it asks the caller to repeat
instead of inventing a task from the model's own speech.

Delegated work runs through the same background job manager as other calls.
Each worker uses a fresh instance of the configured agent, with the caller's
request, conversation fragments, and known worker results. The configured
`policy.max_background_jobs` limit applies; additional work queues and starts
when a slot opens. Workers appear in the browser's existing worker panel.
The voice session keeps listening and speaking while workers run. Streaming
drafts stay out of the voice context; completion supplies verified results
as Live commentary without starting an unsupported manual speech turn.
When background jobs are disabled or the agent has no worker factory, Live
retains its single-backend path.
The voice instructions require confirmed outcomes before claiming success.
This is a conversational instruction, not a guarantee against model mistakes.

Speech interruption and task cancellation have different lifetimes:

- Speaking over a reply lets GPT-Live decide how to respond. It does not
  automatically abort the specialist.
- The browser's explicit Stop clears local playback and asks Live to stop
  speaking. Late output is dropped until an observed output-audio silence
  boundary. A context acknowledgment is never treated as an audio fence.
- An explicitly delegated “cancel it” or “cancel the task” cancels the
  worker. With multiple possible targets, Sleeptalk confirms which one.
  The worker panel's Cancel button targets its own job directly.
  Cancellation cannot undo completed tool side effects.
- “Keep doing it” leaves the worker running. Additional instructions after
  that phrase, or corrections beginning “actually”, “instead”, “wait”,
  “change that”, or “correction”, reach that worker through its steering
  control or its queued follow-up. Status questions report the known state.
- A new request can start another worker while earlier work continues.
  Sleeptalk no longer repeatedly asks the caller to wait, change, or cancel.
  With workers disabled, corrections still cancel and settle the previous
  backend before a replacement, which receives the prior request and must
  reconcile actions already performed.
- Permission and clarification events stay with the configured adapter.
  Permission replies require an explicit supported answer, such as “allow
  once” or “deny”, from audio after the question was raised. Late captions of
  earlier speech cannot authorize it. Permission phrases and correction
  recognition are English; broader semantic steering is
  follow-up work.

The browser's worker panel continues to show work while jobs run, even when
a spoken segment ends. Tool events use the existing worker presentation.
A failed or disconnected Live session stops running and queued workers and
closes the paid connection; callers start a new call rather than replaying
tasks automatically.

## Audio and protocol

```text
Browser microphone → WebRTC → Sleeptalk input clock → OpenAI Live
Browser speaker    ← WebRTC ← Sleeptalk playback    ← OpenAI Live
                                      ↕ delegation / commentary
                               configured AgentAdapter
```

The trusted host owns the OpenAI WebSocket and credential. The browser never
receives the API key. The host starts `/v1/live/sessions` with `session.start`,
client delegation, and `store: false`, then waits for `session.started`.
Waiting for someone to join a call link does not start a billed Live session.

Input is paced at 20 ms and resampled from browser 48 kHz mono PCM to the
Live 24 kHz PCM format. WebRTC can omit silence through discontinuous
transmission (DTX), so the input clock supplies silence when decoded browser
frames stop arriving. Otherwise the model's clock can stall mid-reply.
Input and output buffers are bounded; overload fails visibly rather than
silently accumulating unlimited latency.

Incoming audio continues during playback and specialist work. The existing
local voice-activity detector, utterance commits, and barge-in cancellation
logic are bypassed for this route. There are no Realtime `response.create`
or audio-commit events. Output is resampled to the browser's existing playback
interface. Local playback segments update the UI; they are not invented
provider response-completion events.

User and assistant captions update independently. Original fragments and
timestamps are retained in session events; late fragments are ordered within
their display segments. Captions may overlap, lag audio, and contain errors.
They are not exact alignment evidence for when particular spoken words stop.

OpenAI bills Live session duration, including silence; the agent has separate
usage. Graceful shutdown sends `session.close` and records final session usage
from `session.closed`. Missing final usage remains marked incomplete. There
is no automatic reconnect or side-effect replay. The usage log records
`liveSessionSeconds` from OpenAI's final receipt; it does not substitute a
local duration or invent a dollar estimate when that receipt is absent.

## Validation and remaining gates

Protocol and integration tests cover continuous input during playback,
DTX silence, delayed/overlapping captions, duplicate delegation, correction
cancellation, explicit permissions, stale results, provider failure,
disconnect cleanup, settings selection, and final usage:

```bash
uv run pytest -q tests/test_openai_live.py tests/test_settings_page.py
uv run python -c "from sleeptalk.cli import main"
uv run pytest -q
(cd rendezvous && npm test && npm run build)
```

A bounded account check uses an existing saved OpenAI key without modifying
the profile or opening a microphone:

```bash
uv run python scripts/probe_openai_live.py --config PATH_TO_PROFILE
```

The synthetic browser harness supports `--speech live --key-profile PATH`.
Use prerecorded clips and `SYNTH_OVERLAP_DELAY_MS=150` to interrupt while audio
is actually playing. `analyze_live.py` checks measured simultaneous audio
and visible answers without requiring old turn-completion events. Its pass
means transport and reply evidence only. It does not prove natural
backchannels, semantic interruption latency, or that a real specialist's
tools did the correct work. A deterministic `--agent fault` run can test the
handoff without giving a production agent work.

Real account startup and prerecorded Chromium calls were exercised during
development. A browser run produced “4”, completed all eight bicycle
explanation sentences despite “mm-hmm”, then answered “Paris” after a spoken
interruption. Measured output audio overlapped the “mm-hmm” clip for 440 ms
and the interruption clip for 2,320 ms. These are overlap observations, not
semantic stop-latency scores. A separate clean call delegated to the fake
specialist and spoke “The backend says two plus two is four.” The browser
Stop-and-resume scenario also returned “4” to the next request.

Human gates remain: real microphone/AEC behavior, subjective
backchannel and interruption quality, permissions with the intended agent,
long-call drift, and any service restart. Terminal and Discord full-duplex
support are future work.

Final verification on October 9, 2026: the full Python suite passed with
3,945 passed, 58 skipped, and one expected failure, including 22 Live tests.
The browser/Worker suites passed all 151 tests, the frontend build passed,
and the CLI import check passed. A final real browser hangup recorded a
confirmed OpenAI receipt of seven session seconds. Live microphone and
production-agent tool behavior were not claimed from these automated runs.

## Follow-up plan

1. Use the browser preview with headphones and the intended agent. Test a
   short answer, “mm-hmm” during speech, an interruption, a correction during
   slow work, a permission request, cancellation, and disconnect.
2. Measure speech latency from recordings and annotate the actual utterances.
   Do not infer semantic interruption success solely from transcript arrival
   times or the model's acknowledgment.
3. Extend steering beyond explicit English phrases with a structured task
   coordinator, preserving job identity and the adapter's permission model.
4. Add reconnect only after specifying reconciliation for in-flight tasks,
   already performed side effects, missing usage, and playback history.
5. Reuse the continuous-input capability for other surfaces after their audio
   clocks and echo handling have dedicated tests and human verification.

Protocol references: [Live WebSockets](https://developers.openai.com/api/docs/guides/voice-websockets),
[migration](https://developers.openai.com/api/docs/guides/live-migration),
[client delegation](https://developers.openai.com/api/docs/guides/live-delegation),
[continuous conversations](https://developers.openai.com/api/docs/guides/live-conversations),
and [server controls](https://developers.openai.com/api/docs/guides/voice-server-controls?api=live).
