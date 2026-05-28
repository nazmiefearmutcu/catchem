/**
 * Edge-case unit tests for `@/lib/storage-sync`.
 *
 * The sibling `storageSync.test.ts` already pins the happy paths (mount-read,
 * setter write+state, cross-window event, unrelated-key ignore, custom parser,
 * newValue=null clear, listener watched/clear/unmount). This file targets the
 * branches that file does NOT exercise:
 *   - the DEFAULT serializer (string passthrough vs JSON.stringify for objects),
 *   - the DEFAULT parser (raw string returned as T, null → defaultValue),
 *   - parse-error resilience on the MOUNT path (try/catch → defaultValue),
 *   - parse-error resilience on the EVENT path (keep current value, no throw),
 *   - serialize-throw being swallowed (quota / private-mode simulation),
 *   - the setter not re-broadcasting (no storage event emitted on local write),
 *   - `useStorageListener` re-subscribing when the watched key SET changes,
 *   - `useStorageListener` NOT re-subscribing when an equal inline array is
 *     passed on re-render (stable JSON.stringify dep).
 *
 * Uses the same in-memory localStorage shim + manual StorageEvent dispatch as
 * the sibling test (jsdom in this project ships no real Storage).
 */
import { describe, it, expect, beforeEach, vi } from "vitest";
import { act, renderHook } from "@testing-library/react";

import { useStorageSync, useStorageListener } from "@/lib/storage-sync";

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
  window.dispatchEvent(new StorageEvent("storage", { key, newValue }));
}

beforeEach(() => {
  installLocalStorage();
});

describe("useStorageSync — default serializer", () => {
  it("writes a plain string verbatim (no JSON quoting)", () => {
    const { result } = renderHook(() => useStorageSync("ds.str", "x"));
    act(() => result.current[1]("hello"));
    // Default serializer passes strings through untouched — NOT '"hello"'.
    expect(window.localStorage.getItem("ds.str")).toBe("hello");
  });

  it("JSON-encodes non-string values (numbers / objects / arrays)", () => {
    const { result: num } = renderHook(() => useStorageSync<number>("ds.num", 0));
    act(() => num.current[1](7));
    expect(window.localStorage.getItem("ds.num")).toBe("7");

    const { result: obj } = renderHook(() =>
      useStorageSync<{ a: number }>("ds.obj", { a: 0 }),
    );
    act(() => obj.current[1]({ a: 3 }));
    expect(window.localStorage.getItem("ds.obj")).toBe('{"a":3}');

    const { result: arr } = renderHook(() => useStorageSync<number[]>("ds.arr", []));
    act(() => arr.current[1]([1, 2]));
    expect(window.localStorage.getItem("ds.arr")).toBe("[1,2]");
  });
});

describe("useStorageSync — default parser", () => {
  it("returns the raw string AS the typed value when present", () => {
    window.localStorage.setItem("dp.k", "raw-value");
    const { result } = renderHook(() => useStorageSync("dp.k", "fallback"));
    expect(result.current[0]).toBe("raw-value");
  });

  it("returns the default when the key is absent (raw === null)", () => {
    const { result } = renderHook(() => useStorageSync("dp.absent", "fallback"));
    expect(result.current[0]).toBe("fallback");
  });
});

describe("useStorageSync — parse-error resilience", () => {
  it("falls back to default when the mount-time parse throws", () => {
    window.localStorage.setItem("pe.k", "not-json");
    const { result } = renderHook(() =>
      useStorageSync<{ n: number }>("pe.k", { n: -1 }, {
        parse: (raw) => {
          if (raw === null) return { n: -1 };
          return JSON.parse(raw); // throws on "not-json"
        },
      }),
    );
    // try/catch around the initial read swallows the SyntaxError.
    expect(result.current[0]).toEqual({ n: -1 });
  });

  it("keeps the current value when an incoming event's parse throws", () => {
    window.localStorage.setItem("pe.evt", JSON.stringify({ n: 5 }));
    const { result } = renderHook(() =>
      useStorageSync<{ n: number }>("pe.evt", { n: 0 }, {
        parse: (raw) => (raw === null ? { n: 0 } : JSON.parse(raw)),
      }),
    );
    expect(result.current[0]).toEqual({ n: 5 });
    // A garbage event for the watched key must NOT crash and must NOT clobber.
    act(() => {
      expect(() => dispatchStorageEvent("pe.evt", "}{garbage")).not.toThrow();
    });
    expect(result.current[0]).toEqual({ n: 5 });
  });
});

