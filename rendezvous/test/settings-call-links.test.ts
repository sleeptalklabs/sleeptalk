import { readFileSync } from "node:fs";
import { runInNewContext } from "node:vm";
import { expect, it, vi } from "vitest";

const source = readFileSync(new URL("../../sleeptalk/_settings/settings.js", import.meta.url), "utf8");
// The clipboard helper the controls lean on lives higher up in the file.
const clipboard = source.slice(source.indexOf("// Copies a link that is still on its way."), source.indexOf("// A link to copy: one tinted field"));
const controls = source.slice(source.indexOf("// A call this profile still has running"), source.indexOf("// Sidebar: start a call"));

class Element {
  children: Element[] = [];
  parent?: Element;
  handlers: Record<string, (() => unknown)[]> = {};
  attributes: Record<string, string> = {};
  disabled = false;
  readOnly = false;
  value = "";
  href = "";
  target = "";
  rel = "";
  selected = false;
  focused = false;
  constructor(public tag: string, public className = "", public textContent = "") {}
  append(...items: Element[]) { for (const item of items) { item.parent = this; this.children.push(item); } }
  replaceChildren(...items: Element[]) { this.children = []; this.append(...items); }
  after(item: Element) { item.parent = this.parent; this.parent!.children.splice(this.parent!.children.indexOf(this) + 1, 0, item); }
  remove() { this.parent!.children = this.parent!.children.filter(item => item !== this); }
  setAttribute(key: string, value: string) { this.attributes[key] = value; }
  addEventListener(name: string, callback: () => unknown) { (this.handlers[name] ||= []).push(callback); }
  async click() { if (!this.disabled) for (const callback of this.handlers.click || []) await callback(); }
  select() { this.selected = true; }
  focus() { this.focused = true; }
  all(): Element[] { return [this, ...this.children.flatMap(child => child.all())]; }
  querySelector(selector: string) { return this.all().slice(1).find(node => `.${node.className}` === selector) ?? null; }
  src = "";
  alt = "";
  classList = { add: (name: string) => { this.className = `${this.className} ${name}`.trim(); } };
}

const receipt = { join_url: "https://call.example/#secret-call-ticket", say: "" };
function setup(host = false) {
  const api = vi.fn().mockResolvedValue(receipt);
  const popup = vi.fn(() => { throw new Error("Popups blocked"); });
  const navigate = vi.fn();
  const copy = vi.fn().mockResolvedValue(undefined);
  const state = { surface: {type: "web"}, profile: "local", path: "/profiles/local.toml" };
  const ui = runInNewContext(`${clipboard}; ${controls}; ({startCallControls})`, {
    state, api, HOST: host, t: (text: string) => text,
    el: (tag: string, cls?: string, text?: string) => new Element(tag, cls, text),
    input: () => new Element("input"),
    navigator: {clipboard: {writeText: copy}}, window: {open: popup}, location: {assign: navigate},
    setTimeout, encodeURIComponent,
  }) as {
    startCallControls(className: string, options?: { open?: boolean }): {
      button: Element;
      out: Element;
    };
  };
  return { ...ui, api, popup, navigate, copy, state };
}
function find(out: Element, text: string) {
  const found = out.all().find(node => node.textContent === text);
  expect(found, text).toBeDefined();
  return found!;
}
const settle = async () => { await Promise.resolve(); await Promise.resolve(); };

it("opens a delayed local call directly in this tab without attempting a popup", async () => {
  const app = setup();
  let resolve!: (value: typeof receipt) => void;
  app.api.mockImplementation(() => new Promise(done => { resolve = done; }));
  const {button, out} = app.startCallControls("btn");
  await button.click();
  const pending = find(out, "Open call").click();
  expect(button.disabled).toBe(true);
  expect(out.all().some(node => node.tag === "input")).toBe(false);
  expect(app.navigate).not.toHaveBeenCalled();
  resolve(receipt); await pending;
  expect(app.api).toHaveBeenCalledWith("/api/call", {here: true});
  expect(app.popup).not.toHaveBeenCalled();
  expect(app.navigate).toHaveBeenCalledExactlyOnceWith(receipt.join_url);
  expect(app.copy).not.toHaveBeenCalled();
  expect(find(out, "Open call").href).toBe(receipt.join_url);
  expect(find(out, "Open call").target).toBe("");
  expect(find(out, "Open in new tab").target).toBe("_blank");
  expect(find(out, "Open in new tab").rel).toContain("noopener");
  expect(out.all().find(node => node.tag === "input")?.value).toBe(receipt.join_url);
  await find(out, "Copy link").click();
  expect(app.copy).toHaveBeenCalledWith(receipt.join_url);
  find(out, "Copied");
  await button.click();
  expect(app.api).toHaveBeenCalledTimes(1);
  find(app.startCallControls("btn").out, "Open call");
  expect(app.api).toHaveBeenCalledTimes(1);
});

