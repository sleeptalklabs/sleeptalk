import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';
const [root, cwd, provider, model, launcher] = process.argv.slice(2);
const { createProtectedSession, serve } = await import(pathToFileURL(launcher).href);
let requests = 0;
globalThis.fetch = async (url, options) => {
  requests++;
  assert.equal(url, `https://api.openai.com/v1/models/${model}`);
  assert.equal(options.method, 'GET');
  assert.equal(options.redirect, 'error');
  assert.equal(options.headers.get('authorization'), 'Bearer synthetic-key');
  return Response.json({ id: model, object: 'model' });
};
if (process.env.SLEEPTALK_ONBOARDING_PROBE === '1') {
  await serve(root, cwd, provider, model);
  assert.equal(requests, 1);
} else {
  const mode = process.env.SLEEPTALK_PERMISSION_MODE;
  let consent;
  let attested = false;
  const ui = {
    setStatus(key, value) { if (key === 'sleeptalk-permissions') attested = JSON.parse(value).mode === mode; },
    setWidget() {}, notify() {}, setWorkingMessage() {},
    confirm: () => new Promise(resolve => { consent = resolve; }),
  };
  const { session, auth } = await createProtectedSession(root, cwd, provider, model, ui);
  try {
    assert.equal(attested, true);
    assert.deepEqual(session.getEnabledToolNames().sort(), ["read", "bash", "edit", "write", "grep", "glob"].sort());
    const write = session.agent.state.tools.find(tool => tool.name === 'write');
    assert.ok(write);
    const marker = `${cwd}/authorized-write`;
    const operation = write.execute('synthetic-tool-call', { path: marker, content: 'explicitly approved' }, new AbortController().signal);
    if (mode === 'pending') {
      const deadline = Date.now() + 3000;
      while (!consent && Date.now() < deadline) await Bun.sleep(5);
      assert.ok(consent, 'The actual SDK tool must request permission');
      assert.equal(existsSync(marker), false);
      consent(true);
      await operation;
      assert.equal(readFileSync(marker, 'utf8'), 'explicitly approved');
    } else {
      await operation.catch(() => {});
      assert.equal(existsSync(marker), false);
    }
    assert.equal(requests, 0, 'No model prompt or network request');
    console.log(JSON.stringify({ tested: 'actual-sdk-tool', mode }));
  } finally { await session.dispose(); auth.close(); }
}