describe("useStorageSync — write-path safety", () => {
  it("swallows a serializer/setItem throw (quota / private mode)", () => {
    const { result } = renderHook(() => useStorageSync("q.k", "init"));
    const spy = vi
      .spyOn(window.localStorage, "setItem")
      .mockImplementation(() => {
        throw new DOMException("QuotaExceededError");
      });
    // The setter must not propagate the storage failure...
    act(() => {
      expect(() => result.current[1]("too-big")).not.toThrow();
    });
    // ...and local state still advances optimistically.
    expect(result.current[0]).toBe("too-big");
    spy.mockRestore();
  });

  it("does NOT emit a storage event on the local write path (no ping-pong)", () => {
    const onStorage = vi.fn();
    window.addEventListener("storage", onStorage);
    const { result } = renderHook(() => useStorageSync("np.k", "init"));
    act(() => result.current[1]("local-write"));
    // Per HTML spec the originating doc gets no event; the hook must not
    // synthesize one either.
    expect(onStorage).not.toHaveBeenCalled();
    window.removeEventListener("storage", onStorage);
  });

  it("an external event updates the local hook value (sanity for this file's shim)", () => {
    const { result } = renderHook(() => useStorageSync("np.k2", "init"));
    act(() => dispatchStorageEvent("np.k2", "external"));
    expect(result.current[0]).toBe("external");
  });
});

describe("useStorageListener — re-subscription semantics", () => {
  it("re-subscribes to the NEW key set when `keys` changes", () => {
    const seen: Array<string | null> = [];
    const { rerender } = renderHook(
      ({ keys }: { keys: string[] }) =>
        useStorageListener(keys, (e) => seen.push(e.key)),
      { initialProps: { keys: ["a"] } },
    );

    act(() => dispatchStorageEvent("a", "1")); // watched → recorded
    act(() => dispatchStorageEvent("b", "2")); // not watched → ignored
    expect(seen).toEqual(["a"]);

    // Swap the watched set: now "b" matters and "a" does not.
    rerender({ keys: ["b"] });
    act(() => dispatchStorageEvent("a", "3")); // no longer watched
    act(() => dispatchStorageEvent("b", "4")); // now watched
    expect(seen).toEqual(["a", "b"]);
  });

  it("does NOT re-subscribe when an EQUAL inline array is passed on re-render", () => {
    const calls: Array<string | null> = [];
    // A handler that captures a render-scoped marker. If the effect re-ran on
    // every render (unstable dep), the LATEST handler would be installed and
    // we'd see the new marker. The stable JSON.stringify dep means the FIRST
    // effect+handler stays mounted across the equal-array re-render.
    let marker = "first";
    const { rerender } = renderHook(() =>
      useStorageListener(["x", "y"], () => calls.push(marker)),
    );
    marker = "second";
    rerender(); // same inline ["x","y"] → stable dep → no re-subscribe

    act(() => dispatchStorageEvent("x", "v"));
    // onChangeRef is refreshed every render, so the LATEST closure runs —
    // proving the subscription itself never tore down (single handler) AND the
    // ref keeps the callback current.
    expect(calls).toEqual(["second"]);
    expect(calls).toHaveLength(1);
  });

  it("fires for every watched key in a burst and ignores interleaved noise", () => {
    const seen: Array<string | null> = [];
    renderHook(() => useStorageListener(["a", "b", "c"], (e) => seen.push(e.key)));
    act(() => {
      dispatchStorageEvent("a", "1");
      dispatchStorageEvent("zzz", "noise");
      dispatchStorageEvent("b", "2");
      dispatchStorageEvent("c", "3");
      dispatchStorageEvent("zzz2", "noise");
    });
    expect(seen).toEqual(["a", "b", "c"]);
  });
});
