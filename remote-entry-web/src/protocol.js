import canonicalize from 'canonicalize';
import { parseTree } from 'jsonc-parser';
import { decodeFirst, encode as cborEncode } from 'cborg';
import { CipherSuite, DhkemP256HkdfSha256, HkdfSha256, Aes128Gcm } from '@hpke/core';

const utf8 = new TextEncoder();
const decoder = new TextDecoder('utf-8', { fatal: true });
const suite = new CipherSuite({ kem: new DhkemP256HkdfSha256(), kdf: new HkdfSha256(), aead: new Aes128Gcm() });
const providers = ['deepgram.api_key', 'cartesia.api_key', 'elevenlabs.api_key', 'discord.bot_token', 'agent.credential'];
export const HPKE_SUITE = 'DHKEM-P256-HKDF-SHA256/HKDF-SHA256/AES-128-GCM';
export const fail = () => { throw new Error('The secure setup check failed. Reopen the original invitation or return to the local setup page.'); };
export const check = condition => { if (!condition) fail(); };
export const concat = (...arrays) => { const result = new Uint8Array(arrays.reduce((n, a) => n + a.length, 0)); let offset = 0; for (const a of arrays) { result.set(a, offset); offset += a.length; } return result; };
export function b64(bytes) { return btoa(String.fromCharCode(...new Uint8Array(bytes))).replaceAll('+', '-').replaceAll('/', '_').replaceAll('=', ''); }
export function unb64(value) { check(typeof value === 'string' && value.length <= 44000 && /^[A-Za-z0-9_-]*$/.test(value)); const bytes = Uint8Array.from(atob(value.replaceAll('-', '+').replaceAll('_', '/')), c => c.charCodeAt(0)); check(b64(bytes) === value); return bytes; }
export const random = length => b64(crypto.getRandomValues(new Uint8Array(length)));
export const hash = async value => new Uint8Array(await crypto.subtle.digest('SHA-256', value));
export const hex = bytes => [...bytes].map(n => n.toString(16).padStart(2, '0')).join('');
export const digest = async value => hex(await hash(value));
export function object(value) { check(value !== null && typeof value === 'object' && !Array.isArray(value)); return value; }
export function fields(value, names) { object(value); check(Object.keys(value).length === names.length && names.every(name => Object.hasOwn(value, name))); }
function safe(value, depth = 0) {
  check(depth <= 20);
  if (value === null || typeof value === 'boolean' || typeof value === 'string') return;
  if (typeof value === 'number') { check(Number.isSafeInteger(value)); return; }
  if (Array.isArray(value)) { check(value.length <= 100); value.forEach(v => safe(v, depth + 1)); return; }
  for (const v of Object.values(object(value))) safe(v, depth + 1);
}
export function canon(value) { safe(value); return utf8.encode(canonicalize(value)); }
export function parse(text) {
  check(typeof text === 'string' && utf8.encode(text).length <= 32768);
  const errors = [];
  const tree = parseTree(text, errors, { disallowComments: true, allowTrailingComma: false });
  check(tree && errors.length === 0);
  function visit(node, depth = 0) {
    check(depth <= 40);
    if (node.type === 'object') { const names = node.children.map(child => child.children[0].value); check(new Set(names).size === names.length); }
    if (node.type === 'number') check(/^-?(0|[1-9][0-9]*)$/.test(text.slice(node.offset, node.offset + node.length)));
    for (const child of node.children ?? []) visit(child, depth + 1);
  }
  visit(tree);
  const value = JSON.parse(text); safe(value); return value;
}
const id = value => check(unb64(value).length === 16);
const sha = value => check(typeof value === 'string' && /^[a-f0-9]{64}$/.test(value));
export function readInvitation(fragment, origin, now = Math.floor(Date.now() / 1000)) {
  const invitation = parse(decoder.decode(unb64(fragment.replace(/^#/, ''))));
  fields(invitation, ['kind', 'descriptor', 'signature', 'capability']);
  check(invitation.kind === 'pairing' || invitation.kind === 'setup');
  const d = object(invitation.descriptor);
  const common = ['version', 'host_id', 'host_signing_public_key', 'hpke_suite', 'recipient_public_key', 'workspace_id', 'runtime_binding_digest', 'display_host', 'display_workspace', 'issued_at', 'expires_at', 'setup_origin', 'relay_origin', 'capability_hash'];
  const modelCredential = invitation.kind === 'setup' && Array.isArray(d.allowed_fields) && d.allowed_fields.includes('agent.credential');
  fields(d, [...common, ...(modelCredential ? ['model_auth'] : []), ...(invitation.kind === 'pairing' ? ['enrollment_id', 'registration_challenge'] : ['request_id', 'operator_id', 'operator_binding_revision', 'allowed_fields', 'field_actions', 'affected_profile_ids', 'expected_credential_generations'])]);
  check(d.version === 1 && d.hpke_suite === HPKE_SUITE);
  id(d.workspace_id); sha(d.host_id); sha(d.runtime_binding_digest); sha(d.capability_hash);
  check(unb64(d.host_signing_public_key).length === 32 && unb64(d.recipient_public_key).length === 65);
  check(unb64(invitation.signature).length === 64 && unb64(invitation.capability).length === 32);
  check(d.setup_origin === origin && d.relay_origin === origin && new URL(origin).protocol === 'https:');
  check(Number.isSafeInteger(d.issued_at) && Number.isSafeInteger(d.expires_at) && d.issued_at <= now + 30 && now < d.expires_at);
  check(d.expires_at - d.issued_at > 0 && d.expires_at - d.issued_at <= (invitation.kind === 'pairing' ? 600 : 900));
  for (const name of ['display_host', 'display_workspace']) check(typeof d[name] === 'string' && d[name].length > 0 && d[name].length <= 128);
  if (invitation.kind === 'pairing') { id(d.enrollment_id); check(unb64(d.registration_challenge).length === 32); }
  else {
    id(d.request_id); id(d.operator_id); check(Number.isSafeInteger(d.operator_binding_revision) && d.operator_binding_revision >= 0);
    check(Array.isArray(d.allowed_fields) && d.allowed_fields.length > 0 && d.allowed_fields.length <= 5 && new Set(d.allowed_fields).size === d.allowed_fields.length);
    if (modelCredential) {
      const auth = d.model_auth;
      fields(auth, ['mode', 'protocol', 'base_url', 'model', 'provider']);
      check(['api_key', 'oauth'].includes(auth.mode));
      check(['openai-completions', 'openai-responses', 'anthropic-messages', 'cursor', 'openai-codex-responses'].includes(auth.protocol));
      for (const [name, limit] of [['model', 512], ['provider', 128]]) check(typeof auth[name] === 'string' && auth[name].length > 0 && auth[name].length <= limit && !/[\u0000-\u001f\u007f-\u009f\u202a-\u202e\u2066-\u2069]/.test(auth[name]));
      check(typeof auth.base_url === 'string' && auth.base_url.length > 0 && auth.base_url.length <= 2048 && !/[^\x21-\x7e]|[\\%]/.test(auth.base_url));
      const endpoint = new URL(auth.base_url);
      check(endpoint.protocol === 'https:' && endpoint.hostname && !endpoint.username && !endpoint.password && !endpoint.search && !endpoint.hash && endpoint.port !== '0');
      check(!auth.base_url.split('/')[2].includes('@'));
      check(auth.base_url === endpoint.origin + endpoint.pathname.replace(/\/+$/, ''));
      const nativeOAuth = {
        'anthropic-messages': 'https://api.anthropic.com',
        'openai-codex-responses': 'https://chatgpt.com/backend-api',
        cursor: 'https://api2.cursor.sh',
      };
      if (auth.mode === 'oauth') check(auth.base_url === nativeOAuth[auth.protocol]);
      if (auth.protocol === 'openai-codex-responses') check(auth.mode === 'oauth');
      if (auth.protocol === 'cursor') check(auth.base_url === nativeOAuth.cursor);
    }
    fields(d.field_actions, d.allowed_fields); fields(d.expected_credential_generations, d.allowed_fields);
    for (const field of d.allowed_fields) { check(providers.includes(field) && ['create', 'replace'].includes(d.field_actions[field])); check(Number.isSafeInteger(d.expected_credential_generations[field]) && d.expected_credential_generations[field] >= 0); }
    fields(d.affected_profile_ids, d.allowed_fields);
    for (const profiles of Object.values(d.affected_profile_ids)) { check(Array.isArray(profiles) && profiles.length <= 100); profiles.forEach(value => check(typeof value === 'string' && value.length > 0 && value.length <= 256)); }
  }
  return invitation;
}
export async function verifySignature(payload, signature, publicKey, purpose = 'receipt') {
  check(['receipt', 'invitation', 'enrollment'].includes(purpose));
  const key = await crypto.subtle.importKey('raw', unb64(publicKey), 'Ed25519', false, ['verify']);
  check(await crypto.subtle.verify('Ed25519', key, unb64(signature), concat(utf8.encode(`sleeptalk/setup/${purpose}/v1\0`), canon(payload))));
}
export async function verifyInvitation(invitation) {
  const d = invitation.descriptor;
  check(await digest(unb64(d.host_signing_public_key)) === d.host_id);
  check(await digest(unb64(invitation.capability)) === d.capability_hash);
  await verifySignature(d, invitation.signature, d.host_signing_public_key, invitation.kind === 'pairing' ? 'enrollment' : 'invitation');
  // Exercise the selected HPKE suite before offering any credential field.
  await encrypt(d.recipient_public_key, new Uint8Array(), utf8.encode('sleeptalk/setup/browser-feature-check/v1'));
}
export function bindingKey(hostId) { sha(hostId); return `sleeptalk.host.${hostId}`; }
export function readBinding(storage, d) {
  const value = storage.getItem(bindingKey(d.host_id)); check(value !== null);
  const binding = parse(value); fields(binding, ['host_id', 'public_key', 'operator_id', 'credential_id']);
  check(binding.host_id === d.host_id && binding.public_key === d.host_signing_public_key && binding.operator_id === d.operator_id);
  check(unb64(binding.credential_id).length > 0 && unb64(binding.credential_id).length <= 1024);
  return binding;
}
export async function encrypt(recipient, plaintext, info) {
  const key = await suite.kem.deserializePublicKey(unb64(recipient));
  const sender = await suite.createSenderContext({ recipientPublicKey: key, info });
  return { enc: b64(sender.enc), ct: b64(await sender.seal(plaintext)) };
}
export function serializeCredential(credential) {
  check(credential instanceof PublicKeyCredential);
  const response = credential.response;
  const base = { id: credential.id, rawId: b64(credential.rawId), type: 'public-key', clientExtensionResults: {} };
  if (response instanceof AuthenticatorAttestationResponse) return { ...base, response: { clientDataJSON: b64(response.clientDataJSON), attestationObject: b64(response.attestationObject), transports: response.getTransports() } };
  check(response instanceof AuthenticatorAssertionResponse);
  return { ...base, response: { clientDataJSON: b64(response.clientDataJSON), authenticatorData: b64(response.authenticatorData), signature: b64(response.signature), userHandle: response.userHandle === null ? null : b64(response.userHandle) } };
}
export function registrationOptions(options, d) {
  object(options); check(options.challenge === d.registration_challenge && options.rp.id === new URL(d.setup_origin).hostname && options.user.id === d.enrollment_id);
  check(options.authenticatorSelection.userVerification === 'required' && options.attestation === 'none');
  return { ...options, challenge: unb64(options.challenge), user: { ...options.user, id: unb64(options.user.id) } };
}
export function assertionOptions(options, origin, challenge, credentialId) {
  object(options); check(options.challenge === b64(challenge) && options.rpId === new URL(origin).hostname && options.userVerification === 'required');
  check(options.allowCredentials.length === 1 && options.allowCredentials[0].type === 'public-key' && options.allowCredentials[0].id === credentialId);
  return { ...options, challenge, allowCredentials: [{ type: 'public-key', id: unb64(credentialId) }] };
}
export async function registrationIdentity(registration, enrollmentId) {
  const [attestation, rest] = decodeFirst(unb64(registration.response.attestationObject), { useMaps: true });
  check(rest.length === 0 && attestation instanceof Map);
  const auth = attestation.get('authData'); check(auth instanceof Uint8Array && auth.length >= 55 && (auth[32] & 64) !== 0);
  const length = auth[53] * 256 + auth[54]; check(length > 0 && length <= 1024);
  const credentialId = b64(auth.slice(55, 55 + length)); check(credentialId === registration.id);
  const [key] = decodeFirst(auth.slice(55 + length), { useMaps: true }); check(key instanceof Map);
  const publicKey = cborEncode(key);
  const encodedCredential = concat(auth.slice(37, 55 + length), publicKey);
  const fingerprint = hex((await hash(concat(utf8.encode('sleeptalk/setup/enrollment/fingerprint/v1\0'), canon({ enrollment_id: enrollmentId, credential_id: credentialId, public_key: b64(publicKey) }))))).slice(0, 32);
  return { credentialId, credentialDigest: await digest(encodedCredential), fingerprint };
}
export const enrollmentContext = async d => ({ enrollment_id: d.enrollment_id, host_id: d.host_id, descriptor_digest: await digest(canon(d)) });
export async function encryptRegistration(registration, invitation) {
  const context = await enrollmentContext(invitation.descriptor);
  return { context, ...await encrypt(invitation.descriptor.recipient_public_key, canon({ capability: invitation.capability, registration }), concat(utf8.encode('sleeptalk/setup/enrollment/registration/v1\0'), canon(context))) };
}
export async function possessionChallenge(d, identity, nonce) {
  check(unb64(nonce).length === 32);
  return hash(concat(utf8.encode('sleeptalk/setup/enrollment/possession/v1\0'), canon({ enrollment_id: d.enrollment_id, descriptor_digest: await digest(canon(d)), credential_digest: identity.credentialDigest, nonce })));
}
export async function validateContext(context, d, field, submissionId) {
  fields(context, ['version', 'request_id', 'descriptor_digest', 'host_id', 'operator_id', 'operator_binding_revision', 'workspace_id', 'runtime_binding_digest', 'provider_field', 'action', 'expected_credential_generation', 'submission_id', 'challenge_id', 'challenge_nonce', 'connection_generation', 'request_revision', 'request_expires_at']);
  for (const name of ['version', 'request_id', 'host_id', 'operator_id', 'operator_binding_revision', 'workspace_id', 'runtime_binding_digest']) check(context[name] === d[name]);
  check(context.descriptor_digest === await digest(canon(d)) && context.provider_field === field && context.submission_id === submissionId);
  check(context.action === d.field_actions[field] && context.expected_credential_generation === d.expected_credential_generations[field] && context.request_expires_at === d.expires_at);
  id(context.challenge_id); id(context.connection_generation); check(unb64(context.challenge_nonce).length === 32 && Number.isSafeInteger(context.request_revision) && context.request_revision >= 0);
}
export async function encryptValue(value, context, recipient) {
  check(typeof value === 'string' && utf8.encode(value).length > 0 && utf8.encode(value).length <= 8192);
  const ciphertext = await encrypt(recipient, canon({ value }), concat(utf8.encode('sleeptalk/setup/submission/v1\0'), canon(context)));
  const combined = concat(unb64(ciphertext.enc), unb64(ciphertext.ct));
  return { ...ciphertext, challenge: await hash(concat(utf8.encode('sleeptalk/setup/authorize/v1\0'), canon(context), await hash(combined))) };
}
export const fieldFailures = Object.freeze({
  invalid_credential: 'The provider did not accept this credential. Check it and enter it again.',
  insufficient_scope: 'The credential needs more provider permissions. Update its permissions and try again.',
  voice_unavailable: 'The selected voice is unavailable. Return to local setup to choose a voice.',
  quota_exhausted: 'The provider quota is exhausted. Resolve the account limit before trying again.',
  network_unavailable: 'The host could not reach the provider. Check its connection and try again.',
  validation_failed: 'The host could not validate this credential. Check it and try again.',
  validation_timeout: 'Provider validation timed out. You can enter the credential again.',
  interrupted: 'Provider validation was interrupted. You can enter the credential again.'
});
export function matchingFailure(state, submissionId) { return state !== null && typeof state === 'object' && Object.hasOwn(fieldFailures, state.status) && state.submission_id === submissionId; }
