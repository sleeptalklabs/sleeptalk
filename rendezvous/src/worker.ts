import { DurableObject } from "cloudflare:workers";

import {
  constantTimeEqual,
  normalizedNetworkPrefix,
  parseGrantAuthorization,
  sourceFingerprint,
  validInstallationId,
  validRequestId,
  validSecretHash,
} from "./access_protocol";
import { browserRuntimePolicy } from "./browser_policy";
import { admitSignal, signalMessageBytes, type SignalAllowance } from "./abuse";
import {
  Installation,
  ServiceControl,
  type ControlPlaneEnvironment,
  type ControlResult,
} from "./control_plane";

import {
  TERMINAL_SIGNAL_TYPES,
  applyGuardRelay,
  guardSignal,
  initialSignalGuardState,
  parsePeerRole,
  parseSignal,
  relayedInRoom,
  serverEnvelope,
  signalTypesForKind,
  type PeerRole,
  type SignalEnvelope,
} from "./protocol";
import {
  DEFAULT_TICKET_TTL_SECONDS,
  INACTIVITY_SECONDS,
  MAX_TICKET_TTL_SECONDS,
  MIN_TICKET_TTL_SECONDS,
  SESSION_CAP_SECONDS,
  SETUP_INACTIVITY_SECONDS,
  SETUP_MAX_TTL_SECONDS,
  SETUP_SESSION_CAP_SECONDS,
  connectTicket,
  inactivitySeconds,
  sessionCapSeconds,
  ticketKind,
  type TicketKind,
  nextDeadline,
  observeRelayPath,
  redeemTicket,
  timeoutReason,
  touchTicket,
  type TicketEndReason,
  type TicketRecord,
  type TicketTombstone,
} from "./ticket";
import { isTicketRecord, purgeTicketStorage } from "./ticket_cleanup";
import { parseHostedServiceConfig } from "./service_config";
import {
  STUN_ICE_SERVERS,
  TurnCredentialError,
  generateIceServers,
  type IceServerConfig,
} from "./turn";

// Bindings come from Wrangler; tests may omit limiters, production may not.
type RateLimitBindings = Pick<CloudflareBindings,
  "ENROLL_RATE_LIMIT" | "TICKET_RATE_LIMIT" | "REDEEM_RATE_LIMIT" |
  "REQUEST_RATE_LIMIT" | "GLOBAL_REQUEST_RATE_LIMIT" | "SIGNAL_RATE_LIMIT" | "ADMIN_RATE_LIMIT"
>;
interface Env extends ControlPlaneEnvironment, Partial<RateLimitBindings> {
  DEV_ROOMS: DurableObjectNamespace<DevRoom>;
  ASSETS: CloudflareBindings["ASSETS"];
}

export { Installation, ServiceControl };

interface SocketAttachment {
  role: PeerRole | null;
  authenticated: boolean;
  /** When an unauthenticated socket was accepted; it must auth soon after. */
  acceptedAt?: number;
  allowance?: SignalAllowance;
}

const TICKET_KEY = "ticket";
// The host's TURN credentials, minted at redemption and handed to the host
// over its authenticated socket. Purged with the rest of the room's storage.
const HOST_ICE_KEY = "host_ice";
const AUTH_DEADLINE_MS = 10_000;
const MAX_PENDING_SOCKETS = 4;
const MAX_TICKET_REQUEST_BYTES = 8 * 1024;
const MAX_ENROLL_REQUEST_BYTES = 2 * 1024;
const MAX_REDEEM_REQUEST_BYTES = 4 * 1024;
const MAX_ADMIN_REQUEST_BYTES = 4 * 1024;
const TOMBSTONE_TTL_MILLISECONDS = 60 * 60 * 1000;
const ACTIVITY_WRITE_MILLISECONDS = 15_000;
const TICKET_PATH = /^\/api\/tickets\/([A-Za-z0-9_-]{20,64})\/(redeem|signal)$/;
const TURN_CREDENTIAL_TTL_SECONDS = SESSION_CAP_SECONDS + 60;

function jsonResponse(
  status: number,
  message: string,
  code = "request_failed",
  extra: Record<string, unknown> = {},
): Response {
  return Response.json(
    { error: { code, message }, ...extra },
    {
      status,
      headers: {
        "Cache-Control": "no-store",
        "Strict-Transport-Security": HSTS,
      },
    },
  );
}

const HSTS = "max-age=31536000";

function addSecurityHeaders(response: Response, requestUrl: URL): Response {
  const headers = new Headers(response.headers);
  headers.set("Referrer-Policy", "no-referrer");
  headers.set("X-Content-Type-Options", "nosniff");
  headers.set("Permissions-Policy", "camera=(), microphone=(self)");
  headers.set("Strict-Transport-Security", HSTS);
  // Signaling is same-origin; name the WebSocket origin explicitly because
  // older WebKit does not match 'self' against wss:.
  const socketOrigin = `${requestUrl.protocol === "https:" ? "wss:" : "ws:"}//${requestUrl.host}`;
  headers.set(
    "Content-Security-Policy",
    `default-src 'self'; connect-src 'self' ${socketOrigin}; media-src 'self' blob:; ` +
      "script-src 'self'; style-src 'self'; img-src 'self' data: blob:; base-uri 'none'; frame-ancestors 'none'",
  );
  return new Response(response.body, {
    status: response.status,
    statusText: response.statusText,
    headers,
  });
}

