import { describe, expect, it } from "vitest";

import workerSource from "../src/worker.ts?raw";
import html from "../web/index.html?raw";
import browserSource from "../web/main.ts?raw";
import pythonProviderSource from "../../sleeptalk/call_provider.py?raw";

describe("privacy invariants", () => {
  it("never puts ticket or access credentials in request or WebSocket URLs", () => {
    const source = [workerSource, browserSource, pythonProviderSource].join("\n");

    expect(source).not.toMatch(/[?&](?:key|token|ticket)=/i);
    expect(source).not.toContain("/api/tickets/${token}/redeem");
    expect(source).not.toMatch(/searchParams\.set\(["'](?:key|token|ticket)["']/i);
    expect(source).toContain('"call.auth"');
  });

  it("keeps the call page self-hosted", () => {
    expect(html).not.toMatch(/<(?:script|link)[^>]+https?:\/\//i);
    expect(html).not.toMatch(/analytics|googletagmanager|fonts\.googleapis/i);
  });
});
