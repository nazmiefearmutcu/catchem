import { useEffect, useState, useCallback } from "react";

import { useStorageListener } from "@/lib/storage-sync";

/**
 * Theme-accent picker hook.
 *
 * The `--accent` CSS variable in `styles/globals.css` drives every
 * accent-toned surface: hero gradients, focus rings, the skip-link,
 * the brand monogram, ToggleSwitch on-state, hover lifts, .btn-accent,
 * shimmer middle-stop tint, etc. Tailwind's `bg-accent` / `border-accent`
 * utility classes also read `var(--accent)` after the tailwind.config.js
 * change that pairs with this hook.
 *
 * Persistence:
 *   - `catchem.accent`              → preset id ("blue"|"green"|…|"custom")
 *   - `catchem.accent.custom.light` → user-chosen light-theme hex
 *   - `catchem.accent.custom.dark`  → user-chosen dark-theme hex
 *
 * The hook injects a single `<style id="catchem-accent-override">` tag
 * in <head>. It rewrites `:root { --accent }` and `:root.light { --accent }`
 * so the override slots in front of globals.css's defaults regardless of
 * which theme is active. The injection is idempotent — repeated effect
 * runs reuse the same node.
 *
 * Reduced motion: the accent change is an instant CSS variable swap.
 * Browsers don't animate CSS variable transitions by default, and we
 * never attach a `transition: color` on `var(--accent)` directly.
 *
 * Cross-window sync (v42): mounted in every Tauri analyst window. When
 * window A changes the accent, window B receives a `storage` event for
 * the relevant key, re-reads the value, and re-injects the <style>
 * override — keeping `var(--accent)` in lock-step across all windows.
 * The persistence write only fires when the new value differs from
 * what's already on disk, so the receive path doesn't re-broadcast
 * the same value back.
 */

export const ACCENT_PRESETS = [
  { id: "blue", light: "#1e6fdd", dark: "#5fb3ff" },
  { id: "green", light: "#0e8b51", dark: "#34d399" },
  { id: "purple", light: "#7c3aed", dark: "#a78bfa" },
  { id: "orange", light: "#c2410c", dark: "#fb923c" },
  { id: "red", light: "#b91c1c", dark: "#f87171" },
  { id: "teal", light: "#0d7777", dark: "#5eead4" },
] as const;

export type PresetId = (typeof ACCENT_PRESETS)[number]["id"];
export type AccentId = PresetId | "custom";

export const ACCENT_KEY = "catchem.accent";
export const ACCENT_CUSTOM_LIGHT_KEY = "catchem.accent.custom.light";
export const ACCENT_CUSTOM_DARK_KEY = "catchem.accent.custom.dark";

const STYLE_NODE_ID = "catchem-accent-override";
const DEFAULT_CUSTOM_LIGHT = "#1e6fdd";
const DEFAULT_CUSTOM_DARK = "#5fb3ff";

const VALID_IDS = new Set<AccentId>([
  ...ACCENT_PRESETS.map((p) => p.id),
  "custom",
]);

function readId(): AccentId {
  try {
    const raw = window.localStorage.getItem(ACCENT_KEY);
    if (raw && VALID_IDS.has(raw as AccentId)) return raw as AccentId;
  } catch {
    /* ignore — SSR, blocked storage, etc. */
  }
  return "blue";
}

function readCustomLight(): string {
  try {
    return window.localStorage.getItem(ACCENT_CUSTOM_LIGHT_KEY) ?? DEFAULT_CUSTOM_LIGHT;
  } catch {
    return DEFAULT_CUSTOM_LIGHT;
  }
}

function readCustomDark(): string {
  try {
    return window.localStorage.getItem(ACCENT_CUSTOM_DARK_KEY) ?? DEFAULT_CUSTOM_DARK;
  } catch {
    return DEFAULT_CUSTOM_DARK;
  }
}

/**
 * Resolve the (light, dark) hex pair for a given id and current custom
 * values. Falls back to the "blue" preset if a stored id is corrupted
 * past the validity check (defensive — `readId()` already gates this).
 */
export function resolveAccent(
  id: AccentId,
  customLight: string,
  customDark: string,
): { light: string; dark: string } {
  if (id === "custom") return { light: customLight, dark: customDark };
  const preset = ACCENT_PRESETS.find((p) => p.id === id) ?? ACCENT_PRESETS[0];
  return { light: preset.light, dark: preset.dark };
}

/**
 * Inject (or update) the `<style>` override block so `var(--accent)`
 * reflects the chosen colour pair across both themes. Idempotent: the
 * second call with the same input rewrites the same node's textContent.
 */
function applyAccentOverride(light: string, dark: string) {
  if (typeof document === "undefined") return;
  let styleEl = document.getElementById(STYLE_NODE_ID) as HTMLStyleElement | null;
  if (!styleEl) {
    styleEl = document.createElement("style");
    styleEl.id = STYLE_NODE_ID;
    document.head.appendChild(styleEl);
  }
  // Dark is the default (matches `:root` block); light overrides when
  // <html> does NOT carry the .dark class (mirrors useTheme().theme==="light").
  styleEl.textContent =
    `:root { --accent: ${dark}; }\n` +
    `:root:not(.dark) { --accent: ${light}; }\n`;
}

/** Write only if the persisted value would actually change. */
function writeIfChanged(key: string, value: string) {
  try {
    if (window.localStorage.getItem(key) === value) return;
    window.localStorage.setItem(key, value);
  } catch {
    /* ignore */
  }
}

export function useAccent() {
  const [id, setIdState] = useState<AccentId>(() => readId());
  const [customLight, setCustomLightState] = useState<string>(() => readCustomLight());
  const [customDark, setCustomDarkState] = useState<string>(() => readCustomDark());

  useEffect(() => {
    const { light, dark } = resolveAccent(id, customLight, customDark);
    applyAccentOverride(light, dark);
    // Guarded writes — when this effect ran because of an incoming storage
    // event, the values already match what's on disk so setItem is a no-op.
    // Otherwise it persists and rebroadcasts to other windows.
    writeIfChanged(ACCENT_KEY, id);
    if (id === "custom") {
      writeIfChanged(ACCENT_CUSTOM_LIGHT_KEY, customLight);
      writeIfChanged(ACCENT_CUSTOM_DARK_KEY, customDark);
    }
  }, [id, customLight, customDark]);

  // Cross-window sync — any OTHER analyst window changing one of our keys
  // triggers a re-read so this window mirrors the value (and re-injects
  // the override via the effect above).
  useStorageListener(
    [ACCENT_KEY, ACCENT_CUSTOM_LIGHT_KEY, ACCENT_CUSTOM_DARK_KEY],
    (e) => {
      if (e.key === ACCENT_KEY || e.key === null) {
        setIdState(readId());
      }
      if (e.key === ACCENT_CUSTOM_LIGHT_KEY || e.key === null) {
        setCustomLightState(readCustomLight());
      }
      if (e.key === ACCENT_CUSTOM_DARK_KEY || e.key === null) {
        setCustomDarkState(readCustomDark());
      }
    },
  );

  const setId = useCallback((next: AccentId) => setIdState(next), []);
  const setCustomLight = useCallback((hex: string) => setCustomLightState(hex), []);
  const setCustomDark = useCallback((hex: string) => setCustomDarkState(hex), []);

  return { id, setId, customLight, setCustomLight, customDark, setCustomDark };
}
