import type { OrbState } from "./presentation";

export function voiceState(input: {
  host: OrbState | null;
  remoteLevel: number;
  localLevel: number;
  now: number;
  lastRemoteSpeechAt: number;
  lastLocalSpeechAt: number;
}): OrbState {
  if (input.remoteLevel > 0.04) return "speaking";
  if (input.localLevel > 0.05) return "listening";
  if (input.lastRemoteSpeechAt > 0 && input.now - input.lastRemoteSpeechAt < 220) return "speaking";
  if (input.host) return input.host;
  if (input.lastLocalSpeechAt > 0 && input.now - input.lastLocalSpeechAt < 4_000) return "thinking";
  return "idle";
}

export interface VoiceSignal {
  level: number;
  waveform: Readonly<Float32Array>;
}

export const WAVEFORM_POINTS = 64;

export const SILENCE: Readonly<VoiceSignal> = {
  level: 0, waveform: new Float32Array(WAVEFORM_POINTS),
};
