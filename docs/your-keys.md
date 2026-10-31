# Where your keys live

Sleeptalk is software you run, not a service you sign up for. There is no
Sleeptalk account, and no Sleeptalk server ever receives a key. This page
says exactly what happens to a key from the moment you enter it.

## What happens to a key you enter

1. **Checked with its provider.** Sleeptalk sends the key to the provider
   that issued it, and only there, to prove it can listen and speak: it
   transcribes a short bundled clip and speaks one line, which uses a little
   credit. A key that fails is not kept.
2. **Written to a private store on the host.** The host is the machine
   running Sleeptalk: your laptop, or the VPS your agent lives on. By
   default the store is `~/.local/share/sleeptalk/credentials`, a directory
   and SQLite file readable only by your user. Nothing encrypts that file
   beyond the operating system's permissions, so anyone who can run programs
   as your user can read it. That is the same trust the agent itself already
   has. On a Mac the folder is excluded from Time Machine, so backups don't
   carry plaintext keys; after restoring onto a new machine you enter them
   again. If you install the `credentials` extra (`sleeptalk[credentials]`)
   before the first key is saved, the store keeps values in the macOS
   Keychain or the Linux Secret Service instead. An existing file store can
   move there later: **Move keys** on the settings page's Keys page, or
   `sleeptalk host credentials move-to-keychain`. The trade-off: calls then
   need you logged in to that computer, and macOS may ask once to allow
   access after Python is upgraded, which a call started from your phone
   can't answer. That is why the file is the default. The Keys page says
   which store yours uses.
3. **Referenced from the profile.** A profile under `config/profiles/`
   names the store entry, never the key, so profiles are safe to read,
   copy, and show to an agent.
4. **Used at call time.** The running session reads the key from the store
   and sends it to the provider with each request. It goes nowhere else.

Sleeptalk never writes a key to a log, a receipt, a transcript, or a chat
message. As a backstop, every log line is masked for anything shaped like
a secret (`token=` in a URL, `Authorization` and `Bearer` values, Discord
bot tokens, `sk-` style keys) before it reaches the console or the log
file, and the logs folder is readable only by your user. A custom
`base_url` for a speech provider must use `https://`; plain `http://` is
accepted only on `127.0.0.1` or `localhost`, so a key never crosses a
network unencrypted. Agent gateways (Hermes, OpenClaw) need `wss://` or
`https://` too, unless they are on this machine or on your tailnet. `sleeptalk host` receipts for agents carry the source of a key
("found in `~/.hermes/.env`"), never its value.

## The pages you type a key into

- **The settings page** (`sleeptalk`, or `sleeptalk settings`) is a small web server
  Sleeptalk starts on your own machine, bound to `127.0.0.1` behind a
  token in its link. The browser talks to that process and to nothing else.
  The key goes straight from the page into the store described above.
  The link opens the page in any browser on this machine, and the page
  stays open until you stop the command.
- **When an agent asks you for a key** (`sleeptalk host input link`), the
  same settings page opens on just that request: the services it asked
  about, the key, the voice, and for a Discord bot its steps. On the host's
  own browser it is served as above, except that the agent has seen this
  link: it opens one session, in the first browser that uses it, and that
  session ends after 30 minutes without use.
- **On a phone**, used when the host is a VPS with no browser (an agent's
  request, `sleeptalk settings --phone`, or Advanced settings during a
  call), the page is the one path that crosses the internet. The link's
  fragment, which browsers never send to a server, carries a public key of
  the host's. The page opens a sealed channel to the host with it, and
  everything after that travels inside: every request the page makes, the
  keys, the voice you pick, the check results, the icons and voice samples,
  and for a Discord bot its name, your servers, channels, and your user ID.
  The hosted relay forwards ciphertext it cannot open, replay, or reorder,
  and keeps no copy. On the host the requests reach an ordinary settings
  server on `127.0.0.1`. The link is single use: it opens one session, in
  the first browser that uses it, and that session ends after 30 minutes
  without use. It expires in ten minutes if nobody opens it. The hosted service also
  serves that page, so this holds while it serves this repository's code;
  see [SECURITY.md](../SECURITY.md).
- **The terminal setup** (`sleeptalk init`, or `sleeptalk settings` without
  `--open`) keeps speech, realtime, and Discord keys in the same store and
  writes references to the profile, removing any older plain-text copy
  from `.env`. Tokens for a Hermes dashboard, OpenClaw, or your own agent
  go to the same store; an older copy in `.env` still works and moves to
  the store the next time you save that profile's agent. To pass a key to
  `sleeptalk init` without it showing in `ps` or your shell history, give
  the flag `-` (for example `--speech-key -`): it asks at a hidden prompt,
  or reads the key from stdin when piped.
- **A key another app already has** (OpenRouter, Venice, Groq, Together)
  is only used when you say so. `sleeptalk host ensure` names what it found
  and where, and the agent asks you; the settings page looks only after you
  click **Look on this computer**, and remembers that choice until you click
  **Stop looking**. The places it looks are the environment, `~/.hermes`,
  `~/.openclaw`, `~/.pi/agent`, `~/.omp/agent`, and Sleeptalk's own model
  profiles, and only files your user owns. Looking notes where a key is,
  never its value. Once you choose one, it is read, proved with the provider,
  and copied into the store; the original stays where it was, so rotating
  it there does not update Sleeptalk's copy. It never passes through the
  agent's chat.
- **Subscription logins** (Claude, ChatGPT, Cursor) are never used.
  They belong to the vendor's own app. Sleeptalk takes API keys only.

## Replacing and removing keys

When you replace a key, Sleeptalk removes the old one from its store unless
another profile still uses it. Deleting a profile removes the keys no other
profile uses. The Keys page lists every stored key with the profiles using
it, and **Remove unused keys** clears the rest. In a terminal:

```bash
sleeptalk host credentials list                               # references, providers, who uses them; never values
sleeptalk host credentials prune --confirm-local-deletion     # remove keys no profile uses
```

"Uses" means named by a profile in `~/.sleeptalk/profiles`, `$SLEEPTALK_HOME`,
or a repo you ran `sleeptalk trust` in. A profile kept somewhere else and
started with `--config` isn't seen, so prune before relying on one there.

## What Sleeptalk cannot protect

Removing a key from Sleeptalk's store does not revoke it: do that in the
provider's dashboard. A key stored on a host you share with other people,
or on a VPS whose root you do not control, is only as private as that host.
If you think a key has leaked, rotate it at the provider first.
