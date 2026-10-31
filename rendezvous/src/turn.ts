export interface IceServerConfig {
  urls: string[];
  username?: string;
  credential?: string;
}

export interface TurnEnvironment {
  TURN_KEY_ID?: string;
  TURN_KEY_API_TOKEN?: string;
}

export const STUN_ICE_SERVERS: IceServerConfig[] = [
  { urls: ["stun:stun.cloudflare.com:3478"] },
];

export class TurnCredentialError extends Error {}

export type TurnRequirement = "preferred" | "required";

// The credential request must never stall ticket mint/redeem. Bound it and
// degrade to STUN-only if the TURN service cannot answer in time.
const TURN_REQUEST_TIMEOUT_MS = 5_000;

export async function generateIceServers(
  env: TurnEnvironment,
  ttlSeconds: number,
  fetchImpl: typeof fetch = fetch,
  requirement: TurnRequirement = "preferred",
): Promise<IceServerConfig[]> {
  const keyId = env.TURN_KEY_ID?.trim() ?? "";
  const apiToken = env.TURN_KEY_API_TOKEN?.trim() ?? "";
  if (!keyId && !apiToken) {
    if (requirement === "required") {
      throw new TurnCredentialError("This browser requires the TURN relay.");
    }
    return STUN_ICE_SERVERS;
  }
  if (!keyId || !apiToken) {
    throw new TurnCredentialError("The TURN service secrets are incomplete.");
  }

  let response: Response;
  try {
    response = await fetchImpl(
      `https://rtc.live.cloudflare.com/v1/turn/keys/${encodeURIComponent(keyId)}` +
        "/credentials/generate-ice-servers",
      {
        method: "POST",
        headers: {
          Authorization: `Bearer ${apiToken}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ ttl: ttlSeconds }),
        signal: AbortSignal.timeout(TURN_REQUEST_TIMEOUT_MS),
      },
    );
  } catch (error) {
    if (error instanceof Error && error.name === "TimeoutError") {
      if (requirement === "required") {
        throw new TurnCredentialError("The TURN service timed out.");
      }
      console.warn("TURN credential request timed out; falling back to STUN only.");
      return STUN_ICE_SERVERS;
    }
    throw new TurnCredentialError("The TURN service could not be reached.");
  }
  if (!response.ok) {
    // ADR 0005 degradation matrix: transient provider pressure (429, 5xx)
    // degrades to STUN-only with a warning so direct-capable calls proceed
    // during a Cloudflare control-plane incident; authentication failures
    // and other 4xx fail closed (broken configuration must be loud).
    if (response.status === 429 || response.status >= 500) {
      if (requirement === "required") {
        throw new TurnCredentialError(
          `The TURN service is unavailable (HTTP ${response.status}).`,
        );
      }
      console.warn(
        `TURN service pressure (HTTP ${response.status}); falling back to STUN only.`,
      );
      return STUN_ICE_SERVERS;
    }
    throw new TurnCredentialError(
      `The TURN service rejected credentials (HTTP ${response.status}).`,
    );
  }

  let value: unknown;
  try {
    value = await response.json();
  } catch {
    throw new TurnCredentialError("The TURN service returned unreadable credentials.");
  }
  const iceServers = parseIceServers(value);
  if (!iceServers.some((server) => server.urls.some((url) => url.startsWith("turn")))) {
    throw new TurnCredentialError("The TURN service returned no relay server.");
  }
  return iceServers;
}

export function parseIceServers(value: unknown): IceServerConfig[] {
  if (!isObject(value) || !Array.isArray(value.iceServers)) return [];
  const parsed: IceServerConfig[] = [];
  for (const entry of value.iceServers.slice(0, 8)) {
    if (!isObject(entry)) continue;
    const rawUrls = Array.isArray(entry.urls)
      ? entry.urls
      : typeof entry.urls === "string"
        ? [entry.urls]
        : [];
    const urls = rawUrls
      .filter((url): url is string => typeof url === "string")
      .map((url) => url.trim())
      .filter(isIceUrl)
      // Port 53 is commonly blocked by browsers and delays non-trickle clients.
      .filter((url) => !/:53(?:\?|$)/.test(url))
      .slice(0, 8);
    if (!urls.length) continue;
    const usesTurn = urls.some((url) => url.toLowerCase().startsWith("turn"));
    const username = typeof entry.username === "string" ? entry.username.trim() : "";
    const credential = typeof entry.credential === "string" ? entry.credential.trim() : "";
    if (usesTurn && (!username || !credential)) continue;
    parsed.push({
      urls,
      ...(username ? { username } : {}),
      ...(credential ? { credential } : {}),
    });
  }
  return parsed;
}

function isIceUrl(value: string): boolean {
  return value.length <= 512 && /^(?:stun|stuns|turn|turns):[^\s]+$/i.test(value);
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

/**
 * Revoke one issued TURN credential, so it stops working when its call ends
 * instead of living out its TTL. Best effort: a failure is logged by status
 * only, and the credential still expires on its own.
 */
export async function revokeTurnCredential(
  env: TurnEnvironment,
  username: string,
  fetchImpl: typeof fetch = fetch,
): Promise<boolean> {
  const keyId = env.TURN_KEY_ID?.trim() ?? "";
  const apiToken = env.TURN_KEY_API_TOKEN?.trim() ?? "";
  if (!keyId || !apiToken || !username) return false;
  try {
    const response = await fetchImpl(
      `https://rtc.live.cloudflare.com/v1/turn/keys/${encodeURIComponent(keyId)}` +
        `/credentials/${encodeURIComponent(username)}/revoke`,
      {
        method: "POST",
        headers: { Authorization: `Bearer ${apiToken}` },
        signal: AbortSignal.timeout(TURN_REQUEST_TIMEOUT_MS),
      },
    );
    if (!response.ok) console.warn(`TURN credential revoke failed (HTTP ${response.status}).`);
    return response.ok;
  } catch {
    console.warn("TURN credential revoke could not reach the service.");
    return false;
  }
}

/** The TURN usernames in a set of ICE servers (STUN entries carry none). */
export function turnUsernames(servers: IceServerConfig[]): string[] {
  return [...new Set(servers.map((server) => server.username ?? "").filter(Boolean))];
}
