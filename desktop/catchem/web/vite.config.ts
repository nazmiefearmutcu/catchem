/// <reference types="vitest" />
import { defineConfig } from "vite";
import path from "node:path";

// Catchem's webview loads boot.js first (5-stage state machine) and then
// hands the window to the FastAPI-served React UI once /healthz answers.
// boot.js is also imported by boot.test.js — Vitest reuses this same
// config so the test environment matches what production loads.
export default defineConfig({
  root: path.resolve(__dirname, "."),
  build: {
    outDir: path.resolve(__dirname, "dist"),
    emptyOutDir: true,
  },
  test: {
    environment: "jsdom",
    include: ["**/*.test.{js,ts}"],
    // Boot shim has zero network — every poll either hits the mocked fetch
    // or the deadline. Capping individual tests at 5 s catches infinite
    // loops; the actual asserts complete in <50 ms.
    testTimeout: 5_000,
  },
});
