# OpenClaw qualification — 2026-10-12

Recommend **2026.10.2** for new local installations; retain **2026.7.1-2**.
Both releases passed `tests/test_openclaw_release.py` against their actual
Gateway and ACP processes. Checks include token authentication and rejection,
session creation, streaming text, repeated turns, reconnecting to the same
session, and an ACP text turn. The Gateway used an isolated home/state/config,
loopback binding, disabled discovery, and a synthetic local model provider.
No existing Gateway, service, credential store, or user session was changed.

2026.10.2 requires **Node >=24.16.0 <25 or >=26.1.0**. It was staged with verified
Node 24.16.0; existing 2026.7.1-2 was tested with installed Node 22.23.1.
Both OpenClaw missing-binary installers now use an exact package and
`--engine-strict`, so an incompatible Node installation fails instead of leaving
an unusable recommendation installed. Sleeptalk does not upgrade Node itself.

Actual authentication rejection uncovered an unsafe fallback: a rejected
Gateway token could switch to local ACP. Authentication failures now stop
startup. Connection/protocol availability failures retain existing fallback
behavior. Focused tests check that a rejected token sends no prompt and does
not start the fallback runtime.

Tool approval/denial, cancellation, steering, and usage mapping have existing
adapter fixtures, not actual new-release tool execution evidence in this cycle.
Same-version reconnect is not proof of cross-version session migration.
The candidate was installed with npm dependency scripts disabled; optional
native capabilities outside these protocol checks are not qualified.

For a remote Gateway, its owner stages and upgrades the server separately.
The client must not run a server updater, restart the service, or claim its
local CLI version identifies the remote process. Record the server's actual
version and Node runtime, preserve config/credentials and a private state
backup, and check disposable copied state before an owner-authorized switch.
Rollback needs the previous server environment and compatible pre-upgrade
state; 2026.10.2 declares state schema 20 and agent schema 24, so swapping only
the executable is not a proven rollback.

Sources: [release notes](https://github.com/openclaw/openclaw/releases/tag/v2026.10.2)
and exact npm metadata in `remaining-artifacts.json`. Evidence is macOS arm64;
Linux/macOS CI is configured but not yet run. Real-provider, live voice,
service restart, and remote deployment acceptance remain human gates.
