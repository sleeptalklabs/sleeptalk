/**
 * Stable failure categories for ICE/connection diagnostics
 * (r2-browser-ice-diagnostics).
 *
 * Operators need one vocabulary across browser, Rendezvous, and host —
 * ad-hoc error names (`ice_failed`, `AbortError`, …) are mapped here to a
 * small, stable taxonomy that telemetry and the host runtime agree on.
 * The mapping is pure so failure categorization is testable; nothing here
 * touches SDP, candidate addresses, audio, or transcripts.
 */

export type FailureCategory =
  | "signaling_unreachable"
  | "signaling_rejected"
  | "permission_denied"
  | "ice_failed"
  | "ice_timeout"
  | "peer_disconnected"
  | "media_stalled"
  | "unknown";

/** Map a raw error name/code/state to its stable category. */
export function categorizeFailure(raw: string): FailureCategory {
  const value = raw.trim().toLowerCase();
  if (!value) return "unknown";
  if (
    value.includes("signal") ||
    value.includes("network") ||
    value.includes("abort") ||
    value.includes("fetch")
  ) {
    return "signaling_unreachable";
  }
  if (value.includes("403") || value.includes("401") || value.includes("rejected")) {
    return "signaling_rejected";
  }
  if (value.includes("permission") || value.includes("notallowed") || value.includes("microphone")) {
    return "permission_denied";
  }
  if (value.includes("ice_failed") || value.includes("ice")) return "ice_failed";
  if (value.includes("timeout") || value.includes("timed out")) return "ice_timeout";
  if (value.includes("disconnect") || value.includes("closed") || value.includes("left")) {
    return "peer_disconnected";
  }
  if (value.includes("stall") || value.includes("silent")) return "media_stalled";
  return "unknown";
}

/** Selected network path as a stable operator-visible value. */
export function pathType(summary: {
  usesRelay?: boolean;
  localCandidateType?: string;
  remoteCandidateType?: string;
}): "relay" | "direct" | undefined {
  if (summary.usesRelay === true) return "relay";
  if (summary.usesRelay === false) return "direct";
  const local = summary.localCandidateType;
  const remote = summary.remoteCandidateType;
  if (local === "relay" || remote === "relay") return "relay";
  if (local && remote) return "direct";
  return undefined;
}

/**
 * The correlation key operators use to join browser telemetry with the
 * Rendezvous worker and host logs: the call's routing id + the browser's
 * start time. Redacted by construction — no tokens, addresses, or media.
 */
export function correlationKey(routingId: string, startedAtMs: number): string {
  return `${routingId}:${Math.round(startedAtMs)}`;
}
