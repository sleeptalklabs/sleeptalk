# Changelog

## 0.1.0 (2026-10-31)

First public release.

- Cascade voice route: speech-to-text, your agent, text-to-speech.
- Surfaces: browser call, terminal audio, Discord voice channel.
- Agents: pi, Hermes, Cursor, Codex, OpenClaw, Claude Code, plus
  bring-your-own command, HTTP, bridge, and plugin adapters.
- "Just a model" mode for a single API key or a local Ollama with no agent.
- Speech providers: Deepgram, Cartesia, ElevenLabs, PlayHT, Rime, Hume, and a
  fully local option (Moonshine, Piper, Kokoro).
- One-key setup wizard, `sleeptalk doctor`, `sleeptalk service`, and a Docker
  image.
- Hosted browser-link service (Rendezvous) with short-lived tickets, and a
  package-owned loopback alternative.
- Browser calls over your tailnet (`--tailnet`, or `delivery = "tailnet"`
  in the profile): published with `tailscale serve`, no relay, no TURN, no
  time limit. Setup offers it once when Tailscale is ready.
- Curated voices with a golden default per provider (Cartesia Skylar,
  ElevenLabs Brittney, Deepgram Hannah), any library ID as a custom voice,
  and a Voice menu on the call page that proves the voice, switches it, and
  saves it to the profile.
- Call page settings for playback speed (1.0 by default), tool narration,
  and reply length; keyboard sounds ship in the wheel; private and tailnet
  calls have no session cap, and the hosted relay's 30-minute limit is
  announced before it ends the call.
- Agents use the Hermes you have installed, over ACP; the pinned isolated
  runtime is opt-in.
- OpenAI Realtime as a speech service in settings (terminal and the
  settings page): one OpenAI key; Realtime hears you and speaks itself,
  starts its reply before the transcript arrives, and hands work to your
  agent. Voices Marin and Cedar first, then the earlier eight. A hand-off
  no longer ends the Realtime session, and a session that drops reconnects.
- OpenRouter, Venice, Groq, and Together as speech providers: one key
  listens and speaks through their OpenAI-compatible audio routes. Defaults
  measured for latency (Parakeet listening; Grok Voice or Cartesia Sonic
  speaking; the Flux voices free on OpenRouter), replies streamed as they
  arrive, one connection kept alive per adapter, and tighter endpointing
  for batch listeners.
- ElevenLabs through an OpenRouter key: nine speaking models (Turbo v2.5,
  the quickest measured, then Flash v2.5, v4 Turbo, v4, Multilingual v2, and
  the rest) with its 21 premade voices, and Scribe v2 for listening. Grok
  Voice stays the OpenRouter default.
- Cartesia speaks with Sonic 3.6 by default, Cartesia's recommendation;
  Sonic 2 and Sonic Turbo say they retire on 20 October 2026. Together moves
  from its Sonic 2 to Sonic 3, and profiles that kept the old Together
  default follow it.
- A Discord call is readable, not only audible: each finished answer is
  posted in the voice channel's own chat, from the same event stream the
  web call page's transcript renders — phrases joined into one message, a
  cut-off answer marked where the audio stopped, a background job's
  announcement included, and anything over Discord's 2000-character limit
  split at a line or sentence break. On by default (`post_replies`); what
  the caller said is opt-in (`post_caller_speech`). An authorized profile
  writes only into the voice channel's chat, whose audience it has already
  checked, and stops the moment someone unlisted walks in. Posting is
  queued, so a slow or forbidden channel never delays what you hear.
- A Discord bot stays in its voice channel until `/<slug> leave`, silent
  but ready for days: its receiver restarts itself after a Discord voice
  reset, it rejoins its channel after a dropped connection or a restart
  (three tries per 30 minutes), wakes its speech connections when someone
  walks in, and pi, oh-my-pi, and Hermes keep the conversation across
  restarts. `/<slug> reset` starts a fresh conversation; `/<slug> status`
  says whether it is listening.
- `host ensure` finds a speech-capable key already on the host (Sleeptalk's
  store, the environment, Hermes's `.env` and config, OpenClaw's config,
  pi's login and providers files; never a project's `.env`) and offers it
  with an `ask` line for the agent; once the user says yes,
  `--reuse-key <service>` sets voice up from it with nothing to paste. The
  setup wizard offers such a key first.