export class DevRoom extends DurableObject<Env> {
  async fetch(request: Request): Promise<Response> {
    if (request.headers.get("Upgrade")?.toLowerCase() !== "websocket") {
      return jsonResponse(426, "This endpoint requires a WebSocket connection.");
    }

    const role = parsePeerRole(new URL(request.url).searchParams.get("role"));
    if (!role) return jsonResponse(400, "Role must be agent or browser.");

    const occupied = this.ctx
      .getWebSockets(role)
      .some((socket) => socket.readyState === WebSocket.OPEN);
    if (occupied) return jsonResponse(409, `The ${role} side is already connected.`);

    const pair = new WebSocketPair();
    const [client, server] = Object.values(pair);
    this.ctx.acceptWebSocket(server, [role]);
    server.serializeAttachment({ role, authenticated: true } satisfies SocketAttachment);

    queueMicrotask(() => this.announceReady());
    return new Response(null, { status: 101, webSocket: client });
  }

  async webSocketMessage(socket: WebSocket, message: string | ArrayBuffer): Promise<void> {
    if (socket.readyState !== WebSocket.OPEN) return;
    const attachment = socket.deserializeAttachment() as SocketAttachment | null;
    const allowance = admitSignal(attachment?.allowance, signalMessageBytes(message), Date.now());
    if (!allowance) {
      socket.close(4008, "Signaling allowance exceeded.");
      return;
    }
    socket.serializeAttachment({ ...attachment, allowance });
    if (typeof message !== "string") {
      socket.close(4002, "Text signaling messages only.");
      return;
    }

    const parsed = parseSignal(message);
    if (parsed.kind === "invalid") {
      socket.close(4002, "Invalid signaling message.");
      return;
    }
    if (parsed.kind === "unknown") return;
    const envelope = parsed.envelope;

    if (envelope.type === "ping") {
      this.safeSend(socket, serverEnvelope("pong", envelope.payload));
      return;
    }

    if (!attachment?.authenticated || !attachment.role) {
      socket.close(4002, "Missing peer role.");
      return;
    }

    this.sendToOther(attachment.role, envelope);
    if (envelope.type === "call.hangup") this.closeAll(1000, "Call ended.");
  }

  async webSocketClose(
    socket: WebSocket,
    code: number,
    reason: string,
    wasClean: boolean,
  ): Promise<void> {
    const attachment = socket.deserializeAttachment() as SocketAttachment | null;
    if (attachment?.authenticated && attachment.role) {
      this.sendToOther(
        attachment.role,
        serverEnvelope("call.ended", {
          reason: wasClean ? "peer_hung_up" : "connection_lost",
          code,
          detail: reason,
        }),
      );
    }
  }

  async webSocketError(socket: WebSocket): Promise<void> {
    const attachment = socket.deserializeAttachment() as SocketAttachment | null;
    if (attachment?.authenticated && attachment.role) {
      this.sendToOther(
        attachment.role,
        serverEnvelope("call.ended", { reason: "connection_lost" }),
      );
    }
    socket.close(1011, "Signaling connection failed.");
  }

  private announceReady(): void {
    const agents = this.ctx.getWebSockets("agent").filter(isOpen);
    const browsers = this.ctx.getWebSockets("browser").filter(isOpen);
    if (!agents.length || !browsers.length) return;
    const message = JSON.stringify(serverEnvelope("peer.ready"));
    for (const socket of [...agents, ...browsers]) this.safeSend(socket, message);
  }

  private sendToOther(sender: PeerRole, envelope: SignalEnvelope | string): void {
    const recipient = sender === "agent" ? "browser" : "agent";
    const message = typeof envelope === "string" ? envelope : JSON.stringify(envelope);
    for (const socket of this.ctx.getWebSockets(recipient)) this.safeSend(socket, message);
  }

  private closeAll(code: number, reason: string): void {
    for (const socket of this.ctx.getWebSockets()) {
      if (socket.readyState === WebSocket.OPEN) socket.close(code, reason);
    }
  }

  private safeSend(socket: WebSocket, message: SignalEnvelope | string): void {
    if (socket.readyState !== WebSocket.OPEN) return;
    socket.send(typeof message === "string" ? message : JSON.stringify(message));
  }
}

export class TicketRoom extends DurableObject<Env> {

  /** Signal duplicate/reorder guard state (r2-browser-state-machine). */
  private readonly signalGuard = initialSignalGuardState();
  async fetch(request: Request): Promise<Response> {
    const url = new URL(request.url);
    if (url.hostname === "ticket.internal" && url.pathname === "/initialize") {
      return this.initialize(request);
    }
    if (url.hostname === "ticket.internal" && url.pathname === "/status") {
      return this.internalStatus();
    }
    if (url.hostname === "ticket.internal" && url.pathname === "/finish") {
      return this.internalFinish();
    }
    if (url.pathname.endsWith("/redeem")) return this.redeem(request);
    if (url.pathname.endsWith("/signal")) return this.acceptSignal(request);
    return jsonResponse(404, "Ticket endpoint not found.", "not_found");
  }

