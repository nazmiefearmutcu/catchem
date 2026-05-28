/**
 * Page-specific contextual help content (v23, task #85).
 *
 * Keyed by `useLocation().pathname`. The HelpDrawer component picks the
 * matching entry — exact-match first, then prefix-match for dynamic
 * segments (e.g. `/feed/abc-123` falls back to `/feed`).
 *
 * Keep entries tight: 3-5 quickTips, 0-3 question pairs, and a small set
 * of page-relevant shortcuts (we don't repeat the full NAV_SHORTCUTS
 * registry — the floating-? drawer is for context, the `?` overlay is
 * the canonical shortcut surface).
 */
import { NAV_SHORTCUTS, chordLabel } from "@/lib/nav-shortcuts";

export interface HelpQA {
  q: string;
  a: string;
}

export interface HelpShortcut {
  key: string;
  description: string;
}

export interface HelpContent {
  quickTips: string[];
  questions: HelpQA[];
  shortcuts: HelpShortcut[];
}

function navShortcutFor(path: string, description: string): HelpShortcut[] {
  const spec = NAV_SHORTCUTS.find((s) => s.path === path);
  if (!spec) return [];
  return [{ key: chordLabel(spec), description }];
}

function normalizePath(pathname: string): string {
  if (pathname.length > 1) return pathname.replace(/\/+$/, "");
  return pathname;
}

