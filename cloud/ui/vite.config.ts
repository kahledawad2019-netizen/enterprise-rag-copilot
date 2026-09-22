import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

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
    // In development the Worker runs on 8787 via `wrangler dev`. Proxying
    // means the browser sees one origin, so the CORS path is exercised in
    // production only - which is where it matters and where it is configured.
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8787",
        changeOrigin: true,
      },
    },
  },
});
