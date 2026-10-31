/** The signaling socket must be this page's own origin, over the matching scheme. */
export function validBrowserSignalUrl(value: unknown): string | null {
  if (typeof value !== "string") return null;
  try {
    const signal = new URL(value);
    const expectedProtocol = location.protocol === "https:" ? "wss:" : "ws:";
    if (signal.protocol !== expectedProtocol || signal.host !== location.host) return null;
    return signal.toString();
  } catch {
    return null;
  }
}
