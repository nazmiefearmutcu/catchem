import { describe, it, expect, beforeEach } from "vitest";
import { act, renderHook } from "@testing-library/react";

import { useStorageSync, useStorageListener } from "@/lib/storage-sync";
import { useTheme } from "@/hooks/useTheme";
import { ACCENT_KEY, useAccent } from "@/hooks/useAccent";
import { WATCHLIST_KEY, useWatchlist } from "@/features/quant/useWatchlist";

/**
 * Pins the cross-window storage sync contract for v42:
 *   - Hook reads the persisted value on mount.
 *   - Setting via the returned setter writes to localStorage AND updates
 *     local state synchronously (no event round-trip).
 *   - `storage` events from OTHER windows (simulated via dispatchEvent)
 *     update the hook's value without re-broadcasting.
 *   - Events for unrelated keys are ignored.
 *   - `useStorageListener` fires for each watched key and ignores others.
 *
 * The same `installLocalStorage` shim used by useAccent / Watchlist tests
 * is used here — jsdom in this project doesn't ship a real Storage.
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

function dispatchStorageEvent(key: string | null, newValue: string | null) {
  // jsdom's StorageEvent constructor honours the StorageEventInit dict —
  // see https://html.spec.whatwg.org/multipage/webstorage.html#the-storageevent-interface
  const ev = new StorageEvent("storage", { key, newValue });
  window.dispatchEvent(ev);
}

beforeEach(() => {
  installLocalStorage();
});

describe("useStorageSync", () => {
  it("returns the default value when nothing is persisted", () => {
    const { result } = renderHook(() => useStorageSync("test.k", "fallback"));
    expect(result.current[0]).toBe("fallback");
  });

  it("reads the persisted string value on mount", () => {
    window.localStorage.setItem("test.k", "stored");
    const { result } = renderHook(() => useStorageSync("test.k", "fallback"));
    expect(result.current[0]).toBe("stored");
  });

  it("setter writes localStorage and updates local state immediately", () => {
    const { result } = renderHook(() => useStorageSync("test.k", "fallback"));
    act(() => result.current[1]("written"));
    expect(window.localStorage.getItem("test.k")).toBe("written");
    expect(result.current[0]).toBe("written");
  });

  it("incoming storage event for the watched key updates the value", () => {
    const { result } = renderHook(() => useStorageSync("test.k", "fallback"));
    act(() => {
      dispatchStorageEvent("test.k", "from-other-window");
    });
    expect(result.current[0]).toBe("from-other-window");
  });

  it("ignores storage events for unrelated keys", () => {
    const { result } = renderHook(() => useStorageSync("test.k", "fallback"));
    act(() => {
      dispatchStorageEvent("some.other.key", "noise");
    });
    expect(result.current[0]).toBe("fallback");
  });

  it("honours a custom parser for typed values", () => {
    window.localStorage.setItem("test.json", JSON.stringify({ n: 7 }));
    const { result } = renderHook(() =>
      useStorageSync<{ n: number }>("test.json", { n: 0 }, {
        parse: (raw) => (raw === null ? { n: 0 } : JSON.parse(raw)),
        serialize: (v) => JSON.stringify(v),
      }),
    );
    expect(result.current[0]).toEqual({ n: 7 });
    act(() => result.current[1]({ n: 42 }));
    expect(window.localStorage.getItem("test.json")).toBe(JSON.stringify({ n: 42 }));
  });

  it("clears to default when storage event delivers newValue=null", () => {
    window.localStorage.setItem("test.k", "stored");
    const { result } = renderHook(() => useStorageSync("test.k", "fallback"));
    expect(result.current[0]).toBe("stored");
    act(() => {
      dispatchStorageEvent("test.k", null);
    });
    expect(result.current[0]).toBe("fallback");
  });
});

describe("useStorageListener", () => {
  it("fires onChange only for watched keys", () => {
    const seen: Array<string | null> = [];
    renderHook(() =>
      useStorageListener(["a", "b"], (e) => {
        seen.push(e.key);
      }),
    );
    act(() => {
      dispatchStorageEvent("a", "1");
      dispatchStorageEvent("ignored", "2");
      dispatchStorageEvent("b", "3");
    });
    expect(seen).toEqual(["a", "b"]);
  });

  it("fires on key === null (localStorage.clear) regardless of watched list", () => {
    const seen: Array<string | null> = [];
    renderHook(() =>
      useStorageListener(["a"], (e) => {
        seen.push(e.key);
      }),
    );
    act(() => {
      dispatchStorageEvent(null, null);
    });
    expect(seen).toEqual([null]);
  });

  it("unsubscribes on unmount", () => {
    const seen: Array<string | null> = [];
    const { unmount } = renderHook(() =>
      useStorageListener(["a"], (e) => {
        seen.push(e.key);
      }),
    );
    unmount();
    act(() => {
      dispatchStorageEvent("a", "post-unmount");
    });
    expect(seen).toEqual([]);
  });
});

describe("cross-window hook sync (v42)", () => {
  beforeEach(() => {
    document.documentElement.classList.remove("dark");
    const node = document.getElementById("catchem-accent-override");
    if (node) node.remove();
  });

  it("useTheme: incoming storage event flips classlist + state", () => {
    const { result } = renderHook(() => useTheme());
    expect(result.current.theme).toBe("dark");
    expect(document.documentElement.classList.contains("dark")).toBe(true);
    act(() => {
      dispatchStorageEvent("catchem.theme", "light");
    });
    expect(result.current.theme).toBe("light");
    expect(document.documentElement.classList.contains("dark")).toBe(false);
  });

  it("useAccent: incoming storage event re-reads id and re-injects override", () => {
    const { result } = renderHook(() => useAccent());
    expect(result.current.id).toBe("blue");
    // Simulate window A's write: persist the value, then dispatch the event
    // that window B's browser would receive.
    window.localStorage.setItem(ACCENT_KEY, "purple");
    act(() => {
      dispatchStorageEvent(ACCENT_KEY, "purple");
    });
    expect(result.current.id).toBe("purple");
    const style = document.getElementById("catchem-accent-override") as HTMLStyleElement | null;
    expect(style).not.toBeNull();
    expect(style!.textContent).toContain("#a78bfa"); // purple dark
  });

  it("useWatchlist: incoming storage event updates items in-place", () => {
    const { result } = renderHook(() => useWatchlist());
    expect(result.current.items).toEqual([]);
    window.localStorage.setItem(WATCHLIST_KEY, JSON.stringify(["NVDA", "BTC-USD"]));
    act(() => {
      dispatchStorageEvent(WATCHLIST_KEY, JSON.stringify(["NVDA", "BTC-USD"]));
    });
    expect(result.current.items).toEqual(["NVDA", "BTC-USD"]);
  });
});
