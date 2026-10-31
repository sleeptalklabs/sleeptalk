// Synthetic caller: opens a Sleeptalk web call in Chromium, replaces the
// microphone with scripted speech (clips/*.wav), and records what the agent
// says back: its audio level every 20 ms, a recording, and the transcript /
// agent-state messages the host sends over the call's data channel.
//
//   node caller.mjs <join_url> <out_dir> [scenario,...]
//
// Scenarios run in order on one call (the agent keeps its conversation).
// Playwright comes from remote-entry-web's dev dependencies (npm ci there).
const { chromium } = await import(new URL('../../remote-entry-web/node_modules/playwright/index.mjs', import.meta.url).href);
import { readFile, writeFile, mkdir } from 'node:fs/promises';
import path from 'node:path';

const [joinUrl, outDir, only] = process.argv.slice(2);
// The clips folder make_clips.py wrote (SYNTH_CLIPS).
const CLIPS = process.env.SYNTH_CLIPS;
if (!CLIPS) { console.error('Set SYNTH_CLIPS to a folder make_clips.py wrote.'); process.exit(2); }
const manifest = JSON.parse(await readFile(path.join(CLIPS, 'manifest.json'), 'utf8'));
await mkdir(outDir, { recursive: true });

const INIT = () => {
  const qa = (window.__qa = { events: [], display: [], levels: [], chunks: [] });
  const now = () => Math.round(performance.now());
  let ctx, dest;
  const ensure = () => {
    if (!ctx) { ctx = new AudioContext({ sampleRate: 48000 }); dest = ctx.createMediaStreamDestination(); }
    return ctx;
  };
  navigator.mediaDevices.getUserMedia = async () => { ensure(); await ctx.resume(); qa.events.push({ t: now(), e: 'mic_granted' }); return dest.stream; };
  qa.play = async (url) => {
    ensure();
    const audio = await ctx.decodeAudioData(await (await fetch(url)).arrayBuffer());
    const src = ctx.createBufferSource(); src.buffer = audio; src.connect(dest);
    qa.events.push({ t: now(), e: 'user_start', clip: url.split('/').pop() });
    src.start();
    await new Promise((r) => (src.onended = r));
    qa.events.push({ t: now(), e: 'user_end', clip: url.split('/').pop() });
  };
  const Orig = window.RTCPeerConnection;
  function Patched(...args) {
    const pc = new Orig(...args);
    pc.addEventListener('track', (ev) => {
      ensure();
      const stream = new MediaStream([ev.track]);
      const analyser = ctx.createAnalyser(); analyser.fftSize = 1024;
      ctx.createMediaStreamSource(stream).connect(analyser);
      const buf = new Float32Array(analyser.fftSize);
      setInterval(() => {
        analyser.getFloatTimeDomainData(buf);
        let s = 0; for (const v of buf) s += v * v;
        qa.levels.push([now(), Math.sqrt(s / buf.length)]);
      }, 20);
      try { const rec = new MediaRecorder(stream); rec.ondataavailable = (e) => qa.chunks.push(e.data); rec.start(1000); qa.recorder = rec; } catch (e) { qa.events.push({ t: now(), e: 'recorder_failed', m: String(e) }); }
    });
    const dc = pc.createDataChannel.bind(pc);
    pc.createDataChannel = (label, opts) => {
      const ch = dc(label, opts);
      ch.addEventListener('message', (m) => qa.display.push({ t: now(), data: typeof m.data === 'string' ? m.data : '[binary]' }));
      return ch;
    };
    return pc;
  }
  Patched.prototype = Orig.prototype;
  Object.setPrototypeOf(Patched, Orig);
  window.RTCPeerConnection = Patched;
  qa.now = now;
};

// SYNTH_CHROMIUM: a Chromium to use when Playwright's own download is missing.
const browser = await chromium.launch({ ...(process.env.SYNTH_CHROMIUM ? { executablePath: process.env.SYNTH_CHROMIUM } : {}), args: ['--autoplay-policy=no-user-gesture-required', '--use-fake-ui-for-media-stream'] });
// SYNTH_VIEWPORT=390x844: a phone-sized page (the worker panel lays out differently).
const [viewWidth, viewHeight] = (process.env.SYNTH_VIEWPORT || '').split('x').map(Number);
const viewport = viewWidth && viewHeight ? { viewport: { width: viewWidth, height: viewHeight }, isMobile: true, hasTouch: true } : {};
const page = await (await browser.newContext({ permissions: ['microphone'], ...viewport })).newPage();
const log = [];
const note = (e, extra = {}) => { const row = { wall: Date.now(), e, ...extra }; log.push(row); console.log(JSON.stringify(row)); };
page.on('console', (m) => { if (m.type() === 'error') note('console_error', { text: m.text() }); });
page.on('pageerror', (err) => note('page_error', { text: String(err) }));
await page.addInitScript(INIT);
await page.route('**/__qa/**', async (route) => route.fulfill({ body: await readFile(path.join(CLIPS, route.request().url().split('/').pop())), contentType: 'audio/wav' }));

