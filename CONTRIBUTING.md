# Contributing

PRs are welcome against `main`. Keep them small. One concern per PR.

## Set up

Install Python 3.11+ and [uv](https://docs.astral.sh/uv/):

```bash
git clone https://github.com/sleeptalklabs/sleeptalk.git
cd sleeptalk
uv sync --dev --all-extras
```

The terminal extra needs PortAudio (`brew install portaudio` or
`sudo apt install libportaudio2`). Discord voice needs libopus.

For browser UI or hosted-service changes, also install Node.js 22+ and run
`npm ci` inside `rendezvous/`. Browser source lives in `rendezvous/web/`; the
Worker lives in `rendezvous/src/`. The private key-entry page lives in
`remote-entry-web/`. Both build into the Python package (`sleeptalk/_web/`
and `sleeptalk/_remote_entry/`), and those built files are tracked.

Keys go in this computer's private store (a 0600 file, or the Keychain with
`sleeptalk[credentials]`);
profiles hold only references to them, and a folder's `.env` is never read
(see [your keys](docs/your-keys.md)). Operator profiles under
`config/profiles/` stay local.
Run `sleeptalk trust` once in your checkout so its `config/profiles/` is used
instead of `~/.sleeptalk`.

## Run tests

```bash
uv run pytest                                   # package and runtime suites
uv run pytest scripts/test_audit_release_contents.py
(cd rendezvous && npm test && npm run build)
```

To walk an agent's key request on the settings page in your own browser,
against a throwaway host that accepts any key and fakes Discord:

```bash
uv run python scripts/preview_setup_page.py --mode open|preferred|fixed|reuse|discord
```

Wheels built by CI from `main` carry a local version label such as
`0.1.0+ci.123`, increasing with the run, so installing a newer wheel over an
older one always upgrades; a release tag builds the bare version.

For packaging changes:

```bash
bash scripts/prove_release_wheel.sh
```

The release proof builds the wheel and source archive, checks every member
against tracked inputs, installs into a blank environment, and runs
package-only web and host checks. Docker checks run only when a daemon is
available.

It also exports `dist/constraints.txt` from `uv.lock` and copies the standalone
installer to `dist/install.sh`. CI includes both beside the wheel in the
`sleeptalk-release-artifacts` bundle; the release workflow also uploads a
`sleeptalk-install` bundle and publishes its files on GitHub Releases after
PyPI publishing succeeds. Both bundles include every file named in `SHA256SUMS`.
Constraints cover all optional dependencies without
installing unselected extras. Install that reviewed bundle outside a checkout:

```bash
sh install.sh --constraints constraints.txt ./sleeptalk-*.whl
```

Cutting a release follows the [release checklist](docs/release-checklist.md):
a fixed set of scenarios run once on the release commit, then the tag.

## Where things live

Agent harness and SDK changes follow the [harness upgrade policy](docs/harness-upgrades.md).
Keep version evidence, install recommendations, and compatibility tests together;
existing user installations must not be silently upgraded.

- Conversation turn ownership: `RealtimeSession` in `sleeptalk/session.py`.
- Cascade speech behavior: `VoicePolicyRuntime` in `sleeptalk/voice_policy_runtime.py`.
- Adding an agent: [adapter contract](docs/agent-adapter-contract.md) and
  [component registration](docs/component-contracts.md).
- Shared setup answers, validation, and profile persistence:
  `sleeptalk/setup_core.py`. Browser settings and agent commands call this
  directly; `sleeptalk/wizard.py` owns terminal prompts and re-exports the
  existing setup names for compatibility.
- Python repair requirements come from the installed package metadata generated
  from `pyproject.toml`; `sleeptalk/sysdeps.py` keeps capability/import checks.

## Open a PR

1. Branch from `main`.
2. Make the smallest change that proves the fix or feature.
3. Run the suites that cover what you touched. Say what you ran in the PR.
4. Use an imperative commit subject. Example: `fix(call): reject a reused ticket`.

Discord microphone tests and `sleeptalk run` stay human gates. They are not
the default proof path.
