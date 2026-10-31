# Voice UX standard

This is the product-complete voice interaction standard for Sleeptalk. It describes the operator-tuned behavior that already ships. It is not a redesign and it is not a service-level promise.

The versioned base is the tracked root `sleeptalk.example.toml`. Copy it to `sleeptalk.toml` for a local overlay (`guild_id`, `opus_library`, `voice_id`). A profile overlays only the keys it declares. Schema defaults in `sleeptalk/config.py` apply only when the root file omits a field. Empty `preamble_pools`, `reassure_tool_pools`, and `narration_phrases` resolve to the code constants in `sleeptalk/policy.py`.

This document is a Diátaxis reference. Thresholds live in the field inventory. Human conversation judgments live in their own section and are not automated.

## Baseline receipt

| Item | Value |
|---|---|
| Config identity commit | `9ad5b40246c81f42a5ecc66a3d7f689869d3f565` |
| Recorded conversation tag | `golden-voice-95ffb75` |
| Recorded conversation commit | `95ffb75ea43193c2dc6a8dd1eaa86cce2f975c5e` |
| `sleeptalk.example.toml` SHA-256 | `5eb2ab24bd86ae07ff67d578f3a4013a46ea4c51cba75ba2f559ddb7b9fb2b63` |
| `config/profiles/example.toml` SHA-256 | `8e0a3618e843e1eadcd6fc7bfacd9e2a67daebf1f234fa63c1f9ec31642f8c5b` |

The receipt is also stored at `sleeptalk/tests/fixtures/voice_ux/baseline-receipt.json`. A later retune needs a versioned root diff, a new receipt, automated evidence, and Human QA. Substituting a schema default for a root-tuned value fails `tests/test_voice_ux_standard.py`.

## Authority of a value

| Source | Meaning |
|---|---|
| root-tuned | Present in root TOML and different from the schema default. Operator-chosen product behavior. |
| root-pinned | Present in root TOML at the schema value. The pin is the standard. Dropping the key would hide a later default change. |
| schema-live | Omitted from root TOML. The schema default is live product behavior and must stay named. |
| code-owned | Not a TOML leaf, or an empty TOML map that falls through to `policy.py` / `voice_policy_runtime.py` constants. |

`policy.resume_after_interrupt` and `policy.resume_offer_text` still parse. The proactive resume offer was removed on 2026-08-14. Interrupted context remains, and a user can ask to continue.

`VoicePolicy.effective_filler_delay_ms` uses `policy.first_token_budget_ms` when that field is greater than 0. The pinned `policy.filler_delay_ms` of 1200 does not win while the budget is 800.

Kernel Discord, operator WebRTC, and terminal read `VoicePolicyRuntime`, `UtteranceAssembler`, and `Floor`. `VoiceAgentProcessor` still reads the debounce, barge-char, and reply-cap pins. Those pins stay in the inventory so a later default change cannot hide. They are not kernel law.

## Session lifecycle

Requirement **VUX-LS-001**. The public session contract is `RealtimeSession` in `sleeptalk/session.py`, with states from `SessionState` in `sleeptalk/session_events.py`.

Legal transitions:

```
created -> connecting
connecting -> active
connecting -> closed
active -> recovering
active -> closing
recovering -> active
recovering -> closed
closing -> closed
```

Every transition emits `session.state`. `connect()` jumps `created` to `active` without parking on `connecting`. The legal graph still includes `connecting`. `RealtimeSession.recover()` has no production caller. `cancel` ends the current turn only. The session stays `active`.

## Conversation states

Requirement **VUX-LS-001** also names conversation vocabulary. The session stays `active` for the whole talk. Listening and speaking are surface readings of turn events, not `SessionState`.

Three shipped maps exist. They are exceptions when they disagree, not a second standard.