const t = () => page.evaluate(() => window.__qa.now());
const say = async (clip) => { note('say', { clip, text: manifest[clip].text }); await page.evaluate((u) => window.__qa.play(u), `${new URL(joinUrl).origin}/__qa/${clip}.wav`); };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const LOUD = 0.01;
const overlapDelay = Number(process.env.SYNTH_OVERLAP_DELAY_MS || 4000);
// Bot is "speaking" when any 20 ms level in the last `window` ms is above LOUD.
const botLoudSince = (ms) => page.evaluate(([ms, loud]) => { const q = window.__qa; const n = q.now(); return q.levels.some(([t, v]) => t > n - ms && v > loud); }, [ms, LOUD]);
async function waitBotStart(timeout = 60000) {
  const end = Date.now() + timeout;
  while (Date.now() < end) { if (await botLoudSince(100)) return true; await sleep(50); }
  return false;
}
// Done = bot quiet for `quiet` ms (a gap between sentences is shorter than that).
// The agent's own state from the call's data channel: "idle" once every turn
// and task has settled (a hand-off to the agent keeps it "working").
const agentIdle = () => page.evaluate(() => {
  const q = window.__qa;
  for (let i = q.display.length - 1; i >= 0; i--) {
    try { const env = JSON.parse(q.display[i].data); if (env.type === 'call.agent_state') return env.payload.state === 'idle'; } catch (e) {}
  }
  return true;
});
// Done = the agent reports idle and has been quiet for `quiet` ms. Silence
// alone isn't enough: a hand-off to the agent is quiet for many seconds
// before its answer, and the next scenario then landed inside it.
async function waitBotDone(quiet = 2000, timeout = 180000) {
  const end = Date.now() + timeout;
  while (Date.now() < end) {
    if (!(await botLoudSince(quiet)) && (await agentIdle())) return true;
    await sleep(200);
  }
  return false;
}
async function mark(name) { note('mark', { name, t: await t() }); }

