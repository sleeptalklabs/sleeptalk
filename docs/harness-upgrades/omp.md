# OMP upgrade record

Date: 2026-10-12. Target: [OMP 18.10.2](https://github.com/can1357/oh-my-pi/releases/tag/v18.10.2).
Retained versions: compiled CLI 17.2.12 and protected npm SDK 18.2.1.

## What users can run

| Integration | Verified releases | Recommendation for a new installation |
| --- | --- | --- |
| Compiled CLI, normal RPC with `permission_mode = "auto"` | 17.2.12, 18.10.2 | 18.10.2 |
| Isolated npm SDK, `pending` or `deny` | 18.2.1, 18.10.2 | `@oh-my-pi/pi-coding-agent@18.10.2`, with Bun 1.3.14 |

Existing OMP installations are retained. Discovering 17.2.12 does not invoke
an installer or require an upgrade. Other CLI releases remain unverified,
not rejected merely because of age. The SDK uses private imports and an
exact verified allowlist; unknown SDK releases fail before protected startup.

The standalone CLI cannot supply the isolated SDK, even when its version
matches a verified SDK release. Protected mode needs the npm package selected
explicitly through `agent.pi_bin`, Bun on that runtime's PATH, a native OpenAI
or Anthropic provider, and an explicit model. It must never fall back to
automatic approval. Its conversations are in memory and are reported as
non-resumable. Ordinary CLI sessions can be saved and resumed.

The packaged inventory drives both the CLI install recommendation and the
SDK allowlist. Inspect an existing selection without modifying it:

```bash
sleeptalk adapter versions --omp-bin /absolute/path/to/omp --json
```

The command reports CLI version evidence and protected SDK eligibility
separately. It does not certify Bun availability, a custom build, credentials,
or live voice. The current registration installs a missing compiled CLI using
the [official installer's exact-tag option](https://omp.sh/install):

```bash
curl -fsSL https://omp.sh/install | sh -s -- --binary --ref v18.10.2
```

For protected mode, stage a separate npm package instead of replacing a
global executable. For example, after choosing an unused directory:

```bash
npm install --prefix /tmp/sleeptalk-omp-18.10.2 --save-exact --no-audit --no-fund \
  @oh-my-pi/pi-coding-agent@18.10.2
```

For a deployment, choose a durable directory and select its
`node_modules/.bin/omp` through the supported profile setup/binding flow.
Keep the old installation and profile binding available.

## Migration findings

OMP 18.10.2 moved runtime credential overrides from
`auth.setRuntimeApiKey` to `auth.keys.setRuntime`. The protected launcher now
uses the new API when present and retains the older API for 18.2.1. The real
onboarding probe caught this failure; a startup test without an API key did not.

Full-turn testing also found that our launcher acknowledged a prompt only
after the SDK finished it. Sleeptalk therefore buffered its text until the
end. Admission now precedes streaming, with asynchronous failures still
reported and settlement emitted. A fixture holds the model stream open until
Sleeptalk yields the first sentence, so a completed transcript cannot pass
as proof of streaming.

Upstream changed default task results to plain text and forwards more
background tool updates. The current CLI test exercises a real task worker,
observes native lifecycle events, and keeps child text out of spoken output.
Late worker updates and steering remain covered by adapter contract fixtures.

## Automated evidence

Local environment: macOS arm64, Python 3.11.15, Bun 1.3.14 for SDK tests.
All model responses use deterministic fixtures and synthetic credentials.
No operator profile, existing session store, global OMP installation, or
running service was changed.

`tests/test_omp_cli_release.py` launches the actual compiled CLI against a
loopback HTTP provider in a disposable home. It checks streamed sentences,
usage and identity, saved-session resume in a fresh process, a real automatic
write, and abort followed by another turn. Version 18.10.2 additionally runs
the native task-worker lifecycle test. Older worker behavior is covered by
contract fixtures, not claimed as actual-runtime worker evidence.

`tests/test_omp_sdk.py` and `tests/test_omp_release.py` execute the actual npm
SDK with the production launcher. A trusted test entrypoint replaces only
HTTP fetch responses; SDK sessions, hooks, tools, and RPC remain real. Checks
cover isolated startup, no ambient extension/hook/MCP/preload execution,
native-key onboarding without a model turn, early streaming, usage, identity,
conversation continuity, real writes with approval/denial, abort during both
streaming and pending approval, provider failure recovery, and temporary-home
cleanup. Unknown SDK versions fail before creating a launch environment.

Results from the working checkout:

| Check | Result |
| --- | --- |
| Compiled CLI 17.2.12 | 3 passed; current-release worker test skipped |
| Compiled CLI 18.10.2 | 4 passed |
| npm SDK 18.2.1, including preparation tests | 19 passed |
| npm SDK 18.10.2, including preparation tests | 19 passed |
| Focused OMP, shared RPC, installer, inventory, and conformance tests | 237 passed, 32 skipped |
| `uv run python -c "from sleeptalk.cli import main"` | Passed |
| `uv run pytest -q` | 4,954 passed, 74 skipped, 1 expected failure |
| Wheel build and packaged inventory/launcher import | Passed |
| Workflow YAML parse and diff whitespace check | Passed |

The full suite skips opt-in release checks; the explicit runs above provide
that evidence. These results describe the working checkout, not a deployed
release. The first restricted wheel build could not resolve its build backend;
the reported build passed with network access. Initial CLI attempts also hit
temporary disk exhaustion; only complete runs after reclaiming this task's
temporary files are counted above.

The compiled binaries matched the official GitHub release asset SHA-256:

```text
17.2.12  ab4ff24c8ba3ae6fd9d4b54969c94f4408cc316675f5534729ae502d8c97df74
18.10.2  9752e3d7f7bdab636e21495a6affc196e8ffc3a2dffaf529db2dd73d2bdea255
```

The [OMP compatibility workflow](../../.github/workflows/omp-compatibility.yml)
reads the two version sets from the inventory and runs each on Ubuntu and
macOS. It downloads compiled releases from GitHub and checks their published
digests; the older binary is absent from the newer installer build service.
SDK jobs install exact npm and Bun versions. Each job retains JUnit results,
binary checksums or npm package locks. Remote CI results remain pending until
the workflow is pushed. Normal test runs skip these opt-in real-release tests.

To reproduce the protected SDK checks after staging it and Bun:

```bash
SLEEPTALK_TEST_OMP_SDK_BIN=/tmp/sleeptalk-omp-18.10.2/node_modules/.bin/omp \
SLEEPTALK_TEST_OMP_SDK_VERSION=18.10.2 \
SLEEPTALK_TEST_BUN=/absolute/path/to/bun \
  uv run pytest tests/test_omp_sdk.py tests/test_omp_release.py -q
```

To test a compiled CLI with disposable configuration:

```bash
SLEEPTALK_TEST_OMP_CLI=/absolute/path/to/omp \
SLEEPTALK_TEST_OMP_CLI_VERSION=18.10.2 \
  uv run pytest tests/test_omp_cli_release.py -q
```

## Remaining acceptance and rollback

Real provider/account checks, Anthropic transport qualification, actual-runtime
steering and detached-worker continuation, cross-version session migration,
Windows, live microphone, Discord speech, and service restart acceptance are
not certified here. The SDK fixtures use native OpenAI Responses; the CLI
fixtures use OpenAI-compatible Chat Completions. CLI fixtures disable ambient
extensions and skills; only the protected SDK claims enforced startup isolation.

Before switching an existing profile, retain its executable, profile/binding,
dependency lock, and a private pre-upgrade session snapshot. SDK 18.2.1 can
still be selected directly. CLI rollback restores both the previous executable
and its pre-upgrade session state. The existing 17.2.12 installation was left
unchanged. Service switching and audible voice acceptance remain human gates.

## Subsequent observation

On 2026-10-12 the upstream audit observed 18.10.3. It is recorded as a pending
candidate; 18.10.2 remains the recommendation until a separate protocol review.
