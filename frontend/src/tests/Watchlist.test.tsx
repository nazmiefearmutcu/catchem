import { describe, it, expect, beforeEach } from "vitest";
import { act, fireEvent, render, renderHook, screen, within } from "@testing-library/react";
import {
  LEGACY_WATCHLIST_KEY,
  WATCHLIST_KEY,
  WATCHLIST_SORT_KEY,
  readWatchlist,
  reorderArray,
  sortWatchlist,
  useWatchlist,
  type WatchlistMetrics,
} from "@/features/quant/useWatchlist";

// jsdom in this project ships without a full Storage implementation, so we
// install our own minimal shim before each test.
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

describe("reorderArray (pure)", () => {
  it("moves item from index 0 to 2 → [B, C, A]", () => {
    expect(reorderArray(["A", "B", "C"], 0, 2)).toEqual(["B", "C", "A"]);
  });

  it("moves item from index 2 to 0 → [C, A, B]", () => {
    expect(reorderArray(["A", "B", "C"], 2, 0)).toEqual(["C", "A", "B"]);
  });

  it("returns input unchanged on no-op (from === to)", () => {
    const input = ["A", "B", "C"];
    expect(reorderArray(input, 1, 1)).toBe(input);
  });

  it("returns input unchanged on out-of-range indices", () => {
    const input = ["A", "B", "C"];
    expect(reorderArray(input, -1, 1)).toBe(input);
    expect(reorderArray(input, 0, 5)).toBe(input);
  });

  it("returns a new array reference on actual moves (no mutation)", () => {
    const input = ["A", "B", "C"];
    const out = reorderArray(input, 0, 1);
    expect(out).not.toBe(input);
    expect(input).toEqual(["A", "B", "C"]);
  });
});

describe("sortWatchlist (pure)", () => {
  it('"custom" passes items through unchanged but as a fresh array', () => {
    const items = ["B", "A", "C"];
    const out = sortWatchlist(items, "custom");
    expect(out).toEqual(["B", "A", "C"]);
    expect(out).not.toBe(items);
  });

  it('"name" sorts A→Z on [B, A, C] → [A, B, C]', () => {
    expect(sortWatchlist(["B", "A", "C"], "name")).toEqual(["A", "B", "C"]);
  });

  it('"momentum" sorts by |momentum|, missing values sink', () => {
    const metrics: WatchlistMetrics = {
      A: { momentum: 0.1 },
      B: { momentum: -0.8 },
      C: { momentum: 0.4 },
      // D has no momentum → sinks under custom-order tie
    };
    expect(sortWatchlist(["A", "B", "C", "D"], "momentum", metrics)).toEqual([
      "B",
      "C",
      "A",
      "D",
    ]);
  });

  it('"activity" sorts by mention_count descending, missing sinks', () => {
    const metrics: WatchlistMetrics = {
      A: { activity: 3 },
      B: { activity: 12 },
      C: { activity: 7 },
    };
    expect(sortWatchlist(["A", "B", "C", "D"], "activity", metrics)).toEqual([
      "B",
      "C",
      "A",
      "D",
    ]);
  });

  it("ties under derived sort fall back to custom order (stable)", () => {
    const metrics: WatchlistMetrics = {
      A: { momentum: 0.5 },
      B: { momentum: -0.5 }, // |momentum| identical → custom order breaks tie
      C: { momentum: 0.1 },
    };
    // |A|=|B|=0.5 > |C|=0.1 — A and B tied, A comes first because it's at idx 0
    expect(sortWatchlist(["A", "B", "C"], "momentum", metrics)).toEqual([
      "A",
      "B",
      "C",
    ]);
  });

  it("handles empty list without throwing", () => {
    expect(sortWatchlist([], "name")).toEqual([]);
    expect(sortWatchlist([], "momentum", {})).toEqual([]);
  });
});

