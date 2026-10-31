import {
  snapNarration,
  snapReplyLength,
  snapSpeed,
  type NarrationStyle,
  type ReplyLength,
  type SpeedStep,
} from "./settings";

export type Presence = "idle" | "listening" | "thinking" | "speaking";
export type OrbState = Presence;

export type SpokenWord = {
  text: string;
  startMs?: number;
  endMs?: number;
};

export type UserLine = {
  kind: "user";
  text: string;
};

export type AssistantLine = {
  kind: "assistant";
  text: string;
  words: SpokenWord[];
  spokenIndex: number;
  /** The whole answer, when what was said (`text`) was shortened for speech. */
  full?: string;
};

export type ToolLine = {
  kind: "tool";
  text: string;
  callId?: string;
  phase?: string;
  plus?: number;
  minus?: number;
};

export type ProgressLine = {
  kind: "progress";
  text: string;
};

export type NotesLine = {
  kind: "notes";
  text: string;
};

/** A background job's milestone in the conversation: it started, it needs
 * the caller, it ended. Its working detail lives in the worker panel. */
export type WorkerLine = {
  kind: "worker";
  text: string;
  /** started | permission | question | completed | failed | cancelled | replaced */
  phase: string;
  jobId?: string;
  links: string[];
};

/** One item in a job's own feed. */
export type WorkerEntry = {
  kind: "thinking" | "step" | "said" | "note" | "ask" | "done" | "stopped";
  text: string;
  steps?: number;
  /** When this step or thought began (host clock, epoch ms). */
  startedAt?: number;
  links: string[];
};

/** A background job as the worker panel shows it. */
export type WorkerJob = {
  jobId: string;
  title: string;
  phase: "queued" | "running" | "completed" | "failed" | "cancelled" | "replaced";
  startedAt?: number;
  steps: number;
  waiting: string;
  entries: WorkerEntry[];
};

export type PresentationLine = UserLine | AssistantLine | NotesLine | ToolLine | ProgressLine | WorkerLine;

export type PresentationSnapshot = {
  diagnostics?: boolean;
  presence: Presence;
  lines: PresentationLine[];
  muted: boolean;
  hearing: boolean;
  partial: string;
  stack: string[];
  speaker: string;
  controls: {
    ttsSpeed: SpeedStep;
    narrationStyle: NarrationStyle;
    replyLength: ReplyLength;
  };
  voice: VoiceState | null;
  settings: SettingsState;
  jobs: WorkerJob[];
  /** The job the caller's bare words go to; its tab is the one shown. "" when none. */
  selectedJob: string;
};

/** Whether the host can open the full settings page, and the last result. */
export type SettingsState = { available: boolean; notice: string; url: string };

/** Only a settings link (the call service's /settings page) is offered to the caller. */
function settingsLink(value: unknown): string {
  if (typeof value !== "string") return "";
  try {
    const url = new URL(value);
    // The host's hosted call service, which may differ from this page's origin on a local or tailnet call.
    const here = (globalThis as { location?: { origin: string } }).location?.origin;
    const trusted = url.origin === here || url.protocol === "https:";
    return trusted && url.pathname === "/settings" && url.hash.startsWith("#/settings/") ? url.href : "";
  } catch {
    return "";
  }
}

export function parseSettings(value: unknown): SettingsState {
  if (typeof value !== "object" || value === null) return { available: false, notice: "", url: "" };
  const record = value as Record<string, unknown>;
  return {
    available: record.available === true,
    notice: typeof record.notice === "string" ? record.notice : "",
    url: settingsLink(record.url),
  };
}

export type CuratedVoice = { name: string; description: string; voiceId: string; default: boolean };

export type VoiceState = {
  provider: string;
  voiceId: string;
  name: string;
  curated: CuratedVoice[];
  libraryUrl: string;
  notice: string;
};