  async webSocketMessage(socket: WebSocket, message: string | ArrayBuffer): Promise<void> {
    if (socket.readyState !== WebSocket.OPEN) return;
    const attachment = socket.deserializeAttachment() as SocketAttachment | null;
    const allowance = admitSignal(attachment?.allowance, signalMessageBytes(message), Date.now());
    if (!allowance) {
      socket.close(4008, "Signaling allowance exceeded.");
      // Only an authenticated peer may end the call and release its TURN
      // credentials. A stranger flooding an unauthenticated socket cannot.
      if (attachment?.authenticated) {
        const record = await this.record();
        if (record && record.state !== "ended") await this.finish(record, "connection_lost");
      }
      return;
    }
    socket.serializeAttachment({ ...attachment, allowance });
    if (typeof message !== "string") {
      socket.close(4002, "Text signaling messages only.");
      return;
    }
    const parsed = parseSignal(message);
    if (parsed.kind === "invalid") {
      socket.close(4002, "Invalid signaling message.");
      return;
    }
    if (parsed.kind === "unknown") return;
    const envelope = parsed.envelope;
    if (!attachment?.authenticated || !attachment.role) {
      await this.authenticateSocket(socket, envelope);
      return;
    }

    const record = await this.record();
    if (!record || record.state === "ended") {
      // Ended calls (including expired tickets) cannot reconnect: every
      // subsequent message on the socket is rejected.
      socket.close(4004, "Call ended.");
      return;
    }
    const timeout = timeoutReason(record, Date.now());
    if (timeout) {
      await this.finish(record, timeout);
      return;
    }

    let active = this.hasBothPeers() ? await this.touch(record) : record;
    if (envelope.type === "call.auth") {
      socket.close(4002, "Already authenticated.");
      return;
    }
    if (envelope.type === "ping") {
      safeSend(socket, serverEnvelope("pong", envelope.payload));
      return;
    }
    const kind = ticketKind(active);
    // A setup room relays only setup messages in their allowed direction, and
    // a call room never relays them.
    if (!relayedInRoom(kind, attachment.role, envelope.type)) return;
    // Duplicate/reordered-message guards (r2-browser-state-machine): pure
    // decisions from protocol.ts — duplicate terminal ids are no-ops,
    // answers before any offer are rejected as reordered.
    const guard = this.signalGuard;
    const decision = guardSignal(envelope, guard);
    if (decision.action === "reject") {
      if ((decision.error.payload.code as string) === "duplicate_terminal") return;
      safeSend(socket, decision.error);
      return;
    }
    applyGuardRelay(envelope, guard);
    const relay = relayObservation(envelope);
    if (relay !== null) {
      const observed = observeRelayPath(active, relay);
      if (observed !== active) {
        active = observed;
        await this.ctx.storage.put(TICKET_KEY, active);
      }
    }
    // SDP, ICE and encrypted setup payloads are forwarded to the peer only.
    // Never persist or log them.
    this.sendToOther(attachment.role, envelope);
    if (TERMINAL_SIGNAL_TYPES.has(envelope.type)) {
      // Explicit terminal message (hangup, user cancel, or finished setup):
      // close media and rendezvous state now (r2-browser-state-machine).
      await this.finish(active, envelope.type === "setup.done" ? "setup_complete" : "user_cancel");
    }
  }

  async webSocketClose(socket: WebSocket, _code: number, _reason: string): Promise<void> {
    const record = await this.record();
    if (!record || record.state === "ended") return;
    const attachment = socket.deserializeAttachment() as SocketAttachment | null;
    if (!attachment?.authenticated || !attachment.role) return;
    await this.finish(
      record,
      attachment.role === "agent" ? "agent_disconnected" : "browser_disconnected",
    );
  }

  async webSocketError(socket: WebSocket): Promise<void> {
    const attachment = socket.deserializeAttachment() as SocketAttachment | null;
    if (attachment?.authenticated) {
      const record = await this.record();
      if (record && record.state !== "ended") await this.finish(record, "connection_lost");
    }
    socket.close(1011, "Signaling connection failed.");
  }

  async alarm(): Promise<void> {
    this.closeUnauthenticated();
    const stored = await this.stored();
    if (!stored) return;
    if (!isTicketRecord(stored)) {
      await this.ctx.storage.deleteAll();
      return;
    }
    const record = stored;
    const reason = timeoutReason(record, Date.now());
    if (reason) await this.finish(record, reason);
    else await this.schedule(record);
  }

  private async initialize(request: Request): Promise<Response> {
    if (request.method !== "POST") return jsonResponse(405, "POST required.", "method_not_allowed");
    if (await this.stored()) return jsonResponse(409, "Ticket already exists.", "ticket_exists");
    const record = (await request.json()) as TicketRecord;
    if (record.v === 2 && Date.now() >= record.initializeBy) {
      return jsonResponse(410, "Ticket initialization expired.", "ticket_expired");
    }
    await this.ctx.storage.put(TICKET_KEY, record);
    await this.schedule(record);
    return Response.json({ ok: true });
  }

  private async internalStatus(): Promise<Response> {
    const stored = await this.stored();
    return stored
      ? Response.json({ exists: true, ticket: isTicketRecord(stored) })
      : jsonResponse(404, "Ticket not found.", "not_found");
  }

  private async internalFinish(): Promise<Response> {
    const record = await this.record();
    if (record && record.state !== "ended") {
      await this.finish(record, "installation_revoked");
    }
    return Response.json({ ok: true });
  }

