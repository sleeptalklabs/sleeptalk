/**
 * In-call settings vocabulary.
 *
 * The speed staircase mirrors sleeptalk/speech/speed.py exactly, including the
 * tie rule: a value equidistant between two steps snaps to the higher one. The
 * host snaps again on receipt, so a drift between these two lists shows up as
 * a menu that checks one speed while the agent talks at another.
 */

// Mirrors sleeptalk/speech_speed.py SPEED_STEPS. 2x was removed: no
// supported cloud provider plays it.
export const SPEED_STEPS = [0.5, 1, 1.25, 1.5] as const;
export type SpeedStep = (typeof SPEED_STEPS)[number];
export const DEFAULT_SPEED: SpeedStep = 1;

function finite(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim() !== "") {
    const parsed = Number(value);
    if (Number.isFinite(parsed)) return parsed;
  }
  return null;
}

/** Nearest step. Unparseable input is 1x, never a throw: this runs mid-call. */
export function snapSpeed(value: unknown): SpeedStep {
  const n = finite(value);
  if (n === null) return DEFAULT_SPEED;
  let best: SpeedStep = SPEED_STEPS[0];
  for (const step of SPEED_STEPS) {
    const closer = Math.abs(step - n) < Math.abs(best - n);
    const tieGoesHigher = Math.abs(step - n) === Math.abs(best - n) && step > best;
    if (closer || tieGoesHigher) best = step;
  }
  return best;
}

/** YouTube's label set: 1x reads as "Normal", everything else as a multiple. */
export function speedLabel(step: SpeedStep): string {
  return step === 1 ? "Normal" : `${step}x`;
}

export const NARRATION_STYLES = ["narrated", "silence", "keyboard"] as const;
export type NarrationStyle = (typeof NARRATION_STYLES)[number];
export const DEFAULT_NARRATION: NarrationStyle = "silence";

const NARRATION_LABELS: Record<NarrationStyle, string> = {
  narrated: "Narrated",
  silence: "Silent",
  keyboard: "Keyboard",
};

/**
 * Unknown styles fall back to silence, matching
 * sleeptalk.policy.narration_style_or_default. New calls keep tool work quiet unless narration was selected.
 */
export function snapNarration(value: unknown): NarrationStyle {
  const style = String(value ?? "").trim().toLowerCase();
  return (NARRATION_STYLES as readonly string[]).includes(style)
    ? (style as NarrationStyle)
    : DEFAULT_NARRATION;
}

export function narrationLabel(style: NarrationStyle): string {
  return NARRATION_LABELS[style];
}

export const REPLY_LENGTHS = ["full", "summarized", "concise"] as const;
export type ReplyLength = (typeof REPLY_LENGTHS)[number];
export const DEFAULT_REPLY_LENGTH: ReplyLength = "full";

export type CallSetting =
  | { kind: "ttsSpeed"; value: SpeedStep }
  | { kind: "narrationStyle"; value: NarrationStyle }
  | { kind: "replyLength"; value: ReplyLength }
  | { kind: "ttsVoice"; value: string }
  | { kind: "openSettings" }
  | { kind: "diagnostics"; value: boolean }
  | { kind: "cancelJob"; jobId: string }
  | { kind: "selectJob"; jobId: string };

export function callSettingPayload(setting: CallSetting): Record<string, unknown> {
  switch (setting.kind) {
    case "ttsSpeed":
      return { ttsSpeed: setting.value };
    case "narrationStyle":
      return { narrationStyle: setting.value };
    case "replyLength":
      return { replyLength: setting.value };
    case "ttsVoice":
      return { ttsVoice: setting.value };
    case "openSettings":
      return { openSettings: true };
    case "diagnostics":
      return { diagnostics: setting.value };
    case "cancelJob":
      return { cancelJob: setting.jobId };
    case "selectJob":
      return { selectJob: setting.jobId };
    default: {
      const _exhaustive: never = setting;
      return _exhaustive;
    }
  }
}

const REPLY_LABELS: Record<ReplyLength, string> = {
  full: "Full",
  summarized: "Summarized",
  concise: "Concise speech",
};

/**
 * Unknown lengths fall back to full, matching
 * sleeptalk.policy.reply_length_or_default. The mirror of snapNarration's
 * rule rather than a copy of it: a value that did not round-trip must cost
 * the listener time, never information.
 */
export function snapReplyLength(value: unknown): ReplyLength {
  const length = String(value ?? "").trim().toLowerCase();
  return (REPLY_LENGTHS as readonly string[]).includes(length)
    ? (length as ReplyLength)
    : DEFAULT_REPLY_LENGTH;
}

export function replyLengthLabel(length: ReplyLength): string {
  return REPLY_LABELS[length];
}
