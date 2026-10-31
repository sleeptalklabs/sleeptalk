import { describe, expect, it } from "vitest";

import { parseCallRoute, parseSettingsRoute } from "../web/routes";
import { b64url, unb64url } from "../web/setup_crypto";

const HOST_KEY = b64url(new Uint8Array([4, ...new Array(64).fill(7)]));

describe("phone settings route", () => {
  it("parses routing id, token and host key from the fragment", () => {
    const hash = `#/settings/${"r".repeat(24)}/${"t".repeat(43)}/${HOST_KEY}`;
    expect(parseSettingsRoute(hash)).toEqual({ routingId: "r".repeat(24), token: "t".repeat(43), hostKey: HOST_KEY });
    expect(parseCallRoute(hash)).toBeNull();
    expect(parseSettingsRoute(`#/settings/${"r".repeat(24)}/${"t".repeat(43)}`)).toBeNull();
    expect(parseSettingsRoute(`#/call/${"r".repeat(24)}/${"t".repeat(43)}/${HOST_KEY}`)).toBeNull();
  });
});

import { Aes128Gcm, CipherSuite, DhkemP256HkdfSha256, HkdfSha256 } from "@hpke/core";
import { SetupChannel, channelWithKeyForTest } from "../web/setup_crypto";
import channelVector from "../../tests/fixtures/setup_channel_vector.json?raw";

describe("sealed setup channel", () => {
  it("opens a message the Python host sealed, once", async () => {
    const vector = JSON.parse(channelVector);
    const channel = await channelWithKeyForTest(unb64url(vector.key), vector.routing_id);
    expect(await channel.open(vector.sealed)).toEqual({ type: vector.type, payload: vector.payload });
    expect(await channel.open(vector.sealed)).toBeNull(); // a replay
  });

  it("refuses a message bound to another room or tampered with", async () => {
    const vector = JSON.parse(channelVector);
    const other = await channelWithKeyForTest(unb64url(vector.key), "another-room-1");
    expect(await other.open(vector.sealed)).toBeNull();
    const channel = await channelWithKeyForTest(unb64url(vector.key), vector.routing_id);
    expect(await channel.open({ ...vector.sealed, seq: vector.sealed.seq + 1 })).toBeNull();
  });

  it("hands the host its key sealed to the link key, with the host's binding", async () => {
    const suite = new CipherSuite({ kem: new DhkemP256HkdfSha256(), kdf: new HkdfSha256(), aead: new Aes128Gcm() });
    const host = await suite.kem.generateKeyPair();
    const hostPublic = new Uint8Array(await suite.kem.serializePublicKey(host.publicKey));
    const { channel, hello } = await SetupChannel.open(hostPublic, "r1234567routing");
    const recipient = await suite.createRecipientContext({
      recipientKey: host.privateKey,
      enc: unb64url(hello.enc).slice().buffer,
      info: new TextEncoder().encode('sleeptalk/setup-channel/v1\0{"routing_id":"r1234567routing"}').slice().buffer,
    });
    const { key } = JSON.parse(new TextDecoder().decode(await recipient.open(unb64url(hello.ct).slice().buffer)));
    // The host, holding that key, reads what the page seals.
    const sealed = await channel.seal("settings.call", { id: 1, method: "GET", path: "/api/state" });
    const aes = await crypto.subtle.importKey("raw", unb64url(key).slice(), "AES-GCM", false, ["decrypt"]);
    const plain = await crypto.subtle.decrypt({
      name: "AES-GCM", iv: unb64url(sealed.iv).slice().buffer,
      additionalData: new TextEncoder().encode('sleeptalk/setup-channel/v1\0{"direction":"page","routing_id":"r1234567routing","seq":1}').slice().buffer,
    }, aes, unb64url(sealed.ct).slice().buffer);
    expect(JSON.parse(new TextDecoder().decode(plain))).toEqual({ type: "settings.call", payload: { id: 1, method: "GET", path: "/api/state" } });
  });
});