export function parseVoice(value: unknown): VoiceState | null {
  if (typeof value !== "object" || value === null) return null;
  const record = value as Record<string, unknown>;
  if (typeof record.provider !== "string" || !record.provider) return null;
  const curated: CuratedVoice[] = [];
  if (Array.isArray(record.curated)) {
    for (const item of record.curated) {
      if (typeof item !== "object" || item === null) continue;
      const entry = item as Record<string, unknown>;
      if (typeof entry.voice_id !== "string" || typeof entry.name !== "string") continue;
      curated.push({
        name: entry.name,
        description: typeof entry.description === "string" ? entry.description : "",
        voiceId: entry.voice_id,
        default: entry.default === true,
      });
    }
  }
  return {
    provider: record.provider,
    voiceId: typeof record.voiceId === "string" ? record.voiceId : "",
    name: typeof record.name === "string" ? record.name : "",
    curated,
    libraryUrl: typeof record.libraryUrl === "string" ? record.libraryUrl : "",
    notice: typeof record.notice === "string" ? record.notice : "",
  };
}

export type UserSpeakingHint =
  | { kind: "none" }
  | { kind: "dots" }
  | { kind: "partial"; text: string }
  | { kind: "waiting"; speaker: string };

/** The last row of the feed: what the caller is saying, or, once they have
 * finished, dots in the agent's row while its answer is still coming. */
export function userSpeakingHint(input: {
  partial: string;
  hearing: boolean;
  agentWaiting?: boolean;
  speaker?: string;
}): UserSpeakingHint {
  if (input.partial) return { kind: "partial", text: input.partial };
  if (input.hearing) return { kind: "dots" };
  if (input.agentWaiting) return { kind: "waiting", speaker: input.speaker || "agent" };
  return { kind: "none" };
}

/** How long the agent's voice must be quiet before its dots show, so they
 * don't appear under an acknowledgement ("On it.") still being spoken. */
export const AGENT_QUIET_MS = 700;

/**
 * Whether the agent owes the caller an answer that hasn't started: the
 * host is thinking (after the caller's turn, an acknowledgement, or a tool)
 * and nothing has been audible for a moment. Once shown (`shown`), the dots
 * stay until the host stops thinking, so a typing sound or a short clip
 * doesn't make them blink.
 */
export function agentWaiting(input: {
  presence: Presence;
  hearing: boolean;
  partial: string;
  lines: readonly PresentationLine[];
  quietMs: number;
  shown: boolean;
}): boolean {
  if (input.presence !== "thinking" || input.hearing || input.partial) return false;
  if (!input.lines.some((line) => line.kind === "user")) return false;
  return input.shown || input.quietMs >= AGENT_QUIET_MS;
}

const PRESENCE: ReadonlySet<string> = new Set(["idle", "listening", "thinking", "speaking"]);
const HOST_TO_ORB: Record<string, OrbState> = {
  working: "thinking",
  answering: "speaking",
};
const LINE_KINDS: ReadonlySet<string> = new Set(["user", "assistant", "notes", "tool", "progress", "worker"]);

export function hostOrbState(state: string): OrbState | null {
  if (PRESENCE.has(state)) return state as OrbState;
  return HOST_TO_ORB[state] ?? null;
}

export function lastAssistant(lines: readonly PresentationLine[]): AssistantLine | null {
  for (let index = lines.length - 1; index >= 0; index -= 1) {
    const line = lines[index];
    if (line.kind === "assistant") return line;
  }
  return null;
}

function lineIdentity(line: PresentationLine) {
  switch (line.kind) {
    case "tool":
      return {
        kind: line.kind,
        text: line.text,
        callId: line.callId,
        phase: line.phase,
        plus: line.plus,
        minus: line.minus,
      };
    case "worker":
      return { kind: line.kind, text: line.text, phase: line.phase, links: line.links };
    case "user":
    case "assistant":
    case "notes":
    case "progress":
      return { kind: line.kind, text: line.text };
    default: {
      const _exhaustive: never = line;
      return _exhaustive;
    }
  }
}

export function snapshotKey(snapshot: PresentationSnapshot): string {
  return JSON.stringify({
    presence: snapshot.presence,
    muted: snapshot.muted,
    hearing: snapshot.hearing,
    partial: snapshot.partial,
    stack: snapshot.stack,
    speaker: snapshot.speaker,
    lines: snapshot.lines.map(lineIdentity),
  });
}

