# Hermes qualification — 2026-10-12

Recommend **0.21.7**, tag `v0.21.7`, revision
`00f69310e29c24dfd7c6bb258b0a2337d3455d79`, for the optional isolated source bridge.
The prior **0.21.3** source (tag `v2026.9.14`) remains accepted. Every guarded
file must match one complete reviewed manifest; a mixture of releases fails.
The bridge reports the selected source's version, including older sources.

The installed local Hermes reports 0.18.2 with carried modifications. It was
not upgraded or overwritten, and this qualification does not certify that
custom runtime or an independently operated remote Gateway. Select the optional
bridge explicitly with `options.hermes_root` and its own Python environment.

All 17 guarded private seams changed, so the new source was tested before
adding its hashes to `hermes_bridge_versions.json`. Actual source tests pass
approval, denial, cancellation, repeated turns, ACP/Gateway transports,
credential/model binding, workspace binding, and startup isolation. The focused
bridge suite passed 40 tests; bridge plus installed/permissions/Gateway contracts
passed 86. A subsequently added unit check verifies old/new source selection and
rejects mixed trees. The old source manifest is retained; the old source runtime
was not rerun locally during this cycle and is included in the CI matrix.

Staging used Python 3.11 on macOS arm64 with a separate dependency overlay.
Missing declared dependencies initially prevented repeated turns; completing
the overlay with snowballstemmer 3.1.1, anthropic 0.87.0, and docstring-parser
0.17.0 resolved the failures without changing the installed environment.
Fresh staging installs the release with its `anthropic` extra. CI records its
resolved dependency list; upstream dependencies themselves are not frozen by
these source hashes. The local provider fixture also handles the release's
optional Ollama context probe without treating it as a model turn.

See [release notes](https://github.com/NousResearch/hermes-agent/releases/tag/v0.21.7)
and [setup](../bring-your-own-agent.md#the-pinned-isolated-runtime-optional).
The bridge intentionally starts fresh isolated sessions. Persistent history
migration, real providers, live voice, service restarts, and custom/remote
runtime acceptance remain separate work. New Linux/macOS CI has not run here.


## Release validation — 2026-10-31

The fresh CI install of 0.21.7 on Python 3.11 omitted core dependencies:
its current source metadata guards those requirements with Python >=3.14.
An isolated reproduction failed at `import dotenv`. A failed ACP process
could then leave initialization waiting on an unsettled response future.

The source manifest now records Python 3.14 for 0.21.7 and retains Python
3.11 for 0.21.3. CI stages each independent environment from that record and
checks core imports before protocol tests. The setup guide and failure hint
name Python 3.14 for a new 0.21.7 installation. Existing installed agents
are unchanged. ACP closes outstanding requests on EOF, bounds control requests
to 30 seconds, and cleans up failed startup so it can be retried.

On macOS, the 0.21.7 Python 3.14 runtime passed the complete selected bridge,
installed-runtime, permission, gateway, inventory and shared conformance suite:
232 passed, 17 unrelated runtime cases skipped. The retained 0.21.3 source
also passed the same selection in its Python 3.11 environment (232 passed,
17 skipped). Six failure cases exercise early exits and stalled replies during
initialization, session creation and saved-session resume, including retry.
Connection loss or a deadline during resume retains the saved conversation ID
and never requests a new session. Focused startup and shared Hermes/OpenClaw
tests passed 131 cases. The credential fixtures
block startup network/process IO with the same OSError as production, permitting
optional upstream version probes to handle the denial while preserving their
credential, endpoint and redirect assertions.

The full repository suite passed: 5,189 passed, 92 skipped and one expected
failure; the CLI import also passed. Hosted compatibility and artifact checks
must still pass on the release commit. Live voice and service checks remain
human gates.
