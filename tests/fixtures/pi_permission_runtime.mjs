import { readFile, writeFile } from 'node:fs/promises';
import { createInterface } from 'node:readline';
const asset = process.argv[process.argv.indexOf('--extension') + 1];
const source = await readFile(asset, 'utf8');
const { default: install } = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);
const out = (frame) => process.stdout.write(JSON.stringify(frame) + '\n');
const handlers = {};
const requests = new Map();
const ctx = {
  cwd: process.cwd(),
  ui: {
    setStatus: (statusKey, statusText) => out({ type: 'extension_ui_request', method: 'setStatus', statusKey, statusText }),
    confirm: (title, message, options) => new Promise((resolve) => {
      requests.set('permission', resolve);
      out({ type: 'extension_ui_request', method: 'confirm', id: 'permission', title, message, timeout: options.timeout });
    }),
  },
};
install({ on: (name, handler) => { handlers[name] = handler; } });
await handlers.session_start({}, ctx);
const input = createInterface({ input: process.stdin });
input.on('line', async (line) => {
  const frame = JSON.parse(line);
  if (frame.type === 'extension_ui_response') {
    requests.get(frame.id)?.(frame.confirmed === true);
    requests.delete(frame.id);
  } else if (frame.type === 'get_state') {
    out({ id: frame.id, type: 'response', success: true, data: { isStreaming: false } });
  } else if (frame.type === 'prompt') {
    out({ id: frame.id, type: 'response', success: true });
    out({ type: 'agent_start' });
    const result = await handlers.tool_call({ toolName: 'arbitrary-extension-tool', input: { argument: 'value' } }, ctx);
    if (!result?.block) await writeFile(process.env.TEST_TOOL_MARKER, 'executed');
    out({ type: 'agent_settled' });
  }
});
