# Discord setup

Sleeptalk joins a voice channel in your server, listens to the people you
allow, and speaks your agent's replies back. This page covers the bot
application, what it needs from Discord, and who can talk to it.

## What the wizard asks for

Run `sleeptalk --discord` (or pick Discord as the call destination). It asks
for two things and finds the rest itself:

1. **Bot token.** Open the [Developer Portal](https://discord.com/developers/applications),
   create an application, then Bot -> Reset Token. Paste it into the masked
   prompt. It goes into the private credential store, never into a file in
   your project.
2. **Who can talk to the bot.** Your Discord user ID and one voice channel.
   Everything else is optional.

From the token, Sleeptalk reads the application ID and the servers the bot
belongs to, prints the invite link, and confirms the bot has joined.

## Permissions

The invite link carries exactly the permissions the voice runtime uses:

| Permission | Why |
| --- | --- |
| View Channels | See the voice channel and its chat |
| Send Messages | Post the join confirmation and transcript lines |
| Read Message History | Slash-command context |
| Connect | Join the voice channel |
| Speak | Play the reply |
| Use Voice Activity | Listen without push-to-talk |

Re-inviting a bot that is already in the server updates its permissions in
place. Nothing else is lost.

## Privileged gateway intents

Voice needs none. Leave Presence, Server Members, and Message Content off
unless you turn on one of these features:

| Feature | Setting | Intent |
| --- | --- | --- |
| Text summons (`@bot join` in chat instead of the slash command) | `voice_only = true` | Message Content |
| Rooms with other bots whose posts the agent should read | `participation_mode = "attendee"` or `"proactive"` | Message Content |

Sleeptalk requests Message Content only when one of those is set, and
`sleeptalk doctor` names the toggle to enable if Discord refuses the
connection. Speakers in a voice channel are identified through the voice
websocket, so the Server Members intent is never needed.

## Who can talk to the bot

The wizard's default is a closed list: you, in one voice channel. It writes
these fields to the profile:

```toml
[transport]
type = "discord"
authorization_required = true
operator_ids = "123456789012345678"
allowed_channel_ids = "1234567890123456789"
```

With `authorization_required = true`, the bot enforces the list at every
boundary:

- Slash commands and text summons from anyone else are rejected, quietly.
- Speech from an unlisted user is dropped before it reaches speech-to-text.
- The bot only plays audio while everyone in the channel is on the list.
  If someone else joins, playback stops and stays stopped for that session.
- Only an operator can answer a tool permission request from the agent.

Add people with the three lists, all comma-separated user IDs:

| Field | Can |
| --- | --- |
| `operator_ids` | Summon the bot, talk, and approve agent tool permissions |
| `speaker_ids` | Talk to the agent |
| `listener_ids` | Be in the channel while it speaks |

Find a user ID with Developer Mode on (Settings -> Advanced), then
right-click a name -> Copy User ID. Slash commands must be issued from the
allowed voice channel's chat, since that chat shares the channel's ID.

A profile without `authorization_required` is closed, and the bot refuses
to start until `operator_ids` and `allowed_channel_ids` are set.

Choosing "Anyone who can join its voice channel" in the wizard writes
`authorization_required = false`. Then anyone in the server can summon the
bot and talk to your agent, which has whatever access your agent has to the
workspace, and the bot prints a warning at startup. Use that only in a
server you fully control. `allowed_channel_ids` still limits which voice
channels the bot joins.

A speaker can ask the agent for anything its permission mode allows without
a prompt. Only operators can answer the prompts that do come up.

## Set up by your agent

An agent that already talks to you in the browser can set up the Discord
bot too, from a phone link, without ever seeing the token:

```bash
python -m pip install 'sleeptalk[discord]'
sleeptalk host input link --request-id <id> --profile discord --surface discord \
  --speech-from-profile default --workspace /path/to/workspace --agent hermes
```

The page walks you through the four steps a first-time bot owner needs:
create the application and copy its token, add the bot to your server with
the invite link, pick the one voice channel it may use, and enter your own
Discord user ID so nobody else can talk to it. Your speech key is reused
from the `default` profile, never re-entered. When `host input wait`
reports `profile_ready`, the agent installs the profile as a service
(`sleeptalk service install discord --yes`) and starts it. Then join the
voice channel and type `/discord join` in any chat in that server.

## Running it

```bash
sleeptalk --discord
```

In the voice channel's chat, use `/<slug> join`. The bot joins your channel
and greets you. `/<slug> leave` disconnects it. To keep it running across
logins, see [Deployment](deployment.md).

### Leaving it in the channel

After `/<slug> join` the bot stays in that voice channel until
`/<slug> leave`, even when everyone else has left. It sits there silent:
nothing is transcribed and nothing costs money while nobody talks. Come
back hours or days later and talk; it answers with the same conversation.

- **Its hearing restarts itself.** A Discord voice reset can stop the
  receiver while the bot stays in the channel. The bot checks every 30
  seconds, and again when someone walks in, and starts listening again.
- **It goes back to its seat.** If Discord drops it from voice, or the bot
  restarts (a crash, an update, the computer rebooting), it rejoins the
  same channel silently, without a greeting. After three rejoins in 30
  minutes it stops trying and waits for `/<slug> join`; that is also what
  happens if someone keeps removing it.
- **It keeps the conversation.** With pi, oh-my-pi, or Hermes, the
  conversation is saved (under `~/.sleeptalk/discord-sessions/`) and picked
  up again after a restart. `/<slug> reset` starts a fresh one; pi and
  oh-my-pi keep the old one next to it, Hermes in its own history.
- `/<slug> status` says whether it is listening and which channel it stays in.

Profiles with `voice_only = true` still leave an empty channel and come back
when you summon them.

### Reading the call in the channel

A call is not only audible. As each answer finishes, the bot posts it as a
message in the voice channel's own chat, so you can scroll back through the
conversation, click a link it read out, or catch up on a turn you misheard.
It is on by default:

```toml
[transport]
type = "discord"
post_replies = true        # the bot's answers, one message per finished turn
post_caller_speech = false # and what you said, above each answer
```

The words are the same ones the web call page shows in its transcript: they
come from the call's own event stream, so a reply reads as one message with
its phrases joined, a cut-off answer is marked with a dash where the audio
stopped, and a background job's announcement lands in the channel too. A
reply longer than Discord's 2000-character limit becomes several messages,
split at a line or sentence break. Nothing the bot posts can mention or
ping anyone.

Two things never change with this on. The bot writes only into the voice
channel's own chat, never a server-wide channel: on an authorized profile
every member of that voice channel is already on your list, so the written
call reaches exactly the people the spoken one does, and it stops writing
the moment someone unlisted walks in. And posting is queued, never awaited
by a turn — a slow, rate-limited, or forbidden channel cannot delay what
you hear. If the bot cannot post at all (Send Messages revoked), it gives
up after three tries and the call carries on in voice.

Set `post_caller_speech = true` to see your own side as well. It is off by
default: on a shared channel it doubles the volume, and you already said
the words out loud.

## Troubleshooting

- **Doctor says intents were refused (close 4014).** The profile enables
  text summons or rooms. Enable Message Content in the Developer Portal, or
  turn that feature off.
- **The bot joins but does not hear you.** Your user ID is not in
  `operator_ids` or `speaker_ids`, or you are in a channel that is not in
  `allowed_channel_ids`.
- **It speaks and then goes silent when someone joins.** That person is not
  listed. Add them to `listener_ids` and rejoin.
- **Opus errors on start.** Install libopus (`brew install opus` or
  `sudo apt install libopus0`) or set `opus_library` in the profile.
- **Nothing appears in the chat.** Check Send Messages on the voice channel
  (re-invite the bot to refresh its permissions), and that the profile does
  not set `post_replies = false`.
