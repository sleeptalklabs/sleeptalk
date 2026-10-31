// WebGL voice orb ported from design/orb.html.
// States: idle, listening, thinking, speaking. The motion is synthesized per
// state, and when real audio levels are fed via setLevel() the measured
// amplitude drives the deformation for listening/speaking.

export type OrbState = "idle" | "listening" | "thinking" | "speaking";

interface OrbTargets {
  blur: number;
  chroma: number;
  haze: number;
  rad: number;
  gain: number;
}

const TARGETS: Record<OrbState, OrbTargets> = {
  idle: { blur: 0.048, chroma: 1.0, haze: 0.8, rad: 0.22, gain: 0.1 },
  listening: { blur: 0.056, chroma: 0.88, haze: 1.15, rad: 0.12, gain: 0.55 },
  thinking: { blur: 0.038, chroma: 1.22, haze: 0.65, rad: 0.21, gain: 0.75 },
  speaking: { blur: 0.042, chroma: 1.12, haze: 0.9, rad: 0.26, gain: 1.0 },
};

// State-change size transition: .4s cubic-bezier(.6,.2,.1,1) — fast off the
// mark, settles cleanly. Snappy without snapping.
const RAD_TRANSITION_SECONDS = 0.4;

type Easing = (x: number) => number;

function cubicBezier(x1: number, y1: number, x2: number, y2: number): Easing {
  const sample = (a1: number, a2: number, t: number): number => {
    const inv = 1 - t;
    return 3 * inv * inv * t * a1 + 3 * inv * t * t * a2 + t * t * t;
  };
  const derivative = (a1: number, a2: number, t: number): number => {
    const inv = 1 - t;
    return 3 * inv * inv * a1 + 6 * inv * t * (a2 - a1) + 3 * t * t * (1 - a2);
  };
  return (x: number): number => {
    if (x <= 0) return 0;
    if (x >= 1) return 1;
    let t = x;
    for (let i = 0; i < 6; i++) {
      const err = sample(x1, x2, t) - x;
      const d = derivative(x1, x2, t);
      if (Math.abs(err) < 1e-4 || Math.abs(d) < 1e-4) break;
      t -= err / d;
    }
    return sample(y1, y2, t);
  };
}

const RAD_EASE = cubicBezier(0.6, 0.2, 0.1, 1);

const VERTEX_SHADER = "attribute vec2 a;void main(){gl_Position=vec4(a,0.0,1.0);}";

const FRAGMENT_SHADER = [
  "precision highp float;",
  "uniform vec2 res;uniform float uB,uC,uH,uR,uAmp,uT,uPulse,uPA;",
  "uniform vec4 uBands;",
  "float fall(float x,float s,float w){return 1.0-smoothstep(s-w,s+w,x);}",
  "void main(){",
  "  vec2 p=(gl_FragCoord.xy-0.5*res)/res.y;",
  "  float d=length(p);",
  "  float th=atan(p.y,p.x);",
  "  float def=0.0;",
  "  def+=uBands.x*0.055*sin(2.0*th+uT*0.62);",
  "  def+=uBands.y*0.040*sin(3.0*th-uT*0.87+1.7);",
  "  def+=uBands.z*0.028*sin(4.0*th+uT*1.24+3.1);",
  "  def+=uBands.w*0.020*sin(5.0*th-uT*1.05+0.4);",
  "  float Rr=uR*(1.0+def);",
  "  float x=(d-Rr)/uB;",
  "  float aR=fall(x, 0.55*uC, 0.95);",
  "  float aG=fall(x,-0.10*uC, 0.85);",
  "  float aB=fall(x,-0.18*uC, 0.80);",
  "  vec3 core=vec3(0.933,0.376,0.333);",
  "  vec3 col=core*vec3(aR,aG,aB);",
  "  float ring=clamp(aR-aG,0.0,1.0);",
  "  col.r*=1.0+(0.10+0.22*uAmp)*ring;",
  "  float hz=exp(-pow((x-0.55)/0.62,2.0));",
  "  col+=vec3(0.060,0.050,0.046)*hz*uH;",
  "  float pr=Rr*(1.0+uPulse*0.42);",
  "  float pring=exp(-pow((d-pr)/(uB*1.1),2.0));",
  "  col+=vec3(0.55,0.13,0.09)*pring*uPA;",
  "  float n=fract(sin(dot(gl_FragCoord.xy,vec2(12.9898,78.233)))*43758.5453);",
  "  col+=(n-0.5)*0.0065;",
  "  gl_FragColor=vec4(clamp(col,0.0,1.0),clamp(max(col.r,max(col.g,col.b)),0.0,1.0));",
  "}",
].join("\n");