- Start receipts name their `delivery` (tailnet, hosted relay, or local);
  ensure receipts carry the tailnet question when it applies, and
  `--tailnet` stores the choice on an existing profile.
- CI stamps main-branch wheels with an increasing version so a newer build
  always installs over an older one.
- Speech services that listen one turn at a time (OpenRouter, Groq, Together,
  Venice, ElevenLabs Scribe, Local) start transcribing 300 ms into a
  pause, so the transcript is usually ready when the turn ends
  (`[stt] pause_ahead_ms`, 0 turns it off). The early transcript is used
  only when nothing but quiet followed it; it never ends a turn.
- Browser calls keep half a second of audio from before the voice detector
  opens a turn, including a false start or a fragment too short to be a turn
  on its own, so one-turn-at-a-time listeners no longer lose a soft first
  word ("So", "Okay", "Can you"). OpenRouter-style requests that stall get a
  twin after 0.7 s (listening) or 0.6 s (first audio) and the first answer
  wins; an empty transcript of audio with a voice in it is asked of Whisper
  once; uploads are trimmed FLAC; connections that idled shut reopen when
  you start talking. An `early_end_shadow` latency line records, without
  acting on it, when the early transcript would have ended the turn.
- OpenAI Realtime hears a turn while it is spoken: browser and terminal calls
  send the recorded utterance in 100 ms pieces, and the commit sends only the
  tail. Turns that wait for their text while a task runs take an early
  OpenAI transcript started at the pause when it lands before Realtime's.
- `sleeptalk settings --open`: the profile as a page in the browser, every
  section down the left, compact cards for each choice; voice, speed,
  language, delivery, and behaviour save straight to the profile.
- `sleeptalk settings` in a terminal offers the same speech services, voices,
  models, speed, turn taking, and delivery as the settings page, moves models
  and voices on a language change by the same rules, and keeps settings made
  on the page when it saves.
- Private key pages (local and phone link) offer every speech provider with
  its free usage and card requirement; the user picks one there, so an agent
  never has to ask which provider to use.
- Discord bots can be set up from the phone link too: the page teaches a
  first-time bot owner every step, the speech key is reused from the browser
  profile (`--speech-from-profile`), and the bot is locked to the user's
  account.

### Security

Hardening from a pre-launch security review. The ones that change behaviour
come first.

- Discord bots are closed by default: a profile without `operator_ids` and
  `allowed_channel_ids` does not start unless it says
  `authorization_required = false`. Open bots warn at startup, never ping
  anyone, take commands only in a server, and hear other bots' room echoes
  only from their own chat.
- A repo's `config/profiles/` is used only after `sleeptalk trust` in it,
  and the working directory's `.env` is never read, so a cloned project
  cannot choose your agent command, speech endpoint, or agent environment.
- Keys other apps hold are never used without asking: `host ensure` only
  offers a found key, and the settings page looks for them only after
  **Look on this computer**. Discovery reads only files the user owns.
- Subscription logins (Claude, ChatGPT, Cursor) are refused everywhere,
  and no request identifies itself as another vendor's client.
- The docs and the settings page say where keys really are (a 0600 file by
  default; the Keychain or Secret Service with `sleeptalk[credentials]`),
  that a reused key is copied, and that checking a key uses a little credit.
- A refused Hermes gateway connection no longer quotes its token in the error.
- Every log line is masked for secret-shaped text (URL tokens, Authorization
  and Bearer values, Discord tokens, `sk-` style keys), tracebacks included;
  the logs folder is private to the user.
- A speech provider's custom `base_url` must be `https://` (plain http only
  on loopback), and agent gateways need TLS unless they are local or on a
  tailnet, so no key or token crosses a network unencrypted.
- Replacing a key removes the old one unless another profile uses it;
  deleting a profile removes the keys nobody else uses. The Keys page and
  `host credentials list` / `prune` show and clear what's left over.
- Hermes, OpenClaw, and bring-your-own agent tokens are kept in the private
  store with references in the profile, not in `.env`.
- One settings page everywhere. `sleeptalk settings --phone` and
  `sleeptalk host settings-link` open it on a phone through the sealed
  channel; the call page's Advanced settings offers it; and an agent's key
  request (`host input link`) opens it on just that request: the services
  asked about, key, models, voice, and the Discord steps. The old phone and
  local key pages are gone.
