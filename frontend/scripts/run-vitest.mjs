#!/usr/bin/env node
import { spawn } from "node:child_process";
import process from "node:process";

const args = process.argv.slice(2);
const env = { ...process.env };

// Node 25 enables webstorage plumbing that can emit
// "`--localstorage-file` was provided without a valid path" from Vitest worker
// processes. jsdom provides the browser storage implementation for these tests,
// so disabling Node's experimental webstorage keeps test output signal-only.
const flag = "--no-experimental-webstorage";
if (process.allowedNodeEnvironmentFlags?.has(flag)) {
  env.NODE_OPTIONS = [env.NODE_OPTIONS, flag].filter(Boolean).join(" ");
}

const bin = process.platform === "win32" ? "vitest.cmd" : "vitest";
const child = spawn(bin, args, {
  cwd: process.cwd(),
  env,
  shell: process.platform === "win32",
  stdio: "inherit",
});

child.on("exit", (code, signal) => {
  if (signal) {
    process.kill(process.pid, signal);
    return;
  }
  process.exit(code ?? 1);
});
