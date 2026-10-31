export interface RtcTransportTelemetry {
  /** Stable operator-visible path classification (r2-browser-ice-diagnostics). */
  pathType?: "relay" | "direct";
  usesRelay?: boolean;
  candidatePairId?: string;
  localCandidateType?: string;
  remoteCandidateType?: string;
  protocol?: string;
  relayProtocol?: string;
  currentRttMs?: number;
  availableOutgoingBitrateKbps?: number;
  inboundAudioJitterMs?: number;
  jitterBufferMs?: number;
  packetsLost?: number;
  packetsReceived?: number;
  packetsSent?: number;
  bytesReceived?: number;
  bytesSent?: number;
  concealedSamples?: number;
}

type StatsRecord = Record<string, unknown>;
interface StatsSource {
  getStats(): Promise<{ values(): Iterable<unknown> }>;
}

import { pathType } from "./failure_categories";

export function summarizeRtcStats(
  reports: Iterable<StatsRecord>,
): RtcTransportTelemetry {
  const entries = Array.from(reports);
  const byId = new Map(
    entries
      .filter((entry) => typeof entry.id === "string")
      .map((entry) => [String(entry.id), entry]),
  );
  const transport = entries.find((entry) => entry.type === "transport");
  const selectedId = stringValue(transport?.selectedCandidatePairId);
  const pair =
    (selectedId ? byId.get(selectedId) : undefined) ??
    entries.find(
      (entry) =>
        entry.type === "candidate-pair" &&
        entry.state === "succeeded" &&
        (entry.selected === true || entry.nominated === true),
    );
  const local = byId.get(stringValue(pair?.localCandidateId) ?? "");
  const remote = byId.get(stringValue(pair?.remoteCandidateId) ?? "");
  const inbound = entries.find(
    (entry) => entry.type === "inbound-rtp" && mediaKind(entry) === "audio",
  );
  const outbound = entries.find(
    (entry) => entry.type === "outbound-rtp" && mediaKind(entry) === "audio",
  );
  const remoteInbound = entries.find(
    (entry) => entry.type === "remote-inbound-rtp" && mediaKind(entry) === "audio",
  );

  const emitted = numberValue(inbound?.jitterBufferEmittedCount);
  const jitterDelaySeconds = numberValue(inbound?.jitterBufferDelay);
  const jitterBufferMs =
    emitted && jitterDelaySeconds !== undefined && emitted > 0
      ? (jitterDelaySeconds / emitted) * 1000
      : undefined;
  const pairRtt = numberValue(pair?.currentRoundTripTime);
  const remoteRtt = numberValue(remoteInbound?.roundTripTime);
  const localType = stringValue(local?.candidateType);
  const remoteType = stringValue(remote?.candidateType);

  const usesRelay =
    localType || remoteType ? localType === "relay" || remoteType === "relay" : undefined;
  return compact({
    pathType: pathType({ usesRelay, localCandidateType: localType, remoteCandidateType: remoteType }),
    usesRelay,
    candidatePairId: stringValue(pair?.id),
    localCandidateType: localType,
    remoteCandidateType: remoteType,
    protocol: stringValue(local?.protocol) ?? stringValue(remote?.protocol),
    relayProtocol:
      stringValue(local?.relayProtocol) ?? stringValue(remote?.relayProtocol),
    currentRttMs: secondsToMs(pairRtt ?? remoteRtt),
    availableOutgoingBitrateKbps: bitsToKbps(
      numberValue(pair?.availableOutgoingBitrate),
    ),
    inboundAudioJitterMs: secondsToMs(numberValue(inbound?.jitter)),
    jitterBufferMs: rounded(jitterBufferMs),
    packetsLost: integerValue(inbound?.packetsLost),
    packetsReceived: integerValue(inbound?.packetsReceived),
    packetsSent: integerValue(outbound?.packetsSent),
    bytesReceived: integerValue(inbound?.bytesReceived),
    bytesSent: integerValue(outbound?.bytesSent),
    concealedSamples: integerValue(inbound?.concealedSamples),
  });
}

export async function collectRtcStats(
  peer: StatsSource,
): Promise<RtcTransportTelemetry> {
  const report = await peer.getStats();
  return summarizeRtcStats(
    Array.from(report.values()).map((entry) => entry as unknown as StatsRecord),
  );
}

function mediaKind(entry: StatsRecord): string {
  return String(entry.kind ?? entry.mediaType ?? "").toLowerCase();
}

function stringValue(value: unknown): string | undefined {
  return typeof value === "string" && value ? value : undefined;
}

function numberValue(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

function integerValue(value: unknown): number | undefined {
  const number = numberValue(value);
  return number === undefined ? undefined : Math.round(number);
}

function rounded(value: number | undefined): number | undefined {
  return value === undefined ? undefined : Math.round(value * 10) / 10;
}

function secondsToMs(value: number | undefined): number | undefined {
  return value === undefined ? undefined : rounded(value * 1000);
}

function bitsToKbps(value: number | undefined): number | undefined {
  return value === undefined ? undefined : rounded(value / 1000);
}

function compact<T extends Record<string, unknown>>(value: T): T {
  return Object.fromEntries(
    Object.entries(value).filter(([, item]) => item !== undefined),
  ) as T;
}
