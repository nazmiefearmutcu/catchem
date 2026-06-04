import { describe, it, expect, beforeEach } from "vitest";
import { act, renderHook } from "@testing-library/react";

import {
  DEFAULT_TILE_ORDER,
  OVERVIEW_TILE_ORDER_KEY,
  readTileOrder,
  reorderTiles,
  useOverviewTileOrder,
} from "@/features/overview/useOverviewTileOrder";

/**
 * jsdom in this project ships without a usable Storage implementation, so we
 * install a minimal in-memory shim (same pattern as Watchlist tests).
 */
function installLocalStorage(): Storage {
  const store = new Map<string, string>();
  const shim: Storage = {
    get length() {
      return store.size;
    },
    clear: () => store.clear(),
    getItem: (k) => (store.has(k) ? store.get(k)! : null),
    key: (i) => Array.from(store.keys())[i] ?? null,
    removeItem: (k) => {
      store.delete(k);
    },
    setItem: (k, v) => {
      store.set(k, String(v));
    },
  };
  Object.defineProperty(window, "localStorage", { value: shim, configurable: true });
  return shim;
}

describe("reorderTiles (pure)", () => {
  it("moves a tile forward — index 0 → 2", () => {
    expect(reorderTiles(["total", "relevant", "dlq", "distinct", "f1"], 0, 2)).toEqual([
      "relevant",
      "dlq",
      "total",
      "distinct",
      "f1",
    ]);
  });

  it("moves a tile backward — index 4 → 0", () => {
    expect(reorderTiles(["total", "relevant", "dlq", "distinct", "f1"], 4, 0)).toEqual([
      "f1",
      "total",
      "relevant",
      "dlq",
      "distinct",
    ]);
  });

  it("returns the input reference on no-op (from === to)", () => {
    const input = [...DEFAULT_TILE_ORDER];
    expect(reorderTiles(input, 2, 2)).toBe(input);
  });

  it("returns the input reference on out-of-range indices", () => {
    const input = [...DEFAULT_TILE_ORDER];
    expect(reorderTiles(input, -1, 1)).toBe(input);
    expect(reorderTiles(input, 0, 5)).toBe(input);
  });
});

describe("readTileOrder (persistence)", () => {
  beforeEach(() => {
    installLocalStorage();
  });

  it("returns the default order when nothing is persisted", () => {
    expect(readTileOrder()).toEqual([...DEFAULT_TILE_ORDER]);
  });

  it("reads back a valid persisted order verbatim", () => {
    const stored = ["f1", "dlq", "relevant", "distinct", "total"];
    localStorage.setItem(OVERVIEW_TILE_ORDER_KEY, JSON.stringify(stored));
    expect(readTileOrder()).toEqual(stored);
  });

  it("falls back to default on malformed JSON without throwing", () => {
    localStorage.setItem(OVERVIEW_TILE_ORDER_KEY, "{not-json");
    expect(readTileOrder()).toEqual([...DEFAULT_TILE_ORDER]);
  });

  it("falls back to default when the stored value is not an array", () => {
    localStorage.setItem(OVERVIEW_TILE_ORDER_KEY, JSON.stringify({ foo: "bar" }));
    expect(readTileOrder()).toEqual([...DEFAULT_TILE_ORDER]);
  });

  it("filters out unknown ids and appends missing default ids at the end", () => {
    // User has an older build's persisted order missing `f1`, plus a
    // garbage entry. We should keep the known ones in their order then
    // append `f1` so the new tile still shows up after an upgrade.
    localStorage.setItem(
      OVERVIEW_TILE_ORDER_KEY,
      JSON.stringify(["dlq", "total", "junk", "relevant", "distinct"]),
    );
    expect(readTileOrder()).toEqual(["dlq", "total", "relevant", "distinct", "f1"]);
  });

  it("de-duplicates repeated ids", () => {
    localStorage.setItem(
      OVERVIEW_TILE_ORDER_KEY,
      JSON.stringify(["total", "total", "relevant", "dlq", "distinct", "f1"]),
    );
    expect(readTileOrder()).toEqual([...DEFAULT_TILE_ORDER]);
  });
});

describe("useOverviewTileOrder (hook)", () => {
  beforeEach(() => {
    installLocalStorage();
  });

  it("starts with the default order on a fresh store", () => {
    const { result } = renderHook(() => useOverviewTileOrder());
    expect(result.current.order).toEqual([...DEFAULT_TILE_ORDER]);
  });

  it("reorder() updates state AND localStorage", () => {
    const { result } = renderHook(() => useOverviewTileOrder());
    act(() => {
      result.current.reorder(0, 4);
    });
    expect(result.current.order).toEqual(["relevant", "dlq", "distinct", "f1", "total"]);
    expect(localStorage.getItem(OVERVIEW_TILE_ORDER_KEY)).toBe(
      JSON.stringify(["relevant", "dlq", "distinct", "f1", "total"]),
    );
  });

  it("reorder() with bad indices is a no-op (no write)", () => {
    const { result } = renderHook(() => useOverviewTileOrder());
    act(() => {
      result.current.reorder(3, 3);
    });
    expect(result.current.order).toEqual([...DEFAULT_TILE_ORDER]);
    expect(localStorage.getItem(OVERVIEW_TILE_ORDER_KEY)).toBeNull();
  });

  it("order survives a remount", () => {
    const first = renderHook(() => useOverviewTileOrder());
    act(() => {
      first.result.current.reorder(0, 2);
    });
    const stored = first.result.current.order;
    const second = renderHook(() => useOverviewTileOrder());
    expect(second.result.current.order).toEqual(stored);
  });

  it("reset() clears localStorage and restores the default order", () => {
    const { result } = renderHook(() => useOverviewTileOrder());
    act(() => {
      result.current.reorder(0, 4);
    });
    expect(localStorage.getItem(OVERVIEW_TILE_ORDER_KEY)).not.toBeNull();
    act(() => {
      result.current.reset();
    });
    expect(result.current.order).toEqual([...DEFAULT_TILE_ORDER]);
    expect(localStorage.getItem(OVERVIEW_TILE_ORDER_KEY)).toBeNull();
  });

  it("mirrors a storage event from another window", () => {
    const { result } = renderHook(() => useOverviewTileOrder());
    // Another Tauri window persisted a new order — simulate the browser
    // dispatching a `storage` event to the OTHER documents.
    const next = ["f1", "distinct", "dlq", "relevant", "total"];
    localStorage.setItem(OVERVIEW_TILE_ORDER_KEY, JSON.stringify(next));
    act(() => {
      window.dispatchEvent(
        new StorageEvent("storage", {
          key: OVERVIEW_TILE_ORDER_KEY,
          newValue: JSON.stringify(next),
        }),
      );
    });
    expect(result.current.order).toEqual(next);
  });
});
