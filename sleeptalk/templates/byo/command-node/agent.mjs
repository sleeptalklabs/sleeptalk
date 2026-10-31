#!/usr/bin/env node
// A Sleeptalk agent that speaks Bridge Lite over stdio.
//
// Sleeptalk starts this program and writes one JSON object per line to stdin.
// Write one JSON object per line to stdout. Log with console.error, never
// console.log (stdout carries protocol frames).
//
// The only function you need to change is `reply`. Everything else is the
// protocol, and it already passes `sleeptalk adapter test --fixture`.
//
// Guide: docs/bring-your-own-agent.md in the Sleeptalk repository.

import readline from "node:readline";

// Advertise only what you actually send. Remove a line if you don't use it.
const CAPABILITIES = {
  structured_events: "supported",
  tool_lifecycle: "supported",
  permissions: "supported",
  clarification: "supported",
  steering: "supported",
  status: "supported",
  usage: "supported",
};

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/**
 * Answer one spoken turn. Replace the demo below with your agent.
 *
 *   turn.say(text)                   stream reply text (Sleeptalk splits sentences)
 *   turn.tool(name, detail, opts)    report tool work so Sleeptalk can narrate it
 *   await turn.askPermission(prompt) -> "deny" | "allow_once" | "allow_session"
 *   turn.aborted                     true once the caller interrupts; stop quickly
 */
async function reply(turn, text) {
  // --- Replace from here with your model call, e.g. streaming an LLM: ---
  //   for await (const chunk of await openai.chat.completions.create({ ..., stream: true })) {
  //     if (turn.aborted) return;
  //     turn.say(chunk.choices[0]?.delta?.content ?? "");
  //   }
  const words = text.trim().toLowerCase();
  turn.send({ type: "thinking.started" });
  if (words === "slow") {
    for (const number of ["One.", "Two.", "Three.", "Four.", "Five."]) {
      if (turn.aborted) return;
      turn.say(number + " ");
      await sleep(300);
    }
  } else if (words === "hang") {
    while (!turn.aborted) await sleep(50);
    return;
  } else if (words === "tool") {
    turn.tool("read", "reading the readme", { phase: "started", callId: "t1" });
    await sleep(50);
    turn.tool("read", "reading the readme", { phase: "completed", callId: "t1" });
    turn.say("The readme says hello.");
  } else if (words === "permission") {
    const decision = await turn.askPermission("Allow deleting the build folder?");
    turn.say(decision !== "deny" ? "Deleted it." : "Okay, I left it alone.");
  } else if (words === "clarify") {
    turn.send({ type: "clarification.requested", requestId: `c-${turn.id}`, prompt: "Which file?", choices: ["a", "b"] });
    turn.say("Tell me which file when you're ready.");
  } else {
    turn.say(`You said: ${text.trim()}.`);
  }
  turn.send({ type: "usage.reported", inputTokens: text.split(/\s+/).length, outputTokens: 5 });
  // --- to here. ---
}

// ---------------------------------------------------------------------------
// Protocol plumbing. You shouldn't need to edit below this line.
// ---------------------------------------------------------------------------

function send(frame) {
  process.stdout.write(JSON.stringify({ v: 1, ...frame }) + "\n");
}

class Turn {
  constructor(id) {
    this.id = id;
    this.aborted = false;
    this.permissions = new Map();
  }
  send(frame) {
    send({ ...frame, turnId: this.id });
  }
  say(text) {
    if (text && !this.aborted) this.send({ type: "assistant.delta", text });
  }
  tool(name, detail = "", { phase = "started", callId = null } = {}) {
    this.send({ type: "tool", name, phase, detail, callId });
  }
  askPermission(prompt, options = ["deny", "allow_once"]) {
    const requestId = `perm-${this.id}-${this.permissions.size + 1}`;
    return new Promise((resolve) => {
      this.permissions.set(requestId, resolve);
      this.send({ type: "permission.requested", requestId, prompt, options });
      this.send({ type: "status.changed", state: "blocked" });
    }).then((decision) => {
      this.permissions.delete(requestId);
      this.send({ type: "status.changed", state: "working" });
      return decision;
    });
  }
  resolve(requestId, decision) {
    const resolve = this.permissions.get(requestId);
    if (!resolve) return false;
    resolve(decision);
    return true;
  }
}

let current = null;
let running = false;

async function run(turn, text) {
  running = true;
  try {
    await reply(turn, text);
    turn.send({ type: turn.aborted ? "turn.aborted" : "turn.completed" });
  } catch (error) {
    console.error(`turn ${turn.id} failed:`, error);
    turn.send({ type: "error", code: "agent", message: String(error?.message ?? error), retryable: true });
  } finally {
    running = false;
  }
}

const input = readline.createInterface({ input: process.stdin });
input.on("line", (line) => {
  if (!line.trim()) return;
  let frame;
  try {
    frame = JSON.parse(line);
  } catch {
    console.error("ignored non-JSON input:", line.slice(0, 80));
    return;
  }
  const turnId = frame.turnId ?? frame.turn_id;
  switch (frame.type) {
    case "hello":
      send({ type: "hello.ok", capabilities: CAPABILITIES });
      break;
    case "turn.prompt":
      current = new Turn(turnId ?? 0);
      run(current, String(frame.text ?? ""));
      break;
    case "turn.abort":
      if (current && current.id === turnId && running) {
        current.aborted = true;
        for (const resolve of current.permissions.values()) resolve("deny");
      } else {
        send({ type: "turn.aborted", turnId });
      }
      break;
    case "permission.resolve": {
      const ok = Boolean(current?.resolve(frame.requestId, frame.decision));
      send({ type: ok ? "control.accepted" : "control.rejected", ref: frame.id, requestId: frame.requestId, reason: ok ? null : "unknown request" });
      break;
    }
    case "clarification.answer":
    case "steer":
    case "cancel":
      send({ type: "control.accepted", ref: frame.id, requestId: frame.requestId });
      break;
    case "session.close":
      process.exit(0);
  }
});
input.on("close", () => process.exit(0));
