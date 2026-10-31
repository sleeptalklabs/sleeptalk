import path from "node:path";
import { pathToFileURL } from "node:url";
import permissions from "./pi_permission_extension.js";

const TOOLS = ["read", "bash", "edit", "write", "grep", "glob"];

export async function createProtectedSession(root, cwd, provider, modelId, ui) {
  if (!["pending", "deny"].includes(process.env.SLEEPTALK_PERMISSION_MODE)
      || !["openai", "anthropic"].includes(provider) || !modelId) throw new Error("Unsupported OMP policy");
  const load = (name) => import(pathToFileURL(path.join(root, "src", name)).href);
  const [{ Settings }, { ModelRegistry }, { AuthStorage }, { SessionManager }, { createAgentSession }, { initializeExtensions }] = await Promise.all([
    load("config/settings.ts"), load("config/model-registry.ts"), load("session/auth-storage.ts"),
    load("session/session-manager.ts"), load("sdk.ts"), load("modes/runtime-init.ts"),
  ]);
  const settings = Settings.isolated({
    "memory.backend": "off", "memories.enabled": false, "advisor.enabled": false,
    "generate_image.enabled": false, "speechgen.enabled": false, "plan.enabled": false,
    "startup.checkUpdate": false, "secrets.enabled": false, "task.eager": false,
  });
  const auth = await AuthStorage.create(":memory:");
  const apiKey = process.env[provider === "openai" ? "OPENAI_API_KEY" : "ANTHROPIC_API_KEY"];
  if (apiKey) {
    // 18.10 moved runtime overrides into the auth key facade. Keep the
    // previously qualified 18.2 API until its compatibility window ends.
    if (typeof auth.keys?.setRuntime === "function") auth.keys.setRuntime(provider, apiKey);
    else auth.setRuntimeApiKey(provider, apiKey);
  }
  const home = process.env.HOME;
  const registry = new ModelRegistry(auth, path.join(home, "models.yml"), {
    ignoreLocalModelConfig: true, settings, cacheDbPath: path.join(home, "models.db"),
    fetch: () => Promise.reject(new Error("Background model discovery disabled")),
  });
  const model = registry.find(provider, modelId);
  if (!model) { auth.close(); throw new Error("Selected native model is unavailable in the pinned SDK catalog"); }
  const result = await createAgentSession({
    cwd, agentDir: process.env.PI_CODING_AGENT_DIR, settings, authStorage: auth, modelRegistry: registry, model,
    sessionManager: SessionManager.inMemory(cwd), enableMCP: false, enableLsp: false, enableIrc: false,
    skipPythonPreflight: true, hasUI: true, interactivePrompts: false, toolNames: TOOLS,
    preloadedCustomToolPaths: [], preloadedExtensionPaths: [], disableExtensionDiscovery: true,
    extensions: [permissions], skills: [], rules: [], contextFiles: [], promptTemplates: [], slashCommands: [],
    autoApprove: false,
  });
  result.session.setActiveToolsByName(TOOLS);
  const enabled = result.session.getEnabledToolNames();
  if (enabled.length !== TOOLS.length || enabled.some(name => !TOOLS.includes(name))) {
    await result.session.dispose(); auth.close(); throw new Error("SDK tool policy mismatch");
  }
  result.setToolUIContext(ui, true);
  let extensionFailed = false;
  await initializeExtensions(result.session, {
    mode: "rpc", uiContext: ui,
    reportSendError: () => { extensionFailed = true; },
    reportRuntimeError: () => { extensionFailed = true; },
  });
  if (extensionFailed) { await result.session.dispose(); auth.close(); throw new Error("Required permission extension failed"); }
  return { ...result, auth };
}

async function* frames(stream) {
  let pending = Buffer.alloc(0);
  for await (const chunk of stream) {
    pending = Buffer.concat([pending, chunk]);
    let end;
    while ((end = pending.indexOf(10)) >= 0) {
      if (end > 65536) throw new Error("Oversized RPC frame");
      yield pending.subarray(0, end).toString("utf8");
      pending = pending.subarray(end + 1);
    }
    if (pending.length > 65536) throw new Error("Oversized RPC frame");
  }
  if (pending.length) throw new Error("Incomplete RPC frame");
}

