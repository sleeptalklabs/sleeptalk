#!/usr/bin/env node
// A remote agent that Sleeptalk reaches over Agent Bridge v1, in plain Node.
//
// Sleeptalk connects in with a bearer token, sends turns, and receives
// sentences and events. Use Bridge when the agent is long-lived and remote.
// For a local program use `--kind command`; for a web service that answers
// one request per turn use `--kind http`.
//
// Transport: one JSON object per line over TCP (the protocol's TLS TCP
// option), so this file needs no dependencies. Without a certificate it
// listens in plaintext and only on loopback. Anywhere else, set TLS_CERT_FILE
// and TLS_KEY_FILE and give Sleeptalk a tls://host:port URL.
//
// The only function you need to change is `reply`.
// Protocol: docs/agent-bridge-protocol.md in the Sleeptalk repository.

import crypto from "node:crypto";
import fs from "node:fs";
import net from "node:net";
import readline from "node:readline";
import tls from "node:tls";

const HOST = process.env.HOST ?? "127.0.0.1";
const PORT = Number(process.env.PORT ?? 8766);
const TOKEN = process.env.AGENT_BRIDGE_TOKEN ?? "";
const TLS_CERT_FILE = process.env.TLS_CERT_FILE ?? "";
const TLS_KEY_FILE = process.env.TLS_KEY_FILE ?? "";

// Advertise only what you actually send. Keys use the protocol's camelCase.
const ADVERTISED = {
  structuredEvents: "supported",
  toolLifecycle: "supported",
};

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/**
 * Answer one spoken turn. Replace the demo below with your agent.
 *
 *   turn.say(text)                  stream reply text; sentences are split for you
 *   turn.tool(name, detail, opts)   report tool work so Sleeptalk can narrate it
 *   turn.thinking()                 reasoning started
 *   turn.aborted / turn.signal      the caller interrupted; stop quickly
 */
async function reply(turn, text) {
  // --- Replace from here with your model call, e.g. streaming an LLM: ---
  //   for await (const chunk of stream) {
  //     if (turn.aborted) return;
  //     turn.say(chunk.choices[0]?.delta?.content ?? "");
  //   }
  const words = text.trim().toLowerCase();
  turn.thinking();
  if (words === "slow") {
    for (const number of ["One.", "Two.", "Three.", "Four.", "Five."]) {
      if (turn.aborted) return;
      turn.say(number + " ");
      await sleep(300);
    }
  } else if (words === "hang") {
    while (!turn.aborted) await sleep(50);
  } else if (words === "tool") {
    turn.tool("read", "reading the readme", { phase: "started", callId: "t1" });
    await sleep(50);
    turn.tool("read", "reading the readme", { phase: "completed", callId: "t1" });
    turn.say("The readme says hello.");
  } else {
    turn.say(`You said: ${text.trim()}.`);
  }
  // --- to here. ---
}

// ---------------------------------------------------------------------------
// Protocol plumbing. You shouldn't need to edit below this line.
// ---------------------------------------------------------------------------

const VERSION = 1;
const LIMITS = {
  heartbeatMs: 15000,
  missedHeartbeats: 2,
  sendQueue: 32,
  initialCredit: 16,
  replayWindowMs: 120000,
  maxFrameBytes: 65536,
};
const CAPABILITY_KEYS = [
  "sessionIdentity", "toolLifecycle", "permissions", "clarification", "structuredEvents",
  "status", "usage", "steering", "targetedCancellation", "workers", "voiceSessions",
];
const CREDIT_EXEMPT = new Set(["hello", "hello.ok", "hello.err", "heartbeat", "credit", "disconnect", "turn.aborted", "error"]);
const SIDE_EFFECTING = new Set([
  "turn.prompt", "turn.abort", "permission.resolve", "clarification.answer", "steer", "cancel",
  "voice_session.start", "session.warmup", "session.close",
]);
const CAPABILITY_FOR = {
  "permission.resolve": "permissions",
  "clarification.answer": "clarification",
  steer: "steering",
  cancel: "targetedCancellation",
  "voice_session.start": "voiceSessions",
  "assistant.delta": "structuredEvents",
  tool: "toolLifecycle",
};

