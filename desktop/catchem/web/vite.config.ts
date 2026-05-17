import { defineConfig } from "vite";
import path from "node:path";

// Catchem's webview is pointed at the FastAPI server (http://127.0.0.1:8087).
// This shim only exists to satisfy Tauri's frontendDist requirement during
// dev hand-off — if the sidecar takes a moment, the user sees the boot
// screen briefly before Tauri navigates to the real URL.
export default defineConfig({
  root: path.resolve(__dirname, "."),
  build: {
    outDir: path.resolve(__dirname, "dist"),
    emptyOutDir: true,
  },
});