  private async redeem(request: Request): Promise<Response> {
    if (request.method !== "POST") return jsonResponse(405, "POST required.", "method_not_allowed");
    const stored = await this.stored();
    if (!stored) return jsonResponse(404, "This call link isn't valid.", "invalid_ticket");
    if (!isTicketRecord(stored)) return tombstoneResponse(stored);
    const record = stored;
    const body = await readJsonBody(request, MAX_REDEEM_REQUEST_BYTES);
    if (body === "too_large") return jsonResponse(413, "Ticket proof is too large.", "too_large");
    if (!body) return jsonResponse(400, "Ticket proof must be JSON.", "invalid_ticket");
    const ticket = typeof body.ticket === "string" ? body.ticket : "";
    const detected = browserRuntimePolicy(request.headers.get("User-Agent") ?? "");
    const relayRequired =
      detected.iceTransportPolicy === "relay" || body.relayRequired === true;
    if (!ticket || (await tokenHash(ticket)) !== record.ticketHash) {
      return jsonResponse(404, "This call link isn't valid.", "invalid_ticket");
    }
    const preview = redeemTicket(record, Date.now(), randomToken());
    if (!preview.ok) {
      if (preview.code === "expired") await this.finish(record, "ticket_expired");
      return preview.code === "expired"
        ? jsonResponse(410, "This call link has expired.", "ticket_expired")
        : jsonResponse(409, "This call link has already been used.", "ticket_used");
    }

    const kind = ticketKind(record);
    let iceServers: IceServerConfig[] = [];
    let hostIceServers: IceServerConfig[] | null = null;
    if (kind === "setup") {
      if (record.v !== 2) return jsonResponse(404, "This call link isn't valid.", "invalid_ticket");
      const admission = await this.env.INSTALLATIONS
        .getByName(record.installationId)
        .admitSetupBrowser({ reservationId: record.reservationId });
      if (!admission.ok) return controlErrorResponse(admission);
    } else if (record.v === 2) {
      // Relay units are charged and TURN credentials minted only here, when
      // a browser actually redeems the link.
      const admission = await this.env.INSTALLATIONS
        .getByName(record.installationId)
        .admitCall({
          installationId: record.installationId,
          reservationId: record.reservationId,
          requirement: relayRequired ? "required" : "preferred",
          now: Date.now(),
        });
      if (!admission.ok) return controlErrorResponse(admission);
      iceServers = admission.value.browserIceServers;
      hostIceServers = admission.value.hostIceServers;
    } else {
      try {
        iceServers = await generateIceServers(
          this.env,
          TURN_CREDENTIAL_TTL_SECONDS,
          fetch,
          relayRequired ? "required" : "preferred",
        );
      } catch (error) {
        return turnUnavailableResponse(error);
      }
    }

    // Credential generation awaits an external service. Re-read before the
    // mutation so two concurrent redemption attempts cannot both succeed.
    const current = await this.stored();
    if (!current) return jsonResponse(404, "This call link isn't valid.", "invalid_ticket");
    if (!isTicketRecord(current)) return tombstoneResponse(current);
    const result = redeemTicket(current, Date.now(), randomToken());
    if (!result.ok) {
      if (result.code === "expired") await this.finish(current, "ticket_expired");
      return result.code === "expired"
        ? jsonResponse(410, "This call link has expired.", "ticket_expired")
        : jsonResponse(409, "This call link has already been used.", "ticket_used");
    }
    await this.ctx.storage.put(TICKET_KEY, result.record);
    if (hostIceServers) await this.ctx.storage.put(HOST_ICE_KEY, hostIceServers);
    await this.schedule(result.record);
    const redeemed = JSON.stringify(await this.redeemedEnvelope());
    for (const socket of this.socketsForRole("agent")) safeSend(socket, redeemed);

    const signalUrl = new URL(request.url);
    signalUrl.pathname = signalUrl.pathname.replace(/\/redeem$/, "/signal");
    signalUrl.protocol = signalUrl.protocol === "https:" ? "wss:" : "ws:";
    signalUrl.search = "";
    const session = {
      kind,
      browserSignalUrl: signalUrl.toString(),
      browserAccessKey: result.record.browserKey,
      sessionExpiresAt: new Date(result.record.sessionExpiresAt ?? 0).toISOString(),
      sessionCapSeconds: sessionCapSeconds(result.record),
      inactivitySeconds: inactivitySeconds(result.record),
    };
    return Response.json(
      kind === "setup"
        ? session
        : { ...session, iceServers, iceTransportPolicy: relayRequired ? "relay" : "all" },
      { headers: { "Cache-Control": "no-store" } },
    );
  }

  private async acceptSignal(request: Request): Promise<Response> {
    if (request.headers.get("Upgrade")?.toLowerCase() !== "websocket") {
      return jsonResponse(426, "This endpoint requires a WebSocket connection.");
    }
    const url = new URL(request.url);
    if (url.search) return jsonResponse(400, "Signal URLs cannot contain query parameters.");
    const stored = await this.stored();
    if (!stored) return jsonResponse(404, "This call link isn't valid.", "invalid_ticket");
    if (!isTicketRecord(stored)) return tombstoneResponse(stored);
    const record = stored;
    const timeout = timeoutReason(record, Date.now());
    if (timeout) {
      await this.finish(record, timeout);
      return jsonResponse(410, "This call is no longer available.", timeout);
    }
    if (record.state === "ended") {
      return jsonResponse(410, "This call is no longer available.", record.endReason ?? "call_ended");
    }
    const pending = this.ctx.getWebSockets().filter((socket) => {
      const attachment = socket.deserializeAttachment() as SocketAttachment | null;
      return isOpen(socket) && attachment?.authenticated !== true;
    });
    if (pending.length >= MAX_PENDING_SOCKETS) {
      return jsonResponse(429, "Too many unauthenticated connections.", "too_many_connections");
    }
    const pair = new WebSocketPair();
    const [client, server] = Object.values(pair);
    this.ctx.acceptWebSocket(server);
    server.serializeAttachment({
      role: null,
      authenticated: false,
      acceptedAt: Date.now(),
    } satisfies SocketAttachment);
    // A socket that has not authenticated by the deadline is closed. The
    // timer only holds the object awake for the deadline itself.
    setTimeout(() => this.closeUnauthenticated(), AUTH_DEADLINE_MS + 50);
    return new Response(null, { status: 101, webSocket: client });
  }

