import { dotState, worstState, type WorkerEntry, type WorkerJob } from "./presentation";

/**
 * The background worker's own space: its thinking, steps and narration,
 * kept apart from the conversation. On a wide screen it slides in from the
 * right when a job starts and the conversation makes room; on a phone it
 * waits as a pill in the corner and opens as a sheet when tapped. Either way
 * it can be stowed back into the pill.
 */

const WIDE = "(min-width: 960px)";
const CANCEL_ARM_MS = 3000;
/** A new job's tab is shown briefly, then the row returns to the selected one. */
const FRESH_TAB_MS = 1600;

export type WorkerPanelOptions = {
  onCancel: (jobId: string) => void;
  /** The caller picked a tab: the host routes their bare words to it. */
  onSelect: (jobId: string) => void;
};

/**
 * With several jobs, the panel opens on a list of them, one row each, like
 * an agent sidebar: what state it is in, its name, and how long it has run
 * or how it ended. The selected job's status and Cancel sit under the list,
 * its feed below that. The host keeps which one is selected (voice routing
 * needs it, and Discord or the terminal have no list); the page shows that
 * one, follows the host when it changes, and tells the host when the caller
 * taps another. A new job never takes the selection: its row is lit once.
 * A finished job's row stays, marked unread until it is opened.
 */
export class WorkerPanel {
  private readonly panel: HTMLElement;
  private readonly pill: HTMLButtonElement;
  private readonly titleEl: HTMLElement;
  private readonly statusEl: HTMLElement;
  private readonly tabsEl: HTMLElement;
  private readonly headEl: HTMLElement;
  private readonly jobBar: HTMLElement;
  private readonly jobStatusEl: HTMLElement;
  private readonly feedEl: HTMLElement;
  private readonly cancelButton: HTMLButtonElement;
  private readonly stowButton: HTMLButtonElement;
  private readonly wide: MediaQueryList;
  private jobs: WorkerJob[] = [];
  private known = new Set<string>();
  /** Finished jobs whose tab the caller has had open since they finished. */
  private read = new Set<string>();
  /** The job shown: the host's selection, or the caller's tap ahead of it. */
  private shownId = "";
  private hostSelected = "";
  private freshTimer: number | null = null;
  private isOpen = false;
  private stowedByCaller = false;
  private lastKey = "";
  private cancelArmed: number | null = null;
  /** Where the pill lives on a phone (above the orb); on a wide screen it
   * moves to the page, so the header's fade can't cover it. */
  private readonly pillHome: HTMLElement | null;

  constructor(private readonly options: WorkerPanelOptions) {
    this.panel = required("worker-panel");
    this.pill = required<HTMLButtonElement>("worker-pill");
    this.titleEl = required("worker-title");
    this.statusEl = required("worker-status");
    this.tabsEl = required("worker-tabs");
    this.jobBar = required("worker-jobbar");
    this.jobStatusEl = required("worker-job-status");
    this.feedEl = required("worker-feed");
    this.cancelButton = required<HTMLButtonElement>("worker-cancel");
    this.stowButton = required<HTMLButtonElement>("worker-stow");
    this.headEl = this.stowButton.parentElement ?? this.panel;
    this.wide = window.matchMedia(WIDE);
    this.pillHome = this.pill.parentElement;
    this.pill.addEventListener("click", () => this.open());
    this.stowButton.addEventListener("click", () => this.stow());
    this.cancelButton.addEventListener("click", () => this.cancel());
    this.wide.addEventListener("change", () => this.layout());
    // Timers count between snapshots: a model writing a whole file sends
    // nothing for minutes.
    window.setInterval(() => this.tick(), 1000);
  }

  render(jobs: WorkerJob[], selectedJob = ""): void {
    const key = `${selectedJob}|${JSON.stringify(jobs)}`;
    if (key === this.lastKey) return;
    this.lastKey = key;
    this.jobs = jobs;
    if (selectedJob !== this.hostSelected) {
      // The host moved the selection (or confirmed the caller's tap).
      this.hostSelected = selectedJob;
      if (selectedJob) this.show(selectedJob, false);
    }
    const fresh: string[] = [];
    // A page opened mid-call finds every job at once: none of them is new.
    const firstLook = this.known.size === 0;
    for (const job of jobs) {
      if (this.known.has(job.jobId)) continue;
      this.known.add(job.jobId);
      if (!firstLook) fresh.push(job.jobId);
      // A new job opens the panel where there is room for it, unless the
      // caller put it away during this call.
      if (job.phase === "running" && this.wide.matches && !this.stowedByCaller) this.isOpen = true;
    }
    this.markRead();
    this.paintTabs(fresh);
    this.paintFeed();
    this.paintHeader();
    this.paintPill();
    this.layout();
  }

