// Uses the documented Pi/OMP pre-execution tool_call hook and RPC UI protocol.
export async function validateModel(ctx, fetchModel = globalThis.fetch) {
  const model = ctx.model;
  const registry = ctx.modelRegistry;
  const origins = { openai: "https://api.openai.com", anthropic: "https://api.anthropic.com" };
  if (!model || !origins[model.provider] || typeof model.id !== "string") return "unknown";
  const acceptsBase = (value) => {
    try {
      const url = new URL(value);
      const expectedPath = model.provider === "openai" ? "/v1" : "";
      return url.origin === origins[model.provider] && url.pathname.replace(/\/$/, "") === expectedPath
        && !url.username && !url.password && !url.search && !url.hash;
    } catch { return false; }
  };
  if (!acceptsBase(model.baseUrl) || typeof registry?.isUsingOAuth !== "function") return "unknown";
  if (registry.isUsingOAuth(model)) return "unknown";
  try {
    let auth;
    if (typeof registry.getApiKeyAndHeaders === "function") {
      auth = await registry.getApiKeyAndHeaders(model);
      if (!auth?.ok) return "unavailable";
    } else if (typeof registry.getApiKey === "function") {
      auth = { apiKey: await registry.getApiKey(model), headers: model.headers };
    } else return "unknown";
    if (auth.baseUrl && !acceptsBase(auth.baseUrl)) return "unknown";
    if (auth.apiKey == null || auth.apiKey === "") return "unavailable";
    if (typeof auth.apiKey !== "string") return "unknown";
    const headers = new Headers(auth.headers || {});
    if (model.provider === "anthropic") {
      headers.set("x-api-key", auth.apiKey);
      headers.set("anthropic-version", "2023-06-01");
    } else headers.set("Authorization", `Bearer ${auth.apiKey}`);
    const response = await fetchModel(`${origins[model.provider]}/v1/models/${encodeURIComponent(model.id)}`, {
      method: "GET", headers, redirect: "error", signal: AbortSignal.timeout(8000),
    });
    if (response.status !== 200) {
      await response.body?.cancel();
      return response.status === 401 ? "unavailable" : "unknown";
    }
    const reader = response.body?.getReader();
    if (!reader) return "unknown";
    let size = 0;
    const chunks = [];
    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        size += value.length;
        if (size > 32768) return "unknown";
        chunks.push(value);
      }
      const text = new TextDecoder().decode(Buffer.concat(chunks));
      const data = JSON.parse(text);
      if (model.provider === "openai") return data.object === "model" && data.id === model.id ? "available" : "unknown";
      return data.type === "model" && typeof data.id === "string" && data.id ? "available" : "unknown";
    } finally { await reader.cancel(); }
  } catch { return "unknown"; }
}

export default function sleeptalkPermissions(pi) {
  const mode = process.env.SLEEPTALK_PERMISSION_MODE;
  const nonce = process.env.SLEEPTALK_PERMISSION_NONCE;
  if (!["pending", "deny"].includes(mode) || !nonce) throw new Error("Missing Sleeptalk permission policy");
  pi.on("tool_call", async (event, ctx) => {
    const blocked = { block: true, reason: "Sleeptalk permission was not granted" };
    if (mode === "deny" || ctx.signal?.aborted) return blocked;
    let timer;
    try {
      const tool = typeof event.toolName === "string" && /^[A-Za-z][A-Za-z0-9_.:-]{0,63}$/.test(event.toolName) ? event.toolName : "agent tool";
      const prompt = `Allow the pending ${tool} operation?`;
      const allowed = await Promise.race([
        ctx.ui.confirm("Sleeptalk tool permission", prompt, { timeout: 120000, signal: ctx.signal }),
        new Promise((resolve) => { timer = setTimeout(() => resolve(false), 120000); }),
      ]);
      return allowed === true && !ctx.signal?.aborted ? undefined : blocked;
    } catch { return blocked; }
    finally { clearTimeout(timer); }
  });
  pi.on("session_start", async (_event, ctx) => {
    const login = process.env.SLEEPTALK_ONBOARDING_PROBE === "1" ? await validateModel(ctx) : "unknown";
    ctx.ui.setStatus("sleeptalk-permissions", JSON.stringify({ version: 1, nonce, mode, workspace: ctx.cwd, model: ctx.model?.id, provider: ctx.model?.provider, login }));
  });
}
