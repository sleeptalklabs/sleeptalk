# Codex qualification — 2026-10-12

Recommendation: **0.162.1**. Keep **0.153.4** accepted with its own schema.
The installed 0.153.4 CLI was not changed. Both exact binaries passed four
actual app-server cases against a local Responses SSE provider: text, explicit
tool approval, automatic denial, and cancellation at an approval boundary.
Each case also verifies a subsequent turn and reopening the persisted thread.
Usage and session identity are checked; no real account or model was used.

The [official app-server reference](https://developers.openai.com/codex/app-server)
provides version-specific schema generation. The 0.162.1 bundle was regenerated
with the actual binary; both bundles and SHA-256 values are retained in the
packaged inventory. Definitions grew from 622 to 660. Consumed start/resume
shapes remain compatible; new optional turn fields are not sent by Sleeptalk.
Unused thread rollback types disappeared.

An actual startup check found that 0.162.1 rejects `approval_policy = "untrusted"`
in configuration, even though its generated enum still includes that value.
The fixture explicitly uses `on-request`. Users with that older configuration
must review it before switching; Sleeptalk does not rewrite it automatically.

Two adapter corrections came from the actual runtime: emit usage from
`thread/tokenUsage/updated`, and settle denied requests immediately. Permission
profile requests use a grants response; command/file requests use decisions.
The latter distinction has a schema-based unit test.

Evidence: `tests/test_codex_release.py` (4 passes per exact release),
`tests/test_codex_app_server.py`, and shared conformance. This is macOS arm64
fixture evidence; Linux/macOS compatibility CI is configured but has not run
here. Mid-generation cancellation, real-provider behavior, live microphone,
Discord speech, service restart, and cross-version persisted-state migration
are not established by these tests. Keep separate state backups when switching.
