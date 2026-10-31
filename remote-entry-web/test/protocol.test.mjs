import test from 'node:test';
import assert from 'node:assert/strict';
import { generateKeyPairSync, sign, createHash } from 'node:crypto';
import { CipherSuite, DhkemP256HkdfSha256, HkdfSha256, Aes128Gcm } from '@hpke/core';
import * as p from '../src/protocol.js';
const suite = new CipherSuite({ kem: new DhkemP256HkdfSha256(), kdf: new HkdfSha256(), aead: new Aes128Gcm() });
const text = new TextEncoder();
async function fixture() {
  const host = generateKeyPairSync('ed25519'); const rawHost = host.publicKey.export({format:'jwk'}).x;
  const recipient = await suite.kem.generateKeyPair();
  const d = {version:1, enrollment_id:p.random(16),host_id:await p.digest(p.unb64(rawHost)),host_signing_public_key:rawHost,hpke_suite:p.HPKE_SUITE,recipient_public_key:p.b64(await suite.kem.serializePublicKey(recipient.publicKey)),workspace_id:p.random(16),runtime_binding_digest:'a'.repeat(64),display_host:'Test',display_workspace:'Fixture',setup_origin:'https://setup.example.com',relay_origin:'https://setup.example.com',issued_at:1900000000,expires_at:1900000600,capability_hash:'',registration_challenge:p.random(32)};
  const capability=p.random(32);d.capability_hash=await p.digest(p.unb64(capability));
  const signature=p.b64(sign(null,p.concat(text.encode('sleeptalk/setup/enrollment/v1\0'),p.canon(d)),host.privateKey));
  return {invitation:{kind:'pairing',descriptor:d,signature,capability},recipient,host};
}
test('strict JSON and canonical base64 reject ambiguous messages',()=>{
  for(const raw of ['{"x":1,"x":2}','{"x":1.0}','{"x":9007199254740992}','{"x":NaN}','{"x":1,}','/*comment*/{}']) assert.throws(()=>p.parse(raw));
  for(const raw of ['Zg==','Zh','+/', 'é']) assert.throws(()=>p.unb64(raw));
  assert.deepEqual(p.parse('{"z":1,"a":"é"}'),{z:1,a:'é'});
  assert.equal(new TextDecoder().decode(p.canon({z:1,a:'é'})),'{"a":"é","z":1}');
});
test('pinned descriptor requires signature, origin, capability, expiry and known fields',async()=>{
 const {invitation}=await fixture();
 const encode=i=>p.b64(p.canon(i));
 assert.deepEqual(p.readInvitation(encode(invitation),'https://setup.example.com',1900000001),invitation);
 await p.verifyInvitation(invitation);
 await assert.rejects(()=>p.verifyInvitation({...invitation,capability:p.random(32)}));
 await assert.rejects(()=>p.verifyInvitation({...invitation,descriptor:{...invitation.descriptor,display_host:'Changed'}}));
 for(const i of [{...invitation,unknown:1},{...invitation,descriptor:{...invitation.descriptor,extra:1}}]) assert.throws(()=>p.readInvitation(encode(i),'https://setup.example.com',1900000001));
 assert.throws(()=>p.readInvitation(encode(invitation),'https://wrong.example.com',1900000001));
 assert.throws(()=>p.readInvitation(encode(invitation),'https://setup.example.com',1900000601));
});
test('HPKE credential ciphertext and authorization challenge bind exact context',async()=>{
 const {invitation,recipient}=await fixture();
 const context={request_id:p.random(16),provider_field:'deepgram.api_key'};
 const encrypted=await p.encryptValue('synthetic-private-value',context,invitation.descriptor.recipient_public_key);
 const info=p.concat(text.encode('sleeptalk/setup/submission/v1\0'),p.canon(context));
 const receiver=await suite.createRecipientContext({recipientKey:recipient.privateKey,enc:p.unb64(encrypted.enc),info});
 assert.deepEqual(p.parse(new TextDecoder().decode(await receiver.open(p.unb64(encrypted.ct)))),{value:'synthetic-private-value'});
 const wrong=await suite.createRecipientContext({recipientKey:recipient.privateKey,enc:p.unb64(encrypted.enc),info:text.encode('wrong')});
 await assert.rejects(()=>wrong.open(p.unb64(encrypted.ct)));
 const combined=p.concat(p.unb64(encrypted.enc),p.unb64(encrypted.ct));
 const expected=createHash('sha256').update(p.concat(text.encode('sleeptalk/setup/authorize/v1\0'),p.canon(context),createHash('sha256').update(combined).digest())).digest();
 assert.deepEqual(Buffer.from(encrypted.challenge),expected);
});
test('public binding requires exact host and operator',async()=>{
 const {invitation}=await fixture(); const d={...invitation.descriptor,operator_id:p.random(16)};
 const binding={host_id:d.host_id,public_key:d.host_signing_public_key,operator_id:d.operator_id,credential_id:p.random(32)};
 const storage={getItem:()=>JSON.stringify(binding)};
 assert.deepEqual(p.readBinding(storage,d),binding);
 assert.throws(()=>p.readBinding(storage,{...d,operator_id:p.random(16)}));
 assert.throws(()=>p.readBinding(storage,{...d,host_signing_public_key:p.random(32)}));
});
test('setup invitation maps field actions and expected generations',async()=>{
 const {invitation,host}=await fixture(); const d={...invitation.descriptor}; delete d.enrollment_id; delete d.registration_challenge;
 Object.assign(d,{request_id:p.random(16),operator_id:p.random(16),operator_binding_revision:1,allowed_fields:['deepgram.api_key'],field_actions:{'deepgram.api_key':'replace'},expected_credential_generations:{'deepgram.api_key':1},affected_profile_ids:{'deepgram.api_key':['existing-profile@0123456789ab']}});
 const setup={kind:'setup',descriptor:d,capability:invitation.capability,signature:p.b64(sign(null,p.concat(text.encode('sleeptalk/setup/invitation/v1\0'),p.canon(d)),host.privateKey))};
 assert.deepEqual(p.readInvitation(p.b64(p.canon(setup)),d.setup_origin,1900000001),setup);
 await p.verifyInvitation(setup);
});

