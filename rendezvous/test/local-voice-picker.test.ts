import { readFileSync } from "node:fs";
import { runInNewContext } from "node:vm";
import { describe, expect, it } from "vitest";

const source = readFileSync(new URL("../../sleeptalk/_settings/settings.js", import.meta.url), "utf8");
const picker = source.slice(source.indexOf("function forModel("), source.indexOf("function prettyVoice("));
const wizard = source.slice(source.indexOf("function wizVoiceIds("), source.indexOf("function wizReady("));

class Element {
  children: Element[] = [];
  listeners: Record<string, () => void> = {};
  disabled = false;
  title = "";
  constructor(public tag: string, public className = "", public text = "") {}
  append(...children: Element[]) { this.children.push(...children); }
  setAttribute() {}
  addEventListener(name: string, callback: () => void) { this.listeners[name] = callback; }
}

const models = [
  { id: "kokoro-en", voices: ["af_heart", "af_bella"] },
  { id: "pocket-tts-en", voices: ["pocket-bria", "pocket-loona"] },
  { id: "piper-lessac-medium", voices: ["lessac"] },
];
const voices = [
  { voice_id: "af_heart", name: "Heart", model: "kokoro-en" },
  { voice_id: "af_bella", name: "Bella", model: "kokoro-en" },
  { voice_id: "pocket-bria", name: "Bria", model: "pocket-tts-en" },
  { voice_id: "pocket-loona", name: "Loona", model: "pocket-tts-en" },
  { voice_id: "lessac", name: "Lessac", model: "piper-lessac-medium" },
];
const samples = Object.fromEntries(voices.map(v => [v.voice_id, { model: v.model, url: `/samples/local/${v.voice_id}.mp3` }]));
const service = { id: "local", models: { tts: { models } } };

function harness(custom = false) {
  const localModels = models.map(m => ({...m, voices: [...m.voices]}));
  const localVoices = [...voices];
  if (custom) {
    const voice = {voice_id: "pocket-custom-test", name: "My voice", model: "pocket-tts-en"};
    localModels.find(m => m.id === voice.model)!.voices.push(voice.voice_id);
    localVoices.push(voice);
  }
  const previews: unknown[][] = [];
  const played: string[] = [];
  const selected: string[] = [];
  const state = {
    language: { value: "en-US" },
    speech: { voices_by_service: { local: { voices: localVoices, library_url: "https://example.test/kokoro" } } },
    samples: { local: samples },
  };
  const wiz = { tts: "pocket-tts-en", voice: "af_heart", opts: { voices_by_service: state.speech.voices_by_service } };
  const api = runInNewContext(`${picker}\n${wizard}\n({voiceList, wizVoiceIds})`, {
    state, wiz, wizService: () => service,
    modelById: (_s: unknown, _role: string, id: string) => localModels.find(m => m.id === id),
    el: (tag: string, cls?: string, text?: string) => new Element(tag, cls, text),
    t: (text: string) => text, primary: () => "en", prettyVoice: (id: string) => id,
    icon: () => new Element("svg"), PLAY: "play",
    playUrl: (_button: Element, url: string) => played.push(url),
    preview: (...args: unknown[]) => previews.push(args.slice(1)),
    pocketVoiceUpload: () => new Element("upload"),
  });
  return { api, played, selected, previews };
}

describe("local settings voice samples", () => {
  it.each([false, true])("shows and plays only Pocket voices (live preview allowed: %s)", canPreview => {
    const { api, played, selected } = harness();
    const view = api.voiceList(service, "pocket-tts-en", "pocket-bria", (id: string) => selected.push(id), canPreview);
    const rows = view.children[0].children;
    expect(rows.map((row: Element) => row.children[1].text)).toEqual(["Bria", "Loona"]);
    for (const row of rows) row.children[0].listeners.click();
    expect(played).toEqual(["/samples/local/pocket-bria.mp3", "/samples/local/pocket-loona.mp3"]);
    rows[1].children[2].children[1].listeners.click();
    expect(selected).toEqual(["pocket-loona"]);
    expect(view.children).toHaveLength(2);
    expect(view.children[1].tag).toBe("upload"); // Pocket alone offers custom voices
  });

  it("switching models restores that model's own voice samples", () => {
    const { api, played } = harness();
    for (const [model, expected] of [["kokoro-en", ["Heart", "Bella"]], ["piper-lessac-medium", ["Lessac"]]] as const) {
      const view = api.voiceList(service, model, "", () => {}, false);
      expect(view.children).toHaveLength(1); // no upload control for Kokoro or Piper
      const rows = view.children[0].children;
      expect(rows.map((row: Element) => row.children[1].text)).toEqual(expected);
      for (const row of rows) row.children[0].listeners.click();
    }
    expect(played).toEqual(["/samples/local/af_heart.mp3", "/samples/local/af_bella.mp3", "/samples/local/lessac.mp3"]);
  });

  it("the setup wizard cannot prepend an old Kokoro choice to Pocket's voices", () => {
    expect(harness().api.wizVoiceIds()).toEqual(["pocket-bria", "pocket-loona"]);
  });
});


describe("uploaded Pocket voices", () => {
  it("offers generated previews before switching the profile to local speech", () => {
    const {api, previews} = harness(true);
    const view = api.voiceList(service, "pocket-tts-en", "pocket-bria", () => {}, false);
    const rows = view.children[0].children;
    expect(rows.map((row: Element) => row.children[1].text)).toEqual(["Bria", "Loona", "My voice"]);
    expect(rows[2].children[0].disabled).toBe(false);
    rows[2].children[0].listeners.click();
    expect(previews).toEqual([["pocket-tts-en", "pocket-custom-test", true]]);
    const kokoro = api.voiceList(service, "kokoro-en", "", () => {}, false);
    expect(kokoro.children[0].children.map((row: Element) => row.children[1].text)).not.toContain("My voice");
  });

  it("encodes clipped mono PCM in the required 24 kHz WAV format", () => {
    const code = source.slice(source.indexOf("function pocketWav("), source.indexOf("function rememberPocketVoice("));
    const {pocketWav} = runInNewContext(`${code}; ({pocketWav})`);
    const wav = pocketWav(new Float32Array([-2, 0, 0.5, 2]));
    const view = new DataView(wav.buffer);
    expect(new TextDecoder().decode(wav.subarray(0, 4))).toBe("RIFF");
    expect(view.getUint32(24, true)).toBe(24000);
    expect(view.getUint16(22, true)).toBe(1);
    expect(view.getUint32(40, true)).toBe(8);
    expect([0, 1, 2, 3].map(i => view.getInt16(44 + i * 2, true))).toEqual([-32767, 0, 16384, 32767]);
  });
});
