/**
 * Canonical keyboard-shortcut + palette-entry registry.
 *
 * Before Round 7, four surfaces each carried their own copy of the
 * `g <key>` chord table — Shell's handler, the command palette, HelpPage
 * docs, and SettingsPage docs. They drifted: HelpPage advertised
 * `g m → Model Controls` and `g s → Settings` when the actual Shell
 * handler routed `g m → /map` (Analysis) and `g s → /symbols` (Symbols),
 * and SettingsPage used `g m` while the palette used `g a` for the same
 * Analysis route. Round 7 collapses the four copies onto this module so
 * the docs can no longer disagree with the handler.
 */

export interface NavShortcut {
  /** The second key in the `g <key>` chord. Lowercase, single character. */
  key: string;
  /** Target route (matches an entry in App.tsx). */
  path: string;
  /** User-facing label used in palette + docs. */
  label: string;
  /**
   * Optional second key that triggers the same action. We keep `m` aliased
   * to `a` so older muscle memory still routes to the analysis map after
   * the canonical chord moved to `g a`.
   */
  alias?: string;
}

export const NAV_SHORTCUTS: NavShortcut[] = [
  { key: "o", path: "/",                label: "Overview" },
  { key: "f", path: "/feed",            label: "Live Feed" },
  { key: "r", path: "/replay",          label: "Replay/Upload" },
  { key: "a", path: "/map",             label: "Analysis Map", alias: "m" },
  { key: "s", path: "/symbols",         label: "Symbols" },
  { key: "b", path: "/benchmark",       label: "Benchmark" },
  { key: "c", path: "/model-controls",  label: "Model Controls" },
  { key: "x", path: "/ops",             label: "System / Ops" },
  { key: "h", path: "/help",            label: "Help" },
  { key: ",", path: "/settings",        label: "Settings" },
];

/**
 * Resolve a single key (the second key in `g <key>`) to a target route.
 * Returns null if the key is not a recognized shortcut.
 *
 * Used by Shell.tsx's keydown handler. Tests cross-check by walking the
 * doc arrays in HelpPage / SettingsPage / CommandPalette and asserting
 * every advertised chord parses back to a known path.
 */
export function resolveShortcut(key: string): string | null {
  const k = key.toLowerCase();
  for (const s of NAV_SHORTCUTS) {
    if (s.key === k) return s.path;
    if (s.alias === k) return s.path;
  }
  return null;
}

/** Pretty `g X` chord label for docs + palette. */
export function chordLabel(spec: NavShortcut): string {
  return `g ${spec.key}`;
}
