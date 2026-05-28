import { useCallback, useEffect, useRef, useState } from "react";

import { useStorageListener } from "@/lib/storage-sync";

/**
 * Watchlist hook — localStorage-backed, with custom drag-reorder + multi-sort.
 *
 * The custom order (the `items` array exactly as stored) is the source of
 * truth: dragging always switches `sortBy` back to "custom" and persists the
 * new explicit order. Non-custom sort modes (name/momentum/activity) produce
 * a derived display order without mutating `items`.
 *
 * Metrics for momentum/activity sorts are passed in by the caller because
 * they live in the QuantDashboard query — keeping the hook itself ignorant of
 * that shape keeps it trivially testable.
 *
 * Cross-window sync (v42): when window A drags/adds/removes a symbol, the
 * `storage` event fires on every OTHER Tauri analyst window. The
 * `useStorageListener` below re-reads the persisted list (and sort mode)
 * and updates local state without re-broadcasting — the write only happens
 * inside the user-triggered callbacks (add/remove/reorder/setSortBy).
 */

export type WatchlistSortMode = "custom" | "name" | "momentum" | "activity";

export const WATCHLIST_KEY = "catchem.watchlist";
export const WATCHLIST_SORT_KEY = "catchem.watchlist.sort";
/**
 * Legacy storage key from the original /scan-scoped hook. We migrate any
 * data still living there on first read so existing users don't lose their
 * pinned tickers when this hook took over the canonical "catchem.watchlist"
 * slot.
 */
export const LEGACY_WATCHLIST_KEY = "catchem.quant.watchlist";

/** Per-symbol live metrics used to derive non-custom sort orders. */
export type WatchlistMetric = {
  /** signed [-1..+1] momentum; missing = no momentum data → sinks to bottom */
  momentum?: number;
  /** mention count proxy for "activity" → higher = more recent buzz */
  activity?: number;
};

export type WatchlistMetrics = Record<string, WatchlistMetric>;

export interface WatchlistApi {
  /** Persisted custom order. Always the order on disk. */
  items: string[];
  /** Current sort mode. */
  sortBy: WatchlistSortMode;
  setSortBy: (mode: WatchlistSortMode) => void;
  /** Add a symbol (no-op if already present). Normalised to UPPERCASE. */
  add: (sym: string) => void;
  /** Remove a symbol (no-op if missing). */
  remove: (sym: string) => void;
  /** Toggle: add if absent, remove if present. */
  toggle: (sym: string) => void;
  /** Move `items[fromIdx]` to position `toIdx` (clamped). Switches sortBy → "custom". */
  reorder: (fromIdx: number, toIdx: number) => void;
  /** Replace the whole list (used by drag-drop reorder when sortBy != custom). */
  replace: (next: string[]) => void;
  /** Derived display order under the current `sortBy` + given metrics. */
  displayItems: (metrics?: WatchlistMetrics) => string[];
}

function normaliseSymbolList(raw: unknown): string[] {
  if (!Array.isArray(raw)) return [];
  const seen = new Set<string>();
  const out: string[] = [];
  for (const v of raw) {
    if (typeof v !== "string") continue;
    const sym = v.trim().toUpperCase();
    if (!sym || seen.has(sym)) continue;
    seen.add(sym);
    out.push(sym);
  }
  return out;
}

/**
 * Read+normalise the persisted list from localStorage. Returns `[]` on
 * any failure. On first read we also migrate from the legacy key —
 * older builds wrote `catchem.quant.watchlist`; we copy that across so
 * users don't get an empty list on the first launch of this build.
 */
export function readWatchlist(): string[] {
  try {
    const raw = localStorage.getItem(WATCHLIST_KEY);
    if (raw) {
      return normaliseSymbolList(JSON.parse(raw));
    }
    // No current-key value — try the legacy key.
    const legacyRaw = localStorage.getItem(LEGACY_WATCHLIST_KEY);
    if (!legacyRaw) return [];
    const migrated = normaliseSymbolList(JSON.parse(legacyRaw));
    if (migrated.length > 0) {
      try {
        localStorage.setItem(WATCHLIST_KEY, JSON.stringify(migrated));
        localStorage.removeItem(LEGACY_WATCHLIST_KEY);
      } catch {
        /* private mode — fine, we still return the value in memory */
      }
    }
    return migrated;
  } catch {
    return [];
  }
}

function readSort(): WatchlistSortMode {
  try {
    const raw = localStorage.getItem(WATCHLIST_SORT_KEY);
    if (raw === "name" || raw === "momentum" || raw === "activity" || raw === "custom") {
      return raw;
    }
  } catch {
    /* private mode etc. */
  }
  return "custom";
}

function persistList(next: string[]): void {
  try {
    localStorage.setItem(WATCHLIST_KEY, JSON.stringify(next));
  } catch {
    /* quota / private mode — silent */
  }
}

function persistSort(mode: WatchlistSortMode): void {
  try {
    localStorage.setItem(WATCHLIST_SORT_KEY, mode);
  } catch {
    /* private mode etc. */
  }
}

/**
 * Pure reorder helper — exported for tests.
 *
 * Returns a new array with `items[fromIdx]` moved to `toIdx`. If either
 * index is out of range or both are equal, returns the input unchanged
 * (referentially equal) so consumers can detect no-op moves.
 */
export function reorderArray<T>(items: T[], fromIdx: number, toIdx: number): T[] {
  if (
    !Number.isInteger(fromIdx) ||
    !Number.isInteger(toIdx) ||
    fromIdx < 0 ||
    toIdx < 0 ||
    fromIdx >= items.length ||
    toIdx >= items.length ||
    fromIdx === toIdx
  ) {
    return items;
  }
  const next = items.slice();
  const [moved] = next.splice(fromIdx, 1);
  next.splice(toIdx, 0, moved);
  return next;
}

