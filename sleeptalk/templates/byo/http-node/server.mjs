#!/usr/bin/env node
// A Sleeptalk agent behind an HTTP endpoint (Bridge Lite over HTTP).
//
// Sleeptalk POSTs each spoken turn as JSON:
//   {"type":"turn.prompt","turnId":1,"sessionId":"...","text":"hello"}
// Reply with a streamed body of frames, one JSON object per line
// (Content-Type: application/x-ndjson). Sleeptalk also accepts Server-Sent
// Events, a single JSON object like {"text":"..."}, or plain streamed text.
// Answer GET on the same path with {"type":"hello.ok","capabilities":{...}}
// so Sleeptalk knows which frames you send before the first turn.
// When the caller interrupts, Sleeptalk closes the request.
//
// In Next.js the same thing is a route handler that returns
// `new Response(readableStream, { headers: { "Content-Type": "application/x-ndjson" } })`.
//
// Guide: docs/bring-your-own-agent.md in the Sleeptalk repository.

import crypto from "node:crypto";
import http from "node:http";

const HOST = process.env.HOST ?? "127.0.0.1";
const PORT = Number(process.env.PORT ?? 8765);
const PATH = "/sleeptalk/turn";
// Shared secret. Optional on loopback; required on any other address, since
// anyone who can reach the port could otherwise drive the agent. Set the same
// value in the variable named by token_env.
const TOKEN = process.env.AGENT_TOKEN ?? "";
if (!TOKEN && !["127.0.0.1", "::1", "localhost"].includes(HOST)) {
  console.error(`set AGENT_TOKEN to a long random value to listen on ${HOST} (and the same value for Sleeptalk)`);
  process.exit(1);
}

// Advertise what you send. HTTP turns cannot receive permission answers mid-turn.
const CAPABILITIES = { structured_events: "supported", tool_lifecycle: "supported", usage: "supported" };

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/** Yield frames for one turn. Replace the demo with your agent. */
async function* reply(sessionId, text, signal) {
  // --- Replace from here with your model call. ---
  const words = text.trim().toLowerCase();
  yield { type: "thinking.started" };
  if (words === "slow") {
    for (const number of ["One.", "Two.", "Three.", "Four.", "Five."]) {
      if (signal.aborted) return;
      yield { type: "assistant.delta", text: number + " " };
      await sleep(300);
    }
  } else if (words === "hang") {
    while (!signal.aborted) await sleep(50);
    return;
  } else if (words === "tool") {
    yield { type: "tool", name: "read", phase: "started", callId: "t1", detail: "reading the readme" };
    yield { type: "tool", name: "read", phase: "completed", callId: "t1" };
    yield { type: "assistant.delta", text: "The readme says hello." };
  } else {
    yield { type: "assistant.delta", text: `You said: ${text.trim()}.` };
  }
  yield { type: "usage.reported", inputTokens: text.split(/\s+/).length, outputTokens: 5 };
  // --- to here. ---
}

function authorized(req) {
  if (!TOKEN) return true;
  const presented = Buffer.from(String(req.headers.authorization ?? ""));
  const expected = Buffer.from(`Bearer ${TOKEN}`);
  return presented.length === expected.length && crypto.timingSafeEqual(presented, expected);
}

http
  .createServer(async (req, res) => {
    if (req.method === "HEAD") {
      res.writeHead(authorized(req) ? 204 : 401).end(); // readiness probe; never starts work
      return;
    }
    if (req.url !== PATH) return res.writeHead(404).end();
    if (!authorized(req)) return res.writeHead(401).end();
    if (req.method === "GET") {
      // Capability discovery; never starts work.
      res.writeHead(200, { "Content-Type": "application/json" });
      return res.end(JSON.stringify({ v: 1, type: "hello.ok", capabilities: CAPABILITIES }));
    }
    if (req.method !== "POST") return res.writeHead(405).end();
    let body = "";
    for await (const chunk of req) body += chunk;
    const frame = JSON.parse(body || "{}");
    const controller = new AbortController();
    res.on("close", () => controller.abort()); // the caller interrupted
    res.writeHead(200, { "Content-Type": "application/x-ndjson" });
    for await (const out of reply(String(frame.sessionId ?? ""), String(frame.text ?? ""), controller.signal)) {
      if (controller.signal.aborted) return;
      res.write(JSON.stringify({ v: 1, turnId: frame.turnId, ...out }) + "\n");
    }
    res.end(JSON.stringify({ v: 1, turnId: frame.turnId, type: "turn.completed" }) + "\n");
  })
  .listen(PORT, HOST, () => console.log(`listening on http://${HOST}:${PORT}${PATH}`));
