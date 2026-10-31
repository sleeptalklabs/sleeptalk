import { SELF, env, evictDurableObject, reset, runDurableObjectAlarm, runInDurableObject } from "cloudflare:test";
import { afterEach, describe, expect, it } from "vitest";

import { sha256 } from "../src/access_protocol";

const ORIGIN = "https://call-staging.sleep.talk";

afterEach(async () => {
  await reset();
});

async function enroll(
  installationId: string,
  secret: string,
  requestId = `enroll-${installationId}`,
): Promise<Response> {
  return SELF.fetch(
    new Request(`${ORIGIN}/api/installations/enroll`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "CF-Connecting-IP": "2001:db8::42",
        "Idempotency-Key": requestId,
      },
      body: JSON.stringify({ installationId, secretHash: await sha256(secret) }),
    }),
  );
}

async function mint(
  installationId: string,
  secret: string,
  requestId: string,
): Promise<Response> {
  return SELF.fetch(
    new Request(`${ORIGIN}/api/tickets`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `SleeptalkGrant v1.${installationId}.${secret}`,
        "Idempotency-Key": requestId,
      },
      body: JSON.stringify({ ttlSeconds: 30 }),
    }),
  );
}

describe("durable hosted-access control plane", () => {
  it("replays enrollment only for the same installation and source", async () => {
    const requestId = "same-enrollment-request";
    expect((await enroll("a".repeat(24), "s".repeat(43), requestId)).status).toBe(201);
    expect((await enroll("a".repeat(24), "s".repeat(43), requestId)).status).toBe(201);

    const bypass = await enroll("b".repeat(24), "t".repeat(43), requestId);
    expect(bypass.status).toBe(429);
    expect(await bypass.json()).toMatchObject({
      error: { code: "enrollment_limited" },
    });
  });

  it("enrolls, survives eviction, and mints without caller metadata", async () => {
    const installationId = "i".repeat(24);
    const secret = "s".repeat(43);
    expect((await enroll(installationId, secret)).status).toBe(201);

    const stub = env.INSTALLATIONS.getByName(installationId);
    await evictDurableObject(stub);
    expect(await stub.inspect()).toMatchObject({ grant: "active", mintCountDay: 0 });

    const response = await mint(installationId, secret, "m".repeat(24));
    expect(response.status).toBe(201);
    const ticket = await response.json<Record<string, unknown>>();
    expect(ticket.joinUrl).toMatch(/^https:\/\/call-staging\.sleep\.talk\/#\/call\//);
    expect(JSON.stringify(ticket)).not.toContain("conversationId");
    expect(JSON.stringify(ticket)).not.toContain("platformUserId");
    expect(await stub.inspect()).toMatchObject({ grant: "active", mintCountDay: 1 });
  });

  it("serializes the last per-install concurrency slot", async () => {
    const installationId = "c".repeat(24);
    const secret = "q".repeat(43);
    expect((await enroll(installationId, secret)).status).toBe(201);

    const responses = await Promise.all(
      ["a", "b", "c"].map((letter) =>
        mint(installationId, secret, letter.repeat(24)),
      ),
    );
    expect(responses.map((response) => response.status).sort()).toEqual([201, 201, 429]);
    const limited = responses.find((response) => response.status === 429);
    expect(Number(limited?.headers.get("Retry-After"))).toBeGreaterThan(0);
    const inspection = await env.INSTALLATIONS.getByName(installationId).inspect();
    expect(inspection.reservations).toHaveLength(2);
  });

  it("atomically caps the global relay-attempt allowance", async () => {
    const control = env.SERVICE_CONTROL.getByName("allowance-race");
    // Staging: 400 global, 80 per source. Six sources of 80 each: five fit.
    const results = await Promise.all(
      ["a", "b", "c", "d", "e", "f"].map((letter) =>
        control.reserveRelay({
          permitId: letter.repeat(24),
          units: 80,
          source: `source-${letter}`,
          now: Date.UTC(2026, 8, 15, 12),
        }),
      ),
    );
    expect(results.filter((result) => result.ok)).toHaveLength(5);
    expect(results.filter((result) => !result.ok)).toEqual([
      expect.objectContaining({ code: "relay_allowance_exhausted" }),
    ]);
  });

  it("caps one source network's relay units below the global allowance", async () => {
    const control = env.SERVICE_CONTROL.getByName("allowance-source");
    const now = Date.UTC(2026, 8, 15, 12);
    const charge = (permit: string, source: string) =>
      control.reserveRelay({ permitId: permit.padEnd(24, "p"), units: 40, source, now });
    expect(await charge("one", "network-a")).toMatchObject({ ok: true });
    expect(await charge("two", "network-a")).toMatchObject({ ok: true });
    expect(await charge("three", "network-a")).toMatchObject({
      ok: false,
      code: "relay_allowance_exhausted",
    });
    // Replaying a permit never charges twice.
    expect(await charge("two", "network-a")).toMatchObject({ ok: true });
    expect(await charge("four", "network-b")).toMatchObject({ ok: true });
  });

  it("starts a fresh relay-attempt allowance on the next UTC day", async () => {
    const control = env.SERVICE_CONTROL.getByName("allowance-rollover");
    const firstDay = Date.UTC(2026, 8, 15, 12);
    expect(
      await control.reserveRelay({
        permitId: "first-day-permit",
        units: 80,
        source: "rollover",
        now: firstDay,
      }),
    ).toMatchObject({ ok: true });
    expect(
      await control.reserveRelay({
        permitId: "second-day-permit",
        units: 80,
        source: "rollover",
        now: firstDay + 24 * 60 * 60 * 1000,
      }),
    ).toMatchObject({ ok: true });
  });

  it("revocation blocks later minting", async () => {
    const installationId = "r".repeat(24);
    const secret = "x".repeat(43);
    expect((await enroll(installationId, secret)).status).toBe(201);
    await env.INSTALLATIONS.getByName(installationId).revoke({
      reason: "integration test",
      now: Date.now(),
    });

    const response = await mint(installationId, secret, "z".repeat(24));
    expect(response.status).toBe(403);
    expect(await response.json()).toMatchObject({
      error: { code: "grant_revoked" },
    });
  });

  it("requires admin auth and uses revisions to halt and resume admission", async () => {
    const unauthorized = await SELF.fetch(`${ORIGIN}/api/admin/status`);
    expect(unauthorized.status).toBe(401);

    const installationId = "h".repeat(24);
    const secret = "s".repeat(43);
    expect((await enroll(installationId, secret)).status).toBe(201);

    const headers = {
      Authorization: "Bearer integration-admin-secret",
      "Content-Type": "application/json",
    };
    const status = await SELF.fetch(
      new Request(`${ORIGIN}/api/admin/status`, { headers }),
    );
    expect(await status.json()).toMatchObject({ mode: "running", revision: 1 });

    const halted = await SELF.fetch(
      new Request(`${ORIGIN}/api/admin/control`, {
        method: "POST",
        headers,
        body: JSON.stringify({
          action: "halt",
          expectedRevision: 1,
          reason: "integration test",
        }),
      }),
    );
    expect(await halted.json()).toMatchObject({ mode: "halted", revision: 2 });
    expect((await enroll("n".repeat(24), "t".repeat(43))).status).toBe(503);
    const blockedMint = await mint(installationId, secret, "halted-mint-request");
    expect(blockedMint.status).toBe(503);
    expect(await blockedMint.json()).toMatchObject({
      error: { code: "service_disabled" },
    });
    // A wrong secret is refused by the installation itself, before the
    // global control object is consulted (it would say service_disabled).
    const junkMint = await mint(installationId, "x".repeat(43), "junk-mint-request");
    expect(await junkMint.json()).toMatchObject({ error: { code: "grant_invalid" } });

    const staleResume = await SELF.fetch(
      new Request(`${ORIGIN}/api/admin/control`, {
        method: "POST",
        headers,
        body: JSON.stringify({
          action: "resume",
          expectedRevision: 1,
          reason: "stale test",
        }),
      }),
    );
    expect(staleResume.status).toBe(409);

    const resumed = await SELF.fetch(
      new Request(`${ORIGIN}/api/admin/control`, {
        method: "POST",
        headers,
        body: JSON.stringify({
          action: "resume",
          expectedRevision: 2,
          reason: "integration test complete",
        }),
      }),
    );
    expect(await resumed.json()).toMatchObject({ mode: "running", revision: 3 });
  });

  it("rejects a room initialization after its reservation grace window", async () => {
    const room = env.TICKETS.getByName("late-initialization-room");
    const response = await room.fetch(
      new Request("https://ticket.internal/initialize", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          v: 2,
          state: "issued",
          installationId: "i".repeat(24),
          reservationId: "r".repeat(24),
          initializeBy: Date.now() - 1,
          ticketHash: "ticket-hash",
          agentKey: "agent-key",
          browserKey: null,
          createdAt: Date.now() - 1_000,
          expiresAt: Date.now() + 30_000,
          redeemedAt: null,
          lastActivityAt: null,
          sessionExpiresAt: null,
          endedAt: null,
          endReason: null,
          usesRelay: null,
          connectionCounts: { agent: 0, browser: 0 },
        }),
      }),
    );

    expect(response.status).toBe(410);
    expect(await response.json()).toMatchObject({ error: { code: "ticket_expired" } });
  });
});

