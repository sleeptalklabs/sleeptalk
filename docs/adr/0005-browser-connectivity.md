# ADR 0005: Browser connectivity — Cloudflare Realtime TURN as the operator-configured reference relay

Date: 2026-08-21
Status: Accepted (approved by the operator on 2026-08-21; amended on
2026-09-09 for embedded chat browsers, on 2026-09-15 for anonymous
installation grants, and on 2026-10-21 so later call proofs point at the
support matrix. TURN/TLS on 443 and a UDP-blocked host remain open)

## Approval note

The operator approves the direct-first, Cloudflare TURN fallback topology and
its stated cost, abuse, credential, and regional boundaries. This approval
settles the architecture so downstream hardening can proceed. It does not
claim that the pending representative-network tests have already run or that
the public-launch security gates are already complete.

## Decision

Cloudflare Realtime TURN is the MVP's operator-configured reference relay.
ICE remains **direct-first** and uses TURN only when required. Production
public browser calling requires TURN unless the operator explicitly accepts
the documented STUN-only limitation. Shared Sleeptalk-managed TURN,
alternative vendors, and self-hosted TURN infrastructure are deferred until
demand, billing attribution, residency policy, and abuse containment justify
them (the later `r2-managed-turn` phase).

Topology modes:

1. **Direct-first + TURN fallback (production reference).** The browser and
   host both receive short-lived per-peer TURN credentials once the browser
   redeems a ticket (the host over its authenticated signaling socket, never
   in the mint response);
   ICE selects direct paths whenever possible and relays only when
   necessary. This is the default when TURN secrets are configured.
2. **STUN-only (explicitly degraded).** When the operator configures no
   TURN secrets the deployment is a documented development/self-hosting
   mode with a reliability limitation; it is not a public-calling posture.
3. **Relay-required embedded browsers.** Any browser classified as embedded
   sets `iceTransportPolicy` to `relay`. Generic platform signatures drive the
   classification; known chat-product markers are additional detection hints,
   not policy categories. Ticket redemption requires working TURN credentials
   and fails before consuming the ticket when the relay is unavailable.
   Browser detection happens when the link opens, because a link sent through
   chat can still open in an external browser.

## Why this topology

- **ICE already prefers direct**: relay traffic is selected only when the
  direct path fails (the transport telemetry exposes `pathType` relay/direct
  per call, r2-browser-ice-diagnostics), keeping latency and egress cost
  low while preserving connectivity.
- **Local evidence**: of 29 classified completed calls, 22 connected
  directly and 7 (≈24%) required TURN; those relay sessions covered 28.1
  minutes at 15–64 ms RTT (avg 26 ms). TURN is not hypothetical — a quarter
  of real completions selected it, which rules out STUN-only for public
  browser calling.
- **Cost**: Cloudflare TURN includes 1,000 GB/month free, then $0.05/GB
  egress; measured relay sessions carried ~11 MB endpoint media, so
  voice-only MVP relay cost is negligible. Provider analytics remain the
  authoritative billing measure.
- **Alternatives rejected for MVP**: self-hosted coturn (operational,
  security, regional, availability burden); shared Sleeptalk-managed TURN
  (needs billing, tenant attribution, abuse containment first).

## Boundaries (explicit)

### Credential and abuse boundaries

- **Mint authorization.** A fresh host creates an anonymous, origin-scoped
  installation grant. The service stores only its secret hash. Durable
  per-install mint and concurrency quotas replace the old deployment-wide
  `RENDEZVOUS_MINT_TOKEN` and isolate-local IP counter. Anonymous enrollment
  has durable source-network (IPv4 /24, IPv6 /48) and global limits. The
  browser continues receiving access only through the short-lived
  single-use ticket.
- **Spend is charged at redemption.** Minting a link reserves a call slot
  but charges no relay allowance and issues no TURN credential. When a
  browser redeems, the call's relay units are charged against the
  installation's daily budget, its enrolling network's daily budget, and a
  global backstop, and only then are TURN credentials issued. A link that is
  minted and abandoned costs nothing, and one network cannot exhaust the
  service for everyone.
- **Credential lifetime**: 30-minute session cap + 1-minute grace = 31
  minutes from redemption, with **separate credentials per peer**. Links
  live 1 to 5 minutes; shorter requests are raised to one minute.