| Surface | Vocabulary | Driven by |
|---|---|---|
| Terminal being | `idle`, `listening`, `thinking`, `speaking` | Session events plus speaker-queue PCM |
| Operator WebRTC call | `working`, `answering`, `idle`, `listening` | `WebCallSession._on_session_event` |
| QA browser harness | `idle`, `connecting`, `listening`, `user-speaking`, `thinking`, `bot-speaking`, `error` | Daily, LiveKit, or OpenAI callbacks, not `SessionEvent` |

Discord kernel emits onto the session log and does not subscribe. Operator Discord UX is acoustic plus `bot_speaking` and `/status`.

The QA harness set is the comparison table below. It is not the only live UI.

| Conversation state | Typical session condition |
|---|---|
| `idle` | `created`, `closing`, or `closed` |
| `connecting` | `connecting` |
| `listening` | `active`, no live user or bot speech |
| `user-speaking` | capture in progress. `transcript.partial` is on the contract and is not emitted by cascade or native today |
| `thinking` | `agent.event` name `thinking`, `tool.started`, or `progress`. Thinking is visual only. It is never spoken |
| `bot-speaking` | `response.first_output` or assistant playback |
| `error` | `error`, or session `recovering`. Routes do not emit `error` or `warning` today |

Rendezvous orb accepts `idle`, `listening`, `thinking`, and `speaking`. `rendezvous/web/presentation.ts` maps host `working` to orb `thinking`.

## Session events

These `SessionEventType` values are the observable contract. Audio itself does not travel on events.

`session.state`, `transcript.partial`, `transcript.final`, `response.started`, `response.first_output`, `response.completed`, `response.cancelled`, `agent.event`, `tool.started`, `tool.completed`, `tool.failed`, `progress`, `interruption`, `error`, `warning`, `usage.attempt.started`, `usage.attempt.finished`, `usage.summary`, `roster.changed`.

Usage and roster events are in scope for identity and multi-agent display. They do not change barge-in or endpointing.

`progress` carries a pool. The live pools are `ack`, `action`, `ack_action`, `action:task`, `ack:empathy`, `working`, `control`, `greeting`, `cron_offer`, `reassure`, and `reassure:<tool>`.

`interruption` payload is `source` (`user_speech` or `cancel`) and `phase` (`working` or `answering`). Discord interrupted-context uses `working` or `answer`.

## Policy families

Each family is one requirement. The appendix lists every field. The matching fixture is under `sleeptalk/tests/fixtures/voice_ux/`.

### Thinking and preambles (VUX-PR-001)

Root `policy.fillers` is false. Filler clips do not play. Instant ack remains on, mode `smart`, budget 1200 ms. Empty `policy.preamble_pools` resolve to `DEFAULT_PREAMBLE_POOLS`. A question or lookup maps to the `action` pool. A task maps to `action:task`. Empathy maps to `ack:empathy`. The fallback is `ack`; a bare confirmation ("yeah", "thanks") gets no acknowledgement. After an ack, a generic first agent sentence is treated as a duplicate preamble, not a second spoken filler. Agent `thinking` events are visual only. They never speak chain of thought.

Fixture: `family-thinking-preamble.json`.

### VAD and smart turn (VUX-VD-001)

Discord, operator WebRTC, and terminal use `UtteranceAssembler`. Discord and WebRTC use hybrid VAD. Terminal keeps RMS plus its listen floor. Smart Turn v3 is on at threshold 0.6 even though those keys are omitted from root TOML. `policy.capture.vad_min_volume` is 0.01, not the schema 0.005. Discord and terminal feed streaming STT into endpointing. WebRTC does not. It endpoints on acoustics only. See VUX-SX-001.

Fixture: `family-vad-smart-turn.json`.

### Endpointing (VUX-EP-001)

Browser cascade calls with local STT/TTS automatically use the faster endpoint,
interruption, and phrase scheduling described in [local voice responsiveness](local-voice-responsiveness.md).
There is no experimental toggle. Other transports and cloud speech keep the
baseline values below.

