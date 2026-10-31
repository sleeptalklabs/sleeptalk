"use strict";
// On a phone the page is served by the call service, and its requests travel
// to the host through the sealed setup channel (window.sleeptalkHost, set by
// that page). On the host itself they are ordinary same-origin requests.
const HOST = window.sleeptalkHost || null;
const hostFetch = (path, init) => (HOST ? HOST.fetch(path, init) : fetch(path, init));
// An icon or voice sample: a plain URL on the host, a local blob on the phone.
const hostAsset = path => (HOST ? HOST.url(path) : Promise.resolve(path));
let csrf = sessionStorage.getItem("settings_access") || "";
let state = null;
let openService = "";            // the speech card that is opened
const main = document.getElementById("page");
// Page language: follows the profile's speech language unless the viewer picked another.
const UI_LANGS = ["en", "es", "fr", "de", "it", "pt", "nl", "ja", "ko", "zh", "hi"];
let uiLang = "en";
let strings = {};
const loadedStrings = {};
function t(text, vars) {
  const s = strings[text] || text;
  return vars ? s.replace(/\{(\w+)\}/g, (m, k) => (k in vars ? vars[k] : m)) : s;
}
function pageOverride() { try { return localStorage.getItem("settings_page_lang") || ""; } catch { return ""; } }
function setPageOverride(v) { try { if (v) localStorage.setItem("settings_page_lang", v); else localStorage.removeItem("settings_page_lang"); } catch { /* per-viewer only */ } }
const primary = tag => (tag || "en").toLowerCase().split("-")[0];
async function useLocale(lang) {
  lang = UI_LANGS.includes(lang) ? lang : "en";
  if (lang !== "en" && !loadedStrings[lang]) {
    try { const r = await hostFetch(`/i18n/${lang}.json`, {cache: "no-store"}); loadedStrings[lang] = r.ok ? await r.json() : {}; }
    catch { loadedStrings[lang] = {}; }
  }
  uiLang = lang; strings = lang === "en" ? {} : loadedStrings[lang];
  document.documentElement.lang = lang;
}
const toastEl = document.getElementById("toast");
const el = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text !== undefined) n.textContent = text; return n; };
const PAGES = [
  ["agent", "Agent"], ["speech", "Speech service"], ["voice", "Voice"], ["language", "Language"],
  ["delivery", "Call delivery"], ["discord", "Discord setup"], ["behaviour", "Behavior"], ["keys", "Keys"],
];
const pageLabel = id => t(PAGES.find(([p]) => p === id)[1]);
// One line icon per page in the menu (drawn here, 24-unit grid, stroked).
const NAV_ICONS = {
  agent: "M12 3.5l1.7 4.8 4.8 1.7-4.8 1.7L12 16.5l-1.7-4.8L5.5 10l4.8-1.7zM18.5 15.5l.7 1.8 1.8.7-1.8.7-.7 1.8-.7-1.8-1.8-.7 1.8-.7z",
  speech: "M4 10v4M8 7v10M12 4v16M16 7v10M20 10v4",
  voice: "M12 3a3 3 0 0 1 3 3v6a3 3 0 0 1-6 0V6a3 3 0 0 1 3-3zM5.5 11a6.5 6.5 0 0 0 13 0M12 17.5V21",
  language: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zM3.5 12h17M12 3c2.4 2.5 3.6 5.5 3.6 9s-1.2 6.5-3.6 9c-2.4-2.5-3.6-5.5-3.6-9S9.6 5.5 12 3z",
  delivery: "M6 4h3l1.8 4.5-2.3 1.4a10.5 10.5 0 0 0 5.6 5.6l1.4-2.3L20 15v3a2 2 0 0 1-2 2A15 15 0 0 1 4 6a2 2 0 0 1 2-2z",
  discord: "M4.5 5.5h15v10h-9l-4.5 3.5v-3.5h-1.5z",
  behaviour: "M4 7h9M17 7h3M4 17h3M11 17h9M15 5v4M9 15v4",
  keys: "M15 3.5a5.5 5.5 0 1 1-4.3 8.9L4 19.1V21h2.5v-1.8h2v-2h2l1.4-1.4A5.5 5.5 0 0 1 15 3.5zM16.5 7.5v.01",
};
// A whole card row is its button: clicking anywhere on it (not on a control,
// and not inside the part it opened) does what its Select or Settings does.
document.addEventListener("click", e => {
  const card = e.target.closest(".pcard");
  if (!card || String(getSelection()) ) return;
  if (e.target.closest("button, a, input, select, textarea, label, summary, .detail, .flow")) return;
  const action = card.querySelector(".pcard-top .pcard-action .btn:not(:disabled)");
  if (action) action.click();
});

// Copies a link that is still on its way. Browsers only let a page write the
// clipboard during a click, and Safari counts the wait for the call to start
// as after it; a ClipboardItem made in the click and handed the link later
// is allowed. Resolves true once copied, false if the browser refused.
function copyWhenReady(urlPromise) {
  try {
    if (window.ClipboardItem && navigator.clipboard && navigator.clipboard.write) {
      const blob = urlPromise.then(url => new Blob([url], {type: "text/plain"}));
      return navigator.clipboard.write([new ClipboardItem({"text/plain": blob})]).then(() => true, () => false);
    }
  } catch { /* fall through to a plain write */ }
  return urlPromise.then(url => navigator.clipboard.writeText(url)).then(() => true, () => false);
}

// A small "Copied"-style bubble over a control, gone after a moment. The
// anchor is a .tip-anchor around the control.
function flashTip(anchor, text) {
  anchor.querySelector(".tip")?.remove();
  const tip = el("span", "tip", text); tip.setAttribute("role", "status");
  anchor.append(tip);
  setTimeout(() => { tip.classList.add("out"); setTimeout(() => tip.remove(), 220); }, 2200);
}

// A link to copy: one tinted field, the link on a single line (its whole text
// selects on click), and a copy icon inside it on the right.
const COPY_ICON = "M9 9h10v10H9zM15 9V5H5v10h4";
const CHECK_ICON = "M5 12.5l4.5 4.5L19 7.5";
function copyField(url) {
  const field = el("div", "copy-field");
  const text = el("span", "copy-url", url); text.title = url;
  const button = el("button", "copy-btn"); button.type = "button";
  button.setAttribute("aria-label", t("Copy link")); button.title = t("Copy link");
  const show = path => { const svg = icon(path); svg.classList.add("stroke-icon"); button.replaceChildren(svg); };
  show(COPY_ICON);
  // The check and green edge, for a click here or a copy made for you.
  field.flashCopied = () => {
    show(CHECK_ICON); field.classList.add("copied");
    setTimeout(() => { show(COPY_ICON); field.classList.remove("copied"); }, 2400);
  };
  button.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(url);
      field.flashCopied(); toast(t("Copied"));
    } catch {
      const range = document.createRange(); range.selectNodeContents(text);
      const sel = getSelection(); sel.removeAllRanges(); sel.addRange(range);
    }
  });
  field.append(text, button);
  return field;
}

function navIcon(id) {
  const svg = icon(NAV_ICONS[id] || "");
  svg.classList.add("nav-icon");
  return svg;
}

let toastTimer = 0;
function toast(text, error) {
  toastEl.textContent = text; toastEl.className = "toast" + (error ? " error" : ""); toastEl.hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => { toastEl.hidden = true; }, error ? 7000 : 3200);
}
async function api(path, body) {
  const r = await hostFetch(path, {method: body === undefined ? "GET" : "POST", cache: "no-store",
    headers: {"Content-Type": "application/json", "X-Settings-CSRF": csrf}, body: body === undefined ? undefined : JSON.stringify(body)});
  const text = await r.text(); let data; try { data = JSON.parse(text); } catch { data = {message: text}; }
  if (!r.ok) { const known = KEY_FAILURES[data.code] || (typeof NEW_FAILURES !== "undefined" && NEW_FAILURES[data.code]); const e = new Error((known && t(known)) || data.message || (data.code ? `${t("That didn't work.")} (${data.code})` : t("This settings session has ended. Run `sleeptalk settings --open` again."))); e.code = data.code; throw e; }
  return data;
}
async function set(setting, value, label) {
  try { await api("/api/set", {setting, value}); toast(t("{label} saved.", {label})); await reload(); }
  catch (e) { toast(e.message, true); }
}
async function reload() {
  state = await api("/api/state");
  await useLocale(pageOverride() || (state.profile ? primary(state.language.value) : primary(navigator.language || "en")));
  renderChrome(); renderPage();
}

// ---------- helpers ----------
// Each language named in itself, so anyone can find theirs whatever the page shows.
const LANG_NAMES = {"en-US": "English (US)", "en-GB": "English (UK)", "es-ES": "Español (España)", "es-MX": "Español (México)", "fr-FR": "Français", "de-DE": "Deutsch",
  "it-IT": "Italiano", "pt-BR": "Português (Brasil)", "nl-NL": "Nederlands", "ja-JP": "日本語", "ko-KR": "한국어", "zh-CN": "中文（普通话）", "hi-IN": "हिन्दी"};
const UI_LANG_NAMES = {en: "English", es: "Español", fr: "Français", de: "Deutsch", it: "Italiano", pt: "Português", nl: "Nederlands", ja: "日本語", ko: "한국어", zh: "中文", hi: "हिन्दी"};
// A language code in the page's language ("Spanish", "espagnol", "スペイン語").
function languageWord(code) {
  try { return new Intl.DisplayNames([uiLang], {type: "language"}).of(code) || code.toUpperCase(); } catch { return code.toUpperCase(); }
}
const perHour = usd => usd === null || usd === undefined ? "" : usd === 0 ? t("free") : t("{price}/hour", {price: `$${usd < 1 ? usd.toFixed(2) : usd.toFixed(usd < 10 ? 1 : 0)}`});
const latency = ms => ms ? `≈${ms} ms` : "—";
const FILLED_LOGOS = new Set(["cartesia"]);
// A brand logo when one ships for this id, else the name's first letter.
function mark(name, id) {
  if (id && state && (state.icons || []).includes(id)) {
    // Logos that are already a filled square fill the mark edge to edge.
    const span = el("span", "mark logo" + (FILLED_LOGOS.has(id) ? " fill" : "")); const img = el("img"); img.alt = ""; hostAsset(`/icons/${id}.svg`).then(u => { img.src = u; }, () => {}); span.append(img); return span;
  }
  return el("span", "mark", (name || "?")[0].toUpperCase());
}
function badge(text, cls) { return el("span", "badge" + (cls ? " " + cls : ""), text); }
function segments(options, current, onPick) {
  const s = el("div", "segmented"); s.setAttribute("role", "group");
  for (const o of options) { const b = el("button", o.value === current ? "on" : "", o.label); b.type = "button"; b.addEventListener("click", () => o.value !== current && onPick(o.value)); s.append(b); }
  return s;
}
const SVG = "http://www.w3.org/2000/svg";
function icon(path, viewBox = "0 0 24 24") {
  const svg = document.createElementNS(SVG, "svg"); svg.setAttribute("viewBox", viewBox); svg.setAttribute("aria-hidden", "true");
  const p = document.createElementNS(SVG, "path"); p.setAttribute("d", path); svg.append(p); return svg;
}
const PLAY = "M6 4l14 8-14 8z";
const GLOBE = "M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20zm6.9 6h-3a15.7 15.7 0 0 0-1.4-3.6A8 8 0 0 1 18.9 8zM12 4c.8 1.2 1.5 2.5 1.9 4h-3.8c.4-1.5 1.1-2.8 1.9-4zM4.3 14a8.2 8.2 0 0 1 0-4h3.4a16.5 16.5 0 0 0 0 4H4.3zm.8 2h3c.3 1.3.8 2.5 1.4 3.6A8 8 0 0 1 5.1 16zm3-8h-3a8 8 0 0 1 4.4-3.6C8.9 5.5 8.4 6.7 8.1 8zM12 20c-.8-1.2-1.5-2.5-1.9-4h3.8c-.4 1.5-1.1 2.8-1.9 4zm2.3-6H9.7a14.7 14.7 0 0 1 0-4h4.6a14.7 14.7 0 0 1 0 4zm.3 5.6c.6-1.1 1.1-2.3 1.4-3.6h3a8 8 0 0 1-4.4 3.6zm1.7-5.6a16.5 16.5 0 0 0 0-4h3.4a8.2 8.2 0 0 1 0 4h-3.4z";
const EYE = "M12 5C6.5 5 2.7 9.1 1.5 12c1.2 2.9 5 7 10.5 7s9.3-4.1 10.5-7C21.3 9.1 17.5 5 12 5zm0 11.5A4.5 4.5 0 1 1 12 7.5a4.5 4.5 0 0 1 0 9zm0-2.5a2 2 0 1 0 0-4 2 2 0 0 0 0 4z";
function field(label, ...children) { const f = el("div", "field"); const l = el("div", "field-label", label); f.append(l, ...children.filter(Boolean)); return f; }
function selectBox(items, value, onChange, labelText) {
  const wrap = el("div", "select"); const sel = el("select"); if (labelText) sel.setAttribute("aria-label", labelText);
  for (const it of items) { const o = el("option", "", it.label); o.value = it.value; if (it.disabled) o.disabled = true; sel.append(o); }
  sel.value = value; if (onChange) sel.addEventListener("change", () => onChange(sel.value)); wrap.append(sel); wrap.sel = sel; return wrap;
}
function input(placeholder) { const i = el("input", "input"); i.placeholder = placeholder; return i; }
function chip(text, cls) { return el("span", "chip" + (cls ? " " + cls : ""), text); }
function pageHead(title, intro) { const h = el("div", "page-head"); h.append(el("h1", "", title)); if (intro) h.append(el("p", "", intro)); return h; }
function modelById(service, role, id) { return (service.models[role].models || []).find(m => m.id === id); }
function modelLabel(m) { return m ? m.label || m.id : ""; }
function downloadSize(bytes, approximate = false) {
  const size = bytes >= 1e9 ? `${(bytes / 1e9).toFixed(2)} GB` : `${Math.ceil(bytes / 1e6)} MB`;
  return (approximate ? "≈" : "") + size;
}
function modelChoiceLabel(m) {
  const info = m.download;
  return modelLabel(m) + (info ? " · " + (info.ready ? t("Downloaded")
    : t("{size} download", {size: downloadSize(info.bytes, info.approximate)})) : "");
}
function pendingDownload(models) {
  const missing = models.filter(m => m.download && !m.download.ready);
  return {bytes: missing.reduce((sum, m) => sum + m.download.bytes, 0),
    approximate: missing.some(m => m.download.approximate)};
}
function localActionLabel(status, readyLabel, downloadLabel) {
  if (!status) return readyLabel;
  const pending = pendingDownload(status.models);
  return pending.bytes ? t("{action} — {size}", {action: downloadLabel, size: downloadSize(pending.bytes, pending.approximate)}) : readyLabel;
}
// Venice labels each model: private (nothing kept, by contract) or anonymized
// (the model's maker gets the content, not who you are).
const PRIVACY_WORD = {private: "Private", anonymized: "Anonymized"};
const PRIVACY_TIP = {
  private: "Private: processed for the request only and not kept afterwards, by contract.",
  anonymized: "Anonymized: passed to the model's maker without your identity. The maker still receives what you say, under its own data policy.",
};
function privacyChip(m) {
  if (!m || !PRIVACY_WORD[m.privacy]) return null;
  const c = chip(t(PRIVACY_WORD[m.privacy]), m.privacy === "private" ? "good" : ""); c.title = t(PRIVACY_TIP[m.privacy]); return c;
}
function hasPrivacy(service) { return ["stt", "tts"].some(role => (service.models[role].models || []).some(m => m.privacy)); }
// The card's one word for the pair in use: both private, both anonymized, or which is which.
function privacySummary(service) {
  const stt = modelById(service, "stt", service.current.stt), tts = modelById(service, "tts", service.current.tts);
  const a = stt && stt.privacy, b = tts && tts.privacy;
  if (!a || !b) return "—";
  if (a === b) return t(PRIVACY_WORD[a]);
  return a === "private" ? t("Listening private") : t("Speaking private");
}
function voiceCount(service) {
  const lists = (service.models.tts.models || []).filter(m => m.usable && Array.isArray(m.voices));
  if (!lists.length) return (state.speech.voices_by_service[service.id] || {voices: []}).voices.length ? t("Full library") : "—";
  const n = new Set(lists.flatMap(m => m.voices)).size;
  return n >= 100 ? `${Math.floor(n / 10) * 10}+` : String(n);
}
function roleSummary(service, role) {
  const models = (service.models[role].models || []).filter(m => m.usable);
  const current = modelById(service, role, service.current[role]) || models[0];
  if (!current) return role === "stt" ? service.listen || "—" : service.speak || "—";
  const label = modelLabel(current) + (role === "stt" && current.transport === "streaming" ? t(", streamed") : "");
  return models.length > 1 ? t("{label} + {n} more", {label, n: models.length - 1}) : label;
}

// One line on what each agent is, for people choosing between them.
const AGENT_INFO = {
  pi: "A small open-source coding agent. It has no web search.",
  hermes: "Nous Research's open-source agent, with memory and skills that carry across sessions.",
  claude_code: "Anthropic's coding agent for the terminal.",
  codex: "OpenAI's coding agent for the terminal.",
  cursor: "Cursor's coding agent, run from its command-line tool.",
  openclaw: "An open-source personal assistant agent. Support is in preview.",
  omp: "Pi with more tools built in, web search among them. Sleeptalk's default.",
  model: "No agent: a model answers directly, with no tools. Needs one OpenRouter, OpenAI, or Anthropic key.",
  own: "For developers. An agent that already speaks ACP connects with its command; any other agent needs a small adapter you write first.",
};
// The service in use, then services with a key already on this computer, then a fixed order.
const SERVICE_ORDER = ["cartesia", "openrouter", "deepgram", "elevenlabs", "venice", "groq", "together", "local", "openai"];
function orderedServices() {
  const rank = s => (s.in_use ? 0 : state.speech.reusable[s.id] ? 1 : 2);
  const pos = s => { const i = SERVICE_ORDER.indexOf(s.id); return i < 0 ? SERVICE_ORDER.length : i; };
  return [...state.speech.services].sort((a, b) => rank(a) - rank(b) || pos(a) - pos(b));
}

// Looking for keys other apps keep is the user's call: off until they
// click, remembered after, and it only notes where a key is.
function keyLookupPanel(lookup, reusable, services, applied) {
  if (!lookup) return el("div");
  const box = el("div", "lookup" + (lookup.allowed ? " on" : ""));
  const turn = allowed => async () => {
    try { const r = await api("/api/keys/lookup", {allowed}); applied(r); }
    catch (e) { toast(e.message, true); }
  };
  if (!lookup.allowed) {
    box.append(el("p", "", t(HOST ? "Already use OpenRouter, Groq, Together, or Venice with another app on your agent's computer? Sleeptalk can look for that key in {places}."
      : "Already use OpenRouter, Groq, Together, or Venice with another app? Sleeptalk can look for that key in {places}.", {places: lookup.places.join(", ")})),
      el("p", "hint", t("It only notes where a key is. Nothing is read, sent, or copied until you choose one.")));
    const go = el("button", "btn ghost", t(HOST ? "Look on your agent's computer" : "Look on this computer")); go.type = "button"; go.addEventListener("click", turn(true));
    box.append(go);
    return box;
  }
  const found = Object.values(reusable || {});
  const line = el("p", "hint", found.length
    ? t("Found on this computer: {keys}. Choosing one checks it with its service and copies it into Sleeptalk's store.", {keys: found.map(k => `${((services || []).find(sv => sv.id === k.provider) || {name: k.provider}).name} (${k.source})`).join(", ")})
    : t("No reusable key found in {places}.", {places: lookup.places.join(", ")}));
  const stop = el("button", "linkbtn", t("Stop looking")); stop.type = "button"; stop.addEventListener("click", turn(false));
  line.append(" ", stop); box.append(line);
  return box;
}

