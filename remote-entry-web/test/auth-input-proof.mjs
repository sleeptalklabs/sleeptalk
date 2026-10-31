import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { createInterface } from 'node:readline';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';

const python = process.env.SLEEPTALK_TEST_PYTHON || fileURLToPath(new URL('../../sleeptalk/.venv/bin/python', import.meta.url));
const child = spawn(python, ['-u', fileURLToPath(new URL('./auth_input_host.py', import.meta.url))], { stdio: ['ignore', 'pipe', 'pipe'] });
let stderr = '';
child.stderr.on('data', data => { stderr += data; });
const lines = createInterface({ input: child.stdout })[Symbol.asyncIterator]();
const timeout = setTimeout(() => child.kill('SIGKILL'), 30000);
let browser;
try {
  const invitation = JSON.parse((await lines.next()).value);
  browser = await chromium.launch({ headless: true, executablePath: process.env.CHROME_PATH ?? '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' });
  const page = await browser.newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.goto(invitation.input_url);
  await page.locator('#status').filter({ hasText: 'Ready for private entry.' }).waitFor();
  assert.match(await page.locator('#scope').textContent(), /synthetic-provider\.test/);
  assert.equal(new URL(page.url()).hash, '');
  await page.locator('#credential').fill('incorrect-synthetic-key');
  await page.locator('#confirm').check();
  await page.locator('form button').click();
  await page.locator('#status').filter({ hasText: 'could not verify' }).waitFor();
  assert.equal(await page.locator('#credential').inputValue(), '');
  await page.locator('#credential').fill('synthetic-browser-key');
  await page.locator('form button').click();
  await page.locator('#status').filter({ hasText: 'Credential saved.' }).waitFor();
  assert.equal(await page.locator('form').isVisible(), false);
  assert.deepEqual(await page.evaluate(() => [localStorage.length, sessionStorage.length]), [0, 0]);
  assert.deepEqual(JSON.parse((await lines.next()).value), { status: 'complete', stored: true });
  assert.deepEqual(errors, []);
  assert.equal(stderr, '');
  console.log('Private model credential browser proof passed: rejection, correction, storage and no browser persistence.');
} finally {
  clearTimeout(timeout);
  await browser?.close();
  child.kill('SIGTERM');
}
