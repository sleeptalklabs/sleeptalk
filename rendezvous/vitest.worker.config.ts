import { cloudflareTest } from "@cloudflare/vitest-plugin";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [
    cloudflareTest({
      wrangler: { configPath: "./wrangler.jsonc" },
      miniflare: {
        bindings: {
          ALLOW_STUN_ONLY: "true",
          ENROLLMENT_HASH_KEY: "integration-enrollment-secret",
          ADMIN_TOKEN: "integration-admin-secret",
        },
      },
    }),
  ],
  test: {
    include: ["test/**/*.worker.test.ts"],
  },
});
