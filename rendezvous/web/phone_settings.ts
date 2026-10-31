// The settings page on a phone. It is the host's own settings page (the same
// markup, styles, and script as `sleeptalk settings --open`); only its
// transport differs. Each request, icon, word list, and voice sample travels
// to the host through the sealed setup channel, so this service relays
// ciphertext and never sees a setting, a key, or a profile.
import "../../sleeptalk/_settings/settings.css";
import pageHtml from "../../sleeptalk/_settings/index.html?raw";
import { parseSettingsRoute } from "./routes";
import { SetupChannel, unb64url } from "./setup_crypto";
import { validBrowserSignalUrl } from "./signal_url";

interface Session {
  browserSignalUrl: string;
  browserAccessKey: string;
}

interface Pending {
  parts: string[];
  received: number;
  status: number;
  contentType: string;
  resolve: (response: Response) => void;
  reject: (error: Error) => void;
}

const route = parseSettingsRoute(location.hash);
// The link leaves the address bar and history at once; the page then routes as usual.
history.replaceState(null, "", location.pathname + "#/speech");

// The host page's body: the same layout the host serves.
const parsed = new DOMParser().parseFromString(pageHtml, "text/html");
document.body.replaceChildren(...Array.from(parsed.body.childNodes).map((node) => document.importNode(node, true)));
document.title = parsed.title || "Sleeptalk settings";
const page = document.getElementById("page");

function showEnd(message: string): void {
  const note = document.createElement("p");
  note.className = "status error";
  note.textContent = message;
  page?.replaceChildren(note);
  document.getElementById("nav")?.replaceChildren();
}

let socket: WebSocket | null = null;
let channel: SetupChannel | null = null;
let nextId = 0;
let started = false;
const pending = new Map<number, Pending>();
const assets = new Map<string, Promise<string>>();

function send(type: string, payload: object): void {
  if (socket && socket.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify({ v: 1, id: crypto.randomUUID(), type, payload }));
  }
}

/** One request to the host's settings server, answered through the channel. */
async function hostFetch(path: string, init: RequestInit = {}): Promise<Response> {
  if (!channel) throw new Error("Not connected to your agent's computer.");
  const id = ++nextId;
  const method = (init.method || "GET").toUpperCase();
  const body = typeof init.body === "string" && init.body ? JSON.parse(init.body) : undefined;
  const answer = new Promise<Response>((resolve, reject) => {
    pending.set(id, { parts: [], received: 0, status: 0, contentType: "", resolve, reject });
  });
  const sealed = await channel.seal("settings.call", { id, method, path, body });
  send("setup.sealed", sealed);
  return answer;
}

function receive(reply: Record<string, unknown>): void {
  const id = reply.id;
  const call = typeof id === "number" ? pending.get(id) : undefined;
  if (!call || typeof reply.part !== "number" || typeof reply.parts !== "number" || typeof reply.data !== "string") return;
  if (reply.part === 0) {
    call.status = typeof reply.status === "number" ? reply.status : 502;
    call.contentType = typeof reply.content_type === "string" ? reply.content_type : "application/octet-stream";
  }
  if (call.parts[reply.part] === undefined) call.received += 1;
  call.parts[reply.part] = reply.data;
  if (call.received < reply.parts) return;
  pending.delete(id as number);
  const bytes = unb64(call.parts.join(""));
  call.resolve(new Response(bytes.slice().buffer, { status: call.status, headers: { "Content-Type": call.contentType } }));
}

function unb64(value: string): Uint8Array {
  const binary = atob(value);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
  return bytes;
}

/** An icon or voice sample from the host, as a blob URL this page can show or play. */
function hostUrl(path: string): Promise<string> {
  let url = assets.get(path);
  if (!url) {
    url = hostFetch(path).then(async (response) => {
      if (!response.ok) throw new Error("not found");
      return URL.createObjectURL(await response.blob());
    });
    url.catch(() => assets.delete(path));
    assets.set(path, url);
  }
  return url;
}

(window as unknown as { sleeptalkHost: unknown }).sleeptalkHost = { fetch: hostFetch, url: hostUrl, remote: true };

async function begin(routingId: string, token: string, hostKey: Uint8Array): Promise<void> {
  let session: Session;
  try {
    const response = await fetch(`/api/tickets/${routingId}/redeem`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ticket: token }),
      cache: "no-store",
    });
    if (response.status === 409) return showEnd("This settings link was already opened. Ask your agent for a new one.");
    if (response.status === 410) return showEnd("This settings link has expired. Ask your agent for a new one.");
    if (!response.ok) return showEnd("This settings link isn't valid. Ask your agent for a new one.");
    session = (await response.json()) as Session;
  } catch {
    return showEnd("Couldn't reach the call service. Check your connection and reopen the link.");
  }
  const signalUrl = validBrowserSignalUrl(session.browserSignalUrl);
  if (!signalUrl) return showEnd("This settings link isn't valid. Ask your agent for a new one.");
  socket = new WebSocket(signalUrl);
  const heartbeat = window.setInterval(() => send("ping", {}), 25_000);
  socket.addEventListener("open", () => send("call.auth", { role: "browser", key: session.browserAccessKey }));
  socket.addEventListener("message", (event) => {
    let envelope: { type?: unknown; payload?: unknown };
    try {
      envelope = JSON.parse(String(event.data));
    } catch {
      return;
    }
    const payload = envelope.payload && typeof envelope.payload === "object" ? (envelope.payload as Record<string, unknown>) : {};
    if (envelope.type === "peer.ready") {
      // A fresh channel each time both sides meet; answers owed on an old one never arrive.
      for (const call of pending.values()) call.reject(new Error("Reconnected; try that again."));
      pending.clear();
      void SetupChannel.open(hostKey, routingId).then(({ channel: opened, hello }) => {
        channel = opened;
        send("setup.hello", hello);
        if (!started) {
          started = true;
          // The host's own settings script, now talking through the channel.
          void import("../../sleeptalk/_settings/settings.js");
        }
      }, () => showEnd("This settings link isn't valid. Ask your agent for a new one."));
    }
    if (envelope.type === "setup.sealed" && channel) {
      void channel.open(payload).then((inner) => {
        if (inner && inner.type === "settings.reply") receive(inner.payload);
      });
    }
    if (envelope.type === "call.ended" || envelope.type === "call.cancel") {
      showEnd("Settings closed. Ask your agent for a new link if you need it again.");
    }
  });
  socket.addEventListener("close", () => {
    window.clearInterval(heartbeat);
    for (const call of pending.values()) call.reject(new Error("Settings closed."));
    pending.clear();
    showEnd("Settings closed. Ask your agent for a new link if you need it again.");
  });
}

if (!route) {
  showEnd("This settings link isn't complete. Open it again from your chat, or ask your agent for a new one.");
} else {
  const loading = document.createElement("p");
  loading.className = "loading";
  loading.textContent = "Connecting to your agent's computer…";
  page?.replaceChildren(loading);
  void begin(route.routingId, route.token, unb64url(route.hostKey));
}
