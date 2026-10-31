# First-call defaults and troubleshooting

New browser profiles ask for an agent and workspace, speech service, and voice,
then review and start a call. Every available voice remains selectable. Language,
speech-model choices, and alternate call destinations are available without
adding required setup screens. Keys are entered privately; existing-key reuse
still requires the user's choice.

The defaults are browser delivery, Balanced turn taking, quiet tool narration,
and background workers on eligible routes (three concurrent jobs, more queue).
Microphone interruption stops presentation; cancelling a worker is a separate
action. Captions and worker results remain available during the call.

## Concise speech with complete text

New profiles use `policy.reply_length = "concise"`. On Cascade, the agent is
asked to lead with the outcome and still provide its full written result.
Speech reads the opening and an extractive closing line from long replies;
the full answer stays expandable in the call. Original code and links are
retained when the adapter reports its original text. No extra summary model
request is made. This is not a separate semantic rewrite of the whole answer.

The existing **Full** option speaks the complete reply. **Short answers**
(`summarized`) asks the agent to write less. Existing profiles keep their chosen
reply length; unknown values still fall back to Full.

New Cascade profiles use `instant_ack_mode = "delayed"`: quick answers get no
canned opening; an answer that is still pending after 2.5 seconds can receive
one acknowledgement. Handoffs, permission questions, and completion notices
remain audible. Live OpenAI routes own their conversational timing.

## Help and performance recording

The call screen's **Help** contains **Connection details** and an explicit
**Record performance diagnostics for this call** checkbox. Changing it affects
the current call only; the saved profile preference is restored when the call
ends. There is no automatic support upload.

Detailed performance traces default to off. Off is enforced at the write
boundary, including when a trace path or stdout probe is inherited from the
environment. Ordinary timing traces exclude text, transcripts, audio, tool
arguments, and free-form error bodies. Trace files are owner-only and rotate
at 1 MB with one backup. An explicit `VOICE_LATENCY_LOG_PATH` remains a developer
opt-in when no profile has selected Off.

For ongoing investigation, **Settings → Behavior → Advanced troubleshooting →
Performance traces** saves `log.latency` for future calls. Basic redacted
warnings and error logs remain available independently; their default level
is `WARNING`, with a 1 MB file and one backup. Explicit verbose levels remain
supported.

Transcript debugging (`SLEEPTALK_TRANSCRIPT_DEBUG`) and the session event probe
(`SLEEPTALK_SESSION_EVENT_LOG`) remain separate developer opt-ins. Performance
recording does not enable either. These preferences govern Sleeptalk's
diagnostics, not the selected agent's own conversation history or a provider's
data handling.