// ---------- Speech service ----------
function serviceCard(s) {
  const opened = openService === s.id;
  const card = el("div", "pcard" + (s.in_use ? " in-use" : "") + (opened ? " opened" : ""));
  const top = el("div", "pcard-top");
  const name = el("div", "pcard-name"); name.append(mark(s.name, s.id), el("span", "", s.name));
  if (s.in_use) name.append(badge(t("In use"), "ink"));
  else if (state.speech.reusable[s.id]) name.append(badge(t("key found on this computer"), "good"));
  if (s.id === "local") name.append(badge(t("no key")));
  if (s.experimental) name.append(badge(t("Experimental")));
  top.append(name);
  const credit = el("div", "credit");
  if (s.free) {
    // The first phrase fits the card; the whole line, card and fine print sit behind the info mark.
    credit.append(el("span", "", t(s.free.split(/[:;]/)[0].trim())));
    // The hover says in plain words how far the allowance goes, then card and fine print.
    const i = el("span", "info", "i"); i.title = [t(s.usage || s.free), t(s.card), t(s.fine_print)].filter(Boolean).join("\n\n");
    i.setAttribute("aria-label", i.title); i.tabIndex = 0; credit.append(i);
  }
  top.append(credit);
  const facts = el("div", "facts");
  const tts = modelById(s, "tts", s.current.tts);
  const rows = [["Speech-to-text", roleSummary(s, "stt")], ["Text-to-speech", roleSummary(s, "tts")], ["Voices", voiceCount(s)], ["Latency", latency(tts && tts.latency_ms)]];
  if (hasPrivacy(s)) { rows.push(["Privacy", privacySummary(s)]); facts.classList.add("five"); }
  for (const [k, v] of rows) {
    const f = el("div", "fact"); f.append(el("span", "k", t(k)), el("span", "v" + (v === "—" ? " none" : ""), v));
    if (k === "Latency") f.title = tts && tts.latency_ms ? t("Time to first audio, measured {date}", {date: s.models.tts.measured_on}) : t("Not measured yet");
    if (k === "Privacy") f.title = [t(PRIVACY_TIP.private), t(PRIVACY_TIP.anonymized)].join("\n\n");
    facts.append(f);
  }
  top.append(facts);
  const action = el("div", "pcard-action");
  const btn = el("button", "btn ghost card", t(opened ? "Close" : s.in_use ? "Settings" : "Select")); btn.type = "button";
  btn.setAttribute("aria-expanded", String(opened));
  btn.addEventListener("click", () => { openService = opened ? "" : s.id; renderPage(); });
  action.append(btn); top.append(action);
  card.append(top);
  if (opened) card.append(serviceDetail(s));
  return card;
}

function serviceDetail(s) {
  const d = el("div", "detail");
  const choice = {stt: s.current.stt, tts: s.current.tts, voice: s.in_use ? state.voice.voice_id : ""};
  const status = el("p", "status");

  const local = s.id === "local";
  if (local) { d.append(el("p", "hint", t("Local speech runs on this computer with no key and no usage costs. It needs its runtime and models here first; switching downloads what's missing."))); }

  // API key
  const reusable = state.speech.reusable[s.id];
  const keyInput = el("input"); keyInput.type = "password"; keyInput.autocomplete = "off"; keyInput.maxLength = 8192; keyInput.placeholder = t("Paste your {name} key", {name: s.name});
  const keyField = el("div", "keyfield"); const eye = el("button", "eye"); eye.type = "button"; eye.title = t("Show what you typed"); eye.setAttribute("aria-label", eye.title); eye.append(icon(EYE));
  eye.addEventListener("click", () => { keyInput.type = keyInput.type === "password" ? "text" : "password"; });
  keyField.append(keyInput, eye);
  const box = el("div");
  let usingFound = false;
  if (s.key_saved) {
    const saved = el("div", "saved"); saved.append(el("span", "dot"), el("span", "", t("Saved in this computer's private store")));
    const replace = el("button", "linkbtn", t("Replace")); replace.type = "button";
    replace.addEventListener("click", () => { box.replaceChildren(keyField, el("p", "hint", t("The new key is checked, stored, and replaces the old one; your models and voice stay."))); keyInput.focus(); });
    saved.append(replace); box.append(saved);
  } else if (reusable) {
    usingFound = true;
    const saved = el("div", "saved"); saved.append(el("span", "dot"), el("span", "", t("Found in {source}", {source: reusable.source})));
    const other = el("button", "linkbtn", t("Use a different key")); other.type = "button";
    other.addEventListener("click", () => { usingFound = false; box.replaceChildren(keyField); keyInput.focus(); });
    saved.append(other); box.append(saved);
  } else {
    box.append(keyField);
  }
  const keyHint = el("p", "hint", reusable && !s.key_saved
    ? t("Checked once with {name} (a short clip and one line, which uses a little credit), then copied into Sleeptalk's store. The original stays where it is.", {name: s.name})
    : t("Checked once with {name} (a short clip and one line, which uses a little credit), then kept in Sleeptalk's store. Never shown again.", {name: s.name}));
  if (s.key_url && !s.key_saved) { keyHint.append(" "); const a = el("a", "", /^[AEIOU]/.test(s.name) ? t("Get an {name} key ↗", {name: s.name}) : t("Get a {name} key ↗", {name: s.name})); a.href = s.key_url; a.target = "_blank"; a.rel = "noopener noreferrer"; keyHint.append(a); }
  if (!local) d.append(field(t("API key"), box, keyHint));
  if (hasPrivacy(s)) d.append(el("p", "hint", t("{name} marks every model Private or Anonymized. Private models keep nothing after each request, by contract. Anonymized models pass what you say to the model's maker without your identity, under the maker's own data policy.", {name: s.name})));

  // Local: the runtime and the models this choice needs, on this computer.
  const localWrap = el("div");
  const showLocal = local ? () => localSetup(localWrap, choice, refreshAction) : () => {};
  let refreshAction = () => {};

  // Models, then the voices that belong to the speaking model.
  // Local saves through its own button: a new local model downloads first.
  const voicesWrap = el("div", "field");
  const saveNow = s.in_use && !local;
  for (const [role, label] of (s.id === "openai" ? [["tts", t("Model")]] : [["stt", t("Speech-to-text model")], ["tts", t("Text-to-speech model")]])) {
    const wrap = selectBox(s.models[role].models.map(m => ({value: m.id, disabled: !m.usable,
      label: `${modelChoiceLabel(m)}${PRIVACY_WORD[m.privacy] ? " · " + t(PRIVACY_WORD[m.privacy]) : ""}${m.recommended ? " " + t("(recommended)") : ""}${m.usable ? "" : " " + t("(can't be used yet)")}`})), choice[role], null, label);
    const sel = wrap.sel;
    const facts = el("div", "chips"); const note = el("p", "hint");
    const block = field(label, wrap, facts, note);
    const describe = () => {
      const m = modelById(s, role, sel.value); facts.replaceChildren(); note.textContent = "";
      if (!m) return;
      const privacy = privacyChip(m); if (privacy) facts.append(privacy);
      if (m.covers_language === false) facts.append(chip(t("No {language}", {language: languageWord(primary(state.language.value))}), "warn"));
      const lang = m.languages ? (m.languages.length > 4 ? t("{n} languages", {n: m.languages.length}) : m.languages.map(languageWord).join(", ")) : "";
      for (const text of [t(m.transport === "streaming" ? "Streams as you speak" : m.transport === "local" ? "On this computer" : "One turn at a time"), perHour(m.usd_per_hour), m.latency_ms ? latency(m.latency_ms) : "", lang]) if (text) facts.append(chip(text));
      note.textContent = m.notes || ""; note.hidden = !m.notes;
    };
    describe();
    sel.addEventListener("change", async () => {
      choice[role] = sel.value;
      if (s.id === "openai") choice.stt = choice.tts = sel.value;
      describe();
      if (role === "tts") { choice.voice = voiceForModel(s, modelById(s, "tts", sel.value), choice.voice); renderVoices(); }
      if (saveNow) await set(`${role}.model`, sel.value, label);
      showLocal();
    });
    d.append(block);
  }

  const renderVoices = () => {
    voicesWrap.replaceChildren(el("div", "field-label", t("Voice")));
    voicesWrap.append(voiceList(s, choice.tts, choice.voice, voice => {
      choice.voice = voice;
      if (saveNow) set("tts.voice_id", voice, t("Voice")); else { renderVoices(); showLocal(); }
    }, s.in_use));
  };
  renderVoices(); d.append(voicesWrap);

  if (local) {
    d.append(field(t("On this computer"), localWrap));
    const actions = el("div", "actions");
    const go = el("button", "btn", s.in_use ? t("Save these choices") : t("Switch to {name}", {name: s.name})); go.type = "button";
    refreshAction = (ready, info) => {
      go.disabled = !ready;
      go.textContent = localActionLabel(info, s.in_use ? t("Save these choices") : t("Switch to {name}", {name: s.name}), t("Download and use"));
    };
    go.addEventListener("click", async () => {
      go.disabled = true;
      if (await saveLocal(choice, status, s.in_use ? t("Local speech saved.") : t("{name} is now this profile's speech service.", {name: s.name}))) { openService = s.id; await reload(); return; }
      go.disabled = false; showLocal();
    });
    actions.append(go, status); d.append(actions);
    showLocal();
  }

  // Switch (a service not in use yet)
  if (!local && (!s.in_use || s.key_saved)) {
    const actions = el("div", "actions");
    const go = el("button", "btn", s.in_use ? t("Save the new key") : t("Switch to {name}", {name: s.name})); go.type = "button";
    if (s.in_use && s.key_saved) go.hidden = true;
    keyInput.addEventListener("input", () => { go.hidden = false; });
    go.addEventListener("click", async () => {
      if (!usingFound && !keyInput.value.trim()) { status.className = "status error"; status.textContent = t("Paste a key first."); keyInput.focus(); return; }
      go.disabled = true; status.className = "status"; status.textContent = t("Checking with {name}: it listens to a short clip and speaks one line (a little credit)…", {name: s.name});
      const body = {provider: s.id, voice_id: choice.voice, stt_model: choice.stt, tts_model: choice.tts};
      if (usingFound) body.reuse = true; else body.value = keyInput.value;
      keyInput.value = "";
      try {
        await api("/api/speech", body); body.value = "";
        toast(t("{name} is now this profile's speech service.", {name: s.name})); openService = s.id; await reload();
      } catch (e) { status.className = "status error"; status.textContent = e.message; go.disabled = false; }
    });
    actions.append(go, status); d.append(actions);
  }
  return d;
}

// Local speech: is the runtime installed, and which models does this choice
// still need? `onReady(ready)` enables the switch button.
const localSetupRequests = new WeakMap();
async function localSetup(box, choice, onReady) {
  const request = {};
  localSetupRequests.set(box, request);
  onReady(false);
  let r;
  try { r = await api("/api/speech/local/status", {stt_model: choice.stt, tts_model: choice.tts, voice_id: choice.voice}); }
  catch (e) { if (localSetupRequests.get(box) === request) box.replaceChildren(el("p", "status error", e.message)); return; }
  if (localSetupRequests.get(box) !== request) return;
  box.replaceChildren();
  const list = el("div", "chips");
  for (const m of r.models) list.append(chip(m.ready ? t("{model} ready", {model: m.label}) : t("{model}: {size} to download", {model: m.label, size: downloadSize(m.download.bytes, m.download.approximate)}), m.ready ? "good" : ""));
  list.style.marginBottom = "8px"; box.append(list);
  const pending = pendingDownload(r.models);
  if (pending.bytes) box.append(el("p", "hint", t("{size} of models will download to the computer running Sleeptalk. Runtime installation is additional.", {size: downloadSize(pending.bytes, pending.approximate)})));
  if (!r.runtime) {
    const out = el("p", "status");
    box.append(el("p", "hint", t("The local speech runtime isn't installed yet. Install for me runs exactly this; nothing needs sudo.")), el("code", "cmd", r.install_command));
    const row = el("div", "row-actions");
    const inst = el("button", "btn small", t("Install for me")); inst.type = "button";
    inst.addEventListener("click", async () => {
      inst.disabled = true; out.className = "status"; out.textContent = t("Installing…");
      try {
        await api("/api/speech/local/install", {stt_model: choice.stt});
        const job = await followLocalJob(out);
        if (job.stage === "done") { localSetup(box, choice, onReady); return; }
        out.className = "status error"; out.textContent = (KEY_FAILURES[job.code] && t(KEY_FAILURES[job.code])) || job.message || t("That didn't work.");
      } catch (e) { out.className = "status error"; out.textContent = e.message; }
      inst.disabled = false;
    });
    const again = el("button", "btn ghost small", t("Check again")); again.type = "button";
    again.addEventListener("click", () => localSetup(box, choice, onReady));
    row.append(inst, again); box.append(row, out);
    return;
  }
  onReady(true, r);
}
// Download what a local choice needs, test a turn, then save it: true once saved.
async function saveLocal(choice, status, saved) {
  status.className = "status"; status.textContent = t("Starting…");
  try {
    await api("/api/speech/local", {stt_model: choice.stt, tts_model: choice.tts, voice_id: choice.voice});
    const job = await followLocalJob(status);
    if (job.stage === "done") { status.textContent = ""; toast(saved); return true; }
    status.className = "status error"; status.textContent = (KEY_FAILURES[job.code] && t(KEY_FAILURES[job.code])) || job.message || t("That didn't work.");
  } catch (e) { status.className = "status error"; status.textContent = e.message; }
  return false;
}
// Poll a local speech job until it finishes, saying where it is.
async function followLocalJob(out) {
  for (;;) {
    await new Promise(done => setTimeout(done, 700));
    const {job} = await api("/api/speech/local/status", {});
    if (!job || job.stage === "done" || job.stage === "failed") return job || {stage: "failed"};
    out.textContent = job.stage === "install" ? t("Installing the local speech runtime… this can take a few minutes.")
      : job.stage === "download" ? t("Downloading {model}… {percent}%", {model: job.label, percent: job.percent})
      : t("Testing a turn on this computer: listening to a short clip, then speaking one line…");
  }
}
// Picks and samples made with another of a service's models stay out:
// Venice's Orpheus "leo" is not its Grok "leo", and must not play that sample.
function forModel(service, model) {
  return v => !v.model || !model || v.model === model.id;
}
// One list of voices for a speaking model. `canPreview`: the profile already
// holds a key for this service, so the page can ask it to speak a line.
function voiceList(service, modelId, current, onUse, canPreview) {
  const wrap = el("div");
  const model = modelById(service, "tts", modelId);
  const live = modelId === "gpt-live-1";
  const pocket = service.id === "local" && modelId === "pocket-tts-en";
  if (pocket) canPreview = true;
  if (live) canPreview = false;
  const curated = (state.speech.voices_by_service[service.id] || {voices: [], library_url: ""});
  const fits = forModel(service, model);
  const picks = curated.voices.filter(fits);
  const names = Object.fromEntries(picks.map(v => [v.voice_id, v]));
  const samples = live ? {} : Object.fromEntries(Object.entries((state.samples || {})[service.id] || {}).filter(([, v]) => fits(v)));
  let ids;
  if (model && Array.isArray(model.voices)) {
    // The curated voices (the ones with samples) first, then the rest of the model's own.
    const own = new Set(model.voices);
    ids = [...picks.map(v => v.voice_id).filter(id => own.has(id)), ...model.voices];
    ids = [...new Set(ids)];
  } else ids = picks.map(v => v.voice_id);
  // Deepgram: the voice names its model, so list only this family's voices when there are any.
  if (model && model.model_is_voice && !Array.isArray(model.voices)) { const own = ids.filter(id => id.startsWith(model.id + "-")); if (own.length) ids = own; }
  // Samples come in the page's language only. In another language than
  // English, list just the voices recorded speaking it, so no English
  // preview slips in; in English, without a key, list the ones with samples.
  const english = primary(state.language.value) === "en";
  if ((!english || !canPreview) && ids.some(id => samples[id])) ids = ids.filter(id => samples[id]);
  else if (!english && !canPreview && !live) ids = [];
  const list = el("div", "voices");
  for (const id of ids.slice(0, 60)) {
    const meta = names[id];
    const row = el("div", "voice" + (id === current ? " on" : ""));
    const play = el("button", "play"); play.type = "button"; play.append(icon(PLAY));
    const sample = samples[id];
    play.title = live ? "Try this voice in a browser call" : t(sample || canPreview ? "Hear this voice" : "Save a key for this service to hear this voice");
    play.setAttribute("aria-label", t("Hear {name}", {name: meta ? meta.name : prettyVoice(id)}));
    play.disabled = !sample && !canPreview;
    // A recorded sample plays at once and needs no key; otherwise the profile's own key speaks a line.
    play.addEventListener("click", () => sample ? playUrl(play, sample.url) : preview(play, model && !model.model_is_voice ? model.id : "", id, pocket));
    const name = el("span", "vname", meta ? meta.name : (sample && sample.name) || prettyVoice(id));
    const tag = el("span", "vtag", meta ? (meta.description || "").replace(/\.$/, "") : ""); if (meta) tag.title = meta.description || "";
    const use = el("button", "use" + (id === current ? " current" : ""), t(id === current ? "In use" : "Use voice")); use.type = "button";
    if (id !== current) use.addEventListener("click", () => onUse(id));
    const right = el("span", "right"); right.append(tag, use);
    row.append(play, name, right); list.append(row);
  }
  if (!ids.length) list.append(el("p", "hint", english ? t("This model takes any voice ID from the provider's library.")
    : service.id === "local" ? t("Local voices speak English only.")
    : t("No voices recorded in {language} for this model yet. Save a key to hear any voice, or paste a voice ID.", {language: languageWord(primary(state.language.value))})));
  wrap.append(list);
  if (pocket) wrap.append(pocketVoiceUpload(() => {
    wrap.replaceChildren(voiceList(service, modelId, current, onUse, canPreview));
  }));
  if (service.id === "local") return wrap;
  if (["elevenlabs", "cartesia"].includes(service.id)) {
    wrap.append(hostedVoicePanel(service, modelId, current, onUse));
  }
  const more = el("div", "voice-more");
  const paste = input(t("Or paste a voice ID and press Enter")); paste.maxLength = 200;
  paste.addEventListener("keydown", e => { if (e.key === "Enter" && paste.value.trim()) onUse(paste.value.trim()); });
  more.append(paste);
  if (curated.library_url) { const a = el("a", "", t("Browse voices ↗")); a.href = curated.library_url; a.target = "_blank"; a.rel = "noopener noreferrer"; more.append(a); }
  wrap.append(more);
  return wrap;
}
function prettyVoice(id) {
  const base = id.replace(/^flux-|^aura-2-|^aura-/, "").replace(/-en$|_v[\d.]+$/, "").replace(/[_-]+/g, " ");
  return base.charAt(0).toUpperCase() + base.slice(1);
}
let audio = null;
async function playUrl(button, url) {
  if (audio) { audio.pause(); audio = null; }
  button.classList.add("busy");
  try { audio = new Audio(await hostAsset(url)); await audio.play(); }
  // Starting another sample stops this one; that interruption isn't a failure.
  catch (e) { if (!e || e.name !== "AbortError") toast(t("That sample couldn't play."), true); }
  finally { button.classList.remove("busy"); }
}
async function preview(button, model, voice, pocket = false) {
  if (audio) { audio.pause(); audio = null; }
  button.classList.add("busy"); button.disabled = true;
  try {
    const r = await hostFetch(pocket ? "/api/speech/local/voices/preview" : "/api/preview", {method: "POST", cache: "no-store", headers: {"Content-Type": "application/json", "X-Settings-CSRF": csrf}, body: JSON.stringify({model, voice_id: voice})});
    if (!r.ok) { const data = await r.json().catch(() => ({})); throw new Error(t(data.code === "busy" ? "One voice at a time." : pocket ? "Preview failed. Install the local runtime and download Kyutai Pocket TTS under Speech service, then try again." : "That voice couldn't speak just now.")); }
    const url = URL.createObjectURL(await r.blob());
    audio = new Audio(url); audio.addEventListener("ended", () => URL.revokeObjectURL(url)); await audio.play();
  } catch (e) { toast(e.message, true); }
  finally { button.classList.remove("busy"); button.disabled = false; }
}

