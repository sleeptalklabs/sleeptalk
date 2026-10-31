# Background jobs: the voice keeps talking while the agent builds

Status, 2026-10-13: P0 and P1 are built on branch `background-jobs`: `sleeptalk/jobs.py`, the session's announcements, the cascade hooks, and `policy.background_jobs`. Also built: the replacement rule from P2 (a job whose handoff turn was cut short can be replaced), quick status, cancel and permission replies, and Discord text posts. The backstop (stowing at 45 s), surviving hangup, and clickable links on the call page are not built.

## The problem

"Make a todo app and give me a link" works today. The trouble is that on the cascade route the agent's turn holds the floor until the work is done (`routes/cascade.py`: "Cascade has no detached task"). The caller hears keyboard sounds or lines like "quote mangling in the shell, I'll put the test in a script", and their only choice is to wait or interrupt. The `hybrid` (Realtime) route already detaches one specialist task. Cascade, which every "My agent" call uses, does not.

## What we want

1. A long task "stows away": the agent works out of earshot, and the conversation stays open.
2. Short things stay exactly as they are today, in the same session with the same agent.
3. The caller can ask how it's going, add to the task, or cancel it, without interrupting the work.
4. When it finishes, one short spoken line; links and other unspeakable output go to text.

## Decisions

| Question | Decision |
|---|---|
| Who answers while a job runs | The user's own agent, in its normal foreground session. The job runs in a second session of the same agent. No sleeptalk-owned model for now; sleeptalk's own agent can take the foreground role later |
| How many jobs at once | One |
| Native subagents (Hermes `delegate_task` etc.) | Not the mechanism. Still observed via `subagent_roster.py` inside the job |
| Do jobs survive hangup | Yes, but as phase 3, after the core works. See "Hanging up" |

## When does it go to the background?

Sleeptalk can't tell from the words alone. "Fix the typo in the README" and "fix the login bug" look the same. The agent can, because it knows what the work involves. So the agent decides, and two backstops cover the cases where it doesn't.

**1. The agent hands it off before it starts (the main path).** While voice is on, the foreground prompt carries one more instruction:

> (As built, the rule is about the kind of work, not a time estimate. In the first voice run, Hermes judged a todo app too small to hand off. See `HANDOFF_INSTRUCTION` in `sleeptalk/jobs.py`.) If the request will take more than about a minute of tool work (building something, changes across several files, multi-step research), do not start it. Write the task in `<job title="short name">…</job>`, as a self-contained brief with everything the worker needs from this conversation, then one short sentence to say out loud. Do quick things (look something up, run one command, read a file, answer from what you know) yourself as usual. If the caller says to do it here and now, do it yourself. If they say to do it in the background, hand it off.

Sleeptalk takes the `<job>` block out of the speech, starts the job with the brief, and speaks the sentence ("I'll build that in the background. What else?"). The foreground turn ends within seconds, so this path never needs a timer. The brief is written by the agent that heard the whole conversation, so the job starts with full context.

**2. The caller says so.** "Do that in the background" before the work starts goes through path 1, since the agent follows the instruction. Said while a turn is already running, it triggers path 3 right away.

**3. Backstop: the agent started anyway.** If a foreground turn keeps using tools for more than `jobs.stow_after_seconds` (default 45) without having said anything to the caller, sleeptalk stows it. Quick tool use never reaches this. Stowing works like this:

- The busy session becomes the job. Its request keeps running untouched, as fbadd57 made possible.
- The conversation moves to a new foreground session, started with the call transcript sleeptalk already has.
- The caller hears, once and without being asked a question: "This one's bigger. I'll keep at it in the background; talk to me any time."

Paths 1 and 3 end in the same state: one foreground session and one job session. Path 1 is cheaper and keeps the original session as the conversation. Path 3 exists so the caller is never stuck, even with an agent that ignores the instruction.

## How it works

```
caller ⇄ voice (cascade) ⇄ foreground session (user's agent, unchanged)
                 │  each turn while a job exists: + job status block
                 │  agent output markers: <job>, <job-note>, <job-cancel>
                 ▼
            JobManager ── one job = second adapter instance, same profile + cwd
                 │  events → job record (saved to disk) → announcer → spoken line / text card
```

**JobManager** (new `sleeptalk/jobs.py`) owns at most one job:

