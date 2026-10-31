// Only the HTTP provider is fake. Session, tools, hooks and RPC are upstream.
import assert from "node:assert/strict";
import { appendFileSync, existsSync, writeFileSync } from "node:fs";
import { pathToFileURL } from "node:url";

const [root, cwd, provider, model, launcher] = process.argv.slice(2);
let serial = 0;
const history = new Map();
globalThis.fetch = async (url, options = {}) => {
  const address = url instanceof Request ? url.url : String(url);
  assert.equal(address, "https://api.openai.com/v1/responses");
  const headers = new Headers(options.headers);
  assert.equal(headers.get("authorization"), "Bearer synthetic-key");
  const body = JSON.parse(options.body);
  appendFileSync(`${cwd}/requests.jsonl`, JSON.stringify(body) + "\n");
  const input = [...(history.get(body.previous_response_id) || []), ...body.input];
  const lastUser = input.findLastIndex(item => item.role === "user");
  const prompt = JSON.stringify(input[lastUser]);
  if (prompt.includes("reject-provider-request")) {
    return Response.json({ error: { message: "Synthetic invalid key", type: "authentication_error" } }, { status: 401 });
  }
  const toolDone = input.slice(lastUser + 1).some(item => item.type === "function_call_output");
  const id = `resp_${++serial}`;
  let stopped = false;
  const stream = new ReadableStream({
    async start(controller) {
      const send = (type, fields) => {
        if (!stopped) controller.enqueue(new TextEncoder().encode(`event: ${type}\ndata: ${JSON.stringify({ type, ...fields })}\n\n`));
      };
      const abort = () => { if (!stopped) { stopped = true; controller.close(); } };
      options.signal?.addEventListener("abort", abort, { once: true });
      try {
        send("response.created", { response: { id, status: "in_progress", output: [] } });
        let item;
        if (prompt.includes("exercise-tool") && !toolDone) {
          const args = JSON.stringify({ path: `${cwd}/tool-proof.txt`, content: "approved" });
          item = { type: "function_call", id: "fc_write", call_id: "call_write", name: "write", arguments: "" };
          send("response.output_item.added", { output_index: 0, item });
          send("response.function_call_arguments.delta", { output_index: 0, item_id: item.id, delta: args });
          item = { ...item, arguments: args, status: "completed" };
        } else {
          item = { type: "message", id: `msg_${serial}`, role: "assistant", content: [], status: "in_progress" };
          send("response.output_item.added", { output_index: 0, item });
          send("response.content_part.added", { output_index: 0, item_id: item.id, content_index: 0,
            part: { type: "output_text", text: "", annotations: [] } });
          send("response.output_text.delta", { output_index: 0, item_id: item.id, content_index: 0, delta: "First sentence. " });
          if (prompt.includes("stream-before-finish")) {
            const deadline = Date.now() + 5000;
            while (!existsSync(`${cwd}/release-stream`) && !stopped && Date.now() < deadline) await Bun.sleep(5);
            assert.ok(existsSync(`${cwd}/release-stream`), "Sleeptalk must stream before the model finishes");
          }
          if (prompt.includes("hang-until-abort")) {
            writeFileSync(`${cwd}/model-hanging`, "1");
            while (!stopped) await Bun.sleep(5);
            return;
          }
          send("response.output_text.delta", { output_index: 0, item_id: item.id, content_index: 0, delta: "Second sentence." });
          item = { ...item, status: "completed", content: [{ type: "output_text", text: "First sentence. Second sentence.", annotations: [] }] };
        }
        send("response.output_item.done", { output_index: 0, item });
        history.set(id, [...input, item]);
        send("response.completed", { response: { id, status: "completed", output: [item],
          usage: { input_tokens: 12, output_tokens: 8, total_tokens: 20 } } });
        if (!stopped) { stopped = true; controller.close(); }
      } catch (error) {
        writeFileSync(`${cwd}/fixture-failure`, String(error));
        if (!stopped) { stopped = true; controller.error(error); }
      } finally { options.signal?.removeEventListener("abort", abort); }
    },
    cancel() { stopped = true; },
  });
  return new Response(stream, { headers: { "Content-Type": "text/event-stream" } });
};
const { serve } = await import(pathToFileURL(launcher).href);
await serve(root, cwd, provider, model);