// Custom references are normalized here so neither filenames nor browser codecs
// become dependencies of the model runner. Chunks fit the sealed phone channel.
function pocketWav(samples, rate = 24000) {
  const bytes = new Uint8Array(44 + samples.length * 2);
  const view = new DataView(bytes.buffer);
  const text = (offset, value) => { for (let i = 0; i < value.length; i++) bytes[offset + i] = value.charCodeAt(i); };
  text(0, "RIFF"); view.setUint32(4, bytes.length - 8, true); text(8, "WAVE");
  text(12, "fmt "); view.setUint32(16, 16, true); view.setUint16(20, 1, true);
  view.setUint16(22, 1, true); view.setUint32(24, rate, true); view.setUint32(28, rate * 2, true);
  view.setUint16(32, 2, true); view.setUint16(34, 16, true); text(36, "data"); view.setUint32(40, samples.length * 2, true);
  samples.forEach((sample, i) => view.setInt16(44 + i * 2, Math.round(Math.max(-1, Math.min(1, sample)) * 32767), true));
  return bytes;
}

function rememberPocketVoice(voice) {
  const targets = [state && state.speech, typeof wiz !== "undefined" && wiz && wiz.opts].filter(Boolean);
  for (const target of targets) {
    const model = target.services.find(s => s.id === "local")?.models.tts.models.find(m => m.id === "pocket-tts-en");
    if (model && !model.voices.includes(voice.voice_id)) model.voices.push(voice.voice_id);
    const voices = target.voices_by_service.local.voices;
    if (!voices.some(v => v.voice_id === voice.voice_id)) voices.push(voice);
  }
}

function pocketVoiceUpload(onSaved) {
  const box = el("div", "field");
  box.append(el("div", "field-label", t("Create a Kyutai voice")),
    el("p", "hint", t("Choose a clear recording of one speaker, ideally about 10 seconds (3–30 seconds). WAV, MP3, or M4A. The voice is stored on your Sleeptalk host.")));
  const name = input(t("Voice name")); name.maxLength = 64;
  const file = el("input"); file.type = "file"; file.accept = "audio/*";
  file.setAttribute("aria-label", t("Voice recording"));
  const save = el("button", "btn ghost", t("Save voice")); save.type = "button";
  const status = el("p", "status"); status.setAttribute("role", "status");
  save.addEventListener("click", async () => {
    const clip = file.files[0];
    if (!clip || !name.value.trim()) { status.textContent = t("Choose a recording and give the voice a name."); return; }
    if (clip.size > 20 * 1024 * 1024) { status.textContent = t("Choose a recording smaller than 20 MB."); return; }
    save.disabled = true; file.disabled = true; name.disabled = true;
    status.className = "status"; status.textContent = t("Preparing voice recording…");
    let context;
    try {
      context = new AudioContext();
      const decoded = await context.decodeAudioData(await clip.arrayBuffer());
      if (decoded.duration < 3 || decoded.duration > 30) throw new Error(t("Use a recording between 3 and 30 seconds."));
      const offline = new OfflineAudioContext(1, Math.round(decoded.duration * 24000), 24000);
      const source = offline.createBufferSource(); source.buffer = decoded; source.connect(offline.destination); source.start();
      const rendered = await offline.startRendering();
      const bytes = pocketWav(rendered.getChannelData(0));
      let uploadId = ""; let result;
      for (let offset = 0; offset < bytes.length; offset += 8192) {
        const chunk = bytes.subarray(offset, offset + 8192);
        status.textContent = t("Uploading voice… {percent}%", {percent: Math.round(offset / bytes.length * 100)});
        result = await api("/api/speech/local/voices", {model: "pocket-tts-en", name: name.value.trim(),
          audio: btoa(String.fromCharCode(...chunk)), offset, upload_id: uploadId, final: offset + chunk.length === bytes.length});
        uploadId = result.upload_id || uploadId;
      }
      rememberPocketVoice(result.voice);
      toast(t("Voice saved. Preview it, then choose Use voice."));
      onSaved();
    } catch (e) { status.className = "status error"; status.textContent = e.message || t("That recording could not be read. Try a WAV or MP3 file."); }
    finally { if (context) await context.close(); save.disabled = false; file.disabled = false; name.disabled = false; }
  });
  box.append(field(t("Voice name"), name), field(t("Recording"), file), save, status);
  return box;
}

function hostedVoicePanel(service, model, current, onUse) {
  const box = el("div", "field clone");
  const provider = service.id;
  const eleven = provider === "elevenlabs";
  const providerName = eleven ? "ElevenLabs" : "Cartesia";
  box.append(el("div", "field-label", t("Clone a voice with {provider}", {provider: providerName})));
  if (!service.key_saved) {
    box.append(el("p", "hint", t("Switch to this speech service and save its key before cloning a voice.")));
    return box;
  }
  const dashboard = eleven ? "https://elevenlabs.io/app/voice-lab" : "https://play.cartesia.ai/voices";
  box.append(el("p", "hint", eleven
    ? t("Instant cloning: use 1–2 minutes of clear speech from one speaker (10–120 seconds accepted). WAV, MP3, or M4A.")
    : t("Use 10–60 seconds of clear speech from one speaker. Sonic 3.6 or newer uses up to 60 seconds; older models use the first 10 seconds. WAV, MP3, or M4A.")),
    el("p", "hint", t("Your recording will be uploaded to {provider} and the clone stored in your provider account. Your plan must include voice cloning.", {provider: providerName})));
  const name = input(t("Voice name")); name.maxLength = 64;
  const file = el("input"); file.type = "file"; file.accept = "audio/*";
  file.setAttribute("aria-label", t("Voice recording"));
  const consent = el("input"); consent.type = "checkbox";
  const permission = el("label", "hint");
  permission.append(consent, el("span", "", t("This is my voice, or I have the speaker's permission and the rights needed to upload and clone this recording.")));
  const language = selectBox([...new Set(state.language.options.map(primary))].map(l => ({value: l, label: languageWord(l)})), primary(state.language.value), null, t("Recording language"));
  const save = el("button", "btn ghost", t("Create voice")); save.type = "button";
  const status = el("p", "status"); status.setAttribute("role", "status");
  const accountStatus = el("p", "status"); accountStatus.setAttribute("role", "status");
  const list = el("div", "voices");
  const refresh = el("button", "btn ghost", t("Refresh my voices")); refresh.type = "button";
  const manage = el("a", "", t("Manage voices / verify ↗")); manage.href = dashboard; manage.target = "_blank"; manage.rel = "noopener noreferrer";
  let known = [];
  const show = () => {
    list.replaceChildren();
    for (const voice of known) {
      const row = el("div", "voice" + (voice.voice_id === current ? " on" : ""));
      const play = el("button", "play"); play.type = "button"; play.append(icon(PLAY));
      play.setAttribute("aria-label", t("Hear {name}", {name: voice.name})); play.disabled = !voice.available;
      play.addEventListener("click", () => preview(play, model, voice.voice_id));
      const use = el("button", "use", t(voice.voice_id === current ? "In use" : "Use voice")); use.type = "button"; use.disabled = !voice.available;
      use.addEventListener("click", () => { current = voice.voice_id; onUse(voice.voice_id); show(); });
      const right = el("span", "right");
      if (!voice.available) right.append(el("span", "vtag", t(voice.requires_verification ? "Verification required" : "Unavailable at provider")));
      right.append(use); row.append(play, el("span", "vname", voice.name), right); list.append(row);
    }
  };
  const load = async () => {
    refresh.disabled = true; accountStatus.textContent = t("Loading your voices…");
    try {
      const result = await api("/api/speech/voices", {provider});
      known = result.voices; show();
      accountStatus.textContent = known.length ? "" : t("No cloned voices in this account yet.");
    } catch (e) { accountStatus.textContent = e.message; }
    finally { refresh.disabled = false; }
  };
  refresh.addEventListener("click", load);
  save.addEventListener("click", async () => {
    const clip = file.files[0];
    if (!clip || !name.value.trim()) { status.textContent = t("Choose a recording and give the voice a name."); return; }
    if (!consent.checked) { status.textContent = t("Confirm permission to clone this voice first."); return; }
    if (clip.size > 20 * 1024 * 1024) { status.textContent = t("Choose a recording smaller than 20 MB."); return; }
    const controls = [save, file, name, consent, language.sel, refresh];
    controls.forEach(control => { control.disabled = true; });
    status.className = "status"; status.textContent = t("Preparing voice recording…");
    let context;
    try {
      context = new AudioContext();
      const decoded = await context.decodeAudioData(await clip.arrayBuffer());
      const max = eleven ? 120 : 60;
      if (decoded.duration < 10 || decoded.duration > max) throw new Error(t("Use a recording between 10 and {max} seconds.", {max}));
      const offline = new OfflineAudioContext(1, Math.round(decoded.duration * 48000), 48000);
      const source = offline.createBufferSource(); source.buffer = decoded; source.connect(offline.destination); source.start();
      const bytes = pocketWav((await offline.startRendering()).getChannelData(0), 48000);
      let uploadId = ""; let result;
      for (let offset = 0; offset < bytes.length; offset += 8192) {
        const chunk = bytes.subarray(offset, offset + 8192);
        const final = offset + chunk.length === bytes.length;
        status.textContent = final ? t("Creating voice at {provider}…", {provider: providerName})
          : t("Uploading voice… {percent}%", {percent: Math.round(offset / bytes.length * 100)});
        result = await api("/api/speech/voices/clone", {provider, name: name.value.trim(), language: language.sel.value,
          consent: true, audio: btoa(String.fromCharCode(...chunk)), offset, upload_id: uploadId, final});
        uploadId = result.upload_id || uploadId;
      }
      known = [result.voice, ...known.filter(v => v.voice_id !== result.voice.voice_id)]; show();
      status.textContent = result.voice.requires_verification
        ? t("Voice created. Complete verification in ElevenLabs, then refresh your voices here.")
        : result.voice.available ? t("Voice created. Preview it, then choose Use voice.") : t("Unavailable at provider");
      file.value = ""; name.value = ""; consent.checked = false;
    } catch (e) { status.className = "status error"; status.textContent = e.message || t("That recording could not be read. Try a WAV or MP3 file."); }
    finally { if (context) await context.close(); controls.forEach(control => { control.disabled = false; }); }
  });
  const actions = el("div", "actions"); actions.append(refresh, manage);
  box.append(field(t("Voice name"), name), field(t("Recording"), file), field(t("Recording language"), language), permission,
    save, status, el("div", "field-label", t("My cloned voices")), actions, accountStatus, list);
  load();
  return box;
}

// ---------- Pages ----------
const RENDER = {
  agent() {
    const page = el("div");
    page.append(pageHead(t("Agent"), t("Which agent answers when you talk, and the folder it works in. Switching checks the agent is installed and asks what the terminal asks.")));
    if (!agentOpts) {
      page.append(el("p", "status", t("Checking which agents are installed…")));
      loadAgentOptions().then(renderPage).catch(e => toast(e.message, true));
      return page;
    }
    const current = productOf(state.agent.type);
    const found = new Set(agentOpts.found);
    const rank = a => (a.id === current ? 0 : a.ready ? 1 : found.has(a.id) ? 2 : 3);
    const stack = el("div", "stack");
    for (const a of [...agentOpts.agents].sort((x, y) => rank(x) - rank(y))) {
      const inUse = a.id === current; const opened = openAgent === a.id;
      const card = el("div", "pcard" + (inUse ? " in-use" : "") + (opened ? " opened" : "")); const top = el("div", "pcard-top");
      const title = a.label.split(" — ")[0];
      const name = el("div", "pcard-name"); name.append(mark(title, a.id), el("span", "", t(title)));
      if (inUse) name.append(badge(t("In use"), "ink"));
      else if (a.id === "own") name.append(badge(t("Preview")));
      else if (a.ready) name.append(badge(t("Ready on this computer"), "good"));
      else if (found.has(a.id)) name.append(badge(t("Found on this computer"), "good"));
      top.append(name, el("div", "credit"));
      const body = el("div", "facts-wrap");
      if (AGENT_INFO[a.id]) body.append(el("p", "pdesc", t(AGENT_INFO[a.id])));
      if (inUse) { const facts = el("div", "facts"); for (const [k, v] of [["Name", state.agent.name], ["Workspace", state.agent.workspace || "—"]]) { const f = el("div", "fact"); f.append(el("span", "k", t(k)), el("span", "v", v)); facts.append(f); } body.append(facts); }
      top.append(body);
      const action = el("div", "pcard-action");
      const btn = el("button", "btn ghost card", t(opened ? "Close" : inUse ? "Settings" : "Select")); btn.type = "button";
      btn.addEventListener("click", () => { openAgent = opened ? "" : a.id; if (!opened) agentDraft = freshDraft(a.id); renderPage(); });
      action.append(btn); top.append(action); card.append(top);
      if (opened) card.append(agentPageDetail(a, inUse));
      stack.append(card);
    }
    page.append(stack); return page;
  },
  speech() {
    const page = el("div");
    page.append(pageHead(t("Speech service"), t("Who listens and who speaks. One key covers both. Select a service to choose its models and voice.")));
    page.append(keyLookupPanel(state.speech.key_lookup, state.speech.reusable, state.speech.services, () => reload()));
    const stack = el("div", "stack"); for (const s of orderedServices()) stack.append(serviceCard(s));
    page.append(stack); return page;
  },
  voice() {
    const page = el("div");
    const service = state.speech.services.find(s => s.id === state.speech.tts_provider);
    page.append(pageHead(t("Voice"), service ? t("The voice {name} speaks with. Changes apply to the next call.", {name: service.name}) : t("The voice your speaking service uses.")));
    if (service && service.models) {
      const model = modelById(service, "tts", service.current.tts);
      // Which service and model the list belongs to, and where to change them:
      // this page only picks among their voices.
      const using = el("div", "panel"); const row = el("div", "row");
      const who = el("span", "pcard-name"); who.append(mark(service.name, service.id), el("span", "", model ? `${service.name} · ${modelLabel(model)}` : service.name));
      const change = el("a", "", t("Change under Speech service")); change.href = "#/speech";
      change.addEventListener("click", () => { openService = service.id; });
      row.append(el("span", "k", t("Speaking with")), el("div", "row-actions"),
        el("p", "hint", t("Each service has its own voices. To hear others, switch service or speaking model under Speech service.")));
      row.children[1].append(who, change);
      using.append(row); using.style.marginBottom = "24px"; page.append(using);
      // A local voice can need a model this computer hasn't downloaded yet.
      const status = el("p", "status");
      const use = service.id === "local"
        ? async v => { if (await saveLocal({stt: service.current.stt, tts: "", voice: v}, status, t("{label} saved.", {label: t("Voice")}))) await reload(); }
        : v => set("tts.voice_id", v, t("Voice"));
      page.append(field(model ? t("Voices for {model}", {model: modelLabel(model)}) : t("Voices"),
        voiceList(service, service.current.tts, state.voice.voice_id, use, service.key_saved), status));
    }
    const panel = el("div", "panel"); panel.style.marginTop = "24px";
    const row = el("div", "row"); row.append(el("span", "k", t("Playback speed")), segments(state.voice.speeds.map(v => ({value: v, label: `${v}×`})), state.voice.speed, v => set("tts.speed", v, t("Speed"))));
    panel.append(row); page.append(panel); return page;
  },
  language() {
    const page = el("div"); page.append(pageHead(t("Language"), t("What you speak. Choosing it also picks the recommended models and voice for it, and shows this page in it.")));
    const panel = el("div", "panel"); const row = el("div", "row");
    row.append(el("span", "k", t("You speak")), languagePicker(t("Language")),
      el("p", "hint", t("Models and voice you picked yourself stay when they already cover the new language.")));
    const pageRow = el("div", "row");
    const override = pageOverride();
    pageRow.append(el("span", "k", t("This page in")),
      selectBox([{value: "", label: t("Same as what you speak")}, ...UI_LANGS.map(l => ({value: l, label: UI_LANG_NAMES[l]}))], override,
        async v => { setPageOverride(v); await useLocale(v || primary(state.language.value)); renderChrome(); renderPage(); }, t("This page in")),
      el("p", "hint", t("Only this page's words; calls still use the language you speak.")));
    panel.append(row, pageRow); page.append(panel); return page;
  },
  delivery() {
    const page = el("div"); page.append(pageHead(t("Call delivery"), t("How a call reaches you: calls you start and links your agent sends both go the way chosen here.")));
    const onDiscord = state.delivery.transport === "discord";
    const card = (o, inUse, action) => {
      const c = el("div", "pcard" + (inUse ? " in-use" : "")); const top = el("div", "pcard-top");
      const name = el("div", "pcard-name"); name.append(mark(o.name, o.icon), el("span", "", o.name));
      if (inUse) name.append(badge(t("In use"), "ink")); else if (o.badge) name.append(badge(o.badge));
      top.append(name, el("div", "credit"));
      const facts = el("div", "facts"); for (const [k, v] of o.facts) { const f = el("div", "fact"); f.append(el("span", "k", k), el("span", "v", v)); facts.append(f); } top.append(facts);
      const act = el("div", "pcard-action"); if (action) act.append(action); top.append(act); c.append(top);
      if (o.hint) c.append(el("p", "hint card-hint", o.hint));
      return c;
    };
    if (!onDiscord) {
      // Browser or terminal: what `sleeptalk` in the workspace starts.
      const where = el("div", "panel");
      const row = el("div", "row");
      row.append(el("span", "k", t("Where you talk")), segments([{value: "web", label: t("In a browser")}, {value: "terminal", label: t("In the terminal")}],
        state.surface.type === "terminal" ? "terminal" : "web", v => set("transport.type", v, t("Where you talk"))),
        el("p", "hint", t("What `sleeptalk` starts in this profile's workspace. Links your agent sends are always browser calls.")));
      where.append(row);
      if (state.surface.type === "terminal") {
        const r2 = el("div", "row");
        r2.append(el("span", "k", t("Headphones or speakers?")), segments([{value: "headphones", label: t("Headphones")}, {value: "speakers", label: t("Speakers")}],
          state.surface.capture_mode || "speakers", v => set("transport.capture_mode", v, t("Headphones or speakers?"))),
          el("p", "hint", t("With speakers, Sleeptalk waits for its own voice to stop before it listens.")));
        where.append(r2);
      }
      page.append(where);
    }
    // How calls reach you: the one stored choice, then Discord, its own kind of profile.
    const stack = el("div", "stack"); const tn = state.tailnet;
    const options = [
      {id: "local", name: t("This computer"), facts: [[t("Works from"), t("this computer's browser")], [t("Limit"), t("none")], [t("Setup"), t("nothing to install")]], enabled: true,
        hint: t("Calls open in a browser on this computer (localhost), with no relay. Your phone can't open these links.")},
      {id: "relay", name: t("Hosted relay"), facts: [[t("Works from"), t("anywhere")], [t("Limit"), t("30 minutes, announced")], [t("Setup"), t("nothing to install")]], enabled: true},
      {id: "tailnet", name: t("Your tailnet"), facts: [[t("Works from"), t("devices on your tailnet")], [t("Limit"), t("none")], [t("Setup"), tn.available ? t("ready as {name}", {name: tn.name || t("this machine")}) : t("needs Tailscale with HTTPS")]], enabled: tn.available},
    ];
    for (const o of options) {
      const inUse = !onDiscord && state.delivery.value === o.id;
      let b = null;
      // A Discord bot answers in Discord: its profile can't take a browser
      // delivery (saving one made the profile fail to load).
      if (!inUse && !onDiscord) { b = el("button", "btn ghost card", t(o.enabled ? "Select" : "Not available")); b.type = "button"; b.disabled = !o.enabled; b.addEventListener("click", () => set("transport.options.delivery", o.id, t("Call delivery"))); }
      stack.append(card(o, inUse, b));
    }
    const go = el("a", "btn ghost card", onDiscord ? t("Discord settings") : t("Set up Discord")); go.href = "#/discord";
    stack.append(card({name: "Discord", icon: "discord",
      facts: [[t("Works from"), t("a Discord voice channel")], [t("Limit"), t("none")], [t("Setup"), t("a bot, about five minutes")]],
      hint: onDiscord ? t("This profile answers in Discord, so the choices above aren't used.") : t("Talk to your agent in a voice channel instead. The bot gets its own profile.")}, onDiscord, go));
    page.append(stack);
    return page;
  },
  discord() {
    const page = el("div");
    const dc = state.discord;
    if (!dc.enabled) {
      page.append(pageHead(t("Discord setup"), t("A bot in one voice channel, locked to your account. It becomes its own profile and reuses this profile's speech key.")));
      page.append(discordSetupCard("new"));
      return page;
    }
    page.append(pageHead(t("Discord setup"), t("This profile is a Discord bot. It answers in one voice channel, only for the people you allow.")));
    const locked = !dc.open && dc.guild_id && dc.channel_ids && dc.operator_ids;
    const card = el("div", "pcard in-use"); const top = el("div", "pcard-top");
    const name = el("div", "pcard-name"); name.append(mark("Discord", "discord"), el("span", "", t("Discord bot")));
    name.append(locked ? badge(t("Locked to you"), "good") : dc.open ? badge(t("Open to the whole server"), "warn") : badge(t("Not finished"), "warn"));
    top.append(name, el("div", "credit"));
    const facts = el("div", "facts");
    for (const [k, v] of [["Server", dc.guild_id], ["Voice channel", dc.channel_ids], ["Only for", dc.operator_ids], ["Access", t(locked ? "you only" : dc.open ? "anyone in the server" : "won't start yet")]]) {
      const f = el("div", "fact"); f.append(el("span", "k", t(k)), el("span", "v", v || "—")); facts.append(f);
    }
    top.append(facts);
    const action = el("div", "pcard-action");
    const flowHolder = el("div");
    const openFlow = () => { flowHolder.replaceChildren(discordSetupCard("existing")); btn.hidden = true; };
    const btn = el("button", "btn ghost card", t(locked ? "Change channel or owner" : dc.open ? "Lock it to me" : "Finish setup")); btn.type = "button";
    btn.addEventListener("click", openFlow);
    if (!dc.token_saved) { btn.disabled = true; btn.title = t("This bot's token isn't in the private store; set it up again with a new bot below."); }
    action.append(btn); top.append(action); card.append(top);
    if (!locked && !dc.open) card.append(el("p", "hint", t("This bot won't start until it's locked to one voice channel and to your Discord account. Finish setup takes about a minute and reuses its saved token.")));
    if (dc.open) card.append(el("p", "hint", t("Anyone in the server can talk to your agent, which can run commands in its workspace. Lock it unless the server is only yours.")));
    if (locked || dc.open) card.append(botRunControls(state.profile));
    card.append(flowHolder);
    page.append(card);
    const another = el("div", "after-card");
    const more = el("button", "btn ghost", t("Set up another bot")); more.type = "button";
    more.addEventListener("click", () => another.replaceChildren(discordSetupCard("new")));
    another.append(more); page.append(another);
    if (!locked && !dc.open && dc.token_saved) openFlow();
    page.append(discordBotSettings(dc));
    return page;
  },
  behaviour() {
    const page = el("div"); page.append(pageHead(t("Behavior"), t("How the conversation feels. Each applies to the next call; the call page changes them live too.")));
    const panel = el("div", "panel");
    const rows = [
      ["Reply length", "policy.reply_length", state.behaviour.reply_length, [{value: "concise", label: "Concise speech"}, {value: "full", label: "Full"}, {value: "summarized", label: "Short answers"}], "Concise speech keeps the complete result in text. Short answers ask the agent to write less."],
      ["Tool narration", "policy.narration_style", state.behaviour.narration_style, [{value: "narrated", label: "Spoken"}, {value: "keyboard", label: "Keyboard sounds"}, {value: "silence", label: "Silent"}], "What you hear while the agent works."],
      ["Turn taking", "policy.endpointing", state.behaviour.endpointing, [{value: "relaxed", label: "Patient"}, {value: "balanced", label: "Balanced"}, {value: "aggressive", label: "Snappy"}], "How long a pause counts as the end of your turn."],
    ];
    for (const [k, setting, cur, opts, hint] of rows) { const r = el("div", "row"); r.append(el("span", "k", t(k)), segments(opts.map(o => ({...o, label: t(o.label)})), cur, v => set(setting, v, t(k))), el("p", "hint", t(hint))); panel.append(r); }
    const help = el("details", "panel");
    help.append(el("summary", "", t("Advanced troubleshooting")));
    const lat = el("div", "row");
    lat.append(el("span", "k", t("Performance traces")), segments([{value: true, label: t("On")}, {value: false, label: t("Off")}], state.surface.latency, v => set("log.latency", v, t("Performance traces"))),
      el("p", "hint", t("Off by default. Saves local timing and connection measurements for future calls. Basic error logs remain available. To record just one call, use Help on its call page.")));
    help.append(lat);
    page.append(panel, help); return page;
  },
  keys() {
    const store = state.key_store || {kind: "none", path: ""};
    const where = store.kind === "keychain" ? t("Keys are kept in the macOS Keychain.")
      : store.kind === "secret_service" ? t("Keys are kept in this computer's Secret Service keyring.")
      : store.kind === "file" ? t("Keys are kept in a file only your user account can read ({path}). It isn't encrypted beyond that, so any program running as you can read it.", {path: store.path})
      : t("No key has been stored yet.");
    const page = el("div"); page.append(pageHead(t("Keys"), where + " " + t("The profile only references keys; values are never shown. Replace a speech key from its card under Speech service.")));
    const table = el("table", "keys"); const head = el("tr"); head.append(el("th", "", t("Used for")), el("th", "", t("Reference"))); table.append(head);
    const friendly = env => {
      if (/DISCORD_BOT_TOKEN$/.test(env)) return t("Discord bot token");
      const service = state.speech.services.find(sv => env === `${sv.id.toUpperCase()}_API_KEY`);
      return service ? t("{name} key", {name: service.name}) : env.replace(/_/g, " ").toLowerCase().replace(/^./, c => c.toUpperCase());
    };
    for (const k of state.keys) {
      const tr = el("tr"); const what = el("td"); what.append(el("div", "", friendly(k.env)), el("div", "hint", k.env));
      const td = el("td"); td.append(el("code", "", k.reference)); tr.append(what, td); table.append(tr);
    }
    if (!state.keys.length) { const tr = el("tr"); const td = el("td", "", t("No stored keys yet.")); td.colSpan = 2; tr.append(td); table.append(tr); }
    page.append(table);
    if (store.can_move) {
      const mv = el("div", "lookup");
      const target = store.os_store === "keychain" ? t("the macOS Keychain") : t("the Secret Service keyring");
      mv.append(el("p", "", t("Move keys into {target}?", {target})),
        el("p", "hint", t("They'd be encrypted by the system instead of sitting in a file. Calls then need you logged in to this computer, and after a Python upgrade macOS may ask once to allow access.")));
      const go = el("button", "btn ghost", t("Move keys")); go.type = "button";
      go.addEventListener("click", async () => {
        if (!confirm(t("Move every stored key into {target}? This can't be undone from this page.", {target}))) return;
        go.disabled = true;
        try { const r = await api("/api/keys/move", {confirm: true}); toast(t("Moved {count} keys.", {count: r.moved})); await reload(); }
        catch (e) { toast(e.message, true); go.disabled = false; }
      });
      mv.append(go); page.append(mv);
    }
    page.append(keyInventory(state.key_inventory || []));
    page.append(keyLookupPanel(state.speech.key_lookup, state.speech.reusable, state.speech.services, () => reload()));
    return page;
  },
};