- **Starting a job.** It builds a second adapter instance from the same agent profile and folder, using the same builder path as the foreground. The job instance must never load or save the foreground's saved session id (Hermes `use_saved_session` / `_save_session_id`; check every adapter).
- **The job's prompt** is the brief plus a job contract: "You are working in the background; nobody hears you until you finish. Only ask a question if you are truly blocked. End with a two-sentence spoken summary, and put any link or path on its own line."
- **The job record** holds id, title, brief, phase (reusing `WorkerPhase`), start time, last tool, last assistant text, result, links and pending question. It is written to `<state dir>/jobs/<id>.json`.
- The job adapter's `WorkerEvent`s feed the roster as usual, so Hermes delegation inside a job still shows.

**The foreground sees the job as text** that `prepare_voice_prompt` adds to every turn while a job exists:

```
[Background job "todo app" — running 3m10s — last step: running the tests — it last said: "switching the test to a script file"]
The job owns the files in this folder; don't edit them. To change or add to its work, write <job-note>…</job-note>. To stop it, write <job-cancel/>.
```

After the job finishes, the next foreground turn gets `[Background job "todo app" finished: <summary> <links>]` once. This is how the foreground agent answers "how's it going?" and "what did it build?" in its own voice and with its own memory.

**Quick replies without a model turn.** "How's the job going?" and "cancel the job" are matched without calling the agent, as `answer_status` already does today (`voice_policy_runtime.py:903`), and answered from the job record. Anything less clear goes to the foreground agent with the status block.

**Steering.** `<job-note>` is passed into the running job if the adapter supports steering (`AgentCapabilityManifest.steering`). Otherwise it is queued and sent as a follow-up turn after the job's current turn ends; finishing then waits for the queue to empty.

**Announcer.** Job events become lines in a queue. A line plays only when nobody is talking: no caller speech, no foreground reply playing, and at least 1.5 s of quiet. Lines are said once and never during a reply:

- started (only for path 3)
- question or permission: "The todo job wants to run npm install. Okay?"
- finished: "The todo app is done; the link's in the chat."
- failed

Jobs never narrate their tool use, whatever `narration_style` is.

**Job permissions** reuse `answer_pending`, labelled with the job's title, so "yes" or "no" goes to the job's adapter. **Job questions** are spoken, and the answer goes through the foreground agent (the question is in its status block, and it replies with `<job-note>`).

**Text delivery.** Links and paths from the result go to:

| Surface | Where it shows |
|---|---|
| Discord | A message in the call's text channel |
| Web call page | A job card (new `job.changed` event next to `roster.changed`) |
| Terminal | A printed line |

The spoken line only points at it.

## Hanging up

A job lives in the sleeptalk process that ran the call, so whether it survives a hangup depends on that process:

