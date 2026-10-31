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