// Every key in the store, the profiles that use it, and a way to clear the
// ones left over from replaced keys or deleted profiles.
function keyInventory(items) {
  const box = el("div");
  if (!items.length) return box;
  box.append(el("h2", "section-title", t("All keys on this computer")));
  const serviceName = provider => {
    if (provider === "discord") return t("Discord bot token");
    if (/^agent-/.test(provider)) return t("Model key");
    const sv = state.speech.services.find(x => x.id === provider);
    return sv ? t("{name} key", {name: sv.name}) : provider;
  };
  const table = el("table", "keys"); const head = el("tr");
  head.append(el("th", "", t("Key")), el("th", "", t("Used by")), el("th", "", t("Reference"))); table.append(head);
  for (const k of items) {
    const tr = el("tr"); const used = el("td");
    if (k.unused) used.append(badge(t("Not used"), "warn")); else used.textContent = k.used_by.join(", ");
    const ref = el("td"); ref.append(el("code", "", k.reference.slice(0, 23) + "…"));
    tr.append(el("td", "", serviceName(k.provider)), used, ref); table.append(tr);
  }
  box.append(table);
  const unused = items.filter(k => k.unused);
  if (unused.length) {
    const actions = el("div", "actions");
    const go = el("button", "btn ghost", unused.length === 1 ? t("Remove the unused key") : t("Remove {count} unused keys", {count: unused.length})); go.type = "button";
    go.addEventListener("click", async () => {
      if (!confirm(t("Remove keys no profile uses from this computer? This doesn't revoke them at the provider; do that in its dashboard if a key leaked."))) return;
      go.disabled = true;
      try { const r = await api("/api/keys/remove-unused", {references: unused.map(k => k.reference)}); toast(t("Removed {count} keys.", {count: r.removed})); await reload(); }
      catch (e) { toast(e.message, true); go.disabled = false; }
    });
    actions.append(go); box.append(actions);
  }
  box.append(el("p", "hint", t("Removing a key here only deletes Sleeptalk's copy. Profiles outside this computer's usual folders aren't checked.")));
  return box;
}

// The bot's name, who may talk to it, and its token: what the terminal edits.
function discordBotSettings(dc) {
  const panel = el("div", "panel bot-settings");
  const h = el("h2", "section-title", t("Bot settings"));
  const wrap = el("div"); wrap.append(h);
  const row = (label, control, hint) => { const r = el("div", "row"); r.append(el("span", "k", label), control); if (hint) r.append(el("p", "hint", hint)); panel.append(r); };
  const saveRow = (value, placeholder, setting, label, opts = {}) => {
    const box = el("div", "row-actions"); const i = input(placeholder); i.value = value; if (opts.type) i.type = opts.type;
    const b = el("button", "btn ghost small", opts.button || t("Save")); b.type = "button";
    b.addEventListener("click", () => opts.onSave ? opts.onSave(i, b) : set(setting, i.value, label));
    box.append(i, b); return box;
  };
  row(t("Display name"), saveRow(dc.bot_name || "", t("Bot's display name"), "agent.name", t("Display name")), t("How Sleeptalk names the bot in its own messages."));
  row(t("Who can talk to it"), saveRow(dc.operator_ids || "", t("Discord user IDs, comma-separated"), "transport.operator_ids", t("Who can talk to it")),
    t("Each person's Discord user ID. Only they can start and steer the agent."));
  const open = segments([{value: false, label: t("Only those people")}, {value: true, label: t("Anyone in the server")}], dc.open, async v => {
    if (v && !confirm(t("Anyone in the server will be able to talk to your agent, which can run commands in its workspace. Open it anyway?"))) return;
    await set("transport.authorization_required", !v, t("Access"));
  });
  row(t("Access"), open, t("Keep it to the people above unless the server is only yours."));
  row(t("Bot token"), saveRow("", t("Paste a new bot token"), "", "", {type: "password", button: t("Replace"), onSave: async (i, b) => {
    b.disabled = true;
    try { await api("/api/discord/replace-token", {value: i.value}); i.value = ""; toast(t("Bot token replaced.")); await reload(); }
    catch (e) { toast(e.message, true); b.disabled = false; }
  }}), t("Checked with Discord, then kept in this computer's private store. Restart the bot to use it."));
  wrap.append(panel);
  return wrap;
}

// A call this profile still has running (one that was closed can take a
// moment to shut down): say so, and offer to end it and start again.
function offerReplace(status, retry) {
  status.className = "hint warn"; status.textContent = t("A call is still running on this profile.");
  const end = el("button", "btn small", t("End it and start a new one")); end.type = "button";
  end.addEventListener("click", () => { end.remove(); status.className = "hint"; status.textContent = t("Ending the running call…"); retry(); });
  status.after(end);
}
// Keep the last invitation visible when settings re-renders. Links stay in
// memory on this page; they are never written to browser storage.
const settingsCallLinks = new Map();
// "Start a call" for the current profile: its button and where the outcome shows.
// `open`: show the choice of devices at once, without the Start a call button.
function startCallControls(className, {open = false} = {}) {
  const surface = state.surface ? state.surface.type : "web";
  const profileKey = state.path || state.profile;
  const callBtn = el("button", className, t("Start a call")); callBtn.type = "button";
  const out = el("div", "call-out");
  const showLink = (invitation, copied = null) => {
    out.replaceChildren(); callBtn.disabled = false; callBtn.textContent = t("Call link");
    const status = el("p", "hint", t("Your call link is ready.") + (invitation.say ? " " + invitation.say : ""));
    status.setAttribute("role", "status");
    const link = input(t("Call link")); link.value = invitation.join_url; link.readOnly = true;
    link.setAttribute("aria-label", t("Call link"));
    link.addEventListener("click", () => link.select());
    // These are ordinary links clicked after startup has completed. The
    // primary action stays in this tab and never needs popup permission.
    const open = el("a", "btn small", t("Open call")); open.href = invitation.join_url;
    const newTab = el("a", "linkbtn", t("Open in new tab")); newTab.href = invitation.join_url;
    newTab.target = "_blank"; newTab.rel = "noopener noreferrer";
    const copy = el("button", "btn ghost small", t("Copy link")); copy.type = "button";
    const copyAt = el("span", "tip-anchor"); copyAt.append(copy);
    const copiedNow = () => { copy.textContent = t("Copied"); flashTip(copyAt, t("Link copied")); };
    copy.addEventListener("click", async () => {
      try { await navigator.clipboard.writeText(invitation.join_url); copiedNow(); }
      catch { link.focus(); link.select(); status.textContent = t("Select and copy the link above."); }
    });
    const actions = el("div", "row-actions"); actions.append(open, copyAt, newTab);
    if (copied) copied.then(ok => {
      if (!ok) return;
      copiedNow();
      status.textContent = t("Link copied. Open it on your other device.") + (invitation.say ? " " + invitation.say : "");
    });
    const restart = el("button", "linkbtn", t("End it and start a new one")); restart.type = "button";
    restart.addEventListener("click", () => { settingsCallLinks.delete(profileKey); start(invitation.here, true); });
    out.append(status);
    // A phone opens the link by scanning it (hidden when this page is on the phone).
    if (invitation.qr_svg && !invitation.here) {
      const qr = el("figure", "call-qr");
      const img = el("img");
      img.src = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(invitation.qr_svg);
      img.alt = t("QR code for the call link");
      qr.append(img, el("figcaption", "", t("Scan with your phone's camera.")));
      out.append(qr);
    }
    out.append(link, actions, restart);
  };
  const start = async (isHere, replace = false) => {
    callBtn.disabled = true; callBtn.textContent = t("Start a call");
    const status = el("p", "hint", t("Starting the call…")); status.setAttribute("role", "status");
    out.replaceChildren(status);
    const request = api("/api/call", replace ? {here: isHere, replace: true} : {here: isHere});
    // Copied for the other device, started inside the click (see copyWhenReady).
    const copied = isHere ? Promise.resolve(false) : copyWhenReady(request.then(r => r.join_url));
    try {
      const r = await request;
      const invitation = {...r, here: isHere}; settingsCallLinks.set(profileKey, invitation);
      showLink(invitation, copied);
      if (isHere) location.assign(invitation.join_url);
    } catch (e) {
      callBtn.disabled = false;
      if (e.code === "call_running" && !replace) { offerReplace(status, () => start(isHere, true)); return; }
      status.className = "hint warn"; status.textContent = e.message;
    }
  };
  const choose = () => {
    const saved = settingsCallLinks.get(profileKey);
    if (saved && surface === "web") { showLink(saved); return; }
    out.replaceChildren();
    if (surface === "discord") {
      const dc = state.discord;
      if (!dc.open && !(dc.guild_id && dc.channel_ids && dc.operator_ids)) {
        const go = el("a", "", t("Finish Discord setup")); go.href = "#/discord";
        out.append(el("p", "hint", t("This bot won't start until it's locked to one voice channel and to your Discord account.")), go); return;
      }
      out.append(botRunControls(state.profile, true)); return;
    }
    if (surface === "terminal") { out.append(el("p", "hint", t("This profile talks in the terminal. Run it there:")), el("code", "cmd", `cd ${state.agent.workspace || "."} && sleeptalk`)); return; }
    if (HOST) { start(false); return; }
    const here = el("button", "btn small", t("Open call")); here.type = "button";
    const phone = el("button", "btn ghost small", t("Link for another device")); phone.type = "button";
    here.addEventListener("click", () => start(true)); phone.addEventListener("click", () => start(false));
    const row = el("div", "row-actions"); row.append(here, phone); out.append(row);
  };
  callBtn.addEventListener("click", choose);
  const saved = settingsCallLinks.get(profileKey);
  if (open) { callBtn.hidden = true; choose(); }
  else if (saved && surface === "web") showLink(saved);
  return {button: callBtn, out};
}

// Sidebar: start a call on this profile, rename it, delete it.
function profileActions() {
  const box = document.getElementById("profile-actions"); box.replaceChildren();
  if (!state.profile) return;
  const {button: callBtn, out} = startCallControls("btn small side-call");
  const manage = el("div", "side-links");
  const rename = el("button", "linkbtn", t("Rename")); rename.type = "button";
  rename.addEventListener("click", async () => {
    const name = prompt(t("New name for {profile}:", {profile: state.profile}), state.profile);
    if (!name || name === state.profile) return;
    try { await api("/api/profile/rename", {name}); toast(t("Renamed to {profile}.", {profile: name})); await reload(); } catch (e) { toast(e.message, true); }
  });
  const del = el("button", "linkbtn danger", t("Delete")); del.type = "button";
  del.addEventListener("click", async () => {
    const typed = prompt(t("Delete profile {profile}? Keys no other profile uses are removed from this computer too (not revoked at the provider). Type its name to confirm:", {profile: state.profile}));
    if (typed !== state.profile) { if (typed !== null) toast(t("The name didn't match; nothing was deleted."), true); return; }
    try { await api("/api/profile/delete", {confirm: typed}); toast(t("Deleted {profile}.", {profile: typed})); openService = ""; agentOpts = null; location.hash = "#/speech"; await reload(); } catch (e) { toast(e.message, true); }
  });
  manage.append(rename, el("span", "", "·"), del);
  box.append(callBtn, out, manage);
}

