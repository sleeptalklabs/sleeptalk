// Stored on the hibernating socket, not in a process-global map or in the
// database. Rehydration keeps the allowance; unknown messages count too.
export interface SignalAllowance {
  startedAt: number;
  messages: number;
  bytes: number;
}

export const SIGNAL_WINDOW_MS = 60_000;
export const MAX_SIGNAL_MESSAGES = 240;
export const MAX_SIGNAL_BYTES = 1024 * 1024;
export const MAX_SIGNAL_MESSAGE_BYTES = 64 * 1024;

export function signalMessageBytes(message: string | ArrayBuffer): number {
  if (typeof message !== "string") return message.byteLength;
  // Reject huge strings without allocating a second equally large buffer.
  return message.length > MAX_SIGNAL_MESSAGE_BYTES
    ? MAX_SIGNAL_MESSAGE_BYTES + 1
    : new TextEncoder().encode(message).byteLength;
}

export function admitSignal(
  previous: SignalAllowance | undefined,
  bytes: number,
  now: number,
): SignalAllowance | null {
  if (bytes > MAX_SIGNAL_MESSAGE_BYTES) return null;
  const current = previous && now - previous.startedAt < SIGNAL_WINDOW_MS
    ? previous
    : { startedAt: now, messages: 0, bytes: 0 };
  if (current.messages >= MAX_SIGNAL_MESSAGES || current.bytes + bytes > MAX_SIGNAL_BYTES) return null;
  return { startedAt: current.startedAt, messages: current.messages + 1, bytes: current.bytes + bytes };
}