/**
 * Pure sort helper — exported for tests.
 *
 * Returns a NEW array sorted under `sortBy`. For "custom" the input is
 * passed through unchanged. For "momentum"/"activity" symbols missing
 * from `metrics` are pushed to the bottom, then stable-sorted by their
 * original index so the user's custom order shows through under ties.
 */
export function sortWatchlist(
  items: string[],
  sortBy: WatchlistSortMode,
  metrics: WatchlistMetrics = {},
): string[] {
  if (sortBy === "custom") return items.slice();
  // Pair with index so we can fall back to custom order on ties → stable.
  const indexed = items.map((sym, idx) => ({ sym, idx }));
  if (sortBy === "name") {
    indexed.sort((a, b) => a.sym.localeCompare(b.sym) || a.idx - b.idx);
    return indexed.map((r) => r.sym);
  }
  const key: keyof WatchlistMetric = sortBy === "momentum" ? "momentum" : "activity";
  indexed.sort((a, b) => {
    const va = metrics[a.sym]?.[key];
    const vb = metrics[b.sym]?.[key];
    const hasA = typeof va === "number" && Number.isFinite(va);
    const hasB = typeof vb === "number" && Number.isFinite(vb);
    if (hasA && hasB) {
      // "momentum" sorts by |momentum| so flips and big positive/negative
      // moves both surface; "activity" sorts by raw count (more = first).
      const ka = sortBy === "momentum" ? Math.abs(va as number) : (va as number);
      const kb = sortBy === "momentum" ? Math.abs(vb as number) : (vb as number);
      if (kb !== ka) return kb - ka;
      return a.idx - b.idx;
    }
    if (hasA) return -1;
    if (hasB) return 1;
    return a.idx - b.idx;
  });
  return indexed.map((r) => r.sym);
}

/** React hook — wires the pure helpers to state + localStorage. */
export function useWatchlist(): WatchlistApi {
  const [items, setItems] = useState<string[]>(() => readWatchlist());
  const [sortBy, setSortByState] = useState<WatchlistSortMode>(() => readSort());

  // Hold the latest state in refs so the API callbacks stay stable —
  // important for reorder() which is called inside HTML5 DnD handlers
  // captured at mount time.
  const itemsRef = useRef(items);
  const sortRef = useRef(sortBy);
  useEffect(() => {
    itemsRef.current = items;
  }, [items]);
  useEffect(() => {
    sortRef.current = sortBy;
  }, [sortBy]);

  const writeItems = useCallback((next: string[]) => {
    // De-dupe + uppercase-normalise on every write so legacy/corrupt
    // payloads can't leak through (e.g. a user editing localStorage).
    const seen = new Set<string>();
    const cleaned: string[] = [];
    for (const v of next) {
      const sym = (v ?? "").trim().toUpperCase();
      if (!sym || seen.has(sym)) continue;
      seen.add(sym);
      cleaned.push(sym);
    }
    setItems(cleaned);
    persistList(cleaned);
  }, []);

  const add = useCallback(
    (sym: string) => {
      const k = (sym ?? "").trim().toUpperCase();
      if (!k) return;
      const current = itemsRef.current;
      if (current.includes(k)) return;
      writeItems([...current, k]);
    },
    [writeItems],
  );

  const remove = useCallback(
    (sym: string) => {
      const k = (sym ?? "").trim().toUpperCase();
      if (!k) return;
      const current = itemsRef.current;
      if (!current.includes(k)) return;
      writeItems(current.filter((s) => s !== k));
    },
    [writeItems],
  );

  const toggle = useCallback(
    (sym: string) => {
      const k = (sym ?? "").trim().toUpperCase();
      if (!k) return;
      const current = itemsRef.current;
      writeItems(current.includes(k) ? current.filter((s) => s !== k) : [...current, k]);
    },
    [writeItems],
  );

  const setSortBy = useCallback((mode: WatchlistSortMode) => {
    setSortByState(mode);
    persistSort(mode);
  }, []);

  const reorder = useCallback(
    (fromIdx: number, toIdx: number) => {
      const next = reorderArray(itemsRef.current, fromIdx, toIdx);
      if (next === itemsRef.current) return;
      // Reorder is always an explicit user gesture — switch back to custom
      // so the new order actually shows on screen even if we were sorted by
      // momentum/name/activity. Otherwise the drag would look like a no-op.
      if (sortRef.current !== "custom") {
        setSortByState("custom");
        persistSort("custom");
      }
      writeItems(next);
    },
    [writeItems],
  );

  const replace = useCallback(
    (next: string[]) => {
      writeItems(next);
    },
    [writeItems],
  );

  const displayItems = useCallback(
    (metrics: WatchlistMetrics = {}) => sortWatchlist(items, sortBy, metrics),
    [items, sortBy],
  );

  // Cross-window sync — when another Tauri window mutates the watchlist
  // (add/remove/reorder/sort), re-read from disk so this window mirrors
  // the change. Setting state via setItems / setSortByState only does NOT
  // re-persist (persistList / persistSort live in the user-callbacks), so
  // there's no ping-pong write back to localStorage.
  useStorageListener([WATCHLIST_KEY, WATCHLIST_SORT_KEY], (e) => {
    if (e.key === WATCHLIST_KEY || e.key === null) {
      setItems(readWatchlist());
    }
    if (e.key === WATCHLIST_SORT_KEY || e.key === null) {
      setSortByState(readSort());
    }
  });

  return { items, sortBy, setSortBy, add, remove, toggle, reorder, replace, displayItems };
}