// Start, restart, or stop a Discord bot. It runs as a user service, so it keeps
// going after this page closes and comes back after a restart.
const BOT_BACKGROUND = {
  mac: "Start keeps the bot running in the background, also after this page closes, and starts it again when you log in. macOS shows a notice the first time; Login Items lists it as \"sleeptalk\". Stop turns it off and removes it from Login Items.",
  linux: "Start keeps the bot running in the background, also after you log out, and starts it again when this computer restarts. Stop turns it off.",
};
function botRunControls(profile, startNow = false) {
  const box = el("div", "bot-run"); const line = el("div", "row-actions"); const out = el("p", "hint");
  const badgeHolder = el("span"); const go = el("button", "btn", t("Start the bot")); go.type = "button";
  const halt = el("button", "btn ghost small", t("Stop")); halt.type = "button"; halt.hidden = true;
  line.append(badgeHolder, go, halt); box.append(line, out);
  const background = BOT_BACKGROUND[state.discord?.host_os];
  if (background) box.append(el("p", "hint", t(background)));
  // Why it isn't running, in its own last words, then where the rest is.
  const stoppedBecause = (reason, log) => {
    out.className = "hint warn";
    out.replaceChildren(el("span", "", (reason ? t("It stopped: {reason}", {reason}) : t("It stopped right away.")) + " " + t("Its log:") + " "), el("code", "", log || ""));
  };
  const send = async (path) => {
    const r = await hostFetch(path, {method: "POST", cache: "no-store", headers: {"Content-Type": "application/json", "X-Settings-CSRF": csrf}, body: JSON.stringify({profile})});
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.message || discordFailure(data.code));
    return data;
  };
  const show = running => {
    badgeHolder.replaceChildren(running === null ? badge(t("Not sure if it's running"), "warn") : running ? badge(t("Running"), "good") : badge(t("Stopped"), "warn"));
    go.textContent = running ? t("Restart") : t("Start the bot"); go.className = running ? "btn ghost small" : "btn"; halt.hidden = !running;
  };
  const start = async () => {
    go.disabled = halt.disabled = true; out.className = "hint"; out.textContent = t("Starting the bot…");
    try {
      const r = await send("/api/discord/start"); show(r.status === "running");
      if (r.status === "running") { out.textContent = t("It's running. In Discord, join your voice channel, then type {command} in any chat in that server.", {command: `/${profile} join`}); if (r.note) out.append(el("br"), r.note); }
      else stoppedBecause(r.reason, r.log);
    } catch (e) { out.className = "hint warn"; out.textContent = e.message; }
    go.disabled = halt.disabled = false;
  };
  go.addEventListener("click", start);
  halt.addEventListener("click", async () => {
    go.disabled = halt.disabled = true;
    try { await send("/api/discord/stop"); show(false); out.className = "hint"; out.textContent = t("Stopped. It won't start again until you start it here."); }
    catch (e) { out.className = "hint warn"; out.textContent = e.message; }
    go.disabled = halt.disabled = false;
  });
  if (startNow) start();
  else send("/api/discord/status").then(r => {
    show(r.running);
    if (r.running === false && r.reason) { out.className = "hint warn"; out.textContent = t("It stopped: {reason}", {reason: r.reason}); }
  }, () => show(null));
  return box;
}

// ---------- Discord setup (the four steps) ----------
const DISCORD_FAILURES = {
  invalid_credential: "Discord rejected this token. In the Developer Portal, open Bot, Reset Token, and copy the new one.",
  permission_denied: "This token can't be used for a bot. Reset it in the Developer Portal and try again.",
  network_timeout: "Discord took too long to answer. Try again.",
  bot_not_in_server: "The bot isn't in that server yet. Add it with the link in step 2, then refresh.",
  channel_not_allowed: "The bot can't join and speak in that channel. Pick another, or add the bot again with the link to fix its permissions.",
  channel_unverified: "Couldn't check that channel just now. Try again.",
  discord_choices: "Pick a server and a voice channel, and enter your Discord user ID.",
  discord_unavailable: "Discord didn't answer. Try again in a moment.",
  discord_unfinished: "This bot won't start until it's locked to one voice channel and to your Discord account.",
  profile_exists: "A profile with that name already exists; choose another name.",
  bad_profile_name: "Profile names are lowercase letters, digits, and hyphens.",
  token_missing: "Save the bot token first.",
  profile_failed: "The profile couldn't be written. Check that the agent is still set up, then try again.",
};
function stepTitle(n, text) { const title = el("p", "step-title"); title.append(el("span", "n", String(n)), el("span", "", t(text))); return title; }
function boxed(select) { const b = el("div", "select"); b.append(select); return b; }
function steps(items) { const ol = el("ol", "steps"); items.forEach(text => ol.append(el("li", "", t(text)))); return ol; }
const OWNER_HELP = ["On your phone: profile picture, gear, Advanced, Developer Mode on; then profile picture, ⋯ menu, Copy User ID.", "On a computer: Settings, Advanced, Developer Mode on; then right-click your name, Copy User ID."];
const discordFailure = (code, message) => message || t(DISCORD_FAILURES[code] || "That didn't work. Try again.");
function discordSetupCard(mode, flow) {
  const existing = mode === "existing";
  if (existing) return discordExistingFlow();
  const forRequest = !!flow;
  const c = el("div", "pcard discord"); const top = el("div", "pcard-top");
  const name = el("div", "pcard-name"); name.append(mark("Discord", "discord"), el("span", "", t("Set up a Discord bot"))); if (!forRequest && !state.discord.speech_ready) name.append(badge(t("needs a speech key first")));
  top.append(name, el("div", "credit", t("About five minutes")));
  const factRows = forRequest ? [["Profile", state.request.profile]] : [["Profile", "a separate one"], ["Speech", state.speech.stt_provider === "local" && state.speech.tts_provider === "local" ? "on this computer, no key" : "reuses this key"], ["Discord extra", state.discord.missing_extra ? "not installed yet" : "installed"]];
  const facts = el("div", "facts"); for (const [k, v] of factRows) { const f = el("div", "fact"); f.append(el("span", "k", t(k)), el("span", "v", t(v))); facts.append(f); }
  top.append(facts); c.append(top);
  const body = el("div", "flow"); c.append(body);
  const status = el("p", "status");
  const s1 = el("div", "field"); s1.append(stepTitle(1, "Create the bot and paste its token"),
    steps(["Open the Discord Developer Portal and sign in with your usual account.", "New Application, any name (it becomes the bot's name), Create.", "Open Bot in the menu: Reset Token, confirm, Copy. Discord shows it once.", "Paste it here. Leave the other toggles alone."]));
  const portal = el("a", "", t("Open the Discord Developer Portal ↗")); portal.href = "https://discord.com/developers/applications"; portal.target = "_blank"; portal.rel = "noopener noreferrer";
  const tokenForm = el("form"); const token = input(t("Paste bot token")); token.type = "password"; token.autocomplete = "off"; token.maxLength = 8192;
  const tokenGo = el("button", "btn", t("Save token")); tokenGo.type = "submit"; tokenForm.append(token, tokenGo);
  s1.append(portal, tokenForm, el("p", "hint", HOST ? t("Checked with Discord, then kept on your agent's computer.") : t("Checked with Discord, then kept in this computer's private store."))); body.append(s1);
  const rest = el("div", "field"); rest.hidden = true;
  const botName = el("strong", "", t("your bot")); const invite = el("a", "btn invite", ""); invite.target = "_blank"; invite.rel = "noopener noreferrer";
  const introText = el("p", "hint"); introText.append(...around(t("Invite {bot} into a server you own or manage. Pick the server, tap Authorize, and leave the ticked permissions alone."), botName));
  const refresh = el("button", "btn ghost small", t("I've added it, refresh")); refresh.type = "button";
  const guilds = el("select"); const chans = el("select"); const chanHint = el("p", "hint");
  const ownerHelp = steps(OWNER_HELP);
  const owner = input(t("Your Discord user ID (a long number)")); owner.inputMode = "numeric"; owner.pattern = "[0-9]{1,20}";
  const pname = input(t("Profile name")); pname.value = forRequest ? state.request.profile : state.discord.suggested_profile; pname.maxLength = 64;
  const finish = el("button", "btn", t("Create the Discord profile")); finish.type = "button";
  const done = el("div"); done.hidden = true;
  const serverRow = el("div", "row-actions"); serverRow.append(invite, refresh);
  rest.append(stepTitle(2, "Add the bot to your server"), introText, serverRow, stepTitle(3, "Pick where to talk"), el("p", "hint", t("Server")), boxed(guilds), el("p", "hint", t("Voice channel")), boxed(chans), chanHint,
    el("p", "hint", t("The bot only joins and talks in this voice channel.")),
    stepTitle(4, "Lock the bot to you"), el("p", "hint", t("Only this Discord account will be able to talk to your agent.")), ownerHelp, owner,
    ...(forRequest ? [] : [el("p", "hint", t("Profile name for the bot")), pname]), finish, done);
  body.append(rest, status);
  let guildId = "";
  const fill = (select, items, keep, empty) => { select.replaceChildren(); if (!items.length) { const o = el("option", "", empty); o.value = ""; select.append(o); select.disabled = true; return; } select.disabled = false; for (const it of items) { const o = el("option", "", it.name); o.value = it.id; select.append(o); } select.value = items.some(i => i.id === keep) ? keep : items[0].id; };
  const post = async (path, payload) => { const r = await hostFetch(path, {method: "POST", cache: "no-store", headers: {"Content-Type": "application/json", "X-Settings-CSRF": csrf}, body: JSON.stringify(payload)}); const data = await r.json().catch(() => ({})); if (!r.ok) throw new Error(discordFailure(data.code, data.message)); return data; };
  const showOptions = data => {
    const app = data.application || {}; botName.textContent = app.name || t("your bot");
    // The main action until the bot is in a server; then it steps back for the steps below.
    invite.className = (data.guilds || []).length ? "btn ghost small invite" : "btn invite";
    invite.textContent = app.name ? t("Add {name} to your server ↗", {name: app.name}) : t("Add the bot to your server ↗"); invite.href = app.invite_url && app.invite_url.startsWith("https://discord.com/") ? app.invite_url : "#";
    fill(guilds, data.guilds || [], data.guild_id || guildId, t("Not in any server yet")); guildId = guilds.value;
    if (Array.isArray(data.channels)) { const channels = data.channels.filter(ch => ch.eligible); fill(chans, channels, chans.value, t((data.guilds || []).length ? "No usable voice channel" : "Add the bot to a server first")); chanHint.textContent = !channels.length && data.channels.length ? t("The bot sees channels here but can't join and speak in any; add it again with the link above.") : ""; }
    else if ((data.guilds || []).length) loadOptions(guildId); else fill(chans, [], "", t("Add the bot to a server first"));
    rest.hidden = false;
  };
  const call = (route, fallback) => (forRequest ? post(...route) : post(...fallback));
  const loadOptions = async g => { try { showOptions(await call(flow ? flow.options(g) : null, ["/api/discord/options", {guild_id: g || ""}])); } catch (e) { status.textContent = e.message; } };
  tokenForm.addEventListener("submit", async e => {
    e.preventDefault(); tokenGo.disabled = true; status.textContent = t("Checking the token with Discord…");
    const value = token.value; token.value = "";
    try {
      const data = await call(flow ? flow.token(value) : null, ["/api/discord/token", {value}]);
      status.textContent = t(forRequest ? "Token saved. Now add the bot to your server." : "Token saved on this computer. Now add the bot to your server.");
      s1.hidden = true; showOptions(forRequest ? flow.summary(data) : data);
    }
    catch (err) { status.textContent = err.message; tokenGo.disabled = false; }
  });
  refresh.addEventListener("click", () => loadOptions(guilds.value)); guilds.addEventListener("change", () => loadOptions(guilds.value));
  // Back from Discord's invite page: look again, so the new server shows without a refresh.
  invite.addEventListener("click", () => window.addEventListener("focus", () => loadOptions(guilds.value), {once: true}));
  // A request whose bot token is already saved goes straight to the server step.
  if (forRequest && state.request.step === "discord_place") { s1.hidden = true; showOptions(flow.summary(state.request)); loadOptions(""); }
  finish.addEventListener("click", async () => {
    const id = owner.value.trim(); if (!guilds.value || !chans.value || !/^[0-9]{1,20}$/.test(id)) { status.textContent = discordFailure("discord_choices"); return; }
    finish.disabled = true; status.textContent = t("Writing the profile…");
    try {
      if (forRequest) { await call(flow.finish(guilds.value, chans.value, id), null); await reload(); return; }
      const data = await post("/api/discord/finish", {profile: pname.value.trim(), guild_id: guilds.value, channel_id: chans.value, operator_id: id});
      // The bot is its own profile: open it, where its Start button is.
      await api("/api/profile", {profile: data.profile}); openService = "";
      location.hash = "#/discord"; await reload();
      toast(t("Created {profile} and switched to it. Start the bot here.", {profile: data.profile}));
    } catch (err) { status.textContent = err.message; finish.disabled = false; }
  });
  return c;
}

// Steps 2-4 for this profile's own bot: its token is already saved.
function discordExistingFlow() {
  const body = el("div", "discord"); const flow = el("div", "flow"); body.append(flow);
  const status = el("p", "status", t("Asking Discord which servers the bot is in…"));
  const botName = el("strong", "", t("your bot")); const invite = el("a", "btn ghost small invite", ""); invite.target = "_blank"; invite.rel = "noopener noreferrer";
  const introText = el("p", "hint"); introText.append(...around(t("If {bot} isn't in your server yet, invite it, then refresh."), botName));
  const refresh = el("button", "btn ghost small", t("Refresh")); refresh.type = "button";
  const guilds = el("select"); const chans = el("select"); const chanHint = el("p", "hint");
  const ownerHelp = steps(OWNER_HELP);
  const owner = input(t("Your Discord user ID (a long number)")); owner.inputMode = "numeric"; owner.value = state.discord.operator_ids || "";
  const lock = el("button", "btn", t("Lock the bot to this channel and me")); lock.type = "button";
  const serverRow = el("div", "row-actions"); serverRow.append(invite, refresh);
  flow.append(stepTitle(1, "The server"), introText, boxed(guilds), serverRow,
    stepTitle(2, "The voice channel"), boxed(chans), chanHint,
    stepTitle(3, "Only you"), el("p", "hint", t("Only this Discord account will be able to talk to your agent.")), ownerHelp, owner, lock, status);
  const fill = (select, items, keep, empty) => { select.replaceChildren(); if (!items.length) { const o = el("option", "", empty); o.value = ""; select.append(o); select.disabled = true; return; } select.disabled = false; for (const it of items) { const o = el("option", "", it.name); o.value = it.id; select.append(o); } select.value = items.some(i => i.id === keep) ? keep : items[0].id; };
  const post = async (path, payload) => { const r = await hostFetch(path, {method: "POST", cache: "no-store", headers: {"Content-Type": "application/json", "X-Settings-CSRF": csrf}, body: JSON.stringify(payload)}); const data = await r.json().catch(() => ({})); if (!r.ok) throw new Error(discordFailure(data.code)); return data; };
  const show = data => {
    const app = data.application || {}; botName.textContent = app.name || t("your bot");
    invite.textContent = app.name ? t("Add {name} to a server ↗", {name: app.name}) : t("Add the bot to a server ↗"); invite.href = app.invite_url && app.invite_url.startsWith("https://discord.com/") ? app.invite_url : "#";
    fill(guilds, data.guilds || [], data.guild_id || state.discord.guild_id, t("Not in any server yet"));
    const channels = (data.channels || []).filter(ch => ch.eligible);
    fill(chans, channels, (state.discord.channel_ids || "").split(",")[0], t((data.guilds || []).length ? "Pick a server first" : "Add the bot to a server first"));
    chanHint.textContent = data.channels && !channels.length && data.channels.length ? t("The bot sees channels here but can't join and speak in any; invite it again with the link to fix its permissions.") : "";
    status.textContent = "";
  };
  const load = async () => { try { show(await post("/api/discord/current", {})); } catch (e) { status.className = "status error"; status.textContent = e.message; } };
  const loadGuild = async g => { try { show(await post("/api/discord/options", {guild_id: g})); } catch (e) { status.className = "status error"; status.textContent = e.message; } };
  refresh.addEventListener("click", load); guilds.addEventListener("change", () => loadGuild(guilds.value));
  invite.addEventListener("click", () => window.addEventListener("focus", load, {once: true}));
  lock.addEventListener("click", async () => {
    const id = owner.value.trim(); if (!guilds.value || !chans.value || !/^[0-9]{1,20}$/.test(id)) { status.className = "status error"; status.textContent = discordFailure("discord_choices"); return; }
    lock.disabled = true; status.className = "status"; status.textContent = t("Checking the channel and saving…");
    try { await post("/api/discord/lock", {guild_id: guilds.value, channel_id: chans.value, operator_id: id}); toast(t("The bot is locked to that channel and to you.")); await reload(); }
    catch (e) { status.className = "status error"; status.textContent = e.message; lock.disabled = false; }
  });
  load();
  return body;
}

const KEY_FAILURES = {
  invalid_credential: "The service rejected this key. Copy a current key from its dashboard.",
  permission_denied: "This key lacks the permissions voice needs; create one with full access.",
  voice_unavailable: "That voice isn't available on this account.",
  quota_or_rate_limit: "This account is out of credit or rate limited.",
  network_timeout: "The check took too long. Try again.",
  storage_unavailable: "The credential store is locked; unlock it and retry.",
  key_not_found: "That key isn't on this machine any more.",
  unsupported: "That service or model can't be set up here.",
  validation_failed: "The service didn't pass the check. Check the key and its account, then try again.",
  realtime_no_transcript: "OpenAI Realtime connected but didn't transcribe the test clip. Check that this key's project can use gpt-realtime and gpt-4o-mini-transcribe.",
  realtime_settings_rejected: "OpenAI Realtime didn't accept the model, voice, or transcription settings for this key.",
  realtime_unreachable: "The computer running your agent couldn't reach OpenAI Realtime. Check its internet connection.",
  speech_failed: "OpenAI Speech didn't speak the test line. Check that this key's project can use gpt-4o-mini-tts.",
  local_english_only: "Local speech listens and speaks English only. Choose English under Language first, or pick another service.",
  local_runtime_missing: "The local speech runtime isn't installed on this computer yet. Install it first.",
  local_install_failed: "Installing the local speech runtime failed. Run the command shown in a terminal on this computer, then check again.",
  local_download_failed: "A model couldn't be downloaded. Check this computer's internet connection, then try again.",
  local_check_failed: "Local speech couldn't run a test turn on this computer. Nothing was changed; the log has the reason.",
  local_busy: "Local speech is already being set up. Wait for it to finish.",
};

// ---------- Agent: readiness and each agent's own questions ----------
const AGENT_FAILURES = {
  bad_workspace: "That folder doesn't exist on this computer. Use the full path to your agent's folder.",
  openclaw_token_missing: "No OpenClaw gateway token was found. Paste it, or choose no token.",
  model_key_required: "Paste this vendor's API key.",
};
let agentOpts = null;          // /api/agent: readiness per agent, vendors, what's saved
let openAgent = "";            // the agent card that is open on the Agent page
let agentDraft = null;         // answers for the open card
// A profile's agent type as the product it belongs to.
const productOf = type => ({acp: "hermes", hermes: "hermes", hermes_gateway: "hermes", "hermes-gateway": "hermes", openclaw_acp: "openclaw", cursor_acp: "cursor",
  command: "own", http: "own", bridge: "own"}[type] || type);
