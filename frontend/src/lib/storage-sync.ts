import { useEffect, useRef, useState } from "react";

/**
 * Cross-window localStorage sync helpers — v42.
 *
 * Browsers fire the `storage` event on OTHER same-origin documents (tabs,
 * windows, iframes) when localStorage changes. Tauri WebViews opened by the
 * v30 multi-window feature all share the same origin (`http://127.0.0.1:8087`)
 * so the spec-required behaviour kicks in: when the user toggles the theme,
 * picks an accent, or edits the watchlist in window A, window B's listener
 * fires and we can mirror the value without any extra IPC.
 *
 * Two flavours:
 *   - `useStorageSync<T>` — the full hook. Returns a [value, setter] tuple.
 *     Setting via the returned setter writes localStorage (so OTHER windows
 *     receive the event), updates local state, and skips the write on the
 *     receive-event path (no ping-pong).
 *   - `useStorageListener` — lower-level subscription for hooks that already
 *     manage their own state + persistence (e.g. `useAccent`, `useWatchlist`)
 *     and only need a callback when external windows mutate a key.
 *
 * Importantly: the dispatching window does NOT receive its own `storage`
 * event (per the HTML spec). Tests must `dispatchEvent` manually.
 */

/** Default parser: treat the raw string as the typed value. */
function defaultParse<T>(defaultValue: T) {
  return (raw: string | null): T => (raw === null ? defaultValue : (raw as unknown as T));
}

/** Default serializer: strings pass through; everything else is JSON-encoded. */
function defaultSerialize<T>(value: T): string {
  return typeof value === "string" ? value : JSON.stringify(value);
}

export interface UseStorageSyncOptions<T> {
  /** Parse `localStorage` raw value into T. Receives null when key is absent. */
  parse?: (raw: string | null) => T;
  /** Serialize T → string for `localStorage.setItem`. */
  serialize?: (value: T) => string;
}

/**
 * Subscribe to a localStorage key and keep React state in sync with both
 * local writes (via the returned setter) and external writes (from other
 * Tauri windows via the `storage` event).
 *
 * Per the HTML spec the originating document is NOT notified, so we update
 * state synchronously inside the setter to avoid a "value committed but UI
 * stale" gap.
 */
export function useStorageSync<T>(
  key: string,
  defaultValue: T,
  options: UseStorageSyncOptions<T> = {},
): [T, (value: T) => void] {
  const parse = options.parse ?? defaultParse<T>(defaultValue);
  const serialize = options.serialize ?? defaultSerialize<T>;

  // Hold stable refs so the storage listener doesn't re-subscribe when the
  // caller passes inline `parse`/`serialize` functions on every render.
  const parseRef = useRef(parse);
  const serializeRef = useRef(serialize);
  useEffect(() => {
    parseRef.current = parse;
    serializeRef.current = serialize;
  });

  const [value, setValueInternal] = useState<T>(() => {
    if (typeof localStorage === "undefined") return defaultValue;
    try {
      return parse(localStorage.getItem(key));
    } catch {
      return defaultValue;
    }
  });

  // Other-window storage event → mirror the change locally. No localStorage
  // write on this path: the originating window already persisted the value.
  useEffect(() => {
    if (typeof window === "undefined") return;
    const onStorage = (e: StorageEvent) => {
      if (e.key !== key) return;
      try {
        setValueInternal(parseRef.current(e.newValue));
      } catch {
        /* ignore parse errors — keep current value */
      }
    };
    window.addEventListener("storage", onStorage);
    return () => {
      window.removeEventListener("storage", onStorage);
    };
  }, [key]);

  const setValue = (next: T) => {
    setValueInternal(next);
    try {
      if (typeof localStorage !== "undefined") {
        localStorage.setItem(key, serializeRef.current(next));
      }
    } catch {
      /* quota / private mode — silent */
    }
  };

  return [value, setValue];
}

/**
 * Watch a list of keys and invoke `onChange` whenever ANOTHER window mutates
 * one of them. The dispatching window's own writes do NOT trigger the
 * callback (browser behaviour — we don't simulate it).
 *
 * Useful for hooks (`useAccent`, `useWatchlist`) that already own state +
 * persistence and just need to know when an external window has touched
 * their slot so they can re-read it.
 */
export function useStorageListener(
  keys: ReadonlyArray<string>,
  onChange: (event: StorageEvent) => void,
): void {
  const onChangeRef = useRef(onChange);
  useEffect(() => {
    onChangeRef.current = onChange;
  });

  useEffect(() => {
    if (typeof window === "undefined") return;
    const watched = new Set(keys);
    const handler = (e: StorageEvent) => {
      if (e.key === null) {
        // localStorage.clear() — fires once with key === null. Treat as a
        // signal to refresh all watched keys.
        onChangeRef.current(e);
        return;
      }
      if (!watched.has(e.key)) return;
      onChangeRef.current(e);
    };
    window.addEventListener("storage", handler);
    return () => {
      window.removeEventListener("storage", handler);
    };
    // We intentionally key the effect on a stable serialization so a stable
    // reference avoids re-subscribing when callers pass inline arrays.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [JSON.stringify(keys)]);
}
