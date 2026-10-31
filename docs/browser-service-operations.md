# Browser-link service operations

The hosted browser-link service has independent staging and production Worker
configurations. Staging is `call-staging.sleep.talk`; packages use the launched
production service at `call.sleep.talk` by default.

The Cloudflare Workers are `sleeptalk-call-staging` and
`sleeptalk-call-production`. Their names are not the public URL; keep using
`call.sleep.talk` in user interfaces and documentation. Renaming a Worker
creates a different Worker with empty Durable Object namespaces, so every
host would have to enroll again (`sleeptalk host reset-access`).

## Configure

Create separate Cloudflare Realtime TURN keys for staging and production. For
each environment, set these Worker secrets through the matching Wrangler
configuration:

- `TURN_KEY_ID`
- `TURN_KEY_API_TOKEN`
- `ENROLLMENT_HASH_KEY`, a random deployment-specific HMAC key
- `ADMIN_TOKEN`, a random deployment-specific operator credential

Staging uses `rendezvous/wrangler.jsonc`. Production uses
`rendezvous/wrangler.production.jsonc`. Do not reuse TURN, HMAC, or admin
secrets between them. Do not put the values in Git, shell history, a profile,
or a call receipt.

A source network is an IPv4 /24 or an IPv6 /48. Production admits at most 5
new installations per source network and 100 globally per UTC day. Each
installation may mint 12 links per minute, 100 per day, and hold three
concurrent calls. Links live 1 to 5 minutes; shorter requests are raised to
one minute.

Minting charges no relay allowance and issues no TURN credential. When a
browser redeems a call link, the call is charged 4 relay units (two TURN
credential attempts per peer) and only then are TURN credentials issued: the
browser's in the redemption response, the host's in the `ticket.redeemed`
message on its authenticated signaling socket. The credentials last the
30-minute session cap plus one minute. Relay units are limited per UTC day:

| Budget | Variable | Production | Staging |
| --- | --- | --- | --- |
| One installation | `RELAY_ATTEMPTS_PER_INSTALLATION_PER_DAY` | 160 (40 calls) | 40 |
| One source network (all its installations) | `RELAY_ATTEMPTS_PER_SOURCE_PER_DAY` | 320 (80 calls) | 80 |
| Everyone, an issuance limit | `RELAY_ATTEMPTS_PER_DAY` | 800 (200 calls) | 400 |

Installations enrolled before sources were recorded count as their own
source. Production requires complete TURN configuration and starts halted on
a new durable state.

Workers rate limits reject requests before expensive Durable Object work:

| Request type | Production allowance per minute |
| --- | --- |
| Public API, per source network | 120 |
| Public API, all sources at one Cloudflare location | 1,000 |
| Signaling upgrades, per source network | 30 |
| Admin requests, per source network | 30, independently of public API limits |
| Enrollments, per source network | 5 |
| Mints / redemptions, each per source network | 30 |