const OWN_KINDS = [["command", "A program on this machine"], ["acp", "An agent that speaks ACP"], ["http", "A web service"], ["bridge", "A remote Agent Bridge"], ["starter", "Create a starter agent"]];
async function loadAgentOptions() { agentOpts = await api("/api/agent"); return agentOpts; }
// forNew: a draft for a new profile. It can start from the open profile's
// choices, but never from its saved model key, which stays with that profile.
function freshDraft(id, forNew = false) {
  const cur = (agentOpts && agentOpts.current) || {};
  const vendor = cur.vendor || "openrouter";
  const vendorInfo = (agentOpts ? agentOpts.vendors : []).find(v => v.id === vendor);
  return {agent: id,
    hermes: {mode: cur.gateway_url ? "join" : "", url: cur.gateway_url || "", token: ""},
    openclaw: {token_source: agentOpts && agentOpts.openclaw_config_found ? "read" : "skip", token: "", memory: cur.gateway_session_id === "" && productOf(cur.type) === "openclaw" ? "fresh" : "main"},
    model: {vendor, base_url: "", model: cur.model || (vendorInfo ? vendorInfo.default_model : ""), key: "", keep: !forNew && !!cur.has_model_key},
    own: ownDraft(cur)};
}
function ownDraft(cur) {
  const o = (cur && cur.own) || {};
  const kind = cur && productOf(cur.type) === "own" ? (cur.type === "command" ? (o.protocol === "acp" ? "acp" : "command") : cur.type) : "starter";
  return {kind, command: o.command || "", env: o.env || "", url: o.url || "", token_env: o.token_env || "", token: "",
    permission: o.permission || "pending", language: "python", keep: false, failures: null, suggested: "", hint: ""};
}
function agentFailure(data) {
  const known = AGENT_FAILURES[data.code] || NEW_FAILURES[data.code] || KEY_FAILURES[data.code];
  if (known) return t(known);
  if (data.message) return data.message;
  return t("That didn't work. Try again.");
}
// Is this agent installed, and if not, what installing it takes.
function readinessBlock(a, onChange) {
  const box = el("div", "field");
  box.append(el("div", "field-label", t("On this computer")));
  if (a.ready && (a.id === "model" || a.id === "own")) return document.createDocumentFragment();  // nothing to install
  if (a.ready) { const ok = el("div", "saved"); ok.append(el("span", "dot"), el("span", "", t("Installed and ready"))); box.append(ok); return box; }
  const list = el("ul", "missing");
  for (const need of a.missing) { const li = el("li"); li.append(el("span", "", need.hint)); if (need.command) li.append(el("code", "cmd", need.command)); list.append(li); }
  box.append(list);
  const row = el("div", "row-actions");
  const out = el("p", "hint");
  if (a.missing.some(n => n.installable)) {
    const inst = el("button", "btn small", t("Install for me")); inst.type = "button";
    inst.title = t("Runs the commands above on this computer");
    inst.addEventListener("click", async e => {
      e.stopPropagation(); inst.disabled = true; out.textContent = t("Installing…");
      try { const r = await api("/api/agent/install", {agent: a.id}); Object.assign(a, {ready: r.ready, missing: r.missing}); out.textContent = (r.lines || []).join(" · "); onChange(); }
      catch (err) { out.textContent = err.message; inst.disabled = false; }
    });
    row.append(inst);
  }
  const again = el("button", "btn ghost small", t("Check again")); again.type = "button";
  again.addEventListener("click", async e => { e.stopPropagation(); await loadAgentOptions(); const fresh = agentOpts.agents.find(x => x.id === a.id); if (fresh) Object.assign(a, fresh); onChange(); });
  row.append(again);
  box.append(row, out, el("p", "hint", a.missing.some(n => n.installable) ? t("Install for me runs exactly these commands; nothing needs sudo.") : t("Run the line above in a terminal, then check again.")));
  return box;
}
// What the terminal asks after picking this agent.
function agentQuestions(id, draft, onChange) {
  const box = el("div", "agent-questions");
  if (id === "hermes") {
    const h = draft.hermes;
    const status = el("p", "hint");
    if (!h.found) {
      h.found = "checking";
      api("/api/agent/hermes", {}).then(r => { h.found = r; if (!h.mode) h.mode = r.status === "none" ? "local" : "join"; if (r.origin && !h.url) h.url = r.origin; onChange(); }).catch(() => { h.found = {status: "none"}; h.mode = h.mode || "local"; onChange(); });
    }
    const f = h.found === "checking" ? null : h.found;
    if (!f) { status.textContent = t("Looking for a running Hermes dashboard…"); box.append(field(t("How Sleeptalk reaches Hermes"), status)); return box; }
    const joinLabel = f.status === "none" ? t("Join a Hermes dashboard at another address") : t("Join your running Hermes ({origin})", {origin: f.origin});
    const seg = segments([{value: "local", label: t("Start Hermes for each call")}, {value: "join", label: f.status === "none" ? t("Join a dashboard") : t("Join the running one")}], h.mode, v => { h.mode = v; onChange(); });
    const parts = [seg];
    if (h.mode === "join") {
      parts.push(el("p", "hint", joinLabel));
      if (f.status === "none") { const url = input("http://127.0.0.1:9119"); url.value = h.url; url.addEventListener("input", () => { h.url = url.value; }); url.addEventListener("click", e => e.stopPropagation()); parts.push(url); }
      if (f.status !== "ready") { const tok = input(t("Session token (from the dashboard URL ?token=)")); tok.type = "password"; tok.value = h.token; tok.addEventListener("input", () => { h.token = tok.value; }); tok.addEventListener("click", e => e.stopPropagation()); parts.push(tok); }
      parts.push(el("p", "hint", t("Joining keeps your running Hermes' sessions and memory.")));
    } else parts.push(el("p", "hint", t("Sleeptalk starts Hermes' gateway for each call from your Hermes install, so its memory and settings apply. It falls back to `hermes acp` if the gateway can't start.")));
    box.append(field(t("How Sleeptalk reaches Hermes"), ...parts));
  }
  if (id === "openclaw") {
    const c = draft.openclaw;
    const opts = [];
    if (agentOpts && agentOpts.openclaw_config_found) opts.push({value: "read", label: t("Read it from ~/.openclaw")});
    opts.push({value: "paste", label: t("Paste the token")}, {value: "skip", label: t("No token")});
    const parts = [segments(opts, c.token_source, v => { c.token_source = v; onChange(); })];
    if (c.token_source === "paste") { const tok = input(t("OpenClaw gateway token")); tok.type = "password"; tok.value = c.token; tok.addEventListener("input", () => { c.token = tok.value; onChange(false); }); tok.addEventListener("click", e => e.stopPropagation()); parts.push(tok, el("p", "hint", t("Kept in .env beside your profiles, as terminal setup does."))); }
    if (c.token_source === "skip") parts.push(el("p", "hint", t("Uses `openclaw acp`; each call starts a fresh conversation.")));
    box.append(field(t("OpenClaw's gateway token"), ...parts));
    if (c.token_source !== "skip") box.append(field(t("What a call continues"), segments([{value: "main", label: t("Your main conversation")}, {value: "fresh", label: t("A fresh one each call")}], c.memory, v => { c.memory = v; onChange(); })));
    box.append(el("p", "hint", t("OpenClaw support is in preview.")));
  }
  if (id === "own" && HOST) {
    // A program to run is set up at the computer itself, never from a link.
    box.append(el("p", "hint warn", t("Your own agent is set up on the computer running it: run `sleeptalk settings` there.")));
  } else if (id === "own") {
    const o = draft.own;
    // Honest about the work: this connects an adapter, it doesn't write one.
    const guide = el("p", "hint", t("This isn't plug and play. An agent that already speaks ACP works with its command. Anything else needs a small adapter that follows Sleeptalk's contract, usually a short script or one web route. The check below tells you whether it works."));
    const read = el("a", "", t("Read the guide ↗")); read.href = "https://github.com/sleeptalklabs/sleeptalk/blob/main/docs/bring-your-own-agent.md"; read.target = "_blank"; read.rel = "noopener noreferrer";
    guide.append(" ", read); box.append(guide);
    const kind = selectBox(OWN_KINDS.map(([v, l]) => ({value: v, label: t(l)})), o.kind, v => { o.kind = v; o.failures = null; onChange(); }, t("What kind of agent is it?"));
    kind.addEventListener("click", e => e.stopPropagation());
    box.append(field(t("What kind of agent is it?"), kind, el("p", "hint", t({
      command: "Sleeptalk starts it and talks JSON lines over its input and output; your program has to follow that contract.",
      acp: "Gemini CLI, Goose, Kiro, Qwen Code, Cline, and others: give the command that starts its ACP mode.",
      http: "Each turn is a POST to your service in Sleeptalk's format, and it streams the reply back; your service has to follow that contract.",
      bridge: "A long-lived remote agent that needs permissions, steering, or reconnects.",
      starter: "Creates a small working agent next to your profiles; edit its reply function to call your model.",
    }[o.kind]))));
    const text = (placeholder, key, type = "text") => { const i = input(placeholder); i.type = type; i.value = o[key]; i.addEventListener("input", () => { o[key] = i.value; onChange(false); }); i.addEventListener("click", e => e.stopPropagation()); return i; };
    if (o.kind === "starter") box.append(field(t("Language"), segments([{value: "python", label: "Python"}, {value: "node", label: "Node"}], o.language, v => { o.language = v; onChange(); })));
    if (o.kind === "command" || o.kind === "acp") {
      box.append(field(t("Command that starts it"), text(o.kind === "acp" ? "gemini --experimental-acp" : "node agent.mjs", "command")),
        field(t("Environment variables it needs"), text("OPENAI_API_KEY, MY_TOKEN", "env"), el("p", "hint", t("Names only, comma-separated; the values stay in your environment or .env."))));
    }
    if (o.kind === "acp") box.append(field(t("When it wants to run a tool"), segments([{value: "pending", label: t("Ask me")}, {value: "deny", label: t("Always refuse")}], o.permission, v => { o.permission = v; onChange(); })));
    if (o.kind === "http" || o.kind === "bridge") {
      box.append(field(t("Agent URL"), text(o.kind === "http" ? "https://agent.example.com/sleeptalk/turn" : "wss://agent.example.com/v1/agent-bridge", "url")),
        field(t("Token variable name"), text(o.kind === "bridge" ? "AGENT_BRIDGE_TOKEN" : t("Empty for none"), "token_env")),
        o.token_env ? field(t("Token value"), text(t("Stored in .env under that name"), "token", "password")) : null);
    }
    if (o.failures) {
      const f = el("div", "field");
      f.append(el("div", "field-label", t("The check didn't pass")));
      const ul = el("ul", "missing"); for (const line of o.failures) ul.append(el("li", "", line)); f.append(ul);
      if (o.hint) f.append(el("p", "hint", o.hint));
      const row = el("div", "row-actions");
      if (o.suggested) { const use = el("button", "btn small", t("Use the suggested command")); use.type = "button"; use.addEventListener("click", e => { e.stopPropagation(); o.kind = "acp"; o.command = o.suggested; o.failures = null; onChange(); }); row.append(use); }
      f.append(row); box.append(f);
    }
    const keep = el("label", "check-row"); const cb = el("input"); cb.type = "checkbox"; cb.checked = o.keep;
    cb.addEventListener("change", () => { o.keep = cb.checked; }); keep.addEventListener("click", e => e.stopPropagation());
    keep.append(cb, el("span", "", t("Save even if the check fails")));
    box.append(keep, el("p", "hint", t("Saving checks the agent starts and answers the contract, without sending it a prompt.")));
  }
  if (id === "model") {
    const m = draft.model;
    const vendors = (agentOpts ? agentOpts.vendors : []);
    const vendor = vendors.find(v => v.id === m.vendor) || vendors[0];
    const cur = (agentOpts && agentOpts.current) || {};
    box.append(el("p", "hint", t("Just a model: it talks and remembers this conversation, and nothing else. No tools, no files.")));
    const vbox = selectBox(vendors.map(v => ({value: v.id, label: v.label})), m.vendor, v => { m.vendor = v; const info = vendors.find(x => x.id === v); m.model = info ? info.default_model : ""; m.keep = !!cur.has_model_key && cur.vendor === v; m.reuse = undefined; m.list = null; onChange(); }, t("Vendor"));
    vbox.addEventListener("click", e => e.stopPropagation());
    box.append(field(t("Model vendor"), vbox));
    if (m.vendor === "other") { const url = input("https://…/v1"); url.value = m.base_url; url.addEventListener("input", () => { m.base_url = url.value; }); url.addEventListener("click", e => e.stopPropagation()); box.append(field(t("Endpoint base URL"), url, el("p", "hint", t("An OpenAI-compatible HTTPS endpoint; most end in /v1.")))); }
    // The vendor's models, searchable by typing; any name can still be typed.
    const needsKey = m.vendor === "openai" || m.vendor === "anthropic";
    if (m.vendor !== "other" && !m.list && (!needsKey || m.keep || m.key.trim())) {
      m.list = "loading";
      api("/api/agent/models", {vendor: m.vendor, key: m.keep ? "" : m.key.trim()})
        .then(r => { m.list = r.models; if (m.vendor === "ollama" && r.models.length && !r.models.includes(m.model)) m.model = r.models[0]; onChange(); })
        .catch(() => { m.list = []; onChange(); });
    }
    const listed = Array.isArray(m.list) ? m.list : [];
    const modelField = input(vendor ? vendor.default_model : "");
    modelField.value = m.model; modelField.autocomplete = "off"; modelField.spellcheck = false;
    modelField.addEventListener("input", () => { m.model = modelField.value; });
    modelField.addEventListener("click", e => e.stopPropagation());
    let options = null;
    if (listed.length) {
      options = el("datalist"); options.id = "model-options";
      for (const name of listed) { const o = el("option"); o.value = name; options.append(o); }
      modelField.setAttribute("list", options.id);
    }
    const note = m.list === "loading" ? el("p", "hint", t("Loading models…"))
      : listed.length ? el("p", "hint", t("Type to search {n} models", {n: listed.length}))
      : m.vendor === "ollama" && Array.isArray(m.list) && !m.list.length ? el("p", "hint warn", t("Ollama isn't answering on this computer; start it, or type a model name.")) : null;
    box.append(field(t("Model"), modelField, options, note));
    if (m.vendor !== "ollama") {
      const parts = [];
      const found = modelKeySource(draft);
      if (m.keep) { const saved = el("div", "saved"); saved.append(el("span", "dot"), el("span", "", t("Saved in this computer's private store"))); const rep = el("button", "linkbtn", t("Replace")); rep.type = "button"; rep.addEventListener("click", e => { e.stopPropagation(); m.keep = false; m.reuse = false; onChange(); }); saved.append(rep); parts.push(saved); }
      else if (found && m.reuse !== false) { const saved = el("div", "saved"); saved.append(el("span", "dot"), el("span", "", found)); const other = el("button", "linkbtn", t("Use a different key")); other.type = "button"; other.addEventListener("click", e => { e.stopPropagation(); m.reuse = false; onChange(); }); saved.append(other); parts.push(saved); }
      else { const key = el("input"); key.type = "password"; key.autocomplete = "off"; key.maxLength = 8192; key.placeholder = t("API key"); key.value = m.key; key.addEventListener("input", () => { m.key = key.value; onChange(false); }); if (needsKey) key.addEventListener("change", () => { if (m.key.trim()) { m.list = null; onChange(); } }); key.addEventListener("click", e => e.stopPropagation()); const kf = el("div", "keyfield"); kf.append(key); parts.push(kf); }
      const hint = el("p", "hint", t("Checked with the vendor, then kept in this computer's private store; the profile only references it."));
      if (vendor && vendor.console_url) { hint.append(" "); const a = el("a", "", t("Get a key ↗")); a.href = vendor.console_url; a.target = "_blank"; a.rel = "noopener noreferrer"; hint.append(a); }
      parts.push(hint);
      box.append(field(t("API key"), ...parts));
    }
  }
  return box;
}
// A key for the chosen model vendor already on this computer, said as one sentence; "" when none.
function modelKeySource(draft) {
  const v = draft.model.vendor;
  const info = (agentOpts ? agentOpts.vendors : []).find(x => x.id === v);
  const vendor = info ? info.label.split(" — ")[0] : v;
  if (wiz && draft === wiz.agentDraft) {
    if (wiz.provider === v) return t("Uses the {vendor} key you gave for speech", {vendor});
    const f = wiz.opts && wiz.opts.reusable && wiz.opts.reusable[v];
    return f ? t("Uses your {vendor} key from {source}", {vendor, source: f.source}) : "";
  }
  const r = agentOpts && agentOpts.reusable_model_keys && agentOpts.reusable_model_keys[v];
  if (!r) return "";
  return r.source === "speech" ? t("Uses the {vendor} key speech already uses", {vendor}) : t("Uses your {vendor} key from {source}", {vendor, source: r.label});
}
const modelReuse = draft => { const m = draft.model; return !m.keep && !m.key.trim() && m.reuse !== false && !!modelKeySource(draft); };
// Whether this agent's answers are complete enough to save.
function agentDraftReady(a, draft) {
  if (!a || !a.ready) return false;
  if (a.id === "openclaw" && draft.openclaw.token_source === "paste" && !draft.openclaw.token.trim()) return false;
  if (a.id === "hermes" && (!draft.hermes.found || draft.hermes.found === "checking")) return false;
  if (a.id === "model" && draft.model.vendor !== "ollama" && !draft.model.keep && !draft.model.key.trim() && !modelReuse(draft)) return false;
  if (a.id === "own") { const o = draft.own; if ((o.kind === "command" || o.kind === "acp") && !o.command.trim()) return false; if ((o.kind === "http" || o.kind === "bridge") && !o.url.trim()) return false; }
  return true;
}
function agentBody(id, draft) {
  const body = {agent: id};
  if (id === "hermes") body.hermes = {mode: draft.hermes.mode || "local", url: draft.hermes.url || "", token: draft.hermes.token || ""};
  if (id === "openclaw") body.openclaw = {token_source: draft.openclaw.token_source, token: draft.openclaw.token || "", memory: draft.openclaw.memory};
  if (id === "model") body.model = {vendor: draft.model.vendor, base_url: draft.model.base_url || "", model: draft.model.model || "", key: draft.model.key || "", keep: !!draft.model.keep, reuse: modelReuse(draft)};
  if (id === "own") { const o = draft.own; body.own = {kind: o.kind, command: o.command, env: o.env, url: o.url, token_env: o.token_env, token: o.token, permission: o.permission, language: o.language, keep: !!o.keep}; }
  return body;
}
function agentPageDetail(a, inUse) {
  const d = el("div", "detail");
  d.addEventListener("click", e => e.stopPropagation());
  const rerender = (full = true) => { if (full) renderPage(); else { const b = d.querySelector(".agent-save"); if (b) b.disabled = !agentDraftReady(a, agentDraft); } };
  d.append(readinessBlock(a, rerender));
  if (a.ready) d.append(agentQuestions(a.id, agentDraft, rerender));
  if (inUse) {
    const ws = input(t("Full path to a folder")); ws.value = state.agent.workspace || "";
    const save = el("button", "btn ghost small", t("Move")); save.type = "button";
    const out = el("p", "hint", t("The folder this agent works in."));
    save.addEventListener("click", async () => {
      save.disabled = true;
      const r = await hostFetch("/api/agent/workspace", {method: "POST", cache: "no-store", headers: {"Content-Type": "application/json", "X-Settings-CSRF": csrf}, body: JSON.stringify({workspace: ws.value})});
      const data = await r.json().catch(() => ({}));
      if (r.ok) { toast(t("Workspace saved.")); await reload(); } else { out.className = "hint warn"; out.textContent = agentFailure(data); save.disabled = false; }
    });
    const row = el("div", "row-actions"); row.append(ws, save);
    d.append(field(t("Workspace"), row, out));
  }
  const actions = el("div", "actions");
  const status = el("p", "status");
  const go = el("button", "btn agent-save", inUse ? t("Save") : t("Switch to {name}", {name: t(a.label.split(" — ")[0])})); go.type = "button";
  go.disabled = !agentDraftReady(a, agentDraft);
  if (inUse && !["hermes", "openclaw", "model", "own"].includes(a.id)) go.hidden = true;
  go.addEventListener("click", async () => {
    go.disabled = true; status.className = "status"; status.textContent = t("Saving…");
    const body = agentBody(a.id, agentDraft);
    const r = await hostFetch("/api/agent", {method: "POST", cache: "no-store", headers: {"Content-Type": "application/json", "X-Settings-CSRF": csrf}, body: JSON.stringify(body)});
    if (body.model) body.model.key = "";
    const data = await r.json().catch(() => ({}));
    if (!r.ok && data.code === "check") { Object.assign(agentDraft.own, {failures: data.failures || [], suggested: data.suggested || "", hint: data.hint || ""}); renderPage(); return; }
    if (!r.ok) { status.className = "status error"; status.textContent = agentFailure(data); go.disabled = false; return; }
    agentDraft.model.key = ""; agentDraft.own.token = "";
    toast(data.starter ? t("Starter agent created at {path}. Edit its reply function to call your model.", {path: data.starter})
      : data.note || t("{name} is now this profile's agent.", {name: t(a.label.split(" — ")[0])}));
    openAgent = ""; agentOpts = null; await reload();
  });
  actions.append(go, status); d.append(actions);
  return d;
}

