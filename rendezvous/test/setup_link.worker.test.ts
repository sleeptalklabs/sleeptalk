import { SELF, env, reset, runInDurableObject } from "cloudflare:test";
import { afterEach, describe, expect, it } from "vitest";

import { sha256 } from "../src/access_protocol";

const ORIGIN = "https://call-staging.sleep.talk";

afterEach(async () => {
  await reset();
});

interface MintedSetup {
  kind: string;
  ticket: string;
  routingId: string;
  joinUrl: string;
  agentSignalUrl: string;
  agentAccessKey: string;
  signalTypes: string[];
  iceServers?: unknown;
}

async function enroll(installationId: string, secret: string): Promise<void> {
  const response = await SELF.fetch(
    new Request(`${ORIGIN}/api/installations/enroll`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "CF-Connecting-IP": "2001:db8::42",
        "Idempotency-Key": `enroll-${installationId}`,
      },
      body: JSON.stringify({ installationId, secretHash: await sha256(secret) }),
    }),
  );
  expect(response.status).toBe(201);
}

function mint(
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

async function setupTicket(seed = "setup"): Promise<MintedSetup> {
  const installationId = seed.padEnd(24, "i");
  const secret = seed.padEnd(43, "s");
  await enroll(installationId, secret);
  const response = await mint(installationId, secret, `${seed}-mint`.padEnd(24, "m"), {
    kind: "setup",
    ttlSeconds: 600,
  });
  expect(response.status).toBe(201);
  return response.json<MintedSetup>();
}

async function redeem(ticket: MintedSetup): Promise<Response> {
  return SELF.fetch(
    new Request(`${ORIGIN}/api/tickets/${ticket.routingId}/redeem`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ticket: ticket.ticket }),
    }),
  );
}

async function socketFor(url: string): Promise<WebSocket> {
  const response = await SELF.fetch(
    new Request(url.replace("wss:", "https:"), { headers: { Upgrade: "websocket" } }),
  );
  const socket = response.webSocket;
  if (!socket) throw new Error("Missing socket");
  socket.accept();
  return socket;
}

function inbox(socket: WebSocket) {
  const messages: Array<{ type: string; payload: Record<string, unknown> }> = [];
  const waiters: Array<() => void> = [];
  socket.addEventListener("message", (event) => {
    messages.push(JSON.parse(String(event.data)));
    waiters.splice(0).forEach((wake) => wake());
  });
  return {
    messages,
    async next(type: string) {
      for (;;) {
        const index = messages.findIndex((message) => message.type === type);
        if (index >= 0) return messages.splice(index, 1)[0];
        await new Promise<void>((resolve) => waiters.push(resolve));
      }
    },
  };
}

function send(socket: WebSocket, type: string, payload: Record<string, unknown> = {}) {
  socket.send(JSON.stringify({ v: 1, id: crypto.randomUUID(), type, payload }));
}

async function connectedRoom(seed?: string) {
  const ticket = await setupTicket(seed);
  const agent = await socketFor(ticket.agentSignalUrl);
  const agentInbox = inbox(agent);
  send(agent, "call.auth", { role: "agent", key: ticket.agentAccessKey });
  const redeemed = await redeem(ticket);
  expect(redeemed.status).toBe(200);
  const session = await redeemed.json<Record<string, unknown>>();
  await agentInbox.next("ticket.redeemed");
  const browser = await socketFor(String(session.browserSignalUrl));
  const browserInbox = inbox(browser);
  send(browser, "call.auth", { role: "browser", key: String(session.browserAccessKey) });
  await agentInbox.next("peer.ready");
  await browserInbox.next("peer.ready");
  return { ticket, session, agent, agentInbox, browser, browserInbox };
}