  private closeUnauthenticated(): void {
    const now = Date.now();
    for (const socket of this.ctx.getWebSockets()) {
      const attachment = socket.deserializeAttachment() as SocketAttachment | null;
      if (
        isOpen(socket) &&
        attachment?.authenticated !== true &&
        now - (attachment?.acceptedAt ?? 0) >= AUTH_DEADLINE_MS
      ) {
        socket.close(4003, "Authentication required.");
      }
    }
  }

  private async redeemedEnvelope(): Promise<SignalEnvelope> {
    const iceServers = await this.ctx.storage.get<IceServerConfig[]>(HOST_ICE_KEY);
    return serverEnvelope("ticket.redeemed", iceServers ? { iceServers } : {});
  }

  private async authenticateSocket(socket: WebSocket, envelope: SignalEnvelope): Promise<void> {
    if (envelope.type !== "call.auth") {
      socket.close(4003, "Authentication required.");
      return;
    }
    const role = parsePeerRole(
      typeof envelope.payload.role === "string" ? envelope.payload.role : null,
    );
    const key = typeof envelope.payload.key === "string" ? envelope.payload.key : "";
    if (!role || !key) {
      socket.close(4003, "Invalid call credential.");
      return;
    }
    const record = await this.record();
    if (!record || record.state === "ended") {
      socket.close(4004, "Call ended.");
      return;
    }
    const timeout = timeoutReason(record, Date.now());
    if (timeout) {
      await this.finish(record, timeout);
      return;
    }
    const authorized =
      (role === "agent" && constantTimeEqual(key, record.agentKey)) ||
      (role === "browser" &&
        record.state === "redeemed" &&
        record.browserKey !== null &&
        constantTimeEqual(key, record.browserKey));
    if (!authorized) {
      socket.close(4003, "Invalid call credential.");
      return;
    }
    if (this.socketsForRole(role).some(isOpen)) {
      socket.close(4009, `${role} already connected.`);
      return;
    }
    const prior = socket.deserializeAttachment() as SocketAttachment | null;
    socket.serializeAttachment({ role, authenticated: true, allowance: prior?.allowance } satisfies SocketAttachment);
    const connected = connectTicket(record, role);
    await this.ctx.storage.put(TICKET_KEY, connected);
    const active = await this.touch(connected);
    await this.schedule(active);
    if (role === "agent" && active.state === "redeemed") {
      safeSend(socket, await this.redeemedEnvelope());
    }
    queueMicrotask(() => this.announceReady());
  }

  private async record(): Promise<TicketRecord | undefined> {
    const stored = await this.stored();
    return isTicketRecord(stored) ? stored : undefined;
  }

  private async stored(): Promise<TicketRecord | TicketTombstone | undefined> {
    return this.ctx.storage.get<TicketRecord | TicketTombstone>(TICKET_KEY);
  }

  private async touch(record: TicketRecord): Promise<TicketRecord> {
    // Activity is written at most every ACTIVITY_WRITE_MILLISECONDS: a peer
    // flooding messages must not turn each one into two storage writes. The
    // inactivity deadline is minutes long, so this costs it a few seconds.
    const now = Date.now();
    if (record.state === "redeemed" && now - (record.lastActivityAt ?? 0) < ACTIVITY_WRITE_MILLISECONDS) {
      return record;
    }
    const active = touchTicket(record, now);
    if (active !== record) await this.ctx.storage.put(TICKET_KEY, active);
    await this.schedule(active);
    return active;
  }

  private async schedule(record: TicketRecord): Promise<void> {
    const deadline = nextDeadline(record);
    if (deadline === null) await this.ctx.storage.deleteAlarm();
    else await this.ctx.storage.setAlarm(deadline);
  }

  private async finish(record: TicketRecord, reason: TicketEndReason): Promise<void> {
    if (record.state === "ended") return;
    const now = Date.now();
    const finalized = await this.ctx.storage.transaction((transaction) =>
      purgeTicketStorage(transaction, {
        now,
        tombstoneTtlMilliseconds: TOMBSTONE_TTL_MILLISECONDS,
      }),
    );
    if (!finalized) return;
    if (record.v === 2) {
      this.ctx.waitUntil(
        this.env.INSTALLATIONS
          .getByName(record.installationId)
          .release(record.reservationId),
      );
    }
    // The end reason is the whole point of the summary when a call drops on
    // its own. The browser reports its own call_ended telemetry, but by then
    // the ticket is already ended, so webSocketMessage rejects it: for a
    // server-driven end this log is the only record of why.
    console.log(JSON.stringify({ ...finalized.summary, reason }));
    const message = JSON.stringify(serverEnvelope("call.ended", { reason }));
    for (const socket of this.ctx.getWebSockets()) {
      if (!isOpen(socket)) continue;
      safeSend(socket, message);
      socket.close(1000, "Call ended.");
    }
  }

  private announceReady(): void {
    const agents = this.socketsForRole("agent").filter(isOpen);
    const browsers = this.socketsForRole("browser").filter(isOpen);
    if (!agents.length || !browsers.length) return;
    const message = JSON.stringify(serverEnvelope("peer.ready"));
    for (const socket of [...agents, ...browsers]) safeSend(socket, message);
  }

  private hasBothPeers(): boolean {
    return (
      this.socketsForRole("agent").some(isOpen) &&
      this.socketsForRole("browser").some(isOpen)
    );
  }

