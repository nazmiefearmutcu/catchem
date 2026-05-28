#!/usr/bin/env node
/**
 * Bundle size budget enforcement.
 *
 * Run after `npm run build` (or chain via `npm run build:check`). Walks the
 * emitted JS assets, gzips each, and compares against a hand-tuned budget
 * derived from the current production bundle. Any chunk that crosses its
 * budget exits non-zero so CI and manual builds fail fast on regressions.
 *
 * Budgets are in **gzipped bytes** — that is what the browser actually
 * downloads. Raw sizes are deceiving; gzip ratios for our React + ECharts
 * surface area sit around 3-4×.
 *
 * The project is ESM (`"type": "module"`), so this file uses `import` and
 * derives `__dirname` from `import.meta.url`.
 */

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { gzipSync } from "node:zlib";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

// Vite's BUILD_TARGET is `../src/catchem/static/app`; the JS chunks land in
// the `assets/` subdir alongside the CSS.
const ASSETS_DIR = path.resolve(__dirname, "../../src/catchem/static/app/assets");

// Prefix → gzipped-byte ceiling. We match by `file.startsWith(prefix)` so
// rollup's content-hash suffixes (e.g. `index-DfH7K.js`) are tolerated.
// Longest prefixes are checked first so `QuantScanPage-` wins over `Page-`.
const BUDGETS = {
  // Entry shell — keep it lean; this is the critical path.
  "index-": 75_000,
  // Heaviest analyst page (echarts heatmap + scatter + ticker).
  "QuantScanPage-": 50_000,
  // Mid-weight pages.
  "FeedPage-": 30_000,
  "ReplayUploadPage-": 30_000,
  "BenchmarkPage-": 20_000,
  // Default ceiling for every remaining route-level chunk.
  "Page-": 20_000,
  // Vendor splits emitted by `manualChunks` in vite.config.ts.
  "react-": 60_000,
  "query-": 30_000,
  // ECharts async chunk renamed via `chunkFileNames` in vite.config.ts.
  // The chart surface is genuinely large (heatmap, scatter, line, sankey,
  // bar, tree) so we budget for what we ship today plus headroom.
  "echarts-": 360_000,
};

// Sort prefixes by descending length so the most specific budget wins.
// `Page-` is special: rollup names every route chunk `<RouteName>Page-<hash>.js`,
// so we treat it as a *suffix-anchored* fallback after specific page names.
const PREFIXES = Object.entries(BUDGETS).sort((a, b) => b[0].length - a[0].length);

function matchBudget(filename) {
  for (const [prefix, budget] of PREFIXES) {
    if (prefix === "Page-") continue; // handled below as a fallback
    if (filename.startsWith(prefix)) return { prefix, budget };
  }
  // Fallback: anything matching `*Page-<hash>.js` falls under the generic
  // page budget. This covers all the route chunks that don't have a
  // hand-tuned ceiling above.
  const pageFallback = BUDGETS["Page-"];
  if (pageFallback !== undefined && /Page-[A-Za-z0-9_-]+\.js$/.test(filename)) {
    return { prefix: "Page-", budget: pageFallback };
  }
  return null;
}

if (!fs.existsSync(ASSETS_DIR)) {
  console.error(`\nassets dir not found: ${ASSETS_DIR}`);
  console.error("Run `npm run build` first.\n");
  process.exit(2);
}

const files = fs
  .readdirSync(ASSETS_DIR)
  .filter((f) => f.endsWith(".js"))
  .sort();

let violations = 0;
let measured = 0;

console.log("");
console.log("Bundle budget check — sizes are gzipped (what users actually download)");
console.log("─".repeat(96));

for (const file of files) {
  const raw = fs.readFileSync(path.join(ASSETS_DIR, file));
  const gzipped = gzipSync(raw).length;

  const match = matchBudget(file);
  if (!match) {
    // No budget configured for this prefix — show but don't gate on it.
    console.log(
      `   ${file.padEnd(58)} ${(gzipped / 1024).toFixed(1).padStart(7)} KB   (no budget)`
    );
    continue;
  }

  measured += 1;
  const { budget } = match;
  const pctUsed = (gzipped / budget) * 100;
  const overBudget = gzipped > budget;
  const status = overBudget ? "FAIL" : pctUsed > 90 ? "WARN" : "OK  ";
  if (overBudget) violations += 1;

  console.log(
    `${status.padEnd(4)} ${file.padEnd(58)} ${(gzipped / 1024)
      .toFixed(1)
      .padStart(7)} KB / ${(budget / 1024).toFixed(0).padStart(3)} KB (${pctUsed
      .toFixed(0)
      .padStart(3)}%)`
  );
}

console.log("─".repeat(96));
console.log(`${files.length} JS chunk(s) emitted, ${measured} measured against budget.`);

if (violations > 0) {
  console.error(
    `\n${violations} chunk(s) exceeded budget. Run \`npm run analyze\` to investigate.\n`
  );
  process.exit(1);
} else {
  console.log("All chunks within budget.\n");
}
