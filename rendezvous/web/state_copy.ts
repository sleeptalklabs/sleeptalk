/**
 * Recovery-state copy and actions for the browser call UI
 * (r2-browser-ui-flow).
 *
 * One place maps every backend state (ADR 0003 §2 + TicketEndReason) to
 * visible copy and the actions the UI allows in that state. Pure data —
 * main.ts renders it; tests assert every state has copy and an action,
 * and that no copy renders tokens or secrets.
 */

export type CallUiState =
  | "pending"
  | "permission"
  | "connecting"
  | "connected"
  | "reconnecting"
  | "expired"
  | "failed"
  | "cancelled"
  | "ended";

export interface StateCopy {
  /** Short heading shown in the card. */
  heading: string;
  /** Body copy with the concrete next step. */
  detail: string;
  /** Actions offered in this state (empty = no actions, read-only card). */
  actions: ReadonlyArray<"reload" | "ask_new_link" | "return_to_chat">;
}

export const STATE_COPY: Record<CallUiState, StateCopy> = {
  pending: {
    heading: "Almost there",
    detail: "Tap Talk to continue. Your microphone is requested when you start.",
    actions: ["ask_new_link"],
  },
  permission: {
    heading: "Microphone access is blocked",
    detail:
      "Allow microphone access for this page in your browser settings " +
      "(padlock icon in the address bar → Microphone → Allow), then reload this link.",
    actions: ["reload", "ask_new_link"],
  },
  connecting: {
    heading: "Connecting…",
    detail: "Setting up the audio connection. This usually takes a few seconds.",
    actions: [],
  },
  connected: {
    heading: "You're live",
    detail: "Speak naturally; the agent listens and answers.",
    actions: [],
  },
  reconnecting: {
    heading: "Reconnecting…",
    detail: "The connection dropped. Hold on — we're retrying automatically.",
    actions: ["ask_new_link"],
  },
  expired: {
    heading: "This call link has expired",
    detail: "Return to the conversation and ask for a new link.",
    actions: ["ask_new_link"],
  },
  failed: {
    heading: "This call link isn't available",
    detail: "Return to the conversation and ask for a new link.",
    actions: ["ask_new_link"],
  },
  cancelled: {
    heading: "Call cancelled",
    detail: "The call was cancelled. Ask for a new link when you want to talk again.",
    actions: ["ask_new_link"],
  },
  ended: {
    heading: "Call ended",
    detail: "This call is over. Ask for a new link when you want to talk again.",
    actions: ["ask_new_link"],
  },
};

/** Map a backend TicketEndReason to its UI state. */
export function uiStateForEndReason(reason: string): CallUiState {
  switch (reason) {
    case "ticket_expired":
      return "expired";
    case "user_cancel":
      return "cancelled";
    case "peer_hung_up":
    case "agent_disconnected":
    case "browser_disconnected":
    case "connection_lost":
    case "session_cap":
    case "inactivity_timeout":
      return "ended";
    default:
      return "ended";
  }
}

/** Every state must render non-empty copy and declare its actions. */
export function validateStateCopy(): string[] {
  const problems: string[] = [];
  for (const [state, copy] of Object.entries(STATE_COPY)) {
    if (!copy.heading.trim()) problems.push(`${state}: empty heading`);
    if (!copy.detail.trim()) problems.push(`${state}: empty detail`);
    if (!copy.detail.includes(".")) problems.push(`${state}: detail lacks a next step`);
  }
  return problems;
}
