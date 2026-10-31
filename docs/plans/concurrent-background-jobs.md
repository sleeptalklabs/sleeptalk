# Several background jobs at once

Status, 2026-10-15: decided; phases 1 and 2 built (branch concurrent-jobs-p1), phases 3–4 not started. It revisits the **one job at a time**
decision in `docs/plans/background-jobs.md`, which was a user decision, not a technical
wall. The code is `sleeptalk/jobs.py` (`JobManager`), `_job_reply` in
`sleeptalk/routes/cascade.py`, `_apply_job_actions` in `sleeptalk/voice_policy_runtime.py`,
`_worker` in `sleeptalk/presentation.py`, and `rendezvous/web/worker_panel.ts`.

## The call that asked for this

In one call, two things queued behind a 45-minute latency measurement: a summarization
bug and a Six Flags valuation. Neither had anything to do with the latency run. The
valuation never touched a file at all — it was research. Today `JobManager.start` answers
both with `BUSY_LINE`: "I'm still working on the latency job, so I can't start another one
yet." The caller then has to remember to ask again later, which is exactly the thing
background jobs were built to stop.

So the question is not really "can we run two agents at once" — P0 already proved every
adapter can (`docs/plans/background-jobs.md`, the two-at-once table). It is: with two or
three jobs alive, can a person steer them **by voice alone**, and does the call still feel
fast. Everything below is sized by those two questions.

## Decisions

| Question | Decision | Why |
|---|---|---|
| How many at once | **3 by default**, config key `policy.max_background_jobs` | Decided by the user on the call of 2026-10-15, overruling the 2 recommended below. They judged many-at-once unlikely in practice, so allow a few freely and cap it. Phase 1 still measures what 3 does to voice latency |
| Static or dynamic | **Static**: 3 for every profile, local speech included (changed 2026-10-15; a planned default of 1 for local speech surprised the user on a call) | A caller who cannot predict whether their request starts now or queues will stop trusting the handoff. Kokoro and Moonshine do fight jobs for the same cores; a profile that stutters can set fewer |
| Over the limit | **Queue, never refuse** | "No hard caps" is the standing rule. A refused request is lost; a queued one is not |
| Queue depth | Unbounded, with a spoken notice at five waiting | Same rule |
| File isolation | **Scope declared in the handoff**, not a sandbox. Only one repo-writing job at a time per workspace; reading and build-something-new jobs run freely. Worktrees are an opt-in third scope | Nothing sleeptalk can do actually confines an agent — Hermes has written outside its given workspace. So isolation has to be admission control plus a convention, and the convention should be cheap |
| Jobs and git | **Jobs do not commit.** They leave changes in the working tree and name what they touched | This is the shared-checkout gotcha: `git add <file>` by one worker sweeps another's unreviewed hunks into the commit. Worktree-scope jobs may commit, since they own their index |
| Routing by voice | **By title, then a short list of fallbacks ending at the selected tab.** A bare cancel always confirms, naming the job. Never guess a cancel | A wrong cancel throws away 45 minutes |
| Asking | **One question holds the asking floor at a time**, a job's permission request or a cancel confirmation. The next one waits | "Yes" with two pending permissions cannot be routed, and a mis-routed yes approves something the caller did not approve |
| Announcements | Keep the existing one-at-a-time queue; add priority and coalescing | `Session._deliver_announcements` already plays one line at a time and waits for quiet, so the "talking over each other" problem is mostly solved already |
| Caps on runs | None, as before. Visibility instead | The user's standing rule |

**Decided default, in one line:** three jobs running as tabs, a fourth queued, named by
title, one repo-writer at a time, nothing ever refused.

## Decided on the call, 2026-10-15

The user walked through this out loud. The rest of the plan has been brought in line
with these.

- **Three active jobs**, not two. A fourth queues, as before — nothing is ever refused.
- **Each job is a tab** in the worker side panel, labelled with its job title.
- **All tabs keep running.** Like browser tabs, selection changes only what is shown; an
  unselected job is not paused, throttled or deprioritised.
