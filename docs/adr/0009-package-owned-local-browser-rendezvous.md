# ADR 0009: Package-owned local browser rendezvous

Date: 2026-09-10
Status: Accepted

## Problem

An explicit Web start used the hosted Cloudflare Worker and always printed an
agent JSON receipt. This contradicted the operator's request for a browser on
the same machine. Pointing at `127.0.0.1:8788` was not a solution because an
installed wheel did not start or ship the separate Wrangler project.

## Decision

Treat voice delivery and output format as separate, resolved launch properties:

- Explicit human Web uses a local browser and plain URL.
- An omitted surface or named chat integration uses remote Web and JSON.
- Explicit Terminal uses attached native audio.
- `--json` opts a human Web call into a receipt, and `--rendezvous` explicitly
  selects a custom remote origin.

Ship the compiled Vite browser UI inside the Python package and use the same
artifact for Worker deployment. A detached local call owns one `aiohttp`
rendezvous bound to `127.0.0.1` on a dynamic port. It holds one in-memory
ticket, serves the UI, accepts one atomic redemption, authenticates the agent
and browser with separate keys, relays bounded signaling messages, and exits
on hangup, expiry, inactivity, session cap, or Host stop.

There is no PIN. The local listener is unreachable off-machine; the URL keeps
a 256-bit secret in its fragment; redemption is single-use and short-lived;
and signaling uses independent credentials. A PIN would add friction without
protecting a meaningful additional boundary for a same-user loopback process.

Local calls return no STUN or TURN servers. Same-machine host ICE candidates
are sufficient and avoid contacting Cloudflare. Hosted calls keep the Worker,
TURN policy, mint authorization, and cross-device behavior.

## Consequences

`sleeptalk link` and interactive Web init work from an installed wheel without
Node, Wrangler, or a repository checkout. `sleeptalk host start-voice web
--profile maxwell` prints a literal localhost URL instead of JSON. Agent and
chat integrations retain the hosted receipt contract because a localhost URL
would resolve to the recipient device rather than the Sleeptalk host.

The Python service and Worker now implement the same browser signaling
contract. Protocol and browser tests must cover both paths, and release proof
must verify that the wheel contains the shared browser assets and can bind the
loopback listener.

## Alternatives

Supervising Wrangler would add a Node runtime and development tooling to every
installation. Rewriting a hosted hostname to localhost would produce a dead or
misleading URL. A localhost reverse proxy to Cloudflare would preserve the
address bar but not the local security and availability boundary. All three
were rejected.
