import { useCallback, useRef, useState } from "react";

import { useStorageListener } from "@/lib/storage-sync";

/**
 * Overview KPI tile order — localStorage-backed, drag-reorder + Alt+Arrow.
 *
 * The 5 KPI tiles on the Overview hero ("total records", "finance-relevant",
 * "DLQ", "distinct asset classes", "benchmark F1") have fixed identities but
 * a user-controlled position. This hook owns the persisted order, exposes
 * the same drag-reorder / reset / cross-window-sync surface as
 * `useWatchlist`, and survives partial-write corruption by appending any
 * missing ids to the end of the user's order.
 *
 * Cross-window sync (v42 pattern): write happens inside `reorder` / `reset`,
 * the `storage` event fires on OTHER Tauri analyst windows, and
 * `useStorageListener` mirrors the change here without re-broadcasting.
 */

export const DEFAULT_TILE_ORDER = [
  "total",
  "relevant",
  "dlq",
  "distinct",
  "f1",
] as const;
export type TileId = (typeof DEFAULT_TILE_ORDER)[number];

export const OVERVIEW_TILE_ORDER_KEY = "catchem.overview.tile-order";

const DEFAULT_SET: ReadonlySet<TileId> = new Set(DEFAULT_TILE_ORDER);

function isTileId(value: unknown): value is TileId {
  return typeof value === "string" && DEFAULT_SET.has(value as TileId);
}

/**
 * Read+sanitise the persisted order. Returns the default order on any
 * failure. If the stored list is a partial subset, the missing tiles are
 * appended at the end so newly-introduced tiles still show up after an
 * upgrade — the analyst keeps their custom order for the IDs they know.
 */
export function readTileOrder(): TileId[] {
  if (typeof localStorage === "undefined") return [...DEFAULT_TILE_ORDER];
  try {
    const raw = localStorage.getItem(OVERVIEW_TILE_ORDER_KEY);
    if (!raw) return [...DEFAULT_TILE_ORDER];
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [...DEFAULT_TILE_ORDER];
    const seen = new Set<TileId>();
    const ordered: TileId[] = [];
    for (const v of parsed) {
      if (!isTileId(v) || seen.has(v)) continue;
      seen.add(v);
      ordered.push(v);
    }
    if (ordered.length === 0) return [...DEFAULT_TILE_ORDER];
    for (const id of DEFAULT_TILE_ORDER) {
      if (!seen.has(id)) ordered.push(id);
    }
    return ordered;
  } catch {
    return [...DEFAULT_TILE_ORDER];
  }
}

/**
 * Pure reorder helper — exported for tests.
 *
 * Returns a new array with `order[fromIdx]` moved to `toIdx`. If either
 * index is out of range or both are equal, returns the input unchanged
 * (referentially equal) so callers can detect no-op moves.
 */
export function reorderTiles(
  order: ReadonlyArray<TileId>,
  fromIdx: number,
  toIdx: number,
): TileId[] {
  if (
    !Number.isInteger(fromIdx) ||
    !Number.isInteger(toIdx) ||
    fromIdx < 0 ||
    toIdx < 0 ||
    fromIdx >= order.length ||
    toIdx >= order.length ||
    fromIdx === toIdx
  ) {
    return order as TileId[];
  }
  const next = order.slice();
  const [moved] = next.splice(fromIdx, 1);
  next.splice(toIdx, 0, moved);
  return next;
}

function persistOrder(order: TileId[]): void {
  try {
    localStorage.setItem(OVERVIEW_TILE_ORDER_KEY, JSON.stringify(order));
  } catch {
    /* quota / private mode — silent */
  }
}

export interface OverviewTileOrderApi {
  /** Current ordered list of tile ids. Always length === 5. */
  order: TileId[];
  /** Move `order[fromIdx]` to position `toIdx`. No-op on bad indices. */
  reorder: (fromIdx: number, toIdx: number) => void;
  /** Clear persistence + restore the default order. */
  reset: () => void;
}

export function useOverviewTileOrder(): OverviewTileOrderApi {
  const [order, setOrder] = useState<TileId[]>(() => readTileOrder());

  // Keep latest order in a ref so the reorder callback stays stable —
  // important when wired into HTML5 DnD handlers captured at mount.
  const orderRef = useRef(order);
  orderRef.current = order;

  const reorder = useCallback((fromIdx: number, toIdx: number) => {
    const next = reorderTiles(orderRef.current, fromIdx, toIdx);
    if (next === orderRef.current) return;
    orderRef.current = next;
    setOrder(next);
    persistOrder(next);
  }, []);

  const reset = useCallback(() => {
    const defaults = [...DEFAULT_TILE_ORDER];
    orderRef.current = defaults;
    setOrder(defaults);
    try {
      localStorage.removeItem(OVERVIEW_TILE_ORDER_KEY);
    } catch {
      /* private mode — silent */
    }
  }, []);

  // Cross-window sync — another Tauri window mutated the order; mirror it.
  // We don't re-persist on this path (writes happen in user callbacks).
  useStorageListener([OVERVIEW_TILE_ORDER_KEY], () => {
    setOrder(readTileOrder());
  });

  return { order, reorder, reset };
}