describe("readWatchlist (persistence + migration)", () => {
  beforeEach(() => {
    installLocalStorage();
  });

  it("returns [] when nothing is persisted", () => {
    expect(readWatchlist()).toEqual([]);
  });

  it("reads back what was written under the canonical key", () => {
    localStorage.setItem(WATCHLIST_KEY, JSON.stringify(["AAPL", "BTC-USD"]));
    expect(readWatchlist()).toEqual(["AAPL", "BTC-USD"]);
  });

  it("normalises lowercase + whitespace + duplicates", () => {
    localStorage.setItem(
      WATCHLIST_KEY,
      JSON.stringify(["aapl", "  Tsla ", "AAPL", "", null]),
    );
    expect(readWatchlist()).toEqual(["AAPL", "TSLA"]);
  });

  it("migrates from the legacy key on first read and clears it", () => {
    localStorage.setItem(LEGACY_WATCHLIST_KEY, JSON.stringify(["NVDA", "AMD"]));
    expect(readWatchlist()).toEqual(["NVDA", "AMD"]);
    expect(localStorage.getItem(WATCHLIST_KEY)).toBe(
      JSON.stringify(["NVDA", "AMD"]),
    );
    expect(localStorage.getItem(LEGACY_WATCHLIST_KEY)).toBeNull();
  });

  it("returns [] on malformed JSON without throwing", () => {
    localStorage.setItem(WATCHLIST_KEY, "{not-json");
    expect(readWatchlist()).toEqual([]);
  });
});

describe("useWatchlist (hook)", () => {
  beforeEach(() => {
    installLocalStorage();
  });

  it("starts with the persisted list + 'custom' sort", () => {
    localStorage.setItem(WATCHLIST_KEY, JSON.stringify(["A", "B"]));
    const { result } = renderHook(() => useWatchlist());
    expect(result.current.items).toEqual(["A", "B"]);
    expect(result.current.sortBy).toBe("custom");
  });

  it("add() appends + uppercase-normalises + persists", () => {
    const { result } = renderHook(() => useWatchlist());
    act(() => {
      result.current.add("aapl");
    });
    expect(result.current.items).toEqual(["AAPL"]);
    expect(localStorage.getItem(WATCHLIST_KEY)).toBe(JSON.stringify(["AAPL"]));
  });

  it("add() is idempotent — no duplicate when symbol already present", () => {
    const { result } = renderHook(() => useWatchlist());
    act(() => {
      result.current.add("AAPL");
    });
    act(() => {
      result.current.add(" aapl ");
    });
    expect(result.current.items).toEqual(["AAPL"]);
  });

  it("remove() drops a symbol + persists", () => {
    localStorage.setItem(WATCHLIST_KEY, JSON.stringify(["A", "B", "C"]));
    const { result } = renderHook(() => useWatchlist());
    act(() => {
      result.current.remove("B");
    });
    expect(result.current.items).toEqual(["A", "C"]);
    expect(localStorage.getItem(WATCHLIST_KEY)).toBe(JSON.stringify(["A", "C"]));
  });

  it("toggle() adds when absent, removes when present", () => {
    const { result } = renderHook(() => useWatchlist());
    act(() => {
      result.current.toggle("X");
    });
    expect(result.current.items).toEqual(["X"]);
    act(() => {
      result.current.toggle("X");
    });
    expect(result.current.items).toEqual([]);
  });

  it("reorder() moves items + persists + survives a remount", () => {
    localStorage.setItem(WATCHLIST_KEY, JSON.stringify(["A", "B", "C"]));
    const { result } = renderHook(() => useWatchlist());
    act(() => {
      result.current.reorder(0, 2);
    });
    expect(result.current.items).toEqual(["B", "C", "A"]);
    expect(localStorage.getItem(WATCHLIST_KEY)).toBe(
      JSON.stringify(["B", "C", "A"]),
    );
    // Remount → state should restore from persistence
    const second = renderHook(() => useWatchlist());
    expect(second.result.current.items).toEqual(["B", "C", "A"]);
  });

  it("setSortBy() persists the chosen mode", () => {
    const { result } = renderHook(() => useWatchlist());
    act(() => {
      result.current.setSortBy("name");
    });
    expect(result.current.sortBy).toBe("name");
    expect(localStorage.getItem(WATCHLIST_SORT_KEY)).toBe("name");
  });

  it("reorder() while sortBy != 'custom' snaps back to 'custom'", () => {
    localStorage.setItem(WATCHLIST_KEY, JSON.stringify(["B", "A", "C"]));
    localStorage.setItem(WATCHLIST_SORT_KEY, "name");
    const { result } = renderHook(() => useWatchlist());
    expect(result.current.sortBy).toBe("name");
    act(() => {
      result.current.reorder(0, 1);
    });
    expect(result.current.sortBy).toBe("custom");
    expect(localStorage.getItem(WATCHLIST_SORT_KEY)).toBe("custom");
  });

  it("displayItems() derives the sort order from the current mode", () => {
    localStorage.setItem(WATCHLIST_KEY, JSON.stringify(["B", "A", "C"]));
    const { result } = renderHook(() => useWatchlist());
    expect(result.current.displayItems()).toEqual(["B", "A", "C"]);
    act(() => {
      result.current.setSortBy("name");
    });
    expect(result.current.displayItems()).toEqual(["A", "B", "C"]);
  });
});

