# ADR 0008: Package-owned setup and surface handoff

Status: accepted, 2026-09-10

## Context

Sleeptalk stores an agent and voice-stack profile, but the caller decides how
the current conversation receives voice. A terminal or desktop caller needs an
attached local session. A chat caller needs a bounded browser link. Earlier
setup code treated the stored transport as permanent identity and sometimes
handed Web or Discord launches to a sibling `server/` checkout. That made an
installed wheel insufficient and caused readiness checks to inspect the wrong
surface.

Setup also spans a public profile and a gitignored secret file. An interruption
must not make a new profile boot with missing credentials.

## Decision

Keep the existing profile schema. Apply the requested surface as an in-memory
invocation override:

```python
load_runtime_config(path, surface="terminal" | "web" | "discord")
```

The loader applies the override before checking environment variables,
binaries, Python extras, and native dependencies. `host ensure` passes the same
surface to Doctor and reports that effective surface without rewriting the
profile.

Use `ensure_setup()` as the setup operation for Host, answers-file setup, and
the interactive wizard. Prompting and secret collection remain outside it. The
operation validates answers, repairs only dependencies for the effective
surface when repair is enabled, and returns `ready`, `needs_input`, or `failed`
without printing secrets.

All launch instructions and execution are package-owned:

- Web: `sleeptalk link --config PROFILE --foreground` (package-owned localhost)
- Terminal and Discord: `sleeptalk run --config PROFILE`
- Agent chat: detached `sleeptalk host start-voice` with a redacted receipt

The browser link is a single-use, bounded bearer capability. Opening the link
authorizes the call; there is no additional PIN exchange. Production ticket
minting remains protected separately by the rendezvous mint credential.

Commit the secret file before the profile. The profile is the bootable commit
point. If the second replace is interrupted, unused credentials may remain,
but no new profile can start without them. Both writes are atomic replacements,
and rerunning setup converges without changing identical files or unrelated
profiles and environment entries.

## Consequences

One profile can serve local Terminal, local Web, and hosted agent Web calls
without false conflicts. A wheel no longer discovers or requires the
repository's `server/` directory or a separate Wrangler process. Dependency
errors describe the complete selected extra instead of using one import as a
proxy.

The two-file update is recoverable and idempotent, but it is not a general
multi-file transaction journal. Plan 07 owns clean-environment wheel matrices,
artifact identity, and reproducibility evidence. Provider signup automation,
persistent daemons, and new agent adapters remain out of scope.

## Alternatives considered

A durable profile-schema migration that removed surface identity entirely was
rejected because Plan 01 does not need it. A new setup framework with separate
stores, journals, and handoff modules was also rejected; the existing setup
operation could own the required behavior with a smaller compatibility surface.
