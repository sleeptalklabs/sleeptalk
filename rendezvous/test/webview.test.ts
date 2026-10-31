import { describe, expect, it } from "vitest";

import { browserRuntimePolicy } from "../web/webview";

describe("embedded browser policy", () => {
  it("requires relay inside known chat webviews without making brands the domain", () => {
    expect(
      browserRuntimePolicy(
        "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) " +
          "AppleWebKit/605.1.15 Mobile/15E148 Telegram/11.0",
      ),
    ).toMatchObject({
      context: "embedded",
      iceTransportPolicy: "relay",
    });
    expect(
      browserRuntimePolicy(
        "Mozilla/5.0 (Linux; Android 15; wv) AppleWebKit/537.36 " +
          "Version/4.0 Chrome/138.0 Mobile Safari/537.36 MicroMessenger/8.0.50",
      ),
    ).toMatchObject({
      context: "embedded",
      iceTransportPolicy: "relay",
    });
  });

  it("keeps regular mobile browsers direct-first", () => {
    expect(
      browserRuntimePolicy(
        "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) " +
          "AppleWebKit/605.1.15 Version/18.0 Mobile/15E148 Safari/604.1",
      ),
    ).toMatchObject({ context: "browser", iceTransportPolicy: "all" });
  });

  it("keeps any detected embedded browser in-app and relay-only", () => {
    expect(
      browserRuntimePolicy(
        "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) " +
          "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148",
      ),
    ).toMatchObject({
      context: "embedded",
      iceTransportPolicy: "relay",
    });
    expect(
      browserRuntimePolicy(
        "Mozilla/5.0 (Linux; Android 15; Pixel 9 Build/AP4A; wv) " +
          "AppleWebKit/537.36 Version/4.0 Chrome/138.0 Mobile Safari/537.36",
      ),
    ).toMatchObject({
      context: "embedded",
      iceTransportPolicy: "relay",
    });
  });

  it("recognizes additional chat-app hints without dedicated context variants", () => {
    expect(
      browserRuntimePolicy(
        "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) " +
          "AppleWebKit/605.1.15 Version/18.0 Mobile/15E148 Safari/604.1 WhatsApp/2.26",
      ),
    ).toMatchObject({ context: "embedded", iceTransportPolicy: "relay" });
    expect(
      browserRuntimePolicy(
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) " +
          "AppleWebKit/537.36 Chrome/138.0 Safari/537.36 Slack/4.44",
      ),
    ).toMatchObject({ context: "embedded", iceTransportPolicy: "relay" });
  });
});

// -- r2-browser-ui-flow: recovery-state copy coverage -------------------------

import { STATE_COPY, uiStateForEndReason, validateStateCopy } from "../web/state_copy";

describe("recovery-state copy (r2-browser-ui-flow)", () => {
  const REQUIRED_STATES = [
    "pending",
    "permission",
    "connecting",
    "connected",
    "reconnecting",
    "expired",
    "failed",
    "cancelled",
    "ended",
  ] as const;

  it("every required state has copy with a next step and allowed actions", () => {
    for (const state of REQUIRED_STATES) {
      const copy = STATE_COPY[state];
      expect(copy.heading.length).toBeGreaterThan(0);
      expect(copy.detail.length).toBeGreaterThan(0);
      expect(copy.detail.includes(".")).toBe(true); // concrete next step
      expect(Array.isArray(copy.actions)).toBe(true);
    }
    // validator agrees
    expect(validateStateCopy()).toEqual([]);
  });

  it("mic permission denial is actionable", () => {
    const copy = STATE_COPY.permission;
    expect(copy.detail).toMatch(/settings/i);
    expect(copy.detail).toMatch(/microphone/i);
    expect(copy.actions).toContain("reload");
  });

  it("user cancel maps to the cancelled state with distinct copy", () => {
    expect(uiStateForEndReason("user_cancel")).toBe("cancelled");
    expect(STATE_COPY.cancelled.heading).not.toBe(STATE_COPY.ended.heading);
  });

  it("every backend end reason maps to a UI state", () => {
    const reasons = [
      "ticket_expired",
      "session_cap",
      "inactivity_timeout",
      "peer_hung_up",
      "user_cancel",
      "agent_disconnected",
      "browser_disconnected",
      "connection_lost",
      "call_ended",
    ];
    for (const reason of reasons) {
      expect(Object.keys(STATE_COPY)).toContain(uiStateForEndReason(reason));
    }
    expect(uiStateForEndReason("ticket_expired")).toBe("expired");
  });

  it("no token or secret is rendered in state copy", () => {
    const blob = JSON.stringify(STATE_COPY);
    // copy contains no ticket-shaped secrets (43-char base64url tickets)
    expect(blob).not.toMatch(/[A-Za-z0-9_-]{40,}/);
    expect(blob.toLowerCase()).not.toContain("token");
    expect(blob.toLowerCase()).not.toContain("secret");
    expect(blob.toLowerCase()).not.toContain("key");
  });
});