  open(): void {
    if (!this.jobs.length) return;
    this.isOpen = true;
    this.stowedByCaller = false;
    this.layout();
    this.markRead();
    this.paintTabs([]);
    this.feedEl.scrollTop = this.feedEl.scrollHeight;
  }

  stow(): void {
    this.isOpen = false;
    this.stowedByCaller = true;
    this.layout();
  }

  /** The job on show: the selected one if it is still here, else the one
   * waiting on the caller, else the newest live one, else the newest. */
  private shown(): WorkerJob | undefined {
    const byId = this.jobs.find((job) => job.jobId === this.shownId);
    if (byId) return byId;
    return (
      this.jobs.find((job) => job.waiting) ??
      [...this.jobs].reverse().find((job) => job.phase === "running" || job.phase === "queued") ??
      this.jobs[this.jobs.length - 1]
    );
  }

  private show(jobId: string, byCaller: boolean): void {
    if (jobId === this.shownId) return;
    this.shownId = jobId;
    this.disarmCancel();
    if (byCaller) {
      this.options.onSelect(jobId);
      this.markRead();
      this.paintTabs([]);
      this.paintFeed();
      this.paintHeader();
      this.paintPill();
      this.feedEl.scrollTop = this.feedEl.scrollHeight;
    }
  }

  /** A finished job counts as read once its tab is open on screen. */
  private markRead(): void {
    const job = this.shown();
    if (job && this.isOpen && isFinished(job)) this.read.add(job.jobId);
  }

  private layout(): void {
    const home = this.wide.matches ? document.body : this.pillHome;
    if (home && this.pill.parentElement !== home) home.prepend(this.pill);
    const any = this.jobs.length > 0;
    const open = any && this.isOpen;
    this.panel.hidden = !open;
    this.pill.hidden = !any || open;
    this.pill.setAttribute("aria-expanded", String(open));
    document.body.classList.toggle("worker-open", open && this.wide.matches);
    document.body.classList.toggle("worker-sheet", open && !this.wide.matches);
  }

  private paintTabs(fresh: string[]): void {
    const several = this.jobs.length > 1;
    this.tabsEl.hidden = !several;
    this.jobBar.hidden = !several;
    this.panel.classList.toggle("tabbed", several);
    if (!several) {
      this.tabsEl.replaceChildren();
      return;
    }
    const shown = this.shown();
    const tabs = this.jobs.map((job) => {
      const tab = element("button", "worker-tab");
      tab.type = "button";
      tab.setAttribute("role", "tab");
      tab.dataset.jobId = job.jobId;
      const selected = job.jobId === shown?.jobId;
      tab.setAttribute("aria-selected", String(selected));
      tab.classList.toggle("selected", selected);
      const unread = isFinished(job) && !this.read.has(job.jobId);
      tab.classList.toggle("unread", unread);
      if (fresh.includes(job.jobId) && !selected) tab.classList.add("fresh");
      tab.title = `${job.title} · ${statusText(job) || "Waiting"}${unread ? " · unread" : ""}`;
      tab.append(jobIcon(dotState(job)), element("span", "worker-tab-title", job.title), tabMeta(job));
      tab.addEventListener("click", () => this.show(job.jobId, true));
      return tab;
    });
    this.tabsEl.replaceChildren(...tabs);
    const selectedTab = tabs.find((tab) => tab.classList.contains("selected"));
    const freshTab = tabs.find((tab) => tab.classList.contains("fresh"));
    if (freshTab) {
      // Seen once, then back to the job the caller is reading.
      freshTab.scrollIntoView({ block: "nearest", inline: "nearest" });
      if (this.freshTimer !== null) window.clearTimeout(this.freshTimer);
      this.freshTimer = window.setTimeout(() => {
        this.freshTimer = null;
        this.tabsEl.querySelector<HTMLElement>(".worker-tab.selected")?.scrollIntoView({ block: "nearest", inline: "nearest" });
      }, FRESH_TAB_MS);
    } else if (selectedTab && this.freshTimer === null) {
      selectedTab.scrollIntoView({ block: "nearest", inline: "nearest" });
    }
  }

