import { describe, expect, it } from "vitest";

import {
  AGENT_QUIET_MS,
  agentWaiting,
  hostOrbState,
  lastAssistant,
  parsePresentation,
  parseSettings,
  worstState,
  snapshotKey,
  userSpeakingHint,
} from "../web/presentation";

describe("presentation snapshots", () => {
  it("maps host working and answering onto orb states", () => {
    expect(hostOrbState("working")).toBe("thinking");
    expect(hostOrbState("answering")).toBe("speaking");
    expect(hostOrbState("speaking")).toBe("speaking");
    expect(hostOrbState("unknown")).toBeNull();
  });

  it("parses a live timeline and drops junk fields", () => {
    const snapshot = parsePresentation({
      presence: "speaking",
      muted: true,
      ttsSpeed: 1.25,
      narrationStyle: "keyboard",
      replyLength: "summarized",
      lines: [
        { kind: "user", text: "do the thing" },
        {
          kind: "assistant",
          text: "Done.",
          spokenIndex: 2.8,
          words: [
            { text: "Done.", startMs: 40, endMs: 180 },
            { text: "", startMs: 200 },
            { nope: true },
          ],
        },
        { kind: "tool", text: "edit  serve.py", callId: "c1", phase: "completed" },
        { kind: "notes", text: "The agent found three prices." },
        { kind: "mystery", text: "skip" },
      ],
    });
    expect(snapshot).toEqual({
      diagnostics: false,
      presence: "speaking",
      jobs: [],
      selectedJob: "",
      muted: true,
      hearing: false,
      partial: "",
      stack: [],
      speaker: "agent",
      voice: null,
      settings: { available: false, notice: "", url: "" },
      controls: {
        ttsSpeed: 1.25,
        narrationStyle: "keyboard",
        replyLength: "summarized",
      },
      lines: [
        { kind: "user", text: "do the thing" },
        {
          kind: "assistant",
          text: "Done.",
          spokenIndex: 2,
          words: [{ text: "Done.", startMs: 40, endMs: 180 }],
        },
        { kind: "tool", text: "edit  serve.py", callId: "c1", phase: "completed" },
        { kind: "notes", text: "The agent found three prices." },
      ],
    });
  });

  it("parses stack and live partial and drops junk stack entries", () => {
    const snapshot = parsePresentation({
      presence: "listening",
      partial: "and also the stack",
      stack: ["pi/opus", "", 12, "deepgram/nova-3", null],
      speaker: "max",
      lines: [{ kind: "user", text: "hi" }],
    });
    expect(snapshot).toMatchObject({
      presence: "listening",
      partial: "and also the stack",
      stack: ["pi/opus", "deepgram/nova-3"],
      speaker: "max",
    });
  });

  it("rejects a non-object payload", () => {
    expect(parsePresentation("nope")).toBeNull();
    expect(parsePresentation(null)).toBeNull();
  });

  it("finds the latest assistant line", () => {
    const snapshot = parsePresentation({
      presence: "speaking",
      lines: [
        { kind: "user", text: "hi" },
        { kind: "assistant", text: "One", words: [{ text: "One" }], spokenIndex: 1 },
        { kind: "tool", text: "read" },
        { kind: "assistant", text: "Two", words: [{ text: "Two" }], spokenIndex: 0 },
      ],
    });
    expect(lastAssistant(snapshot?.lines ?? [])?.text).toBe("Two");
    expect(lastAssistant([])).toBeNull();
  });

  it("userSpeakingHint keeps dots while the user turn is active even if the mic drops", () => {
    expect(userSpeakingHint({ partial: "", hearing: true })).toEqual({
      kind: "dots",
    });
    expect(userSpeakingHint({ partial: "hi", hearing: true })).toEqual({
      kind: "partial",
      text: "hi",
    });
    expect(userSpeakingHint({ partial: "", hearing: false })).toEqual({
      kind: "none",
    });
  });

  it("shows the agent's dots once the caller is done and the answer is late", () => {
    const asked = [
      { kind: "user", text: "Fix the build" },
      { kind: "progress", text: "On it." },
    ] as const;
    const waiting = { presence: "thinking", hearing: false, partial: "", lines: asked, shown: false } as const;
    // The acknowledgement is still being heard: no dots under it yet.
    expect(agentWaiting({ ...waiting, quietMs: 100 })).toBe(false);
    expect(agentWaiting({ ...waiting, quietMs: AGENT_QUIET_MS })).toBe(true);
    // Once shown, a typing sound doesn't make them blink.
    expect(agentWaiting({ ...waiting, quietMs: 0, shown: true })).toBe(true);
    // The answer started, or the caller is talking again.
    expect(agentWaiting({ ...waiting, presence: "speaking", quietMs: 5_000, shown: true })).toBe(false);
    expect(agentWaiting({ ...waiting, hearing: true, quietMs: 5_000, shown: true })).toBe(false);
    expect(agentWaiting({ ...waiting, partial: "wait", quietMs: 5_000 })).toBe(false);
    // Nobody has asked anything yet.
    expect(agentWaiting({ ...waiting, lines: [], quietMs: 5_000 })).toBe(false);

    expect(userSpeakingHint({ partial: "", hearing: false, agentWaiting: true, speaker: "claude" })).toEqual({
      kind: "waiting",
      speaker: "claude",
    });
    expect(userSpeakingHint({ partial: "", hearing: false, agentWaiting: true })).toEqual({
      kind: "waiting",
      speaker: "agent",
    });
    // The caller's own hint wins.
    expect(userSpeakingHint({ partial: "", hearing: true, agentWaiting: true })).toEqual({ kind: "dots" });
  });

  it("parses hearing so an active user turn survives a quiet mic frame", () => {
    const snapshot = parsePresentation({
      presence: "listening",
      hearing: true,
      lines: [],
    });
    expect(snapshot?.hearing).toBe(true);
  });

  it("snapshotKey ignores spokenIndex and word timings", () => {
    const base = parsePresentation({
      presence: "speaking",
      muted: false,
      speaker: "agent",
      lines: [
        {
          kind: "assistant",
          text: "Hi there.",
          spokenIndex: 0,
          words: [
            { text: "Hi", startMs: 0, endMs: 80 },
            { text: "there.", startMs: 90, endMs: 200 },
          ],
        },
        { kind: "tool", text: "read", callId: "c1", phase: "completed" },
      ],
    });
    const ticked = parsePresentation({
      presence: "speaking",
      muted: false,
      speaker: "agent",
      lines: [
        {
          kind: "assistant",
          text: "Hi there.",
          spokenIndex: 2,
          words: [
            { text: "Hi", startMs: 40, endMs: 120 },
            { text: "there.", startMs: 400, endMs: 520 },
          ],
        },
        { kind: "tool", text: "read", callId: "c1", phase: "completed" },
      ],
    });
    if (base === null || ticked === null) {
      throw new Error("expected parsed snapshots");
    }
    expect(snapshotKey(base)).toBe(snapshotKey(ticked));
  });

  it("snapshotKey changes when line text or tool fields change", () => {
    const first = parsePresentation({
      presence: "speaking",
      lines: [
        { kind: "assistant", text: "Hi", words: [{ text: "Hi" }], spokenIndex: 1 },
        { kind: "tool", text: "read", callId: "c1" },
      ],
    });
    const grew = parsePresentation({
      presence: "speaking",
      lines: [
        {
          kind: "assistant",
          text: "Hi there",
          words: [{ text: "Hi" }, { text: "there" }],
          spokenIndex: 1,
        },
        { kind: "tool", text: "read", callId: "c1" },
      ],
    });
    const otherTool = parsePresentation({
      presence: "speaking",
      lines: [
        { kind: "assistant", text: "Hi", words: [{ text: "Hi" }], spokenIndex: 1 },
        { kind: "tool", text: "read", callId: "c2" },
      ],
    });
    if (first === null || grew === null || otherTool === null) {
      throw new Error("expected parsed snapshots");
    }
    expect(snapshotKey(first)).not.toBe(snapshotKey(grew));
    expect(snapshotKey(first)).not.toBe(snapshotKey(otherTool));
  });

  it("snapshotKey changes when hearing flips", () => {
    const quiet = parsePresentation({ presence: "listening", hearing: false, lines: [] });
    const active = parsePresentation({ presence: "listening", hearing: true, lines: [] });
    if (quiet === null || active === null) {
      throw new Error("expected parsed snapshots");
    }
    expect(snapshotKey(quiet)).not.toBe(snapshotKey(active));
  });

  it("reads whether the host can open the full settings page", () => {
    expect(parseSettings({ available: true, notice: "Settings opened." })).toEqual({ available: true, notice: "Settings opened.", url: "" });
    expect(parseSettings({ available: "yes", notice: 3 })).toEqual({ available: false, notice: "", url: "" });
    expect(parseSettings(undefined)).toEqual({ available: false, notice: "", url: "" });
    const link = "https://call.sleep.talk/settings#/settings/" + "r".repeat(24) + "/" + "t".repeat(43) + "/" + "k".repeat(87);
    expect(parseSettings({ available: true, notice: "Ready.", url: link }).url).toBe(link);
    for (const bad of ["javascript:alert(1)", "http://evil.example/#/settings/x", "https://call.sleep.talk/#/call/x", "https://x.example/path#/settings/y", "https://call.sleep.talk/#/settings/z"]) {
      expect(parseSettings({ available: true, notice: "", url: bad }).url).toBe("");
    }
  });

  it("parses the worker's milestones and its own feed, dropping junk", () => {
    const snapshot = parsePresentation({
      presence: "listening",
      lines: [{ kind: "worker", text: "Done.", phase: "completed", callId: "job-1", links: ["http://localhost:8000/", 7, ""] }],
      jobs: [
        {
          jobId: "job-1",
          title: "claw machine",
          phase: "running",
          startedAt: 1000,
          steps: 3,
          waiting: "Asks: Which port?",
          entries: [
            { kind: "thinking", text: "Physics first.", startedAt: 1001 },
            { kind: "step", text: "terminal · ls -la", steps: 3, startedAt: 1002 },
            { kind: "bogus", text: "dropped" },
            { kind: "done", text: "Done.", links: ["http://localhost:8000/"] },
          ],
        },
        { title: "no id, dropped" },
      ],
    });
    expect(snapshot?.lines).toEqual([
      { kind: "worker", text: "Done.", phase: "completed", jobId: "job-1", links: ["http://localhost:8000/"] },
    ]);
    expect(snapshot?.jobs).toEqual([
      {
        jobId: "job-1",
        title: "claw machine",
        phase: "running",
        startedAt: 1000,
        steps: 3,
        waiting: "Asks: Which port?",
        entries: [
          { kind: "thinking", text: "Physics first.", startedAt: 1001, links: [] },
          { kind: "step", text: "terminal · ls -la", steps: 3, startedAt: 1002, links: [] },
          { kind: "done", text: "Done.", links: ["http://localhost:8000/"] },
        ],
      },
    ]);
  });
});

describe("several background jobs", () => {
  it("reads queued jobs and the host's selection", () => {
    const snapshot = parsePresentation({
      presence: "listening",
      lines: [],
      selectedJob: "job-2",
      jobs: [
        { jobId: "job-1", title: "latency run", phase: "running", steps: 3, entries: [] },
        { jobId: "job-2", title: "todo app", phase: "queued", steps: 0, entries: [] },
      ],
    });
    expect(snapshot?.selectedJob).toBe("job-2");
    expect(snapshot?.jobs.map((job) => job.phase)).toEqual(["running", "queued"]);
    expect(parsePresentation({ presence: "idle", lines: [] })?.selectedJob).toBe("");
  });

  it("lights the pill for the job that needs the caller first", () => {
    const job = (phase: string, waiting = "") => ({ jobId: phase, title: phase, phase, steps: 0, waiting, entries: [] }) as never;
    expect(worstState([job("completed"), job("running"), job("running", "Waiting for your OK")])).toBe("attention");
    expect(worstState([job("completed"), job("queued"), job("running")])).toBe("live");
    expect(worstState([job("completed"), job("queued")])).toBe("queued");
    expect(worstState([job("completed"), job("failed")])).toBe("done");
  });
});
