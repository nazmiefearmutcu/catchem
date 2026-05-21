import { describe, it, expect } from "vitest";
import {
  NAV_SHORTCUTS,
  chordLabel,
  resolveShortcut,
} from "@/lib/nav-shortcuts";
import { SHORTCUTS as HELP_SHORTCUTS } from "@/features/help/HelpPage";
import { SHORTCUTS as SETTINGS_SHORTCUTS } from "@/features/settings/SettingsPage";
import { NAV as PALETTE_NAV } from "@/components/CommandPalette";

/**
 * Round 7 truth bug pins. Before this round HelpPage advertised
 *   - `g m → Model Controls`  (actual: `g m → /map`)
 *   - `g s → Settings`        (actual: `g s → /symbols`)
 * and SettingsPage used `g m → Market Map` while the palette used
 * `g a → Analysis (Map)` — three documentation surfaces, two different
 * chords for the same route. These tests make any future drift a CI
 * failure.
 */

const KNOWN_PATHS = new Set(NAV_SHORTCUTS.map((s) => s.path));

const parseChord = (label: string): { kind: "g" | "other"; key: string } | null => {
  // Strip whitespace runs so "g  ," still parses.
  const tidy = label.replace(/\s+/g, " ").trim();
  if (!tidy.startsWith("g ")) return { kind: "other", key: tidy };
  const rest = tidy.slice(2);
  // Multi-key chords (e.g. "g , ") collapse to first non-space char.
  return { kind: "g", key: rest.charAt(0) };
};

describe("NAV_SHORTCUTS canonical registry", () => {
  it("has no duplicate keys", () => {
    const seen = new Set<string>();
    for (const s of NAV_SHORTCUTS) {
      expect(seen.has(s.key)).toBe(false);
      seen.add(s.key);
      if (s.alias) {
        expect(seen.has(s.alias)).toBe(false);
        seen.add(s.alias);
      }
    }
  });

  it("resolveShortcut returns the documented path for each entry", () => {
    for (const s of NAV_SHORTCUTS) {
      expect(resolveShortcut(s.key)).toBe(s.path);
      if (s.alias) expect(resolveShortcut(s.alias)).toBe(s.path);
    }
  });

  it("resolveShortcut returns null for unknown keys", () => {
    expect(resolveShortcut("z")).toBeNull();
    expect(resolveShortcut("?")).toBeNull();
    expect(resolveShortcut("")).toBeNull();
  });

  it("aliases the legacy `g m` chord to `/map` so muscle memory still routes", () => {
    // Before Round 7, SettingsPage advertised `g m`. Settings now uses
    // canonical `g a` but the alias keeps the old chord functional.
    expect(resolveShortcut("m")).toBe("/map");
  });
});

describe("HelpPage SHORTCUTS docs match the canonical handler", () => {
  it("every `g X` row parses to a key that resolves to a real path", () => {
    for (const row of HELP_SHORTCUTS) {
      const parsed = parseChord(row.keys);
      if (!parsed || parsed.kind !== "g") continue;
      const target = resolveShortcut(parsed.key);
      expect(target, `HelpPage row "${row.keys}" (${row.description}) maps to unknown key "${parsed.key}"`).not.toBeNull();
      expect(KNOWN_PATHS.has(target!)).toBe(true);
    }
  });

  it("covers every canonical chord (no missing rows)", () => {
    const advertised = new Set(
      HELP_SHORTCUTS
        .map((r) => parseChord(r.keys))
        .filter((p): p is { kind: "g"; key: string } => p?.kind === "g")
        .map((p) => p.key),
    );
    for (const s of NAV_SHORTCUTS) {
      expect(advertised.has(s.key), `HelpPage missing canonical chord ${chordLabel(s)} (${s.label})`).toBe(true);
    }
  });

  it("does not advertise the wrong descriptions any more", () => {
    // Regression pin for the two specific Round 7 bugs.
    const findRow = (keys: string) => HELP_SHORTCUTS.find((r) => r.keys === keys);
    const m = findRow("g m");
    // Either `g m` is gone (preferred) or it correctly labels Analysis/Market Map.
    if (m) expect(m.description.toLowerCase()).not.toContain("model controls");
    const s = findRow("g s");
    if (s) expect(s.description.toLowerCase()).not.toBe("settings");
  });
});

describe("SettingsPage SHORTCUTS docs match the canonical handler", () => {
  it("every `g X` row parses to a key that resolves to a real path", () => {
    for (const row of SETTINGS_SHORTCUTS) {
      const parsed = parseChord(row.keys);
      if (!parsed || parsed.kind !== "g") continue;
      const target = resolveShortcut(parsed.key);
      expect(target, `SettingsPage row "${row.keys}" (${row.description}) maps to unknown key`).not.toBeNull();
      expect(KNOWN_PATHS.has(target!)).toBe(true);
    }
  });

  it("uses the canonical `g a` chord for Analysis (not the old `g m`)", () => {
    // Before Round 7 the row read `g m → Go to Market Map`. We canonicalize
    // on `g a` so the palette/help/settings all agree. `m` still works as
    // an alias in the handler.
    const a = SETTINGS_SHORTCUTS.find((r) => r.keys === "g a");
    expect(a, "expected an entry for `g a`").toBeDefined();
  });
});

describe("CommandPalette NAV matches the canonical registry", () => {
  it("every routed entry has a matching canonical shortcut", () => {
    for (const entry of PALETTE_NAV) {
      if (!entry.path.startsWith("/")) continue;
      if (entry.path === "/legacy") continue; // intentional non-shortcut entry
      expect(KNOWN_PATHS.has(entry.path), `palette entry path ${entry.path} not in canonical NAV_SHORTCUTS`).toBe(true);
      if (entry.kbd) {
        const parsed = parseChord(entry.kbd);
        if (parsed?.kind === "g") {
          expect(resolveShortcut(parsed.key)).toBe(entry.path);
        }
      }
    }
  });
});
