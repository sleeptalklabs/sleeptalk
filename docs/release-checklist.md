# Release checklist

QA for a release has a finish line: run the scenarios below once, on the
release commit, and ship when they pass. Anything found later goes into the
next patch release. The scenarios follow the
[support matrix](launch-support-matrix.md); if the matrix changes, change
this list with it.

## 0.1.0 release decision (2026-10-31)

The maintainer explicitly authorized publication of 0.1.0 with the human
release scenarios below recorded as **unverified**. They remain unchecked:
fresh interactive setup, agent-driven setup and key reuse, browser/phone and
tailnet calls, Discord speech, terminal audio, settings changes, and service
restart/reboot behavior. Automated checks do not establish that these worked
in a live session.

The release-preparation commit passed all 14 required CI checks and the
[release rehearsal](https://github.com/sleeptalklabs/sleeptalk/actions/runs/41245767336),
including package tests and artifact proof. This is a release-specific exception,
not a completed human test record or a change to the gates for future releases.

## Before the first public release (once)

- [x] GitHub: publish the reviewed source at `sleeptalklabs/sleeptalk` and
      verify `origin`, README badges, application links, and private reporting links.
- [x] PyPI: a pending trusted publisher is registered for project `sleeptalk`,
      owner `sleeptalklabs`, repository `sleeptalk`, workflow `release.yml`,
      environment `pypi` (verified 2026-10-31). The first successful upload
      creates the project; a pending publisher does not reserve its name.
- [x] GitHub: the `pypi` environment has required reviewers and permits
      deployment only from release tags matching `v*`.
- [x] GitHub: private vulnerability reporting (SECURITY.md links to it),
      secret scanning, and push protection are on.
- [x] GitHub: rulesets protect `main` (pull request, no force-push) and
      `v*` tags.
- [ ] Cloudflare: the selected $10 usage alert and API firewall rule are
      configured (see [operations](browser-service-operations.md#dashboard-follow-up-pending));
      the production Worker is `running`. The alert is not a spending cap.

## Scenarios (every release)

Record pass or fail and the commit. A failure blocks the release only if it
breaks a supported path.

1. [ ] **Fresh install from the README.** A clean macOS user (or VM), no
       prior `~/.sleeptalk`: follow "Get started" as written. Before tagging,
       download the `sleeptalk-release-artifacts` bundle from the successful
       CI run on the candidate commit; it includes the wheel, installer and
       constraints. Install with those constraints, then run `sleeptalk` in a
       project. Git and Node.js must not be required. After publication, repeat
       the download from the GitHub Release to verify the public links.
2. [ ] **Agent-driven setup.** Give an agent (Hermes or Claude Code) only
       the README prompt on a host without Sleeptalk. It installs using an
       available bundle or source, keeps the user's project as the workspace,
       checks the selected agent, and asks which device will join. It asks
       before reusing a key or sends the private setup page. The call link
       matches that device; no key appears in chat. Confirm an audible reply
       and that the voice agent is working in the intended project.
3. [ ] **Key reuse.** On a host whose agent already has an OpenRouter
       key, `host ensure` asks first; after a yes, `--reuse-key` sets voice
       up with nothing to paste.
4. [ ] **Hosted call, phone on mobile data.** Audio both ways, the
       transcript and task line appear, the settings menu changes voice.
5. [ ] **Tailnet call.** `--tailnet` from a VPS to a phone on the tailnet.
6. [ ] **Same-machine call.** `sleeptalk link` with the local browser.
7. [ ] **Discord.** A bot set up from the settings page or phone link
       joins the allowed channel, answers its operator, ignores anyone else.
8. [ ] **Terminal.** `sleeptalk --terminal`: talk, interrupt, end.
9. [ ] **Settings page.** `sleeptalk settings`: change voice,
       speech service (key and all), and delivery; the profile updates.
10. [ ] **Service.** `sleeptalk service install <slug>` survives a reboot
        (VPS).

## BYOA v1 readiness gate

Before promoting custom harness integration paths from preview to supported,
complete these checks on the release candidate. This gate is pending; the
current [support matrix](launch-support-matrix.md) still applies.

- [ ] A developer outside the implementation team connects a real custom
      harness using only the published BYOA guide, an installed package,
      and its scaffolds. Record setup problems and fix the guide or tooling.
- [ ] Exercise each connection path being promoted with a real harness,
      including a compatible ACP harness, a custom command, and an HTTP
      service if all three are promoted. Record exact versions, capabilities,
      and `adapter test --live --json` results; scaffold results alone are
      insufficient. Bridge and Python plugins can remain preview separately.
- [ ] A person completes the guide's [call checks](bring-your-own-agent.md#check-your-integration)
      for those integrations: reply, contextual follow-up, interruption,
      recovery, and any advertised permission or progress behavior. Record
      the call surface and distinguish these checks from automated results.
- [ ] Published integration instructions explain startup, authentication,
      session continuity, supported capabilities, and failure diagnostics.
      Verify actionable errors for a missing command, rejected credentials,
      an unreachable endpoint, and a program that does not speak the protocol.
- [ ] Document the compatibility policy for the interfaces being promoted,
      including versioning and migration for breaking changes. The existing
      [Bridge v1 version rules](agent-bridge-protocol.md#handshake-and-version-negotiation)
      cover that wire protocol; do not imply they guarantee arbitrary
      upstream harness versions or all Python imports.
- [ ] Update the support matrix, README, guide, and setup copy together to
      name exactly which paths are supported. Keep unsupported capabilities
      explicit. A standalone SDK is not a release prerequisite.

## Release

- [ ] CI is green on the release commit, including the release proof.
- [x] Review the remaining [Discord dependency advisory](#dependency-audit--2026-10-31); the scoped finding below remains unpatched.
- [x] `CHANGELOG.md`: the release heading has its date.
- [x] `scripts/install.sh` and the README point at the new GitHub release
      downloads and describe the included dependency constraints.
- [ ] Tag `vX.Y.Z` on `main`; approve the `pypi` environment when the
      release workflow asks.
- [ ] The README's pip command installs into a clean Python 3.11+ virtual
      environment and `sleeptalk --help` works outside a checkout. Check the
      GitHub preview before publication, then install `sleeptalk==X.Y.Z` with pip
      from PyPI after publishing.
- [ ] After the first PyPI upload is verified, replace the README's GitHub
      preview requirement with `python -m pip install sleeptalk`, remove its
      pending-publication note, and update the agent guide to match.
- [ ] GitHub Release includes the wheel, source archive, installer, constraints,
      checksums, and build/rollback records; downloading all assets and running
      `sha256sum -c SHA256SUMS` succeeds.


## Dependency audit — 2026-10-31

The release candidate updates certifi to 2026.10.18, dnspython to 2.9.0,
filelock to 4.2.1, and opentelemetry-api to 1.45.1 to address the Python
advisories reported by GitHub. The browser development-tool updates leave
`npm audit` with no known vulnerabilities in either JavaScript project.

An audit of the exported all-extras Python constraints still reports
[GHSA-mrfv-m5wm-5w6w / CVE-2025-69277](https://github.com/advisories/GHSA-mrfv-m5wm-5w6w)
in PyNaCl 1.5.0. `pip-audit` reports this same advisory under two PYSEC IDs.
The patch is in PyNaCl 1.6.2, but the available `discord.py[voice]` 2.7.1
metadata requires PyNaCl below 1.6. No dependency override was applied.

The advisory concerns `crypto_core_ed25519_is_valid_point`. Static inspection
found no calls to that API in Sleeptalk or the installed Discord package;
Discord voice uses `nacl.secret.Aead` and `SecretBox`.

Release review completed on 2026-10-31: the same inspection covered
`discord-ext-voice-recv` 0.5.2a179, whose receive path also uses those symmetric
primitives. No use of the affected point-validation API was found in these
three codebases. The [upstream maintainer's analysis](https://00f.net/2025/12/30/libsodium-vulnerability/)
limits the bug to that low-level API; it does not affect the high-level signing
APIs either. Based on those call paths, this finding is not considered a blocker
for the reviewed 0.1.0 voice integration.

This is a scoped dependency exception, not a patch or a claim that all PyNaCl
uses are safe. Keep the audit finding visible and retain the resolver-compatible
version; do not force an override or suppress the advisory. Re-review if the
voice dependencies or cryptographic calls change, and adopt PyNaCl 1.6.2 or newer
as soon as Discord's published constraints permit it. This review does not cover
custom plugins or applications reusing PyNaCl outside Sleeptalk's voice paths.
