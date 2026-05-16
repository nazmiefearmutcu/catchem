import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "node:path";

// Vite bundles into the FastAPI package's static dir so the Python app can
// serve / and /assets/* without a separate frontend process in production.
const BUILD_TARGET = path.resolve(__dirname, "../src/fusion_stack/static/app");

export default defineConfig({
  plugins: [react()],
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
    },
  },
  build: {
    outDir: BUILD_TARGET,
    emptyOutDir: true,
    sourcemap: false,
    target: "es2022",
    rollupOptions: {
      output: {
        manualChunks: {
          react: ["react", "react-dom", "react-router-dom"],
          query: ["@tanstack/react-query", "@tanstack/react-table"],
          charts: ["echarts", "echarts-for-react"],
        },
      },
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/tests/setup.ts"],
  },
});