Preset `balanced` plus explicit overrides. The operator-tuned silence for a complete balanced turn is 650 ms. Schema `EndpointingOverrides.balanced_complete_silence_ms` is 750. `AdaptiveEndpointingConfig` already defaults to 650. The root pin matches the adaptive dataclass, not the PolicyConfig default. Turns are judged by words on every surface: the live transcript of a streaming listener, or, with `early_transcript` (root `true`), a batch listener's early transcript at the pause. While that text is on its way the turn waits the uncertain budget instead of ending on the 120 ms capture silence; a finished sentence then ends at the complete-bucket silence and an unfinished one waits for the rest.

Fixture: `family-endpointing.json`.

### Interruption (VUX-IN-001)

Root `policy.interrupt.mode` is `soft`. All three surfaces run one decision, `BargeGate`, which owns the sustain streak, dip tolerance, `Floor` admission and what a fired barge does; no transport reaches `Floor` directly. `policy.interrupt.min_duration_ms` of 150 is the sustain bar everywhere. What legitimately differs per microphone — the level scale that carries a quiet spoken "stop" over `min_volume`, the dip tolerance, whether the surface can duck or resume — is declared in that transport's `BargeCalibration` rather than hidden in constants. Soft means pause-then-transcript where the surface can resume (Discord and WebRTC: a backchannel such as "mm-hmm", "ajá" or "嗯", in any of the page's languages, resumes the answer; other words stop it and become the turn), and is downgraded to a hard stop with `downgraded` recorded where it cannot, which is terminal today. On WebRTC, speech while the agent works in silence (a turn in flight, nothing playing, past the resume grace) is heard and settled the same way: a backchannel leaves the work alone; other words interrupt it and become the turn. Duck-on-VAD is Discord-only and reversible. `policy.resume_after_interrupt` and `policy.resume_offer_text` are parse-only. Fragment hold and user-driven resume are Discord-only; grace abort (speech within `resume_grace_ms` of a turn ending, before the agent is heard, re-asks the turn with both halves) runs on Discord and WebRTC. See VUX-SX-004.

Fixture: `family-interruption.json`.

### Capture (VUX-CP-001)

`policy.capture.silence_ms` is 120. `policy.capture.rms_threshold` is 0, which disables the RMS floor on Web and Discord. Terminal uses a listen floor of 120. `policy.turn_debounce_ms`, `policy.min_turn_chars`, `policy.duplicate_turn_window_ms`, `policy.post_bot_cooldown_ms`, `policy.barge_in_min_chars`, and `policy.early_barge_in_ms` are VoiceProcessor pins. The kernel Discord, WebRTC, and terminal paths do not read them. Fragment hold is a Discord-only schema-live value; resume grace is live on Discord and WebRTC. See VUX-SX-004.

Fixture: `family-capture.json`.

### Progress (VUX-PG-001)

`policy.keepalive_interval_s` is 10. On the kernel path a thinking, text, or tool event resets the timer. On the legacy knock path the timer is audible silence only. Tool narration and reassure lines come from `DEFAULT_NARRATION_PHRASES` and `DEFAULT_REASSURE_TOOL_POOLS`. Long-running tasks stay in their original agent request until completion, explicit cancellation, or an adapter failure. Progress updates never abort the agent, ask whether to continue, or manufacture a continuation turn. The legacy `policy.turns.long_task_check_in_seconds` and `policy.turns.long_task_auto_continue_seconds` settings are accepted but ignored, including previously saved positive values. Loop timings `_LOOP_DELAY_SECONDS` 2.0, `_LOOP_GAP_SECONDS` 1.4, and `_MIDTURN_GAP_SECONDS` 3.0 are code-owned.

