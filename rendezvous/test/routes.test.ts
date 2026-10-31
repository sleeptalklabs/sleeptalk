import { describe, expect, it } from "vitest";

import { parseCallRoute } from "../web/routes";

const TICKET = "t".repeat(43);
const ROUTING_ID = "r".repeat(24);

describe("live-link routes", () => {
  it("parses a production ticket", () => {
    expect(parseCallRoute(`#/call/${ROUTING_ID}/${TICKET}`)).toEqual({
      kind: "ticket",
      routingId: ROUTING_ID,
      token: TICKET,
    });
  });

  it("parses a development room", () => {
    expect(parseCallRoute("#/dev/codex-smoke")).toEqual({
      kind: "dev",
      roomId: "codex-smoke",
    });
  });

  it("rejects removed comparison and malformed ticket routes", () => {
    expect(parseCallRoute(`#/compare/${TICKET}/${TICKET}`)).toBeNull();
    expect(parseCallRoute("#/call/short")).toBeNull();
    expect(parseCallRoute(`#/call/${TICKET}`)).toBeNull();
  });
});

// -- r2-browser-state-machine: guard semantics (pure, protocol-level) ------

import {
  TERMINAL_SIGNAL_TYPES,
  applyGuardRelay,
  guardSignal,
  initialSignalGuardState,
  parseSignalEnvelope,
} from "../src/protocol";

function env(id: string, type: string, payload: Record<string, unknown> = {}) {
  return parseSignalEnvelope(JSON.stringify({ v: 1, id, type, payload }))!;
}

describe("signal guards (r2-browser-state-machine)", () => {
  it("user cancel is a terminal signal type", () => {
    expect(TERMINAL_SIGNAL_TYPES.has("call.cancel")).toBe(true);
    expect(TERMINAL_SIGNAL_TYPES.has("call.hangup")).toBe(true);
    expect(TERMINAL_SIGNAL_TYPES.has("call.interrupt")).toBe(false);
    expect(TERMINAL_SIGNAL_TYPES.has("webrtc.offer")).toBe(false);
  });

  it("duplicate terminal envelope ids are idempotent no-ops", () => {
    const guard = initialSignalGuardState();
    const cancel = env("cancel-1", "call.cancel", { reason: "user" });
    // first: relayed, guard records the id
    expect(guardSignal(cancel, guard)).toEqual({ action: "relay" });
    applyGuardRelay(cancel, guard);
    // duplicate with the same id: rejected as duplicate → worker no-ops
    const second = guardSignal(cancel, guard);
    expect(second.action).toBe("reject");
    if (second.action === "reject") {
      expect(second.error.payload.code).toBe("duplicate_terminal");
    }
    // a *different* hangup id still relays (distinct terminal event)
    const hangup2 = env("hangup-2", "call.hangup");
    expect(guardSignal(hangup2, guard)).toEqual({ action: "relay" });
  });

  it("answer before any offer is rejected as reordered", () => {
    const guard = initialSignalGuardState();
    const answer = env("ans-1", "webrtc.answer", { sdp: "v=0..." });
    const early = guardSignal(answer, guard);
    expect(early.action).toBe("reject");
    if (early.action === "reject") {
      expect(early.error.payload.code).toBe("answer_before_offer");
    }
    // offer flows, then the answer relays in order
    const offer = env("off-1", "webrtc.offer", { sdp: "v=0..." });
    expect(guardSignal(offer, guard)).toEqual({ action: "relay" });
    applyGuardRelay(offer, guard);
    const retry = env("ans-2", "webrtc.answer", { sdp: "v=0..." });
    expect(guardSignal(retry, guard)).toEqual({ action: "relay" });
  });

  it("non-terminal, in-order messages relay untouched", () => {
    const guard = initialSignalGuardState();
    for (const type of ["webrtc.ice", "call.telemetry", "call.settings"]) {
      expect(guardSignal(env(`id-${type}`, type), guard)).toEqual({ action: "relay" });
    }
    // offers can repeat (renegotiation); only answers are order-guarded
    applyGuardRelay(env("off-1", "webrtc.offer"), guard);
    expect(guardSignal(env("off-2", "webrtc.offer"), guard)).toEqual({ action: "relay" });
  });
});