These [rate limits are approximate and local to each Cloudflare location](https://developers.cloudflare.com/workers/runtime-apis/bindings/rate-limit/),
including the binding named `GLOBAL_REQUEST_RATE_LIMIT`. They do not create a
worldwide request or dollar cap. Production fails closed if any limiter binding
is missing. The durable daily enrollment and relay counters are atomic across
locations. Reaching the global daily relay allowance also denies legitimate
new calls until the next UTC day; review actual demand before raising it.

Static pages, scripts and styles bypass the Worker. Their security headers live
in `rendezvous/web/public/_headers`, which Vite copies to `sleeptalk/_web/`.
Only `/api/*` must run the Worker first. [Static asset requests are free](https://developers.cloudflare.com/workers/static-assets/billing-and-limitations/);
API requests still invoke the Worker, even when the Worker returns 429. The
separate dashboard firewall rule below addresses that remaining request cost.

The `ServiceControl` object keeps each day's counters and idempotency records
as separate small keys and drops days older than yesterday on a daily alarm.
The first alarm after this layout ships also compacts the old single-value
state.

A signaling socket must authenticate within 10 seconds, and a room accepts
at most four unauthenticated sockets at a time. Every socket is limited to
240 messages and 1 MiB per minute, with 64 KiB per message measured as UTF-8
bytes. Unknown messages count too, and counters survive hibernation. Exceeding
an authenticated socket's allowance ends the call and requests TURN credential
revocation; an unauthenticated socket error cannot end someone else's call.
Request bodies are capped by
the bytes received, not the declared length. Pages carry
`Strict-Transport-Security` and a CSP whose `connect-src` is the page's own
origin.

Setup links (tickets of kind `setup`, used for phone key entry) have separate,
fixed limits: two open, six per hour and 20 per UTC day per installation. They
use no TURN allowance and no call slots. A setup room relays only setup
messages, which carry encrypted keys the service can't read, and its summary
log line has `kind: "setup"`. Halt and revocation stop setup links the same
way as calls.

Cloudflare Budget Alerts send notifications; they do not stop TURN usage.
The selected launch warning is **$10 of account-wide usage-based spend per
billing period**. It is not yet configured; the operator chose to complete
dashboard settings later. Use the relay budgets above and service halt as
issuance controls. A caller can repurpose an issued TURN credential for heavy
traffic, so these call counts cannot guarantee a dollar ceiling. Already-issued
credentials may remain usable for up to 31 minutes; a provider-side TURN key
shutdown may be needed during an incident.

## Verify and deploy staging

From `rendezvous/`:

```bash
npm test
npm run check
npm run build
npm run deploy:staging
```

After deployment, prove:

1. an empty home enrolls one installation and mints a link;
2. the same home reuses that installation;
3. invalid and revoked grants fail;
4. the concurrency and relay-attempt limits return their stable errors;
5. halt blocks enrollment, minting, and new browser TURN issuance;
6. an embedded browser receives relay-only TURN credentials;
7. an ordinary browser remains direct-first with TURN available;
8. a setup link mints with `kind: "setup"`, opens its setup page, and is
   refused when reopened.

The staging proof script exercises those controls without printing ticket or
installation credentials:

```bash
cd sleeptalk
RENDEZVOUS_ADMIN_TOKEN=... uv run python ../scripts/prove_hosted_staging.py
```

The default media profile attempts forced TURN/UDP. Use
`--forced-relay-transport tls` for the explicit synthetic TURN/TLS-443 lane.
Both profiles use a synthetic aiortc peer in the browser signaling role. They
do not replace real-browser, microphone, playback, or external-network tests.
A failed media lane exits nonzero with a fixed code and still attempts
reservation release, service resume, and installation revocation.

On the staging operator Mac, keep the admin token in macOS Keychain under
service `sleeptalk-call-staging-admin` and account `sleeptalk` (production:
`sleeptalk-call-production-admin`). The operator helper below reads it directly
without putting it in command-line arguments. On other systems, provide
`RENDEZVOUS_ADMIN_TOKEN` through a private
process environment. Do not print it or place it in a shell profile.

Record the Git commit, Worker version, policy revision, test artifact, and
results. Never record credentials, raw addresses, ticket fragments, SDP, or
ICE candidates.

## Halt and inspect

From the repository root on the operator Mac (curl and the `credentials` extra required):

```bash
uv run --extra credentials python scripts/hosted_service_control.py status
uv run --extra credentials python scripts/hosted_service_control.py halt --reason "Investigating unexpected usage"
uv run --extra credentials python scripts/hosted_service_control.py resume --reason "Incident resolved"
```

Add `--staging` to use the separate staging credential and host. The helper reads
current state before changing it, uses that revision, and never automatically
retries a failed write. It refuses redirects and prints only service state and
allowance counts. A halt prevents new enrollment, links and TURN issuance;
resuming is an explicit operator action. Admin recovery remains available when
public request limits are exhausted or `EMERGENCY_DISABLE` is set (resume still
requires clearing that environment variable).

Administrative requests use `Authorization: Bearer
$RENDEZVOUS_ADMIN_TOKEN`. Read `/api/admin/status` before changing control
state. A halt or resume posts to `/api/admin/control` with `action`,
`expectedRevision`, and a bounded reason. A stale revision is rejected.

Inspect one installation with `GET
/api/admin/installations/<installation-id>`. Revoke it with `POST
/api/admin/installations/<installation-id>/revoke` and a JSON `reason`. The
grant is tombstoned before its known rooms are closed. Revocation cannot be
undone by automatic re-enrollment.

For a broad incident, set the Worker environment variable
`EMERGENCY_DISABLE=true` and redeploy. Durable HTTP control cannot override
that value. Halt does not claim
to terminate an existing direct media path or an already allocated TURN
session; disable or rotate the environment's TURN key when the incident
requires provider-side termination.

## Dashboard follow-up (pending)

These settings were deferred by the operator. The existing CLI authorization
cannot manage billing alerts or zone firewall rules.

1. Select the account, then **Manage Account > Billing > Billable Usage >
   Create budget alert**. Name it `Sleeptalk launch usage`, set **$10**, add
   your email, and save. This covers combined usage-based account spend, not a
   single Worker, and does not disable anything when reached. See
   [Cloudflare budget alerts](https://developers.cloudflare.com/billing/manage/budget-alerts/).
2. Select the `sleep.talk` zone, then **Security rules > Create rule > Rate
   limiting rules**. Start with 60 requests per 10 seconds per IP, action
   **Block**, using the shortest blocking duration the plan allows. Scope it
   to hostname `call.sleep.talk` and paths starting with `/api/`; exclude
   `/api/admin/` so an API flood does not consume the operator's counter.
   Keep the existing separate admin limiter. Check available fields and
   periods in the dashboard before deploying; they vary by plan. Use Block
   rather than a browser challenge because the host CLI cannot solve a web
   challenge. See [rule setup](https://developers.cloudflare.com/waf/rate-limiting-rules/create-zone-dashboard/).
3. Try enrollment and one browser call after saving the rule; check Security
   Events for legitimate clients being blocked. Shared networks can combine
   multiple users behind one IP. Adjust from observed traffic.
4. When a spend alert arrives, inspect service status and Cloudflare usage.
   Halt new calls if usage is unexpected. For continuing TURN abuse, disable
   the affected production TURN key in Cloudflare; the service halt alone
   cannot immediately terminate issued credentials or direct peer media.

Suggested expression where the plan supports these fields:

```text
(http.host eq "call.sleep.talk" and starts_with(http.request.uri.path, "/api/")
 and not starts_with(http.request.uri.path, "/api/admin/"))
```

## Rotate credentials

Rotate `ADMIN_TOKEN` and `ENROLLMENT_HASH_KEY` independently. Changing the
enrollment HMAC key starts new anonymous-enrollment buckets; account for that
when responding to abuse. Rotating a TURN key requires setting both new TURN
secrets before deleting the old key, then exercising direct and forced-relay
staging calls.

Installation grants are nonexpiring in the launch design. A local loss or an
operator revocation requires explicit reset to a new anonymous installation
under the normal enrollment limits. There is no shared grant to rotate.

```bash
sleeptalk host reset-access --rendezvous https://call.sleep.talk
```

## Roll back

Halt first. Select only a Worker version that understands installation grants,
the `Installation` and `ServiceControl` Durable Object bindings, and ticket
record version 2. Never roll public traffic back to the optional-token Worker.

```bash
npx wrangler deployments status --config wrangler.production.jsonc
npx wrangler rollback <compatible-version-id> --config wrangler.production.jsonc --message "Plan 06 rollback"
```

Cloudflare does not roll bindings or Durable Object schema back with Worker
code. If the compatible version cannot read current state, keep the service
halted and fix forward. Verify one candidate client on staging before resuming
the expected durable policy revision.

## Protection rollout — 2026-10-31

The protection patch was built from committed source
`773adb7e9a5c112f53aa31663635650901702145` in an isolated snapshot. Unrelated
local work in progress was excluded. The patch and operator helper were
uncommitted at deployment time and are included in the release preparation.

- Production version: `deaf7e93-ce5f-4400-b7d3-d177eb29dbe1`, 100% of traffic.
- Staging version: `d0ca2470-5cd0-44bb-9bf1-0cab56ab5066`.
- Previous production version: `8c9d6532-ffd6-4822-bb74-237d8e837436`.
- Automated validation: TypeScript checks, 161 unit tests, 35 Worker tests,
  8 operator-helper tests, frontend build, and production deployment dry run.
- Live staging: security headers, unauthenticated admin rejection, invalid
  signaling rejection, and signaling flood closure (4008) passed. A brief halt
  rejected enrollment with `503 service_disabled`; staging was restored to
  running at policy revision 7.
- Live production: operator status showed running, policy revision 2, and an
  800-unit daily relay limit. Unauthenticated admin requests returned 401,
  non-WebSocket signaling returned 426, and GET redemption returned 405.
  Both pages retained their security headers and correct WebSocket CSP;
  all 11 HTML/JS/CSS files matched the deployed snapshot.
- Missing operator credentials were replaced with separate random admin tokens
  for production and staging, saved in this Mac's Keychain. Other service
  secrets were preserved. Private temporary upload files were removed.

The $10 billing alert and dashboard firewall rule remain pending by operator
choice. Real microphone, playback, Discord speech, external-network media,
and local service restart checks were not performed by this rollout.
