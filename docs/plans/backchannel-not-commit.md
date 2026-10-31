# The acknowledgement as a backchannel, not a turn boundary

Decided on a call, 2026-10-15. Not built.

## What the caller noticed

Sometimes the acknowledgement chimes in while they are still talking. Their reaction was
not "that's broken" — it was that it *sounds good*, close to real full duplex. A person
listening says "mm, right" over you without claiming the floor.

So the overlap is not the bug. The bug is what the overlap currently implies: the turn was
committed, so the agent answers half a thought.

## Why it happens today

The local Smart Turn scorer flushes a sentence that sounds finished about 250 ms after the
last word (`_score_early_pause`). Measured on 2026-10-15, that path takes **67% of turns**
— see `logs/discord-endpoint-timing-2026-10-15/README.md`. A mid-thought breath on a
grammatical clause reads as the end of the turn, the turn commits, and the 1.2 s filler
timer then fires an acknowledgement over the caller's next words.

## The change

Separate two things that are currently one:

1. **A backchannel** — a short "mm", "right", "got it", played over the caller while they
   are still going. It claims no floor, commits no turn, and starts no agent request.
2. **A commit** — the turn boundary that sends words to the agent.

An early-pause score high enough to speak a backchannel is *not* high enough to commit.
Commit needs either a real silence or a second confirming signal.

## Rules

- A backchannel never commits the turn, never cancels a pending commit, and never counts
  as the agent taking the floor.
- If the caller resumes within the grace window, the words append to the same turn. The
  backchannel stays spoken — it was honest, it just meant "I'm listening".
- **Do not manufacture them.** The caller was explicit: it should keep happening as
  naturally and as rarely as it does today — once in a while, not every turn. Nothing new
  fires a backchannel; the only change is that when the existing acknowledgement happens
  to land over the caller, it no longer commits the turn. Adding a deliberate "mm" on a
  schedule is out of scope and was ruled out by the caller.
- One backchannel per turn at most. Two is a tic, three is an impression of a person.
- Backchannels are short and quiet: a lower gain than the answer voice, no sentence that
  could be mistaken for an answer, never a question.
- Never over the first word of a turn, and never while the caller is mid-word; only in a
  pause the scorer already found.
- Keep the existing rule that short caller acknowledgements ("yeah", "okay") get no
  response at all — a backchannel answering a backchannel is a loop.
- Barge-in still wins: if the caller talks over the real answer, the answer is cut.
- Off by default on Snappy? To decide with a measurement: Snappy already commits sooner,
  so backchannels there may feel like interruptions.

## What would prove it

- A synthetic call where the caller pauses mid-clause for 400 ms then continues: exactly
  one backchannel, one committed turn, with the full text of both halves.
- The same clip with the old behaviour: two turns and a half-answer. That contrast is the
  regression test.
- A real call where the caller trails off and stops for good: the backchannel must not
  leave the call silent waiting for words that never come.

## Open

- Whether the backchannel is pre-recorded (cheapest, and the pre-made lines already exist)
  or synthesised per call for voice match.
- Which lines qualify. There is already a configured pool of acknowledgements — "yep",
  "got it", "sure" and friends, selected one per turn and never repeated back to back.
  The caller's point is that the backchannel should draw from that existing pool rather
  than introduce a single new "mm-hmm". Worth checking whether every line in the pool
  reads well *over* speech, since the pool was written for lines played into silence;
  anything that sounds like it is claiming the floor ("okay, so…") should be excluded
  from the overlapping case while staying in the normal one.
