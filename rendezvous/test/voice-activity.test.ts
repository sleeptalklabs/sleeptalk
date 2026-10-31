import { describe, expect, it } from "vitest";
import { voiceState } from "../web/voice-activity";

const quiet = {
  host: null,
  remoteLevel: 0,
  localLevel: 0,
  now: 10_000,
  lastRemoteSpeechAt: 0,
  lastLocalSpeechAt: 0,
};

describe("audible voice ownership", () => {
  it.each(["idle", "thinking", "listening"] as const)(
    "shows agent speech even when the latest host state is %s",
    (host) => expect(voiceState({ ...quiet, host, remoteLevel: 0.2 })).toBe("speaking"),
  );

  it("stays with the agent through a short gap between syllables", () => {
    expect(voiceState({ ...quiet, host: "thinking", lastRemoteSpeechAt: 9_900 })).toBe("speaking");
  });

  it("releases the agent after playback ends", () => {
    expect(voiceState({ ...quiet, host: "listening", lastRemoteSpeechAt: 9_000 })).toBe("listening");
  });

  it("hears an interruption even if a speaking event is stale", () => {
    expect(voiceState({ ...quiet, host: "speaking", localLevel: 0.3 })).toBe("listening");
  });

  it("keeps quiet input idle", () => {
    expect(voiceState(quiet)).toBe("idle");
  });
});
