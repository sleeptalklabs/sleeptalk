export const MAX_TICKET_TTL_SECONDS = 5 * 60;
// Shorter requests are raised to this: a link that dies in a second only
// churns reservations and gives no one time to open it.
export const MIN_TICKET_TTL_SECONDS = 60;
export const DEFAULT_TICKET_TTL_SECONDS = MAX_TICKET_TTL_SECONDS;
export const SESSION_CAP_SECONDS = 30 * 60;
export const INACTIVITY_SECONDS = 90;
// Setup links carry no media: the user needs time to find and paste keys,
// and the host needs time to check each one with its provider.
export const SETUP_MAX_TTL_SECONDS = 10 * 60;
export const SETUP_SESSION_CAP_SECONDS = 15 * 60;
export const SETUP_INACTIVITY_SECONDS = 5 * 60;

export type TicketKind = "call" | "setup";

export type TicketState = "issued" | "redeemed" | "ended";

export type TicketEndReason =
  | "ticket_expired"
  | "session_cap"
  | "inactivity_timeout"
  | "peer_hung_up"
  | "user_cancel"
  | "agent_disconnected"
  | "browser_disconnected"
  | "connection_lost"
  | "installation_revoked"
  | "setup_complete";

interface TicketRecordCommon {
  state: TicketState;
  ticketHash: string;
  agentKey: string;
  browserKey: string | null;
  createdAt: number;
  expiresAt: number;
  redeemedAt: number | null;
  lastActivityAt: number | null;
  sessionExpiresAt: number | null;
  endedAt: number | null;
  endReason: TicketEndReason | null;
  usesRelay: boolean | null;
  connectionCounts: Record<PeerRoleName, number>;
  /** Absent on records written before setup links existed: a call. */
  kind?: TicketKind;
}

export interface LegacyTicketRecord extends TicketRecordCommon {
  v: 1;
  metadata: {
    agentInstance: string;
    conversationId: string;
    platformUserId: string;
  };
}

export interface HostedTicketRecord extends TicketRecordCommon {
  v: 2;
  installationId: string;
  reservationId: string;
  initializeBy: number;
}

export type TicketRecord = LegacyTicketRecord | HostedTicketRecord;

export type PeerRoleName = "agent" | "browser";

export interface TicketTombstone {
  state: "ended" | "used";
}

export type RedeemResult =
  | { ok: true; record: TicketRecord }
  | { ok: false; code: "expired" | "already_used"; record: TicketRecord };

export function createTicketRecord(options: {
  now: number;
  ttlSeconds: number;
  agentKey: string;
  ticketHash: string;
  installationId: string;
  reservationId: string;
  initializeBy: number;
  kind?: TicketKind;
}): HostedTicketRecord {
  return {
    v: 2,
    kind: options.kind ?? "call",
    state: "issued",
    installationId: options.installationId,
    reservationId: options.reservationId,
    initializeBy: options.initializeBy,
    ticketHash: options.ticketHash,
    agentKey: options.agentKey,
    browserKey: null,
    createdAt: options.now,
    expiresAt: options.now + options.ttlSeconds * 1000,
    redeemedAt: null,
    lastActivityAt: null,
    sessionExpiresAt: null,
    endedAt: null,
    endReason: null,
    usesRelay: null,
    connectionCounts: { agent: 0, browser: 0 },
  };
}

export function ticketKind(record: TicketRecord): TicketKind {
  return record.kind === "setup" ? "setup" : "call";
}

export function sessionCapSeconds(record: TicketRecord): number {
  return ticketKind(record) === "setup" ? SETUP_SESSION_CAP_SECONDS : SESSION_CAP_SECONDS;
}

export function inactivitySeconds(record: TicketRecord): number {
  return ticketKind(record) === "setup" ? SETUP_INACTIVITY_SECONDS : INACTIVITY_SECONDS;
}

export function connectTicket(
  record: TicketRecord,
  role: PeerRoleName,
): TicketRecord {
  const counts = record.connectionCounts ?? { agent: 0, browser: 0 };
  return {
    ...record,
    connectionCounts: {
      ...counts,
      [role]: counts[role] + 1,
    },
  };
}

export function observeRelayPath(record: TicketRecord, usesRelay: boolean): TicketRecord {
  const observed = record.usesRelay === true || usesRelay;
  return record.usesRelay === observed ? record : { ...record, usesRelay: observed };
}

export function reconnectCount(record: TicketRecord): number {
  const counts = record.connectionCounts ?? { agent: 0, browser: 0 };
  return (
    Math.max(counts.agent - 1, 0) +
    Math.max(counts.browser - 1, 0)
  );
}

export function redeemTicket(
  record: TicketRecord,
  now: number,
  browserKey: string,
): RedeemResult {
  if (record.state === "issued" && now >= record.expiresAt) {
    const ended = endTicket(record, now, "ticket_expired");
    return { ok: false, code: "expired", record: ended };
  }
  if (record.state !== "issued") {
    const code = record.endReason === "ticket_expired" ? "expired" : "already_used";
    return { ok: false, code, record };
  }
  return {
    ok: true,
    record: {
      ...record,
      state: "redeemed",
      browserKey,
      redeemedAt: now,
      lastActivityAt: now,
      sessionExpiresAt: now + sessionCapSeconds(record) * 1000,
    },
  };
}

export function timeoutReason(record: TicketRecord, now: number): TicketEndReason | null {
  if (record.state === "ended") return null;
  if (record.state === "issued") {
    return now >= record.expiresAt ? "ticket_expired" : null;
  }
  if (record.sessionExpiresAt !== null && now >= record.sessionExpiresAt) {
    return "session_cap";
  }
  if (
    record.lastActivityAt !== null &&
    now >= record.lastActivityAt + inactivitySeconds(record) * 1000
  ) {
    return "inactivity_timeout";
  }
  return null;
}

export function nextDeadline(record: TicketRecord): number | null {
  if (record.state === "issued") return record.expiresAt;
  if (record.state !== "redeemed") return null;
  const inactivityDeadline =
    (record.lastActivityAt ?? record.redeemedAt ?? record.createdAt) +
    inactivitySeconds(record) * 1000;
  return Math.min(record.sessionExpiresAt ?? inactivityDeadline, inactivityDeadline);
}

export function touchTicket(record: TicketRecord, now: number): TicketRecord {
  return record.state === "redeemed" ? { ...record, lastActivityAt: now } : record;
}

export function endTicket(
  record: TicketRecord,
  now: number,
  reason: TicketEndReason,
): TicketRecord {
  if (record.state === "ended") return record;
  return { ...record, state: "ended", endedAt: now, endReason: reason };
}
