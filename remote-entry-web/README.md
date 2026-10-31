# Remote private-entry browser

This package builds the static pairing and provider-entry page. It uses the
host's signed protocol, passkeys and HPKE. The relay never receives plaintext
provider credentials. Browser delivery itself remains trusted code.

Build from the pinned lockfile:

```sh
npm ci --ignore-scripts
npm test
npm run build
```

The build writes `sleeptalk/_remote_entry/` with bundled JavaScript,
HTML, CSS, license notices and an `assets.json` SHA-256 manifest. All runtime
libraries are bundled locally. There are no CDN requests or service workers.
Package this whole directory in the Python wheel.

The deployed service must serve the page at `/entry`, its assets at
`/entry/<filename>`, and the relay API under `/v1/setup/`. The configured setup
and relay origins must be the same HTTPS origin. Serve responses with
`Cache-Control: no-store`, `Referrer-Policy: no-referrer`, exact-origin checks,
and the HTML's CSP plus the response-only `frame-ancestors 'none'` directive.
The build does not deploy a service or establish its HTTPS identity.

Invitations carry base64url canonical JSON in the URL fragment:
`{kind, descriptor, signature, capability}`. `kind` is `pairing` or `setup`.
The page removes the fragment before using the relay. Only the trusted local
pairing page may deliver a first enrollment invitation. The remote page
cannot establish that trusted delivery channel on its own.

Successful pairing requires a real registration, a separate possession
assertion, and the trusted local comparison and confirmation. Only afterward
does this browser store `{host_id, public_key, operator_id, credential_id}`
under `sleeptalk.host.<host_id>` in localStorage. Setup invitations require that
existing public binding. Capabilities, provider values, assertions and pending
ciphertext stay in memory. Closing or reloading loses unsent input; signed
host status recovers saved provider progress. JavaScript cannot promise
physical erasure of strings from memory.

The provider labels are Deepgram API key, Cartesia API key, ElevenLabs API key,
Discord bot token, and Model provider credential. Model entry additionally
displays the signed model, provider, protocol, authentication mode and full
HTTPS endpoint. Workspace, adapter, provider choice, and Discord
channel/operator scope come from the host's signed descriptor and local
configuration, not fields in this page. `affected_profile_ids` maps provider
fields to bounded existing CredentialStore labels such as
`profile-name@0123456789ab`. These labels are not filesystem destinations.
Each field shows its signed
create/replace action, expected generation, and affected profile labels before
private entry and passkey approval.

After an uncertain submission response, the page checks signed host status.
A user can explicitly retransmit the same submission twice, with its original
ID and ciphertext. Each retransmission checks signed status first and waits
at least 2 seconds, then 4 seconds, after the preceding transmission. Status
polling backs off from 2 to 10 seconds while progress is unchanged and resets
on progress. The invitation deadline clears private state and inputs even
when the page is idle. Cancellation requires a fresh host challenge and
passkey assertion; a fresh signed status must confirm cancellation. Closing the page only clears
its local state and makes no cancellation claim.

## Browser proof

Use an isolated Python environment with the package's `remote-entry` extra and
an installed Chrome executable. For example, from this directory:

```sh
PYTHONPATH=../sleeptalk \
SLEEPTALK_TEST_PYTHON=/absolute/path/to/python \
CHROME_PATH=/absolute/path/to/chrome \
npm run test:browser
```

The test drives the generated bundle with Chromium's virtual authenticator.
Playwright intercepts a synthetic HTTPS origin and sends protocol messages to
an in-memory Python fixture peer. There is no provider account, real passkey,
service deployment, or external network request. The Python peer runs the
actual HPKE and FIDO2 verifiers. The proof covers local-confirmation ordering,
comparison fingerprints, encrypted provider submission, lost-ack recovery,
signed cancellation, tampered state and browser storage. It also writes mobile
screenshots under `/tmp` for layout inspection.

This proof does not cover real mobile authenticators, provider dashboards,
production TLS, service quotas or an independent security review. Those remain
release acceptance work.

Run `npm run test:integration` with the same Python and Chrome environment to
exercise the actual `RemoteHost`, `RemoteStore`, `CredentialStore` and
`SetupRelay`. Only provider validation and profile preparation are synthetic.
The test starts a local aiohttp relay, connects the actual host WebSocket
client, builds invitations through `remote_local.remote_invitation_url`, drives
the same generated browser, and confirms pairing through the actual local-store
method. It proves that a lost acknowledgement and exact
replay produce one durable credential operation at generation 1, and that a
failed provider validation permits corrected input with a new submission ID.
All database files and workspace/profile artifacts use a temporary directory.


Run `npm run test:integration-model-auth` with the same environment to exercise
paired model entry plus both speech credentials. It uses the real profile
preparer, signed model scope and endpoint-scoped credential references. Provider
responses and the final runtime probe are synthetic. Separate pinned Hermes
tests verify that probe against the actual upstream agent. This proof confirms
three writes at generation 1, including exact replay of the model submission,
and a profile containing references with no credential values.

`npm run test:auth-entry` covers local model entry in a fresh browser profile,
including rejection, corrected input and value-free status recovery.