export function parsePresentation(payload: unknown): PresentationSnapshot | null {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) return null;
  const record = payload as Record<string, unknown>;
  const presenceRaw = typeof record.presence === "string" ? record.presence : "";
  const presence = PRESENCE.has(presenceRaw) ? (presenceRaw as Presence) : "idle";
  const lines = parseLines(record.lines);
  return {
    presence,
    diagnostics: record.diagnostics === true,
    lines: lines.length ? lines : legacyLines(record),
    muted: record.muted === true,
    hearing: record.hearing === true,
    partial: typeof record.partial === "string" ? record.partial : "",
    stack: parseStack(record.stack),
    speaker: typeof record.speaker === "string" && record.speaker ? record.speaker : "agent",
    controls: {
      ttsSpeed: snapSpeed(record.ttsSpeed),
      narrationStyle: snapNarration(record.narrationStyle),
      replyLength: snapReplyLength(record.replyLength),
    },
    voice: parseVoice(record.voice),
    settings: parseSettings(record.settings),
    jobs: parseJobs(record.jobs),
    selectedJob: typeof record.selectedJob === "string" ? record.selectedJob : "",
  };
}

const ENTRY_KINDS: ReadonlySet<string> = new Set(["thinking", "step", "said", "note", "ask", "done", "stopped"]);
const JOB_PHASES: ReadonlySet<string> = new Set(["queued", "running", "completed", "failed", "cancelled", "replaced"]);

function stringList(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string" && item.length > 0) : [];
}

function finiteNumber(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

export function parseJobs(value: unknown): WorkerJob[] {
  if (!Array.isArray(value)) return [];
  const jobs: WorkerJob[] = [];
  for (const item of value) {
    if (!item || typeof item !== "object" || Array.isArray(item)) continue;
    const record = item as Record<string, unknown>;
    if (typeof record.jobId !== "string" || !record.jobId) continue;
    const entries: WorkerEntry[] = [];
    for (const raw of Array.isArray(record.entries) ? record.entries : []) {
      if (!raw || typeof raw !== "object") continue;
      const entry = raw as Record<string, unknown>;
      if (typeof entry.kind !== "string" || !ENTRY_KINDS.has(entry.kind)) continue;
      if (typeof entry.text !== "string" || !entry.text) continue;
      const parsed: WorkerEntry = { kind: entry.kind as WorkerEntry["kind"], text: entry.text, links: stringList(entry.links) };
      const steps = finiteNumber(entry.steps);
      if (steps !== undefined) parsed.steps = steps;
      const startedAt = finiteNumber(entry.startedAt);
      if (startedAt !== undefined) parsed.startedAt = startedAt;
      entries.push(parsed);
    }
    const phase = typeof record.phase === "string" && JOB_PHASES.has(record.phase) ? record.phase : "running";
    const job: WorkerJob = {
      jobId: record.jobId,
      title: typeof record.title === "string" && record.title ? record.title : "background job",
      phase: phase as WorkerJob["phase"],
      steps: finiteNumber(record.steps) ?? 0,
      waiting: typeof record.waiting === "string" ? record.waiting : "",
      entries,
    };
    const startedAt = finiteNumber(record.startedAt);
    if (startedAt !== undefined) job.startedAt = startedAt;
    jobs.push(job);
  }
  return jobs;
}

function parseStack(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  const stack: string[] = [];
  for (const item of value) {
    if (typeof item === "string" && item) stack.push(item);
  }
  return stack;
}

function parseLines(value: unknown): PresentationLine[] {
  if (!Array.isArray(value)) return [];
  const lines: PresentationLine[] = [];
  for (const item of value) {
    const line = parseLine(item);
    if (line) lines.push(line);
  }
  return lines;
}

function parseLine(value: unknown): PresentationLine | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const record = value as Record<string, unknown>;
  if (typeof record.kind !== "string" || !LINE_KINDS.has(record.kind)) return null;
  if (typeof record.text !== "string" || !record.text) return null;
  if (record.kind === "assistant") {
    const line: AssistantLine = {
      kind: "assistant",
      text: record.text,
      words: parseWords(record.words),
      spokenIndex: finiteIndex(record.spokenIndex),
    };
    if (typeof record.full === "string" && record.full) line.full = record.full;
    return line;
  }
  if (record.kind === "user") return { kind: "user", text: record.text };
  if (record.kind === "notes") return { kind: "notes", text: record.text };
  if (record.kind === "progress") return { kind: "progress", text: record.text };
  if (record.kind === "worker") {
    const worker: WorkerLine = {
      kind: "worker",
      text: record.text,
      phase: typeof record.phase === "string" ? record.phase : "",
      links: Array.isArray(record.links)
        ? record.links.filter((link): link is string => typeof link === "string" && link.length > 0)
        : [],
    };
    if (typeof record.callId === "string" && record.callId) worker.jobId = record.callId;
    return worker;
  }
  const tool: ToolLine = { kind: "tool", text: record.text };
  if (typeof record.callId === "string" && record.callId) tool.callId = record.callId;
  if (typeof record.phase === "string" && record.phase) tool.phase = record.phase;
  if (typeof record.plus === "number" && Number.isFinite(record.plus)) tool.plus = record.plus;
  if (typeof record.minus === "number" && Number.isFinite(record.minus)) tool.minus = record.minus;
  return tool;
}