- Phone setup links seal every setup message, not just the keys: the page
  opens an AES-GCM channel keyed through the link's fragment, so the relay
  no longer sees voices, Discord servers, channels, or user IDs, and can't
  replay or reorder anything. The relay forwards only the sealed channel.
- `[agent.auth]` model keys reach only the agents that use them (`model`,
  isolated Hermes), even on paths that skip profile validation.
- `.env` values are written single-quoted and read back literally; a value
  with a line break or control character is refused instead of adding a
  line.
- `connect hermes --url` and `init --gateway-url` warn when the URL carries
  `?token=`.
- The settings page session ends after 30 minutes without use.
- The file store is excluded from Time Machine; **Move keys** on the Keys
  page (or `host credentials move-to-keychain`) moves it into the Keychain
  or Secret Service.
- Terminal setup and `host ensure` keep speech, realtime, and Discord keys
  in the private store, like the settings page; an older plain-text copy
  in `.env` is removed. Key flags on `sleeptalk init` take `-` for a hidden
  prompt.
- Call transcripts and agent state reach the phone over a WebRTC data
  channel, end to end, never through the hosted relay.
- The hosted relay charges its allowance and issues TURN credentials only
  when a browser joins, with per-installation and per-network daily
  budgets and rate limits.
- Agent permission answers follow the ACP option's kind, so "no" can never
  select an allow option, and agent file reads stay inside the workspace.
- Local page links open one session; call logs, speech scratch files, and
  debug probes are private to your user; plain `http` rendezvous URLs work
  only on loopback; the container runs as a non-root user; CI actions are
  pinned to commits.
- SECURITY.md states the one limit a web page can't close: the hosted
  service serves the call and key pages, so their end-to-end protections
  hold while it serves this repository's code.
- Browser calls judge "is the caller done?" by the words: a streaming
  listener's live transcript, or a one-turn-at-a-time listener's early
  transcript at the pause (`early_transcript`, on by default). A finished
  sentence ends at 0.65 s of silence instead of 1.8 s, and a half sentence
  ("I'd like to know about…") is no longer cut at 120 ms. Chinese, Japanese,
  Korean and Hindi sentences are judged by their own end marks.