// Hosts may send snake_case keys; everything below reads camelCase.
function camel(value) {
  if (Array.isArray(value)) return value.map(camel);
  if (!value || typeof value !== "object") return value;
  return Object.fromEntries(
    Object.entries(value).map(([key, item]) => [key.replace(/_([a-z])/g, (_m, c) => c.toUpperCase()), camel(item)]),
  );
}

function level(raw) {
  const value = typeof raw === "string" ? raw : raw?.level;
  return ["supported", "unsupported", "unavailable"].includes(value) ? value : "unsupported";
}

function tokensEqual(presented) {
  const a = Buffer.from(String(presented ?? ""));
  const b = Buffer.from(TOKEN);
  return a.length === b.length && crypto.timingSafeEqual(a, b);
}

function splitSentences(buffer) {
  const sentences = [];
  const pattern = /[.!?]+["')\]]*\s+/g;
  let start = 0;
  let match;
  while ((match = pattern.exec(buffer)) !== null) {
    const sentence = buffer.slice(start, match.index + match[0].length).trim();
    if (sentence) sentences.push(sentence);
    start = match.index + match[0].length;
  }
  return { sentences, rest: buffer.slice(start) };
}

class Session {
  constructor(socket) {
    this.socket = socket;
    this.ready = false;
    this.capabilities = {};
    this.credit = LIMITS.initialCredit;
    this.unacked = [];
    this.creditWaiters = [];
    this.replay = new Map();
    this.seq = 0;
    this.lastRecvId = null;
    this.lastRecvAt = Date.now();
    this.lastSendAt = Date.now();
    this.turn = null;
    this.outbox = Promise.resolve();
    this.closed = false;
    this.timer = setInterval(() => this.tick(), Math.max(LIMITS.heartbeatMs / 2, 50));
    const lines = readline.createInterface({ input: socket, crlfDelay: Infinity });
    lines.on("line", (line) => this.onLine(line));
    socket.on("close", () => this.shutdown());
    socket.on("error", () => this.shutdown());
  }

  nextId() {
    this.seq += 1;
    return `e-${this.seq}-${crypto.randomBytes(4).toString("hex")}`;
  }

  supported(key) {
    return level(this.capabilities[key]) === "supported";
  }

  // Frames go out in order. Application frames wait for credit.
  send(frame) {
    this.outbox = this.outbox.then(() => this.sendNow(frame)).catch(() => {});
    return this.outbox;
  }

  interrupted(frame) {
    // Spoken content from a turn the caller interrupted is dropped, even if
    // it was already waiting for credit.
    return frame.turnId !== undefined && this.turn?.dropFramesFor === frame.turnId && !CREDIT_EXEMPT.has(frame.type);
  }

  async sendNow(frame) {
    if (this.closed || this.interrupted(frame)) return;
    const outgoing = { v: VERSION, ...frame };
    if (!CREDIT_EXEMPT.has(outgoing.type)) {
      outgoing.id ??= this.nextId();
      const deadline = Date.now() + LIMITS.heartbeatMs;
      while (this.credit <= 0 || this.unacked.length >= LIMITS.sendQueue) {
        if (this.interrupted(frame)) return;
        const remaining = deadline - Date.now();
        if (remaining <= 0 || this.closed) {
          this.write({ v: VERSION, type: "error", code: "backpressure", message: "credit or queue exhausted", retryable: true, fatal: false });
          return;
        }
        await new Promise((resolve) => {
          const timer = setTimeout(resolve, remaining);
          this.creditWaiters.push(() => { clearTimeout(timer); resolve(); });
        });
      }
      this.credit -= 1;
      this.unacked.push(outgoing.id);
    }
    this.write(outgoing);
  }

  write(frame) {
    if (this.closed) return;
    this.socket.write(JSON.stringify(frame) + "\n");
    this.lastSendAt = Date.now();
  }

  wake() {
    const waiters = this.creditWaiters.splice(0);
    for (const waiter of waiters) waiter();
  }

  reply(frameId, result) {
    this.replay.set(frameId, { result, expires: Date.now() + LIMITS.replayWindowMs });
    return this.send(result);
  }

  tick() {
    const now = Date.now();
    if (now - this.lastRecvAt > LIMITS.heartbeatMs * LIMITS.missedHeartbeats) {
      this.shutdown();
      return;
    }
    if (this.ready && now - this.lastSendAt >= LIMITS.heartbeatMs) {
      this.write({ v: VERSION, type: "heartbeat", lastId: this.lastRecvId });
    }
    for (const [id, entry] of this.replay) if (entry.expires <= now) this.replay.delete(id);
  }

  shutdown() {
    if (this.closed) return;
    this.closed = true;
    clearInterval(this.timer);
    this.turn?.controller.abort();
    this.wake();
    this.socket.destroy();
  }

  fail(code, message) {
    this.write({ v: VERSION, type: "hello.err", code, message });
    this.socket.end();
    this.closed = true;
    clearInterval(this.timer);
  }

  onLine(line) {
    if (!line.trim() || this.closed) return;
    this.lastRecvAt = Date.now();
    if (Buffer.byteLength(line) > LIMITS.maxFrameBytes) {
      this.write({ v: VERSION, type: "error", code: "protocol", message: "frame exceeds maxFrameBytes", retryable: false, fatal: true });
      this.shutdown();
      return;
    }
    let frame;
    try {
      frame = camel(JSON.parse(line));
    } catch {
      this.write({ v: VERSION, type: "error", code: "protocol", message: "malformed JSON", retryable: false, fatal: true });
      this.shutdown();
      return;
    }
    if (!this.ready) return this.handshake(frame);
    if (frame.id && !["heartbeat", "credit", "disconnect"].includes(frame.type)) this.lastRecvId = frame.id;
    switch (frame.type) {
      case "heartbeat": {
        const index = this.unacked.indexOf(frame.lastId);
        if (index >= 0) this.unacked.splice(0, index + 1);
        this.wake();
        return;
      }
      case "credit":
        this.credit += Math.max(0, Number(frame.add) || 0);
        this.wake();
        return;
      case "disconnect":
        this.shutdown();
        return;
    }
    if (!SIDE_EFFECTING.has(frame.type)) {
      this.send({ type: "error", code: "protocol", message: `out-of-order type ${frame.type}`, retryable: false, fatal: false });
      return;
    }
    if (!frame.id) {
      this.send({ type: "error", code: "protocol", message: "application frame missing id", retryable: false, fatal: false });
      return;
    }
    const needed = CAPABILITY_FOR[frame.type];
    if (needed && !this.supported(needed)) {
      this.send({ type: "error", code: "protocol", message: `${frame.type} requires ${needed}`, retryable: false, fatal: false });
      return;
    }
    const cached = this.replay.get(frame.id);
    if (cached) {
      this.send(cached.result);
      return;
    }
    this.handle(frame);
  }

  handshake(frame) {
    if (frame.type !== "hello") return this.fail("protocol", "first frame must be hello");
    if (Number(frame.v) !== VERSION) return this.fail("version_mismatch", "major version is not 1");
    if (!tokensEqual(frame.auth?.token)) return this.fail("auth", "expired");
    // This template keeps no resumable state; a resume attempt starts over.
    if (frame.sessionId || frame.resumeToken) return this.fail("auth", "expired");
    const offered = frame.capabilities ?? {};
    for (const [key, value] of Object.entries(offered)) {
      if (!CAPABILITY_KEYS.includes(key) && level(value) === "supported") {
        return this.fail("unknown_capability", `required capability is not in v1: ${key}`);
      }
    }
    this.capabilities = Object.fromEntries(
      CAPABILITY_KEYS.map((key) => [
        key,
        { level: level(offered[key]) === "supported" && level(ADVERTISED[key]) === "supported" ? "supported" : "unsupported" },
      ]),
    );
    this.ready = true;
    this.write({
      v: VERSION,
      type: "hello.ok",
      sessionId: `sess-${crypto.randomBytes(9).toString("base64url")}`,
      resumeToken: crypto.randomBytes(24).toString("base64url"),
      capabilities: this.capabilities,
      limits: LIMITS,
    });
  }

  handle(frame) {
    switch (frame.type) {
      case "session.warmup":
        return this.reply(frame.id, { type: "control.accepted", ref: frame.id });
      case "session.close":
        this.reply(frame.id, { type: "control.accepted", ref: frame.id }).then(() => this.shutdown());
        return;
      case "turn.prompt":
        if (this.turn && !this.turn.finished) {
          return this.send({ type: "error", code: "protocol", message: "one turn is already in flight", retryable: false, fatal: false });
        }
        return this.runTurn(frame);
      case "turn.abort": {
        const turn = this.turn;
        if (turn && !turn.finished) {
          turn.controller.abort();
          turn.dropFramesFor = turn.id;
          this.wake();
        }
        return this.reply(frame.id, { type: "turn.aborted", ref: frame.id, turnId: frame.turnId ?? turn?.id ?? 0 });
      }
      default:
        return this.reply(frame.id, { type: "control.rejected", ref: frame.id, reason: `${frame.type} is not implemented by this agent`, retryable: false });
    }
  }

  async runTurn(frame) {
    const session = this;
    const controller = new AbortController();
    let buffer = "";
    const turnId = Number(frame.turnId ?? 0);
    const full = frame.mode === "full";
    const spoken = [];
    const state = { id: turnId, controller, finished: false, dropFramesFor: null };
    this.turn = state;

    const speak = (sentence) => {
      spoken.push(sentence);
      if (!full) session.send({ type: "sentence", turnId, text: sentence });
    };
    const turn = {
      id: turnId,
      signal: controller.signal,
      get aborted() { return controller.signal.aborted; },
      thinking() { session.send({ type: "thinking.started", turnId }); },
      say(text) {
        if (!text || controller.signal.aborted) return;
        if (session.supported("structuredEvents")) session.send({ type: "assistant.delta", turnId, text });
        const split = splitSentences(buffer + text);
        buffer = split.rest;
        split.sentences.forEach(speak);
      },
      tool(name, detail = "", { phase = "started", callId = null } = {}) {
        if (session.supported("toolLifecycle")) session.send({ type: "tool", turnId, name, phase, detail, callId });
      },
    };

    try {
      await reply(turn, String(frame.text ?? ""));
      if (controller.signal.aborted) return;
      if (buffer.trim()) speak(buffer.trim());
      const completed = { type: "turn.completed", ref: frame.id, turnId };
      if (full) completed.text = spoken.join(" ");
      await this.reply(frame.id, completed);
    } catch (error) {
      if (controller.signal.aborted) return;
      console.error(`turn ${turnId} failed:`, error);
      await this.reply(frame.id, { type: "error", ref: frame.id, turnId, code: "agent", message: String(error?.message ?? error), retryable: true, fatal: false });
    } finally {
      state.finished = true;
    }
  }
}

if (!TOKEN) {
  console.error("set AGENT_BRIDGE_TOKEN to a long random value (and the same value for Sleeptalk)");
  process.exit(2);
}
const useTls = Boolean(TLS_CERT_FILE && TLS_KEY_FILE);
const loopback = ["127.0.0.1", "::1", "localhost"].includes(HOST);
if (!useTls && !loopback) {
  console.error("plaintext is only allowed on loopback; set TLS_CERT_FILE and TLS_KEY_FILE to listen on " + HOST);
  process.exit(2);
}
const server = useTls
  ? tls.createServer({ cert: fs.readFileSync(TLS_CERT_FILE), key: fs.readFileSync(TLS_KEY_FILE) }, (socket) => new Session(socket))
  : net.createServer((socket) => new Session(socket));
server.listen(PORT, HOST, () => {
  const { port } = server.address();
  console.log(`Agent Bridge listening on ${useTls ? "tls" : "tcp"}://${HOST}:${port}`);
});
