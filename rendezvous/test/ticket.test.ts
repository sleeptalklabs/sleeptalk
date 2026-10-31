import { describe, expect, it } from "vitest";

import {
  INACTIVITY_SECONDS,
  SESSION_CAP_SECONDS,
  SETUP_INACTIVITY_SECONDS,
  SETUP_SESSION_CAP_SECONDS,
  connectTicket,
  createTicketRecord,
  endTicket,
  nextDeadline,
  observeRelayPath,
  reconnectCount,
  redeemTicket,
  timeoutReason,
  touchTicket,
} from "../src/ticket";

const NOW = Date.UTC(2026, 7, 15, 12, 0, 0);

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

describe("ticket lifecycle", () => {
  it("stores only opaque control references and expires within five minutes", () => {
    const ticket = issued();

    expect(ticket.state).toBe("issued");
    expect(ticket.installationId).toBe("installation-id-123456");
    expect(ticket.reservationId).toBe("reservation-id-1234567");
    expect(ticket.initializeBy).toBe(NOW + 20_000);
    expect(ticket.expiresAt).toBe(NOW + 300_000);
    expect(nextDeadline(ticket)).toBe(ticket.expiresAt);
  });

  it("redeems once and rejects a second redemption", () => {
    const first = redeemTicket(issued(), NOW + 1_000, "browser-secret");
    expect(first.ok).toBe(true);
    if (!first.ok) throw new Error("expected redemption");
    expect(first.record.state).toBe("redeemed");
    expect(first.record.browserKey).toBe("browser-secret");
    expect(first.record.sessionExpiresAt).toBe(NOW + 1_000 + SESSION_CAP_SECONDS * 1000);

    const second = redeemTicket(first.record, NOW + 2_000, "another-secret");
    expect(second).toMatchObject({ ok: false, code: "already_used" });
  });

  it("rejects an unused expired ticket", () => {
    const result = redeemTicket(issued(), NOW + 300_000, "browser-secret");

    expect(result).toMatchObject({
      ok: false,
      code: "expired",
      record: { state: "ended", endReason: "ticket_expired" },
    });
  });

  it("enforces inactivity and the hard session cap", () => {
    const redeemed = redeemTicket(issued(), NOW + 1_000, "browser-secret");
    if (!redeemed.ok) throw new Error("expected redemption");
    const active = touchTicket(redeemed.record, NOW + 30_000);

    expect(timeoutReason(active, NOW + 30_000 + INACTIVITY_SECONDS * 1000 - 1)).toBeNull();
    expect(timeoutReason(active, NOW + 30_000 + INACTIVITY_SECONDS * 1000)).toBe(
      "inactivity_timeout",
    );
    expect(timeoutReason(active, redeemed.record.sessionExpiresAt ?? 0)).toBe("session_cap");
  });

  it("does not mutate an ended ticket", () => {
    const ended = endTicket(issued(), NOW + 5_000, "agent_disconnected");

    expect(touchTicket(ended, NOW + 6_000)).toBe(ended);
    expect(endTicket(ended, NOW + 7_000, "connection_lost")).toBe(ended);
    expect(nextDeadline(ended)).toBeNull();
  });

  it("counts reconnects without retaining connection details", () => {
    let ticket = connectTicket(issued(), "agent");
    ticket = connectTicket(ticket, "browser");
    ticket = connectTicket(ticket, "browser");

    expect(ticket.connectionCounts).toEqual({ agent: 1, browser: 2 });
    expect(reconnectCount(ticket)).toBe(1);
  });

  it("retains only whether a relay was observed", () => {
    const direct = observeRelayPath(issued(), false);
    const relayed = observeRelayPath(direct, true);

    expect(direct.usesRelay).toBe(false);
    expect(relayed.usesRelay).toBe(true);
    expect(observeRelayPath(relayed, false)).toBe(relayed);
  });
});

// -- r2-browser-state-machine: cancel/recovery + expiry + duplicates ------

describe("user cancel and recovery events (r2-browser-state-machine)", () => {
  it("user_cancel is a distinct end reason and endTicket stays idempotent", () => {
    const redeemed = redeemTicket(issued(), NOW + 1_000, "browser-secret").record;
    const cancelled = endTicket(redeemed, NOW + 5_000, "user_cancel");
    expect(cancelled.state).toBe("ended");
    expect(cancelled.endReason).toBe("user_cancel");
    // duplicate end is a no-op — same record back
    expect(endTicket(cancelled, NOW + 6_000, "peer_hung_up")).toBe(cancelled);
  });

  it("expired tickets cannot redeem (reconnect rejected at the door)", () => {
    // issued past expiry → expired on redeem attempt
    const expired = redeemTicket(issued(), NOW + 300_001, "browser-secret");
    expect(expired.ok).toBe(false);
    if (!expired.ok) expect(expired.code).toBe("expired");
    expect(expired.record.endReason).toBe("ticket_expired");
    // ended record can never be redeemed again regardless of key
    const again = redeemTicket(expired.record, NOW + 400_000, "browser-secret");
    expect(again.ok).toBe(false);
    if (!again.ok) expect(again.code).toBe("expired");
  });

  it("timeoutReason surfaces expiry for issued tickets only once", () => {
    const record = issued();
    expect(timeoutReason(record, NOW + 299_000)).toBeNull();
    expect(timeoutReason(record, NOW + 300_000)).toBe("ticket_expired");
    const ended = endTicket(record, NOW + 300_000, "ticket_expired");
    expect(timeoutReason(ended, NOW + 400_000)).toBeNull();
  });
});

describe("setup ticket lifetimes", () => {
  const setup = () => ({ ...issued(), kind: "setup" as const });

  it("gives setup rooms a longer session and idle allowance than calls", () => {
    const redeemed = redeemTicket(setup(), NOW, "browser-key");
    if (!redeemed.ok) throw new Error("setup ticket should redeem");
    expect(redeemed.record.sessionExpiresAt).toBe(NOW + SETUP_SESSION_CAP_SECONDS * 1000);
    expect(timeoutReason(redeemed.record, NOW + INACTIVITY_SECONDS * 1000)).toBeNull();
    expect(timeoutReason(redeemed.record, NOW + SETUP_INACTIVITY_SECONDS * 1000)).toBe("inactivity_timeout");
    expect(nextDeadline(redeemed.record)).toBe(NOW + SETUP_INACTIVITY_SECONDS * 1000);
  });

  it("treats records without a kind as calls", () => {
    const legacy = { ...issued() } as Record<string, unknown>;
    delete legacy.kind;
    const redeemed = redeemTicket(legacy as unknown as ReturnType<typeof issued>, NOW, "browser-key");
    if (!redeemed.ok) throw new Error("call ticket should redeem");
    expect(redeemed.record.sessionExpiresAt).toBe(NOW + SESSION_CAP_SECONDS * 1000);
  });
});
