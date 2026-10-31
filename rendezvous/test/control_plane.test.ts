import { describe, expect, it } from "vitest";

import {
  normalizedNetworkPrefix,
  parseGrantAuthorization,
  sourceFingerprint,
  validInstallationId,
  validRequestId,
  validSecretHash,
} from "../src/access_protocol";
import { parseHostedServiceConfig, type ServiceEnvironment } from "../src/service_config";

function staging(overrides: Partial<ServiceEnvironment> = {}): ServiceEnvironment {
  return {
    DEPLOYMENT_ENV: "staging",
    PUBLIC_ORIGIN: "https://call-staging.sleep.talk",
    ENROLLMENT_HASH_KEY: "enrollment-secret",
    ADMIN_TOKEN: "admin-secret",
    INITIAL_SERVICE_MODE: "running",
    ALLOW_STUN_ONLY: "true",
    ENROLLMENT_PER_PREFIX_PER_DAY: "3",
    ENROLLMENT_GLOBAL_PER_DAY: "100",
    MINTS_PER_MINUTE: "6",
    MINTS_PER_DAY: "30",
    CONCURRENT_CALLS: "2",
    RELAY_ATTEMPTS_PER_DAY: "400",
    RELAY_ATTEMPTS_PER_INSTALLATION_PER_DAY: "40",
    RELAY_ATTEMPTS_PER_SOURCE_PER_DAY: "80",
    ...overrides,
  };
}

describe("hosted service configuration", () => {
  it("accepts an explicit STUN-only staging deployment", () => {
    const result = parseHostedServiceConfig(staging());
    expect(result).toMatchObject({
      ok: true,
      config: {
        environment: "staging",
        publicOrigin: "https://call-staging.sleep.talk",
        allowStunOnly: true,
      },
    });
  });

  it("fails production closed without complete TURN credentials and positive limits", () => {
    const result = parseHostedServiceConfig(
      staging({
        DEPLOYMENT_ENV: "production",
        PUBLIC_ORIGIN: "https://call.sleep.talk",
        INITIAL_SERVICE_MODE: "halted",
        ALLOW_STUN_ONLY: "false",
        CONCURRENT_CALLS: "0",
      }),
    );
    expect(result.ok).toBe(false);
    if (result.ok) throw new Error("expected invalid production configuration");
    expect(result.missing).toEqual(
      expect.arrayContaining(["TURN_KEY_ID", "TURN_KEY_API_TOKEN"]),
    );
    expect(result.invalid).toContain("CONCURRENT_CALLS");
  });

  it("rejects a public origin with a path or non-HTTPS scheme", () => {
    expect(parseHostedServiceConfig(staging({ PUBLIC_ORIGIN: "http://example.test" })).ok).toBe(
      false,
    );
    expect(
      parseHostedServiceConfig(staging({ PUBLIC_ORIGIN: "https://example.test/path" })).ok,
    ).toBe(false);
  });
});

describe("grant and enrollment boundaries", () => {
  it("parses only the versioned grant authorization scheme", () => {
    const installationId = "i".repeat(24);
    const secret = "s".repeat(43);
    expect(
      parseGrantAuthorization(`SleeptalkGrant v1.${installationId}.${secret}`),
    ).toEqual({ installationId, secret });
    expect(parseGrantAuthorization(`Bearer ${installationId}.${secret}`)).toBeNull();
    expect(parseGrantAuthorization(null)).toBeNull();
  });

  it("validates opaque identifiers and verifier hashes", () => {
    expect(validInstallationId("i".repeat(24))).toBe(true);
    expect(validInstallationId("short")).toBe(false);
    expect(validRequestId("r".repeat(18))).toBe(true);
    expect(validRequestId("spaces are invalid")).toBe(false);
    expect(validSecretHash("a".repeat(64))).toBe(true);
    expect(validSecretHash("A".repeat(64))).toBe(false);
  });

  it("normalizes IPv4 and IPv6 networks before hashing", async () => {
    expect(normalizedNetworkPrefix("001.002.003.004")).toBe("1.2.3.0/24");
    // One /24 is one source: walking the last octet does not add sources.
    expect(normalizedNetworkPrefix("1.2.3.250")).toBe("1.2.3.0/24");
    expect(normalizedNetworkPrefix("1.2.4.4")).toBe("1.2.4.0/24");
    expect(normalizedNetworkPrefix("2001:db8::1")).toBe("v6:2001:0db8:0000::/48");
    expect(normalizedNetworkPrefix("2001:db8:0:ffff::1")).toBe("v6:2001:0db8:0000::/48");
    expect(normalizedNetworkPrefix("not-an-ip")).toBeNull();
    const first = await sourceFingerprint("2001:db8::1", "deployment-secret");
    const second = await sourceFingerprint(
      "2001:0db8:0000:0000:ffff::2",
      "deployment-secret",
    );
    expect(first).toBe(second);
    expect(first).not.toContain("2001");
  });
});