export async function serve(root, cwd, provider, modelId) {
  const output = (frame) => process.stdout.write(JSON.stringify(frame) + "\n");
  const waiting = new Map();
  let counter = 0;
  let acknowledged = false;
  const ui = {
    setStatus(key, text) {
      if (key !== "sleeptalk-permissions") return;
      const proof = JSON.parse(text);
      if (proof.nonce !== process.env.SLEEPTALK_PERMISSION_NONCE || proof.mode !== process.env.SLEEPTALK_PERMISSION_MODE || path.resolve(proof.workspace) !== path.resolve(cwd)) return;
      acknowledged = true;
      output({ type: "extension_ui_request", method: "setStatus", statusKey: key, statusText: text });
    },
    async confirm(_title, message, options = {}) {
      if (process.env.SLEEPTALK_ONBOARDING_PROBE === "1") return false;
      const id = `omp-permission-${++counter}`;
      return new Promise((resolve) => {
        const finish = (answer) => {
          clearTimeout(timer); options.signal?.removeEventListener("abort", deny);
          waiting.delete(id); resolve(answer === true);
        };
        const deny = () => finish(false);
        const timer = setTimeout(deny, 120000);
        waiting.set(id, finish);
        options.signal?.addEventListener("abort", deny, { once: true });
        if (options.signal?.aborted) deny();
        else output({ type: "extension_ui_request", id, method: "confirm", message });
      });
    },
    setWidget() {}, setWorkingMessage() {}, setTitle() {}, notify() {},
    select: async () => undefined, input: async () => undefined, editor: async () => undefined,
    getEditorText: () => "", setEditorText() {}, getToolsExpanded: () => false, setToolsExpanded() {},
  };
  const { session, auth } = await createProtectedSession(root, cwd, provider, modelId, ui);
  if (!acknowledged) { await session.dispose(); auth.close(); throw new Error("Required permission extension did not attest startup"); }
  const subscription = session.subscribe(output);
  let active;
  const response = (request, success, data = {}) => output({ type: "response", id: request.id, command: request.type, success, ...(success ? { data } : { error: "Protected OMP request was rejected" }) });

  const cancel = async () => { for (const finish of [...waiting.values()]) finish(false); await session.abort(); };
  const shutdown = async () => {
    process.stdin.destroy(); await cancel(); await active?.catch(() => {}); subscription(); await session.dispose(); auth.close();
  };
  process.once("SIGTERM", () => { void shutdown().finally(() => process.exit(0)); });
  try {
    for await (const line of frames(process.stdin)) {
      if (line.length > 65536) throw new Error("Oversized RPC frame");
      let request;
      try { request = JSON.parse(line); } catch { throw new Error("Invalid RPC frame"); }
      if (!request || typeof request !== "object" || Array.isArray(request)) throw new Error("Invalid RPC frame");
      if (request.type === "get_state") {
        response(request, true, { model: { id: session.model.id, provider: session.model.provider }, sessionId: session.sessionId,
          isStreaming: !!active || session.isStreaming, isCompacting: false, pendingMessageCount: session.queuedMessageCount });
      } else if (process.env.SLEEPTALK_ONBOARDING_PROBE === "1") {
        response(request, false);
      } else if (request.type === "extension_ui_response") {
        waiting.get(request.id)?.(request.confirmed === true && request.cancelled !== true);
      } else if (request.type === "abort") {
        await cancel(); response(request, true);
      } else if (request.type === "set_subagent_subscription") {
        response(request, true, { level: "off" });
      } else if (request.type === "prompt" && typeof request.message === "string" && !active) {
        // Admission precedes streaming. Waiting for prompt() to finish here
        // makes the client buffer the entire turn, including tool progress.
        response(request, true);
        active = session.prompt(request.message, { expandPromptTemplates: false }).catch(
          () => response(request, false),
        ).finally(() => { active = undefined; output({ type: "agent_settled" }); });
      } else response(request, false);
    }
  } finally { await shutdown(); }
}

if (import.meta.main) {
  serve(...process.argv.slice(2)).catch(() => { process.stderr.write("Isolated OMP SDK startup or protocol failed\n"); process.exitCode = 1; });
}