const SCENARIOS = {
  // Speaks the instant the call is live: are the first words heard?
  async first_words() { await say('first_words'); await waitBotStart(); await waitBotDone(); },
  async short() { await say('short'); await waitBotStart(); await waitBotDone(); },
  async stop_resume() {
    await say('long'); await waitBotStart(); await sleep(150);
    await mark('stop_at'); await page.click('#stop'); await sleep(2000);
    await say('short'); await waitBotStart(20000); await waitBotDone();
  },
  // The cut-off check: a long answer, caller silent throughout.
  async long_silent() { await say('long'); await waitBotStart(); await waitBotDone(5000, 180000); },
  // Barge in 4 s into a long answer; how fast does the agent stop?
  async barge() {
    await say('long'); await waitBotStart();
    await sleep(overlapDelay); await mark('barge_at');
    await say('barge'); await mark('barge_said');
    await waitBotStart(); await waitBotDone();
  },
  async resume() { await say('resume'); await waitBotStart(); await waitBotDone(5000, 180000); },
  // One thought with a 700 ms pause inside it.
  async split() { await say('split_a'); await sleep(700); await say('split_b'); await waitBotStart(); await waitBotDone(); },
  // "Mm-hmm" during a long answer should not end it.
  async backchannel() {
    await say('long'); await waitBotStart();
    await sleep(overlapDelay); await mark('backchannel_at'); await say('backchannel');
    await waitBotDone(5000, 180000);
  },
  // Failure injection (fault_proxy.py + fault_agent.py): each step sets a
  // fault, asks a short question, and notes whether anything was heard.
  async faults() {
    const proxy = process.env.SYNTH_FAULT_PROXY;
    const plan = JSON.parse(process.env.SYNTH_FAULT_PLAN || '[]');
    const set = (body) => fetch(`${proxy}/_fault`, { method: 'POST', body: JSON.stringify(body) });
    for (const step of plan) {
      await set({ stt: 'pass', tts: 'pass', agent: 'ok', ...step.set });
      await mark(`fault_start:${step.name}`);
      const before = await page.evaluate(() => window.__qa.display.length);
      await say(step.clip || 'short');
      const saidAt = await t();
      const heard = await waitBotStart(step.wait || 45000);
      const heardAt = await t();
      await waitBotDone(2000, step.done || 150000);
      const lines = await page.evaluate((n) => {
        const out = [];
        for (const m of window.__qa.display.slice(n)) {
          try { const env = JSON.parse(m.data); if (env.type === 'call.presentation') out.splice(0, out.length, ...(env.payload.lines || []).map((l) => `${l.kind || l.speaker || '?'}: ${l.text || ''}`)); } catch (e) {}
        }
        return out.slice(-4);
      }, before);
      note('fault_result', { name: step.name, heard, firstAudioMs: heard ? heardAt - saidAt : null, lines });
      await set({ stt: 'pass', tts: 'pass', agent: 'ok' });
      await sleep(1500);
    }
  },
  // A scripted conversation (SYNTH_SCRIPT, JSON): each step says a clip,
  // then waits for the reply (`done`, ms) or only `after` ms (talking over
  // work in progress), and notes what the page showed. analyze_tools.py
  // scores it.
  async script() {
    const steps = JSON.parse(process.env.SYNTH_SCRIPT || '[]');
    for (const step of steps) {
      await mark(`step_start:${step.name}`);
      const before = await page.evaluate(() => window.__qa.display.length);
      // A step with no clip says nothing: it waits for the bot to speak on
      // its own (a background job's announcement) within `wait` ms.
      if (step.clip) await say(step.clip);
      // `click`: tap an element first (the worker pill on a phone-sized page).
      if (step.click) await page.click(step.click, { timeout: 5000 }).catch((err) => note('click_error', { selector: step.click, err: String(err) }));
      const saidAt = await t();
      let heard = null;
      if (step.after != null) {
        await sleep(step.after);
      } else {
        heard = await waitBotStart(step.wait || 90000) ? (await t()) - saidAt : null;
        await waitBotDone(step.quiet || 3000, step.done || 240000);
      }
      const shown = await page.evaluate((n) => {
        const lines = []; const other = [];
        for (const m of window.__qa.display.slice(n)) {
          try {
            const env = JSON.parse(m.data);
            if (env.type === 'call.presentation') {
              for (const l of env.payload.lines || []) { const x = `${l.kind || '?'}: ${l.text || ''}`; if (!lines.includes(x)) lines.push(x); }
            } else if (env.type !== 'call.agent_state') {
              const x = `${env.type} ${JSON.stringify(env.payload).slice(0, 300)}`; if (!other.includes(x)) other.push(x);
            }
          } catch (e) {}
        }
        return { lines, other };
      }, before);
      note('step_result', { name: step.name, heardMs: heard, ...shown });
      // SYNTH_SHOTS: a picture of the page after each step (worker lines, links).
      if (process.env.SYNTH_SHOTS) await page.screenshot({ path: path.join(outDir, `step-${step.name}.png`) }).catch(() => {});
      await mark(`step_end:${step.name}`);
    }
  },
  // A long call (SYNTH_SOAK_MINUTES): ordinary turns in rotation, to see
  // latency or memory drift over time.
  async soak() {
    const minutes = Number(process.env.SYNTH_SOAK_MINUTES || 45);
    const rotation = ['short', 'first_words', 'long', 'split_a', 'resume'];
    const end = Date.now() + minutes * 60000;
    for (let i = 0; Date.now() < end; i++) {
      const clip = rotation[i % rotation.length];
      await mark(`soak:${i}:${clip}`);
      await say(clip);
      if (clip === 'split_a') { await sleep(700); await say('split_b'); }
      const saidAt = await t();
      const heard = await waitBotStart(60000) ? (await t()) - saidAt : null;
      await waitBotDone(3000, 180000);
      note('soak_turn', { i, clip, heardMs: heard });
      await sleep(4000);
    }
  },
  // A turn that needs a tool (oh-my-pi's web search).
  async search() { await say('search'); await waitBotStart(90000); await waitBotDone(5000, 180000); },
};

await page.goto(joinUrl);
await page.waitForSelector('#talk:not([disabled])', { timeout: 30000 });
await page.click('#talk');
await page.waitForFunction(() => document.getElementById('diag-page')?.textContent === 'live', null, { timeout: 45000 });
note('live', { t: await t() });
await sleep(1500);
for (const name of only ? only.split(',') : Object.keys(SCENARIOS).filter((n) => !['faults', 'script', 'soak', 'stop_resume'].includes(n))) {
  await mark(`start:${name}`);
  try { await SCENARIOS[name](); } catch (err) { note('scenario_error', { name, err: String(err) }); }
  await mark(`end:${name}`);
  await sleep(2000);
}
await page.click('#hangup').catch(() => {});
await sleep(1500);
const qa = await page.evaluate(async () => {
  const q = window.__qa;
  if (q.recorder && q.recorder.state !== 'inactive') { q.recorder.stop(); await new Promise((r) => setTimeout(r, 1200)); }
  const blob = new Blob(q.chunks, { type: 'audio/webm' });
  const b64 = await new Promise((r) => { const fr = new FileReader(); fr.onload = () => r(String(fr.result).split(',')[1] || ''); fr.readAsDataURL(blob); });
  return { events: q.events, display: q.display, levels: q.levels, audio: b64 };
});
await writeFile(path.join(outDir, 'bot.webm'), Buffer.from(qa.audio, 'base64'));
delete qa.audio;
await writeFile(path.join(outDir, 'caller.json'), JSON.stringify({ joinUrl, clips: CLIPS, log, ...qa }, null, 1));
await browser.close();
console.log('saved', outDir);
