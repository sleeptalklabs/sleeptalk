# Agent harness upgrades

Sleeptalk supports people who already have an agent installed, including an
older release. The newest upstream version and a version verified with
Sleeptalk are different facts. An upgrade must preserve the caller's
permissions, workspace, model selection, and conversation state.

This policy applies to Pi, Oh My Pi, Hermes, OpenClaw, Codex, Claude Code,
and Cursor, including their distinct CLI, SDK, ACP, and gateway integrations.
Third-party adapters remain responsible for their own upstream dependencies.

## Existing installations come first

- Keep using the executable or runtime the user selected. Discovery must not
  replace, update, downgrade, or take ownership of it. Finding an older
  installation is not the same as finding a missing dependency.
- A fresh-install recommendation applies only when a dependency is missing
  and installation has been requested. Never run an updater during a call,
  startup, readiness check, or version audit.
- Verify exact releases, not an inferred range. Passing on 0.84.0 and 1.1.0
  does not prove every release between them, or a modified build reporting
  the same version. Unknown versions are **unverified**, not automatically
  incompatible. Offer a protocol check and explain any actual missing
  capability before asking the user to change their installation.
- Keep existing protocol and permission checks. Unverified versions may be
  tried, but a failed required handshake or permission hook must stop the
  session before a prompt or tool executes. Never resolve an incompatibility
  by silently enabling automatic approval or selecting a weaker runtime.
- Exact checks remain necessary where the integration uses private SDK
  interfaces, hash-checked source, or generated schemas. The current Codex,
  isolated Hermes, protected OMP, and protected Cursor checks are examples.
  These require an integration migration, not simply a newer executable.

## Version inventory and evidence

The packaged [inventory](../sleeptalk/harness_versions.json) records the last
upstream release observed, exact versions verified under this policy, a
fresh-install recommendation when qualified, and outstanding work. Pi's
installer and install hints read their recommendation from this file.
OMP's CLI installer and exact protected SDK allowlist also read this inventory;
the two OMP surfaces have separate version evidence. Cursor SDK readiness and
its protected allowlist also use this inventory; its project/lock pins are
checked against it in tests.
Codex schemas, Claude SDK readiness, and Codex/Claude/OpenClaw missing-binary
installers now use this inventory too. The isolated Hermes bridge selects exact
source manifests from it. Claude SDK project and lock pins are checked against
the inventory. Existing runtimes are never updated by these readiness paths.

```bash
sleeptalk adapter versions
sleeptalk adapter versions --pi-bin pi
sleeptalk adapter versions --pi-bin /absolute/path/to/pi --json
sleeptalk adapter versions --omp-bin /absolute/path/to/omp --json
sleeptalk adapter versions --cursor-bin /absolute/path/to/agent --json
sleeptalk adapter versions --upstream --json
```

The default command is offline. `--pi-bin`, `--omp-bin` or `--cursor-bin` runs
only that executable's version command, in a temporary home without inherited provider credentials. It does
not read a voice profile or establish that a running service uses the same
executable. For profiles using `pi_bin`, inspect that exact path. `--upstream`
reads public package/release metadata; it never executes an installer.
OMP inspection also reads the selected executable's package metadata to
distinguish an npm SDK from a compiled CLI. SDK eligibility still requires
Bun and a successful protected startup; the version command does not prove these.

There are separate evidence levels:

| Evidence | What it establishes |
| --- | --- |
| Last observed upstream | A published release exists; no compatibility claim |
| Verified version | The actual upstream runtime passed the documented automated protocol checks |
| Live-provider check | The chosen account/model can complete real turns; record separately from fixtures |
| Deployment acceptance | A human checked the intended voice surface and service lifecycle |

`queued` means the integration has not yet been qualified under this policy,
not that its existing adapter is unsupported. `candidate` records partial
evidence without a fresh-install recommendation; Cursor CLI currently has
`handshake_verified` evidence only. `protocol-verified` permits a
fresh-install recommendation, but does not certify live voice. Read each
record's verification surface: Cursor SDK wire tests do not qualify authenticated execution through its JS bridge. Missing,
skipped, timed-out, or failed checks are never recorded as passes.

## Review cadence and ownership

Maintainers review harness releases weekly and before a Sleeptalk release.
Security fixes and vendor deprecations get an expedited review. Major releases,
0.x changes, and private API changes require the same behavioral checks as
other upgrades; semantic versioning is not evidence of compatibility.

[The release audit workflow](../.github/workflows/harness-releases.yml) runs
weekly and on manual dispatch once merged into the default branch. It checks
all inventory entries, retains a JSON report, and fails when a release differs
from the last reviewed version or metadata is unavailable. A failure asks for
review; it never upgrades anything or opens/merges a PR. An upstream mismatch
may be newer, withdrawn, or a channel change: inspect it before updating the
inventory's `latest_observed` and review date.

The ordinary Dependabot configuration continues to review Python SDKs and
workflow dependencies. External CLI releases are covered by the harness
audit. Each upgrade should be a focused change with an evidence record;
unrelated harnesses must not be upgraded together simply to clear the audit.

## Upgrade procedure