  private sendToOther(sender: PeerRole, envelope: SignalEnvelope): void {
    const recipient = sender === "agent" ? "browser" : "agent";
    const message = JSON.stringify(envelope);
    for (const socket of this.socketsForRole(recipient)) safeSend(socket, message);
  }

  private socketsForRole(role: PeerRole): WebSocket[] {
    return this.ctx.getWebSockets().filter((socket) => {
      const attachment = socket.deserializeAttachment() as SocketAttachment | null;
      return attachment?.authenticated === true && attachment.role === role;
    });
  }
}

function isOpen(socket: WebSocket): boolean {
  return socket.readyState === WebSocket.OPEN;
}

function safeSend(socket: WebSocket, message: SignalEnvelope | string): void {
  if (!isOpen(socket)) return;
  socket.send(typeof message === "string" ? message : JSON.stringify(message));
}

function tombstoneResponse(tombstone: TicketTombstone): Response {
  return tombstone.state === "used"
    ? jsonResponse(409, "This call link has already been used.", "ticket_used")
    : jsonResponse(410, "This call link has expired.", "ticket_expired");
}

function relayObservation(envelope: SignalEnvelope): boolean | null {
  if (envelope.type !== "call.telemetry" || envelope.payload.stage !== "transport_stats") {
    return null;
  }
  return typeof envelope.payload.usesRelay === "boolean"
    ? envelope.payload.usesRelay
    : null;
}

async function mintTicket(request: Request, env: Env): Promise<Response> {
  if (request.method !== "POST") return jsonResponse(405, "POST required.", "method_not_allowed");
  const config = parseHostedServiceConfig(env);
  if (!config.ok) return jsonResponse(503, "The call service is not configured.", "service_misconfigured");
  const origin = new URL(request.url).origin;
  if (origin !== config.config.publicOrigin) {
    return jsonResponse(503, "The call service origin is not configured.", "service_misconfigured");
  }
  const authorization = parseGrantAuthorization(request.headers.get("Authorization"));
  if (!authorization) return jsonResponse(401, "Installation access is required.", "grant_invalid");
  const requestId = request.headers.get("Idempotency-Key");
  if (!validRequestId(requestId)) {
    return jsonResponse(400, "A valid Idempotency-Key is required.", "invalid_request_id");
  }
  const body = await readJsonBody(request, MAX_TICKET_REQUEST_BYTES);
  if (body === "too_large") return jsonResponse(413, "Ticket request is too large.", "too_large");
  if (!body) return jsonResponse(400, "Ticket request must be JSON.", "invalid_json");
  const kindValue = body.kind ?? "call";
  if (kindValue !== "call" && kindValue !== "setup") {
    return jsonResponse(400, "kind must be call or setup.", "invalid_kind");
  }
  const kind: TicketKind = kindValue;
  const maxTtl = kind === "setup" ? SETUP_MAX_TTL_SECONDS : MAX_TICKET_TTL_SECONDS;
  const requestedTtl = body.ttlSeconds ?? (kind === "setup" ? SETUP_MAX_TTL_SECONDS : DEFAULT_TICKET_TTL_SECONDS);
  if (
    typeof requestedTtl !== "number" ||
    !Number.isInteger(requestedTtl) ||
    requestedTtl < 1 ||
    requestedTtl > maxTtl
  ) {
    return jsonResponse(400, `ttlSeconds must be an integer from 1 to ${maxTtl}.`, "invalid_ttl");
  }
  // Very short links are raised to the floor rather than refused, so older
  // hosts that ask for a few seconds keep working.
  const ttlSeconds = Math.max(requestedTtl, MIN_TICKET_TTL_SECONDS);

  const result = await env.INSTALLATIONS.getByName(authorization.installationId).mint({
    installationId: authorization.installationId,
    secret: authorization.secret,
    requestId,
    origin,
    ttlSeconds,
    kind,
    now: Date.now(),
  });
  if (!result.ok) return controlErrorResponse(result);
  const ticket = result.value;
  const common = {
    kind,
    ticket: ticket.token,
    routingId: ticket.routingId,
    joinUrl: ticket.joinUrl,
    agentSignalUrl: ticket.agentSignalUrl,
    agentAccessKey: ticket.agentAccessKey,
    expiresAt: ticket.expiresAt,
    signalTypes: signalTypesForKind(kind),
  };
  return Response.json(
    kind === "setup"
      ? { ...common, sessionCapSeconds: SETUP_SESSION_CAP_SECONDS, inactivitySeconds: SETUP_INACTIVITY_SECONDS }
      : {
          ...common,
          // TURN credentials reach the host with ticket.redeemed, once a
          // browser has redeemed the link. Older hosts use STUN from here.
          iceServers: STUN_ICE_SERVERS,
          sessionCapSeconds: SESSION_CAP_SECONDS,
          inactivitySeconds: INACTIVITY_SECONDS,
        },
    { status: 201, headers: { "Cache-Control": "no-store" } },
  );
}