async function recoveryTicket() {
  const installationId = "recovery".padEnd(24, "a");
  const secret = "recovery".padEnd(43, "s");
  expect((await enroll(installationId, secret)).status).toBe(201);
  const response = await mint(installationId, secret, "recover-start".padEnd(24, "m"));
  expect(response.status).toBe(201);
  return response.json<{
    ticket: string;
    routingId: string;
    agentSignalUrl: string;
    agentAccessKey: string;
    signalTypes: string[];
  }>();
}

async function agentSocket(ticket: Awaited<ReturnType<typeof recoveryTicket>>) {
  const response = await SELF.fetch(new Request(ticket.agentSignalUrl.replace("wss:", "https:"), {
    headers: { Upgrade: "websocket" },
  }));
  const socket = response.webSocket;
  if (!socket) throw new Error("Missing agent socket");
  socket.accept();
  return socket;
}

function signalMessage(socket: WebSocket): Promise<unknown> {
  return new Promise((resolve) => {
    socket.addEventListener("message", (event) => resolve(JSON.parse(String(event.data))), { once: true });
  });
}

function authenticateAgent(socket: WebSocket, key: string) {
  socket.send(JSON.stringify({
    v: 1, id: crypto.randomUUID(), type: "call.auth", payload: { role: "agent", key },
  }));
}

