# Remaining-harness validation — 2026-10-12

Local environment: macOS arm64, Python 3.11. Existing SDKs remained Claude
0.2.157 and Cursor 1.0.31. All upstream candidates were staged separately.
Provider checks used loopback fixtures and synthetic credentials.

| Selected actual runtime | Result | Evidence scope |
| --- | --- | --- |
| Codex 0.153.4 and 0.162.1 | 4 tests passed per release | App-server text, approval, denial, cancellation at approval, next turn, persisted-thread resume, usage |
| Claude SDK 0.2.157 / bundled CLI 2.1.277 | 3 passed | Messages streaming, approval/denial, repeated turns, usage and identity |
| Claude SDK 0.2.165 / bundled CLI 2.1.294 | 3 passed | Same cases |
| Claude SDK 0.2.165 / standalone CLI 2.1.292 and 2.1.296 | 3 passed per CLI | Same cases with executable lookup directed to the standalone runtime |
| Hermes source 0.21.7 | 40 bridge tests passed | Actual provider turns, permission gates, cancellation, credential/model/workspace binding and isolation over ACP/Gateway |
| OpenClaw 2026.7.1-2 and 2026.10.2 | 1 integration test passed per release | Gateway auth/rejection, repeated text turns, same-session reconnect, ACP text turn |

Final repository gates:

- Focused install/schema/authentication regressions: **132 passed, 1 skipped**.
- Final OpenClaw adapter contracts: **49 passed**.
- Required CLI import check: passed.
- Full suite: **4986 passed, 82 skipped, 1 xfailed**, 8 deprecation warnings,
  187.18 seconds. Run with `UV_NO_SYNC=1` to retain the existing environment.
- Temporary wheel build: passed. Inventory, Hermes source manifest, both Codex
  schema bundles, and exact Claude/Cursor SDK metadata were checked inside it.
- New workflow YAML parses; `git diff --check` passes.

These totals describe different runs and must not be added together. Skipped
optional runtimes are not passes. Earlier readiness expectations for an
unpinned Claude install hint were updated and rerun successfully. The actual
runtime tests exposed and verified fixes for Codex denial/usage and OpenClaw
authentication fallback; missing Hermes staging dependencies were resolved in
its separate environment.

The new Linux/macOS CI matrix has not run on hosted runners yet. It stages
releases from the packaged inventory and retains JUnit/dependency artifacts.
`remaining-artifacts.json` retains local candidate npm resolutions, SDK wheel
identity, Node/archive checksums, and the Hermes dependency overlay inventory.
Temporary runtime downloads can be removed after this evidence is saved.

Real-provider account checks, live microphone, Discord speech, service restart,
cross-version session migrations, and remote Gateway deployment acceptance
remain separate gates. Cursor CLI authenticated qualification is still pending;
OMP 18.10.3 is newly observed and not yet recommended. See the individual
records for the exact boundaries of each compatibility claim.
