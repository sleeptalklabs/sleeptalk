import { readFileSync } from "node:fs";
import { runInNewContext } from "node:vm";
import { describe, expect, it } from "vitest";

const source = readFileSync(new URL("../../sleeptalk/_settings/settings.js", import.meta.url), "utf8");
const helpers = source.slice(source.indexOf("function modelLabel("), source.indexOf("// Venice labels each model"));
const setup = source.slice(source.indexOf("const localSetupRequests"), source.indexOf("// Download what a local choice needs"));
const translate = (text: string, vars: Record<string, string> = {}) => text.replace(/\{(\w+)\}/g, (m, k) => vars[k] ?? m);

class Element {
  children: Element[] = [];
  style = {};
  constructor(public textContent = "") {}
  append(...children: Element[]) { this.children.push(...children); }
  replaceChildren(...children: Element[]) { this.children = children; }
}

const piper = { id: "piper", label: "Piper", ready: false, download: { bytes: 21e6, approximate: true, ready: false } };
const kokoro = { id: "kokoro", label: "Kokoro", ready: true, download: { bytes: 350e6, approximate: true, ready: true } };

describe("local model download disclosure", () => {
  it("labels every candidate and totals only models missing on the host", () => {
    const api = runInNewContext(`${helpers}; ({modelChoiceLabel, localActionLabel})`, { t: translate });
    expect(api.modelChoiceLabel(piper)).toBe("Piper · ≈21 MB download");
    expect(api.modelChoiceLabel(kokoro)).toBe("Kokoro · Downloaded");
    expect(api.modelChoiceLabel({label: "Cloud"})).toBe("Cloud");
    expect(api.localActionLabel({models: [piper, kokoro]}, "Save", "Download and use")).toBe("Download and use — ≈21 MB");
    expect(api.localActionLabel({models: [kokoro]}, "Save", "Download and use")).toBe("Save");
    expect(api.modelChoiceLabel({label: "Large model", download: {bytes: 1123947975, ready: false, approximate: false}})).toBe("Large model · 1.12 GB download");
  });

  it("ignores stale readiness replies after a different model is selected", async () => {
    const pending: ((value: unknown) => void)[] = [];
    const paths: string[] = [];
    const callbacks: unknown[][] = [];
    const api = runInNewContext(`${helpers}\n${setup}; ({localSetup})`, {
      t: translate,
      api: (path: string) => { paths.push(path); return new Promise(resolve => pending.push(resolve)); },
      el: (_tag: string, _class: string, text: string) => new Element(text),
      chip: (text: string) => new Element(text),
    });
    const box = new Element();
    const first = api.localSetup(box, {tts: "piper"}, (...args: unknown[]) => callbacks.push(args));
    const second = api.localSetup(box, {tts: "kokoro"}, (...args: unknown[]) => callbacks.push(args));
    const latest = {runtime: true, models: [kokoro]};
    pending[1](latest); await second;
    pending[0]({runtime: true, models: [piper]}); await first;
    expect(callbacks).toEqual([[false], [false], [true, latest]]);
    expect(box.children[0].children[0].textContent).toBe("Kokoro ready");
    expect(paths).toEqual(["/api/speech/local/status", "/api/speech/local/status"]);
  });
});