  private paintHeader(): void {
    const job = this.shown();
    if (!job) return;
    this.panel.dataset.phase = job.waiting ? "waiting" : job.phase;
    // With a list, the header names the set and the bar under the list
    // carries the selected job's status and Cancel.
    const several = this.jobs.length > 1;
    this.titleEl.textContent = several ? "Background jobs" : job.title;
    this.statusEl.classList.toggle("summary", several);
    this.statusEl.textContent = several ? summaryText(this.jobs) : statusText(job);
    this.jobStatusEl.textContent = several ? statusText(job) : "";
    this.statusEl.dataset.startedAt = job.phase === "running" && job.startedAt ? String(job.startedAt) : "";
    const home = several ? this.jobBar : this.headEl;
    if (this.cancelButton.parentElement !== home) home.insertBefore(this.cancelButton, several ? null : this.stowButton);
    const stoppable = job.phase === "running" || job.phase === "queued";
    this.cancelButton.hidden = !stoppable;
    if (!stoppable) this.disarmCancel();
  }

  private paintPill(): void {
    const job = this.shown();
    this.pill.replaceChildren();
    if (!job) return;
    const going = this.jobs.filter((other) => other.phase === "running" || other.phase === "queued");
    let dot: HTMLElement;
    const text = document.createElement("span");
    text.className = "worker-pill-text";
    const latest = job.entries[job.entries.length - 1];
    if (going.length > 1) {
      // One line, whatever the count: how many, then the job that needs
      // the caller, else the one selected. The dot is the worst state.
      const asking = this.jobs.find((other) => other.waiting);
      dot = jobIcon(worstState(this.jobs));
      text.textContent = asking
        ? `${going.length} jobs · Needs you · ${asking.title}`
        : `${going.length} jobs · ${job.title}`;
      this.pill.append(dot, text);
    } else {
      dot = jobIcon(dotState(job));
      if (job.waiting) text.textContent = `Needs you · ${job.title}`;
      else if (job.phase === "running") text.textContent = latest ? latest.text : `Working on ${job.title}`;
      else text.textContent = `${statusText(job)} · ${job.title}`;
      this.pill.append(dot, text);
      if (job.phase === "running" && latest?.startedAt) {
        const elapsed = timer(latest.startedAt);
        elapsed.classList.add("worker-pill-time");
        this.pill.append(elapsed);
      }
    }
    // On a wide screen the tab sits where the panel's close arrow was: this
    // arrow opens it again from the same spot.
    const open = document.createElement("span");
    open.className = "worker-pill-open";
    open.setAttribute("aria-hidden", "true");
    open.textContent = "‹";
    this.pill.append(open);
    this.pill.title = going.length > 1 ? `Background jobs: ${going.map((other) => other.title).join(", ")}` : `Background worker: ${job.title}`;
  }

  private paintFeed(): void {
    const job = this.shown();
    const nearBottom = this.feedEl.scrollHeight - this.feedEl.scrollTop - this.feedEl.clientHeight < 48;
    this.feedEl.replaceChildren();
    if (!job) return;
    const timeline = element("ol", "worker-timeline");
    const placeholder =
      job.phase === "queued" ? "Waiting to start, after one of the running jobs." : job.phase === "running" ? "Getting started…" : "";
    const entries = job.entries.length || !placeholder
      ? job.entries
      : [{ kind: "thinking", text: placeholder, links: [] } as WorkerEntry];
    entries.forEach((entry, at) => {
      const live = job.phase === "running" && at === entries.length - 1 && isLive(entry);
      timeline.appendChild(renderEntry(entry, entries[at + 1], job, live));
    });
    this.feedEl.appendChild(timeline);
    if (nearBottom) this.feedEl.scrollTop = this.feedEl.scrollHeight;
  }

  private cancel(): void {
    const job = this.shown();
    if (!job) return;
    if (this.cancelArmed === null) {
      // Two taps: a stray one must not throw away minutes of work.
      this.cancelButton.textContent = "Tap again to cancel";
      this.cancelButton.classList.add("armed");
      this.cancelArmed = window.setTimeout(() => this.disarmCancel(), CANCEL_ARM_MS);
      return;
    }
    this.disarmCancel();
    this.cancelButton.textContent = "Cancelling…";
    this.cancelButton.disabled = true;
    this.options.onCancel(job.jobId);
  }

  private disarmCancel(): void {
    if (this.cancelArmed !== null) window.clearTimeout(this.cancelArmed);
    this.cancelArmed = null;
    this.cancelButton.textContent = "Cancel";
    this.cancelButton.classList.remove("armed");
    this.cancelButton.disabled = false;
  }

  private tick(): void {
    for (const el of document.querySelectorAll<HTMLElement>(".worker-elapsed[data-started-at]")) {
      const startedAt = Number(el.dataset.startedAt);
      el.textContent = el.dataset.short ? shortClock(startedAt) : el.dataset.bare ? clock(startedAt) : elapsedLabel(startedAt);
    }
    const job = this.shown();
    if (job?.phase === "running") (this.jobs.length > 1 ? this.jobStatusEl : this.statusEl).textContent = statusText(job);
  }
}

