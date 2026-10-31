<h2 align="center">
  <img src="https://raw.githubusercontent.com/sleeptalklabs/sleeptalk/main/docs/images/logo.png" alt="" width="100"><br>
  Sleeptalk
</h2>

<p align="center">
  <strong>Built for voice. Keep talking while your agents work.</strong><br>
  Talk to Claude Code, Codex, Hermes, and other agents by voice.<br>
  Hand off long-running tasks, keep discussing ideas, and steer several background workers<br>
  without waiting for each task to finish. Open source, with cloud or local speech.
</p>

<p align="center">
  <a href="https://sleep.talk">Website</a> ·
  <a href="#get-started">Get started</a> ·
  <a href="https://github.com/sleeptalklabs/sleeptalk/blob/main/docs/README.md">Docs</a> ·
  <a href="https://github.com/sleeptalklabs/sleeptalk/blob/main/docs/your-keys.md">Your keys</a> ·
  <a href="https://github.com/sleeptalklabs/sleeptalk/blob/main/SECURITY.md">Security</a>
</p>

<p align="center">
  <a href="https://github.com/sleeptalklabs/sleeptalk/actions/workflows/ci.yml"><img src="https://github.com/sleeptalklabs/sleeptalk/actions/workflows/ci.yml/badge.svg?branch=main" alt="CI"></a>
  <img src="https://img.shields.io/badge/python-3.11%2B-3776AB" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/macOS%20%7C%20Linux-supported-555" alt="macOS and Linux">
  <a href="https://github.com/sleeptalklabs/sleeptalk/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-MIT-yellow" alt="MIT"></a>
</p>


## Features

