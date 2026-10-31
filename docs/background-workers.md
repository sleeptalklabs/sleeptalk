# Keep talking while agents work

Sleeptalk is designed for voice conversations around work that takes time.
Hand off research, a build, or a larger change, and keep talking while it runs.
The conversation remains available for questions, decisions, and more tasks.

## A voice workflow

1. Give a clear task: “Research these two approaches in the background and
   give me sources.” The agent hands a self-contained brief to a worker.
2. Keep talking: “While that runs, help me think through the tradeoffs.”
   A new conversation turn does not by itself cancel that worker.
3. Start independent work: “Also build a small demo in a separate folder.”
   Another worker can run alongside the research.
4. Ask for progress or refine a job: “How is the research going?” or
   “For the research, include the maintenance costs.” Name the job when
   several are active.
5. Hear the result when the conversation is quiet. Links, paths, and details
   appear in the surface's text output; the browser has a worker panel.

Workers use fresh sessions of your configured agent, with the same profile and
workspace. This is Sleeptalk's own handoff mechanism. An agent's native
sub-agents may also appear in the roster, but their controls depend on what
the adapter reports.

## Which calls support several workers?

The published multi-worker path is **Cascade**, including Local speech, with an
agent that has a worker factory. Background jobs are enabled by default. “Just
a model” has no tools or background workers; command adapters also have no
Sleeptalk worker factory. Custom integrations still need their own checks.

Three jobs can run at once by default, and additional jobs queue. Each worker
uses your agent and model account, so concurrent jobs add model usage and can
compete for CPU while tools build or test. Reduce concurrency if local speech
stutters under load:

```toml
[policy]
background_jobs = true
max_background_jobs = 3
```

Set `background_jobs = false` to disable these handoffs. A handoff still
depends on the foreground agent following the voice instructions. This does
not guarantee that every long request will immediately move to the background.

[GPT Realtime](realtime-hybrid.md) has a single specialist task path. The
[GPT Live browser preview](gpt-live.md) uses the same background-job limit on
eligible agents when jobs are enabled; otherwise it falls back to one backend
task at a time. Its live microphone and long-call checks remain pending.

## Steer and stop a specific job

Ask for status, add instructions, or request cancellation using the job's
title. If several targets are possible, clarify which one. Steering uses the
adapter's control when available; otherwise the note waits for a follow-up
turn. A note to a finished job can start follow-up work.

Speaking over the voice stops or changes presentation according to the route;
it does not prove the worker stopped. Use the browser worker panel's Cancel
control or explicitly name the job to cancel, then check its status.
Cancellation cannot undo actions that already completed. Permissions and
clarifications still depend on the selected agent's capabilities.

## Keep expectations clear

- Jobs belong to the current call and stop when it ends. They do not survive
  hangup or automatically resume in a later call.
- Fresh agent sessions are not isolated filesystems. Workers can share the
  same folder; avoid assigning conflicting edits. Ask for separate folders
  or arrange separate checkouts when work overlaps.
- Quick questions and small changes can stay in the foreground. Voice remains
  useful for discussion as well as delegation.
- Progress cues describe reported state. Check the result before treating a
  task as successful.

For the voice-route split, see [voice modes](voice-modes.md). Implementation
details live in [jobs.py](../sleeptalk/jobs.py) and
[builder.py](../sleeptalk/builder.py).