async function enrollInstallation(request: Request, env: Env): Promise<Response> {
  if (request.method !== "POST") return jsonResponse(405, "POST required.", "method_not_allowed");
  const config = parseHostedServiceConfig(env);
  if (!config.ok) return jsonResponse(503, "The call service is not configured.", "service_misconfigured");
  const requestId = request.headers.get("Idempotency-Key");
  if (!validRequestId(requestId)) {
    return jsonResponse(400, "A valid Idempotency-Key is required.", "invalid_request_id");
  }
  const sourceIp = request.headers.get("CF-Connecting-IP");
  if (!sourceIp) return jsonResponse(400, "A trusted source address is required.", "invalid_source");
  const body = await readJsonBody(request, MAX_ENROLL_REQUEST_BYTES);
  if (body === "too_large") return jsonResponse(413, "Enrollment is too large.", "too_large");
  if (!body) return jsonResponse(400, "Enrollment must be JSON.", "invalid_json");
  if (!validInstallationId(body.installationId) || !validSecretHash(body.secretHash)) {
    return jsonResponse(400, "Enrollment values are invalid.", "invalid_enrollment");
  }
  const fingerprint = await sourceFingerprint(sourceIp, config.config.enrollmentHashKey);
  if (!fingerprint) return jsonResponse(400, "A trusted source address is required.", "invalid_source");
  const result = await env.INSTALLATIONS.getByName(body.installationId).enroll({
    installationId: body.installationId,
    secretHash: body.secretHash,
    requestId,
    sourceFingerprint: fingerprint,
    now: Date.now(),
  });
  return result.ok
    ? Response.json({ installationId: body.installationId, enrolled: true }, { status: 201 })
    : controlErrorResponse(result);
}

async function adminRequest(request: Request, env: Env): Promise<Response> {
  const config = parseHostedServiceConfig(env);
  if (!config.ok) return jsonResponse(503, "The call service is not configured.", "service_misconfigured");
  const token = (request.headers.get("Authorization") ?? "").replace(/^Bearer\s+/i, "");
  if (!constantTimeEqual(token, config.config.adminToken)) {
    return jsonResponse(401, "Administrative access is required.", "admin_unauthorized");
  }
  const url = new URL(request.url);
  const control = env.SERVICE_CONTROL.getByName("global");
  if (url.pathname === "/api/admin/status" && request.method === "GET") {
    const result = await control.status();
    return result.ok ? Response.json(result.value) : controlErrorResponse(result);
  }
  if (url.pathname === "/api/admin/control" && request.method === "POST") {
    const body = await readJsonBody(request, MAX_ADMIN_REQUEST_BYTES);
    if (!body || body === "too_large") {
      return jsonResponse(400, "Control change must be JSON.", "invalid_json");
    }
    if (
      (body.action !== "halt" && body.action !== "resume") ||
      !Number.isSafeInteger(body.expectedRevision) ||
      typeof body.reason !== "string" ||
      !body.reason.trim() ||
      body.reason.length > 256
    ) {
      return jsonResponse(400, "Control change is invalid.", "invalid_control_change");
    }
    const result = await control.change({
      action: body.action,
      expectedRevision: body.expectedRevision as number,
      reason: body.reason.trim(),
    });
    return result.ok ? Response.json(result.value) : controlErrorResponse(result);
  }
  const installationMatch = url.pathname.match(
    /^\/api\/admin\/installations\/([A-Za-z0-9_-]{20,64})(?:\/(revoke))?$/,
  );
  if (installationMatch) {
    const installation = env.INSTALLATIONS.getByName(installationMatch[1]);
    if (!installationMatch[2] && request.method === "GET") {
      return Response.json(await installation.inspect());
    }
    if (installationMatch[2] === "revoke" && request.method === "POST") {
      const body = await readJsonBody(request, MAX_ADMIN_REQUEST_BYTES);
      if (!body || body === "too_large") {
        return jsonResponse(400, "Revocation must be JSON.", "invalid_json");
      }
      const reason = typeof body.reason === "string" ? body.reason.trim() : "operator";
      if (!reason || reason.length > 256) {
        return jsonResponse(400, "Revocation reason is invalid.", "invalid_revocation");
      }
      return Response.json(await installation.revoke({ reason, now: Date.now() }));
    }
  }
  return jsonResponse(404, "Not found.", "not_found");
}

function controlErrorResponse(result: Extract<ControlResult<unknown>, { ok: false }>): Response {
  const messages: Record<string, string> = {
    service_disabled: "The hosted call service is temporarily disabled.",
    service_misconfigured: "The hosted call service is not configured.",
    enrollment_limited: "This network has reached the installation enrollment limit.",
    grant_invalid: "This installation credential is not valid.",
    grant_revoked: "This installation credential has been revoked.",
    mint_limited: "This installation has reached its call-link limit.",
    setup_limited: "This installation has reached its setup-link limit.",
    mint_in_progress: "A call-link request is already being prepared.",
    concurrency_limited: "This installation already has the maximum number of calls.",
    relay_allowance_exhausted: "The hosted relay allowance has been reached.",
    control_conflict: "The service control state changed. Read status and retry.",
    turn_unavailable: "The TURN relay is unavailable.",
  };
  const statuses: Record<string, number> = {
    grant_invalid: 401,
    grant_revoked: 403,
    enrollment_limited: 429,
    mint_limited: 429,
    setup_limited: 429,
    concurrency_limited: 429,
    relay_allowance_exhausted: 503,
    control_conflict: 409,
    mint_in_progress: 409,
    service_disabled: 503,
    service_misconfigured: 503,
    turn_unavailable: 503,
  };
  const response = jsonResponse(statuses[result.code] ?? 503, messages[result.code], result.code, {
    ...(result.retryAt === undefined
      ? {}
      : { retryAt: new Date(result.retryAt).toISOString() }),
  });
  if (result.retryAt === undefined) return response;
  const headers = new Headers(response.headers);
  headers.set("Retry-After", String(Math.max(0, Math.ceil((result.retryAt - Date.now()) / 1000))));
  return new Response(response.body, {
    status: response.status,
    statusText: response.statusText,
    headers,
  });
}