- **Keep talking while work runs.** Hand research, builds, and tests to background workers. Ask for progress, add instructions, or start another task while the conversation continues. Three workers run at once by default on eligible Cascade calls; more queue. See [background workers](https://github.com/sleeptalklabs/sleeptalk/blob/main/docs/background-workers.md).
- **Bring your own agent.** Use Claude Code, Codex, Hermes, pi, Cursor, OpenClaw, or a [custom integration](https://github.com/sleeptalklabs/sleeptalk/blob/main/docs/bring-your-own-agent.md) (preview). Your agent keeps its tools and memory. Or use [just a model](https://github.com/sleeptalklabs/sleeptalk/blob/main/docs/model-only.md) for conversation without tools.
- **Talk from your phone or computer.** Open a browser call, use your terminal's microphone, or join a Discord voice channel. Browser calls work on the host, over your tailnet, or through a single-use hosted link.
- **Cloud or local speech.** Choose a speech service or run English speech on the host with no speech key. Reuse a compatible key when you choose; keys stay in your host's [private store](https://github.com/sleeptalklabs/sleeptalk/blob/main/docs/your-keys.md). See [Local speech](https://github.com/sleeptalklabs/sleeptalk/blob/main/docs/local-speech.md).

## Voice for work that takes time

For example: “Research these two options in the background.” Then: “While that
runs, help me think through the tradeoffs.” Later: “How is the research going?”
Workers report back through the same voice, with links and details in the call's
text output. Name a job when you want to change or cancel it.

This is a workflow for long-running tasks during a call. Jobs currently end
with the call; they do not survive hangup. Available controls depend on the
agent and voice route. Start with [background workers](https://github.com/sleeptalklabs/sleeptalk/blob/main/docs/background-workers.md).

## Get started

**Install from your terminal.** On macOS or Ubuntu with Python 3.11 or newer,
open a terminal in your project and paste:

```bash
python3 -m venv ~/.venvs/sleeptalk
source ~/.venvs/sleeptalk/bin/activate
python -m pip install sleeptalk
sleeptalk
```

This installs Sleeptalk from PyPI and opens setup. No Git or Node.js is needed.
The virtual environment keeps the install separate from your system Python; in a new terminal, activate it again with
`source ~/.venvs/sleeptalk/bin/activate`, then run `sleeptalk` from your project.
If you already have a Python 3.11+ virtual environment active, you can use just
the `python -m pip install ...` and `sleeptalk` lines.

If you need Python installed too, expand **Other installation options** below.

**Or ask your agent.** Open it in the project you want to use. It needs to run
commands on the Mac or Ubuntu host where Sleeptalk will run and have permission
to install software. You need a browser on that computer or your phone.

```text
Help me set up Sleeptalk for this project. Read https://github.com/sleeptalklabs/sleeptalk and follow its installation instructions if needed. Keep my current project as the workspace.

Then follow `sleeptalk host --help` and check that my agent is ready. Ask which device I'll call from. Use the private settings page for speech choices and credentials; ask before reusing an existing key, and never request keys in chat.

Once setup is ready, start one browser call and give me the link. Help me verify that I can speak and hear a response.
```

Your agent checks your setup, asks for any missing choices, and guides you to
your first call. Agent login and local model downloads may need additional
steps. The [agent setup guide](https://github.com/sleeptalklabs/sleeptalk/blob/main/docs/agent-setup.md) covers installation,
workspace selection, readiness checks, and phone versus same-computer calls.

<details>
<summary><strong>Other installation options</strong> · Python installer and release bundles</summary>

<br>

On Ubuntu, if the pip quickstart reports that `venv` or `ensurepip` is missing,
install `python3-venv` with your package manager, or use the installer below.

Download the wheel (`.whl`), `install.sh`, and
`constraints.txt` from the
[latest release](https://github.com/sleeptalklabs/sleeptalk/releases/latest) into
one folder. In that folder, run:

```bash
sh install.sh --constraints constraints.txt ./sleeptalk-*.whl
```

The installer brings uv and Python if needed. Use the full command path it
prints, or open a new terminal if it changes your PATH. Then switch to the
project your agent should work in:

```bash
cd /path/to/your/project
sleeptalk
```

This installs the command without a source checkout, Git, or Node.js. Only the
extras you select are installed; the constraints keep dependencies at the
versions tested for that release. Your selected agent may have its own runtime
requirements.

To try the development version, install from source (requires Git).
Choose a separate destination for Sleeptalk's source, then return to your own
project before setup:

```bash
git clone https://github.com/sleeptalklabs/sleeptalk.git /path/to/sleeptalk-source
sh /path/to/sleeptalk-source/scripts/install.sh
cd /path/to/your/project
sleeptalk
```

Replace the example paths with your own. See [Contributing](https://github.com/sleeptalklabs/sleeptalk/blob/main/CONTRIBUTING.md)
for development setup.

The pip quickstart resolves current compatible dependencies. Use the wheel
bundle and its constraints when you want the exact versions tested for a release.

</details>

`sleeptalk` opens the settings page in your browser. Setup there offers any speech-capable key already on the machine, or asks for one, plays a few voices to choose from, and asks where you want to talk; then you start a call from the page. With no browser on the machine (over SSH, or a server with no display), the same setup runs in the terminal.

> [!NOTE]
> **Early release.** 0.1 covers the setups in the [support matrix](https://github.com/sleeptalklabs/sleeptalk/blob/main/docs/launch-support-matrix.md). If yours isn't there or something breaks, open an issue.

## Choose how voice works

| Mode | Conversation and work | Where to start |
| --- | --- | --- |
| **Cascade, with cloud or local speech** | Your own agent handles the conversation. Separate listening and speaking models add voice; eligible agents can run several background workers. | [Voice modes](https://github.com/sleeptalklabs/sleeptalk/blob/main/docs/voice-modes.md) |
| **Local speech** | Moonshine listens and Kokoro, Piper, or Pocket TTS speaks on the host, with no speech key. Your agent can still use a cloud model; use a local model as well for an offline call. English only. | [Local speech](https://github.com/sleeptalklabs/sleeptalk/blob/main/docs/local-speech.md) |
| **GPT Realtime** | OpenAI handles spoken conversation and hands files, tools, and current information to your configured agent. One specialist task at a time. | [OpenAI Realtime](https://github.com/sleeptalklabs/sleeptalk/blob/main/docs/realtime-hybrid.md) |
| **GPT Live — browser preview** | Continuous listening and speech, with your configured agent doing specialist work. A separate implementation from GPT Realtime; eligible agents can run background workers while conversation continues. | [GPT Live](https://github.com/sleeptalklabs/sleeptalk/blob/main/docs/gpt-live.md) |

Local speech is a Cascade option. Browser delivery—same machine, tailnet, or
hosted link—is a separate choice from the speech mode. See the
[support matrix](https://github.com/sleeptalklabs/sleeptalk/blob/main/docs/launch-support-matrix.md) for surface and preview limits.

## How it works

```text
   you ──voice──▶  browser · tailnet · Discord · terminal
                                 │
                                 ▼
                     Sleeptalk on your machine
                     ├─ listens  (your speech service, or local)
                     ├─ asks     (your agent: Claude Code, Codex, Hermes, pi, …)
                     └─ speaks   (your chosen voice, streamed as the reply is written)
                                 │
                                 ▼
   you ◀──voice──  the reply
```

The hosted relay at `call.sleep.talk` only introduces your phone to your machine: it passes a short-lived ticket and signaling, and settings travel through it as ciphertext it cannot read. On a tailnet it isn't used at all.

## Where it runs

| Surface | How | Limit |
| --- | --- | --- |
| **Browser call** | A single-use link through the hosted relay | 30 minutes, announced before it ends |
| **Your tailnet** | `--tailnet`: published on your Tailscale network, no relay, no TURN | None |
| **Same machine** | The browser on the host, loopback only | None |
| **Discord** | A bot in one voice channel, locked to your account | None |
| **Terminal** | Your microphone and speakers on the host | None |

See [Calls over your tailnet](https://github.com/sleeptalklabs/sleeptalk/blob/main/docs/tailnet-calls.md) and [Discord setup](https://github.com/sleeptalklabs/sleeptalk/blob/main/docs/discord.md).

<details>
<summary><strong>Speech services</strong> · who listens, who speaks, what it costs</summary>

<br>

| Service | Listens with | Speaks with | Note |
| --- | --- | --- | --- |
| Deepgram | Nova-3, streamed | Flux voices | Streams words as you say them; $200 free credit, no card |
| Cartesia | Ink, streamed | Sonic voices | The default pick; free tier, no card |
| ElevenLabs | Scribe | ElevenLabs voices | The biggest voice library |
| OpenRouter | Parakeet | Grok Voice; Flux voices free | The key your agent may already have |
| Venice | Parakeet | Grok Voice | Prepaid credits |
| Together | Parakeet | Cartesia Sonic | Cents an hour to listen |
| Groq | Whisper | Orpheus | Fast, cheap listening |
| OpenAI Realtime / GPT Live | Hears you directly | The selected OpenAI voice model | Conversation plus handoff to your agent. These use separate routes with different controls; GPT Live is a browser preview. See [voice modes](https://github.com/sleeptalklabs/sleeptalk/blob/main/docs/voice-modes.md) |
| Local | Moonshine Streaming | Kokoro, Piper, or Pocket TTS | No speech key; English, on the host. Pocket's bundle also carries a non-commercial notice. See [Local speech](https://github.com/sleeptalklabs/sleeptalk/blob/main/docs/local-speech.md) |

Every service has a golden default voice and a few curated picks; any voice ID from its library works, and you can change voices mid-call from the settings wheel. Streaming services decide the end of your turn from the audio itself; the aggregators listen one turn at a time, so replies start a beat later there. See [Voice from a key you already have](https://github.com/sleeptalklabs/sleeptalk/blob/main/docs/reuse-your-key.md) and [Voices](https://github.com/sleeptalklabs/sleeptalk/blob/main/docs/voices.md).

</details>

<details>
<summary><strong>Your keys</strong> · where they live and where they go</summary>

<br>

Sleeptalk has no account and no server that holds keys. Every key you enter is checked once with its provider (a short clip and one spoken line, which uses a little credit), then written to a private store on the machine that runs Sleeptalk: by default a file only your user can read, or the macOS Keychain or Linux Secret Service if you install `sleeptalk[credentials]` before the first key is saved. The settings page's Keys page says which. Profiles hold a reference to that store, never the key.

There is one settings page. On your own machine Sleeptalk serves it itself; on a phone (for a host with no browser) it opens through an encrypted channel to the host, and the hosted relay passes ciphertext it cannot read. Sleeptalk never logs a key and sends it to nobody but the provider that issued it. See [Where your keys live](https://github.com/sleeptalklabs/sleeptalk/blob/main/docs/your-keys.md).

</details>

<details>
<summary><strong>For agents</strong> · the setup recipe and its receipts</summary>

<br>

Start with [Set up through an agent](https://github.com/sleeptalklabs/sleeptalk/blob/main/docs/agent-setup.md). After installation,
`sleeptalk host --help` describes the available setup commands and their JSON
receipts. Pass the intended workspace and the selected agent explicitly.

`host ensure` checks readiness and never starts a call. When it offers an
existing speech key, ask the user before rerunning with `--reuse-key`. Otherwise,
`host input link` returns a single-use private setup page for speech choices
and credentials. Send the link to the user without opening it yourself, then
check the completion receipt before starting a call.

`host settings-link --json` returns a private link to the full settings page.
Use it to review the voice and delivery choice. The start receipt names the
actual delivery: local, tailnet, or hosted relay. A join link alone does not
prove audio works; ask the user to verify a spoken response.

</details>

<details>
<summary><strong>Everyday commands</strong></summary>

<br>

```bash
sleeptalk                                                   # the settings page: set up, pick a profile, start a call
sleeptalk --web | --terminal | --discord                    # start this folder's profile there, skipping the page
sleeptalk run --config ~/.sleeptalk/profiles/<slug>.toml    # start a saved profile by path
sleeptalk doctor --config ~/.sleeptalk/profiles/<slug>.toml # validate everything, name each problem
sleeptalk service install <slug>                            # launchd / systemd persistence
sleeptalk settings                                          # change agent, keys, voice, destination (the same page)
sleeptalk settings --terminal                               # the same, in the terminal
sleeptalk settings --phone                                  # the same page, on your phone
```

Profiles and state live in `~/.sleeptalk` (or `$SLEEPTALK_HOME`). A repo that keeps its own `config/profiles/` is used only after `sleeptalk trust` in it, so a cloned project can never choose your agent or endpoints.

Turn-taking, endpointing, fillers, barge-in, and reply length live under `[policy]` in the profile TOML; `sleeptalk.example.toml` documents every field.

</details>

## Requirements

Current macOS or Ubuntu, Python 3.11 or newer. The installer brings uv and Python and installs the wheel into an isolated tool environment. A source checkout uses the `web` extra, so `uv tool install ./sleeptalk[web]` from the parent folder does the same by hand. Discord voice needs libopus (`brew install opus` or `sudo apt install libopus0`). Terminal calls on Linux need PortAudio (`sudo apt install libportaudio2`).

The [support matrix](https://github.com/sleeptalklabs/sleeptalk/blob/main/docs/launch-support-matrix.md) is the release contract. Other Linux distributions are unverified. Local speech is English and CPU-only; streaming Moonshine has Apple silicon and Linux x86-64/ARM64 wheels, and Intel Macs use Moonshine Tiny. A phone link through `call.sleep.talk` needs outbound UDP from the host. A host that cannot send UDP uses `--tailnet` or the browser on that machine. `sleeptalk adapter versions` names the exact agent releases the protocol checks cover. Bring-your-own agents stay [preview](https://github.com/sleeptalklabs/sleeptalk/blob/main/docs/bring-your-own-agent.md).

## Repository

| Directory | What it is |
| --- | --- |
| `sleeptalk/` | The Python package and CLI |
| `tests/` | Package tests (`tests/runtime/` holds the transport and session suites) |
| `rendezvous/` | The hosted link service (Cloudflare Worker) and the browser call and settings pages |
| `examples/` | Runnable adapter and session examples |
| `docs/` | Guides, contracts, and decision records. Start at [docs/README.md](https://github.com/sleeptalklabs/sleeptalk/blob/main/docs/README.md) |

[CONTRIBUTING.md](https://github.com/sleeptalklabs/sleeptalk/blob/main/CONTRIBUTING.md) covers tests, the release proof, and the page preview tool. Security reports go through [SECURITY.md](https://github.com/sleeptalklabs/sleeptalk/blob/main/SECURITY.md).

<p align="center"><sub>MIT licensed · by <a href="https://sleep.talk">sleep.talk</a></sub></p>
