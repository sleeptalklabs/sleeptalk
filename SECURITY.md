# Security

Report vulnerabilities privately. Do not open a public issue for a security report.

Use GitHub private vulnerability reporting on this repository:

https://github.com/sleeptalklabs/sleeptalk/security/advisories/new

Include the affected package or surface, a way to reproduce the issue, and the
impact you expect. We will acknowledge the report and say when a fix is in
progress.

Sleeptalk never sends provider keys, agent credentials, transcripts, or
workspace contents to the hosted call service. If you find a path that does,
that is a vulnerability. The service keeps no per-request logs and no
visitor IP addresses: its only log line per call records how long it
lasted, whether it was relayed, and why it ended.

One limit is built into any web page: the hosted service at `call.sleep.talk`
also serves the call and key-entry pages' JavaScript. Keys typed on the
phone page, and everything else about that setup (voice, Discord servers,
channels, your user ID), are encrypted to your host, and call audio, transcripts, and
agent state travel end to end between the phone and your host, but only as
long as that code is the code in this repository. A compromised or
malicious deployment of the service could serve pages that don't: such a
page could read keys typed into it, and change Sleeptalk's settings through
an open settings link. It can't start a program of its choosing on your
computer, though it can talk to your agent like any caller, and most agents
run their tools without asking by default (see "What your agent does without
asking" below). A settings page reached through the service is limited to
settings and to installing one of the agents Sleeptalk lists, with the
fixed command built into Sleeptalk for it (the official npm package, or
Cursor's own installer). It can also set up a Discord bot and start or
stop it, so an owner who only has a phone can run one on a VPS. That bot
is Sleeptalk's own command, but it keeps running after the page closes and
answers the Discord user and channel chosen on the page, with whatever
your agent is allowed to do. A malicious page could choose its own. Choosing
a program to run as your agent, moving the key store, and opening a Discord
bot to everyone are refused there. Calls
over your tailnet, or through the local rendezvous, load the pages from
your own host and don't depend on the hosted service at all.

## What your agent does without asking

A call is a conversation with your coding agent, and the agent keeps the
powers it has when you type to it. Unless a profile sets `permission_mode`
under `[agent]`, most agents run their tools (shell commands, file edits,
web requests) without asking first:

| Agent | Default when `permission_mode` is unset |
| --- | --- |
| Claude Code | `skip`: Claude Code's `bypassPermissions`, every tool allowed |
| oh-my-pi, pi, Cursor, OpenClaw, Hermes gateway, other ACP agents | `auto`: tool requests approved without asking |
| Codex | Codex's own approval requests are read out to the caller |

Profiles set up by an agent with `sleeptalk host ensure`, and Discord bots
made from a profile on the settings page, use `pending` for Hermes, pi, and
other ACP agents: the agent asks and the caller answers yes or no out loud.

Claude Code outside `pending` also loads the workspace's own settings:
`.claude/settings.json` (including hooks), `.mcp.json`, and `CLAUDE.md`. The
workspace is `workspace_dir`, or the folder you ran `sleeptalk` from when it
is unset. Don't start a call from a repository you don't trust.

So anyone who can speak on a call can ask your agent to do anything it is
allowed to do: keep call links, phone settings links, and Discord voice
channels to people you would hand your keyboard to. To be asked first, set
`permission_mode = "pending"` (Claude Code, Codex, pi, Hermes, and ACP
agents); `"deny"` refuses every tool request.

## What runs in the background

Nothing runs at login unless you ask. **Start the bot** on the settings page
(or `sleeptalk service install` followed by the start command it prints)
writes one user-level service for that Discord profile and starts it. It
runs `sleeptalk run --config <profile>` from your own Sleeptalk
installation, keeps running after the page closes, starts again when you
log in, and is restarted if it crashes. A setup problem, such as a token
Discord rejects, stops it instead of retrying.

- **macOS:** `~/Library/LaunchAgents/ai.sleeptalk.<profile>.plist`. macOS
  shows a "Background Items Added" notice the first time, and System
  Settings → General → Login Items & Extensions → Allow in the Background
  lists it as `sleeptalk` from an unidentified developer: Sleeptalk is a
  Python package, not a signed app. Its output goes to
  `~/Library/Logs/sleeptalk/<profile>.log`. **Stop** unloads it and deletes
  the file, so it no longer starts at login or appears in Login Items.
- **Linux:** `~/.config/systemd/user/sleeptalk-<profile>.service`, with
  lingering turned on so it outlives your login session. Its output goes to
  `journalctl --user -u sleeptalk-<profile>`. **Stop** disables it.

The service file holds paths (your Python, the profile, the workspace), your
PATH so the agent finds its tools, and `SLEEPTALK_HOME`. It holds no keys or
tokens; those stay in the credential store. The bot runs as you, with the
agent permissions described above. To remove one by hand:

```bash
# macOS
launchctl bootout gui/$(id -u)/ai.sleeptalk.<profile>
rm ~/Library/LaunchAgents/ai.sleeptalk.<profile>.plist
# Linux
systemctl --user disable --now sleeptalk-<profile>.service
rm ~/.config/systemd/user/sleeptalk-<profile>.service
```
