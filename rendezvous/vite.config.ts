import { defineConfig } from "vite";

export default defineConfig({
  root: "web",
  build: {
    // One built browser artifact serves both the hosted Worker and the
    // package-owned localhost rendezvous shipped in the Python wheel. The
    // path is relative to `root` (web/), so this is the repository's
    // sleeptalk/_web, the directory the wheel and wrangler both read.
    outDir: "../../sleeptalk/_web",
    emptyOutDir: true,
    rollupOptions: {
      // The call and key-link page, and the settings page for a phone.
      input: { index: "web/index.html", settings: "web/settings.html" },
    },
  },
});
