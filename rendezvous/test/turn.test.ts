import { describe, expect, it, vi } from "vitest";

import {
  STUN_ICE_SERVERS,
  TurnCredentialError,
  generateIceServers,
  parseIceServers,
  revokeTurnCredential,
  turnUsernames,
} from "../src/turn";

const CLOUDFLARE_RESPONSE = {
  iceServers: [
    { urls: ["stun:stun.cloudflare.com:3478", "stun:stun.cloudflare.com:53"] },
    {
      urls: [
        "turn:turn.cloudflare.com:3478?transport=udp",
        "turn:turn.cloudflare.com:53?transport=udp",
        "turns:turn.cloudflare.com:443?transport=tcp",
      ],
      username: "short-lived-user",
      credential: "short-lived-secret",
    },
  ],
};

describe("TURN credential generation", () => {
  it("uses STUN only when TURN secrets are intentionally absent", async () => {
    await expect(generateIceServers({}, 2160)).resolves.toEqual(STUN_ICE_SERVERS);
  });

  it("fails closed when an embedded browser requires relay", async () => {
    await expect(generateIceServers({}, 2160, undefined, "required")).rejects.toThrow(
      TurnCredentialError,
    );
  });

  it("rejects a partial secret configuration", async () => {
    await expect(generateIceServers({ TURN_KEY_ID: "key-id" }, 2160)).rejects.toThrow(
      TurnCredentialError,
    );
  });

  it("generates short-lived credentials without returning the long-term token", async () => {
    const fetchMock = vi.fn(async () =>
      Response.json(CLOUDFLARE_RESPONSE, { status: 201 }),
    );

    const servers = await generateIceServers(
      { TURN_KEY_ID: "key-id", TURN_KEY_API_TOKEN: "long-term-token" },
      2160,
      fetchMock,
    );

    expect(fetchMock).toHaveBeenCalledWith(
      "https://rtc.live.cloudflare.com/v1/turn/keys/key-id/credentials/generate-ice-servers",
      expect.objectContaining({
        method: "POST",
        headers: expect.objectContaining({ Authorization: "Bearer long-term-token" }),
        body: JSON.stringify({ ttl: 2160 }),
      }),
    );
    expect(JSON.stringify(servers)).not.toContain("long-term-token");
    expect(servers[1]).toMatchObject({
      username: "short-lived-user",
      credential: "short-lived-secret",
    });
    expect(servers.flatMap((server) => server.urls)).not.toContain(
      "turn:turn.cloudflare.com:53?transport=udp",
    );
  });

  it("falls back to STUN when the TURN service hangs", async () => {
    const fetchMock = vi.fn(
      (_url: unknown, init?: RequestInit) =>
        new Promise<Response>((_resolve, reject) => {
          init?.signal?.addEventListener("abort", () =>
            reject(new DOMException("The operation timed out.", "TimeoutError")),
          );
        }),
    );

    await expect(
      generateIceServers(
        { TURN_KEY_ID: "key-id", TURN_KEY_API_TOKEN: "token" },
        2160,
        fetchMock,
      ),
    ).resolves.toEqual(STUN_ICE_SERVERS);
  }, 10_000);

  it("rejects malformed relay credentials", () => {
    expect(
      parseIceServers({
        iceServers: [{ urls: "turn:turn.cloudflare.com:3478?transport=udp" }],
      }),
    ).toEqual([]);
  });
});

// -- ADR 0005 degradation matrix (r2-browser-connectivity) -------------------

describe("TURN outage behavior (ADR 0005)", () => {
  it("degrades to STUN on 429 provider pressure", async () => {
    const fetchMock = vi.fn(async () => new Response("rate limited", { status: 429 }));
    await expect(
      generateIceServers({ TURN_KEY_ID: "k", TURN_KEY_API_TOKEN: "t" }, 2160, fetchMock),
    ).resolves.toEqual(STUN_ICE_SERVERS);
  });

  it("fails closed on provider pressure when relay is required", async () => {
    const fetchMock = vi.fn(async () => new Response("rate limited", { status: 429 }));
    await expect(
      generateIceServers(
        { TURN_KEY_ID: "k", TURN_KEY_API_TOKEN: "t" },
        2160,
        fetchMock,
        "required",
      ),
    ).rejects.toThrow(TurnCredentialError);
  });

  it("degrades to STUN on 5xx control-plane incidents", async () => {
    const fetchMock = vi.fn(async () => new Response("boom", { status: 503 }));
    await expect(
      generateIceServers({ TURN_KEY_ID: "k", TURN_KEY_API_TOKEN: "t" }, 2160, fetchMock),
    ).resolves.toEqual(STUN_ICE_SERVERS);
  });

  it("fails closed on authentication failure (401/403)", async () => {
    for (const status of [401, 403]) {
      const fetchMock = vi.fn(async () => new Response("denied", { status }));
      await expect(
        generateIceServers({ TURN_KEY_ID: "k", TURN_KEY_API_TOKEN: "bad" }, 2160, fetchMock),
      ).rejects.toThrow(TurnCredentialError);
    }
  });

  it("fails closed on malformed provider responses", async () => {
    const fetchMock = vi.fn(async () =>
      Response.json({ iceServers: [{ urls: ["stun:stun.cloudflare.com:3478"] }] }, { status: 201 }),
    );
    // no relay server in the response -> fail closed, never silently relay-less
    await expect(
      generateIceServers({ TURN_KEY_ID: "k", TURN_KEY_API_TOKEN: "t" }, 2160, fetchMock),
    ).rejects.toThrow(TurnCredentialError);
  });
});

describe("revoking TURN credentials", () => {
  it("revokes one username at Cloudflare and reports failures without throwing", async () => {
    const calls: string[] = [];
    const ok = async (url: RequestInfo | URL) => { calls.push(String(url)); return new Response(null, { status: 200 }); };
    expect(await revokeTurnCredential({ TURN_KEY_ID: "key", TURN_KEY_API_TOKEN: "token" }, "user/1", ok as typeof fetch)).toBe(true);
    expect(calls).toEqual(["https://rtc.live.cloudflare.com/v1/turn/keys/key/credentials/user%2F1/revoke"]);
    const failing = async () => new Response(null, { status: 500 });
    expect(await revokeTurnCredential({ TURN_KEY_ID: "key", TURN_KEY_API_TOKEN: "token" }, "u", failing as typeof fetch)).toBe(false);
    const broken = async () => { throw new Error("network"); };
    expect(await revokeTurnCredential({ TURN_KEY_ID: "key", TURN_KEY_API_TOKEN: "token" }, "u", broken as typeof fetch)).toBe(false);
    expect(await revokeTurnCredential({}, "u", ok as typeof fetch)).toBe(false);
  });

  it("finds the TURN usernames among ICE servers", () => {
    expect(turnUsernames([{ urls: ["stun:x"] }, { urls: ["turn:y"], username: "a", credential: "c" }, { urls: ["turns:z"], username: "a" }])).toEqual(["a"]);
  });
});
