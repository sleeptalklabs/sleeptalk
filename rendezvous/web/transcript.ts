import type { PresentationLine } from "./presentation";
import { sharedWords, transcriptBlocks, type Block } from "./transcript_blocks";

export type { Block } from "./transcript_blocks";

/**
 * The call's transcript, kept in place between snapshots.
 *
 * The host sends the whole conversation on every change. Rebuilding the feed
 * each time made finished text animate again (in waves, when a merge or the
 * line limit shifted which line was which) and cut short words that were
 * still fading in. Here each block has a stable key: a reply is keyed by the
 * caller's words before it, so the same reply is found again and only its
 * new words are added and animated. A reply's sentences flow as one
 * paragraph instead of a row per sentence.
 */

type View = { key: string; sig: string; el: HTMLElement; words: string[]; speaker: string };

/** New words fade in one after another; a long run (catching up after a
 * reconnect, a page that just opened) appears at once instead of in a wave. */
const MAX_ANIMATED_WORDS = 60;
const STAGGER_CAP = 12;

/**
 * A reply shortened for speech shows what was said; the agent's whole answer
 * is folded under it, so the feed matches the voice and nothing is lost.
 */
function setFull(wrap: HTMLElement, full: string): void {
  let fold = wrap.querySelector<HTMLDetailsElement>(":scope > details.full-answer");
  if (!full) {
    fold?.remove();
    return;
  }
  if (!fold) {
    fold = document.createElement("details");
    fold.className = "full-answer";
    const summary = document.createElement("summary");
    summary.textContent = "Full answer";
    const text = document.createElement("p");
    text.className = "full-answer-text";
    fold.append(summary, text);
    wrap.appendChild(fold);
  }
  const text = fold.querySelector<HTMLElement>(".full-answer-text");
  if (text && text.textContent !== full) text.textContent = full;
}

export class Transcript {
  private views: View[] = [];
  private painted = false;

  constructor(
    private readonly feed: HTMLElement,
    /** Draws a block other than a reply (caller lines, tools, worker milestones). */
    private readonly draw: (block: Block) => HTMLElement,
  ) {}

  reset(): void {
    this.views = [];
    this.painted = false;
    this.feed.replaceChildren();
  }

  render(lines: readonly PresentationLine[], speaker: string): void {
    const next = transcriptBlocks(lines);
    // Lines that fell off the top of the host's window: drop their views.
    const start = next.length ? this.views.findIndex((view) => view.key === next[0].key) : -1;
    if (start < 0) {
      for (const view of this.views) view.el.remove();
      this.views = [];
    } else if (start > 0) {
      for (const view of this.views.slice(0, start)) view.el.remove();
      this.views = this.views.slice(start);
    }
    const kept: View[] = [];
    let index = 0;
    for (; index < next.length && index < this.views.length; index += 1) {
      const view = this.views[index];
      const block = next[index];
      if (view.key !== block.key) break;
      if (view.sig !== block.sig || view.speaker !== speaker) this.update(view, block, speaker);
      kept.push(view);
    }
    for (const view of this.views.slice(index)) view.el.remove();
    for (const block of next.slice(index)) kept.push(this.create(block, speaker));
    this.views = kept;
    this.painted = true;
  }

  private create(block: Block, speaker: string): View {
    const el = block.kind === "assistant" ? this.reply(block.words, speaker, this.painted ? 0 : -1) : this.draw(block);
    if (block.kind === "assistant") setFull(el, block.full);
    this.place(el);
    return { key: block.key, sig: block.sig, el, words: block.words, speaker };
  }

  private update(view: View, block: Block, speaker: string): void {
    const wasOpen = view.el.querySelector<HTMLDetailsElement>(":scope > details.full-answer")?.open ?? false;
    if (block.kind !== "assistant" || view.speaker !== speaker) {
      const el = block.kind === "assistant" ? this.reply(block.words, speaker, -1) : this.draw(block);
      view.el.replaceWith(el);
      view.el = el;
    } else {
      const body = view.el.querySelector<HTMLElement>(".line-body");
      const shared = sharedWords(view.words, block.words);
      if (body && shared === view.words.length) {
        // The reply grew: add only its new words. Words already shown keep
        // their element (and any fade still running); a tidied word
        // ("it" to "it.") changes in place.
        const spans = Array.from(body.children) as HTMLElement[];
        block.words.slice(0, shared).forEach((word, i) => {
          const text = `${i ? " " : ""}${word}`;
          if (spans[i] && spans[i].textContent !== text) spans[i].textContent = text;
        });
        this.addWords(body, block.words, shared, shared);
      } else {
        // Rewritten (merged or tidied mid-reply): redraw, fading in only
        // the words past what was shown before.
        const el = this.reply(block.words, speaker, view.words.length);
        view.el.replaceWith(el);
        view.el = el;
      }
    }
    if (block.kind === "assistant") {
      setFull(view.el, block.full);
      // A redraw replaced the element: a fold the caller opened stays open.
      const fold = view.el.querySelector<HTMLDetailsElement>(":scope > details.full-answer");
      if (fold && wasOpen) fold.open = true;
    }
    view.sig = block.sig;
    view.words = block.words;
    view.speaker = speaker;
  }

  /** A reply as one paragraph. Words from `fadeFrom` on fade in (-1: none). */
  private reply(words: readonly string[], speaker: string, fadeFrom: number): HTMLElement {
    const wrap = document.createElement("div");
    wrap.className = "block assistant";
    const line = document.createElement("p");
    line.className = "line assistant";
    const gutter = document.createElement("span");
    gutter.className = "gutter";
    gutter.textContent = speaker || "agent";
    const body = document.createElement("span");
    body.className = "line-body";
    line.append(gutter, body);
    wrap.appendChild(line);
    this.addWords(body, words, 0, fadeFrom < 0 ? words.length : fadeFrom);
    return wrap;
  }

  private addWords(body: HTMLElement, words: readonly string[], from: number, fadeFrom: number): void {
    const fading = words.length - Math.max(from, fadeFrom);
    const animate = fading > 0 && fading <= MAX_ANIMATED_WORDS;
    for (let i = from; i < words.length; i += 1) {
      const span = document.createElement("span");
      span.className = "word";
      span.textContent = `${i ? " " : ""}${words[i]}`;
      if (animate && i >= fadeFrom) {
        span.classList.add("enter");
        span.style.setProperty("--word-i", String(Math.min(i - Math.max(from, fadeFrom), STAGGER_CAP)));
        span.addEventListener("animationend", () => span.classList.remove("enter"), { once: true });
      }
      body.appendChild(span);
    }
  }

  /** New blocks go before the "you are speaking" hint, which stays last. */
  private place(el: HTMLElement): void {
    const hint = this.feed.querySelector("[data-speaking-hint]");
    if (hint) this.feed.insertBefore(el, hint);
    else this.feed.appendChild(el);
  }
}
