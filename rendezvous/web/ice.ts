export interface BrowserIceServer {
  urls: string[];
  username?: string;
  credential?: string;
}

export const DEFAULT_ICE_SERVERS: BrowserIceServer[] = [
  { urls: ["stun:stun.cloudflare.com:3478"] },
];

export function parseIceServers(value: unknown): BrowserIceServer[] {
  if (!Array.isArray(value)) return DEFAULT_ICE_SERVERS;
  // An explicit empty list is the package-owned same-machine path. Host ICE
  // candidates are sufficient there and must not contact a public STUN server.
  if (value.length === 0) return [];
  const parsed: BrowserIceServer[] = [];
  for (const entry of value.slice(0, 8)) {
    if (typeof entry !== "object" || entry === null) continue;
    const candidate = entry as Record<string, unknown>;
    const rawUrls = Array.isArray(candidate.urls)
      ? candidate.urls
      : typeof candidate.urls === "string"
        ? [candidate.urls]
        : [];
    const urls = rawUrls
      .filter((url): url is string => typeof url === "string")
      .map((url) => url.trim())
      .filter((url) => /^(?:stun|stuns|turn|turns):[^\s]+$/i.test(url) && url.length <= 512)
      .slice(0, 8);
    if (!urls.length) continue;
    const usesTurn = urls.some((url) => url.toLowerCase().startsWith("turn"));
    const username = typeof candidate.username === "string" ? candidate.username.trim() : "";
    const credential =
      typeof candidate.credential === "string" ? candidate.credential.trim() : "";
    if (usesTurn && (!username || !credential)) continue;
    parsed.push({
      urls,
      ...(username ? { username } : {}),
      ...(credential ? { credential } : {}),
    });
  }
  return parsed.length ? parsed : DEFAULT_ICE_SERVERS;
}