function turnUnavailableResponse(error: unknown): Response {
  const message =
    error instanceof TurnCredentialError
      ? error.message
      : "The TURN service could not create credentials.";
  return jsonResponse(503, message, "turn_unavailable");
}

function randomToken(byteLength = 32): string {
  const bytes = crypto.getRandomValues(new Uint8Array(byteLength));
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/, "");
}

/**
 * Read a JSON object body, counting the bytes actually received rather than
 * trusting Content-Length. Returns null for a missing or malformed object.
 */
async function readJsonBody(
  request: Request,
  maxBytes: number,
): Promise<Record<string, unknown> | "too_large" | null> {
  const declared = Number(request.headers.get("Content-Length") ?? "0");
  if (declared > maxBytes) return "too_large";
  if (!request.body) return null;
  const reader = request.body.getReader();
  const chunks: Uint8Array[] = [];
  let received = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    received += value.byteLength;
    if (received > maxBytes) {
      await reader.cancel();
      return "too_large";
    }
    chunks.push(value);
  }
  const bytes = new Uint8Array(received);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  try {
    const value: unknown = JSON.parse(new TextDecoder().decode(bytes));
    return typeof value === "object" && value !== null && !Array.isArray(value)
      ? (value as Record<string, unknown>)
      : null;
  } catch {
    return null;
  }
}

/** Per-network rate limit (IPv4 /24, IPv6 /48). Production checks bindings first. */
async function rateLimited(limiter: RateLimit | undefined, request: Request): Promise<boolean> {
  if (!limiter) return false;
  const ip = request.headers.get("CF-Connecting-IP") ?? "";
  const key = normalizedNetworkPrefix(ip) ?? "unknown";
  const { success } = await limiter.limit({ key });
  return !success;
}

function rateLimitedResponse(): Response {
  const response = jsonResponse(429, "Too many requests from this network.", "rate_limited");
  const headers = new Headers(response.headers);
  headers.set("Retry-After", "60");
  return new Response(response.body, { status: 429, headers });
}

async function tokenHash(token: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(token));
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    if (env.DEPLOYMENT_ENV === "production" && [
      env.REQUEST_RATE_LIMIT, env.GLOBAL_REQUEST_RATE_LIMIT, env.SIGNAL_RATE_LIMIT,
      env.ADMIN_RATE_LIMIT, env.ENROLL_RATE_LIMIT, env.TICKET_RATE_LIMIT, env.REDEEM_RATE_LIMIT,
    ].some((limiter) => !limiter)) {
      return jsonResponse(503, "The call service is not configured.", "service_misconfigured");
    }
    // Admin recovery stays reachable when the public admission limit is full.
    if (url.pathname.startsWith("/api/admin/")) {
      if (await rateLimited(env.ADMIN_RATE_LIMIT, request)) return rateLimitedResponse();
      return adminRequest(request, env);
    }
    if (env.EMERGENCY_DISABLE === "true" && url.pathname.startsWith("/api/")) {
      return jsonResponse(503, "The call service is temporarily disabled.", "service_disabled");
    }
    if (await rateLimited(env.REQUEST_RATE_LIMIT, request)) return rateLimitedResponse();
    if (env.GLOBAL_REQUEST_RATE_LIMIT && !(await env.GLOBAL_REQUEST_RATE_LIMIT.limit({ key: "public" })).success) {
      return rateLimitedResponse();
    }
    if (url.pathname === "/api/installations/enroll") {
      if (await rateLimited(env.ENROLL_RATE_LIMIT, request)) return rateLimitedResponse();
      return enrollInstallation(request, env);
    }
    if (url.pathname === "/api/tickets") {
      if (await rateLimited(env.TICKET_RATE_LIMIT, request)) return rateLimitedResponse();
      return mintTicket(request, env);
    }
    const ticketMatch = url.pathname.match(TICKET_PATH);
    if (ticketMatch) {
      if (ticketMatch[2] === "signal") {
        if (request.method !== "GET" || request.headers.get("Upgrade")?.toLowerCase() !== "websocket") {
          return jsonResponse(426, "This endpoint requires a WebSocket connection.");
        }
        if (await rateLimited(env.SIGNAL_RATE_LIMIT, request)) return rateLimitedResponse();
      } else if (request.method !== "POST") {
        return jsonResponse(405, "POST required.", "method_not_allowed");
      }
      if (ticketMatch[2] === "redeem" && (await rateLimited(env.REDEEM_RATE_LIMIT, request))) {
        return rateLimitedResponse();
      }
      const id = env.TICKETS.idFromName(ticketMatch[1]);
      return env.TICKETS.get(id).fetch(request);
    }
    const roomMatch = url.pathname.match(/^\/api\/dev\/rooms\/([A-Za-z0-9_-]{8,64})\/signal$/);
    if (roomMatch && env.DEPLOYMENT_ENV === "staging") {
      if (request.method !== "GET" || request.headers.get("Upgrade")?.toLowerCase() !== "websocket") {
        return jsonResponse(426, "This endpoint requires a WebSocket connection.");
      }
      if (await rateLimited(env.SIGNAL_RATE_LIMIT, request)) return rateLimitedResponse();
      const id = env.DEV_ROOMS.idFromName(roomMatch[1]);
      return env.DEV_ROOMS.get(id).fetch(request);
    }
    if (url.pathname.startsWith("/api/")) return jsonResponse(404, "Not found.", "not_found");
    return addSecurityHeaders(await env.ASSETS.fetch(request), url);
  },
} satisfies ExportedHandler<Env>;
