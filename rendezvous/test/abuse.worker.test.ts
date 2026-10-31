import { SELF, env, reset } from "cloudflare:test";
import { afterEach, expect, it, vi } from "vitest";
import worker from "../src/worker";

afterEach(async () => { await reset(); });
type WorkerEnv = Parameters<typeof worker.fetch>[1];
const allow = { limit: async () => ({ success: true }) };
const deny = { limit: async () => ({ success: false }) };
function bindings(overrides: Partial<WorkerEnv> = {}): WorkerEnv {
  return {
    ...env,
    REQUEST_RATE_LIMIT: allow, GLOBAL_REQUEST_RATE_LIMIT: allow,
    SIGNAL_RATE_LIMIT: allow, ADMIN_RATE_LIMIT: allow,
    ENROLL_RATE_LIMIT: allow, TICKET_RATE_LIMIT: allow, REDEEM_RATE_LIMIT: allow,
    ...overrides,
  } as WorkerEnv;
}

it("blocks abusive requests before any Durable Object routing", async () => {
  const lookup = vi.fn(() => { throw new Error("Must not touch Durable Objects"); });
  const target = bindings({ REQUEST_RATE_LIMIT: deny });
  Object.defineProperty(target, "TICKETS", { value: { idFromName: lookup } });
  const response = await worker.fetch(new Request("https://call.sleep.talk/api/tickets/" + "x".repeat(24) + "/redeem", { method: "POST" }), target);
  expect(response.status).toBe(429);
  expect(response.headers.get("Retry-After")).toBe("60");
  expect(lookup).not.toHaveBeenCalled();
});

it("requires a real upgrade before routing a signaling request", async () => {
  const response = await worker.fetch(new Request("https://call.sleep.talk/api/tickets/" + "x".repeat(24) + "/signal"), bindings());
  expect(response.status).toBe(426);
});

it("limits socket upgrades before reaching a room", async () => {
  const response = await worker.fetch(new Request("https://call.sleep.talk/api/tickets/" + "x".repeat(24) + "/signal", { headers: { Upgrade: "websocket" } }), bindings({ SIGNAL_RATE_LIMIT: deny }));
  expect(response.status).toBe(429);
});

it("fails closed if a production limiter binding is missing", async () => {
  const response = await worker.fetch(new Request("https://call.sleep.talk/api/tickets"), bindings({ DEPLOYMENT_ENV: "production", SIGNAL_RATE_LIMIT: undefined }));
  expect(response.status).toBe(503);
  expect(await response.json()).toMatchObject({ error: { code: "service_misconfigured" } });
});

it("keeps authenticated admin recovery available when public traffic is throttled or disabled", async () => {
  const target = bindings({ REQUEST_RATE_LIMIT: deny, GLOBAL_REQUEST_RATE_LIMIT: deny, EMERGENCY_DISABLE: "true" });
  const response = await worker.fetch(new Request("https://call-staging.sleep.talk/api/admin/status", { headers: { Authorization: "Bearer integration-admin-secret" } }), target);
  expect(response.status).toBe(200);
  const denied = await worker.fetch(new Request("https://call-staging.sleep.talk/api/tickets"), target);
  expect(denied.status).toBe(503);
  expect(await denied.json()).toMatchObject({ error: { code: "service_disabled" } });
});

it("limits admin attempts independently of public traffic", async () => {
  const response = await worker.fetch(new Request("https://call-staging.sleep.talk/api/admin/status"), bindings({ ADMIN_RATE_LIMIT: deny }));
  expect(response.status).toBe(429);
});

it("applies the connection limit to staging development rooms too", async () => {
  const response = await worker.fetch(new Request("https://call-staging.sleep.talk/api/dev/rooms/testroom/signal?role=agent", { headers: { Upgrade: "websocket" } }), bindings({ SIGNAL_RATE_LIMIT: deny }));
  expect(response.status).toBe(429);
});

it("closes a flooding staging development socket", async () => {
  const response = await SELF.fetch("https://call-staging.sleep.talk/api/dev/rooms/testroom/signal?role=agent", { headers: { Upgrade: "websocket" } });
  expect(response.status).toBe(101);
  const socket = response.webSocket!;
  socket.accept();
  const closed = new Promise<number>((resolve) => socket.addEventListener("close", (event) => resolve(event.code), { once: true }));
  for (let index = 0; index < 241; index++) {
    socket.send(JSON.stringify({ v: 1, id: crypto.randomUUID(), type: "unknown.message", payload: {} }));
  }
  expect(await closed).toBe(4008);
});
