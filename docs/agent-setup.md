# Set up through an agent

An agent can install Sleeptalk and guide you to a browser call. It needs command
access to the Mac or Ubuntu computer where your agent and project will run,
permission to install software, and a browser you control on that computer or
your phone. A phone call still needs that host to stay awake and connected.

Open your agent in the intended project and copy the request in the
[README's Get started section](../README.md#get-started). The steps below are
for the agent carrying out that request. Start with one browser call; Terminal
and [Discord](discord.md) can be configured afterwards.

## 1. Establish the host and workspace, then install

Identify the host OS and the current project directory. Ask if the intended
host or project is ambiguous. Keep that project's absolute path throughout
setup, even if installation changes the shell's working directory. Choose a
profile for this project; keep an existing profile's workspace binding intact.

Follow the [README installation instructions](../README.md#get-started): use a
release wheel with its installer and constraints, a successful CI artifact
bundle before the first release, or a separate source checkout. Do not assume
that a PyPI release exists or that `sleeptalk` is already on PATH. Use the full
command path printed by the installer if needed.

Once installed, read:

```bash
sleeptalk host --help
sleeptalk host inventory
```

The installer supplies Sleeptalk and Python. Agent login, an agent's own runtime
requirements, speech credentials, and local speech model downloads are separate.

## 2. Check the selected agent

Identify the agent runtime that should answer calls. Ask if several are
available or the user wants a different one. An agent's ability to run shell
commands does not establish which Sleeptalk adapter it supports. Use
[Bring your own agent](bring-your-own-agent.md) for an unlisted runtime; do not
label it as a different built-in agent to get past setup.

Set the following example values to the actual project, supported agent type,
and profile. `codex` is an example, not a default for every installation:

```bash
voice_workspace="/absolute/path/to/your/project"
voice_agent="codex"
voice_profile="project-voice"

sleeptalk host discover --workspace "$voice_workspace" --agent "$voice_agent" --surface web --json
sleeptalk host probe-runtime --workspace "$voice_workspace" --agent "$voice_agent"
sleeptalk host ensure --profile "$voice_profile" --workspace "$voice_workspace" --agent "$voice_agent" --surface web --json
```

Discovery is read-only. The runtime probe starts the selected integration to
check readiness without sending a task prompt. Resolve login, model, permission,
and runtime problems reported by the receipt. Keep any chosen runtime/model/
permission options consistent with the private input commands; their `--help`
lists the supported flags. Do not broaden permissions to make a check pass.

`host ensure` checks or prepares the profile; it never starts a call. Follow a
`conflict` or `failed` receipt's remediation before continuing. A `needs_input`
receipt means setup is incomplete. If it is already `ready` and the saved
choices match the user's request, keep them and skip the credential-entry step.
Reusing an agent and workspace does not necessarily resume the chat from which
this request was sent; session behavior is specific to the adapter.

## 3. Ask about the call device and speech

Ask whether the user will call from a browser on this host or another device,
such as a phone. A localhost link works only on the host. If `tailnet.available`
is true, ask whether the calling device is on that same Tailscale network before
selecting `--tailnet`. See [Calls over your tailnet](tailnet-calls.md).

Let the user choose speech in the private setup page: a cloud service with its
own credentials, or [Local speech](local-speech.md) with compatible hardware and
model downloads. Local speech needs no speech key; the selected agent may still
need its own login or provider key. Existing subscriptions are not automatically
speech API credentials.

If `host ensure` returns `reusable_keys` and an `ask` message, put that question
to the user and wait. After an explicit yes, repeat the same ensure command with
`--reuse-key` and the chosen provider. This validates the key with a small paid
provider request and copies it into the private store. Without that yes, send
the private page. See [Key reuse](reuse-your-key.md).

For a private setup request, choose a new request ID and retain it for retries:

```bash
voice_setup_id="project-voice-setup-001"
sleeptalk host input link --request-id "$voice_setup_id" --profile "$voice_profile" \
  --workspace "$voice_workspace" --agent "$voice_agent" --surface web
```

Send the returned `setup_url` privately to the user. Never open it yourself or
ask for keys in chat, shell arguments, or an answers file. Do not read the
credential store. The page handles speech choices, key entry, and voice selection.
For a browser controlled by the user on the host itself, `host input start`
with `--user-browser-local` provides the local alternative; check its `--help`.

Poll `host input status --request-id "$voice_setup_id"`, or use a bounded wait:

```bash
sleeptalk host input wait --request-id "$voice_setup_id" --timeout 30
```

If still waiting, leave the request open and check again later. Continue only
when the receipt reports `complete` with `profile_ready: true`. Resolve any
reported runtime or credential problem first. Re-run the same `host ensure`
command to check the completed profile. Keep the same OS user, workspace,
profile, and `SLEEPTALK_HOME` throughout.

The full settings page is available after setup with
`sleeptalk host settings-link --profile "$voice_profile" --json`. Send its
`settings_url` privately; let the user review speech and browser delivery there.

## 4. Start one call and verify it with the user

Start only after readiness and the destination have been resolved. For a chat
request that should return a remote browser link, use:

```bash
voice_call_id="project-voice-call-001"
sleeptalk host start-voice --profile "$voice_profile" --workspace "$voice_workspace" \
  --caller-context chat --request-id "$voice_call_id" --json
```

For a browser on this host, use `start-voice web --caller-context local` with the
same profile, workspace, request ID, and JSON flags. A saved delivery choice
still applies: use the settings page to change it when the user's destination
changes. For an agreed tailnet call, select `--tailnet` as described in the CLI
help. Never send a localhost link to someone calling from another device.

Read the start receipt's `delivery` and tell the user whether this is local,
tailnet, or hosted relay. If it differs from their choice, resolve that before
calling setup complete. Send the returned join link privately. Reuse the same
request ID to recover an interrupted start instead of launching duplicate calls;
use a new ID for a genuinely new call.

Ask the user to allow microphone access, speak a question, confirm an audible
reply, and verify that the agent is in the intended project. Then check a
follow-up, interruption, and ending the call. A ready receipt or join link proves
neither microphone input nor audible output. Report technical readiness and the
user's audio confirmation separately.

Use `sleeptalk host list` to identify a live call and `sleeptalk host stop CALL_ID`
to stop it. Service installation and reboot checks are separate steps in the
[release checklist](release-checklist.md); setting up one call does not establish
unattended operation.
