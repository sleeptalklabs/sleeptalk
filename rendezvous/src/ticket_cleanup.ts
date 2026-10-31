import {
  reconnectCount,
  ticketKind,
  type TicketKind,
  type TicketRecord,
  type TicketTombstone,
} from "./ticket";

export interface TicketCleanupTransaction {
  get<T>(key: string): Promise<T | undefined>;
  list(): Promise<Map<string, unknown>>;
  delete(keys: string[]): Promise<number>;
  put<T>(key: string, value: T): Promise<void>;
  setAlarm(scheduledTime: number): Promise<void>;
}

export interface CallSummary {
  event: "call_summary";
  kind: TicketKind;
  durationMs: number;
  transport: "relay" | "direct" | "unknown";
  reconnectCount: number;
}

export async function purgeTicketStorage(
  transaction: TicketCleanupTransaction,
  options: { now: number; tombstoneTtlMilliseconds: number },
): Promise<{ summary: CallSummary; tombstone: TicketTombstone } | null> {
  const { now, tombstoneTtlMilliseconds } = options;
  const current = await transaction.get<TicketRecord | TicketTombstone>("ticket");
  if (!isTicketRecord(current) || current.state === "ended") return null;
  const keys = [...(await transaction.list()).keys()];
  if (keys.length) await transaction.delete(keys);
  const tombstone: TicketTombstone = {
    state: current.redeemedAt === null ? "ended" : "used",
  };
  await transaction.put("ticket", tombstone);
  await transaction.setAlarm(now + tombstoneTtlMilliseconds);
  return {
    tombstone,
    summary: {
      event: "call_summary",
      kind: ticketKind(current),
      durationMs:
        current.redeemedAt === null ? 0 : Math.max(now - current.redeemedAt, 0),
      transport:
        current.usesRelay === true
          ? "relay"
          : current.usesRelay === false
            ? "direct"
            : "unknown",
      reconnectCount: reconnectCount(current),
    },
  };
}

export function isTicketRecord(
  stored: TicketRecord | TicketTombstone | undefined,
): stored is TicketRecord {
  return stored !== undefined && "v" in stored && (stored.v === 1 || stored.v === 2);
}