export const PAGE_HELP: Record<string, HelpContent> = {
  "/": {
    quickTips: [
      "The hero shows DeepSeek's live cross-asset narrative — auto-refreshes every 60s",
      "Click any 'recent relevant' record to open the full detail drawer",
      "5 KPI cards = your snapshot view; click distribution rows to filter Feed",
    ],
    questions: [
      {
        q: "Why is benchmark F1 at 100%?",
        a: "Stubs are deterministic — they produce 100% on the synthetic golden set. Switch to real ML with --with-ml bootstrap.",
      },
      {
        q: "Where does the live-read narrative come from?",
        a: "DeepSeek synthesis if API key is set, else local stub synthesis. See Settings → DeepSeek reviewer.",
      },
    ],
    shortcuts: [
      ...navShortcutFor("/", "Stay on Overview"),
      ...navShortcutFor("/feed", "Jump to Live Feed"),
    ],
  },
  "/feed": {
    quickTips: [
      "Press the bell in the live status bar to enable arrival toasts for high-relevance items",
      "Use sidebar filters to drill into asset class, reason code, or symbol",
      "The 'pub→ingest' metric shows publisher-side lag (RSS doesn't publish on a schedule)",
    ],
    questions: [
      {
        q: "Why is news poller 'degraded'?",
        a: "≥1 source returned an error on the last fetch. Click 'poll now' to retry. RSS feeds occasionally rate-limit.",
      },
      {
        q: "What's the relevance threshold?",
        a: "Default 0.65. Adjust via the ≥X.XX chip in the alert section. Empirical max scorer output ≈ 0.80.",
      },
    ],
    shortcuts: [
      ...navShortcutFor("/feed", "You're here"),
      { key: "/", description: "Focus the search input" },
      { key: "Esc", description: "Close record drawer" },
    ],
  },
  "/replay": {
    quickTips: [
      "Paste an article URL or full text in the Replay tab — Catchem extracts + scores it",
      "Upload tab accepts .txt, .html, .json files (max ~5MB)",
      "Replay results land in the Feed and persist to the SQLite store",
    ],
    questions: [
      {
        q: "Can I replay an entire JSONL dump?",
        a: "Yes — use the 'replay' mode (not paste/upload). It tails the upstream Awareness JSONL via persisted offsets, idempotent across restarts.",
      },
    ],
    shortcuts: [...navShortcutFor("/replay", "Jump to Replay / Upload")],
  },
  "/map": {
    quickTips: [
      "Click any heatmap cell to drill into the underlying records",
      "Regime shifts mark sudden KL-divergence in topic distribution over 5-minute buckets",
      "The trend line is stacked bar — total volume per bucket",
    ],
    questions: [
      {
        q: "Why is the regime shift count 0?",
        a: "Either: no shift detected (calm market), or bucket gating threshold filtered them out. Check the 'bucket gating' stat for current threshold.",
      },
    ],
    shortcuts: [...navShortcutFor("/map", "You're here")],
  },
  "/symbols": {
    quickTips: [
      "Top-3 hero shows news concentration — a top-3 = 70%+ indicates a dominant single ticker story",
      "Click any symbol pill to open the symbol detail page",
      "'Market quote context' is fixture data, not live prices",
    ],
    questions: [
      {
        q: "Why does my symbol show 'quote unavailable'?",
        a: "Local fixture provider doesn't have a snapshot for that ticker. Configure a real quote provider in Settings to attach prices.",
      },
    ],
    shortcuts: [...navShortcutFor("/symbols", "You're here")],
  },
  "/tags": {
    quickTips: [
      "User-defined tags are analyst memos — independent of pipeline labels like asset class or reason code",
      "Add tags from the record drawer's 'add tag' input (lowercase, no spaces, ≤ 50 chars)",
      "Click any chip in the cloud or row in the table to jump to a tag-filtered Feed view",
    ],
    questions: [
      {
        q: "Why is my new tag not showing up?",
        a: "The aggregate refreshes every 60s. Force a reload, or wait — tags added in the drawer surface here on the next poll.",
      },
      {
        q: "How big can a tag vocabulary get?",
        a: "The page lists the top 200. Above that, less-used tags fall off — collapse old ones into broader buckets if you hit the cap.",
      },
    ],
    shortcuts: [...navShortcutFor("/tags", "You're here")],
  },
  "/benchmark": {
    quickTips: [
      "Re-run executes the golden benchmark (12 items, CPU stubs) — synchronous, cheap",
      "Sparklines show per-metric trajectory across recent runs",
      "Run-over-run delta is in percentage POINTS (pp), not relative",
    ],
    questions: [
      {
        q: "How do I add more golden items?",
        a: "Edit src/catchem/golden.py — add to the SYNTHETIC list with expected labels.",
      },
    ],
    shortcuts: [...navShortcutFor("/benchmark", "You're here")],
  },
  "/backtest": {
    quickTips: [
      "Backtest grades stub predictions against DeepSeek's review as ground truth",
      "Calibration plot: how close is 'avg predicted' to 'avg actual' inside each quintile?",
      "Mean abs error is on the same [0,1] axis — read 0.08 as '8 points off'",
    ],
    questions: [
      {
        q: "Why is the result empty?",
        a: "No paired (stub, DeepSeek) reviews yet. Enable DeepSeek in Settings and run a few replays so both reviewers score the same captures.",
      },
      {
        q: "How big a sample do I need?",
        a: "Default 200. Bump to 1000 once you have that much history. Below ~30 paired items the per-bin counts get noisy.",
      },
    ],
    shortcuts: [...navShortcutFor("/backtest", "You're here")],
  },
  "/reviews": {
    quickTips: [
      "Compares stub reviewer (free, deterministic) vs DeepSeek (paid, contextual)",
      "Toggle 'real news only' to filter out demo records",
      "Agreement % uses Jaccard for label overlap + score delta for relevance",
    ],
    questions: [
      {
        q: "Why are some reviews missing one side?",
        a: "DeepSeek is only invoked when sampling rate fires (default 1.0 for relevant items). Set sampling = 1.0 to always pair.",
      },
      {
        q: "How do I budget DeepSeek calls?",
        a: "Settings → DeepSeek reviewer → set usd_cap. When exceeded, future calls auto-skip with budget_exhausted reason.",
      },
    ],
    shortcuts: [...navShortcutFor("/reviews", "You're here")],
  },
  "/scan": {
    quickTips: [
      "Hero = DeepSeek narrator over the last 1000 records, refreshes every 60s",
      "Each tab = different signal family: Events (clustering), Sentiment (momentum), Sources (lead/lag), Anomalies (z-score), Network (spillover)",
      "Watchlist = your monitored symbols; click + on any item to add",
    ],
    questions: [
      {
        q: "Why is the heatmap empty?",
        a: "No clusters with ≥2 distinct domains in the current window. Run more replays or wait for live news flow.",
      },
      {
        q: "What's a 'spillover edge'?",
        a: "Cross-asset relationship where news on A predicts news on B with a lag. Edge weight = predictive strength.",
      },
    ],
    shortcuts: [...navShortcutFor("/scan", "You're here")],
  },
  "/ops": {
    quickTips: [
      "DLQ > 0 means upstream JSONL is malformed somewhere — investigate the most recent ingestion",
      "Diagnostic stamps should be 0 in production_safe mode",
      "Raw config dump is collapsed by default; click 'show' to reveal",
    ],
    questions: [
      {
        q: "How do I switch modes?",
        a: "Set CATCHEM_MODE env var before launch (production_safe / replay_existing / live_tail / research_diagnostic). research_diagnostic also requires NewsImpact governance.",
      },
    ],
    shortcuts: [...navShortcutFor("/ops", "Jump to System / Ops")],
  },
  "/model-controls": {
    quickTips: [
      "v0.1.0 + branch + sha + uptime — your provenance snapshot",
      "ML path = stubs vs HF: stubs are deterministic and CPU-only",
      "Sidecar health is polled every 4 seconds",
    ],
    questions: [
      {
        q: "How do I switch to real HF models?",
        a: "Re-run bootstrap with --with-ml flag. It downloads ~1.5GB of models. Then restart the app.",
      },
    ],
    shortcuts: [...navShortcutFor("/model-controls", "Open model controls")],
  },
  "/settings": {
    quickTips: [
      "Theme toggle persists to localStorage",
      "Keyboard shortcuts list = canonical registry (lib/nav-shortcuts.ts)",
      "DeepSeek reviewer requires an API key from deepseek.com platform",
    ],
    questions: [
      {
        q: "Where is data stored?",
        a: "SQLite at ~/Library/Application Support/Catchem/data/db/catchem.sqlite3 (release) or ./data/db/ (dev).",
      },
    ],
    shortcuts: [...navShortcutFor("/settings", "Open settings")],
  },
  "/sources": {
    quickTips: [
      "Each row = one RSS/Atom feed Catchem polls in the background (default cadence 10s+)",
      "Status 'degraded' means the most recent poll failed — Catchem will retry on the next tick",
      "Polls / successes / failures are cumulative since sidecar boot (reset on restart)",
      "Click any error cell to expand the full last_error message",
    ],
    questions: [
      {
        q: "Why is success rate < 100% on a healthy-looking feed?",
        a: "Some publishers (Yahoo, ZeroHedge) rate-limit aggressively. A handful of 4xx mixed with 2xx is normal — investigate if the rate drops below ~80%.",
      },
      {
        q: "Can I disable a noisy feed?",
        a: "Override the default list via CATCHEM_NEWS__FEEDS at launch. The default 50+ are tuned for breadth; disable individually if a particular source spams.",
      },
    ],
    shortcuts: [...navShortcutFor("/sources", "You're here")],
  },
  "/help": {
    quickTips: [
      "This is the long-form glossary",
      "For quick contextual help on any page, click the floating ? button bottom-right",
      "Press '?' anywhere for the keyboard shortcut overlay",
    ],
    questions: [],
    shortcuts: [...navShortcutFor("/help", "You're here")],
  },
  "/logs": {
    quickTips: [
      "Use the severity filter chips to focus Error / Warning / Info traces.",
      "Click a row's timestamp to inspect the fully expanded payload.",
      "Sidecar health still lands in /ops — logs are the symptom layer, not the root-cause table.",
    ],
    questions: [
      {
        q: "Why are logs not advancing?",
        a: "Sidecar loggers can stop writing after a fatal startup issue. Check /ops and /ui/sidecars, then restart the pipeline.",
      },
      {
        q: "Can I increase verbosity?",
        a: "Set CATCHEM_LOG_LEVEL to debug before launch. Be careful in production-safe mode — noise can hide the first failure if tailing too aggressively.",
      },
    ],
    shortcuts: [...navShortcutFor("/logs", "You're here")],
  },
};

/**
 * Resolve a pathname to a HelpContent entry.
 *
 * Strategy:
 *   1. Exact match (`/feed` → /feed entry).
 *   2. Prefix match for dynamic segments:
 *      - `/feed/<id>` → /feed
 *      - `/symbols/<ticker>` → /symbols
 *   3. Fall through to null — the drawer renders a "no contextual help"
 *      empty state rather than crashing.
 */
export function matchHelp(pathname: string): HelpContent | null {
  const normalized = normalizePath(pathname);
  if (PAGE_HELP[normalized]) return PAGE_HELP[normalized];
  if (normalized.startsWith("/feed/")) return PAGE_HELP["/feed"] ?? null;
  if (normalized.startsWith("/symbols/")) return PAGE_HELP["/symbols"] ?? null;
  if (normalized === "/analysis") return PAGE_HELP["/map"];
  return null;
}
