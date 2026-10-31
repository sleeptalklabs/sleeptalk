# ADR 0004: Commercial boundary — what Sleeptalk sells and who owns each credential

Date: 2026-08-20
Status: Proposed

## Relationship to Roadmap 2.0 and prior ADRs

Implements Roadmap 2.0 §1.3 (product modes), §2.1 (hard MVP boundary), and the
Phase 0 task `r2-commercial-adr`. Terms follow
the product vocabulary: **payment mode**
(Sleeptalk credits vs BYOK), **account**, **installation**, **access mode**,
**operation**, **attempt**. ADR 0002 decides *how* hosted provider access is
wired technically; this ADR decides *what is bought and sold* and where every
credential lives. Nothing here reverses ADR 0002's safety requirements.

## Decision

### 1. What Sleeptalk sells

Sleeptalk sells **hosted speech operations**: prepaid, metered units of
provider speech work (`stt.stream`, `tts.stream`, `tts.buffered`, and — when
native routes are sold — `rt.audio`), delivered through the access modes of
ADR 0002, priced from the provider catalog, debited from an account's credit
balance per ADR 0002's ledger rules.

Sleeptalk does **not** sell or bill for:

- the customer's **agent** or its **agent model provider** usage (LLM tokens,
  tool calls) — always customer-owned, whatever the payment mode;
- the **surface** or its credentials (Discord bot token, browser, terminal);
- the runtime software — the package and BYOK mode remain usable without a
  Sleeptalk account or a reachable control plane.

### 2. What the customer owns

| Asset | Owner | Paid for by | Where the credential lives |
|---|---|---|---|
| Sleeptalk **account** (credit balance, API keys, installations) | Customer | n/a | Sleeptalk control plane |
| **Installation** credential (per bot host) | Customer's account | n/a | Host OS credential store (protected-file fallback), revocable, never a provider credential |
| Discord bot token / surface credentials | Customer | n/a | Customer's host, exactly as today |
| Agent + agent-model credentials (Claude Code, pi, OpenRouter, …) | Customer | Customer | Customer's host; never proxied, billed, or logged by Sleeptalk |
| Speech provider keys in **BYOK** mode | Customer | Customer (directly to providers) | Customer's host `.env`/environment |
| Speech provider master keys in **hosted** mode | **Sleeptalk** | Sleeptalk | Sleeptalk secret manager, only in broker/proxy workloads (ADR 0002) |
| Temporary brokered provider tokens (hosted mode) | Minted by Sleeptalk for one session | Debited from customer credits | Bot host, memory only, short-lived, never persisted |

One invariant spans the table: **a permanent Sleeptalk provider credential
never appears on a customer-controlled host** (ADR 0002 acceptance), and a
**customer agent/surface credential never appears in Sleeptalk's control
plane**. Neither direction of leakage is acceptable.

### 3. Payment-mode selection and display

- Payment mode is an **installation-level** choice authenticated at startup
  (`sleeptalk login` ⇒ credits; no login ⇒ BYOK), resolved per provider
  operation by the access resolver (ADR 0002 §Runtime changes). A profile is
  payment-agnostic (vocabulary: profiles never assert hosted identity).
- Mode is chosen **before** any provider attempt: the resolver resolves
  static-hosted or BYOK access immediately before each connection/request.
  There is no code path where a provider attempt runs with unresolved
  payment mode.
- Every active session **displays its payment mode and, per provider
  operation, its access mode** (Roadmap 2.0 §1.3). Operator-facing logs and
  the session's normalized events carry the mode so billing surprises are
  observable.

### 4. Prohibiting accidental bypass and accidental charging

**Hosted-use bypass** (using Sleeptalk-funded access outside the product)
and **accidental BYOK charging** (billing a customer for traffic they paid
providers for) are both prohibited by construction:

1. **Single credential choke point.** Provider access is only obtainable via
   the access resolver (ADR 0002). Adapters must not read raw keys from
   configuration when hosted mode is active; the resolver is the only issuer
   of endpoints, headers, and tokens.
