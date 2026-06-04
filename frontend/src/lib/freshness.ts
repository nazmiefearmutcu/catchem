// Reusable "how fresh is this React Query data" indicator.
//
// React Query exposes `dataUpdatedAt` on every query result — a millisecond
// epoch of the last successful fetch. Converting that to a relative-time
// label ("data 30s ago", "data 2m ago") gives the analyst an at-a-glance
// signal of staleness without burning hero real estate on absolute
// timestamps.
//
// Because the relative label is a pure function of the timestamp, React
// won't re-render the component just because wall-clock time passed. The
// `useTick` companion forces a coarse re-render every `intervalMs` (default
// 30s) so labels naturally tick from "30s ago" → "1m ago" → "2m ago".
//
// Pages opt in by calling `useTick()` and rendering `freshnessLabel(query.dataUpdatedAt)`
// inside their existing hero subtitle.

import { useEffect, useState } from "react";

import { fmtRel } from "@/lib/api";

/**
 * Format a React Query `dataUpdatedAt` millisecond timestamp as a relative
 * "data X ago" label suitable for hero subtitle suffixes.
 *
 *  - `undefined` (no data yet) → "—"
 *  - very recent (<5s, where fmtRel returns "just now") → "data just now"
 *  - otherwise → "data 30s ago" / "data 2m ago" / "data 1h ago"
 */
export function freshnessLabel(updatedAt: number | undefined): string {
  if (!updatedAt) return "—";
  const rel = fmtRel(new Date(updatedAt).toISOString());
  return `data ${rel || "just now"}`;
}

/**
 * Re-render the calling component every `intervalMs` milliseconds.
 *
 * Used to keep `freshnessLabel(query.dataUpdatedAt)` advancing — without
 * this, the relative label would only update when the underlying query
 * actually refetched, so a stale value would stay frozen at "data 5s ago"
 * even after several minutes.
 *
 * Default cadence is 30s, which is fast enough that "data 30s ago" rolls
 * over to "data 1m ago" without lag, but slow enough to be invisible in
 * the React DevTools profiler.
 */
export function useTick(intervalMs = 30_000): void {
  const [, setTick] = useState(0);
  useEffect(() => {
    const id = setInterval(() => setTick((t) => t + 1), intervalMs);
    return () => clearInterval(id);
  }, [intervalMs]);
}