1. **Inventory the actual runtime.** Record its resolved executable or SDK,
   version, installation method, selected transport, permission mode, and any
   wrapper or local modifications. For Claude, inspect the SDK's bundled CLI;
   for remote gateways, record the server version as well as the local client.
   Never place keys, private profiles, or session contents in an evidence file.
2. **Review the target.** Use official release notes and package metadata.
   Check RPC/ACP framing, terminal events, tool hooks, auth, default tools,
   extension loading, session format, and runtime requirements. Record an
   exact version/tag and source. Keep the previous installation intact.
3. **Stage separately.** Install the target into a distinct directory or
   environment. Keep the resolved package lock/checksums with the test report.
   Use a temporary home and copied/disposable session state; never test a
   migration against the only copy of a user's conversations.
4. **Prove behavior.** Run focused adapter tests and the shared conformance
   suite. Exercise the actual target runtime against a deterministic provider
   wherever possible: streaming, terminal settlement, tool events, approval
   and denial, cancellation followed by another turn, usage, steering, and
   session resume. Verify that protected startup does not execute ambient
   extensions. Document anything still covered only by fakes or not covered.
5. **Run repository gates.** After focused tests, run
   `uv run python -c "from sleeptalk.cli import main"` and `uv run pytest -q`.
   Update installer hints, metadata, schema/source pins, fixtures, and docs
   together. Read the diff before committing. A failed required gate leaves
   the upgrade a candidate; do not promote it by editing a version string.
6. **Record and promote.** Add the verified release to the inventory, choose
   the fresh-install recommendation, and retain at least the previous verified
   release in compatibility CI during the next upgrade cycle. Preserve older
   support until a documented incompatibility or security reason requires
   removal. The release record names the tests, environment, limitations, and
   remaining human checks.
7. **Accept deployment separately.** Live microphone, Discord speech, and
   service restart checks are human gates. Before a user switches an existing
   profile, verify its provider and voice surface, then perform the service
   switch between calls. Record the actual result; automated fixtures cannot
   complete these gates.

For an existing profile, `sleeptalk adapter test --config PROFILE` performs
the offline contract inspection. `--live` sends real model prompts and may
spend tokens; it is a separate provider check, not proof of audible speech.
Use `--fixture` only with an agent/provider that implements the fixture
prompts. See [adapter conformance](agent-conformance.md).

## Rollback

Retain the previous executable/environment, dependency resolution, profile
binding, and a private backup of session state. For Pi, an explicit `pi_bin`
can select a side-by-side installation; do not overwrite global `pi` to stage
a candidate. If changing a bound profile, rebind it through the supported
setup flow; do not hand-edit its binding hash.

Rollback restores the previous runtime **and compatible state**. A newer
harness may have migrated sessions or credentials, so pointing an old binary
at the newly written state is not a proven rollback. Keep those copies
separate until both forward and backward compatibility have been exercised.
Service restarts and live rollback acceptance stay human gates.

## Rollout checklist

All targets below were observed on 2026-10-12 and must be rechecked when their
work starts. Each row requires its own evidence before recommendation.

| Order | Harness | Target | Work remaining |
| --- | --- | --- | --- |
| 1 | Pi | 1.1.0; retain 0.84.0 | Automated evidence in [Pi record](harness-upgrades/pi.md); human voice/deployment acceptance pending |
| 2 | Oh My Pi | 18.10.2 recommended; 18.10.3 newly observed | Automated evidence in [OMP record](harness-upgrades/omp.md); human voice/deployment acceptance pending |
| 3 | Cursor | CLI 2026.10.01-e373342; SDK 1.0.37 | SDK wire/bridge-startup evidence in [Cursor record](harness-upgrades/cursor.md); CLI handshake only, authenticated ACP qualification and installer pinning remain open |
| 4 | Codex | 0.162.1; retain 0.153.4 | [Actual app-server and schema evidence](harness-upgrades/codex.md); human deployment acceptance pending |
| 5 | Claude Code | CLI 2.1.296; SDK 0.2.165 (bundled CLI 2.1.294) | [Separate SDK/bundled/standalone evidence](harness-upgrades/claude.md); human deployment acceptance pending |
| 6 | Hermes | Isolated bridge 0.21.7; retain source 0.21.3 | [Source bridge evidence](harness-upgrades/hermes.md); customized installed and remote runtimes remain separate |
| 7 | OpenClaw | 2026.10.2; retain 2026.7.1-2 | [Gateway/ACP evidence and remote ownership](harness-upgrades/openclaw.md); Node prerequisite and human deployment gates apply |

The [remaining-harness compatibility workflow](../.github/workflows/remaining-harness-compatibility.yml)
now stages Codex, Claude, Hermes and OpenClaw independently on Linux and macOS,
including previous versions. It retains test results and dependency evidence.
The [validation record](harness-upgrades/validation.md) lists the actual-runtime
and repository gates. Local qualification does not claim that newly added hosted CI has run. OMP
18.10.3 is observed but not promoted; its next review is still pending.

Completing this inventory does not complete deployment acceptance. Keep each row open
until its protocol evidence and deployment status are recorded explicitly.