it("selects the visible URL when clipboard access is denied", async () => {
  const app = setup();
  app.copy.mockRejectedValue(new Error("NotAllowedError"));
  const {button, out} = app.startCallControls("btn");
  await button.click(); await find(out, "Link for another device").click();
  expect(app.api).toHaveBeenCalledWith("/api/call", {here: false});
  expect(app.navigate).not.toHaveBeenCalled();
  await find(out, "Copy link").click();
  const link = out.all().find(node => node.tag === "input")!;
  expect(link.readOnly && link.selected && link.focused).toBe(true);
  find(out, "Select and copy the link above.");
});

it("shows a link for another device as a QR code and says when it was copied", async () => {
  const app = setup();
  app.api.mockResolvedValue({ ...receipt, qr_svg: "<svg/>" });
  const {button, out} = app.startCallControls("btn");
  await button.click(); await find(out, "Link for another device").click(); await settle();
  const qr = out.all().find(node => node.className === "call-qr")!;
  expect(qr.children[0].src).toBe("data:image/svg+xml;charset=utf-8,%3Csvg%2F%3E");
  expect(qr.children[0].alt).toBe("QR code for the call link");
  find(out, "Scan with your phone's camera.");
  // The auto-copy, then a click on Copy: a "Link copied" bubble each time.
  await settle(); await settle();
  expect(app.copy).toHaveBeenCalledWith(receipt.join_url);
  expect(find(out, "Link copied").className).toBe("tip");
  await find(out, "Copied").click();
  expect(out.all().filter(node => node.textContent === "Link copied")).toHaveLength(1);
});

it("opens straight onto the device choice after a new profile, starting nothing yet", async () => {
  const app = setup();
  const {button, out} = app.startCallControls("btn", {open: true});
  expect((button as Element & {hidden?: boolean}).hidden).toBe(true);
  expect(app.api).not.toHaveBeenCalled();
  await find(out, "Link for another device").click(); await settle();
  expect(app.api).toHaveBeenCalledWith("/api/call", {here: false});
  find(out, "Open call");
});

it("never shows a QR code for a link on this device", async () => {
  const app = setup();
  app.api.mockResolvedValue({ ...receipt, qr_svg: "<svg/>" });
  const {button, out} = app.startCallControls("btn");
  await button.click(); await find(out, "Open call").click(); await settle();
  expect(out.all().some(node => node.className === "call-qr")).toBe(false);
});

it("offers the same explicit link on hosted phone settings without navigating away", async () => {
  const app = setup(true);
  const {button, out} = app.startCallControls("btn");
  await button.click(); await settle();
  find(out, "Open call");
  expect(app.api).toHaveBeenCalledWith("/api/call", {here: false});
  expect(app.popup).not.toHaveBeenCalled();
  expect(app.navigate).not.toHaveBeenCalled();
});

it("replaces a call only after the explicit restart action and keeps profiles separate", async () => {
  const app = setup();
  const {button, out} = app.startCallControls("btn");
  await button.click(); await find(out, "Open call").click();
  const next = {...receipt, join_url: "https://call.example/#next-ticket"};
  app.api.mockResolvedValue(next);
  await find(out, "End it and start a new one").click(); await settle();
  expect(app.api).toHaveBeenLastCalledWith("/api/call", {here: true, replace: true});
  expect(find(out, "Open call").href).toBe(next.join_url);
  app.state.profile = "other"; app.state.path = "/profiles/other.toml";
  expect(app.startCallControls("btn").out.all().some((node: Element) => node.tag === "a")).toBe(false);
});

it("retains the explicit replacement choice for an already running call", async () => {
  const app = setup();
  app.api.mockRejectedValueOnce(Object.assign(new Error("Busy"), {code: "call_running"}));
  const {button, out} = app.startCallControls("btn");
  await button.click(); await find(out, "Open call").click();
  expect(app.api).toHaveBeenCalledTimes(1);
  expect(app.navigate).not.toHaveBeenCalled();
  await find(out, "End it and start a new one").click(); await settle();
  expect(find(out, "Open call").href).toBe(receipt.join_url);
  expect(app.api).toHaveBeenLastCalledWith("/api/call", {here: true, replace: true});
});


it("opens the call with one click after creating a profile", async () => {
  const app = setup();
  const {out} = app.startCallControls("btn", {open: true});
  expect(app.api).not.toHaveBeenCalled();
  await find(out, "Open call").click();
  expect(app.api).toHaveBeenCalledExactlyOnceWith("/api/call", {here: true});
  expect(app.navigate).toHaveBeenCalledExactlyOnceWith(receipt.join_url);
  expect(app.copy).not.toHaveBeenCalled();
});

it("stays on setup when opening a call fails and lets the user retry", async () => {
  const app = setup();
  app.api.mockRejectedValueOnce(new Error("Call could not start"));
  const {button, out} = app.startCallControls("btn");
  await button.click(); await find(out, "Open call").click();
  find(out, "Call could not start");
  expect(app.navigate).not.toHaveBeenCalled();
  expect(button.disabled).toBe(false);
  await button.click(); await find(out, "Open call").click();
  expect(app.navigate).toHaveBeenCalledExactlyOnceWith(receipt.join_url);
});
