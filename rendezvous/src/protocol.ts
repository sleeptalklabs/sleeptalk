export const SIGNAL_VERSION = 1;

export type PeerRole = "agent" | "browser";

export type SignalType =
  | "call.auth"
  | "peer.ready"
  | "ticket.redeemed"
  | "webrtc.offer"
  | "webrtc.answer"
  | "webrtc.ice"
  | "webrtc.ice_complete"
  | "call.hangup"
  | "call.cancel"
  | "call.ended"
  | "call.error"
  | "call.telemetry"
  | "call.agent_state"
  | "call.presentation"
  | "call.interrupt"
  | "call.settings"
  | "setup.hello"
  | "setup.sealed"
  | "setup.done"
  | "ping"
  | "pong";

export interface SignalEnvelope {
  v: 1;
  id: string;
  type: SignalType;
  payload: Record<string, unknown>;
}

export const RELAYED_TYPES = new Set<SignalType>([
  "call.auth",
  "webrtc.offer",
  "webrtc.answer",
  "webrtc.ice",
  "webrtc.ice_complete",
  "call.hangup",
  "call.cancel",
  "call.interrupt",
  "call.settings",
  "call.error",
  "call.telemetry",
  // call.agent_state and call.presentation carry transcripts and agent work.
  // They travel on the peer's data channel, end to end, and never through
  // this relay.
  // A setup room carries only the page's sealed channel: setup.hello hands
  // the host a channel key sealed to the link's key, and setup.sealed carries
  // every request, key, result, and Discord choice after it. The relay reads
  // none of it. setup.done is the room's own end marker.
  "setup.hello",
  "setup.sealed",
  "setup.done",
  "ping",
]);

/**
 * Which relayed messages each side may send in a setup room. A setup room
 * carries no media; everything about the setup is sealed end to end.
 */
const SETUP_TYPES: Record<PeerRole, ReadonlySet<SignalType>> = {
  agent: new Set<SignalType>(["setup.sealed", "setup.done", "call.cancel", "call.error"]),
  browser: new Set<SignalType>(["setup.hello", "setup.sealed", "call.cancel"]),
};

/** Whether a room of this kind relays this message from this side. */
export function relayedInRoom(
  kind: "call" | "setup",
  role: PeerRole,
  type: SignalType,
): boolean {
  if (kind === "setup") return SETUP_TYPES[role].has(type);
  return RELAYED_TYPES.has(type) && !type.startsWith("setup.");
}

/** Message types a host may expect in a room of this kind. */
export function signalTypesForKind(kind: "call" | "setup"): SignalType[] {
  return kind === "setup"
    ? ["call.auth", "setup.hello", "setup.sealed", "setup.done", "call.cancel", "call.error", "ping", "ticket.redeemed"]
    : [...RELAYED_TYPES].filter((type) => !type.startsWith("setup.")).concat("ticket.redeemed");
}

export function parsePeerRole(value: string | null): PeerRole | null {
  return value === "agent" || value === "browser" ? value : null;
}

export type SignalParse =
  | { kind: "known"; envelope: SignalEnvelope }
  | { kind: "unknown" }
  | { kind: "invalid" };

function isRecord(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === "object" && !Array.isArray(value);
}

export function parseSignal(raw: string): SignalParse {
  if (raw.length > 64 * 1024) return { kind: "invalid" };
  let value: unknown;
  try {
    value = JSON.parse(raw);
  } catch {
    return { kind: "invalid" };
  }
  if (!isRecord(value)) return { kind: "invalid" };
  if (value.v !== SIGNAL_VERSION) return { kind: "invalid" };
  if (typeof value.id !== "string" || !value.id || value.id.length > 128) {
    return { kind: "invalid" };
  }
  if (typeof value.type !== "string" || !value.type) return { kind: "invalid" };
  if (!isRecord(value.payload)) return { kind: "invalid" };
  if (!RELAYED_TYPES.has(value.type as SignalType)) return { kind: "unknown" };
  return {
    kind: "known",
    envelope: {
      v: SIGNAL_VERSION,
      id: value.id,
      type: value.type as SignalType,
      payload: value.payload,
    },
  };
}

export function parseSignalEnvelope(raw: string): SignalEnvelope | null {
  const parsed = parseSignal(raw);
  return parsed.kind === "known" ? parsed.envelope : null;
}

/**
 * Terminal message kinds: receiving one ends the call (r2-browser-state-machine).
 */
export const TERMINAL_SIGNAL_TYPES: ReadonlySet<SignalType> = new Set<SignalType>([
  "call.hangup",
  "call.cancel",
  "setup.done",
]);

/**
 * State for the signal-ordering/duplicate guards (r2-browser-state-machine).
 * Pure and serializable so the worker keeps no extra runtime state beyond
 * these two fields.
 */
export interface SignalGuardState {
  /** Envelope ids of terminal messages already handled. */
  terminalHandled: ReadonlySet<string>;
  /** Whether a webrtc.offer has been relayed (answers before offers are reordered). */
  offerSeen: boolean;
}

export function initialSignalGuardState(): SignalGuardState {
  return { terminalHandled: new Set<string>(), offerSeen: false };
}

/**
 * Decide what the worker should do with an authenticated signal envelope.
 * Returns either ``relay`` (forward to the peer; update guard state via the
 * mutators on the result) or ``reject`` with a server envelope to send back.
 * Duplicate terminal ids are no-ops; answers before any offer are rejected
 * as reordered.
 */
export function guardSignal(
  envelope: SignalEnvelope,
  guard: SignalGuardState,
): { action: "relay" } | { action: "reject"; error: ReturnType<typeof serverEnvelope> } {
  if (TERMINAL_SIGNAL_TYPES.has(envelope.type)) {
    if (guard.terminalHandled.has(envelope.id)) {
      return { action: "reject", error: serverEnvelope("call.error", {
        code: "duplicate_terminal",
        detail: "Terminal message already handled.",
      }) };
    }
    return { action: "relay" };
  }
  if (envelope.type === "webrtc.answer" && !guard.offerSeen) {
    return { action: "reject", error: serverEnvelope("call.error", {
      code: "answer_before_offer",
      detail: "Answer received before any offer; retry after the offer.",
    }) };
  }
  return { action: "relay" };
}

/** Apply the guard mutations for a relayed envelope. */
export function applyGuardRelay(
  envelope: SignalEnvelope,
  guard: SignalGuardState,
): SignalGuardState {
  if (TERMINAL_SIGNAL_TYPES.has(envelope.type)) {
    guard.terminalHandled = new Set(guard.terminalHandled).add(envelope.id);
  }
  if (envelope.type === "webrtc.offer") {
    guard.offerSeen = true;
  }
  return guard;
}

export function serverEnvelope(
  type: Extract<SignalType, "peer.ready" | "ticket.redeemed" | "call.ended" | "call.error" | "pong">,
  payload: Record<string, unknown> = {},
): SignalEnvelope {
  return {
    v: SIGNAL_VERSION,
    id: crypto.randomUUID(),
    type,
    payload,
  };
}
