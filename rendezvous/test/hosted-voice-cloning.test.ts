import { readFileSync } from "node:fs";
import { runInNewContext } from "node:vm";
import { describe, expect, it } from "vitest";

const source = readFileSync(new URL("../../sleeptalk/_settings/settings.js", import.meta.url), "utf8");
const panel = source.slice(source.indexOf("function hostedVoicePanel("), source.indexOf("// ---------- Pages ----------"));
const wav = source.slice(source.indexOf("function pocketWav("), source.indexOf("function rememberPocketVoice("));

class Element {
  children: Element[] = [];
  listeners: Record<string, () => Promise<void> | void> = {};
  disabled = false;
  value = "";
  type = "";
  checked = false;
  files: unknown[] = [];
  textContent: string;
  sel!: Element;
  constructor(public tag: string, public className = "", text = "") { this.textContent = text; }
  append(...children: Element[]) { this.children.push(...children); }
  replaceChildren(...children: Element[]) { this.children = children; }
  setAttribute() {}
  addEventListener(name: string, callback: () => void) { this.listeners[name] = callback; }
}
const flatten = (el: Element): Element[] => [el, ...el.children.flatMap(flatten)];

function harness(provider = "elevenlabs", keySaved = true, pending = false) {
  const calls: { path: string; body: any }[] = [];
  const selected: string[] = [];
  const previews: unknown[][] = [];
  const t = (s: string, vars: Record<string, unknown> = {}) => s.replace(/\{(\w+)\}/g, (_, k) => String(vars[k]));
  const el = (tag: string, cls?: string, text?: string) => new Element(tag, cls, text);
  let duration = 13;
  const ready = { voice_id: "saved-clone", name: "Saved voice", available: true, requires_verification: false };
  const result = {voice_id: "new-clone", name: "New voice", available: !pending, requires_verification: pending};
  const ctx = runInNewContext(`${wav}\n${panel}; ({hostedVoicePanel})`, {
    state: {language: {value: "en-US", options: ["en-US", "en-GB", "fr-FR"]}},
    el, t, primary: (s: string) => s.split("-")[0], languageWord: (s: string) => s,
    input: () => el("input"), field: (label: string, ...children: Element[]) => { const f = el("div", "field", label); f.append(...children); return f; },
    selectBox: (items: {value: string}[], value: string) => {
      expect(items.map(v => v.value)).toEqual(["en", "fr"]);
      const box = el("div"); box.sel = el("select"); box.sel.value = value; box.append(box.sel); return box;
    },
    api: async (path: string, body: any) => {
      calls.push({path, body});
      return path.endsWith("/clone") ? body.final ? {voice: result} : {upload_id: "upload-token"}
        : {voices: [ready, {voice_id: "pending", name: "Needs verification", available: false, requires_verification: true}]};
    },
    preview: (...args: unknown[]) => previews.push(args.slice(1)),
    icon: () => el("svg"), PLAY: "play",
    btoa: (s: string) => Buffer.from(s, "binary").toString("base64"),
    AudioContext: class { async decodeAudioData() { return {duration}; } async close() {} },
    OfflineAudioContext: class {
      constructor(_channels: number, public frames: number, public rate: number) {}
      destination = {};
      createBufferSource() { return {connect() {}, start() {}}; }
      async startRendering() { return {getChannelData: () => new Float32Array(this.frames).fill(0.1)}; }
    },
  });
  const view = ctx.hostedVoicePanel({id: provider, key_saved: keySaved}, "model-id", "", (id: string) => selected.push(id));
  return {view, calls, selected, previews, duration: (n: number) => { duration = n; }};
}

describe("hosted voice cloning", () => {
  it("asks users to save the selected provider before uploading", () => {
    const h = harness("cartesia", false);
    expect(h.calls).toHaveLength(0);
    expect(flatten(h.view).some(n => n.type === "file")).toBe(false);
  });

  it("loads account clones, previews the selected model, and disables unverified voices", async () => {
    const h = harness(); await new Promise(resolve => setImmediate(resolve));
    const list = flatten(h.view).find(n => n.className === "voices")!;
    expect(list.children).toHaveLength(2);
    const [ready, pending] = list.children;
    await ready.children[0].listeners.click();
    expect(h.previews).toEqual([["model-id", "saved-clone"]]);
    await ready.children[2].children[0].listeners.click();
    expect(h.selected).toEqual(["saved-clone"]);
    expect(pending.children[0].disabled).toBe(true);
    expect(pending.children[2].children[1].disabled).toBe(true);
  });

  it.each(["cartesia", "elevenlabs"])("uploads bounded chunks to %s only after explicit consent", async provider => {
    const h = harness(provider, true, provider === "elevenlabs"); await new Promise(resolve => setImmediate(resolve));
    const nodes = flatten(h.view);
    const save = nodes.find(n => n.textContent === "Create voice")!;
    const name = nodes.find(n => n.tag === "input" && !n.type)!;
    const file = nodes.find(n => n.type === "file")!;
    const consent = nodes.find(n => n.type === "checkbox")!;
    name.value = "New voice"; file.files = [{size: 1000, arrayBuffer: async () => new ArrayBuffer(1)}];
    await save.listeners.click();
    expect(h.calls.filter(c => c.path.endsWith("/clone"))).toHaveLength(0);
    consent.checked = true;
    await save.listeners.click();
    const uploads = h.calls.filter(c => c.path.endsWith("/clone"));
    expect(uploads.length).toBeGreaterThan(1);
    expect(uploads.every(c => c.body.provider === provider && c.body.consent === true && JSON.stringify(c.body).length < 16384)).toBe(true);
    expect(uploads[0].body.upload_id).toBe("");
    expect(uploads[1].body.upload_id).toBe("upload-token");
    expect(uploads.at(-1)!.body.final).toBe(true);
    const bytes = Buffer.concat(uploads.map(c => Buffer.from(c.body.audio, "base64")));
    expect(bytes.readUInt32LE(24)).toBe(48000);
    expect(bytes.length).toBe(44 + 13 * 48000 * 2);
    expect(h.selected).toEqual([]); // Creation does not switch the profile voice.
    expect(consent.checked).toBe(false);
    expect(save.disabled).toBe(false);
    expect(nodes.some(n => n.textContent.includes(provider === "elevenlabs" ? "Complete verification" : "Voice created. Preview"))).toBe(true);
  });

  it("rejects recordings longer than the provider limit without uploading or trimming", async () => {
    const h = harness("cartesia"); await new Promise(resolve => setImmediate(resolve)); h.duration(61);
    const nodes = flatten(h.view);
    nodes.find(n => n.tag === "input" && !n.type)!.value = "Long";
    nodes.find(n => n.type === "file")!.files = [{size: 1000, arrayBuffer: async () => new ArrayBuffer(1)}];
    nodes.find(n => n.type === "checkbox")!.checked = true;
    await nodes.find(n => n.textContent === "Create voice")!.listeners.click();
    expect(h.calls.filter(c => c.path.endsWith("/clone"))).toHaveLength(0);
    expect(nodes.some(n => n.textContent.includes("between 10 and 60 seconds"))).toBe(true);
  });
});
