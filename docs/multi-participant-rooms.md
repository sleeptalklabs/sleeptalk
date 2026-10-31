# Multi-participant voice rooms

A voice room is a roster plus a per-bot speak decision. One-on-one is the
case with one human conversationalist and no other spoken humans or foreign
bots. That path still sends the raw transcript to the agent.

## Modes

Set these on `[agent]` in the profile TOML. Omitted keys stay
conversationalist.

```toml
[agent]
name = "Notes"
participation_mode = "attendee"
addressable_name = "note-taker"
proactive_triggers = ["flash crash"]
```

- `conversationalist` answers every human turn unless the turn names
  another noticed participant.
- `attendee` records everything and speaks only when named or when a
  scripted resolver marks the turn as directed at it.
- `proactive` is attendee plus substring matches on `proactive_triggers`.

Name match is the wake word. Indirect lines are a fixture-injected hit, not
a live model call.

## Hearing and reading

Bot Discord users never reach the opus sink. They cannot become trigger
input. Everything this process transcribes, and every other bot's voice
text echo, lands in a local transcript. Echo is reading only. Two
`sleeptalk run` processes do not share memory.

Echo posts start with `sleeptalk-voice/1` and use the speaker's display
name instead of `**You:**`. Unmarked two-line markdown is not a turn.
Reading those posts needs the Message Content intent on the Discord
application. Sleeptalk requests it automatically once `participation_mode`
is `attendee` or `proactive`; see [Discord setup](discord.md).

## What this is not

This is not the sub-agent session roster. That object tracks background
workers inside one conversation. Floor turn-taking and a live semantic
classifier are later work.
