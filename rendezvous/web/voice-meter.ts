import { SILENCE, WAVEFORM_POINTS, type VoiceSignal } from "./voice-activity";

export class VoiceMeter {
  readonly node: AnalyserNode;
  private readonly samples = new Float32Array(1024);
  private readonly waveform = new Float32Array(WAVEFORM_POINTS);
  private readonly signal: VoiceSignal = { ...SILENCE, waveform: this.waveform };

  constructor(context: AudioContext) {
    this.node = context.createAnalyser();
    this.node.fftSize = this.samples.length;
  }

  read(): Readonly<VoiceSignal> {
    this.node.getFloatTimeDomainData(this.samples);
    let power = 0;
    for (const sample of this.samples) power += sample * sample;
    this.signal.level = Math.min(1, Math.sqrt(power / this.samples.length) * 4);
    const stride = this.samples.length / this.waveform.length;
    for (let i = 0; i < this.waveform.length; i++) {
      const start = i * stride;
      this.waveform[i] = (this.samples[start] + this.samples[start + 1]
        + this.samples[start + 2] + this.samples[start + 3]) / 4;
    }
    return this.signal;
  }
}
