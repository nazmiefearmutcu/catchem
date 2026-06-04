/**
 * v55 regression tests for useTheme's OS prefers-color-scheme auto-detect.
 *
 * Asserts:
 *   1. First run (no localStorage key) + OS prefers-light → theme="light"
 *   2. First run + OS prefers-dark → theme="dark"
 *   3. First run + matchMedia unavailable → theme="dark" (historical default)
 *   4. Explicit user choice "light" → honoured even when OS prefers dark
 *   5. Explicit user choice "dark" → honoured even when OS prefers light
 */
import { describe, it, expect, beforeEach, vi } from "vitest";
import { renderHook } from "@testing-library/react";
import { useTheme } from "@/hooks/useTheme";

function installLocalStorage(): Storage {
  const store = new Map<string, string>();
  const shim: Storage = {
    get length() { return store.size; },
    clear: () => store.clear(),
    getItem: (k) => (store.has(k) ? store.get(k)! : null),
    key: (i) => Array.from(store.keys())[i] ?? null,
    removeItem: (k) => { store.delete(k); },
    setItem: (k, v) => { store.set(k, String(v)); },
  };
  Object.defineProperty(window, "localStorage", { value: shim, configurable: true });
  return shim;
}

function mockMatchMedia(prefersLight: boolean) {
  Object.defineProperty(window, "matchMedia", {
    value: vi.fn().mockImplementation((query: string) => ({
      matches: query === "(prefers-color-scheme: light)" ? prefersLight : false,
      media: query,
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })),
    configurable: true,
  });
}

describe("useTheme — v55 OS auto-detect", () => {
  beforeEach(() => {
    installLocalStorage();
  });

  it("defaults to 'light' on first run when OS prefers light", () => {
    mockMatchMedia(true);
    const { result } = renderHook(() => useTheme());
    expect(result.current.theme).toBe("light");
  });

  it("defaults to 'dark' on first run when OS prefers dark", () => {
    mockMatchMedia(false);
    const { result } = renderHook(() => useTheme());
    expect(result.current.theme).toBe("dark");
  });

  it("falls back to 'dark' when matchMedia is unavailable", () => {
    Object.defineProperty(window, "matchMedia", { value: undefined, configurable: true });
    const { result } = renderHook(() => useTheme());
    expect(result.current.theme).toBe("dark");
  });

  it("honours explicit 'light' even when OS prefers dark", () => {
    mockMatchMedia(false);
    window.localStorage.setItem("catchem.theme", "light");
    const { result } = renderHook(() => useTheme());
    expect(result.current.theme).toBe("light");
  });

  it("honours explicit 'dark' even when OS prefers light", () => {
    mockMatchMedia(true);
    window.localStorage.setItem("catchem.theme", "dark");
    const { result } = renderHook(() => useTheme());
    expect(result.current.theme).toBe("dark");
  });
});
