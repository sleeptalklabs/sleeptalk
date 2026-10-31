# Pi upgrade record

Date: 2026-10-12. Target: [Pi 1.1.0](https://github.com/earendil-works/pi/releases/tag/v1.1.0),
`@earendil-works/pi-coding-agent@1.1.0`. Previous installed release: 0.84.0.
The package namespace is `@earendil-works`, not the deprecated `@mariozechner` scope.

## Scope

Qualify the npm CLI in RPC mode, with Sleeptalk's `pending` and `deny`
permission modes. Keep 0.84.0 working for users who already have Pi.
Recommend exactly 1.1.0 when a missing Pi installation is requested.
Existing installations are neither upgraded nor rejected based on age.

The main upstream changes relevant to review are the new `aborted` field on
`agent_settled`, changes to default tool selection, and fixes to standalone
binary environment loading. Our proof uses the npm CLI; it does not certify
standalone distributions. Model-catalog changes are not a guarantee that a
particular account can access a model.

## Automated evidence

`tests/test_pi_release.py` runs the actual selected Pi executable against a
loopback-only fake OpenAI-compatible provider. It uses disposable homes and
workspaces, no real credentials, and a network guard that rejects connections
outside that provider. It does not replace Pi's RPC server, extension loader,
tool execution, or session store.

Both 0.84.0 and 1.1.0 passed locally on macOS arm64 with Node 22.23.1 and
Python 3.11.15:

- Two streamed sentences, usage events, and session identity.
- Saved session resume in a fresh Pi process, including previous messages.
- `deny` blocks a real write tool without presenting an approval.
- `pending` does not execute a write before a decision; explicit denial and
  one-time approval produce the corresponding filesystem result.
- Abort a streaming response and successfully complete the following turn.
- Project and global extension markers do not run during protected startup.

The suite found and fixed a Sleeptalk issue: ordinary `get_state` admission
did not retain the upstream session ID, so `SessionIdentified` was emitted
only when warmup had run. Identity is now recorded during normal admission
and re-announced when the upstream session changes.

Additional focused tests cover RPC ownership, steering, permission replay,
timeouts, onboarding, and shared adapter conformance. Actual-runtime steering
and cross-version session migration are not yet certified by this record.

Results from the working checkout on 2026-10-12:

| Check | Result |
| --- | --- |
| Actual Pi 0.84.0, `tests/test_pi_release.py` | 5 passed |
| Actual Pi 1.1.0, `tests/test_pi_release.py` | 5 passed |
| Focused Pi, inventory, onboarding, readiness, BYO, and shared conformance tests | 291 passed, 17 skipped |
| `uv run python -c "from sleeptalk.cli import main"` | Passed |
| `uv run pytest -q` | 4,946 passed, 63 skipped, 1 expected failure |
| Package import boundary | Passed |
| Build wheel and read its inventory outside the checkout | Passed |
| Official upstream release audit, all nine CLI/SDK entries | Passed; no change from the recorded releases |

The full suite skips the opt-in actual-release tests; the two explicit runs
above supply that evidence. The initial restricted focused run could not
bind loopback sockets; the reported passing run used local socket access.
These results describe automated checks on the current working tree, not a
released artifact or production deployment.

The [Pi compatibility workflow](../../.github/workflows/pi-compatibility.yml)
reads the verified versions from the inventory and runs both releases on
Ubuntu and macOS. Those CI runs are pending until the workflow is pushed.
It retains JUnit results and each npm resolution as build artifacts.

To reproduce with a separate target install:

```bash
npm install --prefix /tmp/sleeptalk-pi-1.1.0 --save-exact --no-audit --no-fund \
  @earendil-works/pi-coding-agent@1.1.0
SLEEPTALK_TEST_PI=/tmp/sleeptalk-pi-1.1.0/node_modules/.bin/pi \
SLEEPTALK_TEST_PI_VERSION=1.1.0 \
  uv run pytest tests/test_pi_release.py -q
```

Without `SLEEPTALK_TEST_PI`, the actual-release tests skip. CI sets it
explicitly and checks the executable's version against the matrix entry.

## Human acceptance and rollback

Pending: real provider/account verification, microphone speech, Discord
speech, and service restart/resume on the intended host. No service restart
or global Pi upgrade was performed for this record. The existing Pi 0.84.0
installation remains available.

Before a deployment switch, record the selected `pi_bin`, privately back up
the existing profile/binding and sessions, and keep the target's session
state separate. To roll back, restore the old profile/binding, executable,
and pre-upgrade session snapshot. Resuming a session written by 1.1.0 with
0.84.0 is not yet a verified rollback path.