test('only a terminal failure for the pending submission permits new input',()=>{
 const pending=p.random(16);
 assert.equal(p.matchingFailure({status:'invalid_credential',submission_id:pending},pending),true);
 assert.equal(p.matchingFailure({status:'invalid_credential',submission_id:p.random(16)},pending),false);
 assert.equal(p.matchingFailure({status:'invalid_credential'},pending),false);
 assert.equal(p.matchingFailure({status:'validating',submission_id:pending},pending),false);
});

test('replacement scope accepts 65 profile labels and rejects 101',async()=>{
 const {invitation,host}=await fixture(); const d={...invitation.descriptor}; delete d.enrollment_id; delete d.registration_challenge;
 Object.assign(d,{request_id:p.random(16),operator_id:p.random(16),operator_binding_revision:1,allowed_fields:['deepgram.api_key'],field_actions:{'deepgram.api_key':'replace'},expected_credential_generations:{'deepgram.api_key':1},affected_profile_ids:{'deepgram.api_key':Array.from({length:65},(_,i)=>`profile-${i}@0123456789ab`)}});
 const setup={kind:'setup',descriptor:d,capability:invitation.capability,signature:p.b64(sign(null,p.concat(text.encode('sleeptalk/setup/invitation/v1\0'),p.canon(d)),host.privateKey))};
 assert.equal(p.readInvitation(p.b64(p.canon(setup)),d.setup_origin,1900000001).descriptor.affected_profile_ids['deepgram.api_key'].length,65);
 d.affected_profile_ids['deepgram.api_key']=Array.from({length:101},(_,i)=>`profile-${i}`);
 assert.throws(()=>p.readInvitation(p.b64(Buffer.from(JSON.stringify(setup))),d.setup_origin,1900000001));
});

async function modelSetup() {
  const { invitation, host } = await fixture();
  const d = { ...invitation.descriptor };
  delete d.enrollment_id; delete d.registration_challenge;
  const field = 'agent.credential';
  Object.assign(d, {
    request_id: p.random(16), operator_id: p.random(16), operator_binding_revision: 1,
    allowed_fields: [field], field_actions: { [field]: 'create' },
    expected_credential_generations: { [field]: 0 }, affected_profile_ids: { [field]: [] },
    model_auth: { mode: 'api_key', protocol: 'openai-completions', base_url: 'https://provider.example/proxy/v1', model: 'selected-model', provider: 'selected-provider' },
  });
  return { kind: 'setup', descriptor: d, capability: invitation.capability,
    signature: p.b64(sign(null, p.concat(text.encode('sleeptalk/setup/invitation/v1\0'), p.canon(d)), host.privateKey)) };
}
const readSetup = setup => p.readInvitation(p.b64(p.canon(setup)), 'https://setup.example.com', 1900000001);