- Talking over an answer pauses it and your words decide: a backchannel
  ("mm-hmm", "ajá", "嗯", in each of the page's languages) puts it back on;
  anything else stops it and becomes the turn, with its first words kept.
  Speech right after a turn ends, before the agent is heard, is the rest of
  that turn and is asked again with both halves.
- An agent that fails, has its login rejected, or answers nothing, and a
  speech service that fails to transcribe (a rate limit), say so out loud
  instead of leaving the call silent.
- OpenRouter, Groq, Venice and Together voices no longer cut a long sentence
  off mid-word: the speech request timed out after 20 s in total while it
  was read only as fast as it played. Only a stalled connection or read
  times out now.
- Hermes started per call answers browser calls; it no longer needs a saved
  session.
- The settings page shows the listening model, speaking model and voices a
  call will actually use, and says a reserved profile name is reserved.
- `scripts/synthetic_call/`: real browser calls with a scripted caller, across
  agents, speech services and languages, scored and timed.
- OpenAI Realtime judges the end of a turn by the pause's early transcript
  too (0.8–1.3 s instead of 1.8 s), and a soft barge on Realtime hears the
  caller's words instead of stopping outright.
- Words said while the agent works in silence ("stop", a new question) are
  heard: they interrupt the work and become the turn, where before they
  were dropped. A backchannel leaves the work alone.
- OpenAI Realtime answers simple questions itself (general knowledge,
  definitions, math, everyday explanations) instead of handing them to the
  agent, which keeps your projects, live information and work that needs
  tools.
- Talking over an answer that had only just started (within twice the resume
  grace of the turn ending, or within a second of the answer's first audio)
  is the rest of the same turn: both halves are asked again, instead of the
  first half being answered alone.
- "Wait, stop. What's the capital of France?" while a Realtime hand-off is
  running is answered. Cancelling the task also cancelled the reply being
  prepared for it, so the turn failed and the call went silent.
- Realtime also keeps "Don't search the web, just explain it", "I'd like to
  know about…", "Can you explain…" and "repeat back what I said" for itself,
  and judges a hand-off by the caller's words as well as Realtime's rewording.
- A failed Realtime turn is written to the host log with its reason.
- Fresh installs: the Docker image ships Node 22 so its pi agent runs (Debian's
  Node 20 couldn't start it); `sleeptalk --version`; the phone settings link
  prints at once under nohup, a service or a pipe; setup with no terminal
  says how to finish it instead of pointing back at itself; a missing
  sleeptalk.toml says to run sleeptalk rather than copy an example file a
  pip install doesn't have; terminal calls on Linux say to install PortAudio
  when it's missing; with no agent installed the setup menu starts on
  oh-my-pi, the default.
- OpenClaw: every call after a profile's first failed ("label already in
  use"); a call whose label is taken now gets its own session.
- Cartesia in Spanish, Chinese and other non-English languages hears the
  whole sentence: its streaming model dropped words at the end ("…la
  capital de" for "…la capital de Francia?") and whole Chinese clauses, so
  the final transcript now comes from batch (about 0.3 s), with the
  streamed text as the fallback.
- A failure line the caller hears is also shown on the call page; when the
  voice service itself fails, the page shows the reply that couldn't be
  spoken and says the voice isn't answering.
- `scripts/synthetic_call/`: failure injection (`fault_proxy.py`,
  `fault_agent.py`, `analyze_faults.py`).
- Deepgram and Cartesia end a finished sentence in about 0.3 s instead of
  1.8-3.5 s: a pause asks them to finalize the words they were holding
  back, and endpointing looks at the text again 150 ms after the voice
  stops instead of only when the long wait ran out. English calls now hear
  the reply about 1.3 s after the caller stops (was 2.7-4.3 s). Endpoint
  decisions log the score, source and transcript length they were judged on.
- Discord voice judges the end of a turn by words, as web and terminal do:
  the pause brings a batch listener's early transcript (or asks a streaming
  one to finalize). Short utterances used to end on 120 ms of quiet, so
  "I'd like to know about… the history of tea" became two turns; replies
  now start about 1.3 s after the caller stops (was about 2.5 s).
- Discord: "Mm-hmm" during an answer no longer freezes it. The barge's
  buffer was dropped as a false start, so the soft-barged answer stayed
  paused for good; it is kept now, and a soft barge with no words resumes
  after 4 s, as on the web. The barge pre-roll no longer carries the end
  of the previous question ("Just explain it. Mm-hmm.").
- Discord skips its own spoken apology when the voice itself failed (the
  route has played the voice-unavailable tone); trying held a stalled turn
  6 s longer.
- `scripts/synthetic_call/discord_session.py`: Discord voice calls without
  Discord, scored like the web harness.
- When the voice service itself fails, the caller hears a short tone instead
  of silence (the apology needs the same voice); the page shows the reply.
  A voice that sends no audio fails after 8 s instead of holding the turn
  for 30 s.
- Agents set to ask before using a tool (pi and Hermes from `sleeptalk host`,
  Claude Code or pi with `permission_mode = "pending"`) are asked out loud on
  a cascade call: "Allow Write on approved.txt? Say yes or no." The caller's
  yes or no answers it instead of interrupting the work; while it waits,
  the typing sound and "still working" lines stop and the question is asked
  once more. Before, the request had nowhere to go and the call hung on the
  agent's first write.
- "What are you doing?", "How are the subagents doing?", "Any update?"
  asked while the agent works are answered by the call ("I'm still reading
  a file", or which checks are running) and the work goes on. Asking used to
  interrupt it, cancelling Claude Code's subagents as they launched.
- "Stop" during an agent's work is heard in about a second: the agent is
  aborted alongside the cancel instead of after its stream let go (Claude
  Code took 5 s).
- `scripts/synthetic_call/`: tool, permission and subagent scripts in a
  seeded workspace (`--seed`, `--permission-mode`, `script`, `analyze_tools.py`)
  and long calls (`soak`).
- Discord setup in the terminal names the bot (its name is the `/<name> join`
  command, by the settings page's naming rule), asks before reusing a bot set
  up earlier instead of taking its token silently, asks for a new bot's token,
  and `sleeptalk settings` offers "Set up Discord" after saving a new profile.
