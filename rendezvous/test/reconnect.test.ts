import { describe, expect, it } from "vitest";

import {
  SIGNAL_RECONNECT_MAX_ATTEMPTS,
  decideReconnect,
  reconnectDelayMs,
} from "../web/reconnect";

describe("bounded signaling reconnect (r2-browser-connectivity)", () => {
  it("retries with backoff: 1s, 2s, 4s", () => {
    expect(decideReconnect(0)).toEqual({ action: "retry", attempt: 1, delayMs: 1_000 });
    expect(decideReconnect(1)).toEqual({ action: "retry", attempt: 2, delayMs: 2_000 });
    expect(decideReconnect(2)).toEqual({ action: "retry", attempt: 3, delayMs: 4_000 });
  });

  it("exhausts after the bounded attempt count", () => {
    expect(decideReconnect(SIGNAL_RECONNECT_MAX_ATTEMPTS)).toEqual({
      action: "exhausted",
      attempts: SIGNAL_RECONNECT_MAX_ATTEMPTS,
    });
    expect(decideReconnect(99)).toEqual({ action: "exhausted", attempts: 99 });
  });

  it("bounds total worst-case reconnect window to 7 seconds", () => {
    let total = 0;
    for (let i = 0; i < SIGNAL_RECONNECT_MAX_ATTEMPTS; i += 1) {
      total += reconnectDelayMs(i + 1);
    }
    expect(total).toBe(7_000); // 1s + 2s + 4s
  });
});