// ---------- An agent's key request (host input link), answered here ----------
// The same cards, key field, models, and voices as New profile, for just the
// services the agent asked about; saving goes through the request's checks,
// and a Discord bot then walks through the same steps as the Discord page.
let req = null;
function requestPage() {
  const rq = state.request; const page = el("div");
  const repair = rq.mode === "repair"; const discord = rq.surface === "discord" && !repair;
  if (rq.step === "done") return requestDone(page, rq);
  page.append(pageHead(
    repair ? t("Replace a voice key") : discord ? t("Set up your Discord bot") : rq.choice === "open" ? t("Pick a voice service") : t("Add your voice key"),
    repair ? t("The new key is checked, then replaces the saved one. Your models and voice stay.")
      : discord ? t("Your agent will talk in a Discord voice channel only you can use. First the voice, then the bot: about five minutes.")
      : t("Your agent asked for this so you can talk to it. The key is checked with its service, then kept on your agent's computer. It never passes through the chat.")));
  if (rq.step === "discord_token" || rq.step === "discord_place") { page.append(discordSetupCard("new", REQUEST_DISCORD)); return page; }
  // Speech: one wizard over the request's services.
  const services = rq.options.services;
  if (!req || req.rq !== rq.profile + rq.mode) {
    const first = services.find(sv => sv.id === rq.preferred) || services.find(sv => rq.missing.includes(sv.id)) || services[0];
    req = {rq: rq.profile + rq.mode, step: 0};
    wiz = {step: 0, name: rq.profile, workspace: "", agent: "", provider: "", value: "", reuse: false, stt: "", tts: "", voice: "",
      surface: rq.surface, opts: rq.options, lang: rq.options.language, done: ""};
    wizPickService(first.id);
    if (rq.voice_locked) wiz.voice = rq.voice_locked;
  } else wiz.opts = rq.options;
  const steps = [["speech", "Speech service"], ...(repair || rq.voice_locked ? [] : [["voice", "Voice"]])];
  if (steps.length > 1) {
    const bar = el("ol", "wiz-steps");
    steps.forEach(([id, label], i) => {
      const li = el("li", i === req.step ? "current" : i < req.step ? "done" : "");
      const b = el("button", "", ""); b.type = "button"; b.disabled = i > req.step;
      b.append(el("span", "n", String(i + 1)), el("span", "", t(label)));
      b.addEventListener("click", () => { req.step = i; renderPage(); });
      li.append(b); bar.append(li);
    });
    page.append(bar);
  }
  const [stepId] = steps[req.step];
  const body = el("div", "wiz-body"); body.append(WIZ[stepId]()); page.append(body);
  const actions = el("div", "wiz-actions");
  const back = el("button", "btn ghost", t("Back")); back.type = "button"; back.hidden = req.step === 0;
  back.addEventListener("click", () => { req.step -= 1; renderPage(); });
  const status = el("p", "status");
  const last = req.step === steps.length - 1;
  const next = el("button", "btn", last ? (repair ? t("Replace the key") : t("Save")) : t("Continue")); next.type = "button";
  next.disabled = !wizReady("speech");
  next.addEventListener("click", async () => {
    if (!last) { req.step += 1; renderPage(); window.scrollTo(0, 0); return; }
    next.disabled = true; back.disabled = true; status.className = "status";
    status.textContent = t("Checking your key with {name}: it listens to a short clip and speaks one line (a little credit)…", {name: wizService().name});
    const bodyJson = {provider: wiz.provider, voice_id: rq.voice_locked || wiz.voice, stt_model: repair ? "" : wiz.stt, tts_model: repair ? "" : wiz.tts};
    if (wiz.reuse) bodyJson.reuse = true; else bodyJson.value = wiz.value;
    try {
      const r = await hostFetch("/api/request/key", {method: "POST", cache: "no-store", headers: {"Content-Type": "application/json", "X-Settings-CSRF": csrf}, body: JSON.stringify(bodyJson)});
      bodyJson.value = ""; wiz.value = "";
      const data = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(data.code === "detail" ? data.message : t(KEY_FAILURES[data.code] || REQUEST_FAILURES[data.code] || "The service rejected this key. Copy a current key from its dashboard."));
      state.request = data; req = null;
      if (data.step === "done") { await reload(); } else { renderChrome(); renderPage(); window.scrollTo(0, 0); }
    } catch (e) { status.className = "status error"; status.textContent = e.message; next.disabled = false; back.disabled = false; }
  });
  actions.append(back, status, next); page.append(actions);
  return page;
}
const REQUEST_FAILURES = {
  request_ended: "This request has ended. Ask your agent for a new link.",
  invalid_submission: "That didn't go through. Check the key and try again.",
  not_ready: "Save the bot token first.",
  discord_unavailable: "Discord didn't answer. Try again in a moment.",
};
// The Discord card's calls, pointed at the agent's request instead of a new Discord profile.
const REQUEST_DISCORD = {
  token: value => ["/api/request/key", {provider: "discord", value}],
  options: guild => ["/api/request/discord", {guild_id: guild || ""}],
  finish: (guild, channel, owner) => ["/api/request/finish", {guild_id: guild, channel_id: channel, operator_id: owner}],
  summary: data => (data.discord ? {...data.discord, ...data} : data),
};
function requestDone(page, rq) {
  page.append(pageHead(t("All set"), rq.surface === "discord"
    ? t("Your bot is ready. In Discord, join your voice channel, then type {command} in any chat in that server.", {command: `/${rq.profile} join`})
    : rq.mode === "repair" ? t("The new key is saved. Your agent can call you again.")
    : t("Voice is set up. Go back to your chat: your agent can call you now.")));
  if (state.profile) {
    const more = el("button", "btn ghost", t("Adjust other settings")); more.type = "button";
    more.addEventListener("click", () => { state.request = null; location.hash = "#/speech"; renderChrome(); renderPage(); });
    page.append(more);
  }
  return page;
}

// ---------- New profile (the terminal wizard's steps, in the page) ----------
const NEW_FAILURES = {
  profile_exists: "A profile with that name already exists; choose another name.",
  bad_profile_name: "Profile names use letters, digits, and hyphens, up to 64 characters.",
  bad_workspace: "That folder doesn't exist on this computer. Use the full path to your agent's folder.",
};
const NEW_STEPS = [["agent", "Agent and workspace"], ["speech", "Speech service"], ["voice", "Voice"], ["review", "Start call"]];
let wiz = null;
const nameTaken = slug => wiz.opts.existing.includes(slug) || (wiz.opts.reserved || []).includes(slug);
const slugOf = name => name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");

async function wizOptions(language) {
  const opts = await api(`/api/new?language=${encodeURIComponent(language)}`);
  wiz.opts = opts; wiz.lang = opts.language;
  if (!wiz.name) wiz.name = opts.suggested_name;
  if (!wiz.workspace) wiz.workspace = opts.workspace;
  if (!wiz.agent) wiz.agent = (opts.agents.find(a => a.ready) || {}).id || opts.found_agents[0] || "omp";
  const order = wizServices().filter(sv => !sv.english_only);
  if (!wiz.provider || !order.some(sv => sv.id === wiz.provider)) wiz.provider = (order[0] || wizServices()[0]).id;
  wizPickService(wiz.provider, true);
}
// Services with a key already on this computer first, then Local (no key,
// nothing to sign up for), then the usual order.
function wizServices() {
  const pos = sv => { const i = SERVICE_ORDER.indexOf(sv.id); return i < 0 ? SERVICE_ORDER.length : i; };
  const rank = sv => wiz.opts.reusable[sv.id] ? 0 : sv.id === "local" ? 1 : 2;
  return [...wiz.opts.services].sort((a, b) => rank(a) - rank(b) || pos(a) - pos(b));
}
function wizService() { return wiz.opts.services.find(sv => sv.id === wiz.provider); }
// A service's recommended models and voice for the chosen language.
function wizPickService(id, keepKey) {
  wiz.provider = id;
  const sv = wizService();
  wiz.stt = sv.models.stt.recommended; wiz.tts = sv.models.tts.recommended; wiz.voice = sv.voice;
  if (!keepKey) wiz.value = "";
  wiz.reuse = !!wiz.opts.reusable[id] && !wiz.value;
  wiz.localReady = false;
}
function wizVoiceIds() {
  const sv = wizService(); const model = modelById(sv, "tts", wiz.tts);
  let ids = (wiz.opts.voices_by_service[sv.id] || {voices: []}).voices.filter(forModel(sv, model)).map(v => v.voice_id);
  if (model && Array.isArray(model.voices)) ids = model.voices.filter(id => ids.includes(id)).concat(ids.length ? [] : model.voices);
  if (model && model.model_is_voice && !Array.isArray(model.voices)) { const own = ids.filter(id => id.startsWith(model.id + "-")); if (own.length) ids = own; }
  if (wiz.voice && !ids.includes(wiz.voice) && (!model || !Array.isArray(model.voices) || model.voices.includes(wiz.voice))) ids = [wiz.voice, ...ids];
  return ids;
}
function wizReady(step) {
  if (step === "name") return !!wiz.name.trim() && !!slugOf(wiz.name) && !nameTaken(slugOf(wiz.name)) && !!wiz.workspace.trim();
  if (step === "agent") { if (!wizReady("name")) return false; const a = agentOpts && agentOpts.agents.find(x => x.id === wiz.agent); return !!a && !!wiz.agentDraft && agentDraftReady(a, wiz.agentDraft); }
  if (step === "speech") {
    const sv = wizService();
    if (sv && sv.id === "local") return !sv.english_only && !!wiz.localReady;
    return wiz.reuse || wiz.value.trim().length > 0;
  }
  return true;
}

function newProfilePage() {
  const page = el("div");
  page.append(pageHead(t("New profile"), t("Set up a profile from scratch: the same steps as `sleeptalk` in a terminal. Nothing is saved until you create it.")));
  if (!wiz) wiz = {step: 0, name: "", workspace: "", agent: "", provider: "", value: "", reuse: false, stt: "", tts: "", voice: "",
    surface: "web", delivery: state.here === false ? (state.delivery ? state.delivery.value : "relay") : "local", capture: "speakers", opts: null, done: "",
    lang: state.profile ? state.language.value : (Object.keys(LANG_NAMES).find(l => primary(l) === uiLang) || "en-US")};
  if (wiz.done) return wizDone(page);
  if (!wiz.opts) {
    page.append(el("p", "status", t("Loading…")));
    wizOptions(wiz.lang).then(renderPage).catch(e => toast(e.message, true));
    return page;
  }
  // Steps: done ones can be revisited.
  const bar = el("ol", "wiz-steps");
  NEW_STEPS.forEach(([id, label], i) => {
    const li = el("li", i === wiz.step ? "current" : i < wiz.step ? "done" : "");
    const b = el("button", "", ""); b.type = "button"; b.disabled = i > wiz.step;
    b.append(el("span", "n", String(i + 1)), el("span", "", t(label)));
    b.addEventListener("click", () => { wiz.step = i; renderPage(); });
    li.append(b); bar.append(li);
  });
  page.append(bar);
  const [stepId] = NEW_STEPS[wiz.step];
  const body = el("div", "wiz-body");
  body.append(WIZ[stepId]());
  page.append(body);
  const actions = el("div", "wiz-actions");
  const back = el("button", "btn ghost", t("Back")); back.type = "button"; back.hidden = wiz.step === 0;
  back.addEventListener("click", () => { wiz.step -= 1; renderPage(); });
  const status = el("p", "status");
  const next = el("button", "btn", stepId === "review" ? t("Create profile") : t("Continue")); next.type = "button";
  next.disabled = !wizReady(stepId);
  next.addEventListener("click", async () => {
    if (stepId !== "review") { wiz.step += 1; renderPage(); window.scrollTo(0, 0); return; }
    next.disabled = true; back.disabled = true; status.className = "status";
    const localSpeech = wiz.provider === "local";
    status.textContent = localSpeech ? t("Starting…") : wiz.tts === "gpt-live-1" ? t("Checking your key by opening and closing a Live session…") : t("Checking your key with {name}: it listens to a short clip and speaks one line…", {name: wizService().name});
    const bodyJson = {name: wiz.name.trim(), workspace: wiz.workspace.trim(), agent: wiz.agent, language: wiz.lang, provider: wiz.provider,
      voice: wiz.voice, stt_model: wiz.stt, tts_model: wiz.tts, surface: wiz.surface,
      delivery: wiz.surface === "web" ? wiz.delivery : "", capture_mode: wiz.surface === "terminal" ? wiz.capture : ""};
    if (localSpeech) { /* no key */ } else if (wiz.reuse) bodyJson.reuse = true; else bodyJson.value = wiz.value;
    Object.assign(bodyJson, agentBody(wiz.agent, wiz.agentDraft || freshDraft(wiz.agent, true)));
    try {
      const r = await hostFetch("/api/new", {method: "POST", cache: "no-store", headers: {"Content-Type": "application/json", "X-Settings-CSRF": csrf}, body: JSON.stringify(bodyJson)});
      bodyJson.value = "";
      const data = await r.json().catch(() => ({}));
      if (!r.ok) {
        if (data.code === "check" && wiz.agentDraft) { Object.assign(wiz.agentDraft.own, {failures: data.failures || [], suggested: data.suggested || "", hint: data.hint || ""}); throw new Error(t("Your agent didn't pass its check. Go back to Agent to see why, or save it anyway there.")); }
        throw new Error(data.code === "setup" ? t("Setup stopped: {detail}", {detail: data.message || ""}) : agentFailure(data));
      }
      if (localSpeech) {
        // The models download and a turn is tested on this computer first.
        const job = await followLocalJob(status);
        if (job.stage !== "done") {
          const code = String(job.code || "");
          throw new Error(code.startsWith("setup:") ? t("Setup stopped: {detail}", {detail: code.slice(6)})
            : (KEY_FAILURES[code] && t(KEY_FAILURES[code])) || job.message || agentFailure({code}));
        }
        data.profile = job.profile;
      }
      wiz.value = ""; if (wiz.agentDraft) wiz.agentDraft.model.key = ""; wiz.done = data.profile; agentOpts = null;
      await reload(); location.hash = "#/new";
    } catch (e) { status.className = "status error"; status.textContent = e.message; next.disabled = false; back.disabled = false; }
  });
  actions.append(back, status, next);
  page.append(actions);
  if (stepId === "review" && wiz.provider === "local") {
    const downloads = el("div"); body.append(downloads);
    localSetup(downloads, {stt: wiz.stt, tts: wiz.tts, voice: wiz.voice}, (ready, info) => {
      next.disabled = !ready;
      next.textContent = localActionLabel(info, t("Create profile"), t("Download and create profile"));
    });
  }
  return page;
}

function wizChoice(title, id, {facts = [], badges = [], hint = "", chosen = false, disabled = false, onPick}) {
  const c = el("div", "pcard choice" + (chosen ? " in-use" : "") + (disabled ? " disabled" : ""));
  c.setAttribute("role", "radio"); c.setAttribute("aria-checked", String(chosen)); c.tabIndex = disabled ? -1 : 0;
  const top = el("div", "pcard-top");
  const name = el("div", "pcard-name"); name.append(mark(title, id), el("span", "", title), ...badges);
  top.append(name, el("div", "credit"));
  if (facts.length) { const f = el("div", "facts"); for (const [k, v] of facts) { const d = el("div", "fact"); if (k) d.append(el("span", "k", k)); d.append(el("span", "v", v)); f.append(d); } top.append(f); }
  c.append(top);
  if (hint) c.append(el("p", "hint card-hint", hint));
  if (!disabled) { c.addEventListener("click", onPick); c.addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onPick(); } }); }
  return c;
}