function finiteIndex(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) ? Math.max(0, Math.floor(value)) : 0;
}

function parseWords(value: unknown): SpokenWord[] {
  if (!Array.isArray(value)) return [];
  const words: SpokenWord[] = [];
  for (const item of value) {
    if (!item || typeof item !== "object" || Array.isArray(item)) continue;
    const record = item as Record<string, unknown>;
    if (typeof record.text !== "string" || !record.text) continue;
    const word: SpokenWord = { text: record.text };
    if (typeof record.startMs === "number" && Number.isFinite(record.startMs)) {
      word.startMs = record.startMs;
    }
    if (typeof record.endMs === "number" && Number.isFinite(record.endMs)) {
      word.endMs = record.endMs;
    }
    words.push(word);
  }
  return words;
}

function legacyLines(record: Record<string, unknown>): PresentationLine[] {
  const lines: PresentationLine[] = [];
  const headline = typeof record.headline === "string" ? record.headline : "";
  const body = typeof record.body === "string" ? record.body : "";
  if (headline) {
    lines.push({
      kind: "assistant",
      text: headline,
      words: parseWords(record.words),
      spokenIndex: finiteIndex(record.spokenIndex),
    });
  }
  if (body && collapseKey(body) !== collapseKey(headline)) {
    lines.push({
      kind: "assistant",
      text: body,
      words: body.split(/\s+/).filter(Boolean).map((text) => ({ text })),
      spokenIndex: 0,
    });
  }
  if (Array.isArray(record.cards)) {
    for (const item of record.cards) {
      if (!item || typeof item !== "object" || Array.isArray(item)) continue;
      const card = item as Record<string, unknown>;
      if (typeof card.title !== "string" || !card.title) continue;
      if (card.kind === "tool" || card.kind === "edits" || card.kind === "progress") {
        lines.push({
          kind: card.kind === "progress" ? "progress" : "tool",
          text: card.title,
        });
      }
    }
  }
  return lines;
}

function collapseKey(text: string): string {
  return text.toLowerCase().replace(/\s+/g, "");
}

/** A job's dot: needs the caller, working, queued, done or stopped. */
export function dotState(job: WorkerJob): string {
  if (job.waiting) return "attention";
  if (job.phase === "running") return "live";
  if (job.phase === "queued") return "queued";
  return job.phase === "completed" ? "done" : "stopped";
}

/** The pill's dot across every job: needing the caller beats working beats done. */
export function worstState(jobs: readonly WorkerJob[]): string {
  const states = jobs.map(dotState);
  for (const state of ["attention", "live", "queued", "done"]) if (states.includes(state)) return state;
  return "stopped";
}