Background jobs: with `policy.background_jobs` on (the default) and a cascade call to a tool-using agent, each turn tells the agent to hand bigger work (building something new, even a simple one; changes across several files; installing or deploying; research over several searches) to a background job and to do small changes to an existing file itself, by writing `<job title="…">brief</job>` and one sentence to say. The tags never reach the voice. After the first handoff of a call the turn adds "We can keep talking while it works." unless the agent already said as much; later handoffs don't repeat it. A second instance of the same agent runs the brief in the same folder while the conversation carries on; each later turn carries a short status of the job, and the agent can write `<job-note>` (an answer or change for the job) or `<job-cancel/>`; a cancel counts only when the caller's words ask to stop the job ("cancel", or "stop" naming the job), so "you can stop now" never ends it. Once the job has ended, every turn says no job is running; a note sent after a completed job runs as a follow-up job with the earlier brief and report. "How's it going?", "cancel the job", and a yes or no to the job's permission question are answered without an agent turn. The job's finish or failure is announced as a turn of its own once nobody has spoken for 1.5 s, On the call page the job has its own space, the worker panel. On a screen at least 960 px wide it slides in from the right when a job starts and the conversation makes room; on a narrower screen it waits as a pill above the orb (the latest step, a live dot and a timer; amber when the job needs the caller, green when done) and opens as a full-height sheet when tapped. Either is put away with its arrow, and a job the caller put away does not reopen on its own. The panel shows the job's thinking while it has not acted yet, every step as the harness names it ("terminal · ls -la", the newest with a timer), what it writes, and its links, plus a Cancel button that takes two taps. The conversation keeps only a job's milestones (started, needs you, done or stopped), and tapping one opens the panel. The terminal shows the milestones and the running job's latest step. None of it changes the orb state. Up to three jobs run at once (`policy.max_background_jobs`) and later ones queue, said out loud ("the todo app job will start when one of them finishes"); none is refused, and all end with the call. With several running, the caller's words reach a job by its title, else the job just announced, the only one, the one asking, or the selected one; a bare "cancel the job" with several running is confirmed by name first, a note to the selected one is followed by a line naming it, "stop everything" cancels all of them, and only one question (a job's request or a cancel confirmation) is open at a time. Jobs do not commit. See `sleeptalk/jobs.py`, `docs/plans/background-jobs.md` and `docs/plans/concurrent-background-jobs.md`.

Every turn that starts tool work gets a spoken cue. Cascade schedules it on the first tool-start event and delivers it before the answer, including with acknowledgements disabled or Snappy enabled. Hybrid announces specialist work before dispatch. Cancellation still stops pending speech. `policy.narration_style` controls subsequent tool activity: `silence` adds no per-tool lines, `narrated` speaks tool updates, and `keyboard` plays `sounds/keyboard.wav` on surfaces whose audio output can play a file. The timed "still working" check-ins remain independent. A surface without clip playback omits subsequent keyboard clips. Terminal plays clips through its normal audio output so the echo canceller sees them. Terminal cycles the style live with `n`, and the status bar shows `speak`, `quiet`, or `type`. Unknown values resolve to `silence`.

Fixture: `family-progress.json`.

### Reply shaping (VUX-RP-001)

`policy.reply_max_sentences` and `policy.reply_max_chars` cap VoiceProcessor replies. `policy.discord_reply_max_chars` is 1200 on the legacy Discord text path, not the schema 900. Cascade kernel speech is not capped by these fields.

`policy.reply_length` is `full`: every sentence the agent produces is spoken. `summarized` works in two moves. It prefixes the turn prompt with `voice_prompt.BRIEF_SPEECH_CONTRACT`, asking for at most three short sentences led by the outcome — the ask is what actually shortens a turn, and unlike a post-hoc trim it shortens the transcript and the token bill with it. It then backstops that ask: an answer is spoken verbatim until `sleeptalk.summarize.SPOKEN_BUDGET_CHARS` (200, code-owned, roughly the same three sentences) is spent, the rest is held back, and the turn closes with one line drawn from it. The budget is read per sentence, so a change mid-answer lands on the next one. The trim only ever removes words — every line spoken is a span of what the agent actually said — and the transcript keeps the whole answer either way, so a surface that shows text shows all of it while the mouth says less. Unknown values resolve to `full`: a value that did not round-trip must cost the listener time, never information.

Fixture: `family-reply-shaping.json`.

### First-audio latency (VUX-LT-001)

`policy.first_token_budget_ms` is 800. Code-owned preamble timing is ack 1.2 s counted from when the caller's turn is judged over (transcription included), sentence grace 0.4 s, action 2.0 s. A canned line whose audio has not started when the answer's first sentence is ready is dropped, and the next line of each opening pool is synthesized ahead at call start. The first speakable output is `response.first_output`. This is a product budget, not a hosted SLA.

Fixture: `family-latency.json`.

### Recovery and error speech (VUX-ER-001)

A non-fatal `error` keeps the session `active` and shows conversation `error`. A fatal error leaves `active` through `recovering` or `closed`. Recovery never resumes mid-utterance audio. Spoken and visible errors must not include credentials, raw provider dumps, or private transcripts. Later card `r2-voice-error-recovery-ux` implements richer recovery copy. This requirement freezes the observable states.

Fixture: `family-recovery.json`.

### Accessibility (VUX-AX-001)

The user can see a surface conversation state. Captions come from `transcript.final` today. `transcript.partial` is on the contract and is not produced by the kernel routes. Terminal adds the animated being. Keyboard mute exists on the QA browser client. The browser intro chime is a dated exception, not an accessibility feature.

Fixture: `family-accessibility.json`.

## Surface exceptions

Differences are named here. Unlisted differences are drift.

### Terminal speaker-safe capture (VUX-SX-001)

`sleeptalk/transports/terminal.py` defaults to speaker-safe capture so local TTS echo cannot start a recursive turn. After the speaker drains, speech-level loopback stays ignored until a quiet frame. A loud sustained interrupt still cancels playback. On laptop speakers, `s` or space cuts the current turn without ending the session. `SLEEPTALK_TERMINAL_FULL_DUPLEX=1` opts into spoken barge-in (headphones). Commit uses the shared `UtteranceAssembler` stack: `decide_endpoint` on live Deepgram partials, plus Smart Turn v3 as an accelerator. Streaming STT still gets at most 200 ms of trailing hush so Finalize and smart-ack stay snappy. Silero, fragment hold, and user-driven resume do not run. Gold pin: `4b3efe5` / tag `golden-terminal-cascade-4b3efe5` for the speaker-safe loop. Endpointing is no longer the 1000 ms timer.

Fixture: `exception-terminal-speaker-safe.json`.

### Discord pi-bot immediate barge-in (VUX-SX-002)

`config/profiles/example.toml` sets `[policy.interrupt] mode = "immediate"`. It is the only tracked profile that changes voice policy. The root standard remains `soft`. Copy it to `config/profiles/<slug>.toml` for a local bot.

Fixture: `exception-pi-bot-immediate.json`.

### Browser intro chime (VUX-SX-003)

The browser call path still plays an intro chime before the greeting; removing it is planned. The greeting stays the first semantic output and keeps canonical interruption. Operator WebRTC uses the assembler and Floor. The Vite Daily, LiveKit, and OpenAI harness uses `VoiceAgentProcessor` transcript barge instead.

Fixture: `exception-browser-intro-chime.json`.

### Discord post-commit hold and resume (VUX-SX-004)

Fragment hold, grace-period abort, status-query intercept, control-word stop, and user-driven continue live in `PiDiscordLiveSession`. Kernel WebRTC and terminal do not implement that post-commit loop. Resume is a user "continue" or "go on". There is no proactive offer.

Fixture: `exception-discord-post-commit.json`.

## Human conversation judgments

These are human QA script items. Automation records the question. It does not decide the answer.

| ID | Question |
|---|---|
| VUX-HJ-001 | Did the bot wait through a short mid-thought pause instead of answering the fragment? |
| VUX-HJ-002 | Did a cough duck or ignore, and did "stop" cut the audio? |
| VUX-HJ-003 | Did a keepalive line after 10 s of work sound like progress, or like spam? |
| VUX-HJ-004 | Was first audio timely, and was the ack truthful when the answer was late? |
| VUX-HJ-005 | Was a provider or network failure clear, secret-free, and recoverable? |
| VUX-HJ-006 | Did terminal, Discord, and browser feel like one product aside from the named exceptions? |

Fixtures: `human-pause-vs-end.json`, `human-false-start-vs-barge.json`, `human-keepalive-feel.json`, `human-first-audio-feel.json`, `human-error-clarity.json`, `human-cross-surface.json`.

## Change policy

Do not retune a family because a cleaner schema default is easier to write. A change needs a versioned `sleeptalk.toml` or `policy.py` diff, an updated receipt, a fixture that fails on the old value, and later Human QA.

`tests/test_voice_ux_standard.py` fails when a live policy leaf is missing from `policy_map.json`, when a mapped value does not match the loaded root, when a tuned field equals its schema default, when a receipt checksum drifts, or when this document drops a requirement id or field path.

## Field inventory

| Path | Effective | Schema default | Source | Requirement |
|---|---|---|---|---|
| `policy.background_jobs` | `True` | `True` | schema-live | VUX-PG-001 |
| `policy.barge_in_min_chars` | `4` | `4` | root-pinned | VUX-IN-001 |
| `policy.capture.compare_primary` | `'silero'` | `'silero'` | schema-live | VUX-VD-001 |
| `policy.capture.min_listen_utterance_ms` | `450` | `450` | root-pinned | VUX-CP-001 |
| `policy.capture.min_transcript_words` | `4` | `4` | root-pinned | VUX-CP-001 |
| `policy.capture.min_utterance_ms` | `450` | `450` | root-pinned | VUX-CP-001 |
| `policy.capture.rms_threshold` | `0` | `300` | root-tuned | VUX-CP-001 |
| `policy.capture.silence_ms` | `120` | `450` | root-tuned | VUX-CP-001 |
| `policy.capture.smart_turn` | `True` | `True` | schema-live | VUX-VD-001 |
| `policy.capture.smart_turn_threshold` | `0.6` | `0.6` | schema-live | VUX-VD-001 |
| `policy.capture.vad_confidence` | `0.78` | `0.78` | root-pinned | VUX-VD-001 |
| `policy.capture.vad_min_volume` | `0.01` | `0.005` | root-tuned | VUX-VD-001 |
| `policy.capture.vad_mode` | `'hybrid'` | `'hybrid'` | root-pinned | VUX-VD-001 |
| `policy.capture.vad_start_ms` | `250` | `250` | root-pinned | VUX-VD-001 |
| `policy.capture.vad_stop_ms` | `900` | `900` | root-pinned | VUX-VD-001 |
| `policy.discord_reply_max_chars` | `1200` | `900` | root-tuned | VUX-RP-001 |
| `policy.discord_reply_max_sentences` | `8` | `8` | root-pinned | VUX-RP-001 |
| `policy.duplicate_turn_window_ms` | `10000` | `10000` | root-pinned | VUX-CP-001 |
| `policy.early_barge_in_ms` | `900` | `900` | root-pinned | VUX-IN-001 |
| `policy.endpointing` | `'balanced'` | `'balanced'` | root-pinned | VUX-EP-001 |
| `policy.endpointing_overrides.balanced_complete_silence_ms` | `650` | `750` | root-tuned | VUX-EP-001 |
| `policy.endpointing_overrides.complete_silence_ms` | `450` | `450` | root-pinned | VUX-EP-001 |
| `policy.endpointing_overrides.completion_threshold` | `0.72` | `0.72` | root-pinned | VUX-EP-001 |
| `policy.endpointing_overrides.early_transcript` | `True` | `True` | root-pinned | VUX-EP-001 |
| `policy.endpointing_overrides.enabled` | `True` | `True` | root-pinned | VUX-EP-001 |
| `policy.endpointing_overrides.filler_reset_ms` | `3500` | `3500` | root-pinned | VUX-EP-001 |
| `policy.endpointing_overrides.incomplete_silence_ms` | `3500` | `3500` | root-pinned | VUX-EP-001 |
| `policy.endpointing_overrides.monologue_complete_silence_ms` | `850` | `850` | root-pinned | VUX-EP-001 |
| `policy.endpointing_overrides.monologue_incomplete_silence_ms` | `5000` | `5000` | root-pinned | VUX-EP-001 |
| `policy.endpointing_overrides.monologue_mode_ms` | `15000` | `15000` | root-pinned | VUX-EP-001 |
| `policy.endpointing_overrides.responsive_mode_ms` | `5000` | `5000` | root-pinned | VUX-EP-001 |
| `policy.endpointing_overrides.semantic_complete_silence_ms` | `650` | `650` | root-pinned | VUX-EP-001 |
| `policy.endpointing_overrides.uncertain_silence_ms` | `1800` | `1800` | root-pinned | VUX-EP-001 |
| `policy.filler_delay_ms` | `1200` | `800` | root-tuned | VUX-PR-001 |
| `policy.filler_general` | `['Let me check that.', 'One second, I am looking.', 'I am checking now.', 'Let me pull that together.']` | `['Let me check that.', 'One second, I am looking.', 'I am checking now.', 'Let me pull that together.']` | root-pinned | VUX-PR-001 |
| `policy.filler_web_search` | `['Let me check that.', 'Checking now.', "I'll look that up.", 'One second, checking.', 'Let me verify that.']` | `['Let me check that.', 'Checking now.', "I'll look that up.", 'One second, checking.', 'Let me verify that.']` | root-pinned | VUX-PR-001 |
| `policy.fillers` | `False` | `True` | root-tuned | VUX-PR-001 |
| `policy.first_token_budget_ms` | `800` | `800` | root-pinned | VUX-LT-001 |
| `policy.fragment_barge_window_ms` | `10000` | `10000` | schema-live | VUX-CP-001 |
| `policy.fragment_hold_ms` | `1500` | `1500` | schema-live | VUX-CP-001 |
| `policy.instant_ack` | `True` | `True` | schema-live | VUX-PR-001 |
| `policy.instant_ack_mode` | `'smart'` | `'smart'` | schema-live | VUX-PR-001 |
| `policy.interrupt.cooldown_ms` | `1200` | `1200` | root-pinned | VUX-IN-001 |
| `policy.interrupt.enabled` | `True` | `True` | root-pinned | VUX-IN-001 |
| `policy.interrupt.gap_hold_ms` | `600` | `600` | root-pinned | VUX-IN-001 |
| `policy.interrupt.grace_ms` | `400` | `400` | root-pinned | VUX-IN-001 |
| `policy.interrupt.min_duration_ms` | `150` | `150` | root-pinned | VUX-IN-001 |
| `policy.interrupt.min_volume` | `0.03` | `0.03` | root-pinned | VUX-IN-001 |
| `policy.interrupt.mode` | `'soft'` | `'immediate'` | root-tuned | VUX-IN-001 |
| `policy.keepalive_interval_s` | `10.0` | `10.0` | root-pinned | VUX-PG-001 |
| `policy.narration_style` | `'silence'` | `'silence'` | schema-live | VUX-PG-001 |
| `policy.max_background_jobs` | `None` | `None` | schema-live | VUX-PG-001 |
| `policy.min_turn_chars` | `2` | `2` | root-pinned | VUX-CP-001 |
| `policy.post_bot_cooldown_ms` | `1200` | `1200` | root-pinned | VUX-CP-001 |
| `policy.reply_length` | `'full'` | `'full'` | schema-live | VUX-RP-001 |
| `policy.reply_max_chars` | `1200` | `1200` | root-pinned | VUX-RP-001 |
| `policy.reply_max_sentences` | `8` | `8` | root-pinned | VUX-RP-001 |
| `policy.resume_after_interrupt` | `True` | `True` | root-pinned | VUX-IN-001 |
| `policy.resume_grace_ms` | `750` | `750` | schema-live | VUX-CP-001 |
| `policy.resume_offer_text` | `'I was partway through that. Want me to continue?'` | `'I was partway through that. Want me to continue?'` | root-pinned | VUX-IN-001 |
| `policy.smart_ack_budget_ms` | `1200` | `1200` | schema-live | VUX-PR-001 |
| `policy.turn_debounce_ms` | `2000` | `2000` | root-pinned | VUX-CP-001 |
| `policy.turns.bot_grace_ms` | `400` | `400` | root-pinned | VUX-IN-001 |
| `policy.turns.debug` | `False` | `False` | root-pinned | VUX-VD-001 |
| `policy.turns.duck_confidence` | `0.7` | `0.7` | schema-live | VUX-IN-001 |
| `policy.turns.duck_min_ms` | `200` | `200` | schema-live | VUX-IN-001 |
| `policy.turns.duck_min_volume` | `0.03` | `0.03` | schema-live | VUX-IN-001 |
| `policy.turns.duck_on_vad` | `True` | `True` | schema-live | VUX-IN-001 |
| `policy.turns.duck_release_ms` | `600` | `600` | schema-live | VUX-IN-001 |
| `policy.turns.interrupt_min_ms` | `250` | `250` | root-pinned | VUX-IN-001 |
| `policy.turns.long_task_auto_continue_seconds` | `30.0` | `30.0` | parse-only (ignored) | VUX-PG-001 |
| `policy.turns.long_task_check_in_seconds` | `180.0` | `180.0` | parse-only (ignored) | VUX-PG-001 |
| `policy.turns.max_wait_seconds` | `120.0` | `120.0` | root-pinned | VUX-CP-001 |
| `policy.turns.post_interrupt_cooldown_ms` | `1200` | `1200` | root-pinned | VUX-IN-001 |
| `policy.turns.silero_confidence` | `0.7` | `0.7` | root-pinned | VUX-VD-001 |
| `policy.turns.silero_min_volume` | `0.005` | `0.005` | root-pinned | VUX-VD-001 |
| `policy.turns.silero_start_ms` | `220` | `220` | root-pinned | VUX-VD-001 |
| `policy.turns.silero_stop_ms` | `900` | `900` | root-pinned | VUX-VD-001 |
| `policy.turns.vad_profile` | `'default'` | `'default'` | root-pinned | VUX-VD-001 |
| `policy.turns.vad_threshold` | `500` | `500` | root-pinned | VUX-VD-001 |

| Path | Effective | Source | Requirement |
|---|---|---|---|
| `policy.preamble_pools` | `'DEFAULT_PREAMBLE_POOLS'` | code-owned | VUX-PR-001 |
| `policy.reassure_tool_pools` | `'DEFAULT_REASSURE_TOOL_POOLS'` | code-owned | VUX-PG-001 |
| `policy.narration_phrases` | `'DEFAULT_NARRATION_PHRASES'` | code-owned | VUX-PG-001 |
| `VoicePolicyTimings.ack_delay_seconds` | `1.2` | code-owned | VUX-LT-001 |
| `VoicePolicyTimings.sentence_grace_seconds` | `0.4` | code-owned | VUX-LT-001 |
| `VoicePolicyTimings.action_delay_seconds` | `2.0` | code-owned | VUX-LT-001 |
| `VoicePolicyRuntime._LOOP_DELAY_SECONDS` | `2.0` | code-owned | VUX-PG-001 |
| `VoicePolicyRuntime._LOOP_GAP_SECONDS` | `1.4` | code-owned | VUX-PG-001 |
| `VoicePolicyRuntime._MIDTURN_GAP_SECONDS` | `3.0` | code-owned | VUX-PG-001 |
| `TurnPolicyValues.vad_mode` | `'compare'` | code-owned | VUX-VD-001 |