| Surface | What happens at hangup | Cost |
|---|---|---|
| Discord (stay-ready bot, #89) | The process lives for days, so the job keeps going; the result is posted in the text channel | Low |
| Web call (detached child) | The child exits at hangup today. It must stay alive with no audio until the job ends or hits its cap | Medium |
| Terminal | Ctrl-C prints "a job is still running: leave this open, or press Ctrl-C again to cancel it" | Low |

Problems this adds, and the answer for each:

- **A permission request with nobody on the line.** Wait 2 minutes, then deny it and note that in the result.
- **Runaway cost.** A hard cap of `jobs.max_minutes` (default 30), then cancel and save what exists.
- **Delivering the result.** Saved to the job record. The next call opens with "While you were away, the todo app finished. Want the summary?", the same pattern as the `cron_offer` greeting. On Discord it is also posted in the text channel.
- **Replacing a running call (#87).** A process kept alive only for a job must not count as "already has a live call".
- **Controlling the job from a later call in another process.** Not in v1. A later call sees the saved status and result. Cancelling across processes, for example through a flag file the owning process watches, comes later if needed.

Verdict: worth doing, once only the owning process keeps the job alive and there's no control across processes. The real cost is the web child's lifecycle; Discord gets it nearly free.

## Phases

**P0 result (2026-10-13).** Two instances of the same agent ran side by side in one folder, each answering its own prompt:

| Agent | Two at once | Notes |
|---|---|---|
| Hermes (ACP) | yes | |
| Hermes (gateway) | yes, after a fix | Before the fix, text from a cancelled reply leaked into the next reply. Now the adapter waits for the old run to end |
| omp | yes | |
| pi | yes | |
| Claude Code | yes | |
| Cursor (ACP) | yes | Reports permission requests in auto mode and answers them itself. Jobs wait 2 s before saying a request, and drop it if the job carries on |
| Codex | not tested | The model configured on this Mac (`gpt-6.1-sol`) is refused for a ChatGPT account. The adapter now reports that instead of "None" |
| OpenClaw | not tested | Its login had expired |

The "make a todo app" test, run as a text call for each agent (caller adds "keep it local"):

| Agent | Handoff after | Job done at | Link reported |
|---|---|---|---|
| Hermes (ACP) | 12 s | 154 s | yes (deployed to Vercel; that run's request did not say "keep it local") |
| omp | 7 s | 15 s | yes |
| pi | 17 s | 32 s | yes |
| Claude Code | 9.5 s | 142 s | yes |
| Cursor | 9.5 s | 79 s | yes |

Hand-off decisions, as a text call with a fresh session for each request. The job itself is not run; only the decision is measured:

| Agent | Long requests handed off (todo app, landing page, pricing research) | Short requests kept in the reply (read README, create notes.txt, 12 × 11) |
|---|---|---|
| Claude Code | 3/3 | 3/3 |
| omp | 3/3 | 3/3 |
| Hermes (ACP) | 3/3 | 3/3 (12 × 11 got no answer at all; that also happens with no job rule, so it is a separate Hermes issue) |

Two rule changes came out of this:
- The rule is about the kind of work, not a time guess.
- It says not to use the agent's own subagent or delegate tools. Before that line, Hermes and omp sent long work to their own subagents ("I've sent todo app to work on that").

Synthetic voice calls (`scripts/synthetic_call`, script steps `job_todo` → `job_chat` → `job_status` → wait → `job_where`). All passed the full flow:
1. The hand-off was spoken in 1.4–2.5 s.
2. Ordinary chat was answered while the job ran.
3. The status question was answered from the job.
4. The done line played at a quiet moment and showed on the page.
5. "Where" was answered with the local link.

The calls that passed: omp, Claude Code and pi, and Hermes over the gateway after the leak fix and the no-native-delegation line (run v5). In one earlier Hermes run, before that line, Hermes built the app itself.

**First real call and round two (2026-10-13).** A web call on the user's Hermes gateway profile worked end to end, and showed four problems:

1. A one-function edit ("clean board defaults") went to a job. The rule said "even when it looks small". It now names small changes to an existing file as work the agent does itself. Hand-off decisions, with three small-edit requests added (remove the sample cards, change the page title, add one card):

   | Agent | Old rule | New rule |
   |---|---|---|
   | Hermes (gateway) | 7/8 (handed off the title change) | 8/8, 8/8 |
   | omp | | 8/8 |
   | Claude Code | | 8/8 |

2. After the job finished, the agent sent it a `<job-note>` and told the caller it was passed on; the note was dropped. Every turn after a job ends now says no job is running, and a note to a completed job runs as a follow-up job with the earlier brief and report.
3. The call page labelled the agent "def": the profile's `agent.name` was `default`, cut to three letters. A placeholder name (`default`, `sleeptalk`, `agent`, `main`) now shows the profile's slug.
4. The page showed the job only as two notes. The job is now a third speaker, `worker` (see below).

Synthetic calls then found two Hermes gateway bugs that garbled any reply when the caller paused mid-request (each pause restarts the turn):

- A prompt sent while Hermes is still running is queued (`status: "queued"`), and the old run's tail was read as the new reply. The adapter now skips events until that run ends and the queued one starts. A cancelled turn also left its queue read pending, which took the next event.
- A run interrupted while "waiting for model" leaves its model call running inside Hermes (`_fire_stream_delta` has no interrupt check), and its text streams out as the next run's deltas: two hand-off replies woven together mid-word, the job tags broken, and the brief spoken aloud for up to two minutes. For 60 s after such an interrupt the adapter uses only the clean text in `message.complete`. The same fallback speaks replies Hermes sends only in `message.complete` (short answers such as "12 × 11", which used to get no answer).

Briefs are now taken from the agent's raw text. Sentences are shaped for speech, which dropped code, ids and the hex parts of paths.

**The worker on the call page.** Job events are reduced into `worker` lines (`sleeptalk/presentation.py`): "Working on todo app.", one step line that is replaced as the job moves on (with a count of tool calls and a pulsing dot while live), the worker's own sentences, and "Done." with its links (web links open in a new tab; file paths are shown to copy). They never change the orb. The terminal shows the same lines. The second real call (a claw machine game) still felt like a black box: Hermes thought for two minutes before its first tool call, then wrote a 40 KB file in one call, and the page showed nothing for either. Now the page shows the model's reasoning as it streams (`ReasoningDelta`, at most every 1.5 s), "write_file · writing…" while a tool call's arguments are written (Hermes `tool.generating`), and steps by the harness's own tool names with Hermes's labels (`ToolEvent.display_name` and `label`), the last four at a time with a timer on the newest. The same call found two more bugs: "okay, you can stop now" cancelled the job (cancel now needs the caller's words to ask for it), and the job died at the Hermes adapter's five-minute reply limit (job turns now get the job's own time; a job has no time limit unless one is set, after a Claude Code job was cut off at the old half hour). Then the worker moved into its own panel (`rendezvous/web/worker_panel.ts`, fed by `PresentationState.jobs`): it slides in on wide screens and is a pill above the orb that opens a sheet on a phone; the conversation keeps only milestones. Earlier idea: tuck the worker into a small circle in a corner that shows it is working; tapping it opens a drawer with the worker's lines, so the transcript stays the conversation.

**P0: the spike as planned.** For each adapter (Hermes, Claude Code, Codex, omp, pi, Cursor, OpenClaw):

- Can a second instance run at the same time in the same folder? Check for locks on shared state, such as Hermes's session database.
- How long does a cold start take?
- Do `<job>` blocks come through each harness's output unchanged?

The result is a table in this doc. Any adapter that can't run a second instance is marked "no background jobs", and its calls behave as they do today.

**P1: agent-declared handoff on cascade (fixes the todo-app call).**

- `jobs.py` with the JobManager and job record
- The handoff instruction and marker parsing in `voice_policy_runtime.respond`, removing markers before TTS
- The job contract prompt
- The status block in `prepare_voice_prompt`
- The finished/failed announcer
- Text delivery on all three surfaces
- Quick status and cancel replies

Acceptance, on Hermes:

1. "make a todo app and give me a link" gets a spoken handoff within about 5 s.
2. Ordinary chat works while the job runs.
3. "How's it going?" is answered without stopping the job.
4. "Done" is announced at a quiet moment.
5. The link appears on the page or in Discord.
6. "What's in package.json?" stays in the foreground.

**P2: backstop and controls.**

- Stowing at 45 s, with a new foreground session started from the transcript
- "Do that in the background" in the middle of a turn
- `<job-note>` (steering or a queued follow-up)
- Job permissions and questions through the announcer

**P3: surviving hangup.** Everything in the table above: the web child staying alive, the caps, permission timeouts, the welcome-back greeting, and the #87 exception.

**P4: one model everywhere.**

- Move `hybrid`'s detached specialist onto the JobManager, so Realtime and cascade behave the same.
- Replace the text markers with a real tool where the harness accepts one: ACP `mcpServers` (Hermes, Cursor), Claude Code, Codex.
- Sleeptalk's own agent becomes an option for the foreground.

## Evals

Use the synthetic-call harness with a fixed set of prompts for each adapter:

| Kind | Prompts | Expected |
|---|---|---|
| Long | "make a todo app", "refactor the settings page into three files", "research and compare X, Y, Z" | Handoff |
| Short | "what's in package.json", "fix the typo in the README title", "what time is it in Tokyo" | Stays in foreground |
| Override | "do it here", "do it in the background" | Does what was said |

Score how often the handoff decision is right, whether a marker ever leaks into speech, how long until the handoff line plays, and whether a job's line ever plays over the caller.

## Risks

- **The agent misjudges.** It hands off tiny tasks or does huge ones itself. Mitigation: tune the instruction wording against the evals; the backstop catches what's missed.
- **A marker gets read aloud.** Remove it in the sentence splitter before TTS, with tests that include markers split across streamed chunks.
- **Two agents in one folder.** Only the instruction keeps the foreground from editing. Later, give the foreground read-only permissions while a job runs, on adapters that allow it.
- **Two live sessions cost about twice as much** while a job runs.
- **The status block adds tokens to every turn.** Keep it to two or three lines.
