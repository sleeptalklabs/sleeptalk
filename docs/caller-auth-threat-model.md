# Caller authentication threat model (v0.1)

## Decision

v0.1 uses a **bearer single-use link**. The join secret lives in the URL
**fragment**. Redemption is one-time. Tickets expire quickly. Replay fails
closed. No second factor ships in v0.1.

## Why

The launch promise is a chat-safe link a local agent can return without hosting
caller accounts. A second factor improves safety but needs a delivery path that
does not dump secrets into chat logs. That path is not ready, so v0.1 stays on
the bearer link and tightens the hosted mint edge instead.

## Required controls

| Control | Requirement |
|---|---|
| Fragment secret | Secret only in `#…`; never as a query param |
| Referrer | Fragment must not appear in `Referrer` or analytics |
| One-time redeem | Second redeem fails; ticket burned on first success |
| Expiry | Short TTL; expired tickets fail closed |
| Replay | Replayed signaling or ticket IDs fail closed |
| Hosted mint | Per-install grant, hashed at rest, quotas, revocation, kill switch |
| Receipts | Join URL may appear in chat-safe JSON; mint tokens, TURN secrets, PINs, and local paths must not |
| Self-host | Documented override; same ticket rules apply |

## Residual risk

Anyone who obtains the unused link before expiry can join. Mitigations are short
TTL, one-time redeem, grant quotas, and operator kill switch. A second factor
remains an explicit post-v0.1 option if outsider QA or abuse data demands it.

The hosted service serves the call and key-entry pages it relays for, so the
end-to-end protections (keys encrypted to the host's key from the link
fragment, media and the transcript data channel under DTLS) hold only while
it serves this repository's code. The relay cannot read what those pages
send, but a compromised deployment could replace the pages. This is a limit
of delivering the client as a web page, not something the protocol can
close. Tailnet and local-rendezvous calls load the pages from the host.

## Tests before Supported Web ships

Fragment handling, referrer behavior, one-time redemption, expiry, replay,
grant abuse, and receipt redaction must pass on the release commit.