- **Key hygiene**: separate production/staging TURN keys; egress and
  concurrency monitored by credential/session; credentials tagged with an
  opaque session identifier where possible (Cloudflare-recommended abuse
  detection). Budget alerts are informational; application quotas and
  revocation are the actual spend controls.

### Region and compliance boundaries

- Global anycast relay placement; **no customer-selectable residency**.
- China and residency-regulated deployments are **unsupported** without
  separate review (Cloudflare TURN excludes the China network).
- **No self-serve SLA claim** (SLA guarantees are an Enterprise benefit).
- Cloudflare sees DTLS-encrypted media and transport metadata (IPs, ports,
  session timing) — never transcripts or speech-provider credentials.

## Outage behavior (degradation matrix)

| Condition | Behavior |
|---|---|
| Both TURN secrets missing | STUN-only, with an operator warning |
| Partial/invalid secrets | Fail configuration checks (fail closed) |
| Transient timeout / 429 / 5xx | Degrade to STUN-only + explicit redacted telemetry |
| Auth failure / malformed provider response | Fail closed and alert |

The degradation rows above apply to direct-first browsers. A relay-required
embedded browser fails closed for missing credentials, timeouts, `429`, and
`5xx`. It never falls back to STUN-only operation.

This preserves direct-capable calls during a Cloudflare control-plane
incident without concealing broken configuration.

## Transport protocol requirement (open condition)

UDP evidence exists (all 7 relay completions); **TURN/TLS on 443 is not yet
proven**. aiortc 1.15 uses only the first TURN URL it encounters, so the
host generally selects UDP 3478 and does not retry with TLS. Before claiming
corporate/VPN reliability: prove forced-relay TLS-443 from the browser, test
a UDP-blocked agent host, and either implement a bounded TLS retry for the
host or document that the MVP assumes outbound UDP on the host.

## Network evidence matrix

Proofs recorded after this ADR live in the [support matrix](../launch-support-matrix.md): the hosted relay and tailnet calls on 2026-09-20, and OpenAI Realtime browser calls on 2026-09-25. Those notes leave the rows below open. TURN/TLS on 443 and a UDP-blocked host stay unproven. The [release checklist](../release-checklist.md) still requires a phone-on-mobile-data call on the release commit. Record that run in this table when it happens.

| Scenario | Status |
|---|---|
| Home Wi-Fi, normal ICE | pending — operator |
| Cellular/CGNAT | pending — operator |
| Real VPN | pending — operator |
| Corporate / explicitly UDP-blocked | pending — operator |
| Dual-stack IPv4/IPv6 | pending — operator |
| IPv6-only / NAT64 (if available) | pending — operator |
| Forced relay over UDP | pending — operator |
| Forced relay over TLS 443 | pending — operator |
| Agent-host-restricted (host on blocked network) | pending — operator |

Record only: scenario label, browser/OS, success, direct/relay, UDP/TCP/TLS,
setup time, RTT, stable failure category. No addresses, SDP, credentials,
audio, or transcripts.

## Consequences

- `r2-browser-connectivity` hardens the approved topology: independent mint
  grants, the degradation matrix above, bounded reconnect, and user-facing
  recovery copy.
- The architecture is operator-approved as of 2026-08-21. Later proofs are
  the support-matrix notes named above. The rows still marked pending in
  this ADR stay operator work, including TURN/TLS on 443 and a UDP-blocked host.
- Managed TURN (multi-tenant) remains out of scope until Phase r2-managed-turn.

## References

- Local connection-success evidence: `logs/voice_latency.jsonl`, classified
  `call_complete` events through 2026-08-20 (22 direct and 7 TURN completions).
- Cloudflare Realtime TURN service, protocols, regions, limits, and pricing:
  <https://developers.cloudflare.com/realtime/turn/>
- Cloudflare TURN credential generation and revocation:
  <https://developers.cloudflare.com/realtime/turn/generate-credentials/>
- Cloudflare TURN abuse monitoring and credential attribution:
  <https://developers.cloudflare.com/realtime/turn/replacing-existing/>
- Cloudflare TURN analytics:
  <https://developers.cloudflare.com/realtime/turn/analytics/>
- Cloudflare TURN privacy, compliance, China, and SLA boundaries:
  <https://developers.cloudflare.com/realtime/turn/faq/>
