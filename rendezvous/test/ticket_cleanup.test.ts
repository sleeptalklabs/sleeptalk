import { describe, expect, it } from "vitest";

import { createTicketRecord, redeemTicket } from "../src/ticket";
import {
  purgeTicketStorage,
  type TicketCleanupTransaction,
} from "../src/ticket_cleanup";

const NOW = Date.UTC(2026, 7, 16, 12, 0, 0);
const TOMBSTONE_TTL_MS = 60 * 60 * 1000;

class MemoryTransaction implements TicketCleanupTransaction {
  alarm: number | null = null;

  constructor(readonly entries: Map<string, unknown>) {}

  async get<T>(key: string): Promise<T | undefined> {
    return this.entries.get(key) as T | undefined;
  }

  async list(): Promise<Map<string, unknown>> {
    return new Map(this.entries);
  }

  async delete(keys: string[]): Promise<number> {
    return keys.reduce((count, key) => count + Number(this.entries.delete(key)), 0);
  }

  async put<T>(key: string, value: T): Promise<void> {
    this.entries.set(key, value);
  }

  async setAlarm(scheduledTime: number): Promise<void> {
    this.alarm = scheduledTime;
  }
}

function issued() {
  return createTicketRecord({
    now: NOW,
    ttlSeconds: 300,
    agentKey: "agent-secret",
    ticketHash: "ticket-hash",
    installationId: "installation-id-123456",
    reservationId: "reservation-id-1234567",
    initializeBy: NOW + 20_000,
  });
}

describe("Durable Object privacy cleanup", () => {
  it("deletes all finished-call state and retains only a one-field used tombstone", async () => {
    const redeemed = redeemTicket(issued(), NOW + 1_000, "browser-secret");
    if (!redeemed.ok) throw new Error("expected redemption");
    const transaction = new MemoryTransaction(
      new Map<string, unknown>([
        ["ticket", redeemed.record],
        ["legacy-sdp", "private session description"],
        ["legacy-ice", "private candidate"],
      ]),
    );

    const result = await purgeTicketStorage(transaction, {
      now: NOW + 10_000,
      tombstoneTtlMilliseconds: TOMBSTONE_TTL_MS,
    });

    expect(result?.summary).toEqual({
      event: "call_summary",
      kind: "call",
      durationMs: 9_000,
      transport: "unknown",
      reconnectCount: 0,
    });
    expect([...transaction.entries.entries()]).toEqual([["ticket", { state: "used" }]]);
    expect(Object.keys(transaction.entries.get("ticket") as object)).toEqual(["state"]);
    expect(transaction.alarm).toBe(NOW + 10_000 + TOMBSTONE_TTL_MS);
  });

  it("retains only an ended tombstone for an expired unused ticket", async () => {
    const transaction = new MemoryTransaction(new Map([["ticket", issued()]]));

    await purgeTicketStorage(transaction, {
      now: NOW + 300_000,
      tombstoneTtlMilliseconds: TOMBSTONE_TTL_MS,
    });

    expect([...transaction.entries.entries()]).toEqual([["ticket", { state: "ended" }]]);
  });
});