const WIZ = {
  name() {
    const d = el("div");
    const nameIn = input(t("Profile name")); nameIn.value = wiz.name; nameIn.maxLength = 64;
    const slugHint = el("p", "hint");
    const showSlug = () => {
      const slug = slugOf(nameIn.value);
      slugHint.className = "hint" + (nameTaken(slug) ? " warn" : "");
      slugHint.textContent = !slug ? t("Use letters or digits.")
        : (wiz.opts.reserved || []).includes(slug) ? t("The name {slug} is reserved; choose another.", {slug})
        : wiz.opts.existing.includes(slug) ? t("A profile named {slug} already exists.", {slug}) : t("Saved as {slug}.", {slug});
    };
    nameIn.addEventListener("input", () => { wiz.name = nameIn.value; showSlug(); document.querySelector(".wiz-actions .btn:not(.ghost)").disabled = !wizReady("agent"); });
    showSlug();
    const ws = input(t("Full path to a folder")); ws.value = wiz.workspace;
    ws.addEventListener("input", () => { wiz.workspace = ws.value; document.querySelector(".wiz-actions .btn:not(.ghost)").disabled = !wizReady("agent"); });
    d.append(field(t("Profile name"), nameIn, slugHint),
      field(t("Workspace"), ws, el("p", "hint", t("The folder your agent works in. Calls you start from this folder use this profile."))));
    return d;
  },
  language() {
    const d = el("div");
    const box = selectBox(Object.keys(LANG_NAMES).filter(l => wiz.opts.languages.includes(l)).map(l => ({value: l, label: LANG_NAMES[l]})), wiz.lang, async v => {
      try {
        await wizOptions(v);
        if (!state.profile && !pageOverride()) await useLocale(primary(v));
        renderChrome(); renderPage();
      } catch (e) { toast(e.message, true); }
    }, t("Language"));
    d.append(field(t("What you'll speak"), box, el("p", "hint", t("Sets the listening and speaking language, and picks the recommended models and voice for it."))));
    return d;
  },
  agent() {
    const d = el("div", "stack");
    d.append(WIZ.name());
    const language = el("details", "panel");
    language.append(el("summary", "", t("Language")), WIZ.language());
    d.append(language);
    if (!agentOpts) { d.append(el("p", "status", t("Checking which agents are installed…"))); loadAgentOptions().then(renderPage).catch(e => toast(e.message, true)); return d; }
    const found = new Set(wiz.opts.found_agents);
    const agents = [...agentOpts.agents].sort((x, y) => ((y.ready ? 2 : found.has(y.id) ? 1 : 0) - (x.ready ? 2 : found.has(x.id) ? 1 : 0)));
    for (const a of agents) {
      const title = a.label.split(" — ")[0];
      const badges = [a.id === "own" ? badge(t("Preview")) : a.ready ? badge(t("Ready on this computer"), "good") : found.has(a.id) ? badge(t("Found on this computer"), "good") : badge(t("Not installed yet"), "warn")];
      const card = wizChoice(t(title), a.id, {badges, chosen: wiz.agent === a.id, hint: t(AGENT_INFO[a.id] || ""),
        onPick: () => { if (wiz.agent !== a.id) { wiz.agent = a.id; wiz.agentDraft = freshDraft(a.id, true); } renderPage(); }});
      if (wiz.agent === a.id) {
        if (!wiz.agentDraft || wiz.agentDraft.agent !== a.id) wiz.agentDraft = freshDraft(a.id, true);
        const detail = el("div", "detail"); detail.addEventListener("click", e => e.stopPropagation());
        const rerender = (full = true) => { if (full) renderPage(); else document.querySelector(".wiz-actions .btn:not(.ghost)").disabled = !wizReady("agent"); };
        detail.append(readinessBlock(a, rerender));
        if (a.ready) detail.append(agentQuestions(a.id, wiz.agentDraft, rerender));
        card.append(detail);
      }
      d.append(card);
    }
    return d;
  },
  speech() {
    const d = el("div");
    d.append(keyLookupPanel(wiz.opts.key_lookup, wiz.opts.reusable, wiz.opts.services, r => {
      wiz.opts.reusable = r.reusable; wiz.opts.key_lookup = r.key_lookup;
      if (!wiz.opts.reusable[wiz.provider]) wiz.reuse = false;
      renderPage();
    }));
    const list = el("div", "stack");
    for (const sv of wizServices()) {
      const stt = modelById(sv, "stt", sv.models.stt.recommended); const tts = modelById(sv, "tts", sv.models.tts.recommended);
      const local = sv.id === "local";
      const badges = wiz.opts.reusable[sv.id] ? [badge(t("key found on this computer"), "good")] : local ? [badge(t("no key"), "good")] : [];
      const card = wizChoice(sv.name, sv.id, {badges, chosen: wiz.provider === sv.id, disabled: !!sv.english_only,
        hint: sv.english_only ? t("Listens and speaks English only for now. Choose English under Language to use it.") : "",
        facts: [[t("Listens with"), modelLabel(stt) || "—"], [t("Speaks with"), modelLabel(tts) || "—"],
          [t("Free to start"), local ? t("Unlimited, on this computer") : t((sv.free || "").split(/[:;]/)[0].trim()) || "—"]],
        onPick: () => { if (wiz.provider !== sv.id) { wizPickService(sv.id); renderPage(); } }});
      if (wiz.provider === sv.id && !sv.english_only) card.append(local ? wizLocalSetup(sv) : wizKeyAndModels(sv));
      list.append(card);
    }
    d.append(list);
    return d;
  },
  voice() {
    const sv = wizService(); const d = el("div");
    const fits = forModel(sv, modelById(sv, "tts", wiz.tts));
    const catalog = Object.fromEntries((wiz.opts.voices_by_service[sv.id] || {voices: []}).voices.filter(fits).map(v => [v.voice_id, v]));
    const samples = wiz.tts === "gpt-live-1" ? {} : Object.fromEntries(Object.entries((wiz.opts.samples || {})[sv.id] || {}).filter(([, v]) => fits(v)));
    const list = el("div", "voices");
    const ids = wizVoiceIds();
    for (const id of ids.slice(0, sv.id === "local" && wiz.tts === "pocket-tts-en" ? 60 : 40)) {
      const meta = catalog[id]; const sample = samples[id];
      const row = el("div", "voice" + (id === wiz.voice ? " on" : ""));
      const play = el("button", "play"); play.type = "button"; play.append(icon(PLAY)); play.disabled = !sample && !(sv.id === "local" && wiz.tts === "pocket-tts-en");
      if (!sample && !play.disabled) play.addEventListener("click", () => preview(play, wiz.tts, id, true));
      play.title = t(!play.disabled ? "Hear this voice" : "No recorded sample for this voice");
      if (sample) play.addEventListener("click", () => playUrl(play, sample.url));
      const name = el("span", "vname", meta ? meta.name : (sample && sample.name) || prettyVoice(id));
      const tag = el("span", "vtag", meta ? (meta.description || "").replace(/\.$/, "") : "");
      const use = el("button", "use" + (id === wiz.voice ? " current" : ""), t(id === wiz.voice ? "Chosen" : "Choose")); use.type = "button";
      use.addEventListener("click", () => { wiz.voice = id; renderPage(); });
      const right = el("span", "right"); right.append(tag, use);
      row.append(play, name, right); list.append(row);
    }
    if (!ids.length) list.append(el("p", "hint", t("This service picks its default voice; you can change it after the profile is created.")));
    d.append(field(t("Voices for {model}", {model: modelLabel(modelById(sv, "tts", wiz.tts)) || sv.name}), list,
      el("p", "hint", t(wiz.tts === "gpt-live-1" ? "Voice is fixed for each Live call. You can change it in settings before the next call." : "You can change the voice any time, from settings or during a call."))));
    if (sv.id === "local" && wiz.tts === "pocket-tts-en") d.append(pocketVoiceUpload(() => renderPage()));
    return d;
  },
  calls() {
    const d = el("div");
    const live = wiz.provider === "openai" && wiz.tts === "gpt-live-1";
    const surfaces = [{value: "web", label: t("In a browser")}];
    if (live) wiz.surface = "web";
    else surfaces.push({value: "terminal", label: t("In this terminal")});
    d.append(field(t("Where you'll talk"), segments(surfaces, wiz.surface,
      v => { wiz.surface = v; renderPage(); })));
    if (wiz.surface === "web") {
      const tn = state.tailnet || {available: false};
      const stack = el("div", "stack");
      stack.append(wizChoice(t("This computer"), "", {chosen: wiz.delivery === "local",
        facts: [[t("Works from"), t("this computer's browser")], [t("Limit"), t("none")]], onPick: () => { wiz.delivery = "local"; renderPage(); }}));
      stack.append(wizChoice(t("Hosted relay"), "", {chosen: wiz.delivery === "relay" || !wiz.delivery,
        facts: [[t("Works from"), t("anywhere")], [t("Limit"), t("30 minutes, announced")]], onPick: () => { wiz.delivery = "relay"; renderPage(); }}));
      stack.append(wizChoice(t("Your tailnet"), "", {chosen: wiz.delivery === "tailnet", disabled: !tn.available,
        facts: [[t("Works from"), t("devices on your tailnet")], [t("Limit"), t("none")]],
        hint: tn.available ? "" : t("needs Tailscale with HTTPS"), onPick: () => { wiz.delivery = "tailnet"; renderPage(); }}));
      d.append(field(t("How calls reach you"), stack, el("p", "hint", t("Calls you start and links your agent sends both go this way. This computer is the lightest: nothing leaves the machine but the speech and model requests."))));
    } else {
      d.append(field(t("Headphones or speakers?"), segments([{value: "headphones", label: t("Headphones")}, {value: "speakers", label: t("Speakers")}], wiz.capture,
        v => { wiz.capture = v; renderPage(); }), el("p", "hint", t("With speakers, Sleeptalk waits for its own voice to stop before it listens."))));
    }
    if (!live) d.append(el("p", "hint page-note", t("Want a Discord bot? Create this profile first, then open Discord setup.")));
    return d;
  },
  review() {
    const sv = wizService(); const agent = wiz.opts.agents.find(a => a.id === wiz.agent);
    const voiceMeta = (wiz.opts.voices_by_service[sv.id] || {voices: []}).voices.find(v => v.voice_id === wiz.voice);
    const table = el("table", "keys review");
    const rows = [
      [t("Name"), `${wiz.name.trim()} (${slugOf(wiz.name)})`], [t("Workspace"), wiz.workspace.trim()], [t("Language"), LANG_NAMES[wiz.lang] || wiz.lang],
      [t("Agent"), t(agent ? agent.label.split(" — ")[0] : wiz.agent)],
      [t("Speech service"), sv.name + " · " + (sv.id === "local" ? t("on this computer, no key") : wiz.reuse ? t("key found in {source}", {source: wiz.opts.reusable[sv.id].source}) : t("pasted key"))],
      ...(sv.id === "openai" ? [[t("Model"), modelLabel(modelById(sv, "tts", wiz.tts)) || wiz.tts]] : [
        [t("Speech-to-text model"), modelLabel(modelById(sv, "stt", wiz.stt)) || wiz.stt], [t("Text-to-speech model"), modelLabel(modelById(sv, "tts", wiz.tts)) || wiz.tts]]),
      [t("Voice"), voiceMeta ? voiceMeta.name : prettyVoice(wiz.voice || "default")],
      [t("Calls"), wiz.surface === "terminal" ? t("In this terminal") + " · " + t(wiz.capture === "headphones" ? "Headphones" : "Speakers") : t("In a browser") + " · " + t(wiz.delivery === "tailnet" ? "Your tailnet" : wiz.delivery === "local" ? "This computer" : "Hosted relay")],
    ];
    for (const [k, v] of rows) { const tr = el("tr"); tr.append(el("td", "", k), el("td", "", v)); table.append(tr); }
    const d = el("div"); d.append(table, el("p", "hint page-note", sv.id === "local"
      ? t("Creating downloads the local models this computer doesn't have yet, then listens to a short clip and speaks one line here to prove they work. No key, no account, nothing to pay.")
      : wiz.tts === "gpt-live-1" ? t("Creating checks your key by opening and closing a Live session, then keeps it in Sleeptalk's store. OpenAI bills the whole Live session, including silence; agent usage is separate.")
      : t("Creating checks your key once with {name} (it listens to a short clip and speaks one line, which uses a little credit), then keeps it in Sleeptalk's store.", {name: sv.name})));
    const destination = el("details", "panel");
    destination.append(el("summary", "", t("Call destination (browser by default)")), WIZ.calls());
    d.append(destination);
    return d;
  },
};

function wizKeyAndModels(sv) {
  const box = el("div", "detail");
  const found = wiz.opts.reusable[sv.id];
  const keyInput = el("input"); keyInput.type = "password"; keyInput.autocomplete = "off"; keyInput.maxLength = 8192; keyInput.placeholder = t("Paste your {name} key", {name: sv.name});
  keyInput.value = wiz.value;
  const keyField = el("div", "keyfield"); const eye = el("button", "eye"); eye.type = "button"; eye.title = t("Show what you typed"); eye.append(icon(EYE));
  eye.addEventListener("click", e => { e.stopPropagation(); keyInput.type = keyInput.type === "password" ? "text" : "password"; });
  keyField.append(keyInput, eye);
  keyInput.addEventListener("click", e => e.stopPropagation());
  keyInput.addEventListener("input", () => { wiz.value = keyInput.value; document.querySelector(".wiz-actions .btn:not(.ghost)").disabled = !wizReady("speech"); });
  const keyBox = el("div");
  if (found && wiz.reuse) {
    const saved = el("div", "saved"); saved.append(el("span", "dot"), el("span", "", t("Found in {source}", {source: found.source})));
    const other = el("button", "linkbtn", t("Use a different key")); other.type = "button";
    other.addEventListener("click", e => { e.stopPropagation(); wiz.reuse = false; renderPage(); });
    saved.append(other); keyBox.append(saved);
  } else keyBox.append(keyField);
  const hint = el("p", "hint", found && wiz.reuse
    ? t("Checked once when you create the profile (a little credit), then copied into Sleeptalk's store. The original stays where it is.")
    : t("Checked once when you create the profile (a little credit), then kept in Sleeptalk's store. Never shown again."));
  if (sv.key_url && !(found && wiz.reuse)) { hint.append(" "); const a = el("a", "", t("Get a {name} key ↗", {name: sv.name})); a.href = sv.key_url; a.target = "_blank"; a.rel = "noopener noreferrer"; hint.append(a); }
  box.append(field(t("API key"), keyBox, hint));
  const models = el("details"); models.append(el("summary", "", t("Advanced speech models")));
  for (const [role, label] of (sv.id === "openai" ? [["tts", t("Model")]] : [["stt", t("Speech-to-text model")], ["tts", t("Text-to-speech model")]])) {
    const wrap = selectBox(sv.models[role].models.filter(m => m.usable).map(m => ({value: m.id, label: `${modelLabel(m)}${PRIVACY_WORD[m.privacy] ? " · " + t(PRIVACY_WORD[m.privacy]) : ""}${m.recommended ? " " + t("(recommended)") : ""}`})), wiz[role], v => {
      wiz[role] = v;
      if (sv.id === "openai") {
        wiz.stt = wiz.tts = v;
        if (v === "gpt-live-1") wiz.surface = "web";
      }
      if (role === "tts") wiz.voice = voiceForModel(sv, modelById(sv, "tts", v), wiz.voice);
    }, label);
    wrap.addEventListener("click", e => e.stopPropagation());
    (sv.id === "openai" ? box : models).append(field(label, wrap));
  }
  if (sv.id !== "openai") box.append(models);
  box.addEventListener("click", e => e.stopPropagation());
  return box;
}

// Local speech in the new-profile flow: no key; the runtime has to be here,
// and the models download when the profile is created.
function wizLocalSetup(sv) {
  const box = el("div", "detail");
  box.addEventListener("click", e => e.stopPropagation());
  box.append(el("p", "hint", t("Local speech runs on this computer with no key and no usage costs. Voice quality is lower and replies are a little slower than the services above.")));
  const readiness = el("div");
  const enable = ready => { wiz.localReady = ready; const go = document.querySelector(".wiz-actions .btn:not(.ghost)"); if (go) go.disabled = !wizReady("speech"); };
  const check = () => localSetup(readiness, {stt: wiz.stt, tts: wiz.tts, voice: wiz.voice}, enable);
  box.append(field(t("On this computer"), readiness));
  const models = el("details"); models.append(el("summary", "", t("Advanced speech models")));
  for (const [role, label] of [["stt", t("Speech-to-text model")], ["tts", t("Text-to-speech model")]]) {
    const wrap = selectBox(sv.models[role].models.filter(m => m.usable).map(m => ({value: m.id, label: `${modelChoiceLabel(m)}${m.recommended ? " " + t("(recommended)") : ""}`})), wiz[role], v => {
      wiz[role] = v;
      if (role === "tts") wiz.voice = voiceForModel(sv, modelById(sv, "tts", v), wiz.voice);
      check();
    }, label);
    wrap.addEventListener("click", e => e.stopPropagation());
    models.append(field(label, wrap));
  }
  box.append(models);
  check();
  return box;
}

// The voice to keep after picking speaking model m: one of m's own, or, for a
// model that takes library voices, anything but another model's own voice.
function voiceForModel(service, m, voice) {
  if (!m) return voice;
  if (Array.isArray(m.voices)) return m.voices.includes(voice) ? voice : (m.default_voice || m.voices[0] || "");
  const listedElsewhere = (service.models.tts.models || []).some(o => o.id !== m.id && Array.isArray(o.voices) && o.voices.includes(voice));
  return listedElsewhere && m.default_voice ? m.default_voice : voice;
}

function wizDone(page) {
  const slug = wiz.done;
  const panel = el("div", "panel done-panel");
  panel.append(el("h2", "", t("Profile {name} is ready.", {name: slug})));
  // Straight to a call: the device choice is already open.
  if (state.profile === slug) {
    const {out} = startCallControls("btn", {open: true});
    const start = el("div", "done-call"); start.append(out);
    panel.append(start);
  }
  const actions = el("div", "wiz-actions");
  const open = el("button", "btn ghost", t("Open its settings")); open.type = "button";
  open.addEventListener("click", () => { wiz = null; location.hash = "#/speech"; });
  const another = el("button", "btn ghost", t("Create another")); another.type = "button";
  another.addEventListener("click", () => { wiz = null; renderPage(); });
  actions.append(another, el("span"), open);
  panel.append(actions);
  page.append(panel);
  return page;
}

// ---------- Chrome and routing ----------
// An agent's open key request comes first: the page answers it before anything else.
const requestOpen = () => !!(state && state.request && state.request.step !== "done");
function currentPage() {
  const id = location.hash.replace(/^#\/?/, "");
  if (requestOpen() || (state && state.request && !state.profile)) return "request";
  if (!state || !state.profile || id === "new") return "new";
  return PAGES.some(([p]) => p === id) ? id : "speech";
}
// One picker for the language you speak: it sets the profile's speech language,
// which also picks the recommended models and voice, and the page follows it.
function languagePicker(labelText) {
  if (!state.profile || currentPage() === "new") {
    // Nothing to save the language to yet (or the open profile isn't the one
    // being made): it only changes this page's words.
    const current = wiz && primary(wiz.lang) === uiLang ? wiz.lang : Object.keys(LANG_NAMES).find(l => primary(l) === uiLang) || "en-US";
    return selectBox(state.language.options.map(l => ({value: l, label: LANG_NAMES[l] || l})), current, async v => {
      setPageOverride(primary(v)); await useLocale(primary(v)); renderChrome(); renderPage();
    }, labelText);
  }
  const box = selectBox(state.language.options.map(l => ({value: l, label: LANG_NAMES[l] || l})), state.language.value, async v => {
    setPageOverride("");
    try { const r = await api("/api/set", {setting: "language", value: v}); await reload(); toast(r.changed && r.changed.length ? t("Language saved. Switched to the recommended {what}.", {what: r.changed.map(c => t(c)).join(", ")}) : t("Language saved.")); }
    catch (e) { toast(e.message, true); }
  }, labelText);
  return box;
}
// Split a translated sentence around {bot} so a live element can sit in it.
function around(text, node) { const [before, after = ""] = text.split("{bot}"); return [before, node, after]; }
function renderChrome() {
  const lang = document.getElementById("langpick"); lang.replaceChildren();
  if (!requestOpen()) { const box = languagePicker(t("Language")); box.classList.add("lang"); box.prepend(icon(GLOBE)); lang.append(box); }
  document.getElementById("profile-label").textContent = t("Profile");
  document.getElementById("side-foot").textContent = HOST
    ? t("Runs on your agent's computer. This page reaches it through an encrypted link the call service can't read.")
    : t("Runs on this computer. Keys are checked with their provider and kept in a private store here. Sleeptalk has no server and no account.");
  document.getElementById("nav").setAttribute("aria-label", t("Settings sections"));
  const nav = document.getElementById("nav"); nav.replaceChildren();
  const page = currentPage();
  // With no profile yet there is nothing to edit: the page is New profile.
  if (state.profile && !requestOpen()) for (const [id, label] of PAGES) { const a = el("a", id === page ? "current" : ""); a.append(navIcon(id), el("span", "", t(label))); a.href = "#/" + id; if (id === page) a.setAttribute("aria-current", "page"); nav.append(a); }
  // On a narrow screen the menu scrolls sideways; keep the current page in view.
  const current = nav.querySelector("a.current"); if (current && nav.scrollWidth > nav.clientWidth) current.scrollIntoView({block: "nearest", inline: "center"});
  const profile = document.getElementById("profile"); profile.replaceChildren();
  for (const p of state.profiles) { const o = el("option", "", p); o.value = p; profile.append(o); }
  const add = el("option", "", t("+ New profile")); add.value = "__new__"; profile.append(add);
  profile.value = page === "new" || !state.profile ? "__new__" : state.profile; profile.title = state.path || "";
  // While the agent's request is open, the page is about that request only.
  document.querySelector(".side-profile").hidden = requestOpen();
  profileActions();
  if (requestOpen()) document.getElementById("profile-actions").replaceChildren();
}
function renderPage() {
  const page = currentPage();
  main.replaceChildren(page === "request" ? requestPage() : page === "new" ? newProfilePage() : RENDER[page]());
  document.title = t("{page} · Sleeptalk settings", {page: page === "request" ? t("Voice setup") : page === "new" ? t("New profile") : pageLabel(page)});
}
window.addEventListener("hashchange", () => { if (!state) return; renderChrome(); renderPage(); window.scrollTo(0, 0); });
document.getElementById("profile").addEventListener("change", async e => {
  if (e.target.value === "__new__") { location.hash = "#/new"; return; }
  try { await api("/api/profile", {profile: e.target.value}); openService = ""; await reload(); toast(t("Editing {profile}.", {profile: e.target.value})); } catch (err) { toast(err.message, true); }
});
(async () => {
  try {
    const token = HOST ? "" : location.hash.replace(/^#/, "");
    if (token && !token.startsWith("/")) { history.replaceState(null, "", location.pathname + "#/speech"); const r = await api("/session", {token}); csrf = r.csrf; sessionStorage.setItem("settings_access", csrf); }
    await reload();
  } catch (e) { main.replaceChildren(el("p", "status error", e.message)); }
})();
