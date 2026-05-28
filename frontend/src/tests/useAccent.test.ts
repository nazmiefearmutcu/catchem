import { describe, it, expect, beforeEach } from "vitest";
import { act, renderHook } from "@testing-library/react";
import {
  ACCENT_PRESETS,
  ACCENT_KEY,
  ACCENT_CUSTOM_LIGHT_KEY,
  ACCENT_CUSTOM_DARK_KEY,
  resolveAccent,
  useAccent,
} from "@/hooks/useAccent";

/**
 * Pins the accent-picker contract behind SettingsPage's AccentPickerCard:
 *   - Default preset is "blue" (matches globals.css :root --accent).
 *   - Picking a preset writes `catchem.accent` and injects a <style>
 *     overrider so `var(--accent)` reflects the new pair across themes.
 *   - "custom" mode persists separate light/dark hexes the user picks.
 *   - The injected <style> node is reused (idempotent), not duplicated.
 *   - Presets registry has exactly six entries with valid 7-char hex.
 *
 * jsdom in this project ships without a real Storage implementation —
 * see desktopAlerts.test.ts for the same shim pattern.
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

function removeOverrideNode() {
  const node = document.getElementById("catchem-accent-override");
  if (node) node.remove();
}

beforeEach(() => {
  installLocalStorage();
  removeOverrideNode();
  document.documentElement.classList.remove("dark");
});

describe("ACCENT_PRESETS catalogue", () => {
  it("ships exactly six presets", () => {
    expect(ACCENT_PRESETS).toHaveLength(6);
  });

  it("every preset has a valid #rrggbb hex pair", () => {
    const hex = /^#[0-9a-f]{6}$/i;
    for (const p of ACCENT_PRESETS) {
      expect(hex.test(p.light), `${p.id} light invalid: ${p.light}`).toBe(true);
      expect(hex.test(p.dark), `${p.id} dark invalid: ${p.dark}`).toBe(true);
    }
  });

  it("includes the documented preset ids", () => {
    const ids = ACCENT_PRESETS.map((p) => p.id);
    expect(ids).toEqual(["blue", "green", "purple", "orange", "red", "teal"]);
  });

  it("blue preset matches the original tokens.css defaults", () => {
    // Catches regressions where someone reshuffles the colour palette
    // and silently drifts the cockpit's "factory" colour away from the
    // #5fb3ff / #1e6fdd pair documented in styles/globals.css.
    const blue = ACCENT_PRESETS.find((p) => p.id === "blue")!;
    expect(blue.light).toBe("#1e6fdd");
    expect(blue.dark).toBe("#5fb3ff");
  });
});

describe("resolveAccent", () => {
  it("returns the preset pair for a known id", () => {
    expect(resolveAccent("green", "#000000", "#000000")).toEqual({
      light: "#0e8b51",
      dark: "#34d399",
    });
  });

  it("returns custom hexes when id === custom", () => {
    expect(resolveAccent("custom", "#abcdef", "#fedcba")).toEqual({
      light: "#abcdef",
      dark: "#fedcba",
    });
  });
});

describe("useAccent hook", () => {
  it("defaults to blue when nothing is persisted", () => {
    const { result } = renderHook(() => useAccent());
    expect(result.current.id).toBe("blue");
  });

  it("setId persists to localStorage and injects the override style", () => {
    const { result } = renderHook(() => useAccent());
    act(() => result.current.setId("green"));
    expect(window.localStorage.getItem(ACCENT_KEY)).toBe("green");
    const style = document.getElementById("catchem-accent-override") as HTMLStyleElement | null;
    expect(style).not.toBeNull();
    expect(style!.textContent).toContain("#34d399"); // green dark
    expect(style!.textContent).toContain("#0e8b51"); // green light
  });

  it("reads the persisted id on next mount", () => {
    window.localStorage.setItem(ACCENT_KEY, "purple");
    const { result } = renderHook(() => useAccent());
    expect(result.current.id).toBe("purple");
  });

  it("ignores corrupt stored ids and falls back to blue", () => {
    window.localStorage.setItem(ACCENT_KEY, "not-a-preset");
    const { result } = renderHook(() => useAccent());
    expect(result.current.id).toBe("blue");
  });

  it("reuses the same <style> node — does not duplicate it on rerender", () => {
    const { result } = renderHook(() => useAccent());
    act(() => result.current.setId("red"));
    act(() => result.current.setId("teal"));
    act(() => result.current.setId("orange"));
    const nodes = document.querySelectorAll("#catchem-accent-override");
    expect(nodes).toHaveLength(1);
  });

  it("custom mode persists separate light + dark hexes", () => {
    const { result } = renderHook(() => useAccent());
    act(() => result.current.setId("custom"));
    act(() => result.current.setCustomLight("#abcdef"));
    act(() => result.current.setCustomDark("#fedcba"));
    expect(window.localStorage.getItem(ACCENT_KEY)).toBe("custom");
    expect(window.localStorage.getItem(ACCENT_CUSTOM_LIGHT_KEY)).toBe("#abcdef");
    expect(window.localStorage.getItem(ACCENT_CUSTOM_DARK_KEY)).toBe("#fedcba");
    const style = document.getElementById("catchem-accent-override") as HTMLStyleElement | null;
    expect(style!.textContent).toContain("#abcdef");
    expect(style!.textContent).toContain("#fedcba");
  });
});