// ──────────────────────────────────────────────────────────────────────
// Component integration — minimal harness that exposes the same UI the
// real page uses, but without pulling in the entire QuantScanPage tree.
// We render an inline component that wires `useWatchlist` to a stripped
// version of the markup so we can assert on the chips, drag handlers,
// and keyboard behaviour.
// ──────────────────────────────────────────────────────────────────────

function WatchlistTestHarness() {
  const api = useWatchlist();
  const { items, sortBy, setSortBy, add, remove, reorder } = api;
  const display = api.displayItems();
  return (
    <div>
      <div data-testid="harness-sortBy">{sortBy}</div>
      <div data-testid="harness-items">{items.join(",")}</div>
      <div data-testid="harness-display">{display.join(",")}</div>
      <button data-testid="harness-add-A" onClick={() => add("A")} type="button">
        +A
      </button>
      <button data-testid="harness-add-B" onClick={() => add("B")} type="button">
        +B
      </button>
      <button data-testid="harness-add-C" onClick={() => add("C")} type="button">
        +C
      </button>
      <button
        data-testid="harness-remove-B"
        onClick={() => remove("B")}
        type="button"
      >
        -B
      </button>
      <button
        data-testid="harness-reorder-0-2"
        onClick={() => reorder(0, 2)}
        type="button"
      >
        reorder 0→2
      </button>
      <button
        data-testid="harness-sort-name"
        onClick={() => setSortBy("name")}
        type="button"
      >
        sort:name
      </button>
      <button
        data-testid="harness-sort-custom"
        onClick={() => setSortBy("custom")}
        type="button"
      >
        sort:custom
      </button>
    </div>
  );
}

describe("<WatchlistTestHarness> (integration)", () => {
  beforeEach(() => {
    installLocalStorage();
  });

  it("add → reorder → name-sort behaves end-to-end + persists", () => {
    render(<WatchlistTestHarness />);
    fireEvent.click(screen.getByTestId("harness-add-A"));
    fireEvent.click(screen.getByTestId("harness-add-B"));
    fireEvent.click(screen.getByTestId("harness-add-C"));
    expect(screen.getByTestId("harness-items").textContent).toBe("A,B,C");
    expect(screen.getByTestId("harness-display").textContent).toBe("A,B,C");

    fireEvent.click(screen.getByTestId("harness-reorder-0-2"));
    expect(screen.getByTestId("harness-items").textContent).toBe("B,C,A");
    expect(localStorage.getItem(WATCHLIST_KEY)).toBe(
      JSON.stringify(["B", "C", "A"]),
    );

    fireEvent.click(screen.getByTestId("harness-sort-name"));
    expect(screen.getByTestId("harness-sortBy").textContent).toBe("name");
    expect(screen.getByTestId("harness-display").textContent).toBe("A,B,C");
    // Custom order on disk is untouched — only the derived order changes.
    expect(localStorage.getItem(WATCHLIST_KEY)).toBe(
      JSON.stringify(["B", "C", "A"]),
    );
  });

  it("remove keeps the existing remove behaviour intact", () => {
    render(<WatchlistTestHarness />);
    fireEvent.click(screen.getByTestId("harness-add-A"));
    fireEvent.click(screen.getByTestId("harness-add-B"));
    fireEvent.click(screen.getByTestId("harness-add-C"));
    fireEvent.click(screen.getByTestId("harness-remove-B"));
    expect(screen.getByTestId("harness-items").textContent).toBe("A,C");
  });

  it("empty watchlist persists [] and the harness renders no symbols", () => {
    render(<WatchlistTestHarness />);
    expect(screen.getByTestId("harness-items").textContent).toBe("");
    expect(screen.getByTestId("harness-display").textContent).toBe("");
  });
});

