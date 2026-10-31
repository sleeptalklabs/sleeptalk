import { describe, expect, it } from "vitest";
import {
  DEFAULT_SPEED,
  NARRATION_STYLES,
  REPLY_LENGTHS,
  SPEED_STEPS,
  callSettingPayload,
  narrationLabel,
  replyLengthLabel,
  snapNarration,
  snapReplyLength,
  snapSpeed,
  speedLabel,
} from "../web/settings";

describe("call setting messages", () => {
  it("uses the host's canonical field names", () => {
    expect(callSettingPayload({ kind: "ttsSpeed", value: 1.25 })).toEqual({ ttsSpeed: 1.25 });
    expect(callSettingPayload({ kind: "narrationStyle", value: "silence" })).toEqual({
      narrationStyle: "silence",
    });
    expect(callSettingPayload({ kind: "replyLength", value: "summarized" })).toEqual({
      replyLength: "summarized",
    });
  });

  it("names the job a tab cancels or selects", () => {
    expect(callSettingPayload({ kind: "cancelJob", jobId: "job-2" })).toEqual({ cancelJob: "job-2" });
    expect(callSettingPayload({ kind: "selectJob", jobId: "job-3" })).toEqual({ selectJob: "job-3" });
  });
});

describe("speed staircase", () => {
  it("matches sleeptalk/speech_speed.py SPEED_STEPS", () => {
    expect([...SPEED_STEPS]).toEqual([0.5, 1, 1.25, 1.5]);
  });

  it("is idempotent on its own steps", () => {
    for (const step of SPEED_STEPS) expect(snapSpeed(step)).toBe(step);
  });

  it("snaps to the nearest step", () => {
    expect(snapSpeed(0.6)).toBe(0.5);
    expect(snapSpeed(1.2)).toBe(1.25);
    expect(snapSpeed(1.7)).toBe(1.5);
    expect(snapSpeed(5)).toBe(1.5);
    expect(snapSpeed(0)).toBe(0.5);
  });

  it("breaks exact ties upward, like snap_speed", () => {
    expect(snapSpeed(0.75)).toBe(1);
    expect(snapSpeed(1.375)).toBe(1.5);
    expect(snapSpeed(1.75)).toBe(1.5);
  });

  it("never throws on junk from the wire", () => {
    expect(snapSpeed(undefined)).toBe(DEFAULT_SPEED);
    expect(snapSpeed(null)).toBe(DEFAULT_SPEED);
    expect(snapSpeed("nonsense")).toBe(DEFAULT_SPEED);
    expect(snapSpeed(Number.NaN)).toBe(DEFAULT_SPEED);
    expect(snapSpeed("1.25")).toBe(1.25);
  });

  it("labels 1x as Normal", () => {
    expect(speedLabel(1)).toBe("Normal");
    expect(speedLabel(0.5)).toBe("0.5x");
    expect(speedLabel(1.25)).toBe("1.25x");
    expect(speedLabel(1.5)).toBe("1.5x");
  });
});

describe("narration styles", () => {
  it("matches sleeptalk.policy.NARRATION_STYLES", () => {
    expect([...NARRATION_STYLES]).toEqual(["narrated", "silence", "keyboard"]);
  });

  it("keeps tool narration quiet by default", () => {
    for (const junk of [undefined, null, "", "quiet", 7, {}]) {
      expect(snapNarration(junk)).toBe("silence");
    }
  });

  it("accepts known styles case-insensitively", () => {
    expect(snapNarration("KEYBOARD")).toBe("keyboard");
    expect(snapNarration(" silence ")).toBe("silence");
  });

  it("labels each style for the menu", () => {
    expect(narrationLabel("narrated")).toBe("Narrated");
    expect(narrationLabel("silence")).toBe("Silent");
    expect(narrationLabel("keyboard")).toBe("Keyboard");
  });
});

describe("reply lengths", () => {
  it("matches sleeptalk.policy.REPLY_LENGTHS", () => {
    expect([...REPLY_LENGTHS]).toEqual(["full", "summarized", "concise"]);
  });

  it("falls back to full, never to summarized", () => {
    for (const junk of [undefined, null, "", "brief", 7, {}]) {
      expect(snapReplyLength(junk)).toBe("full");
    }
  });

  it("accepts known lengths case-insensitively", () => {
    expect(snapReplyLength("SUMMARIZED")).toBe("summarized");
    expect(snapReplyLength(" full ")).toBe("full");
  });

  it("labels each length for the menu", () => {
    expect(replyLengthLabel("full")).toBe("Full");
    expect(replyLengthLabel("summarized")).toBe("Summarized");
  });
});

it("diagnostic recording sends an explicit call-only boolean", () => {
  expect(callSettingPayload({ kind: "diagnostics", value: true })).toEqual({ diagnostics: true });
  expect(callSettingPayload({ kind: "diagnostics", value: false })).toEqual({ diagnostics: false });
  expect(replyLengthLabel("concise")).toBe("Concise speech");
});
