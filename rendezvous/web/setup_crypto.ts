import { Aes128Gcm, CipherSuite, DhkemP256HkdfSha256, HkdfSha256 } from "@hpke/core";

// Must match sleeptalk/setup_link.py: DHKEM(P-256)/HKDF-SHA256/AES-128-GCM.
const IDENTIFIER = /^[A-Za-z0-9_-]{8,128}$/;

const suite = new CipherSuite({
  kem: new DhkemP256HkdfSha256(),
  kdf: new HkdfSha256(),
  aead: new Aes128Gcm(),
});
const utf8 = new TextEncoder();

export function b64url(bytes: ArrayBuffer | Uint8Array): string {
  let binary = "";
  for (const byte of new Uint8Array(bytes)) binary += String.fromCharCode(byte);
  return btoa(binary).replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/, "");
}

export function unb64url(value: string): Uint8Array {
  if (!/^[A-Za-z0-9_-]+$/.test(value)) throw new Error("invalid base64url");
  const padded = value.replaceAll("-", "+").replaceAll("_", "/") + "=".repeat((4 - (value.length % 4)) % 4);
  return Uint8Array.from(atob(padded), (character) => character.charCodeAt(0));
}

// The sealed setup channel. Must match SetupChannel in sleeptalk/setup_link.py.
const CHANNEL_INFO = "sleeptalk/setup-channel/v1\0";
const CHANNEL_TYPES = new Set(["settings.call", "settings.reply"]);

export interface SealedMessage {
  seq: number;
  iv: string;
  ct: string;
}

/**
 * Both directions of one setup room, under a key only this page and the host
 * hold. The page picks the key and seals it to the host's link key, which the
 * relay never sees, so the relay can neither read the channel nor open one.
 * Every message binds its direction, room, and sequence number.
 */
export class SetupChannel {
  private sent = 0;
  private received = 0;
  private queue: Promise<unknown> = Promise.resolve();

  private constructor(private readonly key: CryptoKey, private readonly routingId: string) {}

  /** A fresh channel and the setup.hello payload that hands its key to the host. */
  static async open(hostPublicKey: Uint8Array, routingId: string): Promise<{ channel: SetupChannel; hello: { enc: string; ct: string } }> {
    if (!IDENTIFIER.test(routingId)) throw new Error("invalid setup binding");
    if (hostPublicKey.length !== 65 || hostPublicKey[0] !== 0x04) throw new Error("invalid host key");
    const raw = crypto.getRandomValues(new Uint8Array(32));
    const recipientPublicKey = await suite.kem.deserializePublicKey(hostPublicKey.slice().buffer);
    const sender = await suite.createSenderContext({
      recipientPublicKey,
      info: utf8.encode(`${CHANNEL_INFO}{"routing_id":${JSON.stringify(routingId)}}`).slice().buffer,
    });
    const ct = await sender.seal(utf8.encode(JSON.stringify({ key: b64url(raw) })).slice().buffer);
    const key = await crypto.subtle.importKey("raw", raw, "AES-GCM", false, ["encrypt", "decrypt"]);
    raw.fill(0);
    return { channel: new SetupChannel(key, routingId), hello: { enc: b64url(sender.enc), ct: b64url(ct) } };
  }

  private static async fromKey(raw: Uint8Array, routingId: string): Promise<SetupChannel> {
    return new SetupChannel(await crypto.subtle.importKey("raw", raw.slice(), "AES-GCM", false, ["encrypt", "decrypt"]), routingId);
  }

  private aad(direction: "host" | "page", seq: number): ArrayBuffer {
    return utf8.encode(`${CHANNEL_INFO}{"direction":"${direction}","routing_id":${JSON.stringify(this.routingId)},"seq":${seq}}`).slice().buffer;
  }

  /** Seal one message; calls are serialized so sequence numbers go out in order. */
  seal(type: string, payload: object): Promise<SealedMessage> {
    const next = this.queue.then(async () => {
      const seq = ++this.sent;
      const iv = crypto.getRandomValues(new Uint8Array(12));
      const ct = await crypto.subtle.encrypt(
        { name: "AES-GCM", iv, additionalData: this.aad("page", seq) },
        this.key,
        utf8.encode(JSON.stringify({ type, payload })),
      );
      return { seq, iv: b64url(iv), ct: b64url(ct) };
    });
    this.queue = next.catch(() => undefined);
    return next;
  }

  /** Open one host message in arrival order; null for anything forged, replayed, or out of order. */
  open(payload: Record<string, unknown>): Promise<{ type: string; payload: Record<string, unknown> } | null> {
    const next = this.queue.then(async () => {
      const { seq, iv, ct } = payload;
      if (typeof seq !== "number" || !Number.isInteger(seq) || seq <= this.received || seq >= 2 ** 31) return null;
      if (typeof iv !== "string" || typeof ct !== "string") return null;
      try {
        const plain = await crypto.subtle.decrypt(
          { name: "AES-GCM", iv: unb64url(iv).slice().buffer, additionalData: this.aad("host", seq) },
          this.key,
          unb64url(ct).slice().buffer,
        );
        const inner = JSON.parse(new TextDecoder().decode(plain)) as { type?: unknown; payload?: unknown };
        if (typeof inner.type !== "string" || !CHANNEL_TYPES.has(inner.type)) return null;
        if (!inner.payload || typeof inner.payload !== "object" || Array.isArray(inner.payload)) return null;
        this.received = seq;
        return { type: inner.type, payload: inner.payload as Record<string, unknown> };
      } catch {
        return null;
      }
    });
    this.queue = next.catch(() => undefined);
    return next;
  }
}

/** A channel over a known key: for the cross-language test vector only. */
export async function channelWithKeyForTest(raw: Uint8Array, routingId: string): Promise<SetupChannel> {
  return (SetupChannel as unknown as { fromKey(raw: Uint8Array, routingId: string): Promise<SetupChannel> }).fromKey(raw, routingId);
}
