import type { OrbState } from "./orb";
import { SILENCE, WAVEFORM_POINTS, type VoiceSignal } from "./voice-activity";

const TAU = Math.PI * 2;
const STROKE = "#EE3124";
const STROKE_WIDTH = 4;
const CONTOUR_FPS = 30;

const MOTION: Record<OrbState, { gain: number; drift: number; radius: number }> = {
  idle: { gain: 0, drift: 0.18, radius: 0.36 },
  listening: { gain: 0.55, drift: 0.25, radius: 0.21 },
  thinking: { gain: 0, drift: 0.65, radius: 0.35 },
  speaking: { gain: 1, drift: 0.35, radius: 0.378 },
};

function smooth(value: number, target: number, dt: number, seconds: number): number {
  return value + (target - value) * (1 - Math.exp(-dt / seconds));
}

export class ScribbleOrb {
  private readonly context: CanvasRenderingContext2D | null;
  private readonly reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
  private state: OrbState = "idle";
  private voice: Readonly<VoiceSignal> = SILENCE;
  private amplitude = 0;
  private readonly contour = new Float32Array(WAVEFORM_POINTS);
  private contourFrame = -1;
  private radius = MOTION.idle.radius;
  private phase = 0;
  private last = 0;
  private raf: number | null = null;

  constructor(private readonly canvas: HTMLCanvasElement) {
    this.context = canvas.getContext("2d", { alpha: true });
  }

  get available(): boolean {
    return this.context !== null;
  }

  setState(state: OrbState): void {
    this.state = state;
  }

  setLevel(level: number): void {
    const value = Number.isFinite(level) ? Math.min(1, Math.max(0, level)) : 0;
    this.setVoice({ ...SILENCE, level: value });
  }

  setVoice(signal: Readonly<VoiceSignal>): void {
    this.voice = signal;
  }

  start(): void {
    if (!this.context || this.raf !== null) return;
    this.last = performance.now();
    const frame = (now: number): void => {
      this.render(now);
      this.raf = requestAnimationFrame(frame);
    };
    this.raf = requestAnimationFrame(frame);
  }

  stop(): void {
    if (this.raf !== null) cancelAnimationFrame(this.raf);
    this.raf = null;
  }

  private render(now: number): void {
    const context = this.context;
    if (!context) return;
    const dt = Math.min(0.05, (now - this.last) / 1000);
    this.last = now;
    const target = MOTION[this.state];
    const level = Math.pow(this.voice.level, 0.55) * target.gain;
    this.amplitude = smooth(this.amplitude, level, dt, level > this.amplitude ? 0.015 : 0.09);
    this.radius = smooth(this.radius, target.radius, dt, 0.2);
    this.phase += dt * target.drift;

    // The reference holds each contour for two frames in its 60 fps recording.
    const contourFrame = Math.floor(now * CONTOUR_FPS / 1000);
    if (contourFrame !== this.contourFrame) {
      this.contourFrame = contourFrame;
      let peak = 0.08;
      for (const sample of this.voice.waveform) peak = Math.max(peak, Math.abs(sample));
      const strength = this.voice.level > 0.008 ? level * 0.18 : 0;
      for (let i = 0; i < this.contour.length; i++) {
        const next = this.voice.waveform[i] / peak * strength;
        this.contour[i] = this.contour[i] * 0.15 + next * 0.85;
      }
    }

    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const width = Math.max(1, Math.round(this.canvas.clientWidth * dpr));
    const height = Math.max(1, Math.round(this.canvas.clientHeight * dpr));
    if (this.canvas.width !== width || this.canvas.height !== height) {
      this.canvas.width = width;
      this.canvas.height = height;
    }
    context.clearRect(0, 0, width, height);
    const reduced = this.reducedMotion.matches;
    const amplitude = reduced ? 0 : this.amplitude;
    const time = reduced ? 0 : this.phase;
    const size = Math.min(width, height);
    const swell = 1 + amplitude * 0.045 + (reduced ? 0 : 0.006 * Math.sin(time * 1.4));
    const radius = size * (reduced ? MOTION.idle.radius : this.radius) * swell;
    context.strokeStyle = STROKE;
    context.lineWidth = STROKE_WIDTH * dpr;
    context.lineJoin = "round";
    context.lineCap = "round";
    context.beginPath();
    for (let i = 0; i < this.contour.length; i++) {
      const angle = i / this.contour.length * TAU;
      const resting = 0.006 * Math.sin(3 * angle + time) + 0.004 * Math.sin(5 * angle - time);
      const r = radius * (1 + resting + (reduced ? 0 : this.contour[i]));
      const x = width / 2 + Math.cos(angle) * r;
      const y = height / 2 + Math.sin(angle) * r;
      if (i === 0) context.moveTo(x, y);
      else context.lineTo(x, y);
    }
    context.closePath();
    context.stroke();
  }
}
