import { build } from 'esbuild';
import { mkdir, copyFile, readFile, writeFile } from 'node:fs/promises';
import { createHash } from 'node:crypto';
const output = new URL('../sleeptalk/_remote_entry/', import.meta.url);
await mkdir(output, { recursive: true });
await build({ entryPoints: [new URL('./src/app.js', import.meta.url).pathname], outfile: new URL('app.js', output).pathname, bundle: true, minify: true, format: 'esm', target: ['es2022'], legalComments: 'linked' });
for (const name of ['index.html', 'style.css']) await copyFile(new URL(`src/${name}`, import.meta.url), new URL(name, output));

const dependencies = ['@hpke/core', '@hpke/common', 'canonicalize', 'cborg', 'jsonc-parser'];
const notices = [];
for (const name of dependencies) {
  const root = new URL(`node_modules/${name}/`, import.meta.url);
  const metadata = JSON.parse(await readFile(new URL('package.json', root), 'utf8'));
  const license = await readFile(new URL(name === 'jsonc-parser' ? 'LICENSE.md' : 'LICENSE', root), 'utf8');
  notices.push(`${name}@${metadata.version}\n${license.replaceAll("\r\n", "\n")}`);
}
await writeFile(new URL('THIRD_PARTY_NOTICES.txt', output), notices.join('\n\n'));
const hashes = {};
for (const name of ['index.html', 'style.css', 'app.js', 'app.js.LEGAL.txt', 'THIRD_PARTY_NOTICES.txt'])
  hashes[name] = createHash('sha256').update(await readFile(new URL(name, output))).digest('hex');
await writeFile(new URL('assets.json', output), JSON.stringify(hashes, null, 2) + '\n');
