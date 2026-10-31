# Cursor qualification — 2026-10-12

Cursor has two independently versioned integrations. The Python SDK is now
qualified at the **SDK wire and bundled bridge startup** surface. The CLI is
still a candidate: its ACP initialization and authentication gate pass, but
its authenticated turn lifecycle has not been qualified in this rollout.
Neither result certifies live voice or deployment acceptance.

## Versions and installation policy

| Surface | Previous / existing | Target | Evidence |
| --- | --- | --- | --- |
| Python SDK | 1.0.29 protected pin, 1.0.31 installed, 1.0.32 project/lock pin | 1.0.37 | Exact wheels pass Connect-wire, bundled bridge discovery, converter and adapter tests |
| CLI ACP | 2026.09.02-c22c1a3 installed | 2026.10.01-e373342 | Both actual binaries pass initialization and reject an unauthenticated new session |

The SDK fresh-install recommendation, optional dependency, lockfile and
readiness install hint now agree on **cursor-sdk==1.0.37**. Protected startup
accepts exactly **1.0.29, 1.0.31, 1.0.32 and 1.0.37**. This is not a version
range. A present older SDK is retained; readiness does not upgrade it. Unknown
SDK versions remain eligible for the ordinary integration, but the protected
bridge fails closed until that exact version is tested and added.

The installed SDK and CLI were left unchanged. SDK wheels were staged into
separate directories and selected with `PYTHONPATH`; tests used `uv run
--no-sync`. A normal explicit dependency sync will adopt the new project pin.
The candidate CLI was downloaded as a versioned archive and extracted into a
temporary directory; the global installer was not run.

CLI promotion and migration of its existing floating installer remain open.
The legacy CLI setup command is **not** a version recommendation approved by
this record. Do not use it to stage or upgrade an existing installation. Finish
the authenticated ACP checks below, then replace its install command with an
exact-version, missing-only path as part of promotion.

## Upstream review

