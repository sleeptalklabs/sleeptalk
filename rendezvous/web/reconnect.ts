/**
 * Bounded reconnect policy for the browser signaling socket
 * (r2-browser-connectivity).
 *
 * Pure decision logic so bounds and backoff are unit-testable: the caller
 * owns the timer and the socket; this module answers "given the attempt
 * count, what happens next" — retry with a backoff delay, or exhaust.
 */

export const SIGNAL_RECONNECT_MAX_ATTEMPTS = 3;
export const SIGNAL_RECONNECT_BASE_DELAY_MS = 1_000;

export type ReconnectDecision =
  | { action: "retry"; attempt: number; delayMs: number }
  | { action: "exhausted"; attempts: number };

/** Backoff for attempt N (1-based): 1s, 2s, 4s — bounded, no jitter needed
 * for a single browser client. */
export function reconnectDelayMs(attempt: number): number {
  return SIGNAL_RECONNECT_BASE_DELAY_MS * 2 ** (attempt - 1);
}

/** Decide the next step after a signaling loss. Attempts are 0-based
 * (completed attempts); the decision names the upcoming 1-based attempt. */
export function decideReconnect(attemptsCompleted: number): ReconnectDecision {
  if (attemptsCompleted >= SIGNAL_RECONNECT_MAX_ATTEMPTS) {
    return { action: "exhausted", attempts: attemptsCompleted };
  }
  const attempt = attemptsCompleted + 1;
  return { action: "retry", attempt, delayMs: reconnectDelayMs(attempt) };
}
