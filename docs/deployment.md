# Deployment

Keep a Sleeptalk bot alive on a host. The product is the `sleeptalk` CLI and
the repo-root Docker image. There is no FastAPI control API to run.

## First run

```
sleeptalk init          # interview -> profile TOML + .env
sleeptalk run           # foreground; Ctrl-C stops it cleanly
sleeptalk doctor        # re-runs every check, names all problems
```

## State directory (`SLEEPTALK_HOME`)

Profile state lives here: `profiles/<slug>.toml`, `sleeptalk.toml`, legacy
`.env`, and `logs/`. New private credentials live separately under
`~/.local/share/sleeptalk/credentials`; isolated package artifacts live under
`~/.local/share/sleeptalk/runtimes`. Deleting profile state does not delete
those credentials or installations.

Resolution order (env-only; there is no `--home` flag. `--config` is the
single explicit-path escape hatch, and the two are orthogonal):

1. `--config PATH` — used verbatim (repo/dev use).
2. `$SLEEPTALK_HOME` — the layout above.
3. Repo context detected from cwd (`config/profiles/*.toml` or a
   `pyproject.toml` declaring the sleeptalk entry point, max 3 levels up,
   never past `$HOME`), only once you have run `sleeptalk trust` in that
   repo. A repo's profiles pick the agent command, provider endpoints, and
   the `.env` the agent inherits, so a cloned repo is never used on sight:
   an untrusted one is named and skipped. Never silent: one line names
   what was detected and that `SLEEPTALK_HOME` overrides it.
   `sleeptalk trust --remove` undoes it.
4. `~/.sleeptalk`.

The `.env` in whatever directory you run a command from is never read.

`doctor`'s first output line always states which tier resolved and why.

## Exit-code contract (supervisors depend on this)

| Code | Meaning | Supervisor action |
|---|---|---|
| 0 | Clean shutdown. SIGTERM/SIGINT handled, left the voice channel, closed the agent subprocess | Intended stop. Do not treat as failure |
| 1 | Runtime crash. A FATAL log line (with traceback) was written first | Safe to restart |
| 2 | Config/startup error. Fix-stating message printed | Never auto-restart. The config will not heal |

## Docker (recommended for servers/VPS)

Build the image from a checkout (no published image yet), set up once, then
run it:

```
docker build -t sleeptalk .
docker run -it -v $PWD/sleeptalk-home:/home/sleeptalk sleeptalk init
docker run -d --restart unless-stopped \
  -v $PWD/sleeptalk-home:/home/sleeptalk sleeptalk
```

Run `init` first: without a profile the server exits 2 (config error), and
`--restart unless-stopped` restarts it in a loop.

The image (repo-root `Dockerfile`) bakes in ffmpeg, libopus, Node 22 and the
`pi` agent CLI at the fresh-install version in `sleeptalk/harness_versions.json`.
Python dependencies are constrained to the versions in `uv.lock`, as in the
release installer bundle.
`SLEEPTALK_HOME=/home/sleeptalk` is the single volume.
Sleeptalk and the agents it launches run as the unprivileged `sleeptalk`
user (uid 1000), never root; on start the entrypoint hands the volume to
that user if it is owned by someone else.
`compose.yaml` (repo root) is the equivalent compose setup. Reboot survival
comes from Docker's own restart policy. The container never supervises
itself.

## Bring-your-own supervisor

### systemd (user)

`Restart=on-failure` matches the exit-code contract: exit 0 (clean SIGTERM
stop) is not a failure. `RestartPreventExitStatus=2` prevents retries for
configuration errors; `Restart=on-failure` alone would retry exit 2.

```ini
# ~/.config/systemd/user/sleeptalk-<slug>.service
[Unit]
Description=sleeptalk voice bot (<slug>)
After=network-online.target
StartLimitIntervalSec=60
StartLimitBurst=5

[Service]
# 0 = clean stop (systemctl stop -> SIGTERM -> exit 0, NOT a failure)
# 1 = crash (restarted)   2 = config error (fix the config; don't loop)
ExecStart=/usr/local/bin/sleeptalk run --config %h/.sleeptalk/profiles/<slug>.toml
Restart=on-failure
RestartPreventExitStatus=2
RestartSec=5
Environment=SLEEPTALK_HOME=%h/.sleeptalk

[Install]
WantedBy=default.target
```

```
systemctl --user daemon-reload && systemctl --user enable --now sleeptalk-<slug>
loginctl enable-linger $USER   # survive logout/reboot
```

### launchd (macOS)

