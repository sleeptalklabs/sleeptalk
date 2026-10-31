import { describe, expect, it } from "vitest";

import { parsePresentation, type AssistantLine, type PresentationLine } from "../web/presentation";
import { sharedWords, transcriptBlocks } from "../web/transcript_blocks";

const you = (text: string): PresentationLine => ({ kind: "user", text });
const agent = (text: string): AssistantLine => ({ kind: "assistant", text, words: [], spokenIndex: 0 });

describe("transcript blocks", () => {
  it("joins a reply's sentences into one paragraph", () => {
    const blocks = transcriptBlocks([you("hi"), agent("Hello there."), agent("How can I help?")]);
    expect(blocks.map((b) => b.kind)).toEqual(["user", "assistant"]);
    expect(blocks[1].words).toEqual(["Hello", "there.", "How", "can", "I", "help?"]);
  });

  it("keeps a reply's key when older lines fall off the top", () => {
    const full = transcriptBlocks([you("first"), agent("One."), you("second"), agent("Two.")]);
    const trimmed = transcriptBlocks([you("second"), agent("Two."), agent("And more.")]);
    expect(trimmed[1].key).toBe(full[3].key);
  });

  it("tells apart the same words said twice", () => {
    const blocks = transcriptBlocks([you("yes"), agent("Okay."), you("yes"), agent("Done.")]);
    expect(new Set(blocks.map((b) => b.key)).size).toBe(4);
  });

  it("shows a shortened reply as said, with the whole answer kept for the fold", () => {
    const said = agent("I fixed it.");
    const closing: PresentationLine = { ...agent("… The tests pass."), full: "I fixed it. Here is how. The tests pass." };
    const [, reply] = transcriptBlocks([you("how did it go"), said, closing]);
    expect(reply.words).toEqual(["I", "fixed", "it.", "…", "The", "tests", "pass."]);
    expect(reply.full).toBe("I fixed it. Here is how. The tests pass.");
    expect(transcriptBlocks([you("hi"), agent("Hello.")])[1].full).toBe("");
  });

  it("redraws a reply when only its full answer changes", () => {
    const before = transcriptBlocks([you("go"), { ...agent("Done."), full: "Done. One." }]);
    const after = transcriptBlocks([you("go"), { ...agent("Done."), full: "Done. One. Two." }]);
    expect(after[1].sig).not.toBe(before[1].sig);
  });

  it("reads the full answer from the host's snapshot", () => {
    const snapshot = parsePresentation({
      presence: "listening",
      lines: [{ kind: "assistant", text: "… Done.", spokenIndex: 0, full: "Lots. Done." }],
    });
    const line = snapshot?.lines[0];
    expect(line?.kind === "assistant" && line.full).toBe("Lots. Done.");
  });

  it("counts shared words without case or punctuation", () => {
    expect(sharedWords(["It", "works"], ["it", "works.", "Great."])).toBe(2);
    expect(sharedWords(["It", "works"], ["That", "works"])).toBe(0);
  });
});