function required<T extends HTMLElement = HTMLElement>(id: string): T {
  const el = document.getElementById(id);
  if (!el) throw new Error(`missing #${id}`);
  return el as T;
}

function isLive(entry: WorkerEntry): boolean {
  return entry.kind === "step" || entry.kind === "thinking";
}

function isFinished(job: WorkerJob): boolean {
  return job.phase === "completed" || job.phase === "failed";
}

function statusText(job: WorkerJob): string {
  switch (job.phase) {
    case "queued":
      return "Waiting to start";
    case "running": {
      if (job.waiting) return "Needs you";
      const elapsed = job.startedAt ? clock(job.startedAt) : "";
      const steps = job.steps ? `${job.steps} step${job.steps === 1 ? "" : "s"}` : "";
      return ["Working", elapsed, steps].filter(Boolean).join(" · ");
    }
    case "completed":
      return "Done";
    case "failed":
      return "Stopped";
    case "cancelled":
      return "Cancelled";
    case "replaced":
      return "Replaced";
    default:
      return "";
  }
}

/** The header's line over several jobs: "1 needs you · 2 working · 1 done". */
function summaryText(jobs: readonly WorkerJob[]): string {
  const counts = new Map<string, number>();
  for (const job of jobs) {
    const state = dotState(job);
    const label =
      state === "attention" ? "needs you" : state === "live" ? "working" : state === "queued" ? "queued" : state === "done" ? "done" : "stopped";
    counts.set(label, (counts.get(label) ?? 0) + 1);
  }
  return ["needs you", "working", "queued", "done", "stopped"]
    .filter((label) => counts.has(label))
    .map((label) => `${counts.get(label)} ${label}`)
    .join(" · ");
}

/** A row's right edge: time so far while it works, else how it stands. */
function tabMeta(job: WorkerJob): HTMLElement {
  const meta = element("span", "worker-tab-meta");
  if (job.waiting) {
    meta.classList.add("chip");
    meta.textContent = "Needs you";
  } else if (job.phase === "running" && job.startedAt) {
    const elapsed = timer(job.startedAt);
    elapsed.dataset.short = "1";
    elapsed.textContent = shortClock(job.startedAt);
    meta.appendChild(elapsed);
  } else if (job.phase === "running") {
    meta.textContent = "Starting";
  } else {
    meta.textContent = job.phase === "queued" ? "Queued" : statusText(job);
  }
  return meta;
}

const SVG = "http://www.w3.org/2000/svg";

/** A job's state as a small mark: an arc that turns while it works, a lit
 * dot when it needs the caller, a dashed ring while queued, a check when
 * done, a cross when it stopped. */
function jobIcon(state: string): HTMLElement {
  const wrap = element("span", `worker-icon ${state}`);
  wrap.setAttribute("aria-hidden", "true");
  const svg = document.createElementNS(SVG, "svg");
  svg.setAttribute("viewBox", "0 0 16 16");
  const shapes: Record<string, [string, Record<string, string>][]> = {
    live: [
      ["circle", { cx: "8", cy: "8", r: "6", class: "track" }],
      ["path", { d: "M8 2a6 6 0 0 1 6 6", class: "arc" }],
    ],
    attention: [
      ["circle", { cx: "8", cy: "8", r: "6.5", class: "halo" }],
      ["circle", { cx: "8", cy: "8", r: "3.5", class: "core" }],
    ],
    queued: [["circle", { cx: "8", cy: "8", r: "6", class: "ring" }]],
    done: [
      ["circle", { cx: "8", cy: "8", r: "7", class: "fill" }],
      ["path", { d: "M5 8.2l2 2 4-4.4", class: "mark" }],
    ],
    stopped: [
      ["circle", { cx: "8", cy: "8", r: "7", class: "fill" }],
      ["path", { d: "M6 6l4 4M10 6l-4 4", class: "mark" }],
    ],
  };
  for (const [tag, attrs] of shapes[state] ?? shapes.stopped) {
    const shape = document.createElementNS(SVG, tag);
    for (const [name, value] of Object.entries(attrs)) shape.setAttribute(name, value);
    svg.appendChild(shape);
  }
  wrap.appendChild(svg);
  return wrap;
}

function element<K extends keyof HTMLElementTagNameMap>(tag: K, className: string, text = ""): HTMLElementTagNameMap[K] {
  const el = document.createElement(tag);
  el.className = className;
  if (text) el.textContent = text;
  return el;
}

