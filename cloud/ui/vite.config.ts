import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

/**
 * Three ways to run the front end, chosen by environment variable.
 *
 *   (default)                      -> the mock on :8787, `node mock/server.mjs`
 *   COPILOT_API=http://…:8787      -> `wrangler dev`, the real Worker
 *   COPILOT_API=http://…:8000      -> the FastAPI backend, no Worker
 *
 * The last one needs BACKEND_TOKEN, because the backend refuses to serve
 * without it - that refusal is what keeps the container private in
 * production, so it is not something to switch off for convenience. The
 * Worker normally supplies the header; talking to the backend directly means
 * the dev server has to.
 *
 * The path also differs: the Worker answers on /api/*, the backend on /*.
 */
const BACKEND = process.env.COPILOT_API ?? "http://127.0.0.1:8787";
const TOKEN = process.env.BACKEND_TOKEN;
// A non-default local port is useful when another project process already
// owns :8787. Make the proxy mode explicit instead of guessing solely from a
// port number and accidentally stripping `/api` from Worker requests.
const TALKING_TO_WORKER =
  process.env.COPILOT_API_KIND === "worker" ||
  (process.env.COPILOT_API_KIND !== "backend" && BACKEND.includes("8787"));

export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "dist",
    // Pages serves this from its CDN and the app is small enough that Vite's
    // default chunking is already one vendor chunk plus the app. Hand-tuning
    // `manualChunks` here bought nothing and no longer typechecks under
    // Rollup 5, which takes a function rather than a map.
    sourcemap: false,
  },
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: BACKEND,
        changeOrigin: true,
        rewrite: TALKING_TO_WORKER ? undefined : (path) => path.replace(/^\/api/, ""),
        headers: TOKEN ? { Authorization: `Bearer ${TOKEN}` } : undefined,
      },
    },
  },
});
