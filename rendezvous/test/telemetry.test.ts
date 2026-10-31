import { describe, expect, it } from "vitest";

import { summarizeRtcStats } from "../web/telemetry";

describe("WebRTC transport telemetry", () => {
  it("identifies a TURN relay and summarizes audio health", () => {
    const result = summarizeRtcStats([
      { id: "transport", type: "transport", selectedCandidatePairId: "pair" },
      {
        id: "pair",
        type: "candidate-pair",
        state: "succeeded",
        nominated: true,
        localCandidateId: "local",
        remoteCandidateId: "remote",
        currentRoundTripTime: 0.084,
        availableOutgoingBitrate: 128_500,
      },
      {
        id: "local",
        type: "local-candidate",
        candidateType: "relay",
        protocol: "tcp",
        relayProtocol: "tls",
      },
      { id: "remote", type: "remote-candidate", candidateType: "srflx" },
      {
        id: "in",
        type: "inbound-rtp",
        kind: "audio",
        jitter: 0.012,
        jitterBufferDelay: 1.5,
        jitterBufferEmittedCount: 100,
        packetsLost: 3,
        packetsReceived: 400,
        bytesReceived: 32_000,
        concealedSamples: 960,
      },
      {
        id: "out",
        type: "outbound-rtp",
        kind: "audio",
        packetsSent: 420,
        bytesSent: 36_000,
      },
    ]);

    expect(result).toMatchObject({
      usesRelay: true,
      localCandidateType: "relay",
      protocol: "tcp",
      relayProtocol: "tls",
      currentRttMs: 84,
      availableOutgoingBitrateKbps: 128.5,
      inboundAudioJitterMs: 12,
      jitterBufferMs: 15,
      packetsLost: 3,
      packetsReceived: 400,
      packetsSent: 420,
    });
  });

  it("reports a direct candidate path without inventing absent metrics", () => {
    expect(
      summarizeRtcStats([
        {
          id: "pair",
          type: "candidate-pair",
          state: "succeeded",
          selected: true,
          localCandidateId: "local",
          remoteCandidateId: "remote",
        },
        { id: "local", type: "local-candidate", candidateType: "host" },
        { id: "remote", type: "remote-candidate", candidateType: "srflx" },
      ]),
    ).toMatchObject({ usesRelay: false, localCandidateType: "host" });
  });
});

// -- r2-browser-ice-diagnostics: failure categories + path visibility -------

import { categorizeFailure, correlationKey, pathType } from "../web/failure_categories";

describe("failure categorization (r2-browser-ice-diagnostics)", () => {
  it("maps raw errors to stable categories", () => {
    expect(categorizeFailure("ice_failed")).toBe("ice_failed");
    expect(categorizeFailure("ICE connection failed")).toBe("ice_failed");
    expect(categorizeFailure("AbortError")).toBe("signaling_unreachable");
    expect(categorizeFailure("fetch failed")).toBe("signaling_unreachable");
    expect(categorizeFailure("NotAllowedError")).toBe("permission_denied");
    expect(categorizeFailure("microphone blocked")).toBe("permission_denied");
    expect(categorizeFailure("signal closed")).toBe("signaling_unreachable");
    expect(categorizeFailure("peer_left")).toBe("peer_disconnected");
    expect(categorizeFailure("connection disconnected")).toBe("peer_disconnected");
    expect(categorizeFailure("")).toBe("unknown");
    expect(categorizeFailure("mystery")).toBe("unknown");
  });

  it("selected relay/direct path is visible to operators", () => {
    expect(pathType({ usesRelay: true })).toBe("relay");
    expect(pathType({ usesRelay: false })).toBe("direct");
    expect(pathType({ localCandidateType: "relay", remoteCandidateType: "prflx" })).toBe("relay");
    expect(pathType({ localCandidateType: "srflx", remoteCandidateType: "host" })).toBe("direct");
    expect(pathType({})).toBeUndefined();
  });

  it("correlation key is redacted (no tokens)", () => {
    const key = correlationKey("routing-123", 1234.6);
    expect(key).toBe("routing-123:1235");
    expect(key).not.toContain("token");
  });
});

describe("transport telemetry path visibility (r2-browser-ice-diagnostics)", () => {
  function reports(withRelay: boolean) {
    const localType = withRelay ? "relay" : "srflx";
    return [
      { id: "t1", type: "transport", selectedCandidatePairId: "p1" },
      {
        id: "p1",
        type: "candidate-pair",
        state: "succeeded",
        selected: true,
        localCandidateId: "l1",
        remoteCandidateId: "r1",
        currentRoundTripTime: 0.05,
      },
      { id: "l1", type: "local-candidate", candidateType: localType, protocol: "udp" },
      { id: "r1", type: "remote-candidate", candidateType: "host" },
    ];
  }

  it("relay path is classified", () => {
    const summary = summarizeRtcStats(reports(true));
    expect(summary.pathType).toBe("relay");
    expect(summary.usesRelay).toBe(true);
  });

  it("direct path is classified", () => {
    const summary = summarizeRtcStats(reports(false));
    expect(summary.pathType).toBe("direct");
    expect(summary.usesRelay).toBe(false);
  });

  it("no raw SDP, candidate address, audio, or transcript is retained", () => {
    const summary = summarizeRtcStats(reports(true));
    const blob = JSON.stringify(summary);
    // candidate addresses (IPs) must never appear
    expect(blob).not.toMatch(/\b\d{1,3}(\.\d{1,3}){3}\b/);
    // no SDP or transcript material
    expect(blob.toLowerCase()).not.toContain("sdp");
    expect(blob.toLowerCase()).not.toContain("candidate:"); // SDP candidate line
    expect(blob.toLowerCase()).not.toContain("transcript");
    expect(blob.toLowerCase()).not.toContain("audio"); // only aggregate stats fields
    // whitelist shape: every key is an aggregate metric or classification
    const allowed = new Set([
      "pathType",
      "usesRelay",
      "candidatePairId",
      "localCandidateType",
      "remoteCandidateType",
      "protocol",
      "relayProtocol",
      "currentRttMs",
      "availableOutgoingBitrateKbps",
      "inboundAudioJitterMs",
      "jitterBufferMs",
      "packetsLost",
      "packetsReceived",
      "packetsSent",
      "bytesReceived",
      "bytesSent",
      "concealedSamples",
    ]);
    for (const key of Object.keys(summary)) {
      if (!allowed.has(key)) {
        throw new Error(`unexpected telemetry key: ${key}`);
      }
    }
  });
});
