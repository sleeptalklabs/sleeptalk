import { chromium } from 'playwright';
import { spawn } from 'node:child_process';
import { createInterface } from 'node:readline';
import { readFile } from 'node:fs/promises';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';

const modelAuth = process.env.SLEEPTALK_BROWSER_MODEL_AUTH === '1';
const python = process.env.SLEEPTALK_TEST_PYTHON;
if (!python) throw new Error('Set SLEEPTALK_TEST_PYTHON to the Python executable with the remote-entry dependencies.');
const peer = spawn(python, [fileURLToPath(new URL(process.env.SLEEPTALK_BROWSER_PEER ?? './fixture_host.py', import.meta.url))], { stdio: ['pipe', 'pipe', 'pipe'], env: process.env });
const lines = createInterface({ input: peer.stdout });
const backlog = [], waiting = [];
lines.on('line', line => { const result = JSON.parse(line); const next = waiting.shift(); if (next) next(result); else backlog.push(result); });
const receive = () => backlog.length ? Promise.resolve(backlog.shift()) : new Promise(resolve => waiting.push(resolve));
const send = async value => { peer.stdin.write(JSON.stringify(value) + '\n'); const result = await receive(); assert.equal(result.fixture_error, undefined, `Synthetic protocol peer rejected ${value.action}: ${JSON.stringify(result)}`); return result; };
const initial = await receive();
const origin = 'https://setup.example.com';
const invitationURL = async value => process.env.SLEEPTALK_BROWSER_PEER === './integrated_host.py'
  ? (await send({ action: 'invitation_url', invitation: value })).url
  : `${origin}/entry#${Buffer.from(JSON.stringify(value)).toString('base64url')}`;