```xml
<!-- ~/Library/LaunchAgents/ai.sleeptalk.<slug>.plist -->
<key>ProgramArguments</key>
<array><string>/path/to/venv/bin/sleeptalk</string><string>run</string>
       <string>--config</string><string>/path/to/profiles/<slug>.toml</string></array>
<key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
<key>EnvironmentVariables</key><dict><key>SLEEPTALK_SERVICE</key><string>launchd</string></dict>
<key>StandardOutPath</key><string>~/Library/Logs/sleeptalk/<slug>.log</string>
```

launchd restarts every non-zero exit. `SLEEPTALK_SERVICE=launchd` makes a
configuration error (exit 2) exit 0, so it isn't retried. The program is the
`sleeptalk` script beside the installation's Python, so Login Items lists the
job as `sleeptalk`. The log lives in `~/Library/Logs/sleeptalk`, one file per
bot: launchd can't start a job whose log file it can't open, and a file in
`~/Documents` can be locked to one app by macOS privacy labels.

`sleeptalk service install <slug>` prints exactly what it would write before
writing anything.

### `sleeptalk service` (the opt-in hatch)

```
sleeptalk service install <slug> --config /absolute/path/to/profile.toml
sleeptalk service status <slug>
sleeptalk service uninstall <slug>   # removes the definition; does not stop a loaded job
```

Installation is opt-in and user-level. It prints a value-free preflight report
and the definition before asking to write it. `--yes` skips that confirmation.
It never enables or starts the service. The wizard does not enroll a service.

Set `agent.workspace_dir` explicitly before installation. Preflight rejects a
missing workspace, mismatched profile slug, invalid binding, unavailable
credential references, and terminal audio without an attached human session.
A successful report means the definition can be written. Service-account
workspace access, credential access, adapter login, and reboot operation are
still unverified. Existing environment-based credentials are reported unknown.

Generated definitions pin the current Python installation and its absolute
executable path, the config path, workspace, and a PATH containing only absolute
entries. They do not embed provider keys. systemd arguments disable dollar substitution and
escape percent expansion. launchd uses an XML serializer and literal argument values,
and `SLEEPTALK_SERVICE=launchd` maps config exit 2 to a successful stop. Review these paths
before enabling the job, especially after an installation moves.

Status distinguishes the definition file from a loaded job. It asks the service
manager even if the definition was already deleted, and prints no environment
or recent application log output. If the manager cannot be queried, state is
unknown. A macOS user agent requires that user's login session. A sleeping or
offline host does not provide continuous availability.

Stop and disable or unload a job before removing its definition:

```bash
# Linux
systemctl --user disable --now sleeptalk-<slug>.service
# macOS, using the numeric UID from `id -u`
launchctl bootout gui/<uid>/ai.sleeptalk.<slug>
```

Then run `sleeptalk service uninstall <slug>`. The settings page's **Stop**
does both on macOS: launchd loads every file in `~/Library/LaunchAgents` at
login, so a definition left behind would start the bot again. On Linux, also run
`systemctl --user daemon-reload`. Uninstall reports that loaded-job state is
unchanged and retains profiles, logs, credentials, and runtime installations.
Deleting a unit file alone cannot prove its process stopped.

The implementation tests render both formats and check the launchd program,
log path, and exit mapping. Actual service login, logout, reboot, sleep,
keyring unlock, and provider expiry remain human operational checks.

## Logging

Console stays primary in the foreground. Every run also appends to
`<state>/logs/<slug>.log` (1 MB × 2 files, size-rotated), so a crash's
FATAL line survives the process. The default is `[log] level = "WARNING"`:
basic warnings and errors, with credential redaction. Explicit levels such as
`INFO` and `DEBUG` are still supported.

Detailed timing traces are off by default (`[log] latency = false`). The browser
call's **Help** offers **Record performance diagnostics for this call**; it
does not change the saved profile. Advanced troubleshooting in settings can
enable traces for future calls. Timing files rotate at 1 MB plus one backup,
stay on the host, and exclude conversation text, audio, and arbitrary error
bodies. Transcript debugging is a separate explicit environment opt-in.
See [first-call defaults and troubleshooting](first-call-defaults.md).

## Uninstall

Stop the owned service or container first, then remove its definition or
container. Profile state, saved credentials, and isolated runtime installations
are separate cleanup choices. Inspect affected profiles before deleting a
shared credential. Deleting a local key does not revoke it at the provider.
Do not delete a workspace or another profile's running environment as part of
service removal.

## Privacy / data flow

Audio leaves your machine only to the providers in your profile: Discord
(the transport), your STT provider (e.g. Deepgram) for transcription, your
TTS provider (e.g. Cartesia/ElevenLabs) for synthesis, and your agent
runtime (e.g. pi) for the conversation text. sleeptalk itself phones
nowhere: no telemetry, no analytics, no update checks. `sleeptalk doctor`
makes the same live calls the runtime does (token validation, a 1-second
transcription round-trip) and nothing more.