function smooth(value: number, target: number, dt: number, tau: number): number {
  return value + (target - value) * (1.0 - Math.exp(-dt / tau));
}

export class Orb {
  private readonly canvas: HTMLCanvasElement;
  private readonly gl: WebGLRenderingContext | null;
  private uniforms: Record<string, WebGLUniformLocation | null> = {};
  private state: OrbState = "idle";
  private cur: OrbTargets = { ...TARGETS.idle };
  private radFrom = TARGETS.idle.rad;
  private radT = RAD_TRANSITION_SECONDS;
  private amp = 0;
  private bands = [0, 0, 0, 0];
  private pulse = 0;
  private pulseA = 0;
  private measured = 0;
  private t0 = 0;
  private last = 0;
  private raf: number | null = null;

  constructor(canvas: HTMLCanvasElement) {
    this.canvas = canvas;
    this.gl = canvas.getContext("webgl", { antialias: false, alpha: true });
    if (!this.gl) return;
    const gl = this.gl;
    const compile = (type: number, source: string): WebGLShader => {
      const shader = gl.createShader(type);
      if (!shader) throw new Error("shader allocation failed");
      gl.shaderSource(shader, source);
      gl.compileShader(shader);
      return shader;
    };
    const program = gl.createProgram();
    if (!program) throw new Error("program allocation failed");
    gl.attachShader(program, compile(gl.VERTEX_SHADER, VERTEX_SHADER));
    gl.attachShader(program, compile(gl.FRAGMENT_SHADER, FRAGMENT_SHADER));
    gl.linkProgram(program);
    gl.useProgram(program);
    const buffer = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW);
    const location = gl.getAttribLocation(program, "a");
    gl.enableVertexAttribArray(location);
    gl.vertexAttribPointer(location, 2, gl.FLOAT, false, 0, 0);
    for (const key of ["res", "uB", "uC", "uH", "uR", "uAmp", "uT", "uPulse", "uPA", "uBands"]) {
      this.uniforms[key] = gl.getUniformLocation(program, key);
    }
  }

  get available(): boolean {
    return this.gl !== null;
  }

  setState(state: OrbState): void {
    if (state === this.state) return;
    this.state = state;
    // Start the bezier size transition from wherever the radius currently is.
    this.radFrom = this.cur.rad;
    this.radT = 0;
  }

  /** Measured audio level 0..1 driving listening/speaking deformation. */
  setLevel(level: number): void {
    this.measured = Math.min(1, Math.max(0, level));
  }

  start(): void {
    if (!this.gl || this.raf !== null) return;
    this.t0 = performance.now();
    this.last = this.t0;
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

  private source(tt: number): number {
    if (this.state === "idle") return 0.0;
    if (this.state === "listening") {
      if (this.measured > 0.005) return this.measured;
      const e = 0.18 + 0.14 * Math.sin(tt * 1.3) + 0.1 * Math.sin(tt * 2.7 + 1.2);
      return Math.max(0, e);
    }
    if (this.state === "thinking") {
      return 0.34 + 0.2 * Math.sin(tt * 0.9) + 0.12 * Math.sin(tt * 1.61 + 2.2);
    }
    if (this.measured > 0.005) return this.measured;
    let syl = Math.max(0, Math.sin(tt * 7.4)) * 0.75;
    syl *= 0.45 + 0.55 * Math.max(0, Math.sin(tt * 1.15 + 0.6));
    syl += 0.22 * Math.max(0, Math.sin(tt * 13.1 + 2.0));
    const gate = Math.sin(tt * 0.47) > -0.72 ? 1.0 : 0.06;
    return Math.min(1.0, syl * gate);
  }

  private render(now: number): void {
    const gl = this.gl;
    if (!gl) return;
    const dt = Math.min(0.05, (now - this.last) / 1000);
    this.last = now;
    const tt = (now - this.t0) / 1000;
    const target = TARGETS[this.state];
    const cur = this.cur;
    cur.blur = smooth(cur.blur, target.blur, dt, 0.28);
    cur.chroma = smooth(cur.chroma, target.chroma, dt, 0.28);
    cur.haze = smooth(cur.haze, target.haze, dt, 0.28);
    this.radT = Math.min(RAD_TRANSITION_SECONDS, this.radT + dt);
    const radProgress = RAD_EASE(this.radT / RAD_TRANSITION_SECONDS);
    cur.rad = this.radFrom + (target.rad - this.radFrom) * radProgress;
    cur.gain = smooth(cur.gain, target.gain, dt, 0.3);
    const raw = this.source(tt) * cur.gain;
    this.amp = smooth(this.amp, raw, dt, raw > this.amp ? 0.035 : 0.22);
    const taus = [0.2, 0.13, 0.085, 0.055];
    for (let i = 0; i < 4; i++) {
      const mod = 0.55 + 0.45 * Math.sin(tt * (1.4 + i * 0.83) + i * 2.1);
      this.bands[i] = smooth(
        this.bands[i],
        raw * mod,
        dt,
        raw * mod > this.bands[i] ? taus[i] * 0.35 : taus[i],
      );
    }
    this.pulse += dt * 0.55;
    if (this.pulse > 1.0) this.pulse -= 1.0;
    if (this.state === "listening") {
      this.pulseA = smooth(this.pulseA, Math.pow(1.0 - this.pulse, 2.2) * 0.28, dt, 0.05);
    } else {
      this.pulseA = smooth(this.pulseA, 0.0, dt, 0.25);
    }
    // Listening breathes a little harder — a visible "I hear you" swell.
    const breatheAmp = this.state === "listening" ? 0.03 : 0.014;
    const breathe = 1.0 + breatheAmp * Math.sin(tt * (this.state === "idle" ? 1.0 : 1.9));
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const w = Math.max(1, Math.round(this.canvas.clientWidth * dpr));
    const h = Math.max(1, Math.round(this.canvas.clientHeight * dpr));
    if (this.canvas.width !== w || this.canvas.height !== h) {
      this.canvas.width = w;
      this.canvas.height = h;
    }
    gl.viewport(0, 0, w, h);
    gl.uniform2f(this.uniforms.res, w, h);
    gl.uniform1f(this.uniforms.uB, cur.blur);
    gl.uniform1f(this.uniforms.uC, cur.chroma);
    gl.uniform1f(this.uniforms.uH, cur.haze);
    gl.uniform1f(this.uniforms.uR, cur.rad * breathe * (1.0 + this.amp * 0.06));
    gl.uniform1f(this.uniforms.uAmp, this.amp);
    gl.uniform1f(this.uniforms.uT, tt);
    gl.uniform1f(this.uniforms.uPulse, this.pulse);
    gl.uniform1f(this.uniforms.uPA, this.pulseA);
    gl.uniform4f(this.uniforms.uBands, this.bands[0], this.bands[1], this.bands[2], this.bands[3]);
    gl.drawArrays(gl.TRIANGLES, 0, 3);
  }
}
