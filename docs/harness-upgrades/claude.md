# Claude qualification — 2026-10-12

| Surface | Recommendation | Previous tested version |
| --- | --- | --- |
| Python Agent SDK | 0.2.165 | 0.2.157 |
| SDK-bundled CLI | 2.1.294 inside SDK 0.2.165 | 2.1.277 inside SDK 0.2.157 |
| Standalone CLI fallback | 2.1.296 | 2.1.292 |

The SDK normally selects its bundled executable before searching PATH.
Updating a global `claude` therefore does not necessarily change Sleeptalk's
runtime. All four combinations above were tested: each SDK with its own bundled
CLI, and SDK 0.2.165 with each standalone CLI. Standalone qualification overrides
only the SDK's executable lookup in the test; production retains upstream lookup.

`tests/test_claude_release.py` runs real binaries against a synthetic local
Anthropic Messages SSE server, with private homes and inherited credentials
removed. Each combination passes text, approval, and denial cases (3 tests),
including repeated turns, session identity, usage, and a disposable file written
only after approval. Existing adapter/conformance fixtures cover cancellation,
startup settings, tool events, and lifecycle; those are not additional actual
binary/provider claims. Session migration across versions remains unqualified.

The SDK extra, lockfile, and missing-dependency install hint now agree on exact
0.2.165. The standalone missing-binary installer pins 2.1.296 with Node 22+ and
npm engine checks. Existing installations are retained. The npm package needs
its postinstall step to replace its placeholder with the platform binary;
CI runs that reviewed step after staging with dependency scripts disabled.

Sources: [SDK releases](https://github.com/anthropics/claude-agent-sdk-python/releases),
[SDK changelog](https://github.com/anthropics/claude-agent-sdk-python/blob/main/CHANGELOG.md),
and exact npm/PyPI artifacts in `remaining-artifacts.json`. No SDK adapter API
change was needed for these checked behaviors. Evidence is macOS arm64; the
Linux/macOS workflow is added but not yet executed. Real account/model, live
voice, service restart, and persistent-state upgrade/rollback remain open.