/** One row of the timeline: a mark on the rail, what happened, and when
 * (time into the job, like a run log). */
function renderEntry(entry: WorkerEntry, next: WorkerEntry | undefined, job: WorkerJob, live: boolean): HTMLElement {
  const row = element("li", `worker-entry ${entry.kind}${live ? " live" : ""}`);
  row.appendChild(element("span", "worker-mark"));
  const body = element("div", "worker-body");
  switch (entry.kind) {
    case "thinking":
      if (live) {
        body.append(element("p", "worker-kicker", "Thinking"), element("p", "worker-thought", entry.text));
      } else {
        // A finished thought folds to its length, open on tap.
        const details = element("details", "worker-fold");
        details.append(element("summary", "worker-kicker", thoughtFor(entry, next)), element("p", "worker-thought", entry.text));
        body.appendChild(details);
      }
      break;
    case "step": {
      // "terminal · ls -la": the harness's tool as a tag, then what it is on.
      const split = entry.text.indexOf(" · ");
      const line = element("p", "worker-step");
      if (split > 0) {
        line.append(element("span", "worker-tag", entry.text.slice(0, split)), element("span", "worker-step-label", entry.text.slice(split + 3)));
      } else {
        // A bare tool name ("browser_console") is still a tool.
        line.appendChild(element("span", /^[\w.:-]+$/.test(entry.text) ? "worker-tag" : "worker-step-label", entry.text));
      }
      body.appendChild(line);
      break;
    }
    case "ask":
      body.append(element("p", "worker-kicker", "Needs you"), element("p", "worker-text", entry.text));
      break;
    case "done":
      body.appendChild(element("p", "worker-kicker", "Done"));
      if (entry.links.length) {
        const links = element("div", "worker-links");
        for (const link of entry.links) links.appendChild(workerLink(link));
        body.appendChild(links);
      }
      break;
    default:
      body.appendChild(element("p", "worker-text", entry.text));
  }
  row.appendChild(body);
  const when = element("span", "worker-when");
  if (live && entry.startedAt) {
    when.appendChild(timer(entry.startedAt));
  } else if (entry.startedAt && job.startedAt) {
    when.textContent = offset(entry.startedAt - job.startedAt);
  }
  row.appendChild(when);
  return row;
}

function thoughtFor(entry: WorkerEntry, next: WorkerEntry | undefined): string {
  if (!entry.startedAt || !next?.startedAt) return "Thought";
  const seconds = Math.round((next.startedAt - entry.startedAt) / 1000);
  return seconds >= 1 ? `Thought for ${seconds}s` : "Thought";
}

function offset(ms: number): string {
  const seconds = Math.max(0, Math.floor(ms / 1000));
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
}

export function workerLink(link: string): HTMLElement {
  if (/^https?:\/\//i.test(link)) {
    const anchor = document.createElement("a");
    anchor.className = "worker-link";
    anchor.href = link;
    anchor.target = "_blank";
    anchor.rel = "noopener noreferrer";
    anchor.textContent = link.replace(/^https?:\/\//i, "");
    anchor.title = link;
    return anchor;
  }
  // A file path on the host: shown to copy, not opened from this page.
  const path = document.createElement("span");
  path.className = "worker-link path";
  path.textContent = link;
  return path;
}

function timer(startedAt: number): HTMLElement {
  const el = document.createElement("span");
  el.className = "worker-elapsed";
  el.dataset.startedAt = String(startedAt);
  el.dataset.bare = "1";
  el.textContent = clock(startedAt);
  return el;
}

function clock(startedAt: number): string {
  const seconds = Math.max(0, Math.floor((Date.now() - startedAt) / 1000));
  const minutes = Math.floor(seconds / 60);
  return minutes ? `${minutes}m ${String(seconds % 60).padStart(2, "0")}s` : `${seconds}s`;
}

/** "45s", "12m", "1h 05m": a row's time, read at a glance. */
function shortClock(startedAt: number): string {
  const seconds = Math.max(0, Math.floor((Date.now() - startedAt) / 1000));
  const minutes = Math.floor(seconds / 60);
  if (minutes >= 60) return `${Math.floor(minutes / 60)}h ${String(minutes % 60).padStart(2, "0")}m`;
  return minutes ? `${minutes}m` : `${seconds}s`;
}

export function elapsedLabel(startedAt: number): string {
  const seconds = Math.max(0, Math.floor((Date.now() - startedAt) / 1000));
  return seconds < 2 ? "" : ` · ${clock(startedAt)}`;
}