- **A new job does not steal the selection.** The first job is selected when it starts;
  later ones appear as tabs and leave the caller on what they were reading. (Briefly
  decided the other way on the call, then reverted the same day to this, the original
  proposal.) The selection moves only when the caller picks a tab, or when the selected
  job's tab is cleared — then it goes to the job needing attention, else the newest live one.
- **The selected tab is what the voice controls** when nothing more specific applies. A
  bare "cancel the job" targets the selected one; "cancel the Discord one" routes by title,
  as specified below.
- **A bare cancel confirms first** when more than one job is live — one short line naming
  the job it is about to kill ("cancel the research one?"). The selected job may not be the
  one the caller has in mind, most of all right after starting a new one, which did not take
  the selection. A cancel is never guessed. With a single job a bare cancel acts at once, as
  today (routing rule 3).
- **The selection lives on the host**, not in the page. Voice routing needs it, and a call
  can have no page (Discord, terminal), a closed phone sheet, or two pages open at once. The
  page shows it and sends changes; every surface without tabs uses the host's value, which
  follows the rule above.

### Decided 2026-10-15: overflow on a phone

At three, nothing needs to scroll on a desktop panel: three tabs fit in the header with
titles truncated to about fourteen characters, full title on hover and in the feed. The
phone is the tight case, where the panel is a pill plus a sheet.

- The **pill** keeps reading as one line, but counts: "3 jobs · research" — the count plus
  the selected job's title. Tapping opens the sheet, as it does today.
- Inside the **sheet**, tabs are a horizontally scrollable row of chips, each a title plus
  a state dot (running, needs you, done). The selected chip is filled, the rest outlined.
  Scroll, not wrap: a second row of chips costs more height than a phone sheet can spare.
- The selected chip is always scrolled into view. A newly started job's chip does not
  take the selection; it scrolls into view once with its dot pulsing, then the row returns
  to the selected chip.
- If the cap is ever raised past three, chips past the fourth collapse into a `+2` chip
  that opens a plain list. Not needed at three; specified so raising the cap is a config
  change rather than a redesign.

### Decided 2026-10-15: a job that finished but was not read

- The tab **stays** when the job finishes, marked done, and keeps its position. A finished
  job's report is the whole point of the handoff; it must not vanish because a new job
  started.
- A finished tab **does not steal selection** either: neither starting nor finishing moves
  it. The caller may be mid-sentence about something else, and the spoken announcement
  already tells them it is done.
- A **follow-up** to a finished job (a note sent after it ended, which today starts a new
  job with the brief and the old report, `JobManager.note`) takes over that job's tab
  rather than opening a second one with a renamed title. It is live work again, so it
  counts against the three and queues if all three are busy.
- It carries an **unread mark** until its tab is opened. The spoken status reply counts it
  the same way: "one still going, and the Discord one is done, unread."
- Finished tabs **do not count against the three**. A fourth job may start while a done
  tab is still sitting there; the cap is about live work and CPU, not about screen space.
- Opening the tab clears its unread mark; the tab itself stays until the end of the call.
  If finished tabs ever crowd the row (more than eight tabs), the **oldest finished one**
  goes first, and a running or queued tab is never dropped for a finished one. As built,
  "unread" lives on the page (the host does not know when a tab is on screen), so the
  spoken status names a finished job as done, not as unread.

## How many, and what a job costs

Each job is a fresh `build_agent` instance — a separate CLI process with its own model
session (`_job_agent_factory` in `sleeptalk/builder.py`). Two jobs plus the foreground is
three concurrent model streams on the caller's account.

