import { describe, expect, it } from "vitest";

import { parsePeerRole, parseSignal, parseSignalEnvelope, relayedInRoom, signalTypesForKind } from "../src/protocol";

describe("development signaling protocol", () => {
  it("accepts the two peer roles only", () => {
    expect(parsePeerRole("agent")).toBe("agent");
    expect(parsePeerRole("browser")).toBe("browser");
    expect(parsePeerRole("viewer")).toBeNull();
  });

  it("accepts a versioned WebRTC offer", () => {
    expect(
      parseSignalEnvelope(
        JSON.stringify({
          v: 1,
          id: "message-1",
          type: "webrtc.offer",
          payload: { type: "offer", sdp: "v=0" },
        }),
      ),
    ).toMatchObject({ type: "webrtc.offer" });
  });

  it("accepts client heartbeats", () => {
    expect(
      parseSignalEnvelope(
        JSON.stringify({ v: 1, id: "heartbeat-1", type: "ping", payload: {} }),
      ),
    ).toMatchObject({ type: "ping" });
  });

  it("accepts a first-message signaling credential", () => {
    expect(
      parseSignalEnvelope(
        JSON.stringify({
          v: 1,
          id: "auth-1",
          type: "call.auth",
          payload: { role: "browser", key: "secret" },
        }),
      ),
    ).toMatchObject({ type: "call.auth" });
  });

  it("accepts bounded call telemetry for peer-to-peer forwarding", () => {
    expect(
      parseSignalEnvelope(
        JSON.stringify({
          v: 1,
          id: "telemetry-1",
          type: "call.telemetry",
          payload: { stage: "transport_stats", currentRttMs: 84 },
        }),
      ),
    ).toMatchObject({ type: "call.telemetry" });
  });

  it("relays opaque PIN submissions and runtime results", () => {
    for (const type of ["call.pin_submit", "call.pin_result"]) {
      expect(
        parseSignalEnvelope(
          JSON.stringify({ v: 1, id: `pin-${type}`, type, payload: {} }),
        ),
      ).toBeNull();
    }
  });

  it("relays a turn interrupt without treating it as hangup", () => {
    expect(
      parseSignalEnvelope(
        JSON.stringify({
          v: 1,
          id: "interrupt-1",
          type: "call.interrupt",
          payload: {},
        }),
      ),
    ).toMatchObject({ type: "call.interrupt" });
  });

  it("relays live voice settings to the host", () => {
    expect(
      parseSignalEnvelope(
        JSON.stringify({
          v: 1,
          id: "settings-1",
          type: "call.settings",
          payload: { narrationStyle: "silence", replyLength: "summarized" },
        }),
      ),
    ).toMatchObject({
      type: "call.settings",
      payload: { narrationStyle: "silence", replyLength: "summarized" },
    });
  });

  it("never relays transcripts or agent state; those ride the peer data channel", () => {
    for (const [type, payload] of [
      ["call.presentation", { presence: "speaking", lines: [{ text: "secret plan" }] }],
      ["call.agent_state", { state: "thinking", task: "Reading .env", detail: "" }],
    ] as const) {
      expect(parseSignalEnvelope(JSON.stringify({ v: 1, id: `${type}-1`, type, payload }))).toBeNull();
      expect(relayedInRoom("call", "agent", type as never)).toBe(false);
    }
    expect(signalTypesForKind("call")).not.toContain("call.presentation");
    expect(signalTypesForKind("call")).not.toContain("call.agent_state");
  });

  it("rejects unknown, malformed, and oversized messages", () => {
    expect(parseSignalEnvelope("not json")).toBeNull();
    expect(
      parseSignalEnvelope(
        JSON.stringify({ v: 2, id: "message-1", type: "webrtc.offer", payload: {} }),
      ),
    ).toBeNull();
    expect(
      parseSignalEnvelope(
        JSON.stringify({ v: 1, id: "message-1", type: "admin.delete", payload: {} }),
      ),
    ).toBeNull();
    expect(parseSignalEnvelope("x".repeat(64 * 1024 + 1))).toBeNull();
  });

  it("classifies known, unknown, and invalid envelopes in one parse", () => {
    expect(
      parseSignal(
        JSON.stringify({
          v: 1,
          id: "future-1",
          type: "call.settings",
          payload: { ttsSpeed: 1 },
        }),
      ),
    ).toMatchObject({ kind: "known", envelope: { type: "call.settings" } });
    expect(
      parseSignal(
        JSON.stringify({ v: 1, id: "future-2", type: "call.future_type", payload: {} }),
      ),
    ).toEqual({ kind: "unknown" });
    expect(parseSignal("not json")).toEqual({ kind: "invalid" });
  });
});


it("rejects client attempts to forge server redemption notifications", () => {
  expect(parseSignal(JSON.stringify({
    v: 1, id: "forged-redemption", type: "ticket.redeemed", payload: {},
  }))).toEqual({ kind: "unknown" });
});

describe("setup room relay policy", () => {
  it("relays only the sealed channel: the phone opens it, both sides use it, the host ends it", () => {
    expect(relayedInRoom("setup", "browser", "setup.hello")).toBe(true);
    expect(relayedInRoom("setup", "agent", "setup.hello")).toBe(false);
    for (const role of ["agent", "browser"] as const) expect(relayedInRoom("setup", role, "setup.sealed")).toBe(true);
    expect(relayedInRoom("setup", "agent", "setup.done")).toBe(true);
    expect(relayedInRoom("setup", "browser", "setup.done")).toBe(false);
    for (const type of ["setup.hello", "setup.sealed"] as const) expect(relayedInRoom("call", "browser", type)).toBe(false);
  });

  it("drops the plaintext setup messages older builds sent", () => {
    for (const type of ["setup.request", "setup.submit", "setup.result", "setup.discord", "setup.discord_pick", "setup.finish"]) {
      expect(parseSignal(JSON.stringify({ v: 1, id: "old", type, payload: {} }))).toEqual({ kind: "unknown" });
    }
  });

  it("keeps media out of setup rooms and setup out of call rooms", () => {
    expect(relayedInRoom("setup", "agent", "webrtc.offer")).toBe(false);
    expect(relayedInRoom("setup", "browser", "call.settings")).toBe(false);
    expect(relayedInRoom("call", "browser", "setup.sealed")).toBe(false);
    expect(relayedInRoom("call", "agent", "webrtc.offer")).toBe(true);
    expect(relayedInRoom("setup", "browser", "call.cancel")).toBe(true);
  });
});