2. **Mode-keyed attempts.** Every usage attempt is stamped
   `payment_mode ∈ {hosted, byok}` and, for hosted, carries the control
   plane's opaque `attemptId`. The ledger debits **only** `payment_mode =
   hosted` attempts whose access was minted by the broker/proxy. BYOK
   attempts are diagnostic-only and can never reach the financial ledger.
3. **No cross-mode fallback.** No automatic hosted→BYOK or BYOK→hosted
   fallback occurs without an explicit user configuration for that exact
   provider (ADR 0002 failure table). A hosted session never silently
   spends a customer's own keys; a BYOK session never silently spends
   credits.
4. **Grant binding.** Hosted grants bind account, installation, session,
   operation, and concurrency lease (ADR 0002). A grant minted for one
   session cannot serve another; a second session on the same installation
   must mint its own grants, which the concurrency limit bounds.
5. **Entitlement gate at resolve time.** The resolver refuses to mint hosted
   access when entitlement says BYOK-only (or vice versa: refuses BYOK when
   policy forbids it), making the failure a startup-time configuration error,
   not a mid-call billing event.

### 5. Billing responsibility

| Cost | Hosted mode | BYOK mode |
|---|---|---|
| STT/TTS provider usage | Sleeptalk pays provider; customer pays Sleeptalk per catalog-priced operation from prepaid credit | Customer pays provider directly; Sleeptalk sees nothing |
| Agent/model usage | Customer (always) | Customer |
| Surface costs (Discord, hosting) | Customer | Customer |
| Failed/retried hosted attempts | Ledger distinguishes warmups, retries, fallbacks, cancellations, provider errors; only authoritative-backed attempts debit (ADR 0002 metering) | n/a |

Prepaid credits cap Sleeptalk's fraud exposure while attribution is being
proven (ADR 0002); refunds and credit treatment are separate decisions
(below).

## Verification: credential-flow threat walk

Walking every launch credential flow against §2 and §4:

- **Discord (cascade, hosted).** Bot token: customer host only ✓. Agent
  (Claude Code et al.): customer host, never proxied ✓. Deepgram/Cartesia:
  resolver mints short-lived tokens bound to the session grant; master keys
  stay in the secret manager ✓. ElevenLabs: text crosses the narrow proxy,
  stated in hosted terms (ADR 0002 privacy clause); BYOK users avoid it ✓.
  Installation credential in OS keychain; theft burns that account's
  allowance only, revocable without rotating provider keys ✓.
- **Web/Rendezvous (browser).** Browser, Rendezvous worker, and TURN never
  receive any speech-provider credential (ADR 0002 acceptance); the agent
  host resolves access exactly as Discord does ✓. Share tickets are surface
  objects, not payment identities ✓.
- **Terminal (future surface).** Same resolver path; no surface-specific
  credential code (ADR 0002 surfaces table); local mic/speaker implies no
  surface credential at all beyond the installation credential ✓.
- **Cross-cutting.** `agent.stream` attempts are metered for diagnostics but
  never priced or billed — the ledger's catalog contains only speech
  operations ✓. BYOK attempts emitted with `payment_mode=byok` cannot debit ✓.
  Mode display means an operator can always see who is paying for the live
  session ✓.

## Handoff: unresolved decisions (tracked as board decision cards)

This ADR deliberately does not decide, and defers to:

- `r2-decision-retail-price` — retail pricing policy and margins.
- `r2-decision-credit-terms` — legal treatment of prepaid credit
  (expiry, escheatment, accounting).
- `r2-decision-refunds` — refund policy and process.
- `r2-decision-min-topup` — minimum top-up.
- `r2-decision-region` — hosted region(s) and data-residency consequences.
- `r2-decision-retention` — data retention defaults for hosted traffic.
- `r2-decision-support` — alpha support commitment.
- `r2-provider-commercial` — per-provider commercial matrix (resale rights,
  attribution, revocation SLAs, overspend bounds).

Until those land, hosted alpha operates under prepaid credits with bounded
exposure, as ADR 0002 already requires.

## Non-goals

- No final legal credit treatment, refund policy, or provider-specific
  commercial terms (decision cards above).
- No change to ADR 0002's topology, gates, or MVP boundary.
- No pricing of the runtime itself or of agent/model usage.