- [PyPI metadata](https://pypi.org/pypi/cursor-sdk/json) reports SDK 1.0.37;
  the target wheel URLs and SHA-256 hashes are retained in `uv.lock`.
- [SDK release notes](https://cursor.com/docs/release-notes/sdk) were available
  through 1.0.35 at review time. They describe changes to local storage and
  connection handling, removal of dotenv/bunfig loading from the standalone
  bridge in 1.0.32, and earlier explicit empty-tool support. Do not infer
  1.0.36/1.0.37 changes from missing release notes.
- [The CLI installer](https://cursor.com/install), inspected as text, selected
  2026.10.01-e373342. The archive URL is
  `https://downloads.cursor.com/lab/VERSION/OS/ARCH/agent-cli-package.tar.gz`.
  CI records the downloaded archive's observed SHA-256; this is reproducibility
  evidence, not a checksum or signature independently published by Cursor.
  The local candidate digest is recorded in [artifact evidence](cursor-artifacts.json).
- [Python SDK](https://cursor.com/docs/sdk/python),
  [bridge](https://cursor.com/docs/sdk/bridge), and
  [ACP](https://cursor.com/docs/cli/acp) documentation were reviewed separately.

## Changes exposed by qualification

- Warmup no longer consumes the session identity event before a listener is
  attached. Isolated deny-only sessions report `resumable=False`, because
  their temporary store is removed when the owned bridge closes.
- Send and terminal waiting are bounded. Timeout, cancellation and early
  consumer closure cancel pending reads and attempt bounded run cancellation
  and settlement. A later turn works after an ordinary run timeout.
- An expired or otherwise unsuccessful terminal result is an error rather
  than a successful response.
- The SDK treats a manually started bridge as remote. Its run-scoped cancel
  and wait calls were blocked with environment credential fallback disabled.
  Our client for the authenticated, owned, disposable bridge narrowly exempts
  `CancelRun` and `WaitLiveRun` from that remote-cloud guard. Environment key
  fallback stays off, and cloud-agent auth checks remain intact. This private
  SDK override is part of the exact-version compatibility contract.

A send that times out before a run ID arrives closes the local response but
cannot target an unknown upstream run for cancellation. This is an explicit
limit, not proof that the upstream operation stopped.

Protected SDK sessions still require a native Cursor API key, an explicit
model, a new session, no built-in tools, no MCP or task tools, and empty setting
sources. Pending approval remains unsupported; no fallback to auto approval
was added. The ordinary auto-mode SDK continues to use its existing settings.

## Repeatable evidence

[SDK release tests](../../tests/test_cursor_release.py) use the **actual Python
SDK** (AsyncClient, AsyncAgent, AsyncRun, types and Connect framing) with a
deterministic HTTP transport fixture. They cover fragmented streaming, tool
and usage events, session identity, terminal failure, cancellation after a
stall or early close, bounded terminal waiting, and a subsequent turn. The
fixture supplies messages; it does not execute a real tool or model.

[Isolation tests](../../tests/test_cursor_sdk_isolation.py) start each wheel's
**actual bundled Node/JS bridge** in a temporary home, verify discovery and an
empty store, and execute its generated converter to preserve empty tools,
setting sources, workspace and auto-review policy. Required release jobs fail
if the selected wheel or bridge is missing; they cannot turn that into a skip.

[CLI release tests](../../tests/test_cursor_cli_release.py) run the **actual
binary** in a clean home with no credentials. They verify ACP version 1,
capability/auth-method discovery and rejection of `session/new` without login.
They do not send a model prompt. Existing ACP adapter tests cover permission
callbacks and events against the repository's fake ACP peer.

Local platform: macOS arm64, Python 3.11. Each of SDK 1.0.29, 1.0.31, 1.0.32 and
1.0.37 passed the same release/isolation/adapter checks. Each CLI release passed
the actual handshake test. The [compatibility workflow](../../.github/workflows/cursor-compatibility.yml)
retains these exact versions on Linux and macOS and archives results, package
versions and observed CLI archive digests. Those hosted jobs have not run from
this local change; they start after push/manual dispatch.

Repository gates on the candidate SDK passed: **263 focused tests passed,
17 skipped**; CLI import succeeded; **4,973 full-suite tests passed, 73 skipped,
1 expected failure**. Each retained SDK passed its 32 release/isolation/adapter
tests. The wheel build passed and its metadata and packaged inventory both
select SDK 1.0.37. The installed SDK was rechecked as 1.0.31 after validation.

Example (with dependencies already installed and a separately staged wheel):

```bash
SLEEPTALK_TEST_CURSOR_SDK_VERSION=1.0.37 \
PYTHONPATH=/absolute/path/to/staged-wheel \
uv run --no-sync pytest -q tests/test_cursor_release.py tests/test_cursor_sdk_isolation.py tests/test_cursor_sdk_adapter.py

SLEEPTALK_TEST_CURSOR_CLI=/absolute/path/to/cursor-agent \
SLEEPTALK_TEST_CURSOR_CLI_VERSION=2026.10.01-e373342 \
uv run --no-sync pytest -q tests/test_cursor_cli_release.py

sleeptalk adapter versions --cursor-bin /absolute/path/to/agent --json
```

## Remaining promotion and acceptance gates

- Authenticated CLI ACP streaming, tool approval/denial, cancellation followed
  by another turn, model/workspace binding, usage and session resume on both
  retained and candidate binaries. Pin its installer only after these pass.
- SDK model execution through the actual JS bridge with a valid native key.
  A synthetic key was rejected by the service as expected; this is not a
  successful provider check. Current full-turn evidence ends at the SDK wire.
- Persistent auto-mode session migration and rollback on disposable state
  copies. Do not test storage migration on the sole copy of a conversation.
- Human microphone, Discord speech and service-restart acceptance for the
  deployment being switched.

Keep previous environments and compatible state backups. For isolated deny
sessions, start a new conversation after a switch; the temporary ID is not a
persistent resume handle. Restore both runtime and compatible state when
rolling back an ordinary persistent session.
