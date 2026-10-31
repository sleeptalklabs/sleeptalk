import * as p from './protocol.js';
import { pollSchedule, retryBackoff } from './timing.js';

let fragment = location.hash;
history.replaceState(null, '', location.pathname);
const main = document.querySelector('main');
const status = document.querySelector('#status');
const content = document.querySelector('#content');
const close = document.querySelector('#close');
let invitation;
let stopped = false;
let revision = -1;
let timer;
let expiryTimer;
let polling;
const lifecycle = new AbortController();
let binding;
let pairingIdentity;
let pendingSubmission;
let retryRunning = false;
const inputs = new Map();
const terminal = new Set(['cancelled', 'expired', 'revoked', 'failed']);
const say = message => { status.textContent = message; };
const button = (label, handler) => { const element = document.createElement('button'); element.textContent = label; element.type = 'button'; element.addEventListener('click', () => guarded(handler, element)); return element; };
function clearInputs() { for (const input of inputs.values()) input.value = ''; }
function finish(message) { fragment = ''; stopped = true; clearTimeout(timer); clearTimeout(expiryTimer); lifecycle.abort(); clearInputs(); inputs.clear(); pendingSubmission = undefined; invitation = undefined; content.replaceChildren(); say(message); }
function active() {
  if (!stopped && invitation && Date.now() >= invitation.descriptor.expires_at * 1000) finish('This invitation expired. Private input was cleared. Return to local setup for a new invitation.');
  return !stopped && invitation !== undefined;
}
function retryEnabled() { return !retryRunning && pendingSubmission !== undefined && retryBackoff(pendingSubmission.transmissions) !== null; }
function updateRetry() { const control = document.querySelector('#retry'); if (control) control.disabled = !retryEnabled(); }
close.addEventListener('click', () => finish('This page is closed and its private input is cleared. This does not cancel the host request.'));
window.addEventListener('hashchange', () => { if (location.hash) { finish('Opening the new invitation…'); location.reload(); } });
async function guarded(action, control) {
  if (stopped) return;
  if (control) control.disabled = true;
  try { await action(); } catch { clearInputs(); if (!stopped) say(pendingSubmission?.transmissions === 3 ? 'The transmission limit is reached. Check progress or return to local setup.' : 'The action could not be verified. Check progress before trying again, or reopen the original invitation.'); }
  finally { if (control && !stopped) control.disabled = (control.id === 'pair' && pairingIdentity !== undefined) || (control.id === 'retry' && !retryEnabled()) || control.closest('section')?.querySelector('input')?.disabled === true; }
}
async function boundedText(response) {
  p.check(response.ok && response.body);
  const reader = response.body.getReader(); let body = new Uint8Array();
  while (true) { const { done, value } = await reader.read(); if (done) break; if (body.length + value.length > 32768) { await reader.cancel(); p.fail(); } body = p.concat(body, value); }
  return new TextDecoder('utf-8', { fatal: true }).decode(body);
}
async function post(action, body) {
  p.check(active());
  const d = invitation.descriptor;
  const requestId = invitation.kind === 'pairing' ? d.enrollment_id : d.request_id;
  const response = await fetch(`/v1/setup/${requestId}/${action}`, { method: 'POST', redirect: 'error', credentials: 'omit', cache: 'no-store', referrerPolicy: 'no-referrer', headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${invitation.capability}` }, body: new TextDecoder().decode(p.canon(body)), signal: AbortSignal.any([lifecycle.signal, AbortSignal.timeout(65000)]) });
  const signed = p.parse(await boundedText(response)); p.fields(signed, ['payload', 'signature']);
  await p.verifySignature(signed.payload, signed.signature, d.host_signing_public_key);
  p.check(active()); p.object(signed.payload); return signed.payload;
}
function requestMatches(payload) { const d = invitation.descriptor; p.check(payload.request_id === (invitation.kind === 'pairing' ? d.enrollment_id : d.request_id)); }
function later() { clearTimeout(timer); if (!stopped) timer = setTimeout(() => guarded(refreshStatus), polling.delay); }
function displayFingerprint(value) {
  let display = document.querySelector('#fingerprint');
  if (!display) { display = document.createElement('p'); display.id = 'fingerprint'; display.className = 'fingerprint'; content.prepend(display); }
  display.textContent = value.match(/.{1,4}/g).join(' ');
}
async function refreshStatus() {
  const client_nonce = p.random(16);
  const result = await post('status', { client_nonce }); requestMatches(result); p.check(result.client_nonce === client_nonce);
  const progress = new TextDecoder().decode(p.canon({ pending_submission: pendingSubmission?.submissionId ?? null, status: result.status, revision: result.revision ?? null, fields: result.fields ?? null, profile_ready: result.profile_ready ?? null, profile_error: result.profile_error ?? null, fingerprint: result.fingerprint ?? null }));
  polling = pollSchedule(polling, progress);
  if (invitation.kind === 'pairing') {
    p.check(result.operation === 'pairing_status');
    if (result.status === 'open') { p.check(!pairingIdentity); say('Create a passkey on this device, then compare the code on the local setup page.'); return result; }
    if (result.status === 'confirmed') {
      p.check(pairingIdentity && result.credential_id === pairingIdentity.credentialId && result.fingerprint === pairingIdentity.fingerprint && p.unb64(result.operator_id).length === 16);
      const d = invitation.descriptor;
      localStorage.setItem(p.bindingKey(d.host_id), JSON.stringify({ host_id: d.host_id, public_key: d.host_signing_public_key, operator_id: result.operator_id, credential_id: result.credential_id }));
      finish('Device paired. You can now open this host’s private setup invitations on this browser.'); return result;
    }
    if (result.status === 'awaiting_confirmation') { p.check(pairingIdentity && result.fingerprint === pairingIdentity.fingerprint); displayFingerprint(pairingIdentity.fingerprint); say('Compare this code with the local setup page. Confirm there only if both codes match.'); later(); return result; }
    p.check(terminal.has(result.status)); finish(`Pairing ${result.status}. Return to the local setup page.`); return result;
  }
  p.check(Number.isSafeInteger(result.revision) && result.revision >= revision); revision = result.revision;
  p.object(result.fields);
  for (const [field, input] of inputs) {
    const fieldState = typeof result.fields[field] === 'string' ? result.fields[field] : result.fields[field]?.status;
    const row = input.closest('section');
    if (fieldState === 'saved') { input.value = ''; input.disabled = true; row.querySelector('button').disabled = true; row.querySelector('.field-status').textContent = 'Saved on host'; if (pendingSubmission?.field === field) pendingSubmission = undefined; }
    else {
      if (pendingSubmission?.field === field && p.matchingFailure(result.fields[field], pendingSubmission.submissionId)) pendingSubmission = undefined;
      row.querySelector('.field-status').textContent = p.fieldFailures[fieldState] ?? (fieldState === 'validating' ? 'Host is validating…' : 'Not saved yet');
    }
  }
  if (result.profile_ready === true) { finish('Setup complete. The host saved the profile. A spoken call is still needed to check your audio.'); return result; }
  if (result.profile_error === 'profile_validation_failed') { finish('Credentials are saved, but the host could not validate the profile. Return to local setup to check the profile and selected runtime.'); return result; }
  if (result.status === 'complete') {
    finish(Object.values(invitation.descriptor.field_actions).every(action => action === 'replace') ? 'Credential update complete. The existing profile keeps its saved reference.' : 'Credentials are saved, but the host has not confirmed a ready profile. Return to local setup.'); return result;
  }
  if (terminal.has(result.status)) { finish(`Setup ${result.status}.`); return result; }
  updateRetry();
  say(pendingSubmission ? (retryBackoff(pendingSubmission.transmissions) !== null ? 'The host has not confirmed this submission yet. Check progress or retry the same submission.' : 'The transmission limit is reached. Check progress or return to local setup.') : 'Enter one provider credential at a time. Validation may consume provider credit.');
  if (pendingSubmission || [...inputs.values()].every(input => input.disabled)) later();
  return result;
}
async function pair() {
  const initial = await refreshStatus(); p.check(initial.status === 'open');
  const d = invitation.descriptor;
  const credential = await navigator.credentials.create({ signal: lifecycle.signal, publicKey: p.registrationOptions(initial.publicKey, d) });
  p.check(active());
  const registration = p.serializeCredential(credential);
  pairingIdentity = await p.registrationIdentity(registration, d.enrollment_id);
  const envelope = await p.encryptRegistration(registration, invitation);
  const result = await post('registration', envelope); requestMatches(result);
  p.check(result.operation === 'pairing_proof' && result.registration_digest === await p.digest(p.canon(envelope)));
  p.check(result.credential_digest === pairingIdentity.credentialDigest && result.fingerprint === pairingIdentity.fingerprint);
  const challenge = await p.possessionChallenge(d, pairingIdentity, result.proof_nonce);
  const signed = await navigator.credentials.get({ signal: lifecycle.signal, publicKey: p.assertionOptions(result.publicKey, d.setup_origin, challenge, pairingIdentity.credentialId) });
  const assertion = p.serializeCredential(signed);
  const proof = await post('proof', { assertion }); requestMatches(proof);
  p.check(proof.operation === 'pairing_status' && proof.assertion_digest === await p.digest(p.canon(assertion)) && proof.status === 'awaiting_confirmation' && proof.fingerprint === pairingIdentity.fingerprint);
  document.querySelector('#pair').disabled = true;
  displayFingerprint(pairingIdentity.fingerprint);
  await refreshStatus();
}
async function submit(field, input) {
  p.check(!pendingSubmission);
  const d = invitation.descriptor; const submissionId = p.random(16);
  const result = await post('challenge', { field, submission_id: submissionId });
  p.check(result.operation === 'challenge' && Number.isSafeInteger(result.expires_at) && result.expires_at > Date.now() / 1000 && result.expires_at <= d.expires_at);
  await p.validateContext(result.context, d, field, submissionId);
  let value = input.value;
  const encrypted = await p.encryptValue(value, result.context, d.recipient_public_key);
  input.value = ''; value = '';
  const assertion = p.serializeCredential(await navigator.credentials.get({ signal: lifecycle.signal, publicKey: { challenge: encrypted.challenge, rpId: new URL(d.setup_origin).hostname, userVerification: 'required', allowCredentials: [{ type: 'public-key', id: p.unb64(binding.credential_id) }] } }));
  p.check(active());
  const envelope = { context: result.context, enc: encrypted.enc, ct: encrypted.ct, assertion };
  const digest = await p.digest(p.canon(envelope));
  p.check(active());
  pendingSubmission = { field, submissionId, digest, envelope, transmissions: 0, lastSentAt: 0 };
  await sendPending();
}
async function sendPending() {
  p.check(pendingSubmission);
  const pending = pendingSubmission;
  p.check(active() && pending.transmissions < 3);
  pending.transmissions += 1; pending.lastSentAt = Date.now(); updateRetry();
  try {
    const receipt = await post('submission', pending.envelope); requestMatches(receipt);
    p.check(receipt.submission_id === pending.submissionId && receipt.digest === pending.digest && receipt.field === pending.field && Number.isSafeInteger(receipt.revision) && receipt.revision >= revision);
    revision = receipt.revision;
  } finally { clearInputs(); await refreshStatus(); }
}
async function retry() {
  p.check(pendingSubmission && retryEnabled());
  const pending = pendingSubmission;
  retryRunning = true; updateRetry();
  try {
    await refreshStatus();
    if (pendingSubmission !== pending || !active()) return;
    const delay = Math.max(0, pending.lastSentAt + retryBackoff(pending.transmissions) - Date.now());
    if (delay) {
      await new Promise(resolve => setTimeout(resolve, delay));
      if (pendingSubmission !== pending || !active()) return;
      await refreshStatus();
    }
    if (pendingSubmission === pending && active()) await sendPending();
  } finally { retryRunning = false; updateRetry(); }
}
async function cancel() {
  const client_nonce = p.random(16);
  const result = await post('cancel', { phase: 'challenge', client_nonce }); requestMatches(result);
  p.check(result.operation === 'cancel_challenge' && result.client_nonce === client_nonce && Number.isSafeInteger(result.expires_at) && result.expires_at > Date.now() / 1000 && result.expires_at <= invitation.descriptor.expires_at);
  p.check(p.unb64(result.challenge_id).length === 16 && p.unb64(result.connection_generation).length === 16 && p.unb64(result.nonce).length === 32 && Number.isSafeInteger(result.revision) && result.revision >= revision);
  const context = Object.fromEntries(['request_id', 'challenge_id', 'connection_generation', 'revision', 'nonce', 'expires_at'].map(name => [name, result[name]]));
  const challenge = await p.hash(p.concat(new TextEncoder().encode('sleeptalk/setup/cancel/v1\0'), p.canon(context)));
  const credential = await navigator.credentials.get({ signal: lifecycle.signal, publicKey: p.assertionOptions(result.publicKey, invitation.descriptor.setup_origin, challenge, binding.credential_id) });
  try { await post('cancel', { phase: 'confirm', challenge_id: result.challenge_id, assertion: p.serializeCredential(credential) }); }
  finally { await refreshStatus(); }
}
const labels = { 'deepgram.api_key': 'Deepgram API key', 'cartesia.api_key': 'Cartesia API key', 'elevenlabs.api_key': 'ElevenLabs API key', 'discord.bot_token': 'Discord bot token', 'agent.credential': 'Model provider credential' };
function setupFields() {
  for (const field of invitation.descriptor.allowed_fields) {
    const section = document.createElement('section');
    const label = document.createElement('label'); label.textContent = labels[field];
    const input = document.createElement('input'); input.type = 'password'; input.autocomplete = 'off'; input.spellcheck = false; input.maxLength = 8192; input.id = field; label.htmlFor = field;
    inputs.set(field, input);
    const state = document.createElement('p'); state.className = 'field-status'; state.textContent = 'Not saved yet';
    const scope = document.createElement('div'); scope.className = 'credential-scope';
    const action = invitation.descriptor.field_actions[field];
    const summary = document.createElement('p');
    summary.textContent = `${action === 'replace' ? 'Replace existing credential' : 'Create new credential'} · Expected generation ${invitation.descriptor.expected_credential_generations[field]}`;
    scope.append(summary);
    if (field === 'agent.credential') {
      const auth = invitation.descriptor.model_auth;
      const details = document.createElement('dl'); details.className = 'model-auth-scope';
      for (const [title, value] of [
        ['Provider', auth.provider], ['Model', auth.model],
        ['Authentication', auth.mode === 'oauth' ? 'OAuth access token' : 'API key'],
        ['Protocol', auth.protocol], ['Credential destination', auth.base_url],
      ]) {
        const name = document.createElement('dt'); name.textContent = title;
        const description = document.createElement('dd'); description.textContent = value;
        details.append(name, description);
      }
      scope.append(details);
    }
    const profiles = invitation.descriptor.affected_profile_ids[field];
    const scopeLabel = document.createElement('p'); scopeLabel.textContent = profiles.length ? 'Affected profiles:' : 'Affected profiles: none'; scope.append(scopeLabel);
    if (profiles.length) {
      const list = document.createElement('ul');
      for (const profile of profiles) { const item = document.createElement('li'); item.textContent = profile; list.append(item); }
      scope.append(list);
    }
    section.append(label, scope, input, button('Validate and save', () => submit(field, input)), state); content.append(section);
  }
  const actions = document.createElement('div'); actions.className = 'actions';
  const retryControl = button('Retry same submission', retry); retryControl.id = 'retry'; retryControl.disabled = true;
  actions.append(button('Check progress', refreshStatus), retryControl, button('Cancel setup on host', cancel)); content.append(actions);
}
(async () => {
  try {
    p.check(window.isSecureContext && typeof PublicKeyCredential !== 'undefined' && navigator.credentials && crypto.subtle);
    invitation = p.readInvitation(fragment, location.origin); fragment = '';
    await p.verifyInvitation(invitation);
    if (!active()) return;
    expiryTimer = setTimeout(() => active(), invitation.descriptor.expires_at * 1000 - Date.now());
    document.querySelector('#host').textContent = invitation.descriptor.display_host;
    document.querySelector('#workspace').textContent = invitation.descriptor.display_workspace;
    if (invitation.kind === 'setup') { binding = p.readBinding(localStorage, invitation.descriptor); setupFields(); }
    else { const control = button('Create passkey', pair); control.id = 'pair'; content.append(control); }
    main.removeAttribute('aria-busy');
    await refreshStatus();
  } catch { if (!stopped) finish('This invitation could not be verified, or this browser does not support secure setup. Use the paired browser and the original invitation.'); }
})();
