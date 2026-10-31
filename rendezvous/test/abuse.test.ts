import { expect, it } from "vitest";
import { admitSignal, signalMessageBytes, MAX_SIGNAL_BYTES, MAX_SIGNAL_MESSAGES, SIGNAL_WINDOW_MS } from "../src/abuse";

it("counts UTF-8 bytes and rejects oversized strings without encoding them", () => {
  expect(signalMessageBytes("é".repeat(40_000))).toBe(80_000);
  expect(admitSignal(undefined, signalMessageBytes("é".repeat(40_000)), 0)).toBeNull();
  expect(signalMessageBytes("x".repeat(100_000))).toBe(65_537);
  expect(signalMessageBytes(new ArrayBuffer(123))).toBe(123);
});

it("permits normal signaling, bounds messages, and replenishes after a minute", () => {
  let state = admitSignal(undefined, 200, 1_000)!;
  for (let index = 1; index < MAX_SIGNAL_MESSAGES; index++) state = admitSignal(state, 200, 1_001)!;
  expect(admitSignal(state, 1, 1_002)).toBeNull();
  expect(admitSignal(state, 200, 1_000 + SIGNAL_WINDOW_MS)).toEqual({ startedAt: 61_000, messages: 1, bytes: 200 });
});

it("bounds bytes independently of message count and does not mutate saved state", () => {
  const state = { startedAt: 1_000, messages: 20, bytes: MAX_SIGNAL_BYTES - 100 };
  expect(admitSignal(state, 101, 1_001)).toBeNull();
  expect(admitSignal(state, 100, 1_001)?.bytes).toBe(MAX_SIGNAL_BYTES);
  expect(state.bytes).toBe(MAX_SIGNAL_BYTES - 100);
  expect(admitSignal(undefined, 64 * 1024 + 1, 1_000)).toBeNull();
});