describe("setup links", () => {
  it("closes a flooding authenticated peer and ends its room", async () => {
    const room = await connectedRoom("flood");
    const closed = new Promise<number>((resolve) =>
      room.browser.addEventListener("close", (event) => resolve(event.code), { once: true }),
    );
    // Unknown envelopes also count, even though they would otherwise be ignored.
    for (let index = 0; index < 241; index++) send(room.browser, "unknown.message");
    expect(await closed).toBe(4008);
    expect(await room.agentInbox.next("call.ended")).toMatchObject({ payload: { reason: "connection_lost" } });
    room.agent.close(1000);
  });

  it("does not let an unauthenticated socket error end an active room", async () => {
    const room = await connectedRoom("stranger-error");
    const stranger = await socketFor(room.ticket.agentSignalUrl);
    const stub = env.TICKETS.getByName(room.ticket.routingId);
    await runInDurableObject(stub, async (instance, state) => {
      const pending = state.getWebSockets().find((socket) => !socket.deserializeAttachment()?.authenticated)!;
      await instance.webSocketError(pending);
    });
    send(room.browser, "setup.sealed", { seq: 1, iv: "i", ct: "still-connected" });
    expect(await room.agentInbox.next("setup.sealed")).toMatchObject({ payload: { ct: "still-connected" } });
    stranger.close(1000);
    room.agent.close(1000);
    room.browser.close(1000);
  });

  it("mints a media-free setup link with its own route and lifetime", async () => {
    const ticket = await setupTicket();
    expect(ticket.kind).toBe("setup");
    expect(ticket.joinUrl).toMatch(/^https:\/\/call-staging\.sleep\.talk\/#\/setup\/[A-Za-z0-9_-]+\/[A-Za-z0-9_-]+$/);
    expect(ticket.iceServers).toBeUndefined();
    expect(ticket.signalTypes).toContain("setup.sealed");
    expect(ticket.signalTypes).not.toContain("setup.submit");
    expect(ticket.signalTypes).not.toContain("webrtc.offer");

    const response = await redeem(ticket);
    const session = await response.json<Record<string, unknown>>();
    expect(session).toMatchObject({ kind: "setup", sessionCapSeconds: 900, inactivitySeconds: 300 });
    expect(session.iceServers).toBeUndefined();
    expect((await redeem(ticket)).status).toBe(409);
  });

  it("keeps call links unchanged and refuses setup lifetimes for calls", async () => {
    const installationId = "calls".padEnd(24, "i");
    const secret = "calls".padEnd(43, "s");
    await enroll(installationId, secret);
    const call = await mint(installationId, secret, "call-mint".padEnd(24, "m"), { ttlSeconds: 30 });
    const body = await call.json<Record<string, unknown>>();
    expect(body.joinUrl).toMatch(/#\/call\//);
    expect(body.kind).toBe("call");
    expect(body.signalTypes).not.toContain("setup.sealed");
    const long = await mint(installationId, secret, "call-long".padEnd(24, "m"), { ttlSeconds: 600 });
    expect(long.status).toBe(400);
    const bad = await mint(installationId, secret, "bad-kind".padEnd(24, "m"), { kind: "admin" });
    expect(bad.status).toBe(400);
    expect(await bad.json()).toMatchObject({ error: { code: "invalid_kind" } });
  });

  it("relays setup messages only in their allowed direction", async () => {
    const room = await connectedRoom("relay");
    try {
      // The phone opens the sealed channel; only it may.
      send(room.agent, "setup.hello", { enc: "e", ct: "c" });
      send(room.browser, "setup.hello", { enc: "e", ct: "c" });
      expect(await room.agentInbox.next("setup.hello")).toMatchObject({ payload: { enc: "e", ct: "c" } });
      expect(room.browserInbox.messages.map((message) => message.type)).not.toContain("setup.hello");

      // Sealed messages pass both ways, unread.
      send(room.agent, "setup.sealed", { seq: 1, iv: "i", ct: "host" });
      expect(await room.browserInbox.next("setup.sealed")).toMatchObject({ payload: { seq: 1, ct: "host" } });
      send(room.browser, "setup.sealed", { seq: 1, iv: "i", ct: "page" });
      expect(await room.agentInbox.next("setup.sealed")).toMatchObject({ payload: { seq: 1, ct: "page" } });

      // Plaintext setup messages and media are dropped.
      send(room.agent, "setup.request", { fields: [{ id: "deepgram" }] });
      send(room.browser, "setup.submit", { field: "deepgram", enc: "e", ct: "c" });
      send(room.browser, "webrtc.offer", { sdp: "v=0" });
      send(room.browser, "setup.sealed", { seq: 2, iv: "i", ct: "marker" });
      await room.agentInbox.next("setup.sealed");
      expect(room.agentInbox.messages.map((message) => message.type)).not.toContain("setup.submit");
      expect(room.agentInbox.messages.map((message) => message.type)).not.toContain("webrtc.offer");
      expect(room.browserInbox.messages.map((message) => message.type)).not.toContain("setup.request");
    } finally {
      room.agent.close(1000);
      room.browser.close(1000);
    }
  });

  it("ends the room when the host reports setup done", async () => {
    const room = await connectedRoom("done");
    const closed = new Promise<number>((resolve) =>
      room.browser.addEventListener("close", (event) => resolve(event.code), { once: true }),
    );
    send(room.agent, "setup.done", { profile_ready: true });
    expect(await room.browserInbox.next("setup.done")).toMatchObject({ payload: { profile_ready: true } });
    expect(await room.browserInbox.next("call.ended")).toMatchObject({ payload: { reason: "setup_complete" } });
    expect(await closed).toBe(1000);
    const inspection = await env.INSTALLATIONS.getByName("done".padEnd(24, "i")).inspect();
    expect(inspection.reservations).toHaveLength(0);
    expect((await redeem(room.ticket)).status).toBe(409);
  });

  it("drops setup messages in a call room", async () => {
    const installationId = "callroom".padEnd(24, "i");
    const secret = "callroom".padEnd(43, "s");
    await enroll(installationId, secret);
    const response = await mint(installationId, secret, "callroom-mint".padEnd(24, "m"), { ttlSeconds: 60 });
    const ticket = await response.json<MintedSetup>();
    const agent = await socketFor(ticket.agentSignalUrl);
    const agentInbox = inbox(agent);
    send(agent, "call.auth", { role: "agent", key: ticket.agentAccessKey });
    const redeemed = await redeem(ticket);
    const session = await redeemed.json<Record<string, unknown>>();
    const browser = await socketFor(String(session.browserSignalUrl));
    const browserInbox = inbox(browser);
    send(browser, "call.auth", { role: "browser", key: String(session.browserAccessKey) });
    await agentInbox.next("peer.ready");
    await browserInbox.next("peer.ready");
    send(browser, "setup.sealed", { seq: 1, iv: "i", ct: "c" });
    send(browser, "call.settings", { speed: 1 });
    await agentInbox.next("call.settings");
    expect(agentInbox.messages.map((message) => message.type)).not.toContain("setup.sealed");
    agent.close(1000);
    browser.close(1000);
  });

  it("limits setup links separately from calls", async () => {
    const installationId = "limits".padEnd(24, "i");
    const secret = "limits".padEnd(43, "s");
    await enroll(installationId, secret);
    const results: number[] = [];
    for (let index = 0; index < 3; index += 1) {
      const response = await mint(installationId, secret, `limit-${index}`.padEnd(24, "m"), { kind: "setup" });
      results.push(response.status);
      if (response.status === 429) {
        expect(await response.json()).toMatchObject({ error: { code: "concurrency_limited" } });
      }
    }
    expect(results).toEqual([201, 201, 429]);
    const call = await mint(installationId, secret, "limit-call".padEnd(24, "m"), { ttlSeconds: 30 });
    expect(call.status).toBe(201);
    const inspection = await env.INSTALLATIONS.getByName(installationId).inspect();
    expect(inspection).toMatchObject({ mintCountDay: 1, setupMintCountDay: 2 });
  });
});
