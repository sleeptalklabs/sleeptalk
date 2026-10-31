export function parseGrantAuthorization(
  authorization: string | null,
): { installationId: string; secret: string } | null {
  const match = authorization?.match(
    /^SleeptalkGrant\s+v1\.([A-Za-z0-9_-]{20,64})\.([A-Za-z0-9_-]{32,128})$/,
  );
  return match ? { installationId: match[1], secret: match[2] } : null;
}

export function validInstallationId(value: unknown): value is string {
  return typeof value === "string" && /^[A-Za-z0-9_-]{20,64}$/.test(value);
}

export function validSecretHash(value: unknown): value is string {
  return typeof value === "string" && /^[a-f0-9]{64}$/.test(value);
}

export function validRequestId(value: string | null): value is string {
  return value !== null && /^[A-Za-z0-9_-]{16,128}$/.test(value);
}

export async function sourceFingerprint(ip: string, key: string): Promise<string | null> {
  const prefix = normalizedNetworkPrefix(ip);
  if (!prefix) return null;
  const cryptoKey = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(key),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const digest = await crypto.subtle.sign(
    "HMAC",
    cryptoKey,
    new TextEncoder().encode(prefix),
  );
  return bytesToHex(new Uint8Array(digest));
}

/**
 * The network a request comes from, for per-source limits: an IPv4 /24 or
 * an IPv6 /48. One host or one small provider allocation is one source, so
 * a single machine cannot multiply itself by walking its own addresses.
 */
export function normalizedNetworkPrefix(ip: string): string | null {
  const ipv4 = ip.split(".");
  if (
    ipv4.length === 4 &&
    ipv4.every((part) => /^\d{1,3}$/.test(part) && Number(part) <= 255)
  ) {
    return `${ipv4.slice(0, 3).map((part) => String(Number(part))).join(".")}.0/24`;
  }
  const value = ip.toLowerCase().split("%", 1)[0];
  if (!/^[0-9a-f:]+$/.test(value) || !value.includes(":")) return null;
  const halves = value.split("::");
  if (halves.length > 2) return null;
  const left = halves[0] ? halves[0].split(":") : [];
  const right = halves[1] ? halves[1].split(":") : [];
  if ([...left, ...right].some((part) => !/^[0-9a-f]{1,4}$/.test(part))) return null;
  const missing = 8 - left.length - right.length;
  if ((halves.length === 1 && missing !== 0) || (halves.length === 2 && missing < 1)) {
    return null;
  }
  const groups = [...left, ...Array(missing).fill("0"), ...right].map((part) =>
    part.padStart(4, "0"),
  );
  return `v6:${groups.slice(0, 3).join(":")}::/48`;
}

export async function sha256(value: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value));
  return bytesToHex(new Uint8Array(digest));
}

export function constantTimeEqual(left: string, right: string): boolean {
  const length = Math.max(left.length, right.length);
  let difference = left.length ^ right.length;
  for (let index = 0; index < length; index += 1) {
    difference |= (left.charCodeAt(index) || 0) ^ (right.charCodeAt(index) || 0);
  }
  return difference === 0;
}

function bytesToHex(bytes: Uint8Array): string {
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
}
