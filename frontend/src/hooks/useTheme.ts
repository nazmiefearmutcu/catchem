import { useEffect, useCallback } from "react";

import { useStorageSync } from "@/lib/storage-sync";

const KEY = "catchem.theme";

export type Theme = "dark" | "light";

/**
 * Resolve the initial theme:
 *   1. If the user has explicitly set `catchem.theme` before, honour it.
 *   2. Otherwise probe the OS `prefers-color-scheme` media query and default
 *      to "light" if the OS is light, "dark" otherwise.
 *
 * The "light" branch only fires when there is NO prior choice AND the OS
 * media query reports light explicitly. Anything else (no matchMedia, no
 * preference, error) falls through to the historical "dark" default so
 * existing users never get yanked into light mode on upgrade.
 *
 * v55: previously the parse function ignored the raw value when it wasn't
 * "light" (returned "dark"). That dropped the "no prior choice" signal,
 * which is exactly what we need to detect for first-run OS auto-detect.
 * useStorageSync passes `null` to the parser when the key is missing — so
 * we now branch on null vs explicit value.
 */
function parseTheme(raw: string | null): Theme {
  if (raw === "dark" || raw === "light") return raw; // explicit user choice
  // First run or corrupted key: probe OS preference
  if (raw === null && typeof window !== "undefined" && typeof window.matchMedia === "function") {
    try {
      const prefersLight = window.matchMedia("(prefers-color-scheme: light)").matches;
      if (prefersLight) return "light";
    } catch {
      // Some sandboxed contexts throw on matchMedia — fall through.
    }
  }
  return "dark";
}

/**
 * Theme hook — persists to `catchem.theme` and applies the `<html>.dark`
 * classlist toggle on every change.
 *
 * Cross-window sync (v42): mounted in Tauri secondary analyst windows,
 * the underlying `useStorageSync` subscribes to the `storage` event so
 * a theme toggle in window A immediately re-renders window B AND the
 * classlist effect below re-applies the .dark class to window B's <html>.
 *
 * The persistence side-effect lives inside `useStorageSync.setValue`
 * (not a `useEffect`), so receiving a storage event in window B does NOT
 * trigger a write-back loop — only direct user toggles persist.
 */
export function useTheme() {
  const [theme, setTheme] = useStorageSync<Theme>(KEY, "dark", { parse: parseTheme });

  // Apply the classlist on every theme change — covers both user toggles
  // (window A) AND incoming storage events (window B).
  useEffect(() => {
    const root = document.documentElement;
    if (theme === "dark") root.classList.add("dark");
    else root.classList.remove("dark");
  }, [theme]);

  const toggle = useCallback(() => {
    setTheme(theme === "dark" ? "light" : "dark");
  }, [theme, setTheme]);

  return { theme, setTheme, toggle };
}
