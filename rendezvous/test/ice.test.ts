import { describe, expect, it } from "vitest";

import { DEFAULT_ICE_SERVERS, parseIceServers } from "../web/ice";

describe("browser ICE configuration", () => {
  it("keeps an explicit empty list for same-machine calls", () => {
    expect(parseIceServers([])).toEqual([]);
  });

  it("accepts bounded STUN and authenticated TURN servers", () => {
    expect(
      parseIceServers([
        { urls: ["stun:stun.cloudflare.com:3478"] },
        {
          urls: ["turns:turn.cloudflare.com:443?transport=tcp"],
          username: "ephemeral-user",
          credential: "ephemeral-secret",
        },
      ]),
    ).toHaveLength(2);
  });

  it("rejects unauthenticated TURN and unsafe URL schemes", () => {
    expect(
      parseIceServers([
        { urls: ["turn:turn.cloudflare.com:3478?transport=udp"] },
        { urls: ["https://attacker.example/ice"] },
      ]),
    ).toEqual(DEFAULT_ICE_SERVERS);
  });
});