**Tokens.** Linear in the number of jobs, and a job's context grows the longer it runs.
Since jobs have no time limit (`max_seconds=None`, after a Claude Code job was killed at
30:04), a single long job is already the expensive case; a second one roughly doubles it
while both are alive. There is no cap to add here — the user has ruled that out — so the
answer is visibility: the worker panel already shows each job's elapsed time and step
count, and the spoken status reply should name each ("two going: the latency run,
thirty-one minutes in, and the bug fix, four"). Add one soft notice, never a kill: when a
job passes `policy.job_long_notice_minutes` (default 45) the agent mentions it at a quiet
moment and carries on. It does not ask a question and does not stop anything.

**CPU.** This is the one that can make the product worse rather than just pricier. Agent
processes are mostly waiting on model tokens, but their tool work is not: builds, test
runs, `uv sync`. That contends with the audio path, and on a local-speech profile it
contends with the TTS and STT models directly. Local speech once defaulted to 1 for that
reason (dropped 2026-10-15: the user wants 3 everywhere), which makes the measurement in
phase 1 matter more: run three jobs and watch first-token and first-audio
latency on the same call. Three is the decided default; if three jobs move voice latency by
more than about 150 ms, that goes back to the user with the numbers, rather than the
default quietly changing.

**Process health.** P0 proved two instances at once for Hermes (ACP and gateway), omp, pi,
Claude Code and Cursor. Three is untested, and Hermes's session database is the obvious
place for a third to trip. Re-run that spike at three before raising anything.

## Keeping jobs off each other's files

Today the foreground agent is told "It owns the files in this folder, so don't edit them
yourself" (`JobManager.context_block`). With two jobs there is nobody to tell.

**What does not work.** Advisory locks in a shared checkout cannot be enforced: the job is
an opaque CLI and sleeptalk never sees its writes. A lock would be exactly as strong as
the sentence already in the prompt, for a lot more code. And a worktree per job is not a
sandbox either — during P0 testing Hermes overwrote `~/Documents/todo-app`, well outside
the folder it was given. Any design that assumes confinement is wrong.

**What does work: admission by declared scope.** The handoff tag grows one attribute:

```
<job title="valuation" scope="read">…</job>        research, reading, measuring — no writes
<job title="bug fix" scope="repo">…</job>          changes files in the workspace
<job title="todo app" scope="new">…</job>          builds something new, in its own folder
<job title="refactor" scope="worktree">…</job>     changes files, wants its own copy
```

`read` and `new` jobs never conflict and start immediately whatever else is running. Two
`repo` jobs cannot both run: the second queues. On the call that prompted this, the
valuation was `read` and would have started at once; the bug fix was `repo` and would have
queued behind the latency run — unless the caller said "do it in its own copy", which is
what `worktree` is for.

**Worktree scope.** `git worktree add` into a sleeptalk-owned path, the job works there on
its own branch, and it reports the branch and path in its result. The project already
works this way by hand (`../sleeptalk-bgjobs`, `../sleeptalk-jobui`). It is opt-in rather
than the default for four reasons: a fresh worktree needs its dependencies installed
before anything runs (`uv sync --extra discord --extra local …`, minutes and gigabytes per
job); untracked files the agent needs — `.env`, `.claude/settings.local.json`, local keys —
are not copied by `git worktree`; the workspace is often not a git repo at all, since
`workspace_dir` can be any folder; and the result lands somewhere the caller did not ask
for, so "where is it?" gets a surprising answer. Make the caller opt in and those costs are
a choice rather than a tax on every job. If the workspace is not a repo, `worktree` falls
back to `repo` and says so.

**The commit gotcha.** Two workers in one checkout plus `git add` equals one worker's
unreviewed hunks inside the other's commit — this has already happened once in this repo.
The fix is one line in `JOB_CONTRACT`: do not commit; leave the changes in the working
tree and list the files you touched. Worktree-scope jobs are exempt. This also makes the
foreground agent's job easier, because it can see and describe what changed.

## Steering by voice: the hard part

Jobs are addressed by their title. `HANDOFF_INSTRUCTION` already asks for a two-or-three
word name ("todo app"), and `context_block` already carries it into every foreground turn.
Three things make it work with more than one.

**1. Titles must differ.** `JobManager.start` checks the new title against every live and
queued job; on a collision it appends a distinguishing word from the brief ("todo app" and
"todo app tests"), and the context block lists the taken names so the agent picks
differently next time.

**2. Routing order.** A note, a cancel or an answer is aimed at a job by these rules, in
order, stopping at the first that fires:

1. The caller named it — any distinctive word of a title matches ("kill the latency run").
2. The caller is replying to an announcement: within about twenty seconds of a line that
   named one job, an unaddressed note goes to that job — even a job that just finished,
   which then runs the note as its follow-up. This is what makes "actually, make it blue"
   work right after "the landing page is done" while another job is still running, so it
   comes before rule 3.
3. Only one job is active. This is today's behaviour, unchanged, so a single-job call
   feels exactly as it does now.
4. The words answer a question and exactly one job is waiting. Guaranteed by the
   one-asker-at-a-time rule below.
5. Otherwise, the selected tab (held on the host; see the decisions above).

**3. The two guards on rule 5.** A cancel that reached rule 5 is confirmed first, in one
line naming the job: "Cancel the latency run?" Nothing is touched until a yes. A note that
reached rule 5 (or rule 2, with several running) goes straight through, but a line names
the job it went to ("That's for the latency run job."), so a wrong pick is heard and
undone in the next breath.
The reasoning: a mis-routed cancel costs 45 minutes of work, a mis-routed note costs one
correction. If the routing eval shows notes landing on the wrong job often, rule 5 for
notes becomes a "which one?" question instead.

**The cancel confirmation takes the asking floor.** "Cancel the latency run?" and a job's
permission request both wait for a "yes", so they cannot both be open. A bare cancel while
a request is open is answered with that request again ("First, the site job is waiting for
your answer: …"), and the caller asks to cancel once it is answered; a job's request that
arrives while a confirmation is open is held and asked once the confirmation is answered
or lapses (any reply other than yes or no lapses it). Otherwise one "yes" could approve a file write the caller meant as
"yes, cancel it".

There is one exception: "stop everything" / "cancel both" / "cancel all" cancels every
running and queued job with no question. It needs to exist so the caller has a
sledgehammer, and it is unambiguous by construction.

`cancel_meant` stays as it is — "stop" on its own still means stop talking — and with
several jobs its "name the job" requirement is doing double duty as the routing key.

**Markers.** `<job-note>` and `<job-cancel/>` grow an optional `job=` attribute:
`<job-note job="todo app">make it blue</job-note>`. Absent, rules 2–5 apply, so every
existing marker keeps working.

**Status questions.** `_JOB_STATUS_RE` and `jobs.status_line()` answer without a model
turn, and they should keep doing that. For two jobs the line names both; for three or more
it says the count and names the two worth naming — a waiting job first, then the most
recently changed — and ends with "ask me about any of them by name".

## Announcements with two jobs finishing at once

Less work than it looks: `Session.announce` already queues lines and
`_deliver_announcements` plays them one at a time, each waiting for `_wait_for_quiet` (no
turn running, no caller speech, a gap either side). Two jobs finishing in the same second
already cannot talk over each other or over the caller. Three gaps remain.

**Coalescing.** Two separate "done" turns separated by a quiet gap sounds broken. When the
announcer wakes and finds more than one job-ending line waiting, it says one: "The todo app
and the valuation are both done — links are in the chat." The existing callable-line
pattern (`_ending_line`) already lets a line decide at the last moment that it has nothing
to say, so this fits.

**Priority.** A job blocked on a permission is burning wall-clock; a finished job is not.
`announce()` takes a priority, and waiting-for-you jumps ahead of done. This is also what
enforces one asker at a time: while a request is outstanding, a second job's request is
held, and if that job carries on by itself (Cursor answers its own, hence `_ASK_GRACE_S`)
the held one is simply dropped.

**Staleness.** A done line that has waited more than a couple of minutes because the
caller kept talking should fold into the next status reply rather than arriving out of
nowhere. `job.told` already does half of this. Not in phase 1: coalescing already folds
several waiting done lines into one.

**The invariant that holds it together:** every spoken job line names its job. "The todo
app job is done", not "it's done". That is what makes routing rule 4 safe.

## What the surfaces look like

**Worker panel** (`rendezvous/web/worker_panel.ts`). The feed is already a list and
already draws a divider per job when there is more than one, so the body is close to right.
The header and the pill are not: both read `this.latest()`, so with two running jobs the
title, the status and the timer all describe one of them arbitrarily, and Cancel cancels
whichever the session thinks is current.

- Header becomes a row of chips, one per job, each with its own dot (attention / live /
  done) and elapsed time. The selected chip's feed shows below. Selection follows the
  decided rule: the first job is selected, a new job never takes it, and a cleared tab
  hands it to the job needing attention, else the newest live one. The page reads it from
  the snapshot and changes it with `selectJob`.
- Cancel moves inside the selected job's header and carries its id. The two-tap arm stays.
- Pill, wide screen and phone alike, is one line and must stay one line: a count, then
  one job. "3 jobs · Needs you · todo app" when any waits; otherwise the selected job, as
  decided: "3 jobs · research". The dot shows the worst state across all of them, so
  attention beats live beats done.
- Phone sheet: the same chips across the top, horizontally scrollable, feed below.
- Queued jobs appear as a chip with a "queued" dot and no feed, so a caller can see and
  cancel something before it ever starts.
- `_WORKER_JOBS` in `presentation.py` goes from 3 to about 8 (three live, queued ones, and
  unread finished ones, which do not count against the three). Watch the snapshot: data-channel snapshots over 64 KB were silently
  dropped and froze the call page once already; `fit_snapshot` and `_FINISHED_JOB_KEEP`
  handle it, but three live feeds of 200 entries is new territory and needs a test.

**Terminal** (`sleeptalk/transports/terminal_visual.py`). It already appends one line per
running job, but with no title, so two jobs produce two anonymous `worker` lines. Prefix
each with its title, keep at most two, and add "+1 more" below when there are more:

```
worker · todo app      writing the tests (12 steps)          0:42
worker · latency run   analyze.py · reading the run          31:18
                       +1 more
```

**Discord** (`live_session.py:_on_job_event`) already posts per job and names the title in
each message, so it needs nothing beyond the title being distinct.

## Phases

**Phase 1 — three jobs, named, with a queue.** The smallest change that fixes the call
that asked for this. No scopes yet: three jobs of any kind may run, with the existing "it
owns the files" sentence rewritten to name each. Because several jobs may now write to one
checkout, "jobs do not commit" goes into `JOB_CONTRACT` here, not in phase 3. The
selection exists on the host from this phase (first job selected, never stolen), so voice
routing is complete before the page can show tabs. Ship this alone if nothing else gets
built.

- `sleeptalk/jobs.py`: `JobManager` holds a list rather than one job and one task —
  `start` queues past the limit instead of returning `BUSY_LINE`, `note`/`cancel`/
  `answer_permission` take an optional job, title collisions are resolved, `status_line`
  and `context_block` cover N, the asking floor is enforced (cancel confirmations
  included), follow-ups take over their finished job's slot, the host-side selection is
  kept here, and the routing rules above live here as a `resolve(text) -> Job | None`
  helper. `_TAG_RE` (here too) gains the optional `job=` attribute.
- `sleeptalk/config.py` and `sleeptalk/policy.py`: `max_background_jobs: int = 3` (every
  profile, local speech included), carried through `VoicePolicy.from_config`. `background_jobs: bool`
  stays as the on/off switch.
- `sleeptalk/voice_policy_runtime.py`: routing in `_apply_job_actions`, the cancel
  confirmation and the job-naming acknowledgement, "stop everything".
- `sleeptalk/routes/cascade.py`: `_job_reply` answers status and permission for N jobs.
- `sleeptalk/session.py`: announcement priority and coalescing.

**Phase 2 — the surfaces.** Without this, two jobs run but the caller can only see one.

- `rendezvous/web/worker_panel.ts`, `rendezvous/web/main.ts`, the panel's CSS, and the
  `#preview-frame?worker` preview path (use it instead of synthetic build jobs).
- `rendezvous/web/settings.ts` and `sleeptalk/web_voice/session.py:1105`: `cancelJob`
  carries a job id, and a new `selectJob` setting sends tab changes to the host's
  selection; the snapshot carries the selection back, so two open pages agree.
- `sleeptalk/presentation.py`: `_WORKER_JOBS`, a `queued` phase, snapshot fitting.
- `sleeptalk/transports/terminal_visual.py`: titled worker lines.
- Remember the two build steps this repo keeps catching people with: `vite build` after
  any web change, and a cloud worker redeploy before remote calls see it.

**Phase 3 — scopes and isolation.** `scope` on the handoff tag, admission by scope, the
worktree-scope exemption from "do not commit" (which itself shipped in phase 1), and
`scope="worktree"` creating and reporting a worktree. Files:
`sleeptalk/jobs.py` (the two prompt constants and admission), plus a small
`sleeptalk/job_workspace.py` for worktree creation, reporting and cleanup at call end when
nothing changed.

**Phase 4 — cost and load.** The 45-minute soft notice, the
spoken totals, and whatever the phase-1 latency measurement says about three.

## What would prove it

Unit, in `tests/runtime/test_background_jobs.py`:

- Two jobs run at the same time and each gets its own result; a third queues and starts
  when one ends; a queued job can be cancelled before it begins.
- A note naming a job reaches that job and not the other.
- A cancel naming nothing, with two active jobs, touches neither and asks to confirm the
  selected one by name; only a "yes" cancels it. A note naming nothing goes to the selected
  job and the acknowledgement names it. These are the tests that matter most.
- A new job does not move the selection; clearing the selected tab moves it to the job
  needing attention, else the newest live one.
- With a permission request open, a bare cancel's confirmation waits; with a confirmation
  open, a job's permission request waits. One "yes" never resolves both.
- A follow-up to a finished job reuses its tab and counts against the three.
- The announcement-referent rule: a note within the window after a job's line goes to that
  job.
- "Yes" with one waiting job resolves that permission; a second job's request is held
  while the first is outstanding, and dropped if that job moves on.
- Colliding titles are renamed; the context block lists the taken names.
- `status_line` for one, two and four jobs.
- "Stop everything" cancels all, running and queued.

Presentation and surfaces: `tests/test_presentation.py` (two feeds stay separate, queued
phase renders, `_WORKER_JOBS` trimming, and a snapshot with three full feeds stays under
64 KB after `fit_snapshot`); `tests/test_terminal_frame.py` (titled lines, two plus
overflow); `tests/runtime/test_web_call.py` (`cancelJob` with an id cancels only that job).

Beyond unit tests, two measurements that no test can stand in for:

- **The three-at-once spike**, as P0 did for two: Hermes (ACP and gateway), omp, pi, Claude
  Code, Cursor. Does a third instance start, and does anything lock?
- **Voice latency under load**, via `scripts/synthetic_call`: first-token and first-audio on
  a call with zero, one, two and three jobs running real tool work, on a remote-speech
  profile and a local-speech one. Three is decided; this number says whether it costs the
  call its feel, and whether local-speech profiles need fewer. Either finding goes back
  to the user.

And a routing eval, in the shape of the handoff evals already in the first plan: scripted
calls with two jobs live where the caller says ambiguous things ("cancel it", "make it
blue", "how's it going", "yes"), scoring how often the right job was picked and how often
it asked when it should have just acted.

## Risks worth saying out loud before building

1. **Voice latency is the thing that can get worse.** Everything else here costs money or
   confusion; this costs the product its feel. Three is decided; measure it in phase 1 and
   take the numbers back to the user if three hurts.
2. **Nothing actually confines a job.** Scope is a declaration an agent may ignore, and at
   least one agent already has — Hermes wrote outside its workspace during P0. Two jobs
   can still land on the same file. The honest mitigation is "jobs don't commit", so a
   collision shows up as a messy working tree rather than a bad commit.
3. **Asking too often is worse than queuing.** If the ambiguity question fires on half the
   notes, the caller will hate it more than they hated waiting. The eval is what catches
   this; the fallback, if it fires too often, is to route unaddressed notes to the most
   recent job and keep asking only for cancels.
4. **A mis-routed "yes" approves something nobody approved.** One asker at a time is the
   guard, and it must hold even when a job asks while another is mid-question.
5. **Cost, with no cap by standing decision.** Three uncapped sessions can run for hours.
   The soft notice and the visible timers are the whole defence; if the user ever wants
   more, it should be a prompt, not a kill.
6. **Snapshot size.** Long calls already froze the page once over the 64 KB data-channel
   limit. Several live job feeds push straight at it.
7. **The context block grows per job.** It is on every foreground turn. One line per extra
   job, no more, or every turn gets slower and dearer.
8. **The queue can surprise.** "I'll start that after the latency run" is a worse answer
   than the caller expects if they thought jobs were now parallel. The spoken line has to
   say why, and the panel has to show the queued job, or it will feel like the old refusal
   with extra steps.