test('model credential selection is signed and required exactly for its field', async () => {
  const setup = await modelSetup();
  assert.deepEqual(readSetup(setup), setup);
  await p.verifyInvitation(setup);
  const missing = structuredClone(setup); delete missing.descriptor.model_auth;
  assert.throws(() => readSetup(missing));
  const unrelated = structuredClone(setup);
  unrelated.descriptor.allowed_fields = ['deepgram.api_key'];
  unrelated.descriptor.field_actions = { 'deepgram.api_key': 'create' };
  unrelated.descriptor.expected_credential_generations = { 'deepgram.api_key': 0 };
  unrelated.descriptor.affected_profile_ids = { 'deepgram.api_key': [] };
  assert.throws(() => readSetup(unrelated));
  const unknown = structuredClone(setup); unknown.descriptor.allowed_fields = ['agent.token'];
  assert.throws(() => readSetup(unknown));
  for (const [key, value] of Object.entries({ model: 'another-model', provider: 'another-provider', mode: 'oauth', protocol: 'anthropic-messages', base_url: 'https://another.example' })) {
    const changed = structuredClone(setup); changed.descriptor.model_auth[key] = value;
    await assert.rejects(() => p.verifyInvitation(changed));
  }
});

test('model selection rejects unknown schema, invalid bounds and ambiguous destinations', async () => {
  const setup = await modelSetup();
  const mutations = [
    auth => { delete auth.provider; }, auth => { auth.credential = 'must-never-be-in-descriptor'; },
    auth => { auth.mode = 'password'; }, auth => { auth.protocol = 'arbitrary'; },
    auth => { auth.provider = ''; }, auth => { auth.provider = 'p'.repeat(129); },
    auth => { auth.model = ''; }, auth => { auth.model = 'm'.repeat(513); },
    ...['http://provider.example', 'https://user@provider.example', 'https://@provider.example',
      'https://provider.example?token=x', 'https://provider.example#x', 'https://provider.example/',
      'https://PROVIDER.example', 'https://provider.example:443', 'https://provider.example:0',
      'https://provider.example/../v1', 'https://provider.example/%2e%2e',
      'https://provider.example/\\v1', 'https://provider.example/é', 'https://provider.example/\n'].map(base => auth => { auth.base_url = base; }),
  ];
  for (const mutate of mutations) {
    const changed = structuredClone(setup); mutate(changed.descriptor.model_auth);
    assert.throws(() => readSetup(changed));
  }
});

test('model authentication permits only the supported native OAuth contracts', async () => {
  const setup = await modelSetup();
  const native = {
    'anthropic-messages': 'https://api.anthropic.com',
    'openai-codex-responses': 'https://chatgpt.com/backend-api',
    cursor: 'https://api2.cursor.sh',
  };
  for (const mode of ['api_key', 'oauth']) {
    for (const protocol of ['openai-completions', 'openai-responses', ...Object.keys(native)]) {
      for (const base_url of ['https://provider.example/v1', ...Object.values(native)]) {
        const changed = structuredClone(setup);
        Object.assign(changed.descriptor.model_auth, { mode, protocol, base_url });
        const allowed = mode === 'oauth' ? native[protocol] === base_url
          : protocol !== 'openai-codex-responses' && (protocol !== 'cursor' || base_url === native.cursor);
        if (allowed) assert.deepEqual(readSetup(changed), changed);
        else assert.throws(() => readSetup(changed), `${mode} ${protocol} ${base_url}`);
      }
    }
  }
});

test('model consent labels reject control and bidi characters without rejecting ordinary Unicode', async () => {
  const setup = await modelSetup();
  const forbidden = [
    ...Array.from({ length: 32 }, (_, i) => i),
    ...Array.from({ length: 33 }, (_, i) => 0x7f + i),
    ...Array.from({ length: 5 }, (_, i) => 0x202a + i),
    ...Array.from({ length: 4 }, (_, i) => 0x2066 + i),
  ];
  for (const name of ['model', 'provider']) {
    for (const code of forbidden) {
      const changed = structuredClone(setup);
      changed.descriptor.model_auth[name] = `visible${String.fromCodePoint(code)}hidden`;
      assert.throws(() => readSetup(changed), `${name} U+${code.toString(16)}`);
    }
    const changed = structuredClone(setup);
    changed.descriptor.model_auth[name] = 'Modèle 日本語 العربية';
    assert.equal(readSetup(changed).descriptor.model_auth[name], changed.descriptor.model_auth[name]);
  }
});
