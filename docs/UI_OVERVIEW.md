# UI Overview

The premium analyst interface lives in `frontend/` (TypeScript + React + Vite)
and is bundled into `src/fusion_stack/static/app/` at build time. The same
FastAPI process serves it at `/` — there is no second runtime.

## Routes

| Path | Purpose | Lazy-loaded |
|---|---|---|
| `/` | Overview — cards, distributions, trend, recent relevant items | ✅ |
| `/feed` | Live Feed — filterable, URL-state, drawer-driven | ✅ |
| `/feed/:capture_id` | Same feed with the record drawer open on that capture | ✅ |
| `/map` | Market Map — asset-class × reason-code heatmap + stacked trend | ✅ |
| `/symbols` | Symbols index | ✅ |
| `/symbols/:symbol` | Symbol detail — records, reason distribution, sentiment | ✅ |
| `/benchmark` | Benchmark Lab — precision/recall/F1, per-item table, history | ✅ |
| `/ops` | System / Ops — guard status, model versions, raw config | ✅ |
| `/settings` | Theme, keyboard shortcuts, mode docs | ✅ |
| `/legacy` | Original vanilla dashboard (kept until premium UI is fully proven) | n/a |

## Information flow (one-trip-per-route)

Each route consumes a small number of aggregated `/ui/*` endpoints:

```
Overview      → /ui/summary, /ui/trends, /ui/benchmark/latest
Feed          → /ui/facets, /recent or /records/by-{asset_class|reason|symbol}
Detail drawer → /record/{capture_id}
Market Map    → /ui/matrix, /ui/trends
Symbols       → /ui/top-symbols, /ui/symbol/{sym}
Benchmark     → /ui/benchmark/latest, /ui/benchmark/history
Ops           → /ui/summary, /ui/guards, /config, /metrics
```

This keeps the network waterfall flat and the JSON shapes typed once on the
backend.

## Live updates

A single SSE stream at `/ui/stream` powers the live status dot. When the
backend reports a `summary` event the client invalidates the matching TanStack
Query keys; React Query then re-fetches lazily where needed. If `EventSource`
is unavailable or the stream errors, the hook transparently falls back to
polling every 12s.

## State

- **URL state** drives all feed filters (`?ac=`, `?rc=`, `?sym=`, `?q=`,
  `?relevant=`, `?sentiment=`). Copy-link works out of the box.
- **localStorage** stores the theme preference (`fusion.theme`).
- **React Query cache** holds API responses; the SSE hook invalidates them.

## Theme

The theme is set on `<html>` before the React app boots (the small inline
script in `index.html`) to prevent FOUC. Tailwind reads `class="dark"` on the
root; CSS variables in `globals.css` deliver the dark and light palettes.
Setting respects the user choice; we do not override `prefers-color-scheme`
once a choice exists.

## Charts

`src/charts/EChart.tsx` is a thin theme-aware wrapper around `echarts-for-react`.
We don't import echarts themes — token-level options keep the bundle smaller
and the chart respects live theme toggling.

ECharts is route-split into the `charts` chunk (~1MB minified, ~350KB gzipped)
and only loads when the user visits a chart-using page.

## Diagnostic transparency

The `StatusBanner` row above every page makes three things visible:

1. The current mode (one of `production_safe`, `replay_existing`, `live_tail`,
   `research_diagnostic`).
2. The NewsImpact guard state — quarantine, release gate, sha256 of governance.
3. When `research_diagnostic` is active, a clear yellow banner saying the
   diagnostic adapter is loaded and may NOT override `is_finance_relevant`.

If the guard verifier reports `ok: false` (e.g. governance file missing or
mutated), the banner turns red. Operators see this before any data.

## Why this matters

The UI never invents intelligence. Every label on screen is either:

- A direct field from the `FinancialImpactRecord` produced by `service.py`, or
- An aggregate the backend computed in a `/ui/*` endpoint.

The frontend does no inference. This matches the document's safety posture:
the model decisions stay in Python, where they are tested, governed, and
guard-protected.
