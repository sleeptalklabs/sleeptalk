import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
const source = await readFile(process.argv[2], 'utf8');
const { default: install, validateModel } = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);
const native = (provider = 'openai') => ({
  model: { provider, id: 'selected-model', baseUrl: provider === 'openai' ? 'https://api.openai.com/v1' : 'https://api.anthropic.com' },
  modelRegistry: {
    isUsingOAuth: () => false,
    getApiKeyAndHeaders: async () => ({ ok: true, apiKey: 'sentinel-key' }),
  },
});
for (const provider of ['openai', 'anthropic']) {
  const ctx = native(provider);
  let calls = 0;
  assert.equal(await validateModel(ctx, async (url, options) => {
    calls++;
    assert.equal(url, `https://api.${provider === 'openai' ? 'openai' : 'anthropic'}.com/v1/models/selected-model`);
    assert.equal(options.method, 'GET');
    assert.equal(options.redirect, 'error');
    assert.ok(options.signal instanceof AbortSignal);
    assert.equal(options.headers.get(provider === 'openai' ? 'Authorization' : 'x-api-key'), provider === 'openai' ? 'Bearer sentinel-key' : 'sentinel-key');
    return Response.json({ id: 'selected-model', [provider === 'openai' ? 'object' : 'type']: 'model' });
  }), 'available');
  assert.equal(calls, 1);
}
for (const status of [401, 403, 404, 429, 500, 302]) {
  assert.equal(await validateModel(native(), async () => new Response('provider-private-body', { status })), status === 401 ? 'unavailable' : 'unknown');
}
for (const mutate of [
  (ctx) => { ctx.model.baseUrl = 'https://proxy.invalid/v1'; },
  (ctx) => { ctx.model.provider = 'custom'; },
  (ctx) => { ctx.modelRegistry.isUsingOAuth = () => true; },
  (ctx) => { ctx.modelRegistry.getApiKeyAndHeaders = async () => ({ ok: true, apiKey: 'sentinel-key', baseUrl: 'https://proxy.invalid' }); },
  (ctx) => { ctx.model.baseUrl = 'https://api.openai.com/foreign/path'; },
]) {
  const ctx = native(); mutate(ctx);
  assert.equal(await validateModel(ctx, () => { throw new Error('Must not send a request'); }), 'unknown');
}
const missing = native(); missing.modelRegistry.getApiKeyAndHeaders = async () => ({ ok: false });
assert.equal(await validateModel(missing), 'unavailable');
assert.equal(await validateModel(native(), async () => new Response('x'.repeat(40000))), 'unknown');
assert.equal(await validateModel(native(), async () => { throw new Error('sentinel-key'); }), 'unknown');
assert.equal(await validateModel(native(), async () => Response.json({ id: 'other', object: 'model' })), 'unknown');
const older = native();
delete older.modelRegistry.getApiKeyAndHeaders;
older.modelRegistry.getApiKey = async () => 'sentinel-key';
assert.equal(await validateModel(older, async () => Response.json({ id: 'selected-model', object: 'model' })), 'available');
for (const mode of ['pending', 'deny']) {
  process.env.SLEEPTALK_PERMISSION_MODE = mode;
  process.env.SLEEPTALK_PERMISSION_NONCE = 'fresh-nonce';
  process.env.SLEEPTALK_ONBOARDING_PROBE = '0';
  const handlers = {};
  install({ on: (name, handler) => { handlers[name] = handler; } });
  const status = [];
  await handlers.session_start({}, { cwd: '/expected/workspace', ui: { setStatus: (...args) => status.push(args) } });
  assert.deepEqual(JSON.parse(status[0][1]), { version: 1, nonce: 'fresh-nonce', mode, workspace: '/expected/workspace', login: 'unknown' });
  for (const toolName of ['bash', 'read', 'write', 'edit', 'mcp__custom', 'task', 'eval']) {
    for (const answer of [false, undefined, true]) {
      let confirms = 0;
      const decision = await handlers.tool_call({ toolName, input: { command: 'anything' } }, { ui: { confirm: async () => { confirms++; return answer; } } });
      assert.equal(decision?.block, mode === 'pending' && answer === true ? undefined : true);
      assert.equal(confirms, mode === 'deny' ? 0 : 1);
    }
  }
  if (mode === 'pending') {
    for (const toolName of ['bash', 'sentinel-private-name\n' + 'x'.repeat(10000)]) {
      let prompt;
      const input = { command: 'sentinel-secret-value'.repeat(10000) };
      await handlers.tool_call({ toolName, input }, { ui: { confirm: async (_title, message) => { prompt = message; return false; } } });
      assert.ok(prompt.length < 130);
      assert.ok(!prompt.includes('sentinel'));
      assert.ok(prompt.startsWith('Allow the pending '));
    }
  }
  const controller = new AbortController(); controller.abort();
  assert.equal((await handlers.tool_call({}, { signal: controller.signal }))?.block, true);
  assert.equal((await handlers.tool_call({}, { ui: { confirm: async () => { throw new Error('closed'); } } }))?.block, true);
  if (mode === 'pending') {
    const oldTimeout = globalThis.setTimeout;
    globalThis.setTimeout = (fn) => { queueMicrotask(fn); return undefined; };
    try {
      assert.equal((await handlers.tool_call({}, { ui: { confirm: () => new Promise(() => {}) } }))?.block, true);
    } finally { globalThis.setTimeout = oldTimeout; }
  }
}
process.env.SLEEPTALK_PERMISSION_MODE = 'auto';
assert.throws(() => install({ on: () => {} }), /permission policy/);
console.log('Extension permission and provider checks passed');