const output = new URL('../../sleeptalk/_remote_entry/', import.meta.url);
const browser = await chromium.launch({ headless: true, executablePath: process.env.CHROME_PATH ?? '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' });
const context = await browser.newContext({ viewport: { width: 390, height: 844 } });
context.setDefaultTimeout(10000);
let dropAcknowledgement = false;
let dropAllAcknowledgements = false;
let tamperStatus = false;
let submitted = 0;
const pageErrors = [];
const seenRequests = [];
try {
  await context.route('**/*', async route => {
    const request = route.request(); const url = new URL(request.url());
    assert.equal(url.origin, origin, 'Page tried to contact another origin');
    if (url.pathname.startsWith('/v1/setup/')) {
      const [, , , request_id, action] = url.pathname.split('/');
      const body = request.postDataJSON(); assert.equal(request.method(), 'POST');
      seenRequests.push({ action, body, at: Date.now() });
      const result = await send({ request_id, action, body, authorization: request.headers().authorization });
      if (action === 'submission') { submitted++; if (dropAcknowledgement || dropAllAcknowledgements) { dropAcknowledgement = false; await route.abort('failed'); return; } }
      if (tamperStatus && action === 'status') result.signature = 'A'.repeat(86);
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(result), headers: { 'Cache-Control': 'no-store' } }); return;
    }
    const name = url.pathname === '/entry' || url.pathname === '/entry/' ? 'index.html' : url.pathname.slice('/entry/'.length);
    assert.ok(['index.html', 'app.js', 'style.css'].includes(name));
    await route.fulfill({ status: 200, contentType: name.endsWith('.js') ? 'text/javascript' : name.endsWith('.css') ? 'text/css' : 'text/html', body: await readFile(new URL(name, output)), headers: { 'Cache-Control': 'no-store', 'Content-Security-Policy': "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'" } });
  });
  const page = await context.newPage(); page.on('pageerror', error => pageErrors.push(error.message));
  const cdp = await context.newCDPSession(page); await cdp.send('WebAuthn.enable');
  await cdp.send('WebAuthn.addVirtualAuthenticator', { options: { protocol: 'ctap2', transport: 'internal', hasResidentKey: true, hasUserVerification: true, isUserVerified: true, automaticPresenceSimulation: true } });
  await page.goto(await invitationURL(initial));
  await page.getByRole('button', { name: 'Create passkey' }).waitFor();
  assert.equal(await page.evaluate(() => location.hash), '');
  assert.equal(await page.evaluate(() => localStorage.length + sessionStorage.length), 0);
  await page.getByRole('button', { name: 'Create passkey' }).click();
  await page.getByText('Compare this code with the local setup page.', { exact: false }).waitFor();
  assert.equal(await page.evaluate(() => localStorage.length), 0, 'Unconfirmed passkey must never establish binding');
  assert.equal(await page.getByRole('button', { name: 'Create passkey' }).isDisabled(), true);
  assert.match(await page.locator('#fingerprint').textContent(), /^[a-f0-9]{4}( [a-f0-9]{4}){7}$/);
  await page.screenshot({ path: '/tmp/sleeptalk-remote-pairing-browser.png', fullPage: true });
  await send({ action: 'confirm_local' });
  await page.getByText('Device paired.', { exact: false }).waitFor();
  const storage = await page.evaluate(() => Object.fromEntries(Object.entries(localStorage)));
  assert.equal(Object.keys(storage).length, 1);
  assert.deepEqual(Object.keys(JSON.parse(Object.values(storage)[0])).sort(), ['credential_id', 'host_id', 'operator_id', 'public_key']);
  assert.ok(!JSON.stringify(storage).includes(initial.capability));
  const setup = await send({ action: 'new_setup' });
  await page.goto(await invitationURL(setup));
  const values = modelAuth ? [
    ['Model provider credential', 'synthetic-model-private-value'],
    ['Deepgram API key', 'synthetic-provider-value'],
    ['Cartesia API key', 'synthetic-tts-value'],
  ] : [['Deepgram API key', 'synthetic-provider-value']];
  if (modelAuth) {
    for (const value of ['custom', 'fixture-model', 'API key', 'openai-completions', 'https://provider.example/v1']) {
      await page.locator('.model-auth-scope').getByText(value, { exact: true }).waitFor();
    }
  }
  for (let index = 0; index < values.length; index++) {
    const [label, value] = values[index];
    const input = page.getByLabel(label);
    await input.fill(value);
    const row = input.locator('..');
    dropAcknowledgement = index === 0;
    await row.getByRole('button', { name: 'Validate and save' }).click();
    if (index === values.length - 1) await page.getByText('Setup complete.', { exact: false }).waitFor();
    else await row.getByText('Saved on host', { exact: true }).waitFor();
  }
  assert.equal(submitted, values.length, 'Lost acknowledgement must recover by signed status without another write');
  assert.equal(await page.locator('input').count(), 0);
  assert.equal(await page.evaluate(() => sessionStorage.length), 0);
  assert.ok(!JSON.stringify(await page.evaluate(() => Object.fromEntries(Object.entries(localStorage)))).includes('synthetic-provider-value'));
  for (const [, value] of values) {
    assert.ok(!JSON.stringify(seenRequests).includes(value), 'Relay saw plaintext provider credential');
    assert.ok(!JSON.stringify(await page.evaluate(() => Object.fromEntries(Object.entries(localStorage)))).includes(value), 'Browser persisted private credential');
  }
  if (process.env.SLEEPTALK_BROWSER_PEER === './integrated_host.py') {
    assert.deepEqual(await send({ action: 'assert_once' }), modelAuth ? { validations: 3, commits: 3, generation: 1, profiles: 1, model_reference_scoped: true, profile_auth_reference: true, replay_same: true } : { validations: 1, commits: 1, generation: 1, profiles: 1, replay_same: true });
  }
  const cancelled = await send({ action: 'new_setup' });
  await page.goto(await invitationURL(cancelled));
  await page.getByRole('button', { name: 'Cancel setup on host' }).click();
  await page.getByText('Setup cancelled.', { exact: true }).waitFor();
  if (process.env.SLEEPTALK_BROWSER_PEER === './integrated_host.py' && !modelAuth) {
    const retrySetup = await send({ action: 'new_setup', fail_first: true });
    await page.goto(await invitationURL(retrySetup));
    await page.getByLabel('Deepgram API key').fill('synthetic-provider-value');
    await page.getByRole('button', { name: 'Validate and save' }).click();
    await page.getByText('The host could not validate this credential.', { exact: false }).waitFor();
    await page.getByLabel('Deepgram API key').fill('synthetic-provider-value');
    await page.getByRole('button', { name: 'Validate and save' }).click();
    await page.getByText('Setup complete.', { exact: false }).waitFor();
    assert.deepEqual(await send({ action: 'assert_failure_recovery' }), { validations: 3, commits: 2, profiles: 2 });
  }
  if (!process.env.SLEEPTALK_BROWSER_PEER) {
    const modelSetup = await send({ action: 'new_setup', mode: 'model_auth' });
    await page.goto(await invitationURL(modelSetup));
    await page.getByLabel('Model provider credential').waitFor();
    for (const value of ['custom-model-provider', '<img src=x onerror=alert(1)>', 'API key', 'openai-completions', 'https://provider.example/proxy/v1']) {
      await page.locator('.model-auth-scope').getByText(value, { exact: true }).waitFor();
    }
    assert.equal(await page.locator('img').count(), 0, 'Signed model selection must remain literal text');
    assert.equal(await page.getByLabel('Model provider credential').inputValue(), '');
    assert.equal(await page.getByLabel('Model provider credential').getAttribute('maxlength'), '8192');
    assert.equal(await page.getByLabel('Model provider credential').evaluate(input => Boolean(input.previousElementSibling.querySelector('.model-auth-scope'))), true, 'Destination scope must precede private input');
    const repairScope = await send({ action: 'new_setup', mode: 'repair_scope' });
    await page.goto(await invitationURL(repairScope));
    await page.getByText('Replace existing credential · Expected generation 1', { exact: true }).waitFor();
    await page.getByText('voice-profile@0123456789ab', { exact: true }).waitFor();
    await page.getByText('<img src=x onerror=alert(1)>', { exact: true }).waitFor();
    assert.equal(await page.locator('img').count(), 0, 'Signed profile label must remain literal text');
    assert.equal(await page.getByLabel('Deepgram API key').inputValue(), '');
    const repair = await send({ action: 'new_setup', mode: 'repair' });
    await page.goto(await invitationURL(repair));
    await page.getByText('Credential update complete.', { exact: false }).waitFor();
    assert.equal(await page.locator('input').count(), 0);
    const failedProfile = await send({ action: 'new_setup', mode: 'profile_error' });
    await page.goto(await invitationURL(failedProfile));
    await page.getByText('Credentials are saved, but the host could not validate the profile.', { exact: false }).waitFor();
    assert.equal(await page.locator('input').count(), 0);
    const retries = await send({ action: 'new_setup', mode: 'retries' });
    await page.goto(await invitationURL(retries));
    await page.getByLabel('Deepgram API key').fill('synthetic-provider-value');
    const requestStart = seenRequests.length;
    dropAllAcknowledgements = true;
    await page.getByRole('button', { name: 'Validate and save' }).click();
    for (let retry = 0; retry < 2; retry++) {
      await page.waitForFunction(() => !document.querySelector('#retry').disabled);
      await page.getByRole('button', { name: 'Retry same submission' }).click();
      await page.waitForFunction(() => !document.querySelector('#retry').disabled || document.querySelector('#status').textContent.includes('transmission limit'));
    }
    await page.getByRole('button', { name: 'Check progress' }).click();
    await page.getByText('The transmission limit is reached.', { exact: false }).waitFor();
    assert.equal(await page.getByRole('button', { name: 'Retry same submission' }).isDisabled(), true);
    const attempts = seenRequests.slice(requestStart).filter(request => request.action === 'submission');
    assert.equal(attempts.length, 3, 'Only initial delivery and two explicit retransmissions are allowed');
    assert.deepEqual(attempts[1].body, attempts[0].body);
    assert.deepEqual(attempts[2].body, attempts[0].body);
    for (let index = 1; index < attempts.length; index++) {
      assert.ok(attempts[index].at - attempts[index - 1].at >= (index === 1 ? 1900 : 3900), 'Explicit retransmission skipped backoff');
      assert.ok(seenRequests.slice(seenRequests.indexOf(attempts[index - 1]) + 1, seenRequests.indexOf(attempts[index])).some(request => request.action === 'status'), 'Retransmission skipped status');
    }
    dropAllAcknowledgements = false;
    await page.locator('#close').click();
  }
  const rejectionInvitation = !process.env.SLEEPTALK_BROWSER_PEER ? await send({ action: 'new_setup' }) : cancelled;
  const stale = structuredClone(rejectionInvitation); stale.descriptor.operator_id = Buffer.alloc(16, 9).toString('base64url');
  await page.goto(await invitationURL(stale));
  await page.getByText('This invitation could not be verified', { exact: false }).waitFor();
  assert.equal(await page.locator('input').count(), 0);
  tamperStatus = true;
  await page.goto(await invitationURL(rejectionInvitation));
  await page.getByText('This invitation could not be verified', { exact: false }).waitFor();
  assert.equal(await page.locator('input').count(), 0);
  await page.addInitScript(() => { window.PublicKeyCredential = undefined; });
  await page.goto(await invitationURL(rejectionInvitation));
  await page.getByText('This invitation could not be verified', { exact: false }).waitFor();
  assert.equal(await page.locator('input').count(), 0, 'Unsupported browser must not display private fields');
  if (!process.env.SLEEPTALK_BROWSER_PEER) {
    tamperStatus = false;
    const idle = await send({ action: 'new_setup' });
    const idlePage = await context.newPage();
    idlePage.on('pageerror', error => pageErrors.push(error.message));
    await idlePage.clock.install();
    await idlePage.goto(await invitationURL(idle));
    await idlePage.getByText('Enter one provider credential at a time.', { exact: false }).waitFor();
    const idleInput = await idlePage.getByLabel('Deepgram API key').elementHandle();
    await idleInput.fill('synthetic-unsent-value');
    const beforeExpiry = seenRequests.length;
    await idlePage.clock.fastForward(Math.max(1, idle.descriptor.expires_at * 1000 - Date.now() + 1000));
    await idlePage.getByText('This invitation expired. Private input was cleared.', { exact: false }).waitFor();
    assert.equal(await idleInput.evaluate(input => input.value), '');
    assert.equal(await idlePage.locator('input').count(), 0);
    assert.equal(await idlePage.evaluate(() => location.hash), '');
    assert.equal(await idlePage.evaluate(() => sessionStorage.length), 0);
    assert.equal(seenRequests.length, beforeExpiry, 'Idle expiry should clear locally without sending private input');
    await idlePage.close();
  }
  if (!process.env.SLEEPTALK_BROWSER_PEER) console.log('PASS: literal model credential destination and replacement scope, explicit retry cap and backoff, repair completion, profile failure guidance and idle expiry');
  assert.deepEqual(pageErrors, []);
  if (process.env.SLEEPTALK_BROWSER_PEER === './integrated_host.py' && !modelAuth) console.log('PASS: actual WebSocket relay, RemoteHost, RemoteStore and CredentialStore; one durable commit across lost acknowledgement and exact replay; failed validation permits new entry');
  if (modelAuth) console.log('PASS: actual relay/host/stores encrypt paired-browser model credential, retain exact provider scope, write production reference-only profile and commit three credentials once across lost acknowledgement/replay');
  console.log('PASS: bundled browser virtual-passkey pairing, local confirmation, Python HPKE/WebAuthn validation, lost-ack recovery, cancellation, signed-state rejection and storage isolation');
} catch (error) { console.error('Browser proof status:', await context.pages()[0].locator('#status').textContent()); console.error('Observed actions:', seenRequests.map(r => r.action).join(',')); throw error; } finally { await browser.close(); peer.kill(); lines.close(); }