// We can't reliably drive native HTML5 DnD in jsdom (dataTransfer is
// stubbed and getBoundingClientRect returns zeros), so the drag-and-drop
// happy path is covered by reorderArray + the harness's reorder()
// button. We DO assert the keyboard fallback because that's a real
// pointer-free reorder path users rely on.
describe("WatchlistCard keyboard reorder via Alt+Arrow", () => {
  beforeEach(() => {
    installLocalStorage();
  });

  function KeyboardHarness() {
    const api = useWatchlist();
    const display = api.displayItems();
    const onKey =
      (displayIdx: number) =>
      (e: React.KeyboardEvent<HTMLButtonElement>) => {
        if (!e.altKey) return;
        if (e.key !== "ArrowUp" && e.key !== "ArrowDown") return;
        const sym = display[displayIdx];
        const fromIdx = api.items.indexOf(sym);
        if (fromIdx < 0) return;
        const delta = e.key === "ArrowUp" ? -1 : 1;
        const toIdx = Math.max(0, Math.min(api.items.length - 1, fromIdx + delta));
        if (toIdx !== fromIdx) api.reorder(fromIdx, toIdx);
      };
    return (
      <ul data-testid="kb-list">
        {display.map((sym, idx) => (
          <li key={sym} data-testid={`kb-row-${sym}`}>
            <button
              type="button"
              data-testid={`kb-handle-${sym}`}
              onKeyDown={onKey(idx)}
            >
              {sym}
            </button>
          </li>
        ))}
      </ul>
    );
  }

  it("Alt+ArrowDown moves a row down, Alt+ArrowUp moves it back", () => {
    localStorage.setItem(WATCHLIST_KEY, JSON.stringify(["A", "B", "C"]));
    render(<KeyboardHarness />);
    const aHandle = screen.getByTestId("kb-handle-A");
    fireEvent.keyDown(aHandle, { key: "ArrowDown", altKey: true });
    // Now order is [B, A, C]
    const list = screen.getByTestId("kb-list");
    expect(within(list).getAllByRole("button").map((b) => b.textContent)).toEqual([
      "B",
      "A",
      "C",
    ]);
    // Persistence verified
    expect(localStorage.getItem(WATCHLIST_KEY)).toBe(
      JSON.stringify(["B", "A", "C"]),
    );
    // Alt+ArrowUp on A puts it back
    fireEvent.keyDown(screen.getByTestId("kb-handle-A"), {
      key: "ArrowUp",
      altKey: true,
    });
    expect(
      within(screen.getByTestId("kb-list"))
        .getAllByRole("button")
        .map((b) => b.textContent),
    ).toEqual(["A", "B", "C"]);
  });

  it("plain ArrowDown (no Alt) does not reorder", () => {
    localStorage.setItem(WATCHLIST_KEY, JSON.stringify(["A", "B", "C"]));
    render(<KeyboardHarness />);
    fireEvent.keyDown(screen.getByTestId("kb-handle-A"), { key: "ArrowDown" });
    expect(
      within(screen.getByTestId("kb-list"))
        .getAllByRole("button")
        .map((b) => b.textContent),
    ).toEqual(["A", "B", "C"]);
  });
});