async function redeemRecoveryTicket(ticket: Awaited<ReturnType<typeof recoveryTicket>>) {
  const response = await SELF.fetch(new Request(`${ORIGIN}/api/tickets/${ticket.routingId}/redeem`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ticket: ticket.ticket }),
  }));
  expect(response.status).toBe(200);
}

describe("call link recovery", () => {
  it("notifies the authenticated host on redemption before a browser connects", async () => {
    const ticket = await recoveryTicket();
    expect(ticket.signalTypes).toContain("ticket.redeemed");
    const socket = await agentSocket(ticket);
    try {
      authenticateAgent(socket, ticket.agentAccessKey);
      const pong = signalMessage(socket);
      socket.send(JSON.stringify({ v: 1, id: crypto.randomUUID(), type: "ping", payload: {} }));
      expect(await pong).toMatchObject({ type: "pong" });
      const redeemed = signalMessage(socket);
      await redeemRecoveryTicket(ticket);
      expect(await redeemed).toMatchObject({ type: "ticket.redeemed", payload: {} });
    } finally {
      socket.close(1000);
    }
  });

  it("replays redemption to a host that authenticates after the browser redeemed", async () => {
    const ticket = await recoveryTicket();
    await redeemRecoveryTicket(ticket);
    const socket = await agentSocket(ticket);
    try {
      const redeemed = signalMessage(socket);
      authenticateAgent(socket, ticket.agentAccessKey);
      expect(await redeemed).toMatchObject({ type: "ticket.redeemed", payload: {} });
    } finally {
      socket.close(1000);
    }
  });

  it("never sends redemption to an unauthenticated connection", async () => {
    const ticket = await recoveryTicket();
    await redeemRecoveryTicket(ticket);
    const socket = await agentSocket(ticket);
    const messages: unknown[] = [];
    socket.addEventListener("message", (event) => messages.push(event.data));
    const closed = new Promise<number>((resolve) => {
      socket.addEventListener("close", (event) => resolve(event.code), { once: true });
    });
    authenticateAgent(socket, "incorrect-key");
    expect(await closed).toBe(4003);
    expect(messages).toEqual([]);
  });
});

