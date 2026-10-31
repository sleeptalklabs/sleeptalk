#!/usr/bin/env node
// Independent Agent Bridge v1 host. Speaks JSONL TCP. Does not import Sleeptalk.

import net from "node:net";

const mode = process.argv[2] || "session";
const host = process.argv[3] || "127.0.0.1";
const port = Number(process.argv[4] || 0);
const token = process.env.BRIDGE_TOKEN || "loopback-token";

if (!port) {
  console.error("usage: bridge_independent_host.mjs session|mismatch|drop host port");
  process.exit(2);
}

function writeFrame(socket, frame) {
  socket.write(`${JSON.stringify(frame)}\n`);
}

function readLines(socket, onFrame) {
  let buf = "";
  socket.on("data", (chunk) => {
    buf += chunk.toString("utf8");
    let idx;
    while ((idx = buf.indexOf("\n")) >= 0) {
      const line = buf.slice(0, idx);
      buf = buf.slice(idx + 1);
      if (!line.trim()) continue;
      onFrame(JSON.parse(line));
    }
  });
}

const socket = net.createConnection({ host, port }, () => {
  if (mode === "mismatch") {
    writeFrame(socket, {
      v: 2,
      type: "hello",
      auth: { scheme: "bearer", token },
      client: { name: "independent-host", language: "javascript" },
    });
    return;
  }
  writeFrame(socket, {
    v: 1,
    type: "hello",
    auth: { scheme: "bearer", token },
    capabilities: {
      sessionIdentity: { level: "supported" },
      toolLifecycle: { level: "supported" },
      structuredEvents: { level: "supported" },
      permissions: { level: "unsupported" },
      clarification: { level: "unsupported" },
      status: { level: "unsupported" },
      usage: { level: "unsupported" },
      steering: { level: "unsupported" },
      targetedCancellation: { level: "unsupported" },
      workers: { level: "unsupported" },
      voiceSessions: { level: "unsupported" },
    },
    client: { name: "independent-host", language: "javascript" },
  });
});

const seen = [];
readLines(socket, (frame) => {
  seen.push(frame.type);
  if (mode === "mismatch") {
    if (frame.type !== "hello.err" || frame.code !== "version_mismatch") {
      console.error(JSON.stringify(frame));
      process.exit(1);
    }
    if (JSON.stringify(frame).includes(token)) {
      console.error("token echoed");
      process.exit(1);
    }
    process.exit(0);
  }
  if (frame.type === "hello.err") {
    console.error(JSON.stringify(frame));
    process.exit(1);
  }
  if (frame.type === "hello.ok" && mode === "session") {
    writeFrame(socket, {
      v: 1,
      type: "turn.prompt",
      id: "p-ind-1",
      turnId: 1,
      text: "hello there",
      mode: "stream",
    });
  }
  if (frame.type === "hello.ok" && mode === "drop") {
    writeFrame(socket, {
      v: 1,
      type: "turn.prompt",
      id: "p-drop-1",
      turnId: 1,
      text: "hold please",
      mode: "stream",
    });
    socket.destroy();
    process.exit(0);
  }
  if (frame.type === "turn.completed" && frame.ref === "p-ind-1") {
    writeFrame(socket, { v: 1, type: "session.close", id: "c-1" });
    process.exit(0);
  }
});

socket.on("error", (err) => {
  console.error(err.message);
  process.exit(1);
});

setTimeout(() => {
  console.error(`timeout after ${seen.join(",")}`);
  process.exit(1);
}, 8000);
