import type { AssistantLine, PresentationLine } from "./presentation";

/** The transcript as keyed blocks (no DOM here, so it can be tested anywhere). */

export type Block = {
  key: string;
  kind: PresentationLine["kind"];
  lines: PresentationLine[];
  /** What the block shows; a change means its element is redrawn. */
  sig: string;
  /** A reply's words, in order (assistant blocks only). */
  words: string[];
  /** A shortened reply's whole answer (assistant blocks only); "" if it was said in full. */
  full: string;
};

/** Words compare without case or punctuation: "it" and "it." are the same word. */
export function sameWord(a: string, b: string): boolean {
  return norm(a) === norm(b);
}

function norm(text: string): string {
  return text.toLowerCase().replace(/[^\p{L}\p{N}]+/gu, "");
}

function lineWords(line: AssistantLine): string[] {
  if (line.words.length) return line.words.map((word) => word.text);
  return line.text.split(/\s+/).filter(Boolean);
}

/** How many leading words two versions of a reply share. */
export function sharedWords(before: readonly string[], after: readonly string[]): number {
  let count = 0;
  while (count < before.length && count < after.length && sameWord(before[count], after[count])) count += 1;
  return count;
}

/**
 * The feed as keyed blocks. Consecutive reply lines are one block (one
 * paragraph), as are consecutive worker milestones; everything else is a
 * block of its own. A caller line is keyed by its words (and how often they
 * were said), everything after it by that key, its kind and its order.
 */
export function transcriptBlocks(lines: readonly PresentationLine[]): Block[] {
  const blocks: Block[] = [];
  const saidBefore = new Map<string, number>();
  let anchor = "start";
  let counts = new Map<string, number>();
  for (const line of lines) {
    const last = blocks[blocks.length - 1];
    if (last && (line.kind === "assistant" || line.kind === "worker") && last.kind === line.kind) {
      last.lines.push(line);
      continue;
    }
    let key: string;
    if (line.kind === "user") {
      const words = norm(line.text);
      const times = (saidBefore.get(words) ?? 0) + 1;
      saidBefore.set(words, times);
      key = `you:${words}#${times}`;
      anchor = key;
      counts = new Map();
    } else {
      const n = (counts.get(line.kind) ?? 0) + 1;
      counts.set(line.kind, n);
      key = `${anchor}|${line.kind}#${n}`;
    }
    blocks.push({ key, kind: line.kind, lines: [line], sig: "", words: [], full: "" });
  }
  for (const block of blocks) {
    if (block.kind === "assistant") {
      block.words = block.lines.flatMap((line) => lineWords(line as AssistantLine));
      block.full = [...block.lines].reverse().map((line) => (line as AssistantLine).full ?? "").find(Boolean) ?? "";
      block.sig = `${block.words.join(" ")}\u0000${block.full}`;
    } else {
      block.sig = JSON.stringify(block.lines);
    }
  }
  return blocks;
}