async function enrollFrom(ip: string, installationId: string): Promise<Response> {
  return SELF.fetch(
    new Request(`${ORIGIN}/api/installations/enroll`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "CF-Connecting-IP": ip,
        "Idempotency-Key": `enroll-${installationId}`,
      },
      body: JSON.stringify({ installationId, secretHash: await sha256("s".repeat(43)) }),
    }),
  );
}

async function mintWith(
  installationId: string,
  secret: string,
  requestId: string,
  body: Record<string, unknown>,
): Promise<Response> {
  return SELF.fetch(
    new Request(`${ORIGIN}/api/tickets`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `SleeptalkGrant v1.${installationId}.${secret}`,
        "Idempotency-Key": requestId,
      },
      body: JSON.stringify(body),
    }),
  );
}

async function redeem(routingId: string, ticket: string): Promise<Response> {
  return SELF.fetch(new Request(`${ORIGIN}/api/tickets/${routingId}/redeem`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ticket }),
  }));
}

async function relayUsed(): Promise<number> {
  const status = await env.SERVICE_CONTROL.getByName("global").status();
  if (!status.ok) throw new Error("status failed");
  return status.value.relayAttemptsUsed;
}

describe("relay spend controls", () => {
  it("groups enrollment limits by IPv4 /24", async () => {
    // Staging allows 3 enrollments per source network per day.
    for (const [index, ip] of ["203.0.113.5", "203.0.113.6", "203.0.113.7"].entries()) {
      expect((await enrollFrom(ip, `slash24-${index}`.padEnd(24, "x"))).status).toBe(201);
    }
    const sameNetwork = await enrollFrom("203.0.113.200", "slash24-3".padEnd(24, "x"));
    expect(sameNetwork.status).toBe(429);
    expect(await sameNetwork.json()).toMatchObject({ error: { code: "enrollment_limited" } });
    expect((await enrollFrom("203.0.114.1", "slash24-4".padEnd(24, "x"))).status).toBe(201);
  });

  it("mints without TURN credentials and charges nothing until a browser redeems", async () => {
    const installationId = "charge".padEnd(24, "i");
    const secret = "s".repeat(43);
    expect((await enroll(installationId, secret)).status).toBe(201);
    const before = await relayUsed();

    const minted = await mintWith(installationId, secret, "charge-mint".padEnd(24, "m"), {
      ttlSeconds: 60,
    });
    expect(minted.status).toBe(201);
    const ticket = await minted.json<{
      ticket: string;
      routingId: string;
      agentSignalUrl: string;
      agentAccessKey: string;
      iceServers: Array<Record<string, unknown>>;
    }>();
    for (const server of ticket.iceServers) {
      expect(server).not.toHaveProperty("credential");
      expect(server).not.toHaveProperty("username");
      expect((server.urls as string[]).every((url) => url.startsWith("stun:"))).toBe(true);
    }
    const stub = env.INSTALLATIONS.getByName(installationId);
    expect(await stub.inspect()).toMatchObject({ relayUnitsDay: 0 });
    expect(await relayUsed()).toBe(before);

    const socket = await agentSocket(ticket as never);
    try {
      const redeemed = signalMessage(socket);
      authenticateAgent(socket, ticket.agentAccessKey);
      expect((await redeem(ticket.routingId, ticket.ticket)).status).toBe(200);
      // The host's ICE servers arrive with the redemption, on its own socket.
      expect(await redeemed).toMatchObject({
        type: "ticket.redeemed",
        payload: { iceServers: expect.any(Array) },
      });
    } finally {
      socket.close(1000);
    }
    expect(await stub.inspect()).toMatchObject({ relayUnitsDay: 4 });
    expect(await relayUsed()).toBe(before + 4);
  });

  it("replays the host's ICE servers to a host that connects after redemption", async () => {
    const installationId = "replay".padEnd(24, "i");
    const secret = "r".repeat(43);
    expect((await enroll(installationId, secret)).status).toBe(201);
    const minted = await mintWith(installationId, secret, "replay-mint".padEnd(24, "m"), {
      ttlSeconds: 60,
    });
    const ticket = await minted.json<{
      ticket: string; routingId: string; agentSignalUrl: string; agentAccessKey: string;
    }>();
    expect((await redeem(ticket.routingId, ticket.ticket)).status).toBe(200);
    const socket = await agentSocket(ticket as never);
    try {
      const redeemed = signalMessage(socket);
      authenticateAgent(socket, ticket.agentAccessKey);
      // Tests run STUN-only; what matters is that the stored host list is replayed.
      expect(await redeemed).toMatchObject({
        type: "ticket.redeemed",
        payload: { iceServers: expect.any(Array) },
      });
    } finally {
      socket.close(1000);
    }
  });

  it("raises very short link lifetimes to the one-minute floor", async () => {
    const installationId = "short-ttl".padEnd(24, "i");
    const secret = "s".repeat(43);
    expect((await enroll(installationId, secret)).status).toBe(201);
    const started = Date.now();
    const minted = await mintWith(installationId, secret, "short-ttl-mint".padEnd(24, "m"), {
      ttlSeconds: 1,
    });
    expect(minted.status).toBe(201);
    const { expiresAt } = await minted.json<{ expiresAt: string }>();
    expect(Date.parse(expiresAt) - started).toBeGreaterThanOrEqual(59_000);
  });

  it("stops an installation at its own daily relay budget", async () => {
    const installationId = "budget".padEnd(24, "i");
    const secret = "s".repeat(43);
    expect((await enroll(installationId, secret)).status).toBe(201);
    const stub = env.INSTALLATIONS.getByName(installationId);
    // Staging budget is 40 units a day; pretend 40 were already used today.
    await runInDurableObject(stub, async (_instance, state) => {
      const current = await state.storage.get<Record<string, unknown>>("state");
      await state.storage.put("state", {
        ...current,
        relayUsage: { period: new Date().toISOString().slice(0, 10), units: 40 },
      });
    });
    const minted = await mintWith(installationId, secret, "budget-mint".padEnd(24, "m"), {});
    expect(minted.status).toBe(201);
    const ticket = await minted.json<{ ticket: string; routingId: string }>();
    const refused = await redeem(ticket.routingId, ticket.ticket);
    expect(refused.status).toBe(503);
    expect(await refused.json()).toMatchObject({ error: { code: "relay_allowance_exhausted" } });
  });

  it("counts request bytes instead of trusting Content-Length", async () => {
    const chunk = new TextEncoder().encode(`{"installationId":"${"x".repeat(4096)}"}`);
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(chunk);
        controller.close();
      },
    });
    const response = await SELF.fetch(
      new Request(`${ORIGIN}/api/installations/enroll`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "CF-Connecting-IP": "198.51.100.9",
          "Idempotency-Key": "oversized-enrollment-request",
        },
        body,
        // @ts-expect-error duplex is required for streamed request bodies
        duplex: "half",
      }),
    );
    expect(response.status).toBe(413);
  });
});

describe("service control storage", () => {
  it("keeps each day's records as small keys and drops old days", async () => {
    const control = env.SERVICE_CONTROL.getByName("cleanup");
    const now = Date.now();
    expect(
      await control.reserveRelay({ permitId: "today".padEnd(24, "p"), units: 4, source: "s", now }),
    ).toMatchObject({ ok: true });
    await runInDurableObject(control, async (_instance, state) => {
      await state.storage.put("d:2020-01-01:relay", 99);
      await state.storage.put("state", {
        v: 1, mode: "running", revision: 1, reason: "legacy",
        enrollments: [{ requestId: "old" }], permits: [{ permitId: "old" }],
      });
    });
    expect(await runDurableObjectAlarm(control)).toBe(true);
    await runInDurableObject(control, async (_instance, state) => {
      const keys = [...(await state.storage.list({ prefix: "d:" })).keys()];
      expect(keys).not.toContain("d:2020-01-01:relay");
      expect(keys.some((key) => key.endsWith(":relay"))).toBe(true);
      expect(await state.storage.get("state")).toEqual({
        v: 1, mode: "running", revision: 1, reason: "legacy",
      });
    });
  });
});
