import { describe, expect, it } from "vitest";

import {
  ICE_DISCONNECT_RESTART_MS,
  ICE_RESTART_MAX_ATTEMPTS,
  decideIceRestart,
} from "../web/ice_restart";

describe("bounded ICE restart", () => {
  it("ignores first-connect failures so the existing ice_failed card stays", () => {
    expect(
      decideIceRestart({
        iceState: "failed",
        connectedOnce: false,
        restartAttempts: 0,
        ended: false,
      }),
    ).toEqual({ action: "ignore" });
  });

  it("restarts after a mid-call failed path", () => {
    expect(
      decideIceRestart({
        iceState: "failed",
        connectedOnce: true,
        restartAttempts: 0,
        ended: false,
      }),
    ).toEqual({ action: "restart" });
  });

  it("waits before restarting a transient disconnect", () => {
    expect(
      decideIceRestart({
        iceState: "disconnected",
        connectedOnce: true,
        restartAttempts: 0,
        ended: false,
      }),
    ).toEqual({ action: "wait", delayMs: ICE_DISCONNECT_RESTART_MS });
  });

  it("gives up after the bounded restart count", () => {
    expect(
      decideIceRestart({
        iceState: "failed",
        connectedOnce: true,
        restartAttempts: ICE_RESTART_MAX_ATTEMPTS,
        ended: false,
      }),
    ).toEqual({ action: "give_up" });
  });

  it("ignores reconnect work after the call has ended", () => {
    expect(
      decideIceRestart({
        iceState: "failed",
        connectedOnce: true,
        restartAttempts: 0,
        ended: true,
      }),
    ).toEqual({ action: "ignore" });
  });
});
