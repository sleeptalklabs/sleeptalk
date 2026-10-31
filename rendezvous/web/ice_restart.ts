/**
 * Bounded ICE restart after a mid-call path death.
 *
 * First-connect failures stay fatal. After the call has connected once,
 * a disconnected/failed ICE path gets a restart instead of hanging up.
 * The caller owns the timer and createOffer; this module only decides.
 */

export const ICE_DISCONNECT_RESTART_MS = 2_000;
export const ICE_RESTART_MAX_ATTEMPTS = 2;

export type IceRestartDecision =
  | { action: "ignore" }
  | { action: "wait"; delayMs: number }
  | { action: "restart" }
  | { action: "give_up" };

export function decideIceRestart(input: {
  iceState: string;
  connectedOnce: boolean;
  restartAttempts: number;
  ended: boolean;
}): IceRestartDecision {
  if (input.ended || !input.connectedOnce) {
    return { action: "ignore" };
  }
  if (input.iceState === "failed") {
    return input.restartAttempts >= ICE_RESTART_MAX_ATTEMPTS
      ? { action: "give_up" }
      : { action: "restart" };
  }
  if (input.iceState === "disconnected") {
    return input.restartAttempts >= ICE_RESTART_MAX_ATTEMPTS
      ? { action: "ignore" }
      : { action: "wait", delayMs: ICE_DISCONNECT_RESTART_MS };
  }
  return { action: "ignore" };
}
