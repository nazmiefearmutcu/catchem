import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "node:path";
import { visualizer } from "rollup-plugin-visualizer";

// Vite bundles into the FastAPI package's static dir so the Python app can
// serve / and /assets/* without a separate frontend process in production.
const BUILD_TARGET = path.resolve(__dirname, "../src/catchem/static/app");

// `ANALYZE=true npm run build` (or `npm run analyze`) opt-in: emits a
// treemap of every chunk with gzip + brotli sizes so we can investigate
// regressions surfaced by `npm run build:check`.
const ANALYZE = process.env.ANALYZE === "true" || process.env.ANALYZE === "1";

export default defineConfig({
  plugins: [
    react(),
    ANALYZE &&
      visualizer({
        filename: "bundle-stats.html",
        open: true,
        gzipSize: true,
        brotliSize: true,
        template: "treemap",
      }),
  ].filter(Boolean),
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "src"),
    },
  },
  server: {
    port: 5173,
    proxy: {
      // Dev mode: vite serves the UI, FastAPI handles all API + /legacy.
      "/healthz": "http://127.0.0.1:8087",
      "/config": "http://127.0.0.1:8087",
      "/metrics": "http://127.0.0.1:8087",
      "/recent": "http://127.0.0.1:8087",
      "/dashboard": "http://127.0.0.1:8087",
      "/record": "http://127.0.0.1:8087",
      "/records": "http://127.0.0.1:8087",
      "/replay": "http://127.0.0.1:8087",
      "/process-one": "http://127.0.0.1:8087",
      "/legacy": "http://127.0.0.1:8087",
      "/ui": {
        target: "http://127.0.0.1:8087",
        changeOrigin: false,
      },
      // Reviews + future API surfaces live under /api/. Without this
      // entry, vite's catch-all rewrites the request to index.html and
      // useQuery sees the SPA shell instead of the JSON body.
      "/api": {
        target: "http://127.0.0.1:8087",
        changeOrigin: false,
      },
    },
  },
  build: {
    outDir: BUILD_TARGET,
    emptyOutDir: true,
    sourcemap: false,
    target: "es2022",
    // echarts (+zrender) is ~1 MB and is the single largest dependency. It is
    // already isolated into its OWN lazy async chunk (see the manualChunks note
    // + chunkFileNames below) so it never lands in the eager entry bundle — it
    // only downloads when a chart first renders. The default 500 kB warning
    // therefore fires on a chunk that is intentionally large AND lazy, which is
    // false-alarm noise on every build. Raise the threshold to just above the
    // echarts chunk so the warning only re-appears if the EAGER bundle bloats
    // (a real regression) rather than on the known, deferred charting vendor.
    chunkSizeWarningLimit: 1100,
    rollupOptions: {
      output: {
        manualChunks: {
          react: ["react", "react-dom", "react-router-dom"],
          query: ["@tanstack/react-query", "@tanstack/react-table"],
          // `echarts` + `echarts-for-react` are loaded lazily from
          // src/charts/EChart.tsx via dynamic import(), so we let rollup
          // emit the async chunk naturally instead of hoisting it into
          // an eagerly-preloaded manualChunk. Keeping them in manualChunks
          // caused page chunks to bare-import the charts bundle and the
          // entry HTML to <link rel="modulepreload"> it on every load.
        },
        // The echarts async chunk would otherwise inherit the name `index`
        // from `node_modules/echarts/index.js`, colliding with the entry
        // chunk and confusing the bundle-budget matcher. Rename any async
        // chunk that drags in an echarts module into the `echarts-` prefix
        // so prefix-based budgets stay unambiguous.
        chunkFileNames: (chunkInfo) => {
          const hasEcharts = (chunkInfo.moduleIds ?? []).some(
            (id) =>
              id.includes("/node_modules/echarts") ||
              id.includes("/node_modules/zrender")
          );
          if (hasEcharts) return "assets/echarts-[hash].js";
          return "assets/[name]-[hash].js";
        },
      },
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/tests/setup.ts"],
    coverage: {
      provider: "v8",
      reporter: ["text", "html"],
      include: ["src/**/*.{ts,tsx}"],
      exclude: [
        "src/tests/**",
        "src/main.tsx",
        "src/**/*.d.ts",
        "src/**/__mocks__/**",
      ],
    },
  },
});
